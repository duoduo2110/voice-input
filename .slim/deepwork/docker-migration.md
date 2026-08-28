# Deepwork Progress: All-in-One Docker Voice Input (Plan B - Direct Audio Ingestion)

## Goal & Architecture (Plan B: 彻底告别 AudioRelay 与 PulseAudio 中间件)
将整套语音输入架构重构为极简高效的 **All-in-One Docker 容器**：
1. **音频直入 (Direct Ingest)**：容器内置轻量级网络音频接收引擎（支持 WebSocket/HTTPS 极简手机端录音推流与局域网 TCP/UDP 字节直通，16kHz/mono PCM），音频流直接写入内存环形缓冲区。
2. **零声卡中转**：彻底告别宿主机/容器内部的 PulseAudio 虚拟声卡 `module-null-sink` 与 Java AudioRelay 客户端，零重采样损失。
3. **GPU 显存常驻加速**：容器内 faster-whisper `large-v3-turbo` 保持常驻 RTX 2080 Ti (CUDA float16)。
4. **X11 穿透粘贴**：通过挂载动态 X11/GDM Cookie，识别结果瞬间打入宿主机光标处。
5. **触发多端一体化**：支持宿主机 `F9` 快捷键、桌面图标、Web 端一键按钮及手机推流页直控。

---

## Phased Implementation Plan & Oracle Gates

- **Phase 1: 方案 B 一体化全栈守护引擎与推流端设计 (Architecture & Ingest Engine)**
  - Specialist: `@fixer`
  - Scope: 编写 `whisper-all-in-one.py`（内置 HTTPS/WSS 麦克风网页服务 + TCP PCM 服务 + GPU Whisper 推理 + X11 注入），更新 `Dockerfile`、`docker-compose.yml` 与 `voice-toggle.sh`。
  - Oracle Gate 1: 审查架构完整性、自签证书麦克风授权机制、内存音频队列设计与 X11 穿透安全性。

- **Phase 2: 容器构建部署与宿主机纯净化清理 (Build & Clean Host Middleware)**
  - Specialist: `@fixer`
  - Scope: 构建镜像，启动新容器，停用并清理宿主机 AudioRelay、旧虚拟麦配置文件 `/etc/pulse/default.pa.d/audiorelay.pa`，恢复宿主机 100% 纯净，联调测试音频直入与转录上屏。
  - Oracle Gate 2: 审查全栈容器运行性能（GPU 占用、转录延迟、内存增长及网络稳定性）。

- **Phase 3: 一键运维控制台与文档验收交付 (Management CLI & Documentation)**
  - Specialist: `@fixer` + Orchestrator
  - Scope: 更新 `/data/voice-input/manage.sh` 看板与 `/data/voice-input/README.md`，完成全链路验收。
  - Oracle Gate 3: 最终验收与交付审计。

---
## Phase Status
- Phase 1: In Progress
- Phase 2: Pending
- Phase 3: Pending
