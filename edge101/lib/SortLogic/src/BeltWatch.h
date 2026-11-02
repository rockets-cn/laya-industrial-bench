// 纯逻辑: 传送带健康监测 (带速估计 + 打滑/卡滞检测). 无 Arduino 依赖.
//
// 只有固件端有原始物理时序 (光门落沿 + 停带窗口 + 视觉锚点), 所以监测核心在
// 固件; 网关/页面只透传 (conveyor 遥测 note + motion fault 事件) 与回放.
//
// 带面时间 belt_ms: 只在带子实际在转时累加 (排除 BeltHold 推料停带 / paused /
// stopped 窗口) -> "行程"一律换算到带面自己的钟: 推料停带不计行程, 不会误报卡滞.
//
// 带速估计 (ratio = 实际/期望, >1 = 物品比预期慢 = 实际带速低于指令带速):
//   - 视觉锚点 (p0, t0_belt): 物品预期过门 g 的带面时刻 = t0 + (dist[g]-p0)/v_cmd
//     实际过门时刻 - 预期 = 偏差 (一个锚点可预测该物品所有门);
//   - 门门区间: 同件物品过门 g 再过门 g+1: v_meas = (dist[g+1]-dist[g]) / Δbelt_time.
// 判定:
//   - SLIP: 最近 slip_window 个样本滑动均值 >= slip_ratio (默认 1.25 = 实际慢 20%+)
//           -> 皮带打滑; ratio < 0.5 的样本视为锚点/落沿错配, 直接丢弃 (防误报);
//   - JAM:  物品过门 g (g<2) 后, 带面时间超过 jam_factor × 期望 (默认 3×) 仍未
//           到门 g+1 -> 卡滞; 期间若 onDivert(g) (该件在门 g 被拨出) 则不期望门 g+1.
// 锁存: SLIP/JAM 一旦触发保持, 直到 clear() (人工 reset 后调用).
// 约束: 需要物理光门; 虚拟光门模式无物理沿, 不喂样本 (避免模型自证恒真).
#pragma once
#include <cstdint>
#include <cstdio>

namespace sort {

class BeltWatch {
 public:
  static constexpr double kEpsMs = 50.0;  // 带面时间 < 50ms 的样本忽略 (去抖/抖动)

  BeltWatch(double dist0_mm, double dist1_mm, double dist2_mm,
            double v_cmd_mps, double slip_ratio = 1.25, double jam_factor = 3.0,
            int slip_window = 3)
      : d0_(dist0_mm), d1_(dist1_mm), d2_(dist2_mm), v_cmd_(v_cmd_mps),
        slip_ratio_(slip_ratio), jam_factor_(jam_factor), slip_window_(slip_window) {
    clear();
  }

  // 指令带速变化 (conveyor start/resume 带不同 speed).
  void setSpeed(double v_mps) {
    if (v_mps > 0.0) v_cmd_ = v_mps;
  }

  // 主循环每拍调用: 累加带面时间 + 评估卡滞.
  void tick(uint32_t now_ms, bool belt_on) {
    int32_t d = (int32_t)(now_ms - last_now_);
    last_now_ = now_ms;
    if (belt_on && d > 0 && d < 5000) belt_ms_ += (double)d;  // 回绕/长空保护
    if (state_ != kOk || v_cmd_ <= 0.0) return;
    int g = last_edge_gate_;
    if (g >= 0 && g < 2 && !diverted_[g] && edge_belt_[g] >= 0.0) {
      double expected_ms = (dist_mm(g + 1) - dist_mm(g)) / mm_per_ms();
      if (belt_ms_ - edge_belt_[g] > jam_factor_ * expected_ms) state_ = kJam;
    }
  }

  // 物理光门落沿 (g=0..2). 只喂物理沿 (gateOk 时), 虚拟沿不入.
  void onGateEdge(int g, uint32_t now_ms) {
    (void)now_ms;
    if (g < 0 || g > 2 || v_cmd_ <= 0.0) return;
    double nowb = belt_ms_;
    // 该门来了新物品: 上一件在此门的"拨出标记"作废, 门 g+1 期望重新打开.
    diverted_[g] = false;
    // 门门区间样本: 上一门同件物品 (未拨出), 间隔换算带面时间.
    if (g > 0 && !diverted_[g - 1] && edge_belt_[g - 1] >= 0.0) {
      double delta = nowb - edge_belt_[g - 1];
      double expected = (dist_mm(g) - dist_mm(g - 1)) / mm_per_ms();
      if (delta > kEpsMs && expected > 0.0)
        recordSample(delta / expected, dist_mm(g) - dist_mm(g - 1), delta);
    }
    // 视觉锚点偏差样本: 同一锚点预测该物品所有门 (防陈旧锚点: 实际远超预期 5× 视为
    // 不是这件, 不采样).
    if (anchor_valid_ && !anchor_done_) {
      double ds = dist_mm(g) - p0_;
      if (ds > 0.0) {
        double expected = ds / mm_per_ms();
        double actual = nowb - anchor_belt_;
        if (expected > kEpsMs && actual > kEpsMs && actual <= 5.0 * expected)
          recordSample(actual / expected, ds, actual);
      }
    }
    edge_belt_[g] = nowb;
    last_edge_gate_ = g;
    if (g == 2) anchor_done_ = true;  // 过最后一门: 该件锚点消费完毕
  }

  // 视觉锚点 (HuskyLens 检测): 物品在带面 p0_mm 处, 带面时刻 = 当前 belt_ms.
  void onVision(double p0_mm, uint32_t now_ms) {
    (void)now_ms;
    p0_ = p0_mm;
    anchor_belt_ = belt_ms_;
    anchor_valid_ = true;
    anchor_done_ = false;
  }

  // 该件物品在门 g 被拨出 (推杆扫掠开始): 不再期望它过门 g+1.
  void onDivert(int g) {
    if (g < 0 || g > 2) return;
    if (g < 2) diverted_[g] = true;
    anchor_valid_ = false;  // 该件已离带 (或被拨入料道), 锚点作废
  }

  // 人工 reset 后调用: 清锁存与样本 (带面时间保持连续, 不清).
  void clear() {
    state_ = kOk;
    n_ = 0;
    edge_belt_[0] = edge_belt_[1] = edge_belt_[2] = -1.0;
    last_edge_gate_ = -1;
    diverted_[0] = diverted_[1] = diverted_[2] = false;
    anchor_valid_ = false;
    anchor_done_ = false;
    v_meas_ = 0.0;
  }

  bool ok() const { return state_ == kOk; }
  const char* status() const {
    return state_ == kJam ? "jam" : state_ == kSlip ? "slip" : "ok";
  }
  const char* via() const {
    return state_ == kJam ? "belt-jam" : "belt-slip";
  }
  const char* reason() const {
    static char buf[96];
    if (state_ == kJam)
      snprintf(buf, sizeof(buf), "belt jam: item stalled between gates (belt-time exceeded)");
    else if (state_ == kSlip)
      snprintf(buf, sizeof(buf), "belt slip: measured %.2f vs commanded %.2f m/s",
               v_meas_, v_cmd_);
    else
      snprintf(buf, sizeof(buf), "belt ok");
    return buf;
  }
  double v_meas_mps() const { return v_meas_; }

 private:
  enum State { kOk, kSlip, kJam };

  double dist_mm(int g) const {
    return g == 0 ? d0_ : g == 1 ? d1_ : d2_;
  }
  double mm_per_ms() const { return v_cmd_; }  // 1 m/s = 1 mm/ms

  void recordSample(double ratio, double dist_mm_span, double delta_ms) {
    if (!(ratio >= 0.5) || ratio > 1e6) return;  // 过快样本 = 锚点/落沿错配, 丢弃
    ring_[n_ % slip_window_] = ratio;
    n_++;
    int fill = n_ < slip_window_ ? n_ : slip_window_;
    v_meas_ = dist_mm_span / delta_ms;  // mm/ms 数值 = m/s 数值
    if (state_ != kOk) return;  // 已锁存: 不再新评估
    double sum = 0.0;
    for (int i = 0; i < fill; i++) sum += ring_[i % slip_window_];
    if (fill >= slip_window_ && sum / fill >= slip_ratio_) state_ = kSlip;
  }

  State state_ = kOk;
  double d0_, d1_, d2_;
  double v_cmd_;
  double slip_ratio_;
  double jam_factor_;
  int slip_window_;

  uint32_t last_now_ = 0;
  double belt_ms_ = 0.0;  // 带面时间 (只在带子转时累加)

  double edge_belt_[3];  // 各门最近一次落沿的带面时刻 (-1 = 无)
  int last_edge_gate_ = -1;
  bool diverted_[3];     // 最近一件在门 g 被拨出 -> 不期望门 g+1

  bool anchor_valid_ = false;
  bool anchor_done_ = false;
  double p0_ = 0.0;
  double anchor_belt_ = 0.0;

  double ring_[4];  // slip_window 最大 4 (默认 3, 留 1 余量); 超过则取模复用
  int n_ = 0;
  double v_meas_ = 0.0;
};

}  // namespace sort
