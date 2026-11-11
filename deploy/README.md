# deploy/ — 开机自启 + 故障自愈 (user systemd)

分拣栈 4 个 PC 侧服务配成 **user systemd 服务**（跑在 `rockets` 用户, 无需 root）,
全部 `Restart=always` → 崩溃即自动拉起。ESP32 (edge101) 是物理设备, 自己上电自启, 不在此列。

## 服务与端口

| 服务文件 | 进程 | 端口 | 依赖 |
|---|---|---|---|
| `laya-mqtt-broker.service` | `broker_local.py` (amqtt) | 0.0.0.0:1883 | network-online |
| `laya-sort-gateway.service` | `sort_mqtt.py` | 127.0.0.1:8123 | broker 先起 |
| `laya-mjpeg.service` | `mjpeg_live.py` (C110 /dev/video0) | 0.0.0.0:8125 | network-online |
| `laya-loop-ui.service` | `loop_ui.py` (孪生网页) | 127.0.0.1:8002 | 网关 (透传 /twin) |

启动顺序: broker → 网关 → 孪生 (After=/Wants= 声明); 网关自带 MQTT 重连, broker 重启后自动回连。

## 安装 (一次性)

```bash
cd /home/rockets/laya-industrial-bench
cp deploy/laya-mqtt-broker.service deploy/laya-sort-gateway.service \
   deploy/laya-mjpeg.service deploy/laya-loop-ui.service ~/.config/systemd/user/
systemctl --user daemon-reload
# 起全部 + 设开机自启 (WantedBy=default.target)
for s in laya-mqtt-broker laya-sort-gateway laya-mjpeg laya-loop-ui; do
  systemctl --user enable --now "$s.service"
done
```

## 关键: 开 linger (否则开机不启动 / 注销即停)

user 服务默认只在"已登录会话"里活。要**开机自启 + 桌面崩溃/注销后仍存活**, 必须开 linger (需 sudo):

```bash
sudo loginctl enable-linger rockets
loginctl show-user rockets | grep -i linger   # 应为 Linger=yes
```

## 日常运维

```bash
systemctl --user status laya-sort-gateway        # 看状态
journalctl --user -u laya-mqtt-broker -f         # 实时日志
systemctl --user restart laya-mjpeg             # 重启单个
systemctl --user is-active laya-mqtt-broker laya-sort-gateway laya-mjpeg laya-loop-ui
```

## 故障自愈

- 各服务 `Restart=always` + `RestartSec=2~3`: 进程崩溃/被杀 → systemd 自动重拉。
- 已实测: `kill -9` 网关 → systemd 3s 内自动重启 + 重连 broker。
- 端口被占导致反复起不来时, systemd 会按重启计数持续尝试; 用 `journalctl --user -u <svc>` 查因。

## 端口速查
`1883`=broker · `8123`=网关 · `8125`=MJPEG 相机流 · `8002`=孪生网页
