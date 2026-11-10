// EDGE 101 (DFR1283, ESP32 原版) PCB 分拣执行器 — 新方案 (2026-11-10)
//
// 架构定案: 网关 = 指令源 (物料,仓,数量), ESP32 = 执行器 (只做物理动作, 不选仓).
//   PC 语音 -> laya 决策模型 -> (物料,仓,数量) -> loop_ui -> 网关 /dispatch
//          -> MQTT sort/sorter{n}/cmd (divert) -> 本执行器.
//   逐件执行: 单点红外 (IR, GPIO34)=T0 -> 带速外推预测到目标仓推杆时刻
//          -> 到点瞬停 -> 推杆展开 (-80°, hold=0) -> 收回 (270°) -> 恢复带.
//   HuskyLens 2 (I2C) 识别物料, 发 sort/inbound/vision 喂数字孪生"真实侧"
//          (不选仓; 与命令物料的对照由网关/网页做).
//   原子指令 (串口, 各自独立可单独下发): belt stop / belt start <mps> /
//          belt resume / push <n>; 诊断与在线调参 ir/irset/spset/hold/confmin.
//
// 引脚/网络/参数全部在 include/config.h. 纯逻辑 (Conveyor/BeltHold/Track/Proto)
// 在 lib/SortLogic (主机可单测). HuskyLens I2C 用 DFRobot 官方库 lib/HuskylensV2.
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
#include "BeltHold.h"
#include "Track.h"
#include "Proto.h"
#include "McpEn.h"
#if HL_I2C_ON
#include "DFRobot_HuskylensV2.h"
#endif

// ================= 全局 =================
static sort::Conveyor conv;
static sort::BeltHold beltHold;   // 推料期间停带 (扫掠中 hold, 结束自动恢复)
static sort::Track track;         // 带面位置跟踪 (保留; 本方案触发源为 IR)

// 步进 EN (MCP23017 PA0; 未接线时浮空=使能, 此处保留探活, 无害).
// TwoWire 实现 I2cReg 传输.
struct WireMcpBus : public sort::I2cReg {
    bool alive() override {
        Wire.beginTransmission(sort::McpEn::kAddr);
        Wire.write(sort::McpEn::kIodirA);
        Wire.write(0x00);
        return Wire.endTransmission() == 0;
    }
    void writeReg(uint8_t reg, uint8_t val) override {
        Wire.beginTransmission(sort::McpEn::kAddr);
        Wire.write(reg); Wire.write(val);
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

static NetworkClient ethClient;
static PubSubClient mqtt(ethClient);
static volatile bool ethUp = false;
static bool netifReady = false;
static esp_netif_t* findEthNetif();

// ================= 网关指令 (物料,仓,数量): 逐件执行 =================
struct Cmd {
    int bin = 0;
    String material;
    int qty = 0;
    int remaining = 0;
    String job_id;
    bool active = false;
    uint32_t last_prog_ms = 0;   // 最近一次 IR 沿/推件 完成时刻 (无进展超时用)
};
static Cmd curCmd;
static uint32_t IR_WAIT_TIMEOUT_MS = 15000;  // 有命令但无 IR 沿/无推件 -> fault

// 在线可调参数 (默认取 config.h, 可被串口命令改).
static double  g_ir_d1_mm   = IR_DIST1_MM;    // IR -> 1号仓推杆
static double  g_ir_spacing = IR_SPACING_MM;  // 推杆间距
static bool    g_ir_low     = IR_ACTIVE_LOW;  // 接近=低电平 (默认 NPN)
static uint32_t g_hold_ms   = 0;             // hold=0 定案 (在线 hold 可调)
static double  g_confmin    = HL_CONFMIN;    // HuskyLens 置信度门槛

// 红外 (GPIO34, 位置基准 T0).
static uint32_t g_ir_count = 0;              // 累计过 IR 物品数 (遥测/顺序对账)
static bool     g_ir_prev  = false;          // 上一拍"物品在 IR" 状态 (沿判)

// 待执行/执行中的推件 (IR 沿后, 预测到仓时刻到点: 停带->展开->hold->收回->恢复).
struct Push {
    bool active = false;      // 正在执行 (停带 + 推杆行程)
    bool pend  = false;       // 已排程, 未到点
    int  bin   = 0;
    uint32_t pend_at_ms = 0;  // 到仓(瞬停)时刻
    int  phase = 0;           // 0=展开行程 1=hold 2=收回行程
    uint32_t phase_at = 0;
};
static Push push;

// 遥测计数.
static uint32_t g_diverted[3] = {0, 0, 0};   // 各仓累计推件

// ================= 步进: RMT 硬件脉冲 (主循环旧路径作回退) =================
// 主循环 digitalWrite 打 STEP 只能出 ~3kHz, 名义带速打不出来 -> RMT TX 通道
// 无限循环 [pulse,gap] 硬件出 50% 占空比脉冲串; 主循环只在离 running 时 disable.
static rmt_channel_handle_t stepChan = nullptr;
static rmt_encoder_handle_t stepEnc = nullptr;
static bool stepRmtReady = false;
static bool stepRmtFailed = false;
static bool stepRmtActive = false;
static uint32_t stepRmtStartMs = 0;
static double stepRmtPps = 0.0;

static uint32_t nextStepUs = 0;
static void steerStepperLegacy() {
    if (conv.st != sort::DevState::kRunning) return;
    if (beltHold.hold > 0) return;   // 推料期间停带
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
    conv.steps_total +=
        (uint64_t)(stepRmtPps * (millis() - stepRmtStartMs) / 1000.0);
    rmt_disable(stepChan);
    delay(20);   // 排空无限循环残留 ISR (防后续复用通道时崩溃)
}
static bool startStepRmt() {
    double pps = conv.speed_mps * 1000.0 * STEPS_PER_MM;
    if (pps < 1000.0) return false;
    uint32_t period = (uint32_t)(80000000.0 / pps);
    if (period < 10 || period > 32766) return false;
    stopStepRmt();
    rmt_symbol_word_t sym = {};
    sym.duration0 = period / 2;
    sym.level0 = 1;
    sym.duration1 = period - period / 2;
    sym.level1 = 0;
    rmt_transmit_config_t tc = {};
    tc.loop_count = -1;
    if (rmt_enable(stepChan) != ESP_OK) {
        stepRmtFailed = true;
        return false;
    }
    if (rmt_transmit(stepChan, stepEnc, &sym, sizeof(sym), &tc) != ESP_OK) {
        rmt_disable(stepChan);
        stepRmtFailed = true;
        return false;
    }
    stepRmtStartMs = millis();
    stepRmtPps = pps;
    stepRmtActive = true;
    return true;
}
static void steerStepper() {
    if (stepRmtReady && !stepRmtFailed) {
        if (conv.st != sort::DevState::kRunning && stepRmtActive) stopStepRmt();
        return;
    }
    steerStepperLegacy();
}
static uint64_t stepsLive() {
    uint64_t s = conv.steps_total;
    if (stepRmtActive)
        s += (uint64_t)(stepRmtPps * (millis() - stepRmtStartMs) / 1000.0);
    return s;
}

// ================= 舵机 (LEDC 硬件 PWM 直连; 夹值修到 -120..280) =================
// 50Hz/15bit, 标准映射 0°=1000µs. 宽量程推杆: 收回=270°(2500µs), 展开=-80°(556µs).
// 2026-11-10 用户实机确认 (早期把方向定义反了). 夹值上限 240->280 才能到 270° 收回位.
static uint32_t servoDutyRaw(int deg) {
    if (deg < -120) deg = -120;
    if (deg > 280) deg = 280;
    int64_t us = 1000 + (1000LL * (int64_t)deg) / 180LL;
    if (us < 500) us = 500;
    return (uint32_t)(us * ((1UL << 15)) / 20000UL);
}
static void servoWrite(int n, int deg) {
    ledcWrite(SERVO_PIN[n - 1], servoDutyRaw(deg));
}
static void servoRestAll() {
    for (int i = 1; i <= 3; i++) servoWrite(i, SERVO_REST_DEG);
}

// ================= MQTT ack / motion 辅助 =================
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
// 运动事件: 每次状态迁移广播一条 (sort/line1/motion, QoS0), 供孪生还原"实际情况".
// via = 迁移原因 (cmd/ir_due/next/done/timeout/cancel/reset...).
static void publishMotion(int n, const char* state, const char* via,
                          int item = 0, int of = 0, const char* job = "") {
    mqtt.publish(sort::motionTopic().c_str(),
                 sort::motionJson(n, state, via, job ? job : "", "",
                                  item, of).c_str(),
                 false);
}

// "sort/sorter{n}/cmd" -> n (1..3); 其它 topic 返回 0.
static int parseSorterN(const std::string& t) {
    const std::string pre = "sort/sorter";
    if (t.compare(0, pre.size(), pre) != 0) return 0;
    int n = 0;
    for (size_t i = pre.size(); t[i] >= '0' && t[i] <= '9'; i++)
        n = n * 10 + (t[i] - '0');
    return n;
}

// ================= 推件 (IR 到仓: 瞬停 -> 展开 -> hold -> 收回 -> 恢复) ========
// 非阻塞状态机, 每 loop 推进一步. 停带窗口内带子物理停, 物品停在目标仓被拨入料道.
static void pushTick() {
    if (push.pend && !push.active && millis() >= push.pend_at_ms) {
        push.pend = false;
        push.active = true;
        push.phase = 0;
        push.phase_at = millis();
        beltHold.sweepStart();      // 停带
        stopStepRmt();
        servoWrite(push.bin, SERVO_EJECT_DEG[push.bin - 1]);   // 展开 -80°
        publishMotion(push.bin, "busy", "ir_due",
                      curCmd.qty - curCmd.remaining + 1, curCmd.qty,
                      curCmd.job_id.c_str());
        Serial.printf("[push] %d 号仓 到点瞬停, 展开推杆\n", push.bin);
    }
    if (!push.active) return;
    uint32_t now = millis();
    if (push.phase == 0) {          // 展开行程: 270 -> -80
        if (now - push.phase_at >= SERVO_EJECT_MOVE_MS) {
            push.phase = (g_hold_ms > 0) ? 1 : 2;
            push.phase_at = now;
            if (push.phase == 2)
                servoWrite(push.bin, SERVO_REST_DEG);   // hold=0: 展开到位立即收回
        }
    } else if (push.phase == 1) {   // hold 驻留 (默认 0)
        if (now - push.phase_at >= g_hold_ms) {
            push.phase = 2;
            push.phase_at = now;
            servoWrite(push.bin, SERVO_REST_DEG);       // 收回 270°
        }
    } else {                        // 收回行程: -80 -> 270
        if (now - push.phase_at >= SERVO_RETRACT_MOVE_MS) {
            push.active = false;
            beltHold.sweepEnd();      // 恢复带 (若网关态 running)
            g_diverted[push.bin - 1]++;
            int item = curCmd.qty - curCmd.remaining + 1;
            curCmd.last_prog_ms = now;
            if (curCmd.active) {
                curCmd.remaining--;
                if (curCmd.remaining <= 0) {
                    publishSorterAck(push.bin, curCmd.job_id.c_str(), "divert",
                                     "ok", curCmd.material.c_str());
                    publishMotion(push.bin, "ready", "done", item, curCmd.qty,
                                  curCmd.job_id.c_str());
                    Serial.printf("[cmd] %s 完成: %d 号仓 %d 件 (%s)\n",
                                  curCmd.job_id.c_str(), push.bin, curCmd.qty,
                                  curCmd.material.c_str());
                    curCmd.active = false;
                } else {
                    publishMotion(push.bin, "armed", "next", item, curCmd.qty,
                                  curCmd.job_id.c_str());
                    Serial.printf("[push] %d 号仓 已推 %d/%d, 等下一件过 IR\n",
                                  push.bin, curCmd.qty - curCmd.remaining,
                                  curCmd.qty);
                }
            }
        }
    }
}

// 有活跃命令 + 无推件进行中 + 带子应转 时, IR 沿排程一次到仓推件.
static void onIrEdge() {
    if (!curCmd.active || push.active || push.pend) return;
    if (conv.st != sort::DevState::kRunning || conv.speed_mps <= 0.0) return;
    double d_bin = g_ir_d1_mm + (curCmd.bin - 1) * g_ir_spacing;
    uint32_t now = millis();
    uint32_t at = sort::Track::predictGateMs(now, 0.0, d_bin, conv.speed_mps);
    if (at <= now) {
        Serial.printf("[ir] 到仓窗口已过 (%d 号仓 d=%.0fmm), 跳过此件\n",
                      curCmd.bin, d_bin);
        return;
    }
    push.pend = true;
    push.bin = curCmd.bin;
    push.pend_at_ms = at;
    curCmd.last_prog_ms = now;
    Serial.printf("[ir] T0 -> %d 号仓 d=%.0fmm, %lums 后瞬停推件 (v=%.3f m/s)\n",
                  curCmd.bin, d_bin, (unsigned long)(at - now), conv.speed_mps);
}

// 命令无进展 (无 IR 沿/无推件) 超时 -> fault, 避免挂死.
static void cmdTimeoutTick() {
    if (!curCmd.active) return;
    if (push.active || push.pend) return;      // 推件进行中不算无进展
    if (millis() - curCmd.last_prog_ms > IR_WAIT_TIMEOUT_MS) {
        publishSorterAck(curCmd.bin, curCmd.job_id.c_str(), "divert", "fault",
                         "no IR edge / no progress (带未运行或无物品到 IR)");
        publishMotion(curCmd.bin, "fault", "timeout", 0, 0, curCmd.job_id.c_str());
        Serial.printf("[cmd] %s 超时 fault (无 IR 沿/无推件 >%lums)\n",
                      curCmd.job_id.c_str(), (unsigned long)IR_WAIT_TIMEOUT_MS);
        curCmd.active = false;
    }
}

// ================= HuskyLens 2 (I2C): 识别物料 -> 发真实侧视觉事件 ===========
// 不选仓 (选仓权在 PC laya 模型). 轮询 getResult -> 按 confmin 过滤 -> 发
// sort/inbound/vision. 与命令物料的 match/mismatch 对照由网关/网页做.
static const char* hlMaterialName(int algo, int id) {
    for (int i = 0; i < MATERIAL_MAP_LEN; i++)
        if ((int)MATERIAL_MAP[i].obj_type == algo &&
            (int)MATERIAL_MAP[i].id == id)
            return MATERIAL_MAP[i].material;
    return nullptr;
}
#if HL_I2C_ON
static HuskylensV2 hl;
static uint32_t lastHlPollMs = 0;
static uint32_t g_hl_ok = 0, g_hl_fail = 0;
static void hlTick() {
    uint32_t now = millis();
    if (now - lastHlPollMs < HL_POLL_MS) return;
    lastHlPollMs = now;
    hl.timeOutDuration = HL_TIMEOUT_MS;        // 限单次 getResult 阻塞 (防卡主循环)
    int8_t n = hl.getResult((eAlgorithm_t)HL_ALGO);
    if (n < 0) { g_hl_fail++; return; }        // I2C 无应答, 下轮再试
    g_hl_ok++;
    for (int i = 0; i < n; i++) {
        Result* r = hl.result[(eAlgorithm_t)HL_ALGO][i];
        if (!r) continue;
        float conf = r->conf;
        int id = (int)r->ID;
        const char* mat = hlMaterialName(HL_ALGO, id);
        if (!mat) {
            Serial.printf("[hl] 未映射 id=%d (type=%d) conf=%.2f\n",
                          id, HL_ALGO, conf);
            continue;
        }
        if (conf < g_confmin) {
            Serial.printf("[hl] %s conf=%.2f < confmin %.2f, 丢弃\n",
                          mat, conf, g_confmin);
            continue;
        }
        char fid[32];
        snprintf(fid, sizeof(fid), "hl-%lu-%d", now, id);
        std::string p = sort::visionJson(mat, conf, fid, r->xCenter, r->yCenter,
                                         r->width, r->height, -1.0);
        mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
        Serial.printf("[hl] %s id=%d conf=%.2f bbox=(%d,%d,%d,%d) -> vision\n",
                      mat, id, conf, r->xCenter, r->yCenter, r->width, r->height);
    }
}
#endif

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
        double speed = doc["speed_mps"] | 0.0;
        auto r = conv.handleCmd(action, speed);
        if (r.handled)
            publishConveyorAck(job_id, action, r.ok ? "ok" : "busy",
                               r.note.c_str());
        if (r.handled && r.ok) {
            if (!strcmp(action, "start") || !strcmp(action, "resume")) {
                if (beltHold.conveyorStart()) startStepRmt();
            } else if (!strcmp(action, "stop") || !strcmp(action, "pause")) {
                beltHold.conveyorStop();
                stopStepRmt();
            } else if (!strcmp(action, "reset")) {
                beltHold.conveyorStop();
                stopStepRmt();
                curCmd.active = false;
                push.active = false;
                push.pend = false;
                servoRestAll();
            }
        }
    } else {
        int n = parseSorterN(t);
        if (n < 1 || n > 3) return;
        if (action && !strcmp(action, "divert")) {
            const char* material = doc["material"] | "";
            int qty = doc["qty"] | 1;
            if (curCmd.active) {
                publishSorterAck(n, job_id, "divert", "busy",
                                 "上一指令执行中, 排队稍候");
                return;
            }
            if (qty < 1 || qty > 100) {
                publishSorterAck(n, job_id, "divert", "fault", "qty 非法");
                return;
            }
            curCmd = Cmd{};
            curCmd.bin = n;
            curCmd.material = material;
            curCmd.qty = qty;
            curCmd.remaining = qty;
            curCmd.job_id = job_id;
            curCmd.active = true;
            curCmd.last_prog_ms = millis();
            publishMotion(n, "armed", "cmd", 1, qty, job_id);
            Serial.printf("[cmd] 接受: %d 号仓 %d 件 (%s) job=%s [带子需 running + 物品过 IR]\n",
                          n, qty, material, job_id);
            // 不立即 ack: 全部推完才 ack ok (同 .bak 语义, 网关据此等待).
        } else if (action && !strcmp(action, "reset")) {
            bool had = curCmd.active;
            curCmd.active = false;
            push.active = false;
            push.pend = false;
            servoRestAll();
            publishSorterAck(n, job_id, "reset", "ok", had ? "cancelled" : "idle");
            if (had) publishMotion(n, "ready", "cancel", 0, 0, job_id);
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
        Serial.println("[mqtt] connected, subscribed conveyor + 3 sorter cmd");
    } else {
        static uint32_t lastFailLog = 0;
        if (millis() - lastFailLog > 10000) {
            lastFailLog = millis();
            Serial.printf("[mqtt] connect 失败 (broker %s:%d)\n",
                          BROKER_IP.toString().c_str(), BROKER_PORT);
        }
    }
}

// ================= 红外 (GPIO34) =================
static bool irItemPresent() {
    bool lvl = digitalRead(IR_PIN);
    return g_ir_low ? !lvl : lvl;
}

// ================= 串口控制台 (原子指令 + 诊断 + 在线调参) =================
static String consoleBuf;
static void onSerialCmd(const String& line) {
    if (line == "help") {
        Serial.println("belt stop | belt start <mps> | belt resume | push <1-3>");
        Serial.println("state | ir | irset <d1mm> [spacingmm] | irset pol | spset <mm>");
        Serial.println("hold <ms> | confmin <0..1> | vision <material> | servo <n> <deg>");
        Serial.println("track | stepstat | mqtt | eth | reboot");
    } else if (line == "state") {
        Serial.printf("conveyor=%s v=%.3fm/s steps=%llu | cmd=%s bin=%d rem=%d/%d job=%s | push=%s ir=%lu\n",
                      conv.state(), conv.speed_mps, (unsigned long long)stepsLive(),
                      curCmd.active ? curCmd.material.c_str() : "-",
                      curCmd.active ? curCmd.bin : 0,
                      curCmd.active ? curCmd.remaining : 0,
                      curCmd.active ? curCmd.qty : 0,
                      curCmd.active ? curCmd.job_id.c_str() : "-",
                      push.active ? "active" : (push.pend ? "pend" : "idle"),
                      (unsigned long)g_ir_count);
    } else if (line == "belt stop") {
        conv.handleCmd("stop", 0.0);
        beltHold.conveyorStop();
        stopStepRmt();
        Serial.println("belt: stopped");
    } else if (line.startsWith("belt start")) {
        double mps = line.substring(11).toDouble();
        if (mps <= 0.0) {
            Serial.println("belt start: 必须带 speed_mps, 例 belt start 0.0525");
            return;
        }
        conv.handleCmd("start", mps);
        if (beltHold.conveyorStart()) startStepRmt();
        Serial.printf("belt: start @ %.3f m/s\n", conv.speed_mps);
    } else if (line == "belt resume") {
        conv.handleCmd("resume", conv.speed_mps);
        if (beltHold.conveyorStart()) startStepRmt();
        Serial.printf("belt: resume @ %.3f m/s\n", conv.speed_mps);
    } else if (line.startsWith("push ")) {
        int n = line.substring(5).toInt();
        if (n < 1 || n > 3) { Serial.println("push: 用法 push <1-3>"); return; }
        // 原子推件 (不碰带子): 展开 -80° -> hold -> 收回 270°. 带子由 belt 单独控制.
        servoWrite(n, SERVO_EJECT_DEG[n - 1]);
        delay(SERVO_EJECT_MOVE_MS);
        if (g_hold_ms > 0) delay(g_hold_ms);
        servoWrite(n, SERVO_REST_DEG);
        delay(SERVO_RETRACT_MOVE_MS);
        g_diverted[n - 1]++;
        Serial.printf("push %d done (manual; 带子未动)\n", n);
    } else if (line == "ir") {
        Serial.printf("ir: pin=GPIO%d count=%lu present=%d pol=接近%s d1=%.0fmm spacing=%.0fmm\n",
                      IR_PIN, (unsigned long)g_ir_count, irItemPresent() ? 1 : 0,
                      g_ir_low ? "low" : "high", g_ir_d1_mm, g_ir_spacing);
    } else if (line.startsWith("irset pol")) {
        g_ir_low = !g_ir_low;
        Serial.printf("irset pol: 接近=%s\n", g_ir_low ? "low" : "high");
    } else if (line.startsWith("irset")) {
        int sp = line.indexOf(' ', 5);
        if (sp < 0) { Serial.println("irset: 用法 irset <d1mm> [spacingmm]"); return; }
        String s = line.substring(sp + 1);
        double d1 = s.toDouble();
        int sp2 = s.indexOf(' ');
        if (sp2 > 0) g_ir_spacing = s.substring(sp2 + 1).toDouble();
        if (d1 > 0.0) g_ir_d1_mm = d1;
        Serial.printf("irset: d1=%.0fmm spacing=%.0fmm\n", g_ir_d1_mm, g_ir_spacing);
    } else if (line.startsWith("spset")) {
        double mm = line.substring(5).toDouble();
        if (mm > 0.0) { g_ir_spacing = mm; Serial.printf("spset: spacing=%.0fmm\n", mm); }
        else Serial.println("spset: 用法 spset <mm>");
    } else if (line.startsWith("hold")) {
        g_hold_ms = line.substring(4).toInt();
        Serial.printf("hold: %lums\n", (unsigned long)g_hold_ms);
    } else if (line.startsWith("confmin")) {
        double v = line.substring(7).toDouble();
        if (v >= 0.0 && v <= 1.0) { g_confmin = v; Serial.printf("confmin: %.2f\n", v); }
        else Serial.println("confmin: 用法 confmin <0..1>");
    } else if (line.startsWith("vision")) {
        String m = line.substring(6);
        m.trim();
        std::string p = sort::visionJson(m.c_str(), 1.0, "console", 0, 0, 0, 0, -1.0);
        mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
        Serial.printf("vision %s injected (真实侧, 不选仓)\n", m.c_str());
    } else if (line.startsWith("servo")) {
        int sp = line.indexOf(' ', 5);
        int sp2 = sp > 0 ? line.indexOf(' ', sp + 1) : -1;
        int n = sp > 0 ? line.substring(sp + 1, sp2 < 0 ? line.length() : sp2).toInt() : 0;
        int deg = sp2 > 0 ? line.substring(sp2 + 1).toInt() : -1;
        if (n >= 1 && n <= 3 && deg >= -120 && deg <= 280) {
            servoWrite(n, deg);
            Serial.printf("servo %d -> %d deg\n", n, deg);
        } else {
            Serial.println("servo: 用法 servo <1-3> <-120..280>");
        }
    } else if (line == "track") {
        Serial.printf("track: v=%.3f m/s ir d1=%.0fmm spacing=%.0fmm count=%lu hold=%lums confmin=%.2f\n",
                      conv.speed_mps, g_ir_d1_mm, g_ir_spacing,
                      (unsigned long)g_ir_count, (unsigned long)g_hold_ms, g_confmin);
    } else if (line == "stepstat") {
        Serial.printf("stepstat: ready=%d failed=%d active=%d pps=%.0f steps=%llu\n",
                      stepRmtReady ? 1 : 0, stepRmtFailed ? 1 : 0,
                      stepRmtActive ? 1 : 0, stepRmtPps,
                      (unsigned long long)stepsLive());
    } else if (line == "mqtt") {
        Serial.printf("mqtt: connected=%d server=%s:%d\n", mqtt.connected() ? 1 : 0,
                      BROKER_IP.toString().c_str(), BROKER_PORT);
    } else if (line == "eth") {
#ifdef CONFIG_IDF_TARGET_ESP32
        esp_netif_t* nif = findEthNetif();
        if (!nif) {
            Serial.println("eth: netif 未找到");
        } else {
            esp_netif_ip_info_t ip;
            esp_netif_get_ip_info(nif, &ip);
            char ipstr[16];
            snprintf(ipstr, sizeof(ipstr), "%d.%d.%d.%d",
                     (int)((ip.ip.addr >> 24) & 0xFF), (int)((ip.ip.addr >> 16) & 0xFF),
                     (int)((ip.ip.addr >> 8) & 0xFF), (int)(ip.ip.addr & 0xFF));
            Serial.printf("eth: up=%d ip=%s\n",
                          esp_netif_is_netif_up(nif) ? 1 : 0, ipstr);
        }
#else
        Serial.println("eth: N/A");
#endif
    } else if (line == "reboot") {
        Serial.println("reboot in 0.5s");
        delay(500);
        esp_restart();
    } else {
        Serial.printf("未知命令: '%s' (help 查看)\n", line.c_str());
    }
}

// ================= 板载以太网 (IP101 PHY, DFRobot 官方 SDK 参数) =============
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
    esp_netif_set_ip4_addr(&info.ip,      EDGE_IP[0], EDGE_IP[1], EDGE_IP[2], EDGE_IP[3]);
    esp_netif_set_ip4_addr(&info.netmask, EDGE_MASK[0], EDGE_MASK[1], EDGE_MASK[2], EDGE_MASK[3]);
    esp_netif_set_ip4_addr(&info.gw,      EDGE_GW[0], EDGE_GW[1], EDGE_GW[2], EDGE_GW[3]);
    esp_netif_set_ip_info(nif, &info);
    netifReady = true;
    ethUp = true;
    Serial.printf("[eth] 静态 IP %s gw %s (broker %s:%d)\n",
                  EDGE_IP.toString().c_str(), EDGE_GW.toString().c_str(),
                  BROKER_IP.toString().c_str(), BROKER_PORT);
    return true;
}
static void applyStaticIp() {
    for (int i = 0; i < 30 && !tryStaticIp(); i++) delay(500);
    if (!netifReady)
        Serial.println("[eth] netif 未就绪, loop 中每 2s 重试");
}

// ================= 生命周期 =================
void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println("\n[edge101] boot - PCB sort EXECUTOR (IR trigger + HL2 I2C)");

    pinMode(STEPPER_STEP_PIN, OUTPUT);
    pinMode(STEPPER_DIR_PIN, OUTPUT);
    digitalWrite(STEPPER_DIR_PIN, LOW);
    pinMode(IR_PIN, INPUT);            // GPIO34 纯输入 (需外接 3.3V 上拉)

    // RMT 步进脉冲
    {
        rmt_tx_channel_config_t cc = {};
        cc.gpio_num = (gpio_num_t)STEPPER_STEP_PIN;
        cc.clk_src = RMT_CLK_SRC_APB;
        cc.resolution_hz = 80000000;
        cc.mem_block_symbols = 64;
        cc.trans_queue_depth = 2;
        cc.intr_priority = 0;
        rmt_copy_encoder_config_t ec = {};
        esp_err_t e1 = rmt_new_tx_channel(&cc, &stepChan);
        esp_err_t e2 = (e1 == ESP_OK) ? rmt_new_copy_encoder(&ec, &stepEnc) : ESP_FAIL;
        if (e1 == ESP_OK && e2 == ESP_OK) {
            stepRmtReady = true;
            Serial.printf("[step] RMT @GPIO%d 硬件脉冲就绪\n", STEPPER_STEP_PIN);
        } else {
            Serial.printf("[step] RMT 初始化失败 (ch=%d enc=%d), 回退主循环\n",
                          (int)e1, (int)e2);
        }
    }

    // 舵机 x3: LEDC 50Hz/15bit, 上电即归位 (收回 270°).
    for (int i = 0; i < 3; i++) {
        ledcAttach(SERVO_PIN[i], 50, 15);
        ledcWrite(SERVO_PIN[i], servoDutyRaw(SERVO_REST_DEG));
    }
    Serial.printf("[servo] LEDC @50Hz 15bit: GPIO%d/%d/%d 归位 %d° (夹值 -120..280)\n",
                  SERVO_PIN[0], SERVO_PIN[1], SERVO_PIN[2], SERVO_REST_DEG);

    if (mcpEn.begin(&mcpBus, conv.state()))
        Serial.printf("[mcp] MCP23017 @0x%02X 在线\n", sort::McpEn::kAddr);
    else
        Serial.println("[mcp] MCP23017 未检测到 (步进 EN 浮空=使能, 不影响)");

    Serial.printf("[ir] 红外位置基准 GPIO%d: d1=%.0fmm spacing=%.0fmm 接近=%s (irset/spset 可调)\n",
                  IR_PIN, g_ir_d1_mm, g_ir_spacing, g_ir_low ? "low" : "high");

#if HL_I2C_ON
    Wire.begin(HL2_I2C_SDA_PIN, HL2_I2C_SCL_PIN);
    Wire.setClock(400000);            // 400kHz
    bool hlOk = hl.begin(Wire);
    Serial.printf("[hl2] I2C @0x50 SDA=GPIO%d SCL=GPIO%d 400kHz begin=%s algo=%d confmin=%.2f\n",
                  HL2_I2C_SDA_PIN, HL2_I2C_SCL_PIN, hlOk ? "ok" : "FAIL",
                  HL_ALGO, g_confmin);
    if (hlOk) {
        // 切到自学习分类 (App 训练的 PCB 模型) + 强制等 5s (wiki: 否则崩溃).
        bool sw = hl.switchAlgorithm((eAlgorithm_t)HL_ALGO);
        Serial.printf("[hl2] switchAlgorithm(algo=%d)=%s, 等待 %lums\n",
                      HL_ALGO, sw ? "ok" : "n/a",
                      (unsigned long)HL_SWITCH_WAIT_MS);
        delay(HL_SWITCH_WAIT_MS);
    }
#else
    Serial.println("[hl2] I2C 未启用 (HL_I2C_ON=0)");
#endif

#ifdef CONFIG_IDF_TARGET_ESP32
    bool phyOk = ETH.begin(ETH_PHY_TLK110, 1, 4, 13, 2, ETH_CLOCK_GPIO0_IN);
    Serial.println(phyOk ? "[eth] PHY/驱动初始化 OK"
                         : "[eth] ETH.begin 失败 (PHY 初始化?)");
    WiFi.onEvent(ethEvent);
    applyStaticIp();
#else
    Serial.println("[eth] 非 ESP32 原版目标: 无板载以太网, MQTT 不可用");
#endif

    mqtt.setServer(BROKER_IP, BROKER_PORT);
    mqtt.setCallback(onMqttMessage);
    Serial.println("[edge101] ready (serial: help)");
}

static uint32_t lastTeleMs = 0;
static uint32_t lastMqttTryMs = 0;
static uint32_t lastEthTryMs = 0;

void loop() {
    steerStepper();
    mcpEn.sync(conv.state());

    // 红外: 沿判 (物品前缘到 IR = T0).
    bool pres = irItemPresent();
    if (pres && !g_ir_prev) {
        g_ir_prev = true;
        g_ir_count++;
        onIrEdge();
    } else if (!pres) {
        g_ir_prev = false;
    }

    pushTick();
    cmdTimeoutTick();

    // 以太网/MQTT 重试 + 循环.
    if (!ethUp && millis() - lastEthTryMs > 2000) {
        lastEthTryMs = millis();
        tryStaticIp();
    }
    if (!mqtt.connected() && ethUp && millis() - lastMqttTryMs > 2000) {
        lastMqttTryMs = millis();
        mqttConnect();
    }
    mqtt.loop();

#if HL_I2C_ON
    hlTick();
#endif

    if (mqtt.connected() && millis() - lastTeleMs >= 500) {
        lastTeleMs = millis();
        mqtt.publish(sort::conveyorTeleTopic().c_str(),
                     sort::conveyorTele(conv.state(), conv.speed_mps,
                                        stepsLive(), "").c_str(), false);
        for (int n = 1; n <= 3; n++)
            mqtt.publish(sort::sorterTeleTopic(n).c_str(),
                         sort::sorterTele(curCmd.active && curCmd.bin == n ? "busy" : "ready",
                                          g_diverted[n - 1],
                                          curCmd.active && curCmd.bin == n
                                              ? curCmd.material.c_str() : "").c_str(),
                         false);
    }

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') { onSerialCmd(consoleBuf); consoleBuf = ""; }
        else if (c != '\r') consoleBuf += c;
    }
}
