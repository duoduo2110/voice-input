# 🎙️ Whisper Voice Input (All-in-One GPU Whisper + LLM ASR Post-Correction)

<div align="center">

![GitHub License](https://img.shields.io/github/license/duoduo2110/voice-input?color=blue)
![Docker Supported](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![CUDA 12](https://img.shields.io/badge/CUDA-12.4-76B900?logo=nvidia&logoColor=white)
![Whisper](https://img.shields.io/badge/Model-Whisper%20Large--v3--turbo-orange)
![LLM Correction](https://img.shields.io/badge/LLM%20ASR%20Correction-Qwen2.5--0.5B-green)

**纯净高效、零声卡中转、GPU 加速的局域网/异地全栈语音输入系统**  
*无需在电脑安装闭源客户端与虚拟声卡，手机浏览器扫码即用 / 开源 App 直连，说话即刻毫秒级贴入光标处！*

[简体中文](./README.md) | [English](./README_EN.md)

</div>

---

## 🌟 核心特性 (Key Features)

- 🚀 **零声卡中转 (Zero Virtual Audio Card)**：手机推流音频直接通过网络写入容器内存环形缓冲区，**彻底摒弃 PulseAudio 虚拟麦与 Java AudioRelay 客户端**，零落盘、零声卡重采样损耗。
- ⚡ **毫秒级 GPU 推理 (Sub-second Inference)**：基于 `faster-whisper` 的 **`large-v3-turbo`** 旗舰大模型常驻在 NVIDIA 显卡显存中 (CUDA float16)，单次语音推理仅需 **~100ms**。
- ✨ **本地 LLM 智能纠错 (Fast & Accurate Post-Correction)**：内置常驻轻量大语言模型 **`Qwen2.5-0.5B-Instruct` (CUDA fp16)**，针对 ASR 常见的同音错别字、语境别字进行毫秒级二次润色修正，支持一键热开关。
- 📱 **手机端免安装 Web 麦克风**：单端口同源安全架构（HTTPS + WSS 同端口），手机自带浏览器直接访问即可调用麦克风，支持 **大拇指黄金触控巨钮** 与 **「点击说话 / 按住说话」** 两种模式。
- 🎮 **手机端远程快捷编辑栏 + 物理震动反馈**：手机页面内置 **`⌫ 删1字`、`⌫⌫ 删2字`、`🗑️ 清空`、`↵ 换行`** 快捷控制栏，配合高光涟漪微动效与差异化物理马达触感震动反馈。
- 💻 **智能窗口感知打字 (Smart X11 Auto-Paste)**：基于 `xclip` + `xdotool` 瞬时注入，自动感知目标窗口类型（终端自动发 `Alt+V`，Chrome/VS Code/桌面软件自动发 `Ctrl+V`），**绝不卡死 X11、不丢失窗口焦点**。
- 🔒 **全开源生态兼容**：内置 **TCP `61394`** 原始 PCM 直连接口，完美兼容 **Otic (F-Droid 1.6MB)**、**AMB (Android Mic Bridge)** 等轻量开源推流 App。

---

## 🏗️ 架构拓扑 (Architecture)

```
[ 手机端 (Mobile Devices) ]
  ├── 🥇 方式 1: 内置 HTTPS Web 麦克风 (浏览器扫码直连: https://<PC_IP>:28768/)
  ├── 🥈 方式 2: 轻量开源 App Otic (F-Droid 1.6MB 极简直推: tcp://<PC_IP>:61394)
  └── 🥉 方式 3: 全功能开源 App AMB (WiFi/USB 调试线直推: tcp://<PC_IP>:61394)
                             │
                             ▼ (局域网 WiFi / TCP / WSS 原始 16kHz PCM 字节流)
┌─────────────────────────────────────────────────────────────────────────────┐
│  Docker 容器: whisper-voice-all-in-one                                      │
│                                                                             │
│  [ 轻量网络音频接收 Ingest 引擎 ] (WSS:28768 / TCP:61394 / HTTP:8766)        │
│                  │                                                          │
│                  ▼ (直接写入内存环形缓冲区 bytearray，零声卡损耗)             │
│  [ Whisper large-v3-turbo ] (CUDA float16 旗舰语音识别，~100ms 转录)         │
│                  │                                                          │
│                  ▼ (原始 ASR 文本流)                                         │
│  [ Qwen2.5-0.5B-Instruct ] (本地轻量大模型 ASR 智能纠错，~20ms 润色同音字)   │
│                  │                                                          │
│                  ▼ (纯净纠错文本)                                           │
│  [ 智能 X11 粘贴控制器 ] (自动判断活动窗口类型: 终端 Alt+V / GUI Ctrl+V)     │
└─────────────────────────────────────────────────────────────────────────────┘
                             │
                             ▼ (Shift+Insert / Ctrl+V / Alt+V 毫秒级上屏)
[ 宿主机当前活动输入框 (Chrome、VS Code、Terminal、聊天软件等) ]
```

---

## 🚀 极速起步 (Quick Start)

### 1. 环境要求
- **操作系统**：Linux (Ubuntu 20.04/22.04/24.04 等运行 X11 桌面环境)
- **硬件**：NVIDIA 独立显卡 (推荐显存 ≥ 4GB，如 RTX 2060 / 2080 / 3060 / 4060 及以上)
- **软件**：已安装 Docker 与 NVIDIA Container Toolkit (支持 `--gpus all`)

### 2. 一键克隆与启动
```bash
# 克隆本仓库
git clone https://github.com/duoduo2110/voice-input.git
cd voice-input

# 使用一键管理脚本构建并启动
./manage.sh start
```

### 3. 查看状态看板
```bash
./manage.sh status
```
*状态显示 `✔ whisper-voice-all-in-one 运行中 (health: healthy)` 即代表系统已完全就绪！*

---

## 📱 手机端连接与使用方式

### 🥇 方式 1：内置 HTTPS Web 麦克风（零安装，推荐 ⭐⭐⭐⭐⭐）
1. 手机连接与电脑相同的局域网 WiFi。
2. 手机自带浏览器打开：
   ```text
   https://<你的电脑局域网IP>:28768/
   ```
   *(可运行 `./manage.sh url` 查看精确地址)*
3. **首次访问**：点击“高级 / 详细信息” ➔ “继续前往（不安全）”（使用自签证书）。
4. **开始说话**：
   - 页面打开后，点击屏幕中央的 **【🎙 说话】巨型大按钮** 允许麦克风权限。
   - 对着手机正常说话，说完**再点一次**，文字立刻自动打在电脑光标处！
   - 点击下方的 **`⌫ 删1字`、`⌫⌫ 删2字`、`🗑️ 清空`、`↵ 换行`** 即可远程控制电脑输入框，带物理震动反馈。

---

### 🥈 方式 2：轻量开源 App `Otic`（F-Droid ⭐⭐⭐⭐）
- **特点**：体积仅 **1.6MB**，纯开源（MIT License），无广告、零云端，手机后台常驻省电。
- **使用方法**：
  1. 在 **F-Droid** 应用商店搜索安装 `Otic`。
  2. 打开 App，目标 IP 填电脑局域网 IP，端口填 **`61394`**。
  3. 点击 **Start** 即可直接向 Docker 容器推送原始 16kHz PCM 语音。

---

### 🥉 方式 3：全功能开源 App `AMB`（Android Mic Bridge ⭐⭐⭐⭐）
- **特点**：支持 WiFi 和 USB 调试线（ADB）推流。
- **使用方法**：手机安装 AMB，音频模式选择 `PCM (16000Hz)`，连接电脑 IP 与端口 `61394` 即可。

---

## 💻 电脑端日常操作与触发

- **键盘快捷键**：在任意输入框按 **`F9`** 开始录音，说完再按 **`F9`** 结束并上屏。
- **桌面图标**：双击桌面生成的 **`🎤 语音输入`** 图标。
- **Web API 触发**：在任意脚本或浏览器中请求 `http://127.0.0.1:8766/toggle`。

---

## 🛠️ 一键运维命令参考 (`manage.sh`)

```bash
./manage.sh status     # 查看综合运行看板 (容器状态、GPU 显存、各端点监听、纠错开关)
./manage.sh url        # 打印所有手机端与电脑端连接 URL
./manage.sh logs       # 实时跟踪 Whisper / LLM 转录日志
./manage.sh restart    # 重启语音识别服务容器
./manage.sh stop       # 停止语音识别容器
./manage.sh start      # 构建并拉起容器服务
./manage.sh certs      # 重新生成包含当前局域网 IP 的自签证书
```

---

## ⚙️ 核心配置参数 (Environment Variables)

在 `docker-compose.yml` 或 `.env` 中可自由配置：

| 环境变量 | 默认值 | 作用说明 |
| :--- | :---: | :--- |
| `ENABLE_LLM_CORRECT` | `true` | 是否启用本地 Qwen2.5-0.5B 智能 ASR 纠错引擎（设为 `false` 可完全关闭） |
| `LLM_CORRECT_MODEL` | `Qwen/Qwen2.5-0.5B-Instruct` | 智能纠错使用的 HuggingFace 模型名称或本地路径 |
| `WEB_PORT` | `28768` | 手机端 Web 麦克风网页与 WSS 实时流服务端口 (HTTPS + WSS 同端口) |
| `CTRL_PORT` | `8766` | 宿主机/脚本调用的轻量控制 API 端口 (`/toggle`, `/status`, `/key`) |
| `TCP_PORT` | `61394` | 供第三方开源 App (Otic / AMB) 直推原始 PCM 的 TCP 端口 |
| `TERMINAL_PASTE_KEY` | `alt+v` | 终端窗口使用的粘贴快捷键组合（可根据个人终端配置覆盖） |
| `GUI_PASTE_KEY` | `ctrl+v` | Chrome、VS Code 等通用 GUI 应用使用的标准粘贴快捷键组合 |

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](./LICENSE) 协议开源。欢迎 Star 与 Issue / PR 交流贡献！
