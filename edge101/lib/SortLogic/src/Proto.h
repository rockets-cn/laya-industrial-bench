// 纯逻辑: sort-mqtt 协议 payload/JSON 构造 (固件侧). 无 Arduino 依赖.
// 协议详见 docs/sort-mqtt.md.
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>

namespace sort {

inline std::string tsIso() {
    // 物理控制器无 RTC: 按契约 (docs/sort-mqtt.md 遥测节) 占位空字符串,
    // 网关忽略 ts. 后续加 NTP 后在此返回网络时间.
    return "";
}

inline std::string jsonStr(const char* s) {
    std::string out = "\"";
    for (const char* p = s; p && *p; p++) {
        char c = *p;
        if (c == '"' || c == '\\') out += '\\';
        if (c < 0x20) { char b[4]; snprintf(b, 4, "\\u%04x", c); out += b; }
        else out += c;
    }
    return out + "\"";
}

// 应答: {job_id, action, result, note, ts}
inline std::string ackJson(const char* job_id, const char* action,
                           const char* result, const char* note,
                           const char* ts = "") {
    return "{\"job_id\":" + jsonStr(job_id ? job_id : "") +
           ",\"action\":" + jsonStr(action ? action : "") +
           ",\"result\":" + jsonStr(result) +
           ",\"note\":" + jsonStr(note ? note : "") +
           ",\"ts\":" + jsonStr(ts) + "}";
}

// 传送带遥测: {state, speed_mps, steps_total, ts}
inline std::string conveyorTele(const char* state, double speed,
                                uint64_t steps, const char* ts = "") {
    char speed_s[32];
    snprintf(speed_s, sizeof(speed_s), "%.2f", speed);
    return "{\"state\":" + jsonStr(state) + ",\"speed_mps\":" + speed_s +
           ",\"steps_total\":" + std::to_string(steps) +
           ",\"ts\":" + jsonStr(ts) + "}";
}

// 分拣器遥测: {state, diverted_total, last_material, ts}
inline std::string sorterTele(const char* state, uint32_t diverted,
                              const char* mat, const char* ts = "") {
    return "{\"state\":" + jsonStr(state) +
           ",\"diverted_total\":" + std::to_string(diverted) +
           ",\"last_material\":" + jsonStr(mat ? mat : "") +
           ",\"ts\":" + jsonStr(ts) + "}";
}

// 视觉事件: {material, confidence, frame_id, bbox, pos_mm, source, ts}
// pos_mm >= 0 时输出: 物品在 t0 时刻的带面位置 (mm, 相机参考系, + 下游),
// 由 Track::pixelToMm 从像素换算 (上游标定/对账 + 动画轨迹用).
inline std::string visionJson(const char* material, double conf,
                              const char* frame_id, int x, int y, int w, int h,
                              double pos_mm = -1.0, const char* ts = "") {
    char conf_s[32];
    snprintf(conf_s, sizeof(conf_s), "%.3f", conf);
    std::string out = "{\"material\":" + jsonStr(material) +
           ",\"confidence\":" + conf_s +
           ",\"frame_id\":" + jsonStr(frame_id ? frame_id : "") +
           ",\"bbox\":[" + std::to_string(x) + "," + std::to_string(y) +
           "," + std::to_string(w) + "," + std::to_string(h) + "]";
    if (pos_mm >= 0.0) {
        char pos_s[32];
        snprintf(pos_s, sizeof(pos_s), "%.1f", pos_mm);
        out += ",\"pos_mm\":" + std::string(pos_s);
    }
    return out + ",\"source\":" + jsonStr("huskylens2") +
               ",\"ts\":" + jsonStr(ts) + "}";
}

// 分拣器运动事件: 状态迁移时间线 (动画/台账还原"实际情况"用).
// via = 迁移原因: immediate (收令即拨) / photo (物理光门沿) / virtual (虚拟光门
// 预测时刻到) / timeout (光门等待超时 fault) / reset (人工复位) / done (job 完成)
// / next (photo job 多件: 拨完一件回 armed 等下一件) / cancel (armed 被 reset 取消).
// 逐件件号 (qty>1 时): item = 本条事件关联的第几件 (1-indexed), of = 总件数.
// 关联件口径: busy/timeout = 正在处理(未到场)的那件; armed/next 与 done =
// 刚完成(刚拨出)的那件; of=0 = 不适用 (reset/cancel 或旧固件).
inline std::string motionJson(int sorter, const char* state, const char* via,
                              const char* job_id, const char* ts = "",
                              int item = 0, int of = 0) {
    std::string out = "{\"sorter\":" + std::to_string(sorter) +
           ",\"state\":" + jsonStr(state ? state : "") +
           ",\"via\":" + jsonStr(via ? via : "") +
           ",\"job_id\":" + jsonStr(job_id ? job_id : "") +
           ",\"ts\":" + jsonStr(ts);
    if (of > 0 && item > 0)
        out += ",\"item\":" + std::to_string(item) +
               ",\"of\":" + std::to_string(of);
    return out + "}";
}

// topic
inline std::string conveyorCmdTopic() { return "sort/line1/conveyor/cmd"; }
inline std::string conveyorAckTopic(const char* job_id) {
    return "sort/line1/conveyor/ack/" + std::string(job_id);
}
inline std::string conveyorTeleTopic() { return "sort/line1/conveyor/telemetry"; }
inline std::string sorterCmdTopic(int n) { return "sort/sorter" + std::to_string(n) + "/cmd"; }
inline std::string sorterAckTopic(int n, const char* job_id) {
    return "sort/sorter" + std::to_string(n) + "/ack/" + std::string(job_id);
}
inline std::string sorterTeleTopic(int n) {
    return "sort/sorter" + std::to_string(n) + "/telemetry";
}
inline std::string visionTopic() { return "sort/inbound/vision"; }
inline std::string motionTopic() { return "sort/line1/motion"; }

}  // namespace sort
