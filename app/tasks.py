"""定时运维任务：补漏、心跳、成本/余额检查、handle 变更检查。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from . import db, notifier, webhook
from .config import get_settings
from .twitterapi import TwitterAPIClient

log = logging.getLogger(__name__)


def backfill() -> dict:
    """每天 N 次：主动拉每个博主最近推文，补推数据库里没有的 tweet_id。"""
    client = TwitterAPIClient()
    added = 0
    queued = 0
    checked = 0
    try:
        for h in db.active_handles():
            checked += 1
            raws = client.last_tweets(h["handle"], count=20)
            for raw in raws:
                norm_list = webhook.parse_tweets(_wrap(raw))
                for norm in norm_list:
                    norm["source"] = "backfill"
                    if not db.tweet_exists(norm["tweet_id"]):
                        if db.insert_tweet(norm):
                            added += 1
                            queued += 1
    finally:
        client.close()
    detail = f"检查 {checked} 个博主，补入 {added} 条新推文"
    db.log_event("backfill", True, detail)
    log.info("补漏完成：%s", detail)
    return {"checked": checked, "added": added, "queued": queued}


def _wrap(raw: dict) -> bytes:
    import json

    return json.dumps({"tweets": [raw]}).encode("utf-8")


def cost_check() -> dict:
    """记录每日规则数/余额，余额低于阈值则告警。"""
    settings = get_settings()
    client = TwitterAPIClient()
    try:
        balance = client.balance()
        rules = db.get_rules()
        active_rules = sum(1 for r in rules if r["active"])
    finally:
        client.close()

    today = date.today().isoformat()
    db.record_cost(today, active_rules, api_calls=0, tweets_returned=0, balance=balance)

    if balance is not None and balance < settings.balance_alert_threshold:
        notifier.notify_admin(f"TwitterAPI.io 余额不足：${balance:.2f}（阈值 ${settings.balance_alert_threshold}）")
        db.log_event("cost", False, f"余额 ${balance}")
    else:
        db.log_event("cost", True, f"余额 {balance}, active_rules={active_rules}")
    return {"balance": balance, "active_rules": active_rules}


def heartbeat() -> dict:
    """每天 1 次综合体检，异常推送飞书。"""
    settings = get_settings()
    problems: list[str] = []
    client = TwitterAPIClient()
    try:
        # 1. API key 是否有效 / 余额
        info = client.account_info()
        if not info:
            problems.append("无法获取 TwitterAPI.io 账户信息（API key 可能失效）")
        balance = client.balance()
        if balance is not None and balance < settings.balance_alert_threshold:
            problems.append(f"余额不足 ${balance:.2f}")

        # 2. 规则是否 active
        remote_rules = client.list_rules()
        if remote_rules:
            # TwitterAPI.io 激活字段为 is_effect(1/0)
            inactive = [r for r in remote_rules if not r.get("is_effect", r.get("is_active", 1))]
            if inactive:
                tags = ", ".join(str(r.get("tag")) for r in inactive)
                problems.append(f"{len(inactive)} 条 Filter Rule 未激活: {tags}")
    finally:
        client.close()

    # 3. 最近 webhook 到达时间（> 2 小时无到达可能异常，按你的博主活跃度调整）
    last_wh = db.last_event("webhook")
    if last_wh:
        age_min = _age_minutes(last_wh["created_at"])
        if age_min is not None and age_min > 180:
            problems.append(f"已 {int(age_min)} 分钟没有 webhook 到达")

    # 4. 最近飞书是否连续失败
    fails = db.count_recent_failures("alert", minutes=120)
    if fails >= 3:
        problems.append(f"近 2 小时有 {fails} 次推送失败")

    # 5. 补漏任务最近是否成功
    last_bf = db.last_event("backfill")
    if last_bf and not last_bf["ok"]:
        problems.append("最近一次补漏任务失败")

    ok = not problems
    detail = "一切正常" if ok else "；".join(problems)
    db.log_event("heartbeat", ok, detail)
    if not ok:
        notifier.notify_admin("心跳异常：" + detail)
    log.info("心跳：%s", detail)
    return {"ok": ok, "detail": detail}


def check_handle_changes() -> dict:
    """检查重点 handle 是否改名（user_id 对不上）。保存 user_id 防失效。"""
    client = TwitterAPIClient()
    changed: list[str] = []
    try:
        for h in db.active_handles():
            user = client.get_user(h["handle"])
            if not user:
                changed.append(f"{h['handle']}（查不到，可能已改名/封禁）")
                continue
            new_id = str(user.get("id") or user.get("rest_id") or "")
            new_name = user.get("userName") or user.get("screen_name")
            if h["user_id"] and new_id and h["user_id"] != new_id:
                changed.append(f"{h['handle']} user_id 变化 {h['user_id']}->{new_id}")
            db.upsert_handle(new_name or h["handle"], new_id or h["user_id"], user.get("name"))
    finally:
        client.close()
    if changed:
        notifier.notify_admin("handle 变更检查：\n" + "\n".join(changed))
    db.log_event("heartbeat", not changed, "handle 检查：" + ("无变化" if not changed else "；".join(changed)))
    return {"changed": changed}


def _age_minutes(iso_ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except (ValueError, TypeError):
        return None
