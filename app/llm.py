"""LLM 客户端：翻译 / AI 分析 / 早报预处理 / 早报写作。

每类任务可独立选 provider（openai | deepseek），见 .env：
  TRANSLATE_PROVIDER / TRANSLATE_MODEL    翻译 + AI 分析
  REPORT_PROVIDER / REPORT_MODEL          早报写作
  DEEPSEEK_MODEL                          早报预处理（固定 deepseek）

各家差异已在 _resolve 里处理：
  - OpenAI gpt-5.x：必须 max_completion_tokens，且只支持默认温度
  - DeepSeek 推理模型：可用 thinking.type=disabled 关思考（简单任务省钱提速）
"""
from __future__ import annotations

import httpx

from .config import get_settings

_TRANSLATE_SYS = (
    "你是专业翻译。把用户给出的社交媒体推文翻译成简体中文。"
    "严格保留原文的换行、空行和段落结构：原文换行的地方译文也要换行，原文几段译文就几段。"
    "只输出译文本身，不要解释、不要加引号、不要保留原文。"
)
_SUMMARY_SYS = (
    "你是中文金融资讯助理。基于用户给出的推文内容，用简体中文写一段简短分析总结（2-4 句）。"
    "严格只依据推文内容，不要编造价格、数字或事实，不要给出任何买入/卖出/投资建议。"
)


def _resolve(provider: str, model: str) -> dict:
    """把 provider+model 解析成一次请求所需的连接参数与兼容项。"""
    s = get_settings()
    if provider == "deepseek":
        return {
            "base_url": s.deepseek_base_url,
            "api_key": s.deepseek_api_key,
            "model": model or s.deepseek_model,
            "token_param": "max_tokens",
            "proxy": "",
            "temperature": 0.3,
            "no_thinking": s.deepseek_no_thinking,
        }
    m = model or s.openai_model
    is_gpt5 = m.startswith("gpt-5")
    return {
        "base_url": s.openai_base_url,
        "api_key": s.openai_api_key,
        "model": m,
        # gpt-5.x 不支持 max_tokens，且只允许默认温度
        "token_param": "max_completion_tokens" if is_gpt5 else "max_tokens",
        "proxy": s.openai_proxy,
        "temperature": None if is_gpt5 else 0.3,
        "no_thinking": False,
    }


def _call(cfg: dict, system: str, user: str, max_tokens: int, json_mode: bool = False,
          timeout: float = 180.0) -> str:
    if not cfg["api_key"]:
        raise RuntimeError(f"未配置 API key（model={cfg['model']}）")
    body: dict = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        cfg["token_param"]: max_tokens,
    }
    if cfg["temperature"] is not None:
        body["temperature"] = cfg["temperature"]
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if cfg["no_thinking"]:
        body["thinking"] = {"type": "disabled"}

    kwargs: dict = {"timeout": timeout}
    if cfg["proxy"]:
        kwargs["proxy"] = cfg["proxy"]
    with httpx.Client(**kwargs) as client:
        resp = client.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    u = data.get("usage") or {}
    from . import db

    db.record_usage(cfg["model"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return data["choices"][0]["message"]["content"].strip()


# ---- 翻译（简单任务，用便宜模型）----
def translate_to_zh(text: str) -> str:
    s = get_settings()
    cfg = _resolve(s.translate_provider, s.translate_model)
    return _call(cfg, _TRANSLATE_SYS, text, max_tokens=1200, timeout=90.0)


# ---- AI 逐条分析（需理解力，可用更强模型）----
def summarize_zh(text: str) -> str:
    s = get_settings()
    cfg = _resolve(s.analysis_provider, s.analysis_model)
    return _call(cfg, _SUMMARY_SYS, text, max_tokens=800, timeout=120.0)


# ---- 早报预处理（固定 DeepSeek，量大用便宜的 flash）----
def deepseek_json(system: str, user: str, max_tokens: int = 8000) -> str:
    return _call(_resolve("deepseek", ""), system, user, max_tokens, json_mode=True)


# ---- 早报写作 ----
def report_json(system: str, user: str, max_completion_tokens: int = 8000) -> str:
    s = get_settings()
    cfg = _resolve(s.report_provider, s.report_model)
    cfg["no_thinking"] = False  # 早报是高认知任务，保留思考能力
    return _call(cfg, system, user, max_completion_tokens, json_mode=True)
