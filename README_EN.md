# 🎙️ Whisper Voice Input (All-in-One GPU Whisper + LLM ASR Post-Correction)

<div align="center">

![GitHub License](https://img.shields.io/github/license/duoduo2110/voice-input?color=blue)
![Docker Supported](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![CUDA 12](https://img.shields.io/badge/CUDA-12.4-76B900?logo=nvidia&logoColor=white)
![Whisper](https://img.shields.io/badge/Model-Whisper%20Large--v3--turbo-orange)
![LLM Correction](https://img.shields.io/badge/LLM%20ASR%20Correction-Qwen2.5--0.5B-green)

**Pure, High-Performance, Zero-Virtual-Audio-Card GPU Voice Input System**  
*No proprietary desktop clients or virtual audio cables required. Mobile browser scan-to-use / Open-source App direct streaming, sub-second typing straight to your active cursor!*

[English](./README_EN.md) | [简体中文](./README.md)

</div>

---

## 🌟 Key Features

- 🚀 **Zero Virtual Audio Card Overhead**: Audio streams directly into in-memory ring buffer over network. **Completely eliminated PulseAudio null-sink and Java AudioRelay desktop client**.
- ⚡ **Sub-second GPU Inference**: Powered by `faster-whisper` **`large-v3-turbo`** resident in GPU VRAM (CUDA float16), single speech transcription takes only **~100ms**.
- ✨ **Local LLM ASR Post-Correction**: Embedded resident **`Qwen2.5-0.5B-Instruct` (CUDA fp16)** model automatically fixes homophone typos and domain jargon in real time (~20ms).
- 📱 **Zero-Install Mobile Web Mic**: Single-port secure architecture (HTTPS + WSS on the same port), thumb-ergonomic giant button with **Tap-to-Talk** and **Push-to-Talk** modes.
- 🎮 **Mobile Remote Control Toolbar + Haptic Feedback**: Mobile page includes **`⌫ Del 1`**, **`⌫⌫ Del 2`**, **`🗑️ Clear`**, and **`↵ Enter`** buttons with differentiated physical vibration patterns and ripple visual effects.
- 💻 **Smart Window-Aware Auto-Paste**: Window-type auto-detection (sends `Alt+V` in Terminals, `Ctrl+V` in Chrome/VS Code/GUI apps) with **zero X11 freeze and zero focus stealing**.
- 🔒 **Open Source Ecosystem Ready**: Built-in **TCP `61394`** raw PCM ingest port, compatible with **Otic (F-Droid 1.6MB)** and **AMB (Android Mic Bridge)**.

---

## 🏗️ Architecture

```
[ Mobile Devices (Phone / Tablet) ]
  ├── 🥇 Method 1: Built-in HTTPS Web Mic (Scan & Open: https://<PC_IP>:28768/)
  ├── 🥈 Method 2: Lightweight Open-Source App Otic (F-Droid 1.6MB: tcp://<PC_IP>:61394)
  └── 🥉 Method 3: Feature-Rich App AMB (WiFi/USB Debug Cable: tcp://<PC_IP>:61394)
                             │
                             ▼ (Raw 16kHz PCM stream over WiFi / TCP / WSS)
┌─────────────────────────────────────────────────────────────────────────────┐
│  Docker Container: whisper-voice-all-in-one                                 │
│                                                                             │
│  [ Lightweight Network Ingest Engine ] (WSS:28768 / TCP:61394 / HTTP:8766)   │
│                  │                                                          │
│                  ▼ (Direct in-memory bytearray buffer, zero audio loss)      │
│  [ Whisper large-v3-turbo ] (CUDA float16 flagship ASR, ~100ms inference)   │
│                  │                                                          │
│                  ▼ (Raw ASR Text Stream)                                    │
│  [ Qwen2.5-0.5B-Instruct ] (Local LLM Post-Correction, ~20ms refinement)    │
│                  │                                                          │
│                  ▼ (Clean Corrected Text)                                   │
│  [ Smart X11 Typing Controller ] (Terminal Alt+V / GUI Ctrl+V)              │
└─────────────────────────────────────────────────────────────────────────────┘
                             │
                             ▼ (Shift+Insert / Ctrl+V / Alt+V instant paste)
[ Host Active Window Cursor (Chrome, VS Code, Terminal, Chat Apps, etc.) ]
```

---

## 🚀 Quick Start

### 1. Prerequisites
- **OS**: Linux with X11 desktop session (Ubuntu 20.04/22.04/24.04, Debian, Arch, etc.)
- **GPU**: NVIDIA dedicated GPU with ≥4GB VRAM (RTX 2060 / 2080 / 3060 / 4060 and above)
- **Docker**: Docker & NVIDIA Container Toolkit (`--gpus all` supported)

### 2. Clone & Launch
```bash
git clone https://github.com/duoduo2110/voice-input.git
cd voice-input

./manage.sh start
```

### 3. Check Status
```bash
./manage.sh status
```

---

## 📱 Mobile Connection Options

### 🥇 Method 1: Built-in Web Microphone (Recommended ⭐⭐⭐⭐⭐)
1. Connect your phone to the same local Wi-Fi.
2. Open phone browser and navigate to:
   ```text
   https://<YOUR_PC_LAN_IP>:28768/
   ```
   *(Run `./manage.sh url` to print the exact link)*
3. Accept self-signed certificate, tap the giant **【🎙 Speak】** button to grant microphone permission.
4. Speak normally and tap again to finish. Text will be typed instantly onto your PC cursor!

---

### 🥈 Method 2: Lightweight Open-Source App `Otic` (F-Droid ⭐⭐⭐⭐)
- Size: **1.6MB**, F-Droid open-source, battery-friendly.
- Set target IP to your PC LAN IP, port to **`61394`**, and tap **Start**.

---

### 🥉 Method 3: Android Mic Bridge `AMB` (⭐⭐⭐⭐)
- Supports Wi-Fi and USB cable streaming. Select `PCM (16000Hz)` and connect to port `61394`.

---

## 🛠️ Management CLI (`manage.sh`)

```bash
./manage.sh status     # View full dashboard (health, VRAM, ports, LLM status)
./manage.sh url        # Print mobile and PC connection URLs
./manage.sh logs       # Live stream Whisper / LLM logs
./manage.sh restart    # Restart service container
./manage.sh stop       # Stop service container
./manage.sh start      # Build and start container
./manage.sh certs      # Regenerate SSL certificate with current LAN IP
```

---

## 📄 License

This project is licensed under the [MIT License](./LICENSE).
