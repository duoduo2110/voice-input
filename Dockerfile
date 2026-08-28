# syntax=docker/dockerfile:1
#
# Whisper All-in-One 容器 — 方案 B (零 AudioRelay / 零 PulseAudio)
# ----------------------------------------------------------------------------
# 基线: CUDA 12.4.1 + cuDNN runtime (Ubuntu 22.04)。
# 音频全部走内存字节流 (WebSocket/TCP -> bytearray), 不再安装/配置声卡与
# PulseAudio 栈; 仅保留 X11 注入 (xclip/xdotool) 与 GPU 推理依赖。
#
# 构建: docker compose -f /data/voice-input/docker-compose.yml up -d --build
# ----------------------------------------------------------------------------
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

# 让动态链接优先命中 pip nvidia-* wheel 自带的 cuBLAS/cuDNN
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib

# 系统依赖 (无 PulseAudio 栈):
#   - X11 注入: xdotool / xclip / libx11-6 / libxext6
#   - 运行支撑: libgomp1 (ctranslate2 OpenMP)、libportaudio2 (sounddevice
#               PortAudio 运行时)、openssl (证书自愈)、curl (健康检查)
#   - 编译支撑: gcc / g++ / build-essential / python3-dev (Triton / PyTorch JIT 必需)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        gcc \
        g++ \
        build-essential \
        xdotool \
        xclip \
        libx11-6 \
        libxext6 \
        libgomp1 \
        libportaudio2 \
        openssl \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 包: faster-whisper + WSS 服务 + 音频/数值 + CUDA 加速库
# + 本地 LLM 智能纠错 (transformers + torch, CUDA 12.x 捆绑)
RUN pip3 install --no-cache-dir \
        faster-whisper \
        websockets \
        sounddevice \
        numpy \
        nvidia-cublas-cu12 \
        nvidia-cudnn-cu12 \
        torch \
        transformers \
        accelerate \
        safetensors

WORKDIR /app

# All-in-One 守护进程 (HTTPS 28768 / WSS 同端口 / HTTP 8766 / TCP 61394 / 跳转 28765)
COPY whisper-all-in-one.py /app/whisper-all-in-one.py
# LLM ASR 智能纠错引擎 (Qwen2.5-0.5B-Instruct, CUDA fp16)
COPY corrector.py /app/corrector.py

CMD ["python3", "/app/whisper-all-in-one.py"]