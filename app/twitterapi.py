"""TwitterAPI.io 客户端：规则管理、博主信息、最近推文（补漏用）。

注意：TwitterAPI.io 的具体 endpoint 路径可能随版本调整。下面的路径集中在
本文件顶部常量，便于上线时按官方文档一处改全。所有方法都做了容错。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

# ---- endpoint 路径（按官方文档需要时在此调整）----
EP_RULE_LIST = "/oapi/tweet_filter/get_rules"
EP_RULE_ADD = "/oapi/tweet_filter/add_rule"
EP_RULE_UPDATE = "/oapi/tweet_filter/update_rule"
EP_RULE_DELETE = "/oapi/tweet_filter/delete_rule"
EP_USER_INFO = "/twitter/user/info"
EP_USER_LAST_TWEETS = "/twitter/user/last_tweets"
EP_ACCOUNT_INFO = "/oapi/my/info"  # 余额/用量


class TwitterAPIClient:
    def __init__(self) -> None:
        s = get_settings()
        self.base = s.twitterapi_base_url.rstrip("/")
        self.key = s.twitterapi_key
        self._client = httpx.Client(
            base_url=self.base,
            headers={"X-API-Key": self.key},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        r = self._client.get(path, params=params or {})
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict[str, Any]:
        r = self._client.post(path, json=payload)
        r.raise_for_status()
        return r.json()

    # ---- 规则 ----
    def list_rules(self) -> list[dict]:
        try:
            data = self._get(EP_RULE_LIST)
            return data.get("rules") or data.get("data") or []
        except Exception as e:
            log.warning("list_rules 失败: %s", e)
            return []

    def add_rule(self, value: str, interval_seconds: int, webhook_url: str, tag: str = "") -> str | None:
        # 官方 add_rule 仅认 tag/value/interval_seconds；多传 webhook_url 无害（部分版本支持）
        payload = {
            "tag": tag,
            "value": value,
            "interval_seconds": interval_seconds,
            "webhook_url": webhook_url,
        }
        data = self._post(EP_RULE_ADD, payload)
        return str(data.get("rule_id") or data.get("id") or "") or None

    def update_rule(
        self,
        rule_id: str,
        tag: str,
        value: str,
        interval_seconds: int,
        is_effect: bool,
        webhook_url: str = "",
    ) -> dict:
        # is_effect=1 激活规则；带上 webhook_url 尝试用 API 绑定回调（不支持则被忽略）
        payload = {
            "rule_id": rule_id,
            "tag": tag,
            "value": value,
            "interval_seconds": interval_seconds,
            "is_effect": int(is_effect),
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url
        return self._post(EP_RULE_UPDATE, payload)

    def delete_rule(self, rule_id: str) -> None:
        # 官方 delete 用 HTTP DELETE + JSON body
        r = self._client.request("DELETE", EP_RULE_DELETE, json={"rule_id": rule_id})
        r.raise_for_status()

    # ---- 博主 ----
    def get_user(self, handle: str) -> dict | None:
        try:
            data = self._get(EP_USER_INFO, params={"userName": handle})
            return data.get("data") or data
        except Exception as e:
            log.warning("get_user(%s) 失败: %s", handle, e)
            return None

    def last_tweets(self, handle: str, count: int = 20) -> list[dict]:
        try:
            data = self._get(EP_USER_LAST_TWEETS, params={"userName": handle, "count": count})
            inner = data.get("data") or data
            tweets = inner.get("tweets") if isinstance(inner, dict) else inner
            return tweets or []
        except Exception as e:
            log.warning("last_tweets(%s) 失败: %s", handle, e)
            return []

    # ---- 账户/余额 ----
    def account_info(self) -> dict:
        try:
            data = self._get(EP_ACCOUNT_INFO)
            return data.get("data") or data
        except Exception as e:
            log.warning("account_info 失败: %s", e)
            return {}

    def balance(self) -> float | None:
        """返回剩余 credits。"""
        info = self.account_info()
        for k in ("recharge_credits", "credits", "remaining_credits", "balance"):
            if k in info:
                try:
                    return float(info[k])
                except (TypeError, ValueError):
                    pass
        return None
