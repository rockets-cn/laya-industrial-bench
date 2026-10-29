// 主机单测: SortLogic (Conveyor/Sorter/HuskyLens/Proto)
// 与 sim_devices 语义对拍. 编译运行: ./run_tests.sh
#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include "Conveyor.h"
#include "Sorter.h"
#include "HuskyLens.h"
#include "Proto.h"

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
    check("running 时 start -> busy", r.handled && !r.ok && r.note == "state=running");
    r = c.handleCmd("pause", 0);
    check("running->pause ok", r.ok && c.st == DevState::kPaused);
    r = c.handleCmd("start", 2.0);
    check("paused 时 start -> busy (需 resume)", !r.ok && r.note == "state=paused");
    r = c.handleCmd("resume", 2.0);
    check("paused->resume ok", r.ok && c.st == DevState::kRunning && c.speed_mps == 2.0);
    r = c.handleCmd("stop", 0);
    check("running->stop ok", r.ok && c.st == DevState::kStopped);
    r = c.handleCmd("teleport", 0);
    check("未知 action 不处理", !r.handled);
    c.setFault();
    r = c.handleCmd("reset", 0);
    check("fault->reset ok", r.ok && c.st == DevState::kStopped);
}

static void testSorter() {
    printf("\n== Sorter 状态机 (对拍 sim_devices) ==\n");
    Sorter s(2);
    check("初始 ready", s.state() == std::string("ready"));
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
                          && vis.find("\"source\":\"huskylens2\"") != std::string::npos);

    check("conveyor cmd topic", conveyorCmdTopic() == "sort/line1/conveyor/cmd");
    check("conveyor ack topic",
          conveyorAckTopic("j9") == "sort/line1/conveyor/ack/j9");
    check("sorter3 cmd topic", sorterCmdTopic(3) == "sort/sorter3/cmd");
    check("sorter2 ack topic", sorterAckTopic(2, "jX") == "sort/sorter2/ack/jX");
    check("sorter3 tele topic", sorterTeleTopic(3) == "sort/sorter3/telemetry");
    check("vision topic", visionTopic() == "sort/inbound/vision");
}

int main() {
    testConveyor();
    testSorter();
    testHuskyLens();
    testProto();
    printf("\n%s\n", FAIL ? "有失败 ✗" : "全部通过 ✔");
    printf("PASS %d  FAIL %d\n", PASS, FAIL);
    return FAIL ? 1 : 0;
}
