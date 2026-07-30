"""飞书自定义机器人发送（含签名校验支持）。

Apprise 没有原生飞书插件，飞书自定义机器人需要特定 body 与可选 HMAC 签名，
所以这里实现一个一等公民的发送器；其它渠道交给 Apprise（见 notifier.py）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import httpx


def _gen_sign(secret: str, timestamp: int) -> str:
    """飞书签名算法： HMAC-SHA256(key = "{timestamp}\n{secret}", msg = "") 再 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_text(webhook_url: str, text: str, secret: str = "", timeout: float = 10.0) -> None:
    """发送纯文本消息。失败抛异常（交由上层重试）。"""
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    _post(webhook_url, payload, secret, timeout)


def send_card(webhook_url: str, card: dict, secret: str = "", timeout: float = 10.0) -> None:
    """发送交互卡片消息（msg_type=interactive）。"""
    payload: dict = {"msg_type": "interactive", "card": card}
    _post(webhook_url, payload, secret, timeout)


def _post(webhook_url: str, payload: dict, secret: str, timeout: float) -> None:
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _gen_sign(secret, ts)
    resp = httpx.post(webhook_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    code = data.get("code", data.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"Feishu API error code={code}: {data.get('msg') or data}")
