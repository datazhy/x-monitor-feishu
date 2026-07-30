"""解析 TwitterAPI.io Filter Rule 的 webhook 负载。

TwitterAPI.io 的字段命名可能随版本变化，这里做"尽量兼容"的解析：
覆盖 tweets / data / event_data 等常见包裹层与多种字段别名。
若上线后实际字段不同，只需在 _norm_tweet 里补别名即可，主流程不动。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def extract_delivery_id(headers: dict[str, str], raw_body: bytes) -> str:
    """优先用请求头里的 delivery/request id 做幂等键；没有就用 body 内容哈希。"""
    for key in ("x-delivery-id", "x-request-id", "x-webhook-id", "x-twitterapi-delivery"):
        if headers.get(key):
            return headers[key]
    return "sha256:" + hashlib.sha256(raw_body).hexdigest()


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _norm_tweet(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把单条原始 tweet 规整成 db.insert_tweet 需要的字段。"""
    tweet_id = _first(raw, "id", "tweet_id", "id_str", "rest_id")
    if not tweet_id:
        return None
    tweet_id = str(tweet_id)

    author = raw.get("author") or raw.get("user") or {}
    handle = _first(raw, "author_handle", "screen_name", "username") or _first(
        author, "userName", "screen_name", "username", default=""
    )
    name = _first(author, "name", "displayName") or _first(raw, "author_name", "name")
    user_id = _first(author, "id", "id_str", "rest_id") or _first(raw, "author_id", "author_user_id")

    url = _first(raw, "url", "tweet_url", "twitterUrl")
    if not url and handle:
        url = f"https://x.com/{handle}/status/{tweet_id}"

    # 媒体计数：兼容多种结构
    media = raw.get("media") or raw.get("extendedEntities", {}).get("media") if isinstance(
        raw.get("extendedEntities"), dict
    ) else raw.get("media")
    media_count = len(media) if isinstance(media, list) else int(raw.get("media_count", 0) or 0)

    return {
        "tweet_id": tweet_id,
        "author_handle": (handle or "").lstrip("@").lower(),
        "author_name": name,
        "author_user_id": str(user_id) if user_id else None,
        "text": _first(raw, "text", "full_text", "content", default=""),
        "tweet_url": url,
        # TwitterAPI.io: isReply(bool), retweeted_tweet(对象非空=转推), quoted_tweet(对象非空=引用)
        "is_reply": bool(
            _first(raw, "isReply", "is_reply") or raw.get("inReplyToId") or raw.get("in_reply_to_status_id")
        ),
        "is_retweet": bool(
            raw.get("retweeted_tweet") or raw.get("retweetedTweet") or _first(raw, "isRetweet", "is_retweet")
        ),
        "is_quote": bool(
            raw.get("quoted_tweet") or raw.get("quotedTweet") or _first(raw, "isQuote", "is_quote")
        ),
        "media_count": media_count,
        "created_at": _first(raw, "createdAt", "created_at", "date"),
        "source": "webhook",
    }


def parse_tweets(body: bytes) -> list[dict[str, Any]]:
    """从 webhook body 中抽取所有规整后的推文。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    # 找到 tweet 列表所在位置
    candidates: list[Any] = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("tweets", "data", "results", "event_data", "items"):
            v = data.get(key)
            if isinstance(v, list):
                candidates = v
                break
            if isinstance(v, dict) and isinstance(v.get("tweets"), list):
                candidates = v["tweets"]
                break
        if not candidates and ("id" in data or "tweet_id" in data):
            candidates = [data]  # 单条推文直接在顶层

    out: list[dict[str, Any]] = []
    for raw in candidates:
        if isinstance(raw, dict):
            norm = _norm_tweet(raw)
            if norm:
                out.append(norm)
    return out
