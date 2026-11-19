// 纯逻辑: HuskyLens 2 串口协议解析 (DFRobot HUSKYLENS 协议) + 进画面去重.
// 无 Arduino 依赖. 帧格式 (对象帧 type=0x01):
//   0x55 0x55 | TYPE | ADDR | LEN_LO LEN_HI | DATA[LEN] | CHECKSUM
//   CHECKSUM = TYPE..DATA 所有字节之和 & 0xFF
//   对象帧 DATA: [0]=object_type [1,2]=ID(le16) [3,4]=X [5,6]=Y [7,8]=W [9,10]=H
// object_type (HuskyLens 系): 0=none 1=颜色块 2=线 3=标签 4=人脸 5=标识/物体
//   HuskyLens 2 的自定义模型 (YOLO/基础分类) 输出按 object_type+ID 映射物料.
#pragma once
#include <cmath>
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

// 纯逻辑: 位置式在视野去重门 (TrackGate) — I2C 轮询路径 (hlTick) 用.
// 背景 (2026-11-19): RetrigGate 纯时间门的两段教训 —
//   ① 无门控: 单件每 300ms 轮询报一次, ~5.7s 停留 ≈ 19 条;
//   ② 时间门 500ms: HL2 对薄/远工件丢跟踪间隙常 >=500ms → 同一件重报;
//   ③ 时间门 1500ms: 实测仍有 ~1.8s 同物料复现 → 纯时间阈值会被更长的
//      闪烁间隙穿透 (whack-a-mole, 不是正解).
// 正解: bbox 位置 + 带速漂移预测判"同一件 vs 新件" (多槽位, 每物料可多件):
//   - 每件占一个槽位, 记录其位置; 新观测匹配"各槽位上次位置 + 漂移方向 ×
//     速率 × 间隙"的最近预测点; 距离 <= tol → 同一件 (位置滑动更新, 抑制);
//     距离 > tol → 新件 (占新槽, 上报).
//   - 带在跑: 件随带下游漂移, 像素速率 = HL_PX_PER_MM × 当前带速 (固件已知);
//     闪烁复现位置 ≈ 预测点 → 抑制; 入射边新件远离预测 → 上报.
//   - 带停: 速率=0, 预测=原地 → 原处复现=同一件, 远处=新件.
//   - 连续在视野: 每 tick 匹配自身槽位 → 只报一次.
//   - 密集同物料 (<1.5s 两件): 第二件从入射边出现, 与第一件预测点相距远超
//     tol → 各占一槽 → 两件都上报 (修纯时间门的密集同物料欠发).
// 校准量 (config.h, 由单件过视野实测得):
//   - HL_PX_PER_MM: 像素/毫米 (带面方向, 相机标定)
//   - HL_BELT_DIR_X/Y: 带移动方向在图像中的单位向量 (像素坐标, +x 右 / +y 下)
//   - tol_px ≈ 0.75 × bbox 长边 (像素); 须 < 密集件距像素间距
// 注意: 只在 conf 通过的对象上调用. 键是物料名 (定制模型 129 ID 全 0).
class TrackGate {
 public:
    static constexpr int kMax = 16;          // 槽位总数 (最多同时 16 件在跟踪)
    static constexpr uint32_t kStaleMs = 15000;  // 槽位过期: 超时未匹配不可再用

    // 返回 true = 应上报一件; false = 同一件在跟踪, 抑制.
    //   x,y        : bbox 中心 (像素)
    //   now_ms     : 本 tick 时刻
    //   rate_px_ms : 带面漂移像素速率 (= HL_PX_PER_MM × 当前带速 m/s); 0=带停
    //   dir_x,dir_y: 漂移方向单位向量 (像素坐标); rate=0 时无效
    //   tol_px     : 匹配容差 (像素)
    //   absence_ms : 短间隙直通窗口 (< 此间隙必并回最近槽, 不查距离 —
    //                吸收亚秒级多帧抖动)
    bool tick(const char* mat, int x, int y, uint32_t now_ms,
              double rate_px_ms, double dir_x, double dir_y,
              double tol_px, uint32_t absence_ms) {
        if (!mat || !*mat) return false;
        last_ref_ = now_ms;
        int best = -1;
        double best_d = 1e18;
        for (int i = 0; i < used_; i++) {
            if (mats_[i] != mat) continue;
            if (now_ms - last_[i] > kStaleMs) continue;   // 过期槽不匹配
            double d = distToPredicted(i, x, y, rate_px_ms, dir_x, dir_y);
            if (d < best_d) { best_d = d; best = i; }
        }
        bool same = false;
        uint32_t gap = (best >= 0) ? (now_ms - last_[best]) : 0xFFFFFFFF;
        if (best >= 0) {
            // 短间隙用宽松窗 (2×tol, 吸收抖动/多帧), 长间隙用严格窗 (tol):
            // 不能对短间隙无条件并回 — 密集第二件的"间隙"是相对第一件槽位
            // 上次 tick 的, 可能 < absence, 但位置远离预测 → 必须靠距离判.
            double tol_eff = (gap < absence_ms) ? (tol_px * 2.0) : tol_px;
            same = (best_d <= tol_eff);
        }
        if (same) {
            px_[best] = x; py_[best] = y; last_[best] = now_ms;  // 位置滑动
            return false;
        }
        // 新件: 找空槽
        int slot = -1;
        for (int i = 0; i < kMax; i++)
            if (i >= used_ || now_ms - last_[i] > kStaleMs) { slot = i; break; }
        if (slot < 0) return true;                        // 满: 放行不丢事件
        if (slot >= used_) used_ = slot + 1;
        mats_[slot] = mat;
        px_[slot] = x; py_[slot] = y; last_[slot] = now_ms;
        return true;
    }

    void reset() {
        used_ = 0;
        for (int i = 0; i < kMax; i++) { mats_[i].clear(); last_[i] = 0; }
    }

 private:
    // 当前位置到槽 i 的 "上次位置 + 漂移方向×速率×(now-last_i)" 预测点距离.
    double distToPredicted(int i, int x, int y,
                           double rate_px_ms, double dir_x, double dir_y) const {
        double gap_ms = (double)(last_ref_ - last_[i]);
        double pred_x = px_[i] + dir_x * rate_px_ms * gap_ms;
        double pred_y = py_[i] + dir_y * rate_px_ms * gap_ms;
        double dx = (double)x - pred_x, dy = (double)y - pred_y;
        return std::sqrt(dx * dx + dy * dy);
    }

    int used_ = 0;
    uint32_t last_ref_ = 0;   // tick 入口设置的 now_ms (供 distToPredicted 用)
    std::string mats_[kMax];
    uint32_t last_[kMax] = {0};
    int px_[kMax] = {0}, py_[kMax] = {0};
};

}  // namespace sort
