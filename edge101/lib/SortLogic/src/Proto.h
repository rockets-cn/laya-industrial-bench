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

// 视觉事件: {material, confidence, frame_id, bbox, source, ts}
inline std::string visionJson(const char* material, double conf,
                              const char* frame_id, int x, int y, int w, int h,
                              const char* ts = "") {
    char conf_s[32];
    snprintf(conf_s, sizeof(conf_s), "%.3f", conf);
    return "{\"material\":" + jsonStr(material) +
           ",\"confidence\":" + conf_s +
           ",\"frame_id\":" + jsonStr(frame_id ? frame_id : "") +
           ",\"bbox\":[" + std::to_string(x) + "," + std::to_string(y) +
           "," + std::to_string(w) + "," + std::to_string(h) + "]" +
           ",\"source\":" + jsonStr("huskylens2") +
           ",\"ts\":" + jsonStr(ts) + "}";
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

}  // namespace sort
