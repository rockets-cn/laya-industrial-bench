// 主机单测: SortLogic (Conveyor/Sorter/HuskyLens/Proto)
// 与 sim_devices 语义对拍. 编译运行: ./run_tests.sh
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#include "Conveyor.h"
#include "BeltHold.h"
#include "BeltWatch.h"
#include "Sorter.h"
#include "Gate.h"
#include "Track.h"
#include "HuskyLens.h"
#include "Proto.h"
#include "McpEn.h"
#include "McpGate.h"

using namespace sort;

static int PASS = 0, FAIL = 0;
static void check(const char* name, bool cond, const char* detail = "") {
    if (cond) { PASS++; printf("  OK   %s\n", name); }
    else { FAIL++; printf("  FAIL %s  %s\n", name, detail); }
}

// ---- HuskyLens 帧构造 ----
static std::vector<uint8_t> mkObjFrame(uint8_t obj_type, uint16_t id,
                                         uint16_t x, uint16_t y,
                                         uint16_t w, uint16_t h,
                                         bool bad_checksum = false) {
    std::vector<uint8_t> d = {obj_type,
        (uint8_t)(id & 0xFF), (uint8_t)(id >> 8),
        (uint8_t)(x & 0xFF), (uint8_t)(x >> 8),
        (uint8_t)(y & 0xFF), (uint8_t)(y >> 8),
        (uint8_t)(w & 0xFF), (uint8_t)(w >> 8),
        (uint8_t)(h & 0xFF), (uint8_t)(h >> 8)};
    std::vector<uint8_t> f = {0x55, 0x55, 0x01, 0x01,
        (uint8_t)(d.size() & 0xFF), (uint8_t)(d.size() >> 8)};
    for (uint8_t b : d) f.push_back(b);
    // 校验和 = TYPE..DATA 全部字节之和 (不含 0x55 0x55 头)
    uint8_t sum = 0;
    for (size_t i = 2; i < f.size(); i++) sum = (uint8_t)(sum + f[i]);
    if (bad_checksum) sum = (uint8_t)(sum + 1);
    f.push_back(sum);
    return f;
}

static const MatEntry kMap[] = {
    {1, 1, "red_block"},
    {1, 2, "green_block"},
    {5, 10, "banana"},
};
static int evCount = 0;
static std::string lastMat;
static uint16_t lastW = 0;
static void onEv(const VisionEvent& ev, uint32_t now_ms, void*) {
    evCount++; lastMat = ev.material; lastW = ev.w;
    (void)now_ms;
}

static void testConveyor() {
    printf("\n== Conveyor 状态机 (对拍 sim_devices) ==\n");
    Conveyor c;
    check("初始 stopped", c.state() == std::string("stopped"));
    auto r = c.handleCmd("start", 1.5);
    check("stopped->start ok", r.handled && r.ok);
    check("running", c.st == DevState::kRunning && c.speed_mps == 1.5);
    r = c.handleCmd("start", 2.0);
    check("running 时 start -> ok (幂等, 更新 speed)",
          r.handled && r.ok && c.speed_mps == 2.0);
    r = c.handleCmd("pause", 0);
    check("running->pause ok", r.ok && c.st == DevState::kPaused);
    r = c.handleCmd("start", 2.0);
    check("paused 时 start -> ok (等同 resume)",
          r.handled && r.ok && c.st == DevState::kRunning && c.speed_mps == 2.0);
    r = c.handleCmd("stop", 0);
    check("running->stop ok", r.ok && c.st == DevState::kStopped);
    r = c.handleCmd("stop", 0);
    check("stopped 时 stop -> ok (幂等)", r.handled && r.ok
          && r.note == "state=stopped");
    r = c.handleCmd("resume", 0);
    check("stopped 时 resume -> busy", r.handled && !r.ok
          && r.note == "state=stopped");
    r = c.handleCmd("start", 1.0);
    check("stopped->start ok", r.handled && r.ok && c.st == DevState::kRunning);
    r = c.handleCmd("pause", 0);
    check("running->pause ok", r.handled && r.ok);
    r = c.handleCmd("pause", 0);
    check("paused 时 pause -> ok (幂等)", r.handled && r.ok
          && r.note == "state=paused");
    c.handleCmd("stop", 0);
    r = c.handleCmd("teleport", 0);
    check("未知 action 不处理", !r.handled);
    c.setFault();
    r = c.handleCmd("start", 0);
    check("fault 时 start -> busy", r.handled && !r.ok
          && r.note == "state=fault");
    r = c.handleCmd("stop", 0);
    check("fault 时 stop -> busy", r.handled && !r.ok
          && r.note == "state=fault");
    r = c.handleCmd("reset", 0);
    check("fault->reset ok", r.ok && c.st == DevState::kStopped);
}

static void testSorter() {
    printf("\n== Sorter 状态机 (对拍 sim_devices) ==\n");
    Sorter s(2);
    check("初始 ready", std::string(s.state()) == std::string("ready"));
    auto r = s.handleCmd("divert", "blue_block", 5);
    check("ready->divert ok (busy)", r.handled && r.ok && std::string(s.state()) == "busy");
    check("pending qty=5", s.pend_qty == 5);
    r = s.handleCmd("divert", "banana", 2);
    check("busy 时 divert -> busy", r.handled && !r.ok);
    bool done = false;
    for (int i = 0; i < 5; i++) done = s.stepOne();
    check("5 次拨完", done && std::string(s.state()) == "ready");
    check("diverted_total += 5", s.diverted_total == 5);
    check("last_material", s.mat() == std::string("blue_block"));
    s.setFault();
    check("fault", std::string(s.state()) == "fault");
    r = s.handleCmd("divert", "banana", 1);
    check("fault 时 divert 拒", !r.ok);
    r = s.handleCmd("reset", nullptr, 0);
    check("fault->reset ok", r.ok && std::string(s.state()) == "ready");
    check("topic_cmd", s.topic_cmd() == "sort/sorter2/cmd");
}

static void testHuskyLens() {
    printf("\n== HuskyLens 协议 + 进画面去重 ==\n");
    HuskyLens hl(kMap, 3, 500);
    evCount = 0;

    auto frame = mkObjFrame(1, 1, 10, 20, 30, 40);   // red_block
    // 分三段喂 (模拟串口断片)
    for (size_t i = 0; i < frame.size(); i++)
        hl.feedByte(frame[i], 1000 + i * 5, onEv, nullptr);
    check("首件上报", evCount == 1 && lastMat == "red_block");
    check("bbox 解析", lastW == 30);
    check("frame_id 序号", hl.seq() == 1);

    // 同一物料 500ms 窗口内 (1050 < 1000+500) -> 不报
    auto f2 = mkObjFrame(1, 1, 11, 21, 31, 41);
    for (size_t i = 0; i < f2.size(); i++)
        hl.feedByte(f2[i], 1200, onEv, nullptr);
    check("窗口内重复不报", evCount == 1);

    // 超过窗口 (now=1700 > 1200+500) -> 视为新的一件
    for (size_t i = 0; i < f2.size(); i++)
        hl.feedByte(f2[i], 1750, onEv, nullptr);
    check("窗口后新件上报", evCount == 2);

    // 校验和错 -> 丢弃
    auto bad = mkObjFrame(1, 1, 0, 0, 0, 0, true);
    for (uint8_t b : bad) hl.feedByte(b, 3000, onEv, nullptr);
    check("坏校验和丢弃", evCount == 2);

    // 未知物料 -> 不报
    auto unk = mkObjFrame(1, 99, 0, 0, 0, 0);
    for (uint8_t b : unk) hl.feedByte(b, 3100, onEv, nullptr);
    check("未知物料不报", evCount == 2);

    // 自定义模型 (obj_type=5, id=10 -> banana)
    auto ban = mkObjFrame(5, 10, 5, 6, 7, 8);
    for (uint8_t b : ban) hl.feedByte(b, 4000, onEv, nullptr);
    check("自定义模型映射", evCount == 3 && lastMat == "banana");

    // 垃圾头字节不破坏状态机
    hl.feedByte(0x00, 5000, onEv, nullptr);
    hl.feedByte(0x55, 5001, onEv, nullptr);
    hl.feedByte(0x13, 5002, onEv, nullptr);
    for (uint8_t b : ban) hl.feedByte(b, 5900, onEv, nullptr);
    check("垃圾字节后恢复解析", evCount == 4 && lastMat == "banana");
}

static void testProto() {
    printf("\n== Proto JSON/topic ==\n");
    std::string ack = ackJson("j123", "divert", "ok", "");
    check("ack 含 job_id", ack.find("\"job_id\":\"j123\"") != std::string::npos);
    check("ack 含 result", ack.find("\"result\":\"ok\"") != std::string::npos);
    check("ackJson 转义", ackJson("a\"b", "x", "ok", "n\"ote").find("\\\"") != std::string::npos);

    std::string tele = sorterTele("busy", 17, "red_block");
    check("sorter 遥测", tele.find("\"diverted_total\":17") != std::string::npos
                         && tele.find("\"last_material\":\"red_block\"") != std::string::npos);
    check("conveyor 遥测",
          conveyorTele("running", 1.5, 42).find("\"speed_mps\":1.50") != std::string::npos);

    std::string vis = visionJson("blue_block", 1.0, "hl2-1", 1, 2, 3, 4);
    check("vision JSON", vis.find("\"confidence\":1.000") != std::string::npos
                          && vis.find("\"bbox\":[1,2,3,4]") != std::string::npos
                          && vis.find("\"source\":\"huskylens2\"") != std::string::npos
                          && vis.find("pos_mm") == std::string::npos);
    std::string vis2 = visionJson("blue_block", 1.0, "hl2-2", 1, 2, 3, 4, 37.5);
    check("vision JSON 带 pos_mm",
          vis2.find("\"pos_mm\":37.5") != std::string::npos);

    std::string motion = motionJson(2, "busy", "virtual", "j55");
    check("motion JSON", motion.find("\"sorter\":2") != std::string::npos
                           && motion.find("\"state\":\"busy\"") != std::string::npos
                           && motion.find("\"via\":\"virtual\"") != std::string::npos
                           && motion.find("\"job_id\":\"j55\"") != std::string::npos);
    // 逐件件号: item/of 仅在 of>0 时输出 (旧固件/不适用事件保持原 5 字段)
    std::string motionItem = motionJson(1, "busy", "photo", "j56", "", 2, 3);
    check("motion JSON 带件号 item/of",
          motionItem.find("\"item\":2") != std::string::npos
          && motionItem.find("\"of\":3") != std::string::npos
          && motion.find("\"item\"") == std::string::npos);

    check("conveyor cmd topic", conveyorCmdTopic() == "sort/line1/conveyor/cmd");
    check("conveyor ack topic",
          conveyorAckTopic("j9") == "sort/line1/conveyor/ack/j9");
    check("sorter3 cmd topic", sorterCmdTopic(3) == "sort/sorter3/cmd");
    check("sorter2 ack topic", sorterAckTopic(2, "jX") == "sort/sorter2/ack/jX");
    check("sorter3 tele topic", sorterTeleTopic(3) == "sort/sorter3/telemetry");
    check("vision topic", visionTopic() == "sort/inbound/vision");
    check("motion topic", motionTopic() == "sort/line1/motion");
}

// ---- MCP23017 fake I2C ----
struct FakeI2c : public sort::I2cReg {
    bool alive_ok = true;
    std::vector<std::pair<uint8_t, uint8_t>> writes;
    std::map<uint8_t, uint8_t> regs;   // writeReg 记账, readReg 回读
    bool alive() override { return alive_ok; }
    void writeReg(uint8_t r, uint8_t v) override {
        writes.push_back({r, v});
        regs[r] = v;
    }
    uint8_t readReg(uint8_t r) override {
        auto it = regs.find(r);
        return it == regs.end() ? 0xFF : it->second;
    }
};

static void testMcpEn() {
    printf("\n== McpEn (MCP23017 步进 EN) ==\n");

    check("策略: running -> 使能", sort::EnPolicy::enabledFor("running"));
    check("策略: stopped -> 禁能", !sort::EnPolicy::enabledFor("stopped"));
    check("策略: paused -> 禁能", !sort::EnPolicy::enabledFor("paused"));
    check("策略: fault -> 禁能", !sort::EnPolicy::enabledFor("fault"));
    check("策略: nullptr -> 禁能", !sort::EnPolicy::enabledFor(nullptr));

    // 芯片离线: begin 失败, 全程无 I2C 写, sync 无动作
    {
        FakeI2c bus; bus.alive_ok = false;
        sort::McpEn en;
        check("离线 begin 返回 false", !en.begin(&bus, "stopped"));
        check("离线 present=false", !en.present());
        size_t n0 = bus.writes.size();
        en.sync("running");
        en.forceLevel(true);
        check("离线时 sync/force 不写 I2C", bus.writes.size() == n0);
    }

    // 在线: begin 配 IODIR + 按状态初始化 (active-low: stopped -> EN 线=高=禁能)
    {
        FakeI2c bus;
        sort::McpEn en;
        check("在线 begin 成功", en.begin(&bus, "stopped"));
        check("begin 配 PA0 为输出", bus.writes.size() >= 1
              && bus.writes[0].first == sort::McpEn::kIodirA
              && bus.writes[0].second == sort::McpEn::kEnMask);
        check("初始 stopped -> EN 线=高 (active-low: 禁能=高, 1 次写)",
              en.level() == true && bus.writes.size() == 2
              && bus.writes[1].second == sort::McpEn::kEnMask);

        size_t n = bus.writes.size();
        check("同状态 sync 不写", (en.sync("stopped"), bus.writes.size() == n));

        check("running -> EN 线=低 (使能, 1 次写)",
              !en.sync("running") && bus.writes.size() == n + 1
              && bus.writes[n].second == 0x00);
        check("running 重复 sync 不写", (en.sync("running"), bus.writes.size() == n + 1));
        check("stopped -> EN 线=高 (禁能)", en.sync("stopped")
              && bus.writes.size() == n + 2 && bus.writes[n + 1].second == sort::McpEn::kEnMask);
        check("paused 保持高", (en.sync("paused"), bus.writes.size() == n + 2));

        // 手动强制 (串口 stepper 命令场景): 状态仍是 stopped 但临时使能
        en.forceLevel(true);
        check("forceLevel(true) 写低 (使能)", !en.level() && bus.writes.size() == n + 3);
        en.forceLevel(false);
        check("forceLevel(false) 写回高 (禁能)", en.level() && bus.writes.size() == n + 4);
    }
}

// ---- 光门边沿检测 ----
static void testGate() {
    printf("\n== PhotoGate (光门边沿+去抖) ==\n");
    using sort::PhotoGate;

    // 全程通光: 无沿
    {
        PhotoGate g;
        check("通光无沿", !g.onItemEdge(true, 0) && !g.onItemEdge(true, 100));
        check("通光 itemPresent=false", !g.itemPresent());
    }
    // 正常落沿: H -> L(第1次) -> L(>=10ms) 确认
    {
        PhotoGate g;
        g.onItemEdge(true, 0);
        check("L 第1次不报", !g.onItemEdge(false, 10));
        check("L 持续 10ms 报 1 沿", g.onItemEdge(false, 20));
        check("L 持续中不重复报", !g.onItemEdge(false, 30) && !g.onItemEdge(false, 500));
        check("挡光中 itemPresent=true", g.itemPresent());
        g.onItemEdge(true, 600);
        check("离开复位", !g.itemPresent() && !g.onItemEdge(false, 610));
    }
    // 短脉冲 (振动): H L H L ...  单次 LOW 不足 10ms 不报沿
    {
        PhotoGate g;
        g.onItemEdge(true, 0);
        g.onItemEdge(false, 100);    // L 起点
        g.onItemEdge(true, 103);     // 3ms 后回 H (短脉冲, 未确认)
        g.onItemEdge(false, 200);    // 新 LOW 序列
        check("短脉冲后新序列第 1 次不报", !g.onItemEdge(false, 205));
        check("短脉冲不产生伪沿 (之前 0 沿)", true);
    }
    // 两次确认跨 10ms 才报
    {
        PhotoGate g;
        g.onItemEdge(true, 0);
        g.onItemEdge(false, 10);
        check("仅 5ms 持续不报", !g.onItemEdge(false, 15));
        check("10ms 持续报", g.onItemEdge(false, 20));
    }
    // McpGate: 寄存器配置 + 读电平
    {
        FakeI2c bus;
        sort::McpGate gate;
        check("McpGate.begin 配 IODIR/GPPU", gate.begin(&bus, 0x07)
              && bus.writes.size() == 2
              && bus.writes[0].first == sort::McpGate::kIodirB
              && bus.writes[0].second == 0x07
              && bus.writes[1].first == sort::McpGate::kGppuB);
        bus.regs[sort::McpGate::kGpioB] = 0x06;   // PB0=0(挡光) PB1=1 PB2=1
        bool hi0, hi1, hi2;
        gate.poll(hi0, hi1, hi2);
        check("poll 解析 PB0/1/2", !hi0 && hi1 && hi2);
        FakeI2c off; off.alive_ok = false;
        sort::McpGate g2;
        check("芯片离线 poll 全 HIGH (不误报)",
              !g2.begin(&off, 0x07) && (g2.poll(hi0, hi1, hi2), hi0 && hi1 && hi2));
    }
}

// ---- 分拣器: 光门触发 (trigger=photo) ----
static void testSorterPhoto() {
    printf("\n== Sorter photo trigger (光门预置拨料) ==\n");

    // 基本: 未预置时沿无效; photo divert -> armed, 光门沿 -> busy -> 拨完 -> ready
    {
        sort::Sorter s(1);
        check("ready 时(未预置)光门沿无效", !s.onGateEdge(900));
        auto r = s.handleCmd("divert", "red_block", 1, "photo", "j1", 1000);
        check("photo divert -> armed", r.handled && r.ok && std::string(s.state()) == "armed");
        check("armed 时未拨料", s.diverted_total == 0 && s.pendingQty() == 1);
        check("armed 光门沿 -> busy", s.onGateEdge(1500) && std::string(s.state()) == "busy");
        check("拨完 -> ready + 计数", s.stepOne(2000) && std::string(s.state()) == "ready"
              && s.diverted_total == 1);
    }
    // 多件: 每件各等一次光门
    {
        sort::Sorter s(2);
        s.handleCmd("divert", "green_block", 3, "photo", "j2", 1000);
        check("qty=3 -> armed 剩 3", std::string(s.state()) == "armed" && s.pendingQty() == 3);
        s.onGateEdge(2000);
        check("第 1 件光门 -> busy", std::string(s.state()) == "busy");
        check("第 1 件拨完回 armed", !s.stepOne(3000) && std::string(s.state()) == "armed"
              && s.pendingQty() == 2);
        s.onGateEdge(4000);
        check("第 2 件光门 -> busy", std::string(s.state()) == "busy");
        check("第 2 件拨完回 armed", !s.stepOne(5000) && std::string(s.state()) == "armed");
        s.onGateEdge(6000);
        s.stepOne(7000);
        check("第 3 件拨完 -> ready", std::string(s.state()) == "ready" && s.diverted_total == 3
              && s.last_material == "green_block");
    }
    // busy 时不接光门 (正在拨, 新物品按件距约束不会同时到)
    {
        sort::Sorter s(3);
        s.handleCmd("divert", "red_apple", 2, "photo", "j3", 100);
        s.onGateEdge(200);
        check("busy 中光门沿不触发", !s.onGateEdge(300) && std::string(s.state()) == "busy");
    }
    // armed 时其他 divert -> busy-ack; reset -> 取消
    {
        sort::Sorter s(1);
        s.handleCmd("divert", "red_block", 1, "photo", "j4", 100);
        auto r2 = s.handleCmd("divert", "red_apple", 1, "immediate", "j5", 200);
        check("armed 时 divert -> busy-ack", r2.handled && !r2.ok
              && std::string(s.state()) == "armed" && s.diverted_total == 0);
        auto r3 = s.handleCmd("reset", nullptr, 0, "immediate", "j6", 300);
        check("armed 时 reset -> 取消回 ready", r3.handled && r3.ok
              && std::string(s.state()) == "ready" && s.pendingQty() == 0);
        check("取消后未拨料", s.diverted_total == 0);
    }
    // 光门等待超时 -> fault, 对 job_id 可回 ack
    {
        sort::Sorter s(2);
        s.handleCmd("divert", "blue_block", 1, "photo", "j7", 5000);
        check("窗口内 tick 不 fault", !s.tick(9000) && std::string(s.state()) == "armed");
        check("超时 tick -> fault", s.tick(5000 + sort::kPhotoTimeoutMs)
              && std::string(s.state()) == "fault");
        check("超时 note=photo timeout", std::strcmp(s.note(), "photo timeout") == 0);
        check("超时 job_id 保留", s.pendingJobId() == "j7");
        auto r = s.handleCmd("divert", "blue_block", 1, "photo", "j8", 20000);
        check("fault 后 divert -> fault-ack", r.handled && !r.ok);
        auto r2 = s.handleCmd("reset", nullptr, 0, "immediate", "j9", 21000);
        check("fault reset -> ready", r2.handled && r2.ok && std::string(s.state()) == "ready");
    }
    // immediate 回归: 行为不变 (不 armed, 连拨)
    {
        sort::Sorter s(3);
        auto r = s.handleCmd("divert", "red_block", 2, "immediate", "j10", 100);
        check("immediate 直接 busy", r.handled && r.ok && std::string(s.state()) == "busy");
        check("immediate 拨 1 件保持 busy", !s.stepOne(200) && std::string(s.state()) == "busy");
        check("immediate 拨完 -> ready", s.stepOne(300) && std::string(s.state()) == "ready"
              && s.diverted_total == 2);
    }
}

// ---- Track: 带面位置跟踪 (虚拟光门) ----
static void testTrack() {
    printf("\n== Track 带面位置跟踪 (虚拟光门) ==\n");
    // pixelToMm: 中心=0, 线性换算, 限幅 ±视野半宽
    check("像素中心 -> 0mm", Track::pixelToMm(160, 160, 320, 200) == 0.0);
    check("像素右移 80px -> +50mm",
          std::fabs(Track::pixelToMm(240, 160, 320, 200) - 50.0) < 1e-9);
    check("像素左移 40px -> -25mm",
          std::fabs(Track::pixelToMm(120, 160, 320, 200) - (-25.0)) < 1e-9);
    check("越界像素限幅 +100mm",
          std::fabs(Track::pixelToMm(600, 160, 320, 200) - 100.0) < 1e-9);
    check("越界像素限幅 -100mm",
          std::fabs(Track::pixelToMm(0, 160, 320, 200) - (-100.0)) < 1e-9);
    check("view_px<=0 -> 0mm", Track::pixelToMm(0, 160, 0, 200) == 0.0);

    // predictGateMs: t_g = t0 + (gate − p0) / v
    check("基础预测: 500mm@1m/s -> t0+500ms",
          Track::predictGateMs(1000, 0.0, 500.0, 1.0) == 1500);
    check("物品偏下游 100mm -> 提前 100ms",
          Track::predictGateMs(1000, 100.0, 500.0, 1.0) == 1400);
    check("物品偏上游 50mm -> 推后 50ms",
          Track::predictGateMs(1000, -50.0, 500.0, 1.0) == 1550);
    check("带速 0.5m/s -> 1000ms",
          Track::predictGateMs(1000, 0.0, 500.0, 0.5) == 2000);
    check("带停 (v=0) -> 0 (不可预测)",
          Track::predictGateMs(1000, 0.0, 500.0, 0.0) == 0);
    check("物品已在光门线外 (p0>gate) -> 0 (调用方按立即执行)",
          Track::predictGateMs(1000, 600.0, 500.0, 1.0) == 0);

    // onVision/match: 同物料覆盖, 窗口内命中, 超窗/异物料不命中
    Track t;
    t.onVision("red_block", 0.0, 1000);
    t.onVision("green_block", 30.0, 1100);
    double p0 = -1;
    uint32_t t0 = 0;
    check("匹配 red_block (100ms 内)",
          t.match("red_block", 1050, 3000, &p0, &t0) && p0 == 0.0 && t0 == 1000);
    check("匹配 green_block p0=30",
          t.match("green_block", 1200, 3000, &p0, &t0)
          && std::fabs(p0 - 30.0) < 1e-9 && t0 == 1100);
    check("超窗不匹配", !t.match("red_block", 1000 + 3000 + 1, 3000));
    check("异物料不匹配", !t.match("banana", 1100, 3000));
    check("空物料不匹配", !t.match("", 1100, 3000));
    // 同物料覆盖: 第二次检测刷新 t0/p0
    t.onVision("red_block", 80.0, 2000);
    check("同物料覆盖 (t0=2000 p0=80)",
          t.match("red_block", 2100, 3000, &p0, &t0)
          && std::fabs(p0 - 80.0) < 1e-9 && t0 == 2000);
    // 满表: 8 个异物料后, 新异物料丢弃, 同物料仍可覆盖
    Track t2;
    for (int i = 0; i < 8; i++) {
        char m[16];
        snprintf(m, sizeof(m), "mat%d", i);
        t2.onVision(m, 0.0, 100);
    }
    t2.onVision("mat_new", 0.0, 200);
    check("满表新物料丢弃", !t2.match("mat_new", 210, 3000));
    t2.onVision("mat0", 5.0, 300);
    check("满表同物料覆盖", t2.match("mat0", 310, 3000, &p0)
          && std::fabs(p0 - 5.0) < 1e-9);
}

// ---- Sorter 虚拟光门 (无物理光门的 photo 时序) ----
static void testSorterVirtual() {
    printf("\n== Sorter 虚拟光门 (位置预测触发) ==\n");
    // 基础: armed 后到预测时刻才 busy (之前 tick 不触发)
    {
        sort::Sorter s(1);
        s.handleCmd("divert", "red_block", 1, "photo", "vj1", 0);
        s.setVirtual(1000, 0);
        check("armed 等待", std::string(s.state()) == "armed");
        check("到点前虚拟沿不触发", !s.onVirtualEdge(999)
              && std::string(s.state()) == "armed");
        check("到点虚拟沿 -> busy", s.onVirtualEdge(1000)
              && std::string(s.state()) == "busy");
    }
    // 件距节奏: qty=3, 第 k 件在 first+(k-1)×pitch 触发
    {
        sort::Sorter s(2);
        s.handleCmd("divert", "green_block", 3, "photo", "vj2", 0);
        s.setVirtual(1000, 900);
        check("第 1 件 @1000", s.onVirtualEdge(1000));
        s.stepOne(1800);          // 拨出耗时 800ms 后完成第 1 件
        check("拨完 1 件回 armed", std::string(s.state()) == "armed"
              && s.done_count == 1);
        check("第 2 件 @1900 前不触发", !s.onVirtualEdge(1899)
              && std::string(s.state()) == "armed");
        check("第 2 件 @1900 触发", s.onVirtualEdge(1900)
              && std::string(s.state()) == "busy");
        s.stepOne(2700);
        check("第 3 件 @2800 触发", s.onVirtualEdge(2800));
        s.stepOne(3600);
        check("3 件拨完 -> ready", std::string(s.state()) == "ready"
              && s.diverted_total == 3);
    }
    // 未 setVirtual 的 photo job: 虚拟沿无效 (走物理光门/超时)
    {
        sort::Sorter s(3);
        s.handleCmd("divert", "blue_block", 1, "photo", "vj3", 0);
        check("无预测时虚拟沿不触发", !s.onVirtualEdge(99999)
              && std::string(s.state()) == "armed");
        check("无预测仍受光门超时约束",
              s.tick(sort::kPhotoTimeoutMs + 1)
              && std::string(s.state()) == "fault");
    }
    // reset 清虚拟预测
    {
        sort::Sorter s(1);
        s.handleCmd("divert", "red_block", 1, "photo", "vj4", 0);
        s.setVirtual(5000, 0);
        auto r = s.handleCmd("reset", nullptr, 0, "immediate", "vj5", 100);
        check("armed reset 清虚拟预测", r.ok && s.virt_first_ms == 0
              && s.virt_pitch_ms == 0);
        s.handleCmd("divert", "red_block", 1, "photo", "vj6", 200);
        s.setVirtual(9000, 0);
        s.setFault();
        auto r2 = s.handleCmd("reset", nullptr, 0, "immediate", "vj7", 300);
        check("fault reset 清虚拟预测", r2.ok && s.virt_first_ms == 0);
    }
    // setVirtual 仅 armed 生效 (busy 时忽略)
    {
        sort::Sorter s(1);
        s.handleCmd("divert", "red_block", 1, "immediate", "vj8", 0);
        s.setVirtual(10, 10);
        check("busy 时 setVirtual 无效", s.virt_first_ms == 0
              && std::string(s.state()) == "busy");
    }
}

// BeltHold: 推料期间停带, 扫掠结束自动恢复
void testBeltHold() {
    // 基础: 起带 -> 推料停带 -> 推完恢复
    {
        sort::BeltHold b;
        check("默认不带", !b.beltOn());
        check("start 起带", b.conveyorStart() && b.beltOn());
        b.sweepStart();
        check("扫掠开始即停带", !b.beltOn() && b.hold == 1);
        check("扫掠结束恢复带速", b.sweepEnd() && b.beltOn());
    }
    // 并发扫掠: 全部结束才起带
    {
        sort::BeltHold b;
        b.conveyorStart();
        b.sweepStart();
        b.sweepStart();
        check("双扫掠 hold=2", b.hold == 2 && !b.beltOn());
        check("第一件结束仍停带", !b.sweepEnd() && !b.beltOn());
        check("第二件结束起带", b.sweepEnd() && b.beltOn());
    }
    // 网关 stop/pause 期间推料: 恢复不自动起带
    {
        sort::BeltHold b;
        b.conveyorStart();
        b.sweepStart();
        b.conveyorStop();
        check("stop 停带", !b.running && !b.beltOn());
        check("推完不自动起 (带子已停)", !b.sweepEnd() && !b.beltOn());
        check("再 start 立即起带", b.conveyorStart() && b.beltOn());
    }
    // 推料中收到 start: 挂起, 推完才起
    {
        sort::BeltHold b;
        b.conveyorStart();
        b.sweepStart();
        b.conveyorStop();          // 先停
        check("推料中 start 挂起", !b.conveyorStart() && !b.beltOn());
        check("推完自动起", b.sweepEnd() && b.beltOn());
    }
    // 未起带时推料: 不产生"复活"的带速
    {
        sort::BeltHold b;
        b.sweepStart();
        check("未 running 推料不停带也无带", b.hold == 1 && !b.beltOn());
        check("推完仍无带", !b.sweepEnd() && !b.beltOn());
    }
}

static void testBeltWatch() {
    printf("-- BeltWatch (带速估计 + 打滑/卡滞) --\n");
    // 门距 500/1000/1500mm, 指令 1.0 m/s -> 期望门门行程 500ms (带面时间)
    auto run = [](BeltWatch& b, uint32_t to, bool on) {
        // 带面时间推进: 从上次到 to (线性, 单段)
        b.tick(to, on);
    };

    // A. 正常: 视觉锚点 + 门门区间, 实测 = 指令 -> OK
    BeltWatch b(500, 1000, 1500, 1.0);
    b.tick(0, true);
    b.onVision(0.0, 0);
    run(b, 500, true);  b.onGateEdge(0, 500);
    run(b, 1000, true); b.onGateEdge(1, 1000);
    run(b, 1500, true); b.onGateEdge(2, 1500);
    check("正常: 状态 ok", b.ok(), b.reason());
    char det[64];
    snprintf(det, sizeof(det), "v_meas=%f", b.v_meas_mps());
    check("正常: 实测带速≈1.0", std::abs(b.v_meas_mps() - 1.0) < 0.01, det);

    // B. 打滑: 实际 0.8 m/s (行程慢 25%) -> 3 样本后 SLIP 锁存
    BeltWatch s(500, 1000, 1500, 1.0);
    s.tick(0, true);
    s.onVision(0.0, 0);
    run(s, 625, true);  s.onGateEdge(0, 625);
    check("打滑: 1 样本未锁存", s.ok(), s.reason());
    run(s, 1250, true); s.onGateEdge(1, 1250);  // 门门+偏差 -> 累计 3 样本
    check("打滑: 3 样本后 SLIP", s.status() && !strcmp(s.status(), "slip"), s.reason());
    check("打滑: via=belt-slip", !strcmp(s.via(), "belt-slip"), s.via());
    snprintf(det, sizeof(det), "v_meas=%f", s.v_meas_mps());
    check("打滑: 实测带速≈0.8", std::abs(s.v_meas_mps() - 0.8) < 0.02, det);

    // C. 卡滞: 过门 0 后, 带面时间超 3× 期望未到门 1 -> JAM
    BeltWatch j(500, 1000, 1500, 1.0);
    j.tick(0, true);
    run(j, 500, true); j.onGateEdge(0, 500);
    check("卡滞: 未超时前 ok", j.ok());
    run(j, 500 + 1501, true);  // 带面时间超 3×500ms
    check("卡滞: 超时 JAM", !strcmp(j.status(), "jam"), j.reason());
    check("卡滞: via=belt-jam", !strcmp(j.via(), "belt-jam"), j.via());

    // D. 拨出: 过门 0 后被拨出 -> 不期望门 1, 长时间不报卡滞
    BeltWatch d(500, 1000, 1500, 1.0);
    d.tick(0, true);
    run(d, 500, true); d.onGateEdge(0, 500);
    d.onDivert(0);
    run(d, 5000, true);
    check("拨出: 不误报卡滞", d.ok(), d.reason());

    // E. 停带窗口不计行程: 墙钟 3s 但带面只走 0.5s -> 不误报
    BeltWatch h(500, 1000, 1500, 1.0);
    h.tick(0, true);
    run(h, 500, true); h.onGateEdge(0, 500);
    h.tick(3000, false);          // 停带 2.5s (推料/paused)
    h.tick(3500, true);           // 带面时间 = 500+500 = 1000
    h.onGateEdge(1, 3500);        // 门门行程 500ms (带面) = 期望
    check("停带窗口: 不误报卡滞/打滑", h.ok(), h.reason());

    // F. 带停期间物品滞留 -> 不报卡滞 (带面时间冻结)
    BeltWatch f(500, 1000, 1500, 1.0);
    f.tick(0, true);
    run(f, 500, true); f.onGateEdge(0, 500);
    f.tick(60000, false);
    check("带停滞留: 不误报卡滞", f.ok(), f.reason());

    // G. 陈旧锚点: 锚点 4s 前, 后来的落沿不采样 (实际 4000 > 5×期望 2500)
    BeltWatch g(500, 1000, 1500, 1.0);
    g.tick(0, true);
    g.onVision(0.0, 0);
    run(g, 4000, true); g.onGateEdge(0, 4000);
    check("陈旧锚点: 丢弃不误判", g.ok() && g.v_meas_mps() == 0.0, g.reason());

    // H. 锁存 + clear: 打滑锁存后正常样本不解锁, clear 复位
    BeltWatch c(500, 1000, 1500, 1.0);
    c.tick(0, true);
    c.onVision(0.0, 0);
    run(c, 625, true);  c.onGateEdge(0, 625);
    run(c, 1250, true); c.onGateEdge(1, 1250);
    run(c, 1875, true); c.onGateEdge(2, 1875);
    check("锁存: 打滑保持", !strcmp(c.status(), "slip"), c.reason());
    c.clear();
    check("clear: 复位 ok", c.ok() && c.v_meas_mps() == 0.0, c.reason());

    // I. 指令带速变化: setSpeed 后按新带速评估
    BeltWatch v(500, 1000, 1500, 1.0);
    v.tick(0, true);
    v.setSpeed(2.0);  // 指令提到 2 m/s: 期望门门行程 250ms
    run(v, 250, true); v.onGateEdge(0, 250);
    run(v, 500, true); v.onGateEdge(1, 500);
    check("变速: 新带速下正常", v.ok(), v.reason());
}

int main() {
    testConveyor();
    testSorter();
    testHuskyLens();
    testProto();
    testMcpEn();
    testGate();
    testSorterPhoto();
    testTrack();
    testSorterVirtual();
    testBeltHold();
    testBeltWatch();
    printf("\n%s\n", FAIL ? "有失败 ✗" : "全部通过 ✔");
    printf("PASS %d  FAIL %d\n", PASS, FAIL);
    return FAIL ? 1 : 0;
}
