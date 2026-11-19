// 纯逻辑: HuskyLens 2 串口协议解析 (DFRobot HUSKYLENS 协议) + 进画面去重.
// 无 Arduino 依赖. 帧格式 (对象帧 type=0x01):
//   0x55 0x55 | TYPE | ADDR | LEN_LO LEN_HI | DATA[LEN] | CHECKSUM
//   CHECKSUM = TYPE..DATA 所有字节之和 & 0xFF
//   对象帧 DATA: [0]=object_type [1,2]=ID(le16) [3,4]=X [5,6]=Y [7,8]=W [9,10]=H
// object_type (HuskyLens 系): 0=none 1=颜色块 2=线 3=标签 4=人脸 5=标识/物体
//   HuskyLens 2 的自定义模型 (YOLO/基础分类) 输出按 object_type+ID 映射物料.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>

namespace sort {

// 物料映射条目: {HuskyLens 对象类型, 对象/颜色块 ID, 网关白名单物料名}
struct MatEntry {
    uint8_t obj_type;
    uint16_t id;
    const char* material;
};

struct VisionEvent {
    std::string material;
    uint16_t x = 0, y = 0, w = 0, h = 0;
    uint32_t seq = 0;
    std::string toTopic() const { return "sort/inbound/vision"; }
};

class HuskyLens {
 public:
    static constexpr int kMaxData = 128;
    static constexpr int kFrame = 6 + kMaxData + 1;  // 2头+TYPE+ADDR+2LEN+DATA+SUM

    HuskyLens(const MatEntry* map, int map_len, uint32_t retrigger_ms = 500)
        : map_(map), map_len_(map_len), retrigger_ms_(retrigger_ms) {}

    // 每读到一个字节调用一次; 若产生视觉事件, 通过 fn 回调 (携带 now_ms 时刻).
    using EventFn = void (*)(const VisionEvent& ev, uint32_t now_ms, void* ud);

    void feedByte(uint8_t b, uint32_t now_ms, EventFn fn, void* ud) {
        if (idx_ < 2) {                                   // 帧头: 必须是 0x55 0x55
            if (b != 0x55) { idx_ = 0; return; }
            buf_[idx_++] = b;
            return;
        }
        buf_[idx_++] = b;
        if (idx_ < 6) return;                                // 等 LEN 收齐
        if (idx_ == 6) {                                   // TYPE ADDR LEN(2B) 到齐
            int len = buf_[4] | (buf_[5] << 8);
            if (len > kMaxData) { idx_ = 0; return; }
            pending_total_ = 6 + len + 1;
        }
        if (idx_ < pending_total_) return;
        uint8_t sum = 0;
        for (int i = 2; i < pending_total_ - 1; i++)
            sum = (uint8_t)(sum + buf_[i]);
        idx_ = 0;
        if (sum != buf_[pending_total_ - 1]) return;      // 校验和错, 丢弃
        if (buf_[2] != 0x01) return;                       // 只处理对象帧
        int d = 6;
        uint8_t obj_type = buf_[d++];
        uint16_t id = buf_[d] | (buf_[d + 1] << 8); d += 2;
        uint16_t x = buf_[d] | (buf_[d + 1] << 8); d += 2;
        uint16_t y = buf_[d] | (buf_[d + 1] << 8); d += 2;
        uint16_t w = buf_[d] | (buf_[d + 1] << 8); d += 2;
        uint16_t h = buf_[d] | (buf_[d + 1] << 8);
        onObject(obj_type, id, x, y, w, h, now_ms, fn, ud);
    }

    // 供单测直接调用 (不走字节流).
    void onObject(uint8_t obj_type, uint16_t id, uint16_t x, uint16_t y,
                  uint16_t w, uint16_t h, uint32_t now_ms, EventFn fn, void* ud) {
        const char* mat = nullptr;
        for (int i = 0; i < map_len_; i++)
            if (map_[i].obj_type == obj_type && map_[i].id == id)
                mat = map_[i].material;
        if (!mat) return;
        int slot = findSlot(mat);
        if (slot < 0) {           // 新物料 -> "进入画面" 立即上报
            if (seen_ >= kMaxSeen) return;
            slot = seen_++;
            seen_mat_[slot] = mat;
            seen_last_[slot] = now_ms;
        } else if (now_ms - seen_last_[slot] >= retrigger_ms_) {
            // 消失超过窗口后再次出现 -> 视为新的一件, 上报
        } else {
            seen_last_[slot] = now_ms;   // 仍在画面中: 滑动窗口, 不上报
            return;
        }
        seen_last_[slot] = now_ms;
        seq_++;
        VisionEvent ev;
        ev.material = seen_mat_[slot];
        ev.x = x; ev.y = y; ev.w = w; ev.h = h; ev.seq = seq_;
        fn(ev, now_ms, ud);
    }

    void reset() { seen_ = 0; seq_ = 0; idx_ = 0; }
    uint32_t seq() const { return seq_; }

 private:
    static constexpr int kMaxSeen = 8;
    const MatEntry* map_;
    int map_len_;
    uint32_t retrigger_ms_;
    uint8_t buf_[kFrame] = {0};
    int idx_ = 0;
    int pending_total_ = 0;
    int seen_ = 0;
    std::string seen_mat_[kMaxSeen];
    uint32_t seen_last_[kMaxSeen] = {0};
    uint32_t seq_ = 0;

    int findSlot(const char* mat) const {
        for (int i = 0; i < seen_; i++)
            if (seen_mat_[i] == mat) return i;
        return -1;
    }
};

// 纯逻辑: 在视野去重门 (按任意字符串键, 如物料名).
// I2C 轮询路径 (main.cpp hlTick) 用: HL2 对同一件每轮询(300ms)都报一次,
// 单件视野停留 ~5.7s -> 一件被"识别" ~19 次 (2026-11-19 用户反馈"看到一个
// 产生好多个"). 本门: 某物料首次见到 (或间隔 >= retrigger_ms 后再次出现,
// 即上一件已出视野/新件入) 返回 true (应上报一次); 窗口内的连续观测返回
// false (同一件持续在视野, 不再重复上报).
// 键用物料名而非对象 ID: 定制模型 129 的 ID 全 0, ID 无法区分件.
// 取舍: 两件同物料同时在视野内会被合并成一件 (密集同物料欠发, 见
// OPEN_QUESTIONS; 彻底方案 = 位置式下发).
// 注意: 只在实际上报的对象上调用 (conf 被丢弃的不更新计时).
class RetrigGate {
 public:
    static constexpr int kMax = 8;

    // 在视野去重 (I2C 轮询路径):
    //   - 每物料连续在视野 (轮询期每 tick 更新 last_) → 只在首见上报一次,
    //     之后间隔永远停在轮询周期 (300ms < absence_ms), 不重报;
    //   - 出视野后复现: 间隔(=缺席时长) >= absence_ms → 视为新件上报;
    //     < absence_ms (HL2 短暂丢跟踪/闪烁) → 并回同一件, 不重报。
    // 2026-11-19: 原阈值 500ms 太小 — HL2 对薄/远工件丢跟踪间隙常 >=500ms,
    // 同一件在 ~5.7s 视野停留内被重报多次 (用户报"还是出现重复多余的传感器")。
    // 抬到 1500ms: 吸收闪烁间隙; 真实新件间隔 >=3.5s (带速 52.5mm/s + 人工放料)
    // 仍可区分。密集同件 <1.5s 会合并 — 已知取舍 (OPEN_QUESTIONS §F, 用户选 A)。
    bool tick(const char* mat, uint32_t now_ms, uint32_t absence_ms) {
        if (!mat || !*mat) return false;
        int i = findSlot(mat);
        bool fresh;
        if (i < 0) {
            if (used_ >= kMax) return true;      // 表满: 放行 (退回旧行为, 不丢事件)
            i = used_++;
            mats_[i] = mat;
            fresh = true;
        } else {
            // last_ 每次轮询都更新 → 连续在视野只报一次; 复现时 now-last_ = 缺席时长
            fresh = (now_ms - last_[i] >= absence_ms);
        }
        last_[i] = now_ms;
        return fresh;
    }

    void reset() {
        used_ = 0;
        for (int i = 0; i < kMax; i++) { mats_[i].clear(); last_[i] = 0; }
    }

 private:
    int findSlot(const char* mat) const {
        for (int i = 0; i < used_; i++)
            if (mats_[i] == mat) return i;
        return -1;
    }

    int used_ = 0;
    std::string mats_[kMax];
    uint32_t last_[kMax] = {0};
};

}  // namespace sort
