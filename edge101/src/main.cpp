// EDGE 101 (DFR1283, ESP32 原版) 分拣控制器 — 实现 docs/sort-mqtt.md
// 板载 RJ45 以太网 + 步进 (STEP=14 DIR=5, EN 未接线/浮空实测可用)
// + 3 路舵机 (LEDC PWM 直连, 12/15/33) + HuskyLens 2 (I2C 18/23, 预留)
// + 2Hz 遥测
//
// 引脚/网络全部在 include/config.h (依据 DFRobot wiki DFR1283).
// 纯逻辑 (状态机/解析/协议 JSON) 在 lib/SortLogic, 主机可单测.
#include <Arduino.h>
#include <Wire.h>
#include <ETH.h>
#include <WiFi.h>
#include <NetworkClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <esp_netif.h>
#include "driver/rmt_tx.h"
#include "driver/rmt_encoder.h"
#include "driver/rmt_common.h"

#include "config.h"
#include "Conveyor.h"
#include "Sorter.h"
#include "Gate.h"
#include "Track.h"
#include "HuskyLens.h"
#include "Proto.h"
#include "McpEn.h"
#include "McpGate.h"
#include "BeltHold.h"
#include "BeltWatch.h"

static sort::Conveyor conv;
static sort::BeltHold beltHold;   // 推料期间停带 (扫掠中 hold, 扫掠结束自动恢复)
static sort::Sorter sorters[3] = {sort::Sorter(1), sort::Sorter(2), sort::Sorter(3)};
#if HL2_ON
static sort::HuskyLens hl2(MATERIAL_MAP, MATERIAL_MAP_LEN, HL2_RETRIGGER_MS);
#endif
static sort::Track track;         // 带面位置跟踪 (虚拟光门; 配置见 config.h)
static sort::BeltWatch beltWatch(GATE_DIST_MM[0], GATE_DIST_MM[1],
                                 GATE_DIST_MM[2], 1.0);  // 带速估计+打滑/卡滞 (物理光门)
static bool beltFaultSent = false;

#if HL2_ON
static HardwareSerial hlSerial(2);            // UART2 重映射到 GPIO5/12 (本轮未用)
#endif
static NetworkClient ethClient;               // 3.x: 以太网客户端 (自动绑定默认 netif)
static PubSubClient mqtt(ethClient);

// MCP23017 (步进 EN + 预留数字量): TwoWire 实现 I2cReg 传输
struct WireMcpBus : public sort::I2cReg {
    bool alive() override {
        Wire.beginTransmission(sort::McpEn::kAddr);
        Wire.write(sort::McpEn::kIodirA);
        Wire.write(0x00);                     // IODIR=全输入 (默认值, 探测写无害)
        return Wire.endTransmission() == 0;
    }
    void writeReg(uint8_t reg, uint8_t val) override {
        Wire.beginTransmission(sort::McpEn::kAddr);
        Wire.write(reg);
        Wire.write(val);
        Wire.endTransmission();
    }
    uint8_t readReg(uint8_t reg) override {
        Wire.beginTransmission(sort::McpEn::kAddr);
        Wire.write(reg);
        if (Wire.endTransmission(false) != 0 ||
            Wire.requestFrom(sort::McpEn::kAddr, 1) < 1)
            return 0xFF;
        return (uint8_t)Wire.read();
    }
};
static WireMcpBus mcpBus;
static sort::McpEn mcpEn;
static sort::McpGate mcpGate;          // 光门 Port B (PB0/1/2 -> 1/2/3 号)
static sort::PhotoGate gates[3];
static bool gateOk = false;            // 光门通道在线 (MCP 在线 + 配置成功)

// 以太网状态 (tryStaticIp/applyStaticIp 见下文以太网节; 提前声明供事件回调使用)
static volatile bool ethUp = false;    // 链路可用 (静态 IP 已配好或 DHCP 拿到)
static bool netifReady = false;        // 静态 IP 已设置 (幂等, 不重复设置)
static esp_netif_t* findEthNetif();    // 定义见以太网节 (串口 eth 命令提前引用)

// ================= 步进: RMT 硬件脉冲 (主循环旧路径作回退) =================
// 主循环 digitalWrite 打 STEP 只能出 ~3kHz, 名义 320k pps (speed_mps=1)
// 完全打不出来 -> 改用 RMT TX 通道: 写一个 [pulse, gap] 符号, loop_count=-1
// 无限循环, 硬件按 APB 80MHz 时钟精确出 50% 占空比脉冲串; 主循环只负责
// 在传送带离开 running 时 rmt_disable. 停带精度 = 主循环周期 (~1ms),
// 1m/s 下 ±1mm 量级, 对"按件分拣"的带速无影响.
static rmt_channel_handle_t stepChan = nullptr;
static rmt_encoder_handle_t stepEnc = nullptr;
static bool stepRmtReady = false;      // 通道/编码器创建成功
static bool stepRmtFailed = false;     // 运行中 RMT 出错 -> 回退主循环打脉冲
static bool stepRmtActive = false;
static uint32_t stepRmtStartMs = 0;
static double stepRmtPps = 0.0;

// 回退路径: 主循环非阻塞单脉冲 (旧逻辑, 只保证 RMT 不可用时仍有带速)
static uint32_t nextStepUs = 0;
static void steerStepperLegacy() {
    if (conv.st != sort::DevState::kRunning) return;
    if (beltHold.hold > 0) return;   // 推料期间停带 (扫掠结束 hold 归零即自动恢复)
    double stepsPerSec = conv.speed_mps * 1000.0 * STEPS_PER_MM;
    if (stepsPerSec < 1.0) stepsPerSec = 1.0;
    uint32_t interval = (uint32_t)(1000000.0 / stepsPerSec);
    uint32_t now = micros();
    if (now >= nextStepUs) {
        nextStepUs = now + interval;
        digitalWrite(STEPPER_STEP_PIN, HIGH);
        delayMicroseconds(2);
        digitalWrite(STEPPER_STEP_PIN, LOW);
        conv.steps_total++;
    }
}

static void stopStepRmt() {
    if (!stepRmtActive || !stepChan) return;
    stepRmtActive = false;
    // 步数按 pps × 时长结算 (停带时刻误差 = 主循环周期)
    conv.steps_total +=
        (uint64_t)(stepRmtPps * (millis() - stepRmtStartMs) / 1000.0);
    rmt_disable(stepChan);
}

static bool startStepRmt() {
    double pps = conv.speed_mps * 1000.0 * STEPS_PER_MM;
    if (pps < 1000.0) return false;   // <1 步/s 不起 RMT
    uint32_t period = (uint32_t)(80000000.0 / pps);   // 80MHz 分辨率
    if (period < 10 || period > 32766) return false;  // 每段 15bit
    stopStepRmt();                    // 变速/重入: 先停旧的再按新速率起
    rmt_symbol_word_t sym = {};
    sym.duration0 = period / 2;       // 50% 占空比
    sym.level0 = 1;
    sym.duration1 = period - period / 2;
    sym.level1 = 0;
    rmt_transmit_config_t tc = {};
    tc.loop_count = -1;               // 无限循环, 由 stopStepRmt() 收
    esp_err_t e1 = rmt_enable(stepChan);
    if (e1 != ESP_OK) {
        stepRmtFailed = true;
        Serial.printf("[step] rmt_enable 失败 %d, 回退主循环打脉冲\n", (int)e1);
        return false;
    }
    esp_err_t e2 = rmt_transmit(stepChan, stepEnc, &sym, sizeof(sym), &tc);
    if (e2 != ESP_OK) {
        rmt_disable(stepChan);
        stepRmtFailed = true;
        Serial.printf("[step] rmt_transmit 失败 %d (period=%lu), 回退主循环\n",
                      (int)e2, (unsigned long)period);
        return false;
    }
    stepRmtStartMs = millis();
    stepRmtPps = pps;
    stepRmtActive = true;
    Serial.printf("[step] RMT 启动 pps=%.0f\n", pps);
    return true;
}

// loop() 入口: RMT 正常时只看守门 (传送带离 running 即停), 否则回退旧路径
static void steerStepper() {
    if (stepRmtReady && !stepRmtFailed) {
        if (conv.st != sort::DevState::kRunning && stepRmtActive) stopStepRmt();
        return;
    }
    steerStepperLegacy();
}

// 遥测用的实时步数 (含 RMT 运行中的部分)
static uint64_t stepsLive() {
    uint64_t s = conv.steps_total;
    if (stepRmtActive)
        s += (uint64_t)(stepRmtPps * (millis() - stepRmtStartMs) / 1000.0);
    return s;
}

// ================= 舵机扫掠任务 (非阻塞, 每分拣器 1 路, 可并发) =================
// 一件物料 = 一次 拨出->归位 扫掠; qty=N 则 N 次, 件间 SERVO_ITEM_GAP_MS.
// 与 sim_devices 语义一致: 全部拨完才发 ack ok.
// 3 个分拣器各占 1 路 LEDC PWM (GPIO12/15/33), 光门触发下可能同时拨, 各自独立任务.
struct SweepTask {
    int n = 0;             // 分拣器 1..3
    bool active = false;
    bool inEject = false;
    uint32_t phaseAt = 0;
};
static SweepTask sweeps[3] = {};
static String lastJobId[3] = {"", "", ""};   // 各分拣器当前 job_id (divert 完成时应答用)
// 防重复执行: 最近一次完成的 divert 的 job_id. 网关重发/QoS1 重投同 job_id 时
// 只补应答不重拨 (job_id 每个批次唯一, 窗口内重复必为重发).
static std::string lastDoneJob[3];
static uint32_t lastDoneAtMs[3] = {0, 0, 0};

// 舵机: LEDC 硬件 PWM 直连 (arduinoespressif32 3.x: ledcAttach/ledcWrite,
// duty = 分辨率原始计数, 非千分比): 50Hz / 15bit, 0°=1000µs, 180°=2000µs
// 20ms 周期 = 32768 计数 -> 1000µs=1638, 1450µs=2355
static uint32_t servoDutyRaw(int deg) {
    if (deg < 0) deg = 0;
    if (deg > 180) deg = 180;
    uint32_t us = 1000UL + 5UL * (uint32_t)deg;
    return us * ((1UL << 15)) / 20000UL;
}
static void servoWrite(int n, int deg) {
    ledcWrite(SERVO_PIN[n - 1], servoDutyRaw(deg));
}

static void publishSorterAck(int n, const char* job_id, const char* action,
                             const char* result, const char* note) {
    mqtt.publish(sort::sorterAckTopic(n, job_id).c_str(),
                 sort::ackJson(job_id, action, result, note).c_str(), true);
}
static void publishConveyorAck(const char* job_id, const char* action,
                               const char* result, const char* note) {
    mqtt.publish(sort::conveyorAckTopic(job_id).c_str(),
                 sort::ackJson(job_id, action, result, note).c_str(), true);
}

// 运动事件: 分拣器每次状态迁移广播一条 (sort/line1/motion, QoS0 非保留).
// via = 迁移原因 (immediate/photo/virtual/timeout/reset/done/next/cancel).
// 网关据此维护 GET /motion 时间线, 供"让示意图动起来"的动画还原实际情况.
// 逐件件号 (item/of): qty>1 时每件各触发一次光门+一次扫掠, 事件带件号 —
// busy/timeout = 正在处理(未到场)的第几件; next/done = 刚完成的那件; 0 = 不适用.
static void publishMotion(int n, const char* state, const char* via,
                          int item = 0, int of = 0) {
    mqtt.publish(sort::motionTopic().c_str(),
                 sort::motionJson(n, state, via,
                                  lastJobId[n - 1].c_str(), "", item, of).c_str(),
                 false);
}

static void sweepStart(int n) {
    SweepTask& t = sweeps[n - 1];
    t.n = n; t.active = true; t.inEject = true;
    t.phaseAt = millis();
    servoWrite(n, SERVO_EJECT_DEG);
    beltHold.sweepStart();   // 推料期间停带: 物品停在推料点被拨入料道
    beltWatch.onDivert(n - 1);  // 该件在门 n-1 被拨出: 不再期望它过下一门
    stopStepRmt();
}
static void sweepTick() {
    for (int i = 0; i < 3; i++) {
        SweepTask& t = sweeps[i];
        if (!t.active) continue;
        sort::Sorter& s = sorters[t.n - 1];
        uint32_t now = millis();
        if (t.inEject) {
            if (now - t.phaseAt >= SERVO_SWEEP_MS / 2) {
                servoWrite(t.n, SERVO_REST_DEG);
                t.inEject = false;
                t.phaseAt = now;
            }
        } else if (now - t.phaseAt >= (uint32_t)(SERVO_ITEM_GAP_MS - SERVO_SWEEP_MS / 2)) {
            if (s.stepOne(millis())) {            // 最后一件
                t.active = false;
                if (beltHold.sweepEnd()) startStepRmt();   // 扫掠全结束: 恢复带速
                publishSorterAck(t.n, lastJobId[t.n - 1].c_str(), "divert",
                                 "ok", s.mat());
                publishMotion(t.n, "ready", "done", s.done_count, s.pend_qty);
                lastDoneJob[t.n - 1] = lastJobId[t.n - 1].c_str();
                lastDoneAtMs[t.n - 1] = millis();
            } else if (s.state_str == "armed") {
                // photo job 中间件: 等下一件的光门沿, 本扫掠到此为止
                t.active = false;
                if (beltHold.sweepEnd()) startStepRmt();   // 本件拨完先走带, 下一件到门再停
                publishMotion(t.n, "armed", "next", s.done_count, s.pend_qty);
            } else {
                t.inEject = true;                 // immediate: 连续拨下一件
                t.phaseAt = now;
                servoWrite(t.n, SERVO_EJECT_DEG);
            }
        }
    }
}

// ================= MQTT =================
static void ethEvent(arduino_event_id_t event, arduino_event_info_t) {
    if (event == ARDUINO_EVENT_ETH_GOT_IP) ethUp = true;
    if (event == ARDUINO_EVENT_ETH_LOST_IP) ethUp = false;
}

static void onMqttMessage(char* topic, byte* payload, unsigned int len) {
    std::string t(topic);
    JsonDocument doc;
    if (deserializeJson(doc, payload, len)) return;
    const char* job_id = doc["job_id"] | "";
    const char* action = doc["action"] | "";
    if (t == sort::conveyorCmdTopic()) {
        double speed = doc["speed_mps"] | 1.0;
        auto r = conv.handleCmd(action, speed);
        if (r.handled)
            publishConveyorAck(job_id, action, r.ok ? "ok" : "busy", r.note.c_str());
        if (r.handled && r.ok && action) {
            // RMT 步进随带速起停 (start/resume 起, stop/pause 停; 变速自动重起);
            // 推料进行中 (BeltHold hold>0) 则起带挂起, 扫掠结束由 sweepEnd 自动起
            if (!strcmp(action, "start") || !strcmp(action, "resume")) {
                if (beltHold.conveyorStart()) startStepRmt();
            } else if (!strcmp(action, "stop") || !strcmp(action, "pause")) {
                beltHold.conveyorStop();
                stopStepRmt();
            } else if (!strcmp(action, "reset")) {
                beltWatch.clear();     // 人工 reset: 解除带速监测锁存
                beltFaultSent = false;
            }
        }
    } else {
        int n = 0;
        for (n = 1; n <= 3; n++) if (t == sort::sorterCmdTopic(n)) break;
        if (n > 3) return;
        if (action && !strcmp(action, "divert")) {
            // 防重复执行: 已完成 job 的同 job_id 重发 -> 只补应答, 不再拨料
            if (!lastDoneJob[n - 1].empty() &&
                job_id && !strcmp(job_id, lastDoneJob[n - 1].c_str()) &&
                millis() - lastDoneAtMs[n - 1] < 60000) {
                publishSorterAck(n, job_id, "divert", "ok", "duplicate job");
                return;
            }
            lastJobId[n - 1] = job_id;
        }
        const char* material = doc["material"] | "";
        int qty = doc["qty"] | 1;
        const char* trigger = doc["trigger"] | "immediate";
        // 光门解析顺序: 物理光门 -> 虚拟光门 (未装光门时, 用相机检测锚点 +
        // 带速匀速外推预测物品过光门线时刻, 见 Track.h / config.h) -> immediate.
        // 回退原因写串口日志, 保证未装光门也能跑通 (时序与 gate_layout.svg 对齐).
        bool wantPhoto = !strcmp(trigger, "photo");
        bool photo = wantPhoto && gateOk;
        bool useVirtual = false;
        uint32_t virtAt = 0;
        if (wantPhoto && !gateOk && TRACK_ENABLED &&
            action && !strcmp(action, "divert")) {
            double p0 = 0.0;
            uint32_t t0 = 0;
            if (track.match(material, millis(), TRACK_MATCH_WINDOW_MS,
                            &p0, &t0)) {
                virtAt = sort::Track::predictGateMs(
                    t0, p0, GATE_DIST_MM[n - 1], conv.speed_mps);
                if (virtAt > millis()) {
                    photo = true;
                    useVirtual = true;
                    Serial.printf("[vgate] 分拣器%d 虚拟光门: 预测 %lums 后过光门线 (v=%.2f m/s p0=%+.0fmm)\n",
                                  n, (unsigned long)(virtAt - millis()),
                                  conv.speed_mps, p0);
                }
            }
        }
        if (wantPhoto && !photo)
            Serial.println("[gate] trigger=photo 但物理光门不可用且虚拟光门未匹配 (无同物料窗口内检测/带停/物品已过光门线), 回退 immediate");
        auto r = sorters[n - 1].handleCmd(action, material, qty,
                                          photo ? "photo" : "immediate",
                                          job_id, millis());
        if (!r.handled) return;
        if (useVirtual && r.ok &&
            !strcmp(sorters[n - 1].state(), "armed")) {
            // 件距 -> 件间时刻间隔 (ms): m/s 与 mm/ms 数值相等
            uint32_t pitch_ms = conv.speed_mps > 0.0
                ? (uint32_t)(ITEM_PITCH_MM / conv.speed_mps) : 0;
            sorters[n - 1].setVirtual(virtAt, pitch_ms);
        }
        if (action && !strcmp(action, "reset")) {
            publishSorterAck(n, job_id, "reset", r.ok ? "ok" : "busy",
                             r.note.c_str());
            if (r.ok)
                publishMotion(n, "ready", r.note == "cancelled" ? "cancel"
                                                                 : "reset");
        } else if (r.ok) {
            const char* via = useVirtual ? "virtual"
                              : (photo ? "photo" : "immediate");
            publishMotion(n, sorters[n - 1].state(), via, 1, qty);
            if (std::strcmp(sorters[n - 1].state(), "armed") != 0)
                sweepStart(n);    // immediate: 立即拨; photo/虚拟: 等光门沿再拨
        } else {
            const char* res = (r.note == "sorter fault") ? "fault" : "busy";
            publishSorterAck(n, job_id, "divert", res, r.note.c_str());
        }
    }
}

static void mqttConnect() {
    char id[40];
    snprintf(id, sizeof(id), "%s-%04lX", MQTT_CLIENT_ID,
             (unsigned long)(ESP.getEfuseMac() & 0xFFFF));
    if (mqtt.connect(id, nullptr, nullptr)) {
        mqtt.subscribe(sort::conveyorCmdTopic().c_str(), 1);
        for (int n = 1; n <= 3; n++)
            mqtt.subscribe(sort::sorterCmdTopic(n).c_str(), 1);
        Serial.println("[mqtt] connected, subscribed 4 cmd topics");
    } else {
        static uint32_t lastFailLog = 0;
        if (millis() - lastFailLog > 10000) {    // 失败日志限速 10s
            lastFailLog = millis();
            Serial.printf("[mqtt] connect 失败 (broker %s:%d)\n",
                          BROKER_IP.toString().c_str(), BROKER_PORT);
        }
    }
}

// ================= 视觉 (HuskyLens 2) =================
#if HL2_ON
static void onVision(const sort::VisionEvent& ev, uint32_t now_ms, void*) {
    char fid[24];
    snprintf(fid, sizeof(fid), "hl2-%lu", (unsigned long)ev.seq);
    // 位置锚点: 像素横坐标 -> 带面 mm (相机参考系), 供虚拟光门预测
    // 与动画轨迹还原 (pos_mm 随视觉事件透传网关, 见 Proto.h visionJson).
    double p0 = sort::Track::pixelToMm(ev.x, VIEW_CX_PX, VIEW_WIDTH_PX,
                                       VIEW_WIDTH_MM);
    track.onVision(ev.material.c_str(), p0, now_ms);
    beltWatch.onVision(p0, now_ms);  // 带速估计: 位置锚点
    std::string p = sort::visionJson(ev.material.c_str(), 1.0, fid,
                                     ev.x, ev.y, ev.w, ev.h, p0);
    mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
    Serial.printf("[vision] %s id=%s pos=%+.0fmm\n",
                  ev.material.c_str(), fid, p0);
}
#endif

// ================= 串口控制台 (调试) =================
static String consoleBuf;
static void onSerialCmd(const String& line) {
    if (line == "help") {
        Serial.println("help | state | eth | mqtt | stepstat | stepinit | gates | track | reboot | stepper <n> | sweep <n> | vision <material>");
    } else if (line == "gates") {
        bool hi0 = true, hi1 = true, hi2 = true;
        if (gateOk) mcpGate.poll(hi0, hi1, hi2);
        Serial.printf("gates: ok=%d s1=%d s2=%d s3=%d (0=物品挡光, 1=通光; ok=0 为全默认 HIGH)\n",
                      gateOk ? 1 : 0, hi0 ? 1 : 0, hi1 ? 1 : 0, hi2 ? 1 : 0);
    } else if (line == "state") {
        Serial.printf("conveyor=%s s1=%s s2=%s s3=%s diverted=%lu/%lu/%lu steps=%llu\n",
                      conv.state(), sorters[0].state(), sorters[1].state(),
                      sorters[2].state(), (unsigned long)sorters[0].diverted_total,
                      (unsigned long)sorters[1].diverted_total,
                      (unsigned long)sorters[2].diverted_total,
                      (unsigned long long)stepsLive());
    } else if (line == "stepstat") {
        Serial.printf("stepstat: ready=%d failed=%d active=%d pps=%.0f steps_live=%llu\n",
                      stepRmtReady ? 1 : 0, stepRmtFailed ? 1 : 0,
                      stepRmtActive ? 1 : 0, stepRmtPps,
                      (unsigned long long)stepsLive());
    } else if (line == "stepinit") {
        if (stepRmtReady) {
            Serial.println("stepinit: RMT 已就绪 (boot 时成功)");
        } else {
            rmt_tx_channel_config_t cc = {};
            cc.gpio_num = (gpio_num_t)STEPPER_STEP_PIN;
            cc.clk_src = RMT_CLK_SRC_APB;
            cc.resolution_hz = 80000000;
            cc.mem_block_symbols = 64;   // ESP32 经典: 偶数且 >=64
            cc.trans_queue_depth = 2;
            cc.intr_priority = 0;
            rmt_copy_encoder_config_t ec = {};
            esp_err_t e1 = rmt_new_tx_channel(&cc, &stepChan);
            esp_err_t e2 = (e1 == ESP_OK) ? rmt_new_copy_encoder(&ec, &stepEnc) : ESP_FAIL;
            Serial.printf("stepinit: channel=%d encoder=%d\n", (int)e1, (int)e2);
            if (e1 == ESP_OK && e2 == ESP_OK) {
                stepRmtReady = true;
                stepRmtFailed = false;
                Serial.println("stepinit: RMT 就绪, 后续 start 将走硬件脉冲");
            }
        }
    } else if (line == "reboot") {
        Serial.println("reboot in 1s");
        delay(1000);
        esp_restart();
    } else if (line.startsWith("stepper")) {
        int n = line.substring(8).toInt();
        // 手动脉冲验证: 必须走 RMT 硬件通道 (boot 后 RMT 已接管 GPIO14,
        // digitalWrite 写 GPIO 寄存器到不了物理引脚 — 与 sketch 纯 digitalWrite
        // 路径不同, 这是本固件能发脉冲的唯一通路). 50% 占空 400µs 周期 = 5kHz.
        if (stepRmtReady && stepChan && !stepRmtActive && n >= 1 && n <= 20000) {
            // 经典 ESP32 RMT 不支持 loop_count>0 (ESP_ERR_NOT_SUPPORTED),
            // 把 N 个符号排成数组一次发完. 每符号 [200µs H, 200µs L] = 5kHz.
            rmt_symbol_word_t *syms =
                (rmt_symbol_word_t *)malloc(n * sizeof(rmt_symbol_word_t));
            if (!syms) {
                Serial.println("stepper: malloc 失败");
            } else {
                for (int i = 0; i < n; i++) {
                    syms[i].duration0 = 16000;   // 200µs HIGH @80MHz
                    syms[i].level0 = 1;
                    syms[i].duration1 = 16000;   // 200µs LOW
                    syms[i].level1 = 0;
                }
                rmt_transmit_config_t tc = {};
                tc.loop_count = 0;
                esp_err_t e1 = rmt_enable(stepChan);
                esp_err_t e2 = (e1 == ESP_OK) ?
                    rmt_transmit(stepChan, stepEnc, syms,
                                 n * sizeof(rmt_symbol_word_t), &tc) : ESP_FAIL;
                free(syms);
                if (e2 != ESP_OK) {
                    if (e1 == ESP_OK) rmt_disable(stepChan);
                    Serial.printf("stepper %d: RMT 发射失败 e1=%d e2=%d\n", n, (int)e1, (int)e2);
                } else {
                    delay(400 * n / 1000 + 300);  // 等全部符号发完
                    rmt_disable(stepChan);
                    Serial.printf("stepper %d done\n", n);
                }
            }
        } else {
            Serial.println("stepper: RMT 未就绪或带在运行, 拒绝");
        }
    } else if (line.startsWith("sweep")) {
        String sn = line.substring(6);  sn.trim();   // "sweep" = 5 字符 + 空格, 数字从 6 起
        int n = sn.toInt();
        if (n >= 1 && n <= 3) {
            servoWrite(n, SERVO_EJECT_DEG); delay(450);
            servoWrite(n, SERVO_REST_DEG);
            Serial.printf("sweep %d done\n", n);
        }
    } else if (line.startsWith("vision")) {
        String m = line.substring(7);   // "vision" = 6 字符 + 空格, 物料从 7 起
        m.trim();
        // 注入视觉事件: 视为物品此刻在相机中心 (p0=0mm), 同时记位置锚点,
        // 之后 trigger=photo 的 divert 可走虚拟光门 (真机时序验证用).
        track.onVision(m.c_str(), 0.0, millis());
        std::string p = sort::visionJson(m.c_str(), 1.0, "console",
                                         0, 0, 0, 0, 0.0);
        mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
        Serial.printf("vision %s published (pos anchor set, p0=0mm)\n", m.c_str());
    } else if (line == "track") {
        char buf[192] = {0};
        int off = snprintf(buf, sizeof(buf),
            "track: v=%.2f m/s (= %.2f mm/ms) pitch=%.0fmm win=%ums",
            conv.speed_mps, conv.speed_mps, ITEM_PITCH_MM,
            (unsigned)TRACK_MATCH_WINDOW_MS);
        for (int i = 0; i < sort::Track::kMaxAnchors; i++) {
            const sort::Track::Anchor& a = track.anchors[i];
            if (a.valid)
                off += snprintf(buf + off, sizeof(buf) - off,
                                " | %s p0=%+.0fmm age=%lums", a.material,
                                a.p0_mm,
                                (unsigned long)(millis() - a.t0_ms));
        }
        Serial.printf("%s\n", buf);
        Serial.printf("gates: s1=%.0fmm s2=%.0fmm s3=%.0fmm view=%.0fmm (config.h)\n",
                      GATE_DIST_MM[0], GATE_DIST_MM[1], GATE_DIST_MM[2],
                      VIEW_WIDTH_MM);
    } else if (line == "mqtt") {
        Serial.printf("mqtt: connected=%d server=%s:%d\n",
                      mqtt.connected() ? 1 : 0,
                      BROKER_IP.toString().c_str(), BROKER_PORT);
    } else if (line == "eth") {
#ifdef CONFIG_IDF_TARGET_ESP32
        esp_netif_t* nif = findEthNetif();
        if (!nif) {
            Serial.println("eth: netif 未找到");
        } else {
            esp_netif_ip_info_t ip;
            esp_netif_get_ip_info(nif, &ip);
            char ipstr[16], gwstr[16];
            snprintf(ipstr, sizeof(ipstr), "%d.%d.%d.%d",
                     (int)((ip.ip.addr >> 24) & 0xFF), (int)((ip.ip.addr >> 16) & 0xFF),
                     (int)((ip.ip.addr >> 8) & 0xFF), (int)(ip.ip.addr & 0xFF));
            snprintf(gwstr, sizeof(gwstr), "%d.%d.%d.%d",
                     (int)((ip.gw.addr >> 24) & 0xFF), (int)((ip.gw.addr >> 16) & 0xFF),
                     (int)((ip.gw.addr >> 8) & 0xFF), (int)(ip.gw.addr & 0xFF));
            Serial.printf("eth: up=%d ip=%s gw=%s\n",
                          esp_netif_is_netif_up(nif) ? 1 : 0, ipstr, gwstr);
        }
#else
        Serial.println("eth: N/A (非 ESP32 原版目标)");
#endif
    }
}

// ================= 板载以太网 (IP101 PHY, DFRobot 官方 SDK 参数) =================
// PHY=IP101 (注册为 TLK110 驱动), addr=1, MDC=GPIO4, MDIO=GPIO13, PWR=GPIO2,
// 50MHz 参考时钟走 GPIO0 (以上引脚板内走线, 不出 40PIN 扩展口, 与外设不冲突).
// 此版本 arduino-esp32 无静态 IP begin() 重载: 先起 PHY + DHCP, 再停 DHCP 设静态.
// 实测: 此版本 arduino-esp32 3.x 默认 eth 的 if_key 是 "ETH_DEF" (非 "eth0"),
// 查错 key 会一直轮询不到 -> tryStaticIp() 幂等, setup 内多等, 之后 loop()
// 每 2s 重试直到成功 (兼顾 PHY/netif 上电慢的场景).
static esp_netif_t* findEthNetif() {
    esp_netif_t* nif = esp_netif_get_handle_from_ifkey("ETH_DEF");
    if (!nif) nif = esp_netif_get_handle_from_ifkey("eth0");
    return nif;
}

static bool tryStaticIp() {
    if (netifReady) return true;
    esp_netif_t* nif = findEthNetif();
    if (!nif) return false;
    esp_netif_dhcpc_stop(nif);
    esp_netif_ip_info_t info;
    esp_netif_set_ip4_addr(&info.ip,        EDGE_IP[0],   EDGE_IP[1],   EDGE_IP[2],   EDGE_IP[3]);
    esp_netif_set_ip4_addr(&info.netmask,   EDGE_MASK[0], EDGE_MASK[1], EDGE_MASK[2], EDGE_MASK[3]);
    esp_netif_set_ip4_addr(&info.gw,        EDGE_GW[0],   EDGE_GW[1],   EDGE_GW[2],   EDGE_GW[3]);
    esp_netif_set_ip_info(nif, &info);
    netifReady = true;
    ethUp = true;   // 静态 IP 已配好, 不依赖 GOT_IP 事件
    Serial.printf("[eth] 静态 IP %s gw %s (broker %s:%d)\n",
                  EDGE_IP.toString().c_str(), EDGE_GW.toString().c_str(),
                  BROKER_IP.toString().c_str(), BROKER_PORT);
    return true;
}

static void applyStaticIp() {
    for (int i = 0; i < 30 && !tryStaticIp(); i++) delay(500);   // boot 最多等 15s
    if (!netifReady)
        Serial.println("[eth] netif 未就绪, loop 中每 2s 重试");
}

// ================= 生命周期 =================
void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println("\n[edge101] boot - sort controller");

    pinMode(STEPPER_STEP_PIN, OUTPUT);
    pinMode(STEPPER_DIR_PIN, OUTPUT);
    digitalWrite(STEPPER_DIR_PIN, LOW);

    // RMT 步进脉冲 (硬件 320k pps 级): APB 80MHz 分辨率, 1 个 [pulse,gap] 符号
    {
        rmt_tx_channel_config_t cc = {};
        cc.gpio_num = (gpio_num_t)STEPPER_STEP_PIN;
        cc.clk_src = RMT_CLK_SRC_APB;
        cc.resolution_hz = 80000000;
        cc.mem_block_symbols = 64;   // ESP32 经典: 偶数且 >=64
        cc.trans_queue_depth = 2;
        cc.intr_priority = 0;
        rmt_copy_encoder_config_t ec = {};
        esp_err_t ec_chan = rmt_new_tx_channel(&cc, &stepChan);
        esp_err_t ec_enc = (ec_chan == ESP_OK) ? rmt_new_copy_encoder(&ec, &stepEnc) : ESP_FAIL;
        if (ec_chan == ESP_OK && ec_enc == ESP_OK) {
            stepRmtReady = true;
            Serial.printf("[step] RMT @GPIO%d 硬件脉冲就绪 (APB 80MHz)\n",
                          STEPPER_STEP_PIN);
        } else {
            Serial.printf("[step] RMT 初始化失败 (channel=%d encoder=%d), 回退主循环打脉冲 (~3kHz)\n",
                          (int)ec_chan, (int)ec_enc);
        }
    }

    // 舵机 ×3: LEDC 硬件 PWM 直连 (50Hz / 15bit), 上电即归位
    for (int i = 0; i < 3; i++) {
        ledcAttach(SERVO_PIN[i], 50, 15);
        ledcWrite(SERVO_PIN[i], servoDutyRaw(SERVO_REST_DEG));
    }
    Serial.printf("[servo] LEDC @50Hz 15bit: GPIO%d/GPIO%d/GPIO%d 归位 %d°\n",
                  SERVO_PIN[0], SERVO_PIN[1], SERVO_PIN[2], SERVO_REST_DEG);

    if (mcpEn.begin(&mcpBus, conv.state()))
        Serial.printf("[mcp] MCP23017 @0x%02X 在线\n", sort::McpEn::kAddr);
    else
        Serial.println("[mcp] MCP23017 未检测到 (光门未装; 步进 EN 未接线: 浮空=使能, 高=禁能, 2026-11-03 实测)");

    if (PHOTO_GATE_ENABLED) {
        if (mcpEn.present() && mcpGate.begin(&mcpBus, PHOTO_GATE_MASK_B)) {
            gateOk = true;
            Serial.println("[gate] 光门 x3 -> MCP23017 PB0/1/2 (divert trigger=photo 时序触发)");
        } else {
            Serial.println("[gate] 光门不可用 (MCP23017 未在线或配置失败), trigger=photo 走虚拟光门或回退 immediate");
        }
    }
    if (TRACK_ENABLED)
        Serial.printf("[vgate] 虚拟光门已启用: 光门距离 %.0f/%.0f/%.0fmm 件距 %.0fmm 视野 %.0fmm (标定项见 config.h / wiring.md)\n",
                      GATE_DIST_MM[0], GATE_DIST_MM[1], GATE_DIST_MM[2],
                      ITEM_PITCH_MM, VIEW_WIDTH_MM);

#if HL2_ON
    hlSerial.begin(HL2_BAUD, SERIAL_8N1, HL2_RX_PIN, HL2_TX_PIN);
    Serial.printf("[hl2] UART2 @%lu rx=GPIO%d tx=GPIO%d\n",
                  (unsigned long)HL2_BAUD, HL2_RX_PIN, HL2_TX_PIN);
#else
    Serial.printf("[hl2] 未接线: I2C 总线预留 SDA=GPIO%d / SCL=GPIO%d, "
                  "接线确认后实现 I2C 传输并置 HL2_ON=1 (期间可用串口 vision 注入验证虚拟光门)\n",
                  HL2_I2C_SDA_PIN, HL2_I2C_SCL_PIN);
#endif

#ifdef CONFIG_IDF_TARGET_ESP32
    // 板载 RJ45 (EDGE 101): 先起 PHY + DHCP, 再停 DHCP 设静态 IP
    bool phyOk = ETH.begin(ETH_PHY_TLK110, 1, 4, 13, 2, ETH_CLOCK_GPIO0_IN);
    Serial.println(phyOk ? "[eth] PHY/驱动初始化 OK"
                         : "[eth] ETH.begin 失败 (PHY 初始化?)");
    WiFi.onEvent(ethEvent);   // ETH 事件经 Network 事件循环分发 (3.x 无 ETH.onEvent)
    applyStaticIp();
#else
    Serial.println("[eth] 非 ESP32 原版目标 (如 S3 仿真): 无板载以太网, MQTT 不可用");
#endif

    mqtt.setServer(BROKER_IP, BROKER_PORT);
    mqtt.setCallback(onMqttMessage);
    Serial.println("[edge101] ready (serial console: help)");
}

static uint32_t lastTeleMs = 0;
static uint32_t lastMqttTryMs = 0;
static uint32_t lastEthTryMs = 0;

void loop() {
    steerStepper();
    sweepTick();
    mcpEn.sync(conv.state());   // 步进 EN 随状态 (只有电平变化才写 I2C)

    // 带健康监测: 带面时间只在带子实际转时推进 (BeltHold 停带窗口不计),
    // 评估卡滞/打滑; 异常 -> 锁存 conveyor fault (需人工 reset).
    beltWatch.setSpeed(conv.speed_mps);
    beltWatch.tick(millis(), beltHold.beltOn());
    if (!beltWatch.ok() && !beltFaultSent) {
        beltFaultSent = true;
        conv.setFault();
        publishMotion(1, "fault", beltWatch.via(), 0, 0);
        Serial.printf("[belt] %s\n", beltWatch.reason());
    }

    // 光门: 读电平 -> 落沿 (物品到达推杆前侧) -> 触发 armed 分拣器推出
    if (gateOk) {
        bool hi0, hi1, hi2;
        mcpGate.poll(hi0, hi1, hi2);
        for (int i = 0; i < 3; i++) {
            bool hi = (i == 0) ? hi0 : (i == 1 ? hi1 : hi2);
            if (gates[i].onItemEdge(hi, millis())) {
                beltWatch.onGateEdge(i, millis());  // 带监测: 全部物理落沿 (与分拣 armed 态无关)
                if (sorters[i].onGateEdge(millis())) {
                sweepStart(i + 1);
                publishMotion(i + 1, "busy", "photo",
                              sorters[i].done_count + 1, sorters[i].pend_qty);
                Serial.printf("[gate] 分拣器%d 光门触发, 开始拨料 (剩 %d 件)\n",
                              i + 1, sorters[i].pendingQty());
                }
            }
        }
    }
    // 虚拟光门: 无物理光门时, 到预测的过光门线时刻触发 (armed -> busy).
    for (int i = 0; i < 3; i++) {
        if (sorters[i].onVirtualEdge(millis())) {
            sweepStart(i + 1);
            publishMotion(i + 1, "busy", "virtual",
                          sorters[i].done_count + 1, sorters[i].pend_qty);
            Serial.printf("[vgate] 分拣器%d 虚拟光门触发, 开始拨料 (剩 %d 件)\n",
                          i + 1, sorters[i].pendingQty());
        }
    }
    // armed 光门等待超时 -> fault + 对 job_id 回 fault ack
    for (int i = 0; i < 3; i++) {
        if (sorters[i].tick(millis())) {
            Serial.printf("[gate] 分拣器%d 光门等待超时 (%ums 无物品)\n",
                          i + 1, (unsigned)sort::kPhotoTimeoutMs);
            publishSorterAck(i + 1, sorters[i].pendingJobId().c_str(),
                             "divert", "fault", "photo timeout");
            publishMotion(i + 1, "fault", "timeout",
                          sorters[i].done_count + 1, sorters[i].pend_qty);
        }
    }

    if (!ethUp && millis() - lastEthTryMs > 2000) {     // netif 晚到: 持续重试
        lastEthTryMs = millis();
        tryStaticIp();
    }
    if (!mqtt.connected() && ethUp && millis() - lastMqttTryMs > 2000) {
        lastMqttTryMs = millis();
        mqttConnect();
    }
    mqtt.loop();

#if HL2_ON
    while (hlSerial.available())
        hl2.feedByte((uint8_t)hlSerial.read(), millis(), onVision, nullptr);
#endif

    if (mqtt.connected() && millis() - lastTeleMs >= 500) {
        lastTeleMs = millis();
        mqtt.publish(sort::conveyorTeleTopic().c_str(),
                     sort::conveyorTele(conv.state(), conv.speed_mps,
                                        stepsLive(), "", beltWatch.reason()).c_str(), false);
        for (int n = 1; n <= 3; n++)
            mqtt.publish(sort::sorterTeleTopic(n).c_str(),
                         sort::sorterTele(sorters[n - 1].state(),
                                          sorters[n - 1].diverted_total,
                                          sorters[n - 1].mat()).c_str(),
                         false);
    }

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') { onSerialCmd(consoleBuf); consoleBuf = ""; }
        else if (c != '\r') consoleBuf += c;
    }
}
