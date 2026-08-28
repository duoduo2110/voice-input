# Deepwork Progress: Three-Pillar Feature Upgrade & Open Source Polish

## Goals
1. **Pillar 1 (Web 远程快捷控制栏)**：手机端网页新增人体工学辅助按键：退格-1字、退格-2字、全选清空、换行回车，点击直接通过容器调用 X11 发送对应按键，支持触感震动反馈。
2. **Pillar 2 (本地轻量级大模型 LLM ASR 智能纠错引擎 —— “快准狠”)**：利用 RTX 2080 Ti 剩余的 19.5GB 显存，部署轻量高效的 LLM (Qwen2.5-0.5B/1.5B-Instruct)，单次纠错时延 <50ms，自动修正 ASR 语音同音错字、语境语病与专业专有名词，带开关与缓存。
3. **Pillar 3 (开源标准 README.md 全面升级与 GitHub 同步)**：重构顶尖开源项目级别的中英文图文文档，更新一键运维脚本，同步提交并推送到 GitHub 仓库。

---

## Phased Implementation Plan & Oracle Gates

- **Phase 1: 手机 Web 端远程编辑控制栏与后端按键控制开发 (Web Remote Key Control)**
  - Specialist: `@fixer` + `@designer`
  - Scope: 前端新增退格x1、退格x2、清空输入框、换行按钮；后端 `whisper-all-in-one.py` 增加 `/api/key` 与安全按键调度。
  - Oracle Gate 1: 审查按键注入安全性（防命令注入）、X11 焦点窗口兼容性及触控体验。

- **Phase 2: 本地极速轻量大模型 ASR 纠错引擎研发与集成 (Local LLM Post-Correction Engine)**
  - Specialist: `@librarian` (调研与选型) + `@fixer` (实现与 GPU 加速)
  - Scope: 调研并选型最快最准的轻量级模型 (Qwen2.5-0.5B/1.5B float16/int4)，在容器内部集成 GPU 智能纠错流水线，增加前端纠错开关与延迟控制。
  - Oracle Gate 2: 审查模型推理延迟 (<50ms 目标)、GPU 显存安全、纠错准确率与降级容灾机制。

- **Phase 3: 开源 README.md 全量重构、推送 GitHub 与交付验收 (Open Source Polish & Release)**
  - Specialist: `@fixer` + Orchestrator
  - Scope: 重构中英双语图文 README.md，更新 `manage.sh`，全流程端到端冒烟测试，安全推送至 GitHub 远端仓库。
  - Oracle Gate 3: 最终交付审计与全链路验收。

---
## Phase Status
- Phase 1: In Progress
- Phase 2: Pending
- Phase 3: Pending
