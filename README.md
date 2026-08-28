# 语音输入整合方案（方案 B：All-in-One 纯净容器 · 最终交付）

> **革命性演进**：彻底移除 **AudioRelay 闭源客户端** 与 **PulseAudio 虚拟声卡**。
> 音频从手机经 **HTTPS/WSS 或原生 TCP** 直接进入容器**内存字节缓冲区**，零声卡中转，
> 送入 **RTX 2080 Ti 上的 GPU fastWhisper `large-v3-turbo`（CUDA float16）**，
> 完成后经 **xclip + xdotool** 毫秒级注入当前 X11 窗口光标处。

---

## 一、架构全景

```
┌─ 手机端（三种连接方式，任选其一）─────────────────────────────┐
│  方式 1（零安装首选）                                        │
│    浏览器打开 https://192.168.31.25:28768/                    │
│    Web 麦克风页（点击/按住说话）→ 同端口 WSS(/stream) 推 16kHz PCM       │
│  方式 2（轻量开源）                                          │
│    Otic (F-Droid, 1.6MB) → TCP:61394 直推 PCM                │
│  方式 3（全功能开源）                                        │
│    AMB Android Mic Bridge → TCP:61394（PCM/Opus 双模）        │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─ Docker 容器 whisper-voice-all-in-one ────────────────────────┐
│  内存 bytearray 环形缓冲（零声卡 / 零 PulseAudio / 零 ALSA）    │
│  GPU: large-v3-turbo (CUDA float16) 常驻 22GB 显存             │
│  VAD 人声滤波 + 中英双语提示词, beam=5                          │
│  结果文本 ─► xclip(clipboard+primary) ─► xdotool Shift+Insert  │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
                  [宿主机 X11 活动窗口 — 瞬时上屏, 零焦点丢失]
```

**触发链路（F9 / Web / IPC）**
```
F9 / 桌面图标 / http://192.168.31.25:8766/toggle
        │
        ▼
voice-toggle.sh ─► (快速直发 SIGUSR1) 或 (HTTP 8766) 或 (docker exec 信号) 三重保障
        │
        ▼
容器内守护进程
   第 1 次触发 ──► 开始录音（从 Web/WSS 或 TCP 内存缓冲收流）
   第 2 次触发 ──► 结束录音 → GPU 转录 → X11 光标处上屏
```

---

## 二、方案演进历程

| 阶段 | 方案 | 结果 |
| :--- | :--- | :--- |
| 1-4 | nerd-dictation / Vosk / WO Mic / AudioRelay + PulseAudio 虚拟麦 | 逐一淘汰：离线词库弱、中文空格、麦克风不转发、闭源客户端 |
| 5 | AudioRelay + PulseAudio Null-Sink + GPU Whisper（宿主进程） | 方案成型但依赖闭源 App 与 Pulse 虚拟声卡 |
| 6 | Docker Compose 容器化（挂在 PulseAudio socket 上） | 容器化达成，但仍依赖宿主机 PulseAudio 虚拟麦 |
| **7（本版）** | **All-in-One 纯净容器：Web/WSS/TCP 直推 + 内存缓冲 + GPU Whisper** | **彻底去掉 AudioRelay 与 PulseAudio，宿主机只保留 Docker + X11** |

**核心删除项（宿主机纯净化）**
- AudioRelay 客户端（闭源、依赖其局域网协议）
- PulseAudio 虚拟声卡 `audiorelay-virtual-mic`（`/etc/pulse/default.pa.d/audiorelay.pa` 已移除，`pulseaudio -k` 复位）
- 宿主机声卡/走送链路（手机音频不再“落盘”到任何声卡设备）

---

## 三、文件清单

| 文件路径 | 作用说明 |
| :--- | :--- |
| `/data/voice-input/whisper-all-in-one.py` | **All-in-One 核心守护进程**（HTTPS:28768 手机页 + 同端口 WSS(/stream) + TCP:61394 + HTTP:8766 控制 + :28765 跳转 + IPC/SIGUSR1 + GPU 转录 + X11 注入）。 |
| `/data/voice-input/Dockerfile` | 容器镜像（CUDA 12.4.1 + cuDNN runtime，无 PulseAudio 栈）。 |
| `/data/voice-input/docker-compose.yml` | 编排（host 网络、GPU 直通、X11/模型缓存/IPC/证书挂载）。 |
| `/data/voice-input/manage.sh` | **一键运维脚本**（start/stop/restart/status/logs/toggle/url/certs）。 |
| `/data/voice-input/voice-toggle.sh` | 宿主机 F9 触发脚本（动态 XAUTHORITY + 三重信号保障）。 |
| `/data/voice-input/certs/` | HTTPS/WSS 自签证书（含 IP SAN，容器启动自动校验 IP 变化并重签）。 |
| `/data/voice-input/trigger-server.py` | 旧 Web 触发器（已被容器内 8766 API 取代，停用）。 |
| `/data/voice-input/browser-*.html|py` | 旧浏览器方案（历史保留）。 |
| `/data/voice-input/configs/` | 旧 PulseAudio/systemd 配置（历史保留，不再使用）。 |

---

## 四、手机端三种连接方式（超详细）

> 所有方式的前提：手机与电脑连接**同一局域网**；首次启动容器 `./manage.sh start`。

### 方式 1（零安装 · 首选）：内置 HTTPS Web 麦克风

1. 手机浏览器访问 **`https://192.168.31.25:28768/`**（也可用任意二维码工具给电脑屏幕扫码）。
2. 首次会提示“证书不受信任”：
   - Android（Chrome/Edge）：点「高级」→「继续前往 192.168.31.25（不安全）」；
   - iPhone/iPad：会提示证书校验失败，按系统提示在「设置→通用→关于本机→证书信任设置」信任该自签证书后重进页面。
3. 允许「麦克风」权限（页面会在顶部请求）。
4. 选择模式：**「点击说话」**（点一下开始、再点结束）或**「按住说话」**（按住录、松开结束）。
5. 说完后自动 GPU 转录，文本立即出现在电脑当前光标处。

页面特性：极简现代卡片式 UI、发光录音状态、自动重连、页面轮询实时状态，针对手机触控做了 `touch-action` 与防误触优化。

### 方式 2（轻量开源 App · 首选）：Otic（F-Droid）

- 体积仅 **1.6MB**、**无广告**、**纯开源**。
- 安装：F-Droid 搜索 “Otic”（或 OSS 仓库下载）。
- 配置：目标电脑 **192.168.31.25**，端口 **61394**，采样率 16000（16kHz/16bit/单声道 PCM）。
- 使用：打开 App 点「Start / ▶」，即自动向容器 TCP:61394 推流；录音中再次点击停止、容器完成转录。

### 方式 3（全功能开源 App）：AMB — Android Mic Bridge

- 地址：Google Play / F-Droid 搜索 “Android Mic Bridge”。
- 支持 **Opus / PCM 双模**：推荐选 **RAW PCM（TCP）**，并设置 **16kHz / 16bit / 单声道**（与容器端匹配，识别最准）。
- 主机填 **192.168.31.25**，端口 **61394**，传输选 **TCP**，开启“自动重连”。
- 点「Play」开始，再次点击结束并转录。

> 协议约定（任意 TCP 工具均可用）：连接 :61394 后直接发送 **16kHz、16bit、单声道、小端序** PCM 字节流；断开连接或发送一行 `STOP` 即触发转录。超过 200ms 的静默或过短音频会被自动忽略。

---

## 五、电脑端使用与触发

### 1. F9 快捷键
系统设置 → 键盘 → 自定义快捷键：
- 名称：`voice-input`
- 命令：`/data/voice-input/voice-toggle.sh`
- 快捷键：`F9`

### 2. 桌面图标
```bash
cat > ~/.local/share/applications/voice-input.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=🎤 语音输入
Exec=/data/voice-input/voice-toggle.sh
Terminal=false
Icon=audio-input-microphone
Categories=Utility;
EOF
chmod +x ~/.local/share/applications/voice-input.desktop
```

### 3. Web 触发 API（内部浏览器 / 脚本）
```bash
curl http://192.168.31.25:8766/toggle   # 触发开始/结束（GET, 等效按 F9）
curl http://192.168.31.25:8766/status   # 查询状态 JSON
curl http://127.0.0.1:8766/healthz      # 探活
```

---

## 六、运维命令指南

```bash
cd /data/voice-input
./manage.sh start       # 构建并启动容器, 等待 Whisper 就绪
./manage.sh stop        # 停止容器(数据保留)
./manage.sh restart     # 重启并等待就绪
./manage.sh status      # 一键看板: 容器健康/GPU显存/服务端点/IPC 状态
./manage.sh logs        # 跟随容器日志 (logs 200 只看最近 200 行)
./manage.sh toggle      # 等效按一次 F9
./manage.sh url         # 打印手机/电脑全部访问地址
./manage.sh certs       # 重新生成自签证书(默认探测局域网IP, 可传 IP 参数)
```

Docker Compose 原生命令：
```bash
docker compose -f /data/voice-input/docker-compose.yml up -d --build
docker compose -f /data/voice-input/docker-compose.yml logs -f
docker exec -it whisper-voice-all-in-one bash
```

**端口一览（host 网络）**

| 端口 | 协议 | 用途 |
| :--- | :--- | :--- |
| 28768 | HTTPS+WSS | 手机 Web 麦克风页面 + 同源 /toggle /status + 浏览器 PCM 推流（/stream，同端口同证书） |
| 8766 | HTTP | 控制 API：/toggle /status /healthz |
| 28765 | HTTP | http→https 跳转入口 |
| 61394 | TCP | 原生（Otic/AMB 等）PCM 直推 |

---

## 七、排障指南（FAQ）

| 症状 | 处理 |
| :--- | :--- |
| **手机无法打开 Web 页** | 确认同一局域网；先用 `./manage.sh status` 看 `HTTPS 200`；页面提示证书风险属正常，需“高级→继续访问”；若 IP 变化，执行 `./manage.sh certs` + `restart`（容器启动时也会按当前 IP 自动重签）。 |
| **页面打不开麦克风权限** | 浏览器地址栏选择「网站设置」→ 允许麦克风；**必须走 HTTPS/WSS**（自签证书已含 IP SAN）；检查是否误用 http://28765（它只做跳转）。 |
| **点了说话但一直“连接中”** | 检查 28768 的 WSS 是否可用（`manage.sh status`）；手机与电脑防火墙放行 28768；重新加载页面。 |
| **Otic / AMB 连不上 61394** | `ss -tlnp \| grep 61394` 确认监听；App 内 TCP 模式、IP 与端口正确；防火墙放行 61394。 |
| **识别为空 / 结果乱码** | 采样率务必设为 **16kHz 单声道 16bit**（App 端）；说话时与手机保持 ~30cm；过短音频(<200ms) 会被忽略属正常。 |
| **F9 无响应** | 检查自定义快捷键指向 `/data/voice-input/voice-toggle.sh`；`./manage.sh status` 确认守护进程就绪；容器未运行时 `toggle` 会自动拉起（含首次模型加载等待）。 |
| **能听见/能网页连接但不上屏** | X11 授权 Cookie 失效 → 执行 `./manage.sh restart`（脚本自动重探 `$XAUTHORITY`/GDM Cookie）；确认登录会话为 `:1`。 |
| **显存或慢** | 常驻 ~2.5GB；其他任务占用显存会降低吞吐；重启容器即重新加载模型。 |
| **容器反复重启 / 端口冲突** | 确认 28765/8766/28768/61394 未被宿主机旧进程占用：`ss -tlnp`；旧 `trigger-server.py`/`AudioRelay` 已在迁移时停用。 |
| **健康检查一直 starting** | 首次模型加载约 10~30s，`start_period: 120s` 内转 healthy 均正常；超过则 `./manage.sh logs` 查看报错。 |

---

## 八、关键内部机制（备查）

- **内存缓冲**：`begin_recording()/feed()/begin_stop_and_transcribe()` 原子切换
  IDLE→RECORDING→TRANSCRIBING→IDLE；`started` 守卫保证被拒连接不误杀他方会话；
  转录跑独立 worker 线程，不阻塞 5 个服务器。
- **IPC**：`/tmp/whisper-ipc/whisper-dictation.{pid,state}`（宿主机 ↔ 容器共享），
  `SIGUSR1` 切换，healthcheck 校验 pid 存活 + `/healthz` 探活。
- **X11 注入**：`xclip` 写入 clipboard+primary → `xdotool key --clearmodifiers Shift+Insert`，
  `DISPLAY`/`XAUTHORITY` 动态探测（GDM Cookie 优先）。
- **证书自愈**：容器启动比对当前局域网 IP 与证书 SAN，不一致自动用容器内 `openssl`
  重签（含 IP:当前IP、127.0.0.1、172.18.0.1、DNS:localhost）。
- **触发三重保障**：`voice-toggle.sh` 依次尝试快速直发信号 → HTTP 8766 → 容器内信号。