// 纯逻辑: 带面物品位置跟踪 (虚拟光门). 无 Arduino 依赖.
//
// 没有物理光门时, 用"相机检测锚点 + 带速匀速外推"预测物品过推杆光门线的
// 时刻, 作为推出时序 (与 gate_layout.svg 的时序一致):
//   p(t) = p0_mm + v_mps × (t_ms − t0_ms) / 1000
//   p = 沿带面距相机参考平面的距离 (mm, 正向 = 下游, 即光门/推杆方向)
// 解析顺序 (固件 main.cpp): 物理光门 -> 虚拟光门 -> immediate 回退.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>

namespace sort {

class Track {
 public:
  static constexpr int kMaxAnchors = 8;
  struct Anchor {
    bool valid = false;
    char material[24] = {0};
    double p0_mm = 0.0;   // 检测时刻物品位置 (mm, 相机参考系, + 下游)
    uint32_t t0_ms = 0;   // 检测时刻 (ms)
  };
  Anchor anchors[kMaxAnchors];

  // 像素横坐标 -> 带面 mm (相对相机参考平面):
  //   p0 = (px − cx_px) / view_px × view_mm, 限幅 ±view_mm/2 (视野半宽).
  static double pixelToMm(uint16_t px, double cx_px, double view_px,
                          double view_mm) {
    if (view_px <= 0.0) return 0.0;
    double p = (double(px) - cx_px) / view_px * view_mm;
    double lim = view_mm / 2.0;
    if (p > lim) p = lim;
    if (p < -lim) p = -lim;
    return p;
  }

  // 记录一次检测锚点 (同物料覆盖, 只保留最新一次; 不同物料各占一格).
  void onVision(const char* material, double p0_mm, uint32_t t0_ms) {
    if (!material || !*material) return;
    int slot = -1;
    for (int i = 0; i < kMaxAnchors; i++)
      if (anchors[i].valid && !strcmp(anchors[i].material, material)) {
        slot = i;
        break;
      }
    if (slot < 0) {
      for (int i = 0; i < kMaxAnchors; i++)
        if (!anchors[i].valid) { slot = i; break; }
      if (slot < 0) return;   // 满且无同物料: 丢弃 (物料数远小于 8)
    }
    anchors[slot].valid = true;
    strncpy(anchors[slot].material, material, sizeof(anchors[slot].material) - 1);
    anchors[slot].material[sizeof(anchors[slot].material) - 1] = 0;
    anchors[slot].p0_mm = p0_mm;
    anchors[slot].t0_ms = t0_ms;
  }

  // 匹配 now_ms 前 window_ms 内同物料的最新检测.
  // 返回 true 且 (可选) 输出 p0_mm/t0_ms.
  bool match(const char* material, uint32_t now_ms, uint32_t window_ms,
             double* p0_mm = nullptr, uint32_t* t0_ms = nullptr) const {
    if (!material || !*material) return false;
    for (int i = 0; i < kMaxAnchors; i++) {
      const Anchor& a = anchors[i];
      if (a.valid && !strcmp(a.material, material) &&
          a.t0_ms <= now_ms && now_ms - a.t0_ms <= window_ms) {
        if (p0_mm) *p0_mm = a.p0_mm;
        if (t0_ms) *t0_ms = a.t0_ms;
        return true;
      }
    }
    return false;
  }

  // 预测物品过"相机下游 gate_mm 处光门线"的时刻 (ms, 绝对时间):
  //   p(t_g) = gate_mm  =>  t_g = t0 + (gate_mm − p0_mm) / (v_mps/1000)
  // v_mps <= 0 (带停) 或物品已在/已过光门线 => 返回 0 (调用方按立即执行处理).
  static uint32_t predictGateMs(uint32_t t0_ms, double p0_mm, double gate_mm,
                                double v_mps) {
    if (v_mps <= 0.0) return 0;
    // m/s 与 mm/ms 数值相等: 1 m/s = 1000mm/1000ms = 1 mm/ms
    double dt_ms = (gate_mm - p0_mm) / v_mps;    // mm / (mm/ms)
    if (dt_ms < 0.0) return 0;
    return t0_ms + (uint32_t)(dt_ms + 0.5);
  }

  void clear() {
    for (int i = 0; i < kMaxAnchors; i++) anchors[i].valid = false;
  }
};

}  // namespace sort
