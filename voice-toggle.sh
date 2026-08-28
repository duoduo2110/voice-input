#!/usr/bin/env bash
# voice-toggle.sh — 宿主机触发脚本 (F9 快捷键 / 桌面图标 / trigger-server 调用)
# ----------------------------------------------------------------------------
# 方案 B 容器: whisper-voice-all-in-one (零 PulseAudio / 零 virelual-sink)。
# 职责: 确保容器运行 -> 触发容器内 All-in-One 守护进程「开始录音 / 停止转录」。
# 触发通道(三重保障, 按优先级):
#   1) 快速直发: pid: host 下容器 PID 即宿主 PID, 直接 kill -USR1 (零 exec 开销)
#   2) HTTP 控制: curl http://127.0.0.1:8766/toggle (帧速, 无需 docker CLI)
#   3) 容器内信号: docker exec kill -USR1 (命名空间内发送, PID 唯一不误伤)
# ----------------------------------------------------------------------------
set -uo pipefail

IPC_DIR="/tmp/whisper-ipc"
PID_FILE="$IPC_DIR/whisper-dictation.pid"
STATE_FILE="$IPC_DIR/whisper-dictation.state"
COMPOSE_FILE="/data/voice-input/docker-compose.yml"
CONTAINER="whisper-voice-all-in-one"
CTRL_URL="http://127.0.0.1:8766"

notify() { notify-send -h string:synchronous:voice-dictation -t 1200 "$1" "${2:-}" >/dev/null 2>&1 || true; }

# 解析宿主机 Xauthority（$XAUTHORITY_HOST > $XAUTHORITY > GDM cookie > ~/.Xauthority）
resolve_xauthority() {
    local cand
    for cand in "${XAUTHORITY_HOST:-}" "$XAUTHORITY" \
                /run/user/1000/gdm/Xauthority /home/tt/.Xauthority; do
        if [ -n "$cand" ] && [ -f "$cand" ]; then
            printf '%s' "$cand"
            return 0
        fi
    done
    printf '%s' /home/tt/.Xauthority
}
: "${XAUTHORITY_HOST:=$(resolve_xauthority)}"
export XAUTHORITY_HOST

# 1) 确保共享 IPC 目录存在（容器内 /tmp/whisper-ipc 对应此目录）。
mkdir -p "$IPC_DIR" 2>/dev/null || true

# 2) 容器未运行 -> 一键拉起（首次需构建/加载模型, 10~60s）。
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q '^true$'; then
    notify "🎤 正在启动 Whisper 容器" "首次启动需加载模型, 请稍后再按 F9..."
    docker compose -f "$COMPOSE_FILE" up -d || true
    exit 0
fi

# 3) 守护进程未就绪（pid 文件缺失或指向已退出进程 -> 仍在加载/异常/残留）。
if ! docker exec "$CONTAINER" sh -c 'test -r "$1" && kill -0 "$(cat "$1")" 2>/dev/null' sh "$PID_FILE" >/dev/null 2>&1; then
    notify "🎤 模型加载中..." "large-v3-turbo 就绪后请再次按 F9"
    exit 0
fi

# 4) 触发前读取状态, 用于提示（避免 kill 后异步竞态）。
STATE="$(cat "$STATE_FILE" 2>/dev/null || true)"

# 5) 触发「开始/结束录音」: 快速直发 -> HTTP 8766 -> 容器内信号, 三重保障。
DAEMON_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
sent=0
if [ -n "$DAEMON_PID" ] \
   && docker top "$CONTAINER" 2>/dev/null | awk -v p="$DAEMON_PID" 'NR>1 && $2==p {found=1} END{exit !found}' \
   && kill -USR1 "$DAEMON_PID" 2>/dev/null; then
    sent=1
elif curl -fsS --max-time 3 "$CTRL_URL/toggle" >/dev/null 2>&1; then
    sent=1
elif docker exec "$CONTAINER" sh -c 'kill -USR1 "$(cat "$1")"' sh "$PID_FILE" >/dev/null 2>&1; then
    sent=1
fi

# 6) 极简状态提示。
if [ "$sent" = 1 ]; then
    case "$STATE" in
        RECORDING|TRANSCRIBING) notify "✔ 停止录音, 转录上屏中..." ;;
        *)                      notify "🎙 开始录音, 再次按 F9 完成并输入" ;;
    esac
else
    notify "⚠ Whisper 触发失败" "请查看容器日志: docker logs $CONTAINER"
    exit 1
fi
exit 0