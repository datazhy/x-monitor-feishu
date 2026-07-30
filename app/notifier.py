"""推送分发层 + 消息格式化（翻译、AI 总结、北京时间、去裸链）。

飞书为一等公民，Apprise 处理未来扩展渠道。主流程只调 notify_tweet/notify_admin。
"""
from __future__ import annotations

import logging
import sqlite3

from . import feishu, llm, textutils
from .config import get_settings

log = logging.getLogger(__name__)

SEPARATOR = "---------------------------"


def _g(t, key):
    return t[key] if isinstance(t, sqlite3.Row) else t.get(key)


def _prepare_body(raw_text: str) -> tuple[str, str]:
    """按翻译规则得到展示正文 + 可选失败提示。返回 (display_text, note)。

    - 含中文：直接展示（去裸链）
    - 不含中文但含英文：翻成简体中文，只展示译文；失败则展示原文 + 失败原因
    - 既无中文也无英文：原样展示
    """
    text = raw_text or ""
    if textutils.has_chinese(text):
        return textutils.strip_urls(text), ""
    if textutils.has_english(text):
        if not get_settings().openai_api_key:
            return textutils.strip_urls(text), ""  # 未配翻译：原文直推，不加提示
        try:
            zh = llm.translate_to_zh(text)
            return textutils.strip_urls(zh), ""
        except Exception as e:
            log.warning("翻译失败: %s", e)
            return textutils.strip_urls(text), f"（自动翻译失败：{e}；以上为原文）"
    return textutils.strip_urls(text), ""


def _ai_summary(raw_text: str) -> str:
    """正文非空白字符数 > 阈值 且配置了 key 时，返回中文 AI 分析；否则/失败返回空。"""
    s = get_settings()
    if not s.openai_api_key:
        return ""
    if textutils.nonspace_len(raw_text) <= s.ai_summary_min_chars:
        return ""
    try:
        return llm.summarize_zh(raw_text)
    except Exception as e:
        log.warning("AI 总结失败: %s", e)
        return ""


def format_tweet(t: sqlite3.Row | dict) -> str:
    name = _g(t, "author_name") or _g(t, "author_handle")
    handle = _g(t, "author_handle")
    raw_text = _g(t, "text") or ""

    display_text, note = _prepare_body(raw_text)
    summary = _ai_summary(raw_text)
    when = textutils.to_beijing(_g(t, "created_at"))
    url = _g(t, "tweet_url") or ""

    lines = [f"{name}（@{handle}）", "", display_text]
    if note:
        lines.append(note)
    if summary:
        lines += ["", f"🤖 AI分析：{summary}"]
    lines += ["", SEPARATOR, when, f"查看原文: {url}"]
    return "\n".join(lines)


def notify_tweet(t: sqlite3.Row | dict) -> None:
    """推送一条推文。按博主路由到对应飞书群（未配置则进主群）。失败抛异常触发重试。"""
    settings = get_settings()
    text = format_tweet(t)

    # 按博主分流：命中 route_map 用专属群，否则用主群
    handle = (_g(t, "author_handle") or "").lstrip("@").lower()
    url, secret = settings.route_map.get(handle, (settings.feishu_webhook_url, settings.feishu_secret))
    if url and "CHANGE_ME" not in url:
        feishu.send_text(url, text, secret)
    else:
        raise RuntimeError("飞书 webhook 未配置")

    _apprise_send(settings.apprise_url_list, title="新推文", body=text)


def notify_admin(text: str) -> None:
    """管理员告警（心跳/dead-letter/余额）。尽力而为，不抛异常。"""
    settings = get_settings()
    body = f"⚠️ [监控告警] {text}"
    try:
        if settings.alert_webhook and "CHANGE_ME" not in settings.alert_webhook:
            feishu.send_text(settings.alert_webhook, body, settings.alert_secret)
    except Exception as e:
        log.error("管理员告警发送失败: %s", e)


def _apprise_send(urls: list[str], title: str, body: str) -> None:
    if not urls:
        return
    try:
        import apprise

        ap = apprise.Apprise()
        for u in urls:
            ap.add(u)
        if not ap.notify(title=title, body=body):
            raise RuntimeError("Apprise notify 返回 False")
    except Exception as e:
        log.warning("Apprise 推送失败: %s", e)
