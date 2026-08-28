#!/usr/bin/env python3
"""
corrector.py — 本地轻量 LLM ASR 智能纠错引擎 (Qwen2.5-0.5B-Instruct)
================================================================================
- transformers + AutoModelForCausalLM, CUDA float16, 常驻显存 ~1GB
- 超精简纠错 System Prompt + Few-Shot 对齐; 目标单次纠错 15~40ms
- 防幻觉 / 防过度纠错过滤: 空输出、长度膨胀 >1.4x、废话词、无字符重叠时
  自动回退到 Whisper 原始 ASR 文本
- 纯英文/数字短句(无同音歧义)直接跳过, 保持极低时延

 环境变量:
  ENABLE_LLM_CORRECT  默认 false; 设为 true 才启用 (以 Whisper 原文为基准，默认零改写)
  LLM_CORRECT_MODEL   默认 Qwen/Qwen2.5-0.5B-Instruct
"""

import os
import re
import threading
import time

SYSTEM_PROMPT = (
    "严格逐字校对ASR同音错别字。规则："
    "1.绝对禁止概括、缩写、润色或删除口语词；"
    "2.字数与原句必须严格一致；"
    "3.只改同音错字，原句正确的字绝对不动；"
    "4.直接输出全文。"
)

# 模型一出现这些词(且原文不包含)即视为“解释/废话” -> 回退
JUNK_WORDS = (
    "解释", "原因", "正确应为", "正确的应该是", "应该为", "以下是",
    "修正为", "纠正后", "原始语音转写", "注：", "注意：", "输出：",
    "抱歉", "对不起", "补充", "综上所述",
)


class ASRCorrector:
    """Qwen 智能纠错单例 (支持 0.5B / 1.5B 等尺寸，带优雅回退)。"""

    def __init__(self):
        self.env_default = os.environ.get("ENABLE_LLM_CORRECT", "false").lower() == "true"
        self.enabled = self.env_default
        self.primary = os.environ.get("LLM_CORRECT_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
        self.fallback = os.environ.get("LLM_FALLBACK_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
        self.model_name = self.primary
        self.model = None          # HF CLM (cuda fp16)
        self.tokenizer = None
        self.torch = None
        self.lock = threading.Lock()
        if self.enabled:
            self._load()

    # ------------------------------------------------------------------ 加载
    def _load(self):
        try:
            import torch  # 延迟导入: 禁用时不占显存/内存
            from transformers import AutoModelForCausalLM, AutoTokenizer
            self.torch = torch

            candidates = [self.primary]
            if self.fallback and self.fallback != self.primary:
                candidates.append(self.fallback)

            for target in candidates:
                try:
                    print(f"[Corrector] 加载 {target} (CUDA float16) ...", flush=True)
                    t0 = time.perf_counter()
                    self.tokenizer = AutoTokenizer.from_pretrained(target, trust_remote_code=True)
                    self.model = AutoModelForCausalLM.from_pretrained(
                        target,
                        torch_dtype=torch.float16,
                        device_map="cuda",
                        low_cpu_mem_usage=True,
                        trust_remote_code=True
                    )
                    self.model.eval()
                    self.model_name = target
                    print(f"[Corrector] 模型 {target} 加载完成 ({time.perf_counter()-t0:.1f}s), 预热中...", flush=True)
                    self.correct("我想吃平果")          # 预热 CUDA kernel / CUDA graph
                    print(f"[Corrector] {target} 预热完成, 常驻就绪", flush=True)
                    return
                except Exception as e:
                    print(f"[Corrector] 加载 {target} 异常: {e}", flush=True)
                    if 'torch' in locals() and hasattr(torch.cuda, 'empty_cache'):
                        torch.cuda.empty_cache()

            print("[Corrector] 全部候选模型加载失败，引擎关闭，回退原始 ASR 文本", flush=True)
            self.enabled = False
            self.model = None
        except Exception as e:
            print(f"[Corrector] 引擎初始化失败: {e}", flush=True)
            self.enabled = False
            self.model = None

    def set_enabled(self, on):
        """运行时开关: 开启且未加载则按需加载; 返回实际生效状态。"""
        if on and self.model is None:
            self._load()
        self.enabled = bool(on and self.model is not None)
        return self.enabled

    # ------------------------------------------------------------- 过滤规则
    @staticmethod
    def skip_if_unambiguous(text):
        """纯英文/数字/标点(不含中文、无同音歧义) -> True 跳过纠错。"""
        t = (text or "").strip()
        if not t:
            return True
        return not re.search(r"[\u4e00-\u9fff]", t)

    @staticmethod
    def _levenshtein(a, b):
        """纯 Python Levenshtein，O(n*m) 双行优化，适配 200 字内短句熔断。"""
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        # 确保 b 为较短者以节省内存
        if len(a) < len(b):
            a, b = b, a
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i] + [0] * len(b)
            for j, cb in enumerate(b, 1):
                cost = 0 if ca == cb else 1
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            prev = cur
        return prev[len(b)]

    @staticmethod
    def accept(orig, cand):
        """极严字级护栏：长度±10% / Levenshtein>15% / 废话词 / 零重叠 -> 100%回退原文。
        返回 (ok, cleaned_cand)。"""
        c = (cand or "").strip().strip("\"'“”‘’ ")
        c = re.sub(r"^(更正|纠错|改为|应该是|正确为)[:：]?\s*", "", c)
        if not c:
            return False, ""
        o = (orig or "").strip()
        if not o:
            return False, ""
        lo, lc = len(o), len(c)
        # 1) 字数严格对齐硬护栏：±10% 外一律丢弃（模型擅自总结/删口语词）
        if abs(lc - lo) > lo * 0.10 + 1e-9:
            return False, c
        # 2) Levenshtein 熔断：改动>15% 视为过度改写/润色（短句至少容忍1字同音错，向上取整避免5字改1字被误熔）
        dist = ASRCorrector._levenshtein(o, c)
        thresh = max(1.0, lo * 0.15)
        if dist > thresh + 1e-9:
            return False, c
        for junk in JUNK_WORDS:
            if junk in c and junk not in o:
                return False, c
        if "你好" in c and "你好" not in o:                  # “你好”式废话
            return False, c
        # 字符重叠护栏: 与原文字母/数字/汉字零重叠 -> 幻觉
        if not any(ch in c for ch in o if ch.isalnum()):
            return False, c
        return True, c

    # ------------------------------------------------------------- 主入口
    def correct(self, text):
        """对单条 ASR 转写做智能纠错; 任一过滤规则不满足时原样返回。"""
        if self.model is None or not self.enabled:
            return text
        if ASRCorrector.skip_if_unambiguous(text):
            return text
        torch = self.torch
        with self.lock:
            if self.model is None or not self.enabled:
                return text
            try:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"原始语音转写：{text}"},
                ]
                prompt = self.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True)
                inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
                t0 = time.perf_counter()
                with torch.no_grad():
                    gen = self.model.generate(
                        inputs.input_ids,
                        attention_mask=inputs.attention_mask,
                        max_new_tokens=48,
                        do_sample=False,
                        repetition_penalty=1.1,
                        pad_token_id=self.tokenizer.eos_token_id,
                    )
                dt = (time.perf_counter() - t0) * 1000
                cand = self.tokenizer.decode(
                    gen[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
                ok, cand = ASRCorrector.accept(text, cand)
                if not ok:
                    print(f"[Corrector] 过滤回退 {text!r} -> {cand!r} ({dt:.1f}ms)", flush=True)
                    return text
                if cand != (text or "").strip():
                    print(f"[Corrector] 纠错 {text!r} -> {cand!r} ({dt:.1f}ms)", flush=True)
                return cand or text
            except Exception as e:
                print(f"[Corrector] 推理异常, 回退原始文本: {e}", flush=True)
                return text


# ----------------------------------------------------------------------------
# CLI 自检: python3 corrector.py "我想吃平果" "在次尝试" "Python语言"
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    eng = ASRCorrector()
    samples = sys.argv[1:] or ["我想吃平果", "在次尝试", "Python语言", "Hello 123"]
    worst = 0.0
    for s in samples:
        t0 = time.perf_counter()
        out = eng.correct(s)
        dt = (time.perf_counter() - t0) * 1000
        worst = max(worst, dt)
        print(f"{s!r} -> {out!r}   [{dt:.1f}ms]")
    print(f"worst single-call latency: {worst:.1f}ms")