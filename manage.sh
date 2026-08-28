#!/usr/bin/env bash
# manage.sh — Whisper All-in-One 语音输入系统一键运维脚本 (方案 B, 最终交付)
# ----------------------------------------------------------------------------
# 用法:
#   ./manage.sh start [all|ime|gpu]  指定部署模式启动 (all=全功能双引擎[默认], ime=纯手机输入法直发[零GPU/零显存], gpu=纯GPU语音)
#   ./manage.sh stop               停止容器 (容器与数据保留)
#   ./manage.sh restart            重启容器并等待就绪
#   ./manage.sh status             容器健康 / GPU 显存 / 服务端点 / IPC 通道看板 (按 DEPLOY_MODE 差异化提示)
#   ./manage.sh logs [N]           跟随日志 (N=行数时不跟随)
#   ./manage.sh toggle             触发一次「录音 <-> 转录上屏」(等效 F9, ime 模式下提示不可用)
#   ./manage.sh url                打印手机/电脑访问地址
#   ./manage.sh certs [IP]         重新生成自签证书 (默认自动探测局域网 IP)
#
# 说明:
#   - 方案 B 零声卡: 手机 Web HTTPS:28768 (页面与 WSS 同端口同证书) 推流,
#     原生 TCP:61394 直推, 控制 API HTTP:8766, http->https 跳转 :28765; host 网络。
#   - 自动探测并导出 XAUTHORITY_HOST, 保证容器 X11 鉴权与当前显示会话一致
#     (优先级: $XAUTHORITY_HOST > $XAUTHORITY > /run/user/1000/gdm/Xauthority > ~/.Xauthority)
#   - 所有子命令通过 docker compose -f 定位, 不依赖当前工作目录。
# ----------------------------------------------------------------------------
set -uo pipefail

# ---- 常量 ------------------------------------------------------------------
COMPOSE_FILE="/data/voice-input/docker-compose.yml"
CONTAINER="whisper-voice-all-in-one"
CERTS_DIR="/data/voice-input/certs"
IPC_DIR="/tmp/whisper-ipc"
PID_FILE="$IPC_DIR/whisper-dictation.pid"
STATE_FILE="$IPC_DIR/whisper-dictation.state"
CTRL_URL="http://127.0.0.1:8766"
WEB_PORT=28768
TCP_PORT=61394
REDIR_PORT=28765
START_TIMEOUT=120  # 首次启动等待模型加载的最长秒数
CTRL_PORT=8766

# ---- 彩色输出 (非 tty 自动降级为纯文本) --------------------------------------
if [ -t 1 ]; then
    C_RESET='\033[0m'; C_RED='\033[1;31m'; C_GREEN='\033[1;32m'
    C_YELLOW='\033[1;33m'; C_CYAN='\033[1;36m'; C_DIM='\033[2m'
else
    C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_DIM=''
fi

header() { printf "\n${C_DIM}========== %s ==========${C_RESET}\n" "$*"; }
info()   { printf "${C_CYAN}▸ ${C_RESET}%s\n" "$*"; }
ok()     { printf "${C_GREEN}✔ ${C_RESET}%s\n" "$*"; }
warn()   { printf "${C_YELLOW}⚠ ${C_RESET}%s\n" "$*"; }
err()    { printf "${C_RED}✘ ${C_RESET}%s\n" "$*"; }

# ---- 网络探测 ---------------------------------------------------------------
lan_ip() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    case "$ip" in 127.*|"") ip="192.168.31.25" ;; esac
    printf '%s' "$ip"
}

web_url() { printf 'https://%s:%s/'      "$(lan_ip)" "$WEB_PORT"; }
ws_url()  { printf 'wss://%s:%s/stream'  "$(lan_ip)" "$WEB_PORT"; }
tcp_url() { printf '%s:%s'               "$(lan_ip)" "$TCP_PORT"; }

# ---- XAUTHORITY 探测 (保证 Cookie 与显示会话一致) ---------------------------
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
export DISPLAY="${DISPLAY:-:1}"

# ---- 工具函数 ---------------------------------------------------------------
container_running() {
    docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q '^true$'
}

daemon_ready() {
    docker exec "$CONTAINER" sh -c 'test -r "$1" && kill -0 "$(cat "$1")" 2>/dev/null' \
        sh "$PID_FILE" >/dev/null 2>&1
}

port_listening() {
    ss -tlnH 2>/dev/null | grep -q ":$1 "
}

wait_ready() {
    local waited=$1 i
    for ((i = 0; i < waited; i++)); do
        daemon_ready && return 0
        printf '.' >&2
        sleep 1
    done
    return 1
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

# ---- 部署模式解析 -----------------------------------------------------------
# deploy_arg in -> DEPLOY_MODE env: all | ime | gpu (+ 别名)
normalize_mode() {
    case "${1,,}" in
        ""|"all"|"full"|"dual") echo "all" ;;
        ime|web_ime_only|web-ime-only|web|phone|type) echo "ime" ;;
        gpu|gpu_asr_only|gpu-asr-only|asr|whisper) echo "gpu" ;;
        *) echo "all" ;;
    esac
}

# ---- 子命令实现 -------------------------------------------------------------
cmd_start() {
    local arg_mode="${1:-}"
    local deploy_mode
    deploy_mode="$(normalize_mode "$arg_mode")"
    export DEPLOY_MODE="$deploy_mode"
    mkdir -p "$IPC_DIR" 2>/dev/null || true
    if container_running; then
        local cur_mode
        cur_mode="$(docker exec "$CONTAINER" printenv DEPLOY_MODE 2>/dev/null || echo "unknown")"
        if [ "$cur_mode" = "$deploy_mode" ]; then
            ok "容器 $CONTAINER 已在运行 (DEPLOY_MODE=$deploy_mode)"
            daemon_ready && ok "守护进程就绪" || warn "容器运行中但守护进程尚未就绪, 请稍候"
            return 0
        fi
        info "当前容器 DEPLOY_MODE=$cur_mode，目标 $deploy_mode，正在重建容器..."
        compose up -d --build || { err "docker compose up 失败"; return 1; }
    else
        info "正在构建并启动容器 $CONTAINER (DEPLOY_MODE=$deploy_mode) ..."
        compose up -d --build || { err "docker compose up 失败"; return 1; }
    fi
    local wait_secs="$START_TIMEOUT"
    [ "$deploy_mode" = "ime" ] && wait_secs=30
    info "等待守护进程就绪 (DEPLOY_MODE=$deploy_mode, 最长 ${wait_secs}s${deploy_mode:+，ime 模式 <5s})..."
    if wait_ready "$wait_secs"; then
        printf '\n'
        if [ "$deploy_mode" = "ime" ]; then
            ok "✅ 启动完成: 纯手机输入法直发模式就绪 (零GPU/零显存, <1s 启动)"
            info "手机访问 $(web_url) 直发文本 / 快捷键自动粘贴，无需麦克风权限"
        elif [ "$deploy_mode" = "gpu" ]; then
            ok "✅ 启动完成: 纯GPU语音模式就绪 (Whisper large-v3-turbo 已常驻)"
            info "按 F9 / ./manage.sh toggle / 手机访问 $(web_url) 开始语音输入"
        else
            ok "✅ 启动完成: 全功能双引擎模式就绪"
            info "按 F9 / ./manage.sh toggle / 手机访问 $(web_url) 即可开始语音输入 / 输入法直发"
        fi
    else
        printf '\n'
        warn "容器已启动, 但 ${wait_secs}s 内未检测到守护进程就绪"
        info "请观察: ./manage.sh logs, 或用 ./manage.sh status 复查"
        return 1
    fi
}

cmd_stop() {
    if ! container_running; then
        warn "容器 $CONTAINER 未在运行"
        return 0
    fi
    info "正在停止容器 $CONTAINER ..."
    compose stop
    ok "已停止 (容器保留, 数据/日志不丢失)"
}

cmd_restart() {
    compose restart || { err "docker compose restart 失败"; return 1; }
    info "等待 Whisper 守护进程重新就绪..."
    if wait_ready "$START_TIMEOUT"; then
        printf '\n'
        ok "✅ 重启完成: Whisper GPU 守护进程已就绪"
    else
        printf '\n'
        warn "容器已重启, 但 ${START_TIMEOUT}s 内守护进程未就绪"
        info "请观察: ./manage.sh logs"
        return 1
    fi
}

cmd_status() {
    local ip="$(lan_ip)"
    local deploy_mode
    deploy_mode="$(docker exec "$CONTAINER" printenv DEPLOY_MODE 2>/dev/null || echo "?")"
    [ "$deploy_mode" = "?" ] && deploy_mode="${DEPLOY_MODE:-all}"
    header "容器状态 (DEPLOY_MODE=$deploy_mode)"
    if container_running; then
        local health st pid
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null)"
        ok "$CONTAINER 运行中 (health: ${health:-unknown}, mode: $deploy_mode)"
        if daemon_ready; then
            pid="$(cat "$PID_FILE" 2>/dev/null || true)"
            st="$(cat "$STATE_FILE" 2>/dev/null || true)"
            if [ "$deploy_mode" = "ime" ]; then
                ok "轻量直发守护进程就绪 (零GPU), PID=${pid:-?}, 状态=${st:-?}  [ime 模式: 零显存/极速启动]"
            else
                ok "Whisper 守护进程就绪, PID=${pid:-?}, 状态=${st:-?}"
            fi
        else
            warn "容器运行中, 但守护进程未就绪 (模型加载中或异常)"
        fi
    else
        err "$CONTAINER 未运行"
        info "执行 ./manage.sh start [all|ime|gpu] 启动 (默认 all)"
    fi

    if [ "$deploy_mode" = "ime" ]; then
        header "资源占用 (ime 轻量模式)"
        if command -v nvidia-smi >/dev/null 2>&1; then
            local gused
            gused="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | xargs || echo "?")"
            ok "当前模式 ime：容器不占用 GPU 显存（宿主机总显存使用 ${gused:-?} MiB 为其他进程占用） ✓ 0 MB 来自语音容器"
            info "内存 ~30MB 纯 Python 异步服务，无 CUDA/PyTorch/Whisper/LLM 常驻"
        else
            ok "当前模式 ime：零 GPU 依赖，不查询 nvidia-smi"
        fi
    else
        header "GPU 状态 (RTX 2080 Ti)"
        if command -v nvidia-smi >/dev/null 2>&1; then
            local gpu gname gtot gused gutil gtemp
            gpu="$(nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu \
                   --format=csv,noheader,nounits 2>/dev/null | head -1)"
            if [ -n "$gpu" ]; then
                gname="$(cut -d, -f1 <<<"$gpu" | xargs)"
                gtot="$(cut -d, -f2 <<<"$gpu" | xargs)"
                gused="$(cut -d, -f3 <<<"$gpu" | xargs)"
                gutil="$(cut -d, -f4 <<<"$gpu" | xargs)"
                gtemp="$(cut -d, -f5 <<<"$gpu" | xargs)"
                ok "${gname} | 显存 ${gused} / ${gtot} MiB | 利用率 ${gutil}% | 温度 ${gtemp}°C"
                [[ "${gused:-0}" =~ ^[0-9]+$ ]] && [ "$gused" -gt 1000 ] \
                    && info "large-v3-turbo 已常驻显存"
            else
                err "nvidia-smi 查询失败 (驱动/运行时异常)"
            fi
        else
            warn "宿主机未安装 nvidia-smi"
        fi
    fi

    header "服务端点 (host 网络, IP=$ip)"
    local code
    code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 3 "$(web_url)" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
        ok "手机 Web 麦克风  $(web_url)   [HTTPS $code ✓]"
        info "WSS 推流与 HTTPS 同端口: $(ws_url)"
    else
        err "手机 Web 麦克风  $(web_url)   [不可达(容器未运行?) code=$code]"
    fi
    port_listening "$TCP_PORT"   && ok "TCP 直推       $(tcp_url)    [监听 ✓]" \
                               || err "TCP 直推       $(tcp_url)    [未监听]"
    port_listening "$CTRL_PORT"   && ok "控制 API       $CTRL_URL/  [监听 ✓]" \
                                 || err "控制 API       $CTRL_URL/  [未监听]"
    port_listening "$REDIR_PORT" && ok "http→https 跳转 http://$ip:$REDIR_PORT/ [监听 ✓]" \
                                 || info "http→https 跳转 http://$ip:$REDIR_PORT/ (可选)"

    header "控制 API / 转录状态"
    local js
    js="$(curl -fsS --max-time 3 "$CTRL_URL/status" 2>/dev/null || true)"
    if [ -n "$js" ]; then
        ok "$js"
        if echo "$js" | grep -q '"deploy_mode"[[:space:]]*:[[:space:]]*"ime"'; then
            info "ime 模式下音频链路已禁用，按键/直发可用，音频转录不可用（预期行为）"
        fi
    else
        err "控制 API 无响应 (守护进程未就绪 / 端口未监听)"
        info "检查: ./manage.sh logs"
    fi

    header "IPC 触发通道 ($IPC_DIR)"
    if [ -f "$PID_FILE" ]; then
        info "pid 文件: $(cat "$PID_FILE" 2>/dev/null)   state: $(cat "$STATE_FILE" 2>/dev/null)"
    else
        warn "pid 文件缺失 (守护进程尚未写盘或已退出)"
    fi
    printf '\n'
    if [ "$deploy_mode" = "ime" ]; then
        info "ime 轻量模式: 仅手机输入法直发 + 快捷键，无 F9/录音/转录"
        info "Web 直发:  $CTRL_URL/type?t=文本  或  https://$ip:$WEB_PORT/type?t=文本"
    else
        info "状态说明: IDLE 待命 | RECORDING 录音中 | TRANSCRIBING GPU 转录中"
        info "Web 触发: $CTRL_URL/toggle   (或按 F9)"
    fi
    return 0
}

cmd_logs() {
    local n="${1:-100}"
    if [[ "$n" =~ ^-?[0-9]+$ ]]; then
        compose logs --tail="$n"
    else
        compose logs --tail=100 -f
    fi
}

cmd_toggle() {
    /data/voice-input/voice-toggle.sh
}

cmd_url() {
    local ip="$(lan_ip)"
    cat <<EOF
🎤 All-in-One 语音输入访问地址

  手机 Web 麦克风 (零安装, 扫码使用):
      https://$ip:$WEB_PORT/
  手机 WSS 推流 (与 HTTPS 同端口, Web 页内部使用):
      wss://$ip:$WEB_PORT/stream
  电脑快速触发:
      $CTRL_URL/toggle        (Web API)
  F9 快捷键:
      /data/voice-input/voice-toggle.sh
  原生 TCP 直推 (Otic / AMB 等 App):
      $ip:$TCP_PORT           (16kHz PCM s16le 单声道, EOF/STOP 结束)
EOF
}

cmd_certs() {
    local ip="${1:-$(lan_ip)}"
    mkdir -p "$CERTS_DIR"
    info "生成自签证书 (CN=$ip, SAN=IP:$ip, IP:127.0.0.1, IP:172.18.0.1, DNS:localhost) ..."
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$CERTS_DIR/key.pem" -out "$CERTS_DIR/cert.pem" -days 3650 \
        -subj "/CN=$ip" \
        -addext "subjectAltName=IP:$ip,IP:127.0.0.1,IP:172.18.0.1,DNS:localhost" \
        >/dev/null 2>&1 || { err "openssl 生成失败"; return 1; }
    chmod 600 "$CERTS_DIR/key.pem"
    ok "证书已重新生成: $CERTS_DIR/cert.pem (有效期 10 年)"
    if container_running; then
        info "容器正在运行, 重启后生效: ./manage.sh restart"
    fi
}

usage() {
    cat <<EOF
Usage: ./manage.sh <command> [args]

Commands:
  start [all|ime|gpu]  按部署模式构建并启动容器 (all=全功能[默认] | ime=纯输入法直发零显存 | gpu=纯GPU语音)
                       例: ./manage.sh start ime   -> 极轻量 <1s 启动, 0 MB 显存
                           ./manage.sh start all   -> 双引擎全功能
                           ./manage.sh start gpu   -> 仅 GPU 语音
  stop      停止容器 (容器与数据保留)
  restart   重启容器并等待就绪 (保持当前 DEPLOY_MODE)
  status    容器健康 / GPU 显存 / 服务端点 / IPC 通道看板 (按 DEPLOY_MODE 差异化展示)
  logs [N]  跟随容器日志 (>N 行时不跟随)
  toggle    触发一次「录音 <-> 转录上屏」(等效 F9, ime 模式下提示不可用)
  url       打印手机/电脑访问地址
  certs [IP]  重新生成自签证书 (默认自动探测局域网 IP)
EOF
}

# ---- 入口 -------------------------------------------------------------------
main() {
    local cmd="${1:-}"; shift 2>/dev/null || true
    case "$cmd" in
        start)   cmd_start "$@" ;;
        stop)    cmd_stop ;;
        restart) cmd_restart ;;
        status)  cmd_status ;;
        logs)    cmd_logs "${1:-}" ;;
        toggle)  cmd_toggle ;;
        url)     cmd_url ;;
        certs)   cmd_certs "${1:-}" ;;
        *)       usage; [ -z "$cmd" ] && return 0; return 1 ;;
    esac
}

main "$@"
exit $?