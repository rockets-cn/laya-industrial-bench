#!/usr/bin/env bash
# 开机自启: 等 Laya 回路 Web UI (systemd 用户服务 laya-loop-ui, :8002) 就绪后开浏览器.
# 由 ~/.config/autostart/laya-loop-ui-browser.desktop 在会话启动时执行.
set -u
URL="http://127.0.0.1:8002/"

# 服务由 systemd 拉起, 首启含 ASR 预热, 最多等 60 s
for _ in $(seq 1 60); do
  if curl -sf -m 2 "$URL" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 服务没起来就不开死页面 (服务侧有 Restart=on-failure, 之后可手动重开)
if ! curl -sf -m 2 "$URL" >/dev/null 2>&1; then
  echo "loop_ui not reachable at $URL, not opening browser" >&2
  exit 1
fi

# 脱离 autostart 会话独立运行, 不阻塞桌面启动
setsid chromium --new-window "$URL" >/dev/null 2>&1 &
exit 0
