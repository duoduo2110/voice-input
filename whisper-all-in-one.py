#!/usr/bin/env python3
"""
whisper-all-in-one.py — 方案 B: All-in-One 纯净容器 (零 AudioRelay / 零 PulseAudio)
====================================================================================
彻底告别声卡中继: 音频以字节流直接在内存 bytearray 中流转, 无需 virtaul-sink、
无需 parec、无需宿主机 PulseAudio。容器同时内置 5 个服务:

  端口   协议   服务
  ----   ----   --------------------------------------------------------
  28768  HTTPS  手机 Web 麦克风页面 + WSS WebSocket(/stream) 同端口
                (单端口单证书 = 浏览器信任一次即同时覆盖页面与麦克风推流,
                 规避 iOS/新版 Chrome 的跨端口证书拦截问题)
  61394  TCP    第三方原生推流工具原始 PCM 字节直连 (EOF 或 "STOP" 停止)
  8766   HTTP   宿主机/RustDesk 内部浏览器快速控制 API (/toggle, /status, /healthz)
  28765  HTTP   http:// -> https://28768 的跳转入口
  (端口均可经环境变量 WEB_PORT / CTRL_PORT / REDIR_PORT / TCP_PORT 覆盖)

信号链路 (宿主机 F9):
  voice-toggle.sh -> SIGUSR1(/home/ipc: /tmp/whisper-ipc/whisper-dictation.pid)
      第 1 次 -> 开始录音 (接受 Web/TCP 任意来源字节流)
      第 2 次 -> 结束录音, GPU 转录 (faster-whisper large-v3-turbo float16),
                 结果经 xclip + xdotool 按焦点窗口智能粘贴 (默认终端 alt+v / GUI ctrl+v,
                 可用 TERMINAL_PASTE_KEY / GUI_PASTE_KEY 覆盖) 注入 X11 活动光标

状态文件: /tmp/whisper-ipc/whisper-dictation.{pid,state}
state 取值: IDLE / RECORDING / TRANSCRIBING
"""

import asyncio
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response as WSResponse

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # websockets < 10
    from websockets import ConnectionClosed
from faster_whisper import WhisperModel

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
IPC_DIR = "/tmp/whisper-ipc"
PID_FILE = os.path.join(IPC_DIR, "whisper-dictation.pid")
STATE_FILE = os.path.join(IPC_DIR, "whisper-dictation.state")
CERTS_DIR = Path("/app/certs")
CERT_PEM = CERTS_DIR / "cert.pem"
KEY_PEM = CERTS_DIR / "key.pem"

WEB_PORT = int(os.environ.get("WEB_PORT", "28768"))    # HTTPS: 手机页 + WSS(/stream) 同端口
CTRL_PORT = int(os.environ.get("CTRL_PORT", "8766"))   # HTTP: 控制 API
REDIR_PORT = int(os.environ.get("REDIR_PORT", "28765"))  # HTTP: 跳转到 HTTPS
TCP_PORT = int(os.environ.get("TCP_PORT", "61394"))    # TCP: 原生 PCM 推流

RATE = 16000
CHANNELS = 1
MIN_BYTES = int(RATE * 2 * 0.2)   # <200ms 视为误触

MODEL_NAME = "large-v3-turbo"
RECORD_LAN = 0
START_TIME = time.time()

# ---------------------------------------------------------------------------
# 环境自检 / 模型加载 (模型加载成功才写 pid, 保证"就绪"语义)
# ---------------------------------------------------------------------------
print(f"[AllInOne] DISPLAY={os.environ.get('DISPLAY', 'NOT SET')}", flush=True)
print(f"[AllInOne] XAUTHORITY={os.environ.get('XAUTHORITY', 'NOT SET')}", flush=True)
print(f"[AllInOne] 正在加载 {MODEL_NAME} (CUDA float16) ...", flush=True)
MODEL = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
print(f"[AllInOne] {MODEL_NAME} 加载完成。", flush=True)

# ---------------------------------------------------------------------------
# 本地轻量 LLM 智能纠错引擎 (Qwen2.5-0.5B-Instruct, CUDA fp16 ~1GB)
# 可用环境变量 ENABLE_LLM_CORRECT=false 关闭; 异常时自动回退原始 ASR 文本
# ---------------------------------------------------------------------------
CORRECTOR = None
try:
    from corrector import ASRCorrector
    CORRECTOR = ASRCorrector()
except Exception as e:
    print(f"[AllInOne] 智能纠错引擎不可用: {e}", flush=True)
    CORRECTOR = None


def set_corrector_enabled(on):
    """运行时开关 LLM 纠错; 返回实际生效状态。"""
    if CORRECTOR is not None:
        return CORRECTOR.set_enabled(on)
    return False

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
recording = False
transcribing = False
recording_source = ""
audio_buffer = bytearray()
lock = threading.Lock()
state_lock = threading.Lock()

wss_clients = 0
tcp_clients = 0

NOTIFY_BIN = shutil.which("notify-send")      # 容器内通常无此工具 -> 通知短路


# ---------------------------------------------------------------------------
# 基础工具: X11 / 通知 / 状态文件
# ---------------------------------------------------------------------------
def detect_display():
    display = os.environ.get("DISPLAY", "").strip()
    if display and display != ":0":
        return display
    for num in ("1", "0"):
        if os.path.exists(f"/tmp/.X11-unix/X{num}"):
            return f":{num}"
    return display or ":1"


def detect_xauthority():
    for cand in (os.environ.get("XAUTHORITY", "").strip(),
                 "/run/user/1000/gdm/Xauthority",
                 "/root/.Xauthority"):
        if cand and os.path.isfile(cand):
            return cand
    return "/root/.Xauthority"


def get_env():
    env = os.environ.copy()
    env["DISPLAY"] = detect_display()
    env.setdefault("XAUTHORITY", detect_xauthority())
    return env


def send_notify(title, expire_ms=600):
    if NOTIFY_BIN is None:
        return
    subprocess.call(
        [NOTIFY_BIN, "-h", "string:synchronous:voice-dictation",
         "-t", str(expire_ms), title],
        env=get_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def write_state(state):
    with state_lock:
        try:
            with open(STATE_FILE, "w") as f:
                f.write(state)
        except Exception as e:
            print(f"[AllInOne] 写入状态失败: {e}", flush=True)


def current_state():
    try:
        return open(STATE_FILE).read().strip()
    except Exception:
        return "IDLE"


# 终端特征串(小写子串匹配): 覆盖 GNOME/KDE/XFCE/xterm 系常见终端
TERMINAL_HINTS = (
    "gnome-terminal", "kitty", "alacritty", "xterm", "uxterm", "rxvt",
    "urxvt", "konsole", "tilix", "terminator", "wezterm", "termite",
    "xfce4-terminal", "mate-terminal", "lxterminal", "qterminal",
    "sakura", "cool-retro-term", "contour", "foot", "pterm", "st-",
)
# 粘贴组合键(可用环境变量覆盖; 用户已将终端快捷键改为 Alt+V)
TERMINAL_PASTE_KEY = os.environ.get("TERMINAL_PASTE_KEY", "alt+v")   # 终端
GUI_PASTE_KEY = os.environ.get("GUI_PASTE_KEY", "ctrl+v")            # 常规 GUI


def _active_window_info(env):
    """返回当前活动窗口的进程特征串(comm + cmdline, 小写合并), 失败返回空串。
    依赖 pid: host 共享命名空间: xdotool getwindowpid 拿到业主人进程 PID 后,
    直接读 /proc/<pid>/comm 与 cmdline 推断应用类型, 无需额外安装 xprop。"""
    try:
        out = subprocess.run(["xdotool", "getactivewindow"],
                             check=True, capture_output=True, text=True, env=env)
        win_id = out.stdout.strip()
        if not win_id:
            return ""
        ps = subprocess.run(["xdotool", "getwindowpid", win_id],
                            check=False, capture_output=True, text=True,
                            env=env).stdout.strip()
        if not ps.isdigit():
            return ""
        comm, cmdline = "", ""
        try:
            comm = open(f"/proc/{ps}/comm").read().strip()
        except Exception:
            pass
        try:
            cmdline = open(f"/proc/{ps}/cmdline").read().replace("\0", " ").strip()
        except Exception:
            pass
        return (comm + "\n" + cmdline).lower()
    except Exception:
        return ""


def _paste_combo(wm_info):
    """按活动窗口类型选择粘贴组合:
    终端 -> TERMINAL_PASTE_KEY(默认 alt+v, 用户自定义快捷键可经环境变量覆盖);
    其余 GUI / 检测失败 -> GUI_PASTE_KEY(默认 ctrl+v)。"""
    if not wm_info:
        return GUI_PASTE_KEY                  # 检测不到 -> GUI 默认 ctrl+v
    if any(t in wm_info for t in TERMINAL_HINTS):
        return TERMINAL_PASTE_KEY             # 终端 -> alt+v
    return GUI_PASTE_KEY                      # Chrome/VS Code/文本框等一律 ctrl+v


def type_via_clipboard(text):
    text = (text or "").strip()
    if not text:
        print("[AllInOne] 转录为空, 不注入。", flush=True)
        return
    print(f"[AllInOne Output] {text}", flush=True)
    env = get_env()
    try:
        # 1) 写入全套选择区: clipboard(ctrl+v 读取) + primary(中键/选择区)
        for selection in ("clipboard", "primary"):
            p = subprocess.Popen(
                ["xclip", "-selection", selection],
                stdin=subprocess.PIPE, env=env,
            )
            p.communicate(input=text.encode("utf-8"))
        # 2) 等待剪贴板写入完成落盘, 避免按键时内容尚未就绪
        time.sleep(0.05)
        # 3) 智能按键: 终端 -> TERMINAL_PASTE_KEY(alt+v), GUI -> GUI_PASTE_KEY(ctrl+v)
        combo = _paste_combo(_active_window_info(env))
        print(f"[AllInOne] 焦点窗口分类完毕, 粘贴组合: {combo}", flush=True)
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "--delay", "20", combo],
            check=False, env=env,
        )
    except Exception as e:
        print(f"[AllInOne] 注入剪贴板失败: {e}", flush=True)


# ---------------------------------------------------------------------------
# 快捷按键分发 (严格白名单, 拒绝任意命令注入)
# ---------------------------------------------------------------------------
# 仅允许以下动作; 每个动作映射到硬编码的 xdotool 参数列表, 不接受任意输入
KEY_ACTIONS = ("backspace1", "backspace2", "clear", "enter")


def _send_keys(combo, env):
    subprocess.run(["xdotool", "key", "--clearmodifiers", "--delay", "20", *combo],
                   check=False, env=env)


def dispatch_key(action):
    """按白名单动作向当前活动窗口发送按键。返回 (ok, message)。
    - backspace1/2: 删除 1/2 个字符 (BackSpace)
    - clear: 清空输入框 (终端 -> ctrl+c 中断行输入; GUI -> ctrl+a 全选后删除)
    - enter: 发送回车 (Return)
    非白名单动作一律拒绝, 绝不将用户输入拼接进 xdotool 命令。"""
    if action not in KEY_ACTIONS:
        return False, f"forbidden action: {action!r}"
    env = get_env()
    wm = _active_window_info(env)
    try:
        if action == "backspace1":
            _send_keys(["BackSpace"], env)
        elif action == "backspace2":
            _send_keys(["BackSpace", "BackSpace"], env)
        elif action == "enter":
            _send_keys(["Return"], env)
        elif action == "clear":
            if any(t in wm for t in TERMINAL_HINTS):
                _send_keys(["ctrl+c"], env)               # 终端: 中断/清行
            else:
                _send_keys(["ctrl+a", "BackSpace"], env)  # GUI: 全选后删除
        print(f"[AllInOne] key action: {action} -> {wm or '(unknown window)'}", flush=True)
        return True, action
    except Exception as e:
        print(f"[AllInOne] key action 失败: {e}", flush=True)
        return False, str(e)


def _key_from_request_path(req_path, req_body=b""):
    """从请求路径(?k=..)与可选 body(JSON/form)中解析按键动作; 缺失/非法返回 ''。"""
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(req_path).query)
    k = (qs.get("k") or [""])[0]
    if k:
        return k
    if req_body:
        try:
            data = json.loads(req_body)
            k = data.get("k") or data.get("action") or ""
        except Exception:
            pass
    return k


# ---------------------------------------------------------------------------
# 录音会话 (内存 bytearray, 零声卡)
# ---------------------------------------------------------------------------
def begin_recording(source):
    """开启录音会话(任一来源)。失败(忙/转录中)返回 False。"""
    global recording, recording_source
    with lock:
        if recording or transcribing:
            print(f"[AllInOne] busy: recording={recording} transcribing={transcribing}", flush=True)
            return False
        recording = True
        recording_source = source
        audio_buffer.clear()
    write_state("RECORDING")
    print(f"[AllInOne] 开始录音 (来源: {source})", flush=True)
    send_notify("🎙 录音中...", expire_ms=3000)
    return True


def feed(data):
    """往内存缓冲区追加 PCM。会话被外部结束则返回 False(上层应停止推流)。"""
    with lock:
        if not recording:
            return False
        audio_buffer.extend(data)
        return True


def begin_stop_and_transcribe():
    """结束录音并异步转录(锁内原子切换状态, 转录跑在独立 worker 线程)。"""
    global recording, recording_source, transcribing
    with lock:
        if not recording or transcribing:
            return False
        recording = False
        transcribing = True
        raw = bytes(audio_buffer)
        src = recording_source
        recording_source = ""
        audio_buffer.clear()
    write_state("TRANSCRIBING")
    threading.Thread(target=_transcribe_worker, args=(raw, src), daemon=True).start()
    return True


def _transcribe_worker(raw, src):
    global transcribing
    try:
        if len(raw) < MIN_BYTES:
            print(f"[AllInOne] 音频过短({len(raw)}B), 跳过转录。", flush=True)
            return
        if len(raw) % 2 != 0:
            raw = raw[:-1]          # 奇数字节帧截断对齐, 防 np.frombuffer 字节不对齐异常
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.time()
        segments, _info = MODEL.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500,
                                speech_pad_ms=200),
            initial_prompt="简体中文与English中英混合语音输入，包含英文字母与专业术语",
        )
        text = "".join(seg.text for seg in segments).strip()
        elapsed = (time.time() - t0) * 1000
        print(f"[AllInOne] 转录完成 {elapsed:.1f}ms ({len(raw)}B, 来源={src}) -> {text!r}",
              flush=True)
        if text:
            out_text = text
            if CORRECTOR is not None and CORRECTOR.enabled:
                out_text = CORRECTOR.correct(text)
            type_via_clipboard(out_text)
            send_notify("✔ 已输出", expire_ms=400)
    except Exception as e:
        print(f"[AllInOne] 转录出错: {e}", flush=True)
    finally:
        with lock:
            transcribing = False
        write_state("IDLE")


def toggle_record():
    """统一切换入口(SIGUSR1 / HTTP /toggle 均走此)。"""
    global recording, transcribing
    with lock:
        if transcribing:
            send_notify("⏳ 正在转录中...", expire_ms=800)
            return False
        want_start = not recording
    if want_start:
        return begin_recording("ipc")
    return begin_stop_and_transcribe()


# ---------------------------------------------------------------------------
# 证书 (自签, 含 IP SAN; 缺失时自动生成)
# ---------------------------------------------------------------------------
def detect_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _cert_has_ip(cert_path, ip):
    """检查现有证书 SAN 是否已包含目标 IP。"""
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout",
             "-ext", "subjectAltName"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
        return f"IP Address:{ip}" in (out.stdout or "")
    except Exception:
        return False


def ensure_certs():
    """自签证书管理: 缺失或当前局域网 IP 与证书 SAN 不一致时自动重签。"""
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    ip = detect_lan_ip()
    if CERT_PEM.is_file() and KEY_PEM.is_file() and _cert_has_ip(CERT_PEM, ip):
        return
    print(f"[AllInOne] 生成/更新自签证书 (当前 IP={ip}, 旧证书 IP SAN 不一致或缺失) ...",
          flush=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(KEY_PEM), "-out", str(CERT_PEM), "-days", "3650",
         "-subj", f"/CN={ip}",
         "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.chmod(KEY_PEM, 0o600)


def build_ssl_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(CERT_PEM), str(KEY_PEM))
    return ctx


# ---------------------------------------------------------------------------
# WSS: 手机 Web 麦克风推流
# ---------------------------------------------------------------------------
async def ws_handler(ws):
    global wss_clients
    with lock:
        wss_clients += 1
    started = False
    try:
        if not begin_recording("web"):
            await ws.send(json.dumps({"status": "busy",
                                      "error": "另一路音频会话进行中"}))
            return
        started = True               # 仅本次连接成功接管会话时才负责收尾
        await ws.send(json.dumps({"status": "RECORDING"}))
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    if not feed(msg):          # 会话被外部(F9/HTTP)结束
                        await ws.send(json.dumps({"status": "STOPPED", "err": "external"}))
                        break
                else:
                    try:
                        data = json.loads(msg)
                    except Exception:
                        data = {"cmd": str(msg)}
                    cmd = data.get("cmd")
                    if cmd == "stop":
                        break
                    if cmd == "key":
                        ok, msg = dispatch_key(data.get("action") or "")
                        try:
                            await ws.send(json.dumps(
                                {"status": "KEY", "ok": ok, "action": msg}))
                        except Exception:
                            pass
        except ConnectionClosed:
            pass
    finally:
        if started:                  # 只有启动成功的连接才允许停止/转录当前会话
            begin_stop_and_transcribe()
            try:
                await ws.send(json.dumps({"status": "IDLE"}))
            except Exception:
                pass
        with lock:
            wss_clients -= 1


def _http_response(status, ctype, body):
    reason = "OK" if status < 400 else "Not Found"
    headers = Headers([
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
        ("Cache-Control", "no-store"),
    ])
    return WSResponse(status, reason, headers, body)


async def http_process_request(connection, request):
    """同一 TLS 端口上的 HTTP/WS 路由:
    /stream 且携带 Upgrade: websocket -> 返回 None 交给 WebSocket handler;
    其余路径在 HTTP 层直接应答(页面 / toggle / status / healthz / key)。"""
    raw = request.path or "/"
    path = urllib.parse.urlsplit(raw).path
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        if path == "/stream":
            return None                       # 转交 WebSocket handler
        return _http_response(404, "text/plain; charset=utf-8", b"not found")
    if path in ("/", "/index.html"):
        return _http_response(200, "text/html; charset=utf-8",
                              HTML_PAGE.encode("utf-8"))
    if path == "/toggle":
        toggle_record()
        return _http_response(200, "application/json; charset=utf-8",
                              json.dumps(status_payload(), ensure_ascii=False).encode("utf-8"))
    if path == "/status":
        return _http_response(200, "application/json; charset=utf-8",
                              json.dumps(status_payload(), ensure_ascii=False).encode("utf-8"))
    if path == "/healthz":
        return _http_response(200, "text/plain; charset=utf-8", b"ok")
    if path in ("/key", "/api/key"):
        k = _key_from_request_path(raw, getattr(request, "body", b""))
        ok, msg = dispatch_key(k)
        return _http_response(200 if ok else 400,
                              "application/json; charset=utf-8",
                              json.dumps({"ok": ok, "action": msg},
                                         ensure_ascii=False).encode("utf-8"))
    if path == "/corrector":
        on = (urllib.parse.parse_qs(urllib.parse.urlsplit(raw).query).get("on") or [""])[0]
        if on in ("1", "true", "on"):
            set_corrector_enabled(True)
        elif on in ("0", "false", "off"):
            set_corrector_enabled(False)
        return _http_response(200, "application/json; charset=utf-8",
                              json.dumps({"ok": True,
                                          "llm_correct": bool(CORRECTOR is not None
                                                              and CORRECTOR.enabled)},
                                         ensure_ascii=False).encode("utf-8"))
    return _http_response(404, "text/plain; charset=utf-8", b"not found")


async def web_main(ssl_ctx):
    """单端口承载 HTTPS 网页 + WSS(/stream): 手机浏览器只需信任 28768 一次,
    页面与麦克风推流同证书同源, 彻底规避跨端口证书拦截。"""
    async with websockets.serve(
        ws_handler, "0.0.0.0", WEB_PORT, ssl=ssl_ctx,
        process_request=http_process_request,
        max_size=1 << 20,
    ) as server:
        print(f"[AllInOne] HTTPS 页面 + WSS(/stream) 同端口监听 :{WEB_PORT}", flush=True)
        await asyncio.Future()


# ---------------------------------------------------------------------------
# TCP: 原生 PCM 直推 (发字节=录, EOF/"STOP" 停)
# ---------------------------------------------------------------------------
def tcp_session(conn):
    global tcp_clients
    started = False
    try:
        with lock:
            tcp_clients += 1
        conn.settimeout(600)
        if not begin_recording("tcp"):
            conn.close()
            return
        started = True               # 仅接管会话成功后才负责收尾/转录
        while True:
            try:
                chunk = conn.recv(8192)
            except socket.timeout:
                break
            except Exception:
                break
            if not chunk:
                break
            if chunk.strip() == b"STOP":
                break
            if not feed(chunk):
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if started:                  # 被拒绝的连接受 finally 约束, 不误杀他方会话
            begin_stop_and_transcribe()
        with lock:
            tcp_clients -= 1


def tcp_main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", TCP_PORT))
    srv.listen(16)
    print(f"[AllInOne] TCP PCM 监听 :{TCP_PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=tcp_session, args=(conn,), daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP(S) 控制层: 页面 + /toggle + /status + 跳转
# ---------------------------------------------------------------------------
def status_payload():
    with lock:
        return {
            "service": "whisper-all-in-one",
            "state": current_state(),
            "recording": recording,
            "transcribing": transcribing,
            "source": recording_source,
            "audio_bytes": len(audio_buffer),
            "wss_clients": wss_clients,
            "tcp_clients": tcp_clients,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - START_TIME),
            "model": MODEL_NAME,
            "llm_correct": bool(CORRECTOR is not None and CORRECTOR.enabled),
            "llm_model": (CORRECTOR.model_name if CORRECTOR else ""),
"ports": {"web": WEB_PORT, "wss": WEB_PORT,   # WSS 与 HTTPS 同端口(28768)
          "ctrl": CTRL_PORT, "tcp": TCP_PORT,
          "redirect": REDIR_PORT},
        }


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0a0c16">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>🎤 语音输入</title>
<style>
:root{
  --bg0:#0a0c16;
  --fg:#eef1fb; --muted:#9aa6c4;
  --accent:#7c9bff; --accent2:#a86bff;
  --rec:#ff5d79; --rec2:#ffa35c;
  --stroke:rgba(255,255,255,.15);
  --glass:rgba(255,255,255,.06);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}
html,body{height:100%}
body{
  background:
    radial-gradient(90% 55% at 12% 0%,rgba(76,96,255,.26),transparent 62%),
    radial-gradient(80% 65% at 92% 14%,rgba(168,86,255,.18),transparent 60%),
    radial-gradient(110% 80% at 50% 118%,rgba(46,130,255,.15),transparent 62%),
    var(--bg0);
  color:var(--fg);
  font-family:"SF Pro Display","HarmonyOS Sans SC","MiSans","PingFang SC","Noto Sans SC",system-ui,sans-serif;
  min-height:100vh;min-height:100dvh;
  display:flex;flex-direction:column;
  padding:18px 20px 26px;
  touch-action:manipulation;
  -webkit-user-select:none;user-select:none;
  overflow-y:auto;overscroll-behavior:none;
}
@supports (padding:max(0px,env(safe-area-inset-top))){
  body{
    padding-top:max(18px,env(safe-area-inset-top));
    padding-right:max(20px,env(safe-area-inset-right));
    padding-bottom:max(26px,env(safe-area-inset-bottom));
    padding-left:max(20px,env(safe-area-inset-left));
  }
}
/* 胶片噪点叠加 */
body::after{
  content:"";position:fixed;inset:0;z-index:99;pointer-events:none;opacity:.55;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.05'/%3E%3C/svg%3E");
  mix-blend-mode:overlay;
}
.stage{flex:1;display:flex;flex-direction:column;width:100%;max-width:440px;margin:0 auto}
header{text-align:center;flex-shrink:0}
.stage header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.stage header .ttl{flex:1;min-width:0}
#corrToggle{
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
  background:linear-gradient(160deg,rgba(83,232,139,.20),rgba(83,232,139,.05));
  border:1px solid rgba(83,232,139,.38);color:#b7f5cd;border-radius:999px;
  font-size:12px;padding:7px 12px;cursor:pointer;flex-shrink:0;margin-top:2px;
  touch-action:manipulation;-webkit-tap-highlight-color:transparent;
  transition:transform .06s,background .15s;
}
#corrToggle.off{background:linear-gradient(160deg,rgba(255,255,255,.10),rgba(255,255,255,.03));border-color:rgba(255,255,255,.16);color:var(--muted)}
#corrToggle:active{transform:scale(.92)}
h1{font-size:20px;font-weight:700;letter-spacing:.6px;display:flex;align-items:center;justify-content:center;gap:9px}
h1 .live{width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,#53e88b,#2fb968);box-shadow:0 0 8px rgba(83,232,139,.85);animation:hb 2.4s ease-in-out infinite}
@keyframes hb{50%{opacity:.35}}
.sub{color:var(--muted);font-size:12px;margin-top:6px;letter-spacing:.5px}
#banner{display:block;font-size:13.5px;line-height:1.65;background:rgba(255,88,84,.12);border:1px solid rgba(255,110,100,.4);color:#ffd7d4;border-radius:14px;padding:10px 14px;margin:12px 0 0;text-align:left;white-space:pre-wrap;-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);flex-shrink:0}
#banner[hidden]{display:none}
#banner.err{animation:blink 1.4s ease-in-out 3}
@keyframes blink{50%{opacity:.55}}
#status{margin:12px auto 0;display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:600;color:var(--muted);background:var(--glass);border:1px solid var(--stroke);border-radius:999px;padding:8px 16px;min-height:36px;letter-spacing:.4px;text-align:center;-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);flex-shrink:0}
#status::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;flex:none}
.modes{display:flex;gap:10px;justify-content:center;margin-top:16px;flex-shrink:0}
.modes button{
  flex:1;max-width:156px;padding:13px 18px;border:1px solid var(--stroke);background:var(--glass);
  color:var(--muted);border-radius:999px;font-size:15px;font-weight:600;letter-spacing:.5px;cursor:pointer;
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);
  transition:transform .1s ease,background .25s ease,color .25s ease,border-color .25s ease,box-shadow .25s ease;
}
.modes button:active{transform:scale(.94)}
.modes button.active{
  background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-color:transparent;
  box-shadow:0 8px 22px -6px rgba(124,155,255,.55),inset 0 1px 1px rgba(255,255,255,.4);
}
.guide{
  font-size:14px;line-height:1.7;margin-top:16px;color:var(--muted);background:var(--glass);
  border:1px solid var(--stroke);border-radius:16px;padding:12px 16px;text-align:center;white-space:pre-wrap;
  letter-spacing:.4px;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);flex-shrink:0;
}
.guide.good{background:rgba(56,210,119,.12);border-color:rgba(72,222,132,.38);color:#c9f7d9}
/* ---------- 大拇指黄金触控区 ---------- */
.thumb{flex:1;min-height:clamp(200px,60vw,224px);display:flex;align-items:center;justify-content:center;position:relative}
.orb{position:relative;width:clamp(196px,58vw,220px);height:clamp(196px,58vw,220px);display:flex;align-items:center;justify-content:center;animation:bob 6s ease-in-out infinite}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
/* 录音态: 多层涟漪扩散 */
.orb .ring{position:absolute;inset:0;border-radius:50%;pointer-events:none;opacity:0;border:2px solid rgba(255,99,120,.5);box-shadow:0 0 26px rgba(255,99,120,.2),inset 0 0 20px rgba(255,99,120,.12)}
.orb.rec .ring{animation:ripple 2s cubic-bezier(.22,.68,.35,1) infinite}
.orb.rec .ring:nth-child(2){animation-delay:.66s}
.orb.rec .ring:nth-child(3){animation-delay:1.32s}
@keyframes ripple{0%{transform:scale(.78);opacity:.7}100%{transform:scale(1.55);opacity:0}}
/* 环境光晕 */
.orb::after{content:"";position:absolute;inset:-20px;border-radius:50%;z-index:-1;background:radial-gradient(50% 50% at 50% 50%,rgba(124,155,255,.38),transparent 70%);filter:blur(16px);transition:background .35s ease}
.orb.rec::after{background:radial-gradient(50% 50% at 50% 50%,rgba(255,93,121,.55),rgba(255,140,80,.22) 55%,transparent 74%)}
.big{
  position:relative;width:100%;height:100%;border-radius:50%;border:1px solid rgba(255,255,255,.22);cursor:pointer;
  color:#fff;font-weight:600;letter-spacing:1px;touch-action:none;will-change:transform;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;
  background:
    radial-gradient(130% 130% at 30% 22%,rgba(255,255,255,.34),rgba(255,255,255,.05) 34%,rgba(255,255,255,0) 60%),
    linear-gradient(155deg,rgba(124,155,255,.55),rgba(58,86,220,.5) 45%,rgba(24,34,92,.62));
  box-shadow:0 22px 46px -10px rgba(0,0,0,.6),0 10px 28px -6px rgba(96,130,255,.38),inset 0 1px 1px rgba(255,255,255,.42),inset 0 -16px 28px rgba(10,14,40,.38);
  -webkit-backdrop-filter:blur(16px) saturate(150%);backdrop-filter:blur(16px) saturate(150%);
  transition:transform .12s cubic-bezier(.34,1.56,.64,1),background .35s ease,box-shadow .35s ease,border-color .35s ease;
}
.big::before{content:"";position:absolute;inset:16px;border-radius:50%;pointer-events:none;z-index:0;background:radial-gradient(60% 60% at 50% 36%,rgba(255,255,255,.24),rgba(255,255,255,.02) 72%);filter:blur(3px)}
.big > *{position:relative;z-index:1}
.big em{font-style:normal;font-size:54px;line-height:1;filter:drop-shadow(0 5px 12px rgba(0,0,0,.4))}
.big small{font-size:15px;font-weight:500;opacity:.95;letter-spacing:3px}
.big:active{transform:scale(.92);box-shadow:0 10px 22px -6px rgba(0,0,0,.5),inset 0 2px 10px rgba(0,0,0,.32)}
.big.granted{
  background:
    radial-gradient(130% 130% at 30% 22%,rgba(255,255,255,.34),rgba(255,255,255,.05) 34%,rgba(255,255,255,0) 60%),
    linear-gradient(155deg,rgba(83,214,140,.58),rgba(26,150,90,.5) 45%,rgba(9,56,38,.62));
  box-shadow:0 22px 46px -10px rgba(0,0,0,.6),0 10px 28px -6px rgba(60,210,130,.38),inset 0 1px 1px rgba(255,255,255,.42),inset 0 -16px 28px rgba(6,38,24,.4);
}
.big.rec{
  border-color:rgba(255,180,190,.42);
  background:
    radial-gradient(130% 130% at 30% 22%,rgba(255,255,255,.32),rgba(255,255,255,.05) 34%,rgba(255,255,255,0) 60%),
    linear-gradient(155deg,rgba(255,93,121,.66),rgba(214,45,80,.56) 45%,rgba(88,12,38,.62));
  box-shadow:0 22px 46px -10px rgba(0,0,0,.6),0 0 36px -4px rgba(255,93,121,.6),0 0 90px -12px rgba(255,120,90,.38),inset 0 1px 1px rgba(255,255,255,.46),inset 0 -16px 28px rgba(70,8,30,.42);
  animation:breathe 1.5s ease-in-out infinite;
}
@keyframes breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
/* ---------- 快捷编辑工具栏 (毛玻璃 + 动态触感涟漪特效) ---------- */
.keys{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}
.keys button{
  position:relative;overflow:hidden;
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
  background:linear-gradient(160deg,rgba(255,255,255,.10),rgba(255,255,255,.03));
  border:1px solid rgba(255,255,255,.16);border-radius:14px;color:var(--fg);
  font-size:11px;padding:9px 2px;cursor:pointer;line-height:1.3;
  touch-action:manipulation;-webkit-tap-highlight-color:transparent;
  transition:transform .08s cubic-bezier(.34,1.56,.64,1),background .15s ease,box-shadow .15s ease,border-color .15s ease;
}
.keys button b{display:block;font-size:17px;font-weight:600;margin-bottom:1px;pointer-events:none}
.keys button small{display:block;color:var(--muted);font-size:10px;pointer-events:none}
.keys button:active{
  transform:scale(.88);
  background:rgba(255,255,255,.22);
  border-color:rgba(255,255,255,.45);
  box-shadow:0 0 15px rgba(255,255,255,.25);
}
.keys button .ripple{
  position:absolute;border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,.6) 0%,rgba(124,155,255,.3) 60%,transparent 100%);
  transform:scale(0);animation:keyRipple .35s ease-out;pointer-events:none;
}
@keyframes keyRipple{
  0%{transform:scale(0);opacity:1}
  100%{transform:scale(2.5);opacity:0}
}
</style>
</head>
<body>
<div class="stage">
  <header>
    <div class="ttl">
      <h1><span class="live"></span>🎤 语音输入</h1>
      <p class="sub">Web 麦克风 → GPU Whisper 转录 → 光标处即时上屏</p>
    </div>
    <button id="corrToggle" type="button" title="LLM 智能纠错(Qwen2.5-0.5B)">✨ 智能纠错 开</button>
  </header>
  <div id="banner" hidden role="alert"></div>
  <div id="status">正在检测环境…</div>
  <div class="modes">
    <button id="mTap" type="button">点击说话</button>
    <button id="mHold" type="button">按住说话</button>
  </div>
  <div id="guide" class="guide">👉 请点击下方【🎙 说话】大按钮，浏览器将弹出系统录音权限确认框，请选择【允许】</div>
  <main class="thumb">
    <div class="orb" id="orb">
      <span class="ring"></span><span class="ring"></span><span class="ring"></span>
      <button id="talk" class="big"><em>🎙</em><small>说话</small></button>
    </div>
  </main>
  <div class="keys" id="keys">
    <button id="kDel1"  type="button" aria-label="删除1字"><b>⌫</b><small>删1字</small></button>
    <button id="kDel2"  type="button" aria-label="删除2字"><b>⌫⌫</b><small>删2字</small></button>
    <button id="kClear" type="button" aria-label="清空输入框"><b>🗑️</b><small>清空</small></button>
    <button id="kEnter" type="button" aria-label="换行"><b>↵</b><small>换行</small></button>
  </div>
</div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
// WSS 与 HTTPS 页面同端口同证书: 直接基于当前页面协议/主机名/端口
const WS = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/stream";
const BIG = $("talk"), ST = $("status"), GUIDE = $("guide"), BAN = $("banner");
const wF = 16000;

let mode = "tap";
let ws = null, ctx = null, stream = null, src = null, proc = null;
let micGranted = false, live = false, connecting = false, pressed = false;
let sendBuf = [], carry = 0, sF = 0, lastTouch = 0;

/* ---------- 状态/引导/告警 ---------- */
function setStatus(t){ ST.textContent = t; }
function buzz(){ if (navigator.vibrate){ try{ navigator.vibrate(30); }catch(_){} } }
function setGuide(t, good){ GUIDE.textContent = t; GUIDE.classList.toggle("good", !!good); }
function showBanner(t){ BAN.textContent = t; BAN.hidden = false; }
function hideBanner(){ BAN.hidden = true; }
function onGranted(){
  micGranted = true;
  hideBanner();
  setGuide("✅ 麦克风已就绪，随时点击开始说话", true);
  BIG.classList.add("granted");
  BIG.innerHTML = "<em>🎙</em><small>说话</small>";
}

/* ---------- 麦克风权限探测与引导 ---------- */
function isSecureCtx(){ return window.isSecureContext !== false; }

function probePermission(){
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !isSecureCtx()){
    showBanner("⚠️ 请务必使用 https:// 打开本页面（当前为非安全上下文，浏览器已禁止麦克风）。\n例如：https://" + location.host + "/");
    setGuide("⚠️ 非安全上下文，麦克风不可用，请改用 https:// 访问");
    setStatus("环境错误：非 https");
    return;
  }
  if (!navigator.permissions || !navigator.permissions.query){
    setGuide("👉 请点击下方【🎙 说话】大按钮，浏览器将弹出系统录音权限确认框，请选择【允许】");
    return;
  }
  navigator.permissions.query({ name: "microphone" }).then((s) => {
    if (s.state === "granted") onGranted();
    else if (s.state === "denied"){
      showBanner("🔴 麦克风权限当前为【已拒绝】。\n请点击地址栏左侧 🔒 锁头 / ⚙️ 网站设置图标 →（网站设置）→ 麦克风 → 改为【允许】，然后刷新页面。");
      setStatus("权限：已拒绝");
    } else {
      setGuide("👉 请点击下方【🎙 说话】大按钮，浏览器将弹出系统录音权限确认框，请选择【允许】");
      setStatus("权限：待授权");
    }
    s.onchange = () => probePermission();
  }).catch(() => {
    setGuide("👉 请点击下方【🎙 说话】大按钮，浏览器将弹出系统录音权限确认框，请选择【允许】");
  });
}

function fmtMicErr(e){
  const n = (e && e.name) || "UNKNOWN";
  switch (n) {
    case "NotAllowedError":
      return "🔴 麦克风权限未授予。\n请点击手机浏览器地址栏左侧的 🔒 锁头 或 ⚙️ 网站设置图标，将【麦克风】改为【允许】，然后刷新页面。\n（微信内：请点右上角 ⋯ → 在浏览器打开；部分机型还需在 系统设置→应用管理→浏览器 中允许麦克风）";
    case "SecurityError":
      return "⚠️ 安全上下文校验失败，请务必使用 https:// 打开本页面（不支持 http）。";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "🔍 未检测到可用麦克风设备。\n请摘掉有线耳机/检查蓝牙麦克风连接，然后刷新重试。";
    case "NotReadableError":
      return "🔇 麦克风正被其他 App 占用（通话/录音中）。退出占用后刷新重试。";
    case "OverconstrainedError":
    case "AbortError":
      return "🔄 麦克风启动异常，请刷新页面重试。";
    default:
      return "❌ 麦克风获取失败（" + n + "）。\n请刷新重试；若持续失败请改用 Chrome、Edge 或系统浏览器打开。";
  }
}

/* ---------- 音频管线（授权后常驻, 空闲挂起省电） ---------- */
function teardownPipeline(){
  if (proc){ try { proc.disconnect(); } catch (_) {} proc = null; }
  if (src){ try { src.disconnect(); } catch (_) {} src = null; }
  if (ctx){ try { ctx.close(); } catch (_) {} ctx = null; }
}
function ensurePipeline(){
  if (ctx && proc && stream) return;
  const AC = window.AudioContext || window.webkitAudioContext;
  ctx = new AC();
  src = ctx.createMediaStreamSource(stream);
  proc = ctx.createScriptProcessor(4096, 1, 1);
  sF = ctx.sampleRate; carry = 0; sendBuf = [];
  proc.onaudioprocess = pump;
  src.connect(proc);
  proc.connect(ctx.destination);
}
function resumePipeline(){
  ensurePipeline();
  // 刷新后 AudioContext 默认为 suspended: 必须在点击手势内显式恢复音频回调
  if (ctx.state === "suspended") ctx.resume().catch(()=>{});
}
function pausePipeline(){ if (ctx && ctx.state === "running") ctx.suspend().catch(()=>{}); }
function pump(e){
  if (!live) return;
  const ch = e.inputBuffer.getChannelData(0);
  const ratio = wF / sF;
  const n = Math.floor(ch.length * ratio + carry);
  carry = (ch.length * ratio + carry) - n;
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const v = ch[Math.min(ch.length - 1, Math.floor(i / ratio))];
    out[i] = Math.max(-1, Math.min(1, v)) * 0x7fff;
  }
  sendBuf.push(out.buffer);
  flush();
}
function flush(){
  if (ws && ws.readyState === 1 && sendBuf.length) {
    while (sendBuf.length) ws.send(sendBuf.shift());
  }
}

/* ---------- WebSocket / 会话控制 ---------- */
function setRecUI(on){
  BIG.classList.remove("granted");
  if (on){
    buzz();
    BIG.classList.add("rec");
    BIG.innerHTML = "<em>⏹</em><small>停止</small>";
    setGuide(mode === "tap" ? "🎙 录音中… 完成后【再点一次】即转录上屏" : "🎙 录音中… 【松开】即转录上屏", true);
  } else {
    buzz();
    BIG.classList.remove("rec");
    if (micGranted) BIG.classList.add("granted");
    BIG.innerHTML = "<em>🎙</em><small>说话</small>";
    if (micGranted) setGuide("✅ 麦克风已就绪，随时点击开始说话", true);
  }
}

function openSocket(){
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  live = true;                    // 立即泵取; ws 未开时先缓冲, 开后再 flush
  resumePipeline();
  ws = new WebSocket(WS);
  ws.binaryType = "arraybuffer";
  ws.onopen  = () => { setRecUI(true); setStatus("录音中…"); };
  ws.onmessage = (m) => {
    if (typeof m.data === "string") {
      try {
        const j = JSON.parse(m.data);
        if (j.status === "busy") {
          showBanner("⚠️ 另一路录音会话进行中（可能 F9 已触发）。\n请先按一次 F9 或在电脑端结束当前录音，再重试。");
          endTalk();
        }
      } catch (_) {}
    }
  };
  ws.onerror = () => {
    showBanner("⚠️ 无法连接服务器 (" + WS + ")。\n请确认容器已启动并信任自签证书，然后刷新页面。");
    try { if (ws) ws.close(); } catch (_) {}
  };
  ws.onclose = () => { ws = null; live = false; setRecUI(false); pausePipeline(); setStatus(""); };
}

async function ensureMicStream(){
  // 每次点击确保存在可用麦克风流: 刷新后旧 stream 可能已失效 -> 重新请求/复用
  if (stream && stream.active) return true;
  teardownPipeline();
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
    });
    micGranted = true;
    onGranted();
    return true;
  } catch (err) {
    micGranted = false;
    showBanner(fmtMicErr(err));
    setStatus("麦克风权限被拒 / 设备异常");
    return false;
  }
}

async function startTalk(){
  if (live || connecting) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { probePermission(); return; }
  hideBanner();
  connecting = true;
  setStatus("正在准备麦克风…");
  if (micGranted && (!stream || !stream.active)){ micGranted = false; teardownPipeline(); }
  if (!micGranted){
    setGuide("系统正在弹出录音权限确认框，请选择【允许】…");
    if (!(await ensureMicStream())){ connecting = false; return; }
  }
  connecting = false;
  // 刷新后 AudioContext 默认 suspended: 点击瞬间显式恢复(在用户手势内)
  resumePipeline();
  // 按住模式: 授权期间用户已松手 -> 本次不启动录音（下次按住再录）
  if (mode === "hold" && !pressed) return;
  openSocket();
}

function endTalk(){
  if (ws && ws.readyState === 1) {
    flush();
    try { ws.send(JSON.stringify({ cmd: "stop" })); } catch (_) {}
    setTimeout(() => { try { ws.close(); } catch (_) {} }, 100);
  } else if (ws) {
    try { ws.close(); } catch (_) {}
  }
  live = false;
  pausePipeline();
  setStatus("");
}

/* ---------- 手势（兼容 iOS / Android / 微信, 100% 用户手势授权） ---------- */
function onPress() { live || connecting ? endTalk() : startTalk(); }

function bindBig(){
  BIG.addEventListener("touchstart", (e) => {
    e.preventDefault();
    lastTouch = Date.now();
    if (mode === "hold"){ pressed = true; onPress(); }
    else onPress();
  }, { passive: false });
  BIG.addEventListener("mousedown", (e) => {
    if (Date.now() - lastTouch < 600) return;   // 吞掉触摸后的幽灵点击
    if (mode === "hold"){ pressed = true; onPress(); }
    else onPress();
  });
}

/* 按住模式: 松开/取消/离开 -> 结束录音 (命名函数便于 setMode 增删监听) */
function holdUpL(e){ e.preventDefault(); pressed = false; lastTouch = Date.now(); if (live) endTalk(); }
function holdCancelL(e){ e.preventDefault(); pressed = false; if (live) endTalk(); }

function setMode(m){
  mode = m;
  $("mTap").classList.toggle("active", m === "tap");
  $("mHold").classList.toggle("active", m === "hold");
  if (m === "hold"){
    BIG.addEventListener("touchend", holdUpL, { passive: false });
    BIG.addEventListener("touchcancel", holdCancelL, { passive: false });
    BIG.addEventListener("mouseup", holdUpL);
    BIG.addEventListener("mouseleave", holdUpL);
  } else {
    BIG.removeEventListener("touchend", holdUpL, { passive: false });
    BIG.removeEventListener("touchcancel", holdCancelL, { passive: false });
    BIG.removeEventListener("mouseup", holdUpL);
    BIG.removeEventListener("mouseleave", holdUpL);
  }
}
$("mTap").onclick  = () => setMode("tap");
$("mHold").onclick = () => setMode("hold");
bindBig();
setMode("tap");

/* ---------- 快捷编辑工具栏: 异步按键分发 (精细震动节奏 + 动态涟漪特效) ---------- */
let lastKeyAt = 0;
function createRipple(btn, e){
  const r = document.createElement("span");
  r.className = "ripple";
  const rect = btn.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  r.style.width = r.style.height = size + "px";
  const clientX = (e && e.touches && e.touches[0] ? e.touches[0].clientX : (e ? e.clientX : rect.left + rect.width / 2));
  const clientY = (e && e.touches && e.touches[0] ? e.touches[0].clientY : (e ? e.clientY : rect.top + rect.height / 2));
  r.style.left = (clientX - rect.left - size / 2) + "px";
  r.style.top = (clientY - rect.top - size / 2) + "px";
  btn.appendChild(r);
  setTimeout(() => { try{ r.remove(); }catch(_){} }, 400);
}

function keyVibrate(action){
  if (!navigator.vibrate) return;
  try {
    if (action === "backspace1") {
      navigator.vibrate(28);                      // 删1字: 清脆短震
    } else if (action === "backspace2") {
      navigator.vibrate([22, 35, 22]);            // 删2字: 动感双震
    } else if (action === "clear") {
      navigator.vibrate([40, 40, 70]);            // 清空: 警示重击感震动
    } else if (action === "enter") {
      navigator.vibrate(45);                      // 换行: 沉稳确认中震
    } else {
      navigator.vibrate(25);
    }
  } catch(_) {}
}

function keyAction(action, btn, e){
  const now = Date.now();
  if (now - lastKeyAt < 160) return;                 // 防误触节流
  lastKeyAt = now;
  if (btn) createRipple(btn, e);                     // 扩散高光波纹特效
  keyVibrate(action);                                // 精准物理震动反馈
  fetch("/key?k=" + encodeURIComponent(action), { cache: "no-store" })
    .then(r => r.json())
    .then(j => { if (!j.ok) setStatus("按键被服务器拒绝: " + j.action); })
    .catch(() => setStatus("按键请求失败"));
}

$("kDel1").onpointerdown  = (e) => { e.preventDefault(); keyAction("backspace1", $("kDel1"), e); };
$("kDel2").onpointerdown  = (e) => { e.preventDefault(); keyAction("backspace2", $("kDel2"), e); };
$("kClear").onpointerdown = (e) => { e.preventDefault(); keyAction("clear", $("kClear"), e); };
$("kEnter").onpointerdown = (e) => { e.preventDefault(); keyAction("enter", $("kEnter"), e); };

/* ---------- LLM 智能纠错开关 (与后端 ENABLE_LLM_CORRECT 同步) ---------- */
let llmCorrect = true;
function updateCorrUI(){
  const on = !!llmCorrect;
  $("corrToggle").classList.toggle("off", !on);
  $("corrToggle").textContent = "✨ 智能纠错 " + (on ? "开" : "关");
}
$("corrToggle").onclick = () => {
  buzz();
  llmCorrect = !llmCorrect;
  updateCorrUI();
  fetch("/corrector?on=" + (llmCorrect ? 1 : 0), { cache: "no-store" })
    .then(r => r.json())
    .then(j => { llmCorrect = !!j.llm_correct; updateCorrUI(); })
    .catch(() => {});
};
updateCorrUI();

async function pollStatus(){
  try {
    const r = await fetch("/status", { cache: "no-store" });
    const j = await r.json();
    setStatus("服务器状态: " + j.state + (j.state === "RECORDING" ? " · 已收 " + j.audio_bytes + " B" : ""));
    if (j.llm_correct !== undefined){ llmCorrect = !!j.llm_correct; updateCorrUI(); }
  } catch (_) { setStatus("服务器离线"); }
}
setInterval(pollStatus, 2000);
pollStatus();

probePermission();

window.addEventListener("pagehide", () => {
  if (stream){ stream.getTracks().forEach(t => t.stop()); stream = null; }
  if (ws){ try { ws.close(); } catch (_) {} ws = null; }
  micGranted = false;      // 回到页面(bfcache)时重新请求权限, 避免陈旧 stream
  live = false;
  pressed = false;
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WhisperAllInOne/1.0"

    def _headers(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _text(self, s, code=200):
        body = s.encode("utf-8")
        self._headers(code, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def _redirect(self, location):
        self._headers(302, "text/plain; charset=utf-8", 0,
                      extra={"Location": location})

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""
        except Exception:
            return b""

    def _do_key(self):
        k = _key_from_request_path(self.path, self._read_body())
        ok, msg = dispatch_key(k)
        self._json({"ok": ok, "action": msg}, 200 if ok else 400)

    def _do_corrector(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        on = (qs.get("on") or [""])[0]
        if not on:
            body = self._read_body()
            if body:
                try:
                    on = str(json.loads(body).get("on", ""))
                except Exception:
                    pass
        if on in ("1", "true", "on"):
            set_corrector_enabled(True)
        elif on in ("0", "false", "off"):
            set_corrector_enabled(False)
        self._json({"ok": True,
                    "llm_correct": bool(CORRECTOR is not None and CORRECTOR.enabled)})

    def do_GET(self):
        mode = getattr(self.server, "mode", "api")
        path = self.path.split("?", 1)[0]
        try:
            if mode == "redirect":
                host = self.headers.get("Host", "localhost").split(":")[0] or "localhost"
                self._redirect(f"https://{host}:{WEB_PORT}/")
            else:  # api (8766 控制口; HTTPS 页面由 websockets 同端口提供)
                if path == "/toggle":
                    toggle_record()
                    self._json(status_payload())
                elif path == "/status":
                    self._json(status_payload())
                elif path == "/healthz":
                    self._text("ok")
                elif path in ("/key", "/api/key"):
                    self._do_key()
                elif path == "/corrector":
                    self._do_corrector()
                elif path == "/":
                    self._text("whisper-all-in-one control API: /toggle /status /healthz /key /corrector")
                else:
                    self._text("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/key", "/api/key"):
                self._do_key()
            elif path == "/corrector":
                self._do_corrector()
            elif path == "/toggle":
                toggle_record()
                self._json(status_payload())
            else:
                self._text("not found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def log_message(self, format, *args):
        pass


class MultiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    mode = "api"                      # page / redirect / api


def http_server(port, mode, ssl_ctx=None):
    srv = MultiHTTPServer(("0.0.0.0", port), Handler)
    srv.mode = mode
    if ssl_ctx is not None:
        srv.socket = ssl_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.5},
        daemon=True, name=f"http-{port}",
    ).start()
    proto = "https" if ssl_ctx else "http"
    print(f"[AllInOne] {proto} 服务监听 :{port} (mode={mode})", flush=True)


# ---------------------------------------------------------------------------
# 信号处理
# ---------------------------------------------------------------------------
def sig_toggle(signum=None, frame=None):
    toggle_record()


def sig_exit(signum=None, frame=None):
    for path in (PID_FILE, STATE_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    print("[AllInOne] 守护进程退出。", flush=True)
    sys.exit(0)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    os.makedirs(IPC_DIR, exist_ok=True)
    ensure_certs()
    ssl_ctx = build_ssl_ctx()

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    write_state("IDLE")

    signal.signal(signal.SIGUSR1, sig_toggle)
    signal.signal(signal.SIGTERM, sig_exit)
    signal.signal(signal.SIGINT, sig_exit)

    # HTTP 28765 跳转 / HTTP 8766 控制 / HTTPS 28768(页面+WSS 同端口)
    http_server(REDIR_PORT, "redirect")
    http_server(CTRL_PORT, "api")

    # WSS(/stream) 与 HTTPS 页面共用 28768, 单端口单证书
    threading.Thread(
        target=lambda: asyncio.run(web_main(ssl_ctx)),
        daemon=True, name="web-wss",
    ).start()

    # TCP 61394
    threading.Thread(target=tcp_main, daemon=True, name="tcp").start()

    ip = detect_lan_ip()
    print(f"[AllInOne] 守护进程就绪 (PID {os.getpid()}).", flush=True)
    print(f"[AllInOne] 手机麦克风页:  https://{ip}:{WEB_PORT}/  "
          f"(页面与 WSS 同端口, 扫码访问并信任自签证书)", flush=True)
    print(f"[AllInOne] 控制 API:      http://127.0.0.1:{CTRL_PORT}/toggle  "
          f"/status /healthz", flush=True)
    print(f"[AllInOne] WSS 推流:      wss://{ip}:{WEB_PORT}/stream  "
          f"(与 HTTPS 同端口, 16kHz PCM s16le mono)", flush=True)
    print(f"[AllInOne] TCP 直推:      {ip}:{TCP_PORT}  "
          f"(原始 PCM 字节流, EOF/STOP 结束)", flush=True)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()