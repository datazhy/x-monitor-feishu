"""LLM 客户端：翻译/总结(gpt-4o)、早报预处理(deepseek-v4-flash)、早报写作(gpt-5.4)。

- OpenAI gpt-5.x 系列必须用 max_completion_tokens（不支持 max_tokens）、温度用默认。
- DeepSeek 用 max_tokens + response_format json_object。
失败抛异常，由调用方兜底。
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


def _call(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    token_param: str = "max_tokens",
    max_tokens: int = 800,
    json_mode: bool = False,
    temperature: float | None = 0.3,
    proxy: str = "",
    timeout: float = 120.0,
) -> str:
    if not api_key:
        raise RuntimeError(f"未配置 API key（model={model}）")
    url = base_url.rstrip("/") + "/chat/completions"
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        token_param: max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    kwargs: dict = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    u = data.get("usage") or {}
    from . import db

    db.record_usage(model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return data["choices"][0]["message"]["content"].strip()


# ---- 翻译 / 总结（gpt-4o）----
def translate_to_zh(text: str) -> str:
    s = get_settings()
    return _call(
        base_url=s.openai_base_url, api_key=s.openai_api_key, model=s.openai_model,
        system=_TRANSLATE_SYS, user=text, max_tokens=800, proxy=s.openai_proxy,
    )


def summarize_zh(text: str) -> str:
    s = get_settings()
    return _call(
        base_url=s.openai_base_url, api_key=s.openai_api_key, model=s.openai_model,
        system=_SUMMARY_SYS, user=text, max_tokens=400, proxy=s.openai_proxy,
    )


# ---- 早报预处理（DeepSeek，JSON）----
def deepseek_json(system: str, user: str, max_tokens: int = 8000) -> str:
    s = get_settings()
    return _call(
        base_url=s.deepseek_base_url, api_key=s.deepseek_api_key, model=s.deepseek_model,
        system=system, user=user, token_param="max_tokens", max_tokens=max_tokens,
        json_mode=True, temperature=0.2,
    )


# ---- 早报写作（gpt-5.4，JSON）----
def report_json(system: str, user: str, max_completion_tokens: int = 8000) -> str:
    s = get_settings()
    return _call(
        base_url=s.openai_base_url, api_key=s.openai_api_key, model=s.report_model,
        system=system, user=user, token_param="max_completion_tokens",
        max_tokens=max_completion_tokens, json_mode=True, temperature=None,  # gpt-5 仅支持默认温度
        proxy=s.openai_proxy, timeout=180.0,
    )
