// EDGE 101 (DFR1283, ESP32 原版) 分拣控制器 — 实现 docs/sort-mqtt.md
// 板载 RJ45 以太网 + HuskyLens 2 (UART, GPIO5/12) + 步进 (STEP=14 DIR=15)
// + 3 路舵机 (PCA9685 I2C, SDA=18 SCL=23, 通道 0/1/2) + 2Hz 遥测
//
// 引脚/网络全部在 include/config.h (依据 DFRobot wiki DFR1283).
// 纯逻辑 (状态机/解析/协议 JSON) 在 lib/SortLogic, 主机可单测.
#include <Arduino.h>
#include <ETH.h>
#include <WiFi.h>
#include <NetworkClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <esp_netif.h>

#include "config.h"
#include "Conveyor.h"
#include "Sorter.h"
#include "HuskyLens.h"
#include "Proto.h"
#include "ServoI2C.h"
#include "McpEn.h"

static sort::Conveyor conv;
static sort::Sorter sorters[3] = {sort::Sorter(1), sort::Sorter(2), sort::Sorter(3)};
static sort::HuskyLens hl2(MATERIAL_MAP, MATERIAL_MAP_LEN, HL2_RETRIGGER_MS);

static HardwareSerial hlSerial(2);            // UART2 重映射到 GPIO5/12
static NetworkClient ethClient;               // 3.x: 以太网客户端 (自动绑定默认 netif)
static PubSubClient mqtt(ethClient);
static ServoI2C servos;

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
};
static WireMcpBus mcpBus;
static sort::McpEn mcpEn;

// 以太网状态 (tryStaticIp/applyStaticIp 见下文以太网节; 提前声明供事件回调使用)
static volatile bool ethUp = false;    // 链路可用 (静态 IP 已配好或 DHCP 拿到)
static bool netifReady = false;        // 静态 IP 已设置 (幂等, 不重复设置)
static esp_netif_t* findEthNetif();    // 定义见以太网节 (串口 eth 命令提前引用)

// ================= 步进: 非阻塞步脉冲 =================
static uint32_t nextStepUs = 0;
static void steerStepper() {
    if (conv.st != sort::DevState::kRunning) return;
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

// ================= 舵机扫掠任务 (非阻塞) =================
// 一件物料 = 一次 拨出->归位 扫掠; qty=N 则 N 次, 件间 SERVO_ITEM_GAP_MS.
// 与 sim_devices 语义一致: 全部拨完才发 ack ok.
struct SweepTask {
    int n = 0;             // 分拣器 1..3
    bool active = false;
    bool inEject = false;
    uint32_t phaseAt = 0;
};
static SweepTask sweep;
static String lastJobId[3] = {"", "", ""};   // 各分拣器当前 job_id (divert 完成时应答用)

static void servoWrite(int n, int deg) {
    servos.setAngle(SERVO_CH[n - 1], deg);
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

static void sweepStart(int n) {
    sweep.n = n; sweep.active = true; sweep.inEject = true;
    sweep.phaseAt = millis();
    servoWrite(n, SERVO_EJECT_DEG);
}
static void sweepTick() {
    if (!sweep.active) return;
    uint32_t now = millis();
    if (sweep.inEject) {
        if (now - sweep.phaseAt >= SERVO_SWEEP_MS / 2) {
            servoWrite(sweep.n, SERVO_REST_DEG);
            sweep.inEject = false;
            sweep.phaseAt = now;
        }
    } else if (now - sweep.phaseAt >= (uint32_t)(SERVO_ITEM_GAP_MS - SERVO_SWEEP_MS / 2)) {
        if (sorters[sweep.n - 1].stepOne()) {      // 最后一件
            sweep.active = false;
            publishSorterAck(sweep.n, lastJobId[sweep.n - 1].c_str(), "divert",
                             "ok", sorters[sweep.n - 1].mat());
        } else {
            sweep.inEject = true;
            sweep.phaseAt = now;
            servoWrite(sweep.n, SERVO_EJECT_DEG);
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
    } else {
        int n = 0;
        for (n = 1; n <= 3; n++) if (t == sort::sorterCmdTopic(n)) break;
        if (n > 3) return;
        if (action && !strcmp(action, "divert")) lastJobId[n - 1] = job_id;
        const char* material = doc["material"] | "";
        int qty = doc["qty"] | 1;
        auto r = sorters[n - 1].handleCmd(action, material, qty);
        if (!r.handled) return;
        if (action && !strcmp(action, "reset")) {
            publishSorterAck(n, job_id, "reset", r.ok ? "ok" : "busy",
                             r.note.c_str());
        } else if (r.ok) {
            sweepStart(n);    // divert: 拨完才应答
        } else {
            const char* res = r.note == "sorter fault" ? "fault" : "busy";
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
static void onVision(const sort::VisionEvent& ev, uint32_t now_ms, void*) {
    char fid[24];
    snprintf(fid, sizeof(fid), "hl2-%lu", (unsigned long)ev.seq);
    std::string p = sort::visionJson(ev.material.c_str(), 1.0, fid,
                                     ev.x, ev.y, ev.w, ev.h);
    mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
    Serial.printf("[vision] %s id=%s\n", ev.material.c_str(), fid);
    (void)now_ms;
}

// ================= 串口控制台 (调试) =================
static String consoleBuf;
static void onSerialCmd(const String& line) {
    if (line == "help") {
        Serial.println("help | state | eth | mqtt | stepper <n> | sweep <n> | vision <material>");
    } else if (line == "state") {
        Serial.printf("conveyor=%s s1=%s s2=%s s3=%s diverted=%lu/%lu/%lu steps=%llu\n",
                      conv.state(), sorters[0].state(), sorters[1].state(),
                      sorters[2].state(), (unsigned long)sorters[0].diverted_total,
                      (unsigned long)sorters[1].diverted_total,
                      (unsigned long)sorters[2].diverted_total,
                      (unsigned long long)conv.steps_total);
    } else if (line.startsWith("stepper")) {
        int n = line.substring(8).toInt();
        if (mcpEn.present()) mcpEn.forceLevel(true);   // 手动脉冲: 临时使能
        for (int i = 0; i < n; i++) {
            digitalWrite(STEPPER_STEP_PIN, HIGH); delayMicroseconds(2);
            digitalWrite(STEPPER_STEP_PIN, LOW); delay(2);
        }
        if (mcpEn.present()) mcpEn.forceLevel(false);  // 发完恢复禁能
        Serial.printf("stepper %d done\n", n);
    } else if (line.startsWith("sweep")) {
        int n = line.substring(7).toInt();
        if (n >= 1 && n <= 3) {
            servoWrite(n, SERVO_EJECT_DEG); delay(450);
            servoWrite(n, SERVO_REST_DEG);
            Serial.printf("sweep %d done\n", n);
        }
    } else if (line.startsWith("vision")) {
        String m = line.substring(8);
        m.trim();
        std::string p = sort::visionJson(m.c_str(), 1.0, "console", 0, 0, 0, 0);
        mqtt.publish(sort::visionTopic().c_str(), p.c_str(), true);
        Serial.printf("vision %s published\n", m.c_str());
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
                     (ip.ip.addr >> 24) & 0xFF, (ip.ip.addr >> 16) & 0xFF,
                     (ip.ip.addr >> 8) & 0xFF, ip.ip.addr & 0xFF);
            snprintf(gwstr, sizeof(gwstr), "%d.%d.%d.%d",
                     (ip.gw.addr >> 24) & 0xFF, (ip.gw.addr >> 16) & 0xFF,
                     (ip.gw.addr >> 8) & 0xFF, ip.gw.addr & 0xFF);
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

    servos.begin(SERVO_SDA_PIN, SERVO_SCL_PIN);
    for (int i = 0; i < 3; i++) servos.setAngle(SERVO_CH[i], SERVO_REST_DEG);
    Serial.printf("[servo] PCA9685 @0x%02X ch %d/%d/%d\n",
                  SERVO_ADDR, SERVO_CH[0], SERVO_CH[1], SERVO_CH[2]);

    if (mcpEn.begin(&mcpBus, conv.state()))
        Serial.printf("[mcp] MCP23017 @0x%02X 在线, 步进 EN -> PA0 (停带禁能)\n",
                      sort::McpEn::kAddr);
    else
        Serial.println("[mcp] MCP23017 未检测到, 步进 EN 保持外部跳线 (5V 常使能)");

    hlSerial.begin(HL2_BAUD, SERIAL_8N1, HL2_RX_PIN, HL2_TX_PIN);
    Serial.printf("[hl2] UART2 @%lu rx=GPIO%d tx=GPIO%d\n",
                  (unsigned long)HL2_BAUD, HL2_RX_PIN, HL2_TX_PIN);

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
    if (!ethUp && millis() - lastEthTryMs > 2000) {     // netif 晚到: 持续重试
        lastEthTryMs = millis();
        tryStaticIp();
    }
    if (!mqtt.connected() && ethUp && millis() - lastMqttTryMs > 2000) {
        lastMqttTryMs = millis();
        mqttConnect();
    }
    mqtt.loop();

    while (hlSerial.available())
        hl2.feedByte((uint8_t)hlSerial.read(), millis(), onVision, nullptr);

    if (mqtt.connected() && millis() - lastTeleMs >= 500) {
        lastTeleMs = millis();
        mqtt.publish(sort::conveyorTeleTopic().c_str(),
                     sort::conveyorTele(conv.state(), conv.speed_mps,
                                        conv.steps_total).c_str(), false);
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
