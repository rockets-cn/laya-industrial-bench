"""本地 MQTT broker (amqtt, 纯 Python). 生产环境建议换 mosquitto (需 root 安装),
topic 约定完全一致, 只需改 SORT_MQTT_BROKER.

Run: .venv/bin/python broker_local.py   # 0.0.0.0:1883 (物理链路边际: EDGE 101 经网线接入)
     HOST=127.0.0.1 PORT=1883 .venv/bin/python broker_local.py   # 仅本机 (纯模拟调试)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import amqtt.broker

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "1883"))
    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}}}

    async def boot():
        broker = amqtt.broker.Broker(cfg)
        await broker.start()
        print(f"[broker] MQTT broker 监听 {host}:{port} (amqtt)", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(boot())
    except KeyboardInterrupt:
        print("[broker] 停止", flush=True)


if __name__ == "__main__":
    main()
