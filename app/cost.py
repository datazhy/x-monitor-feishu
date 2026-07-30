"""成本核算：按实测 token 用量 × 单价，生成周期成本报告推送到告警群。

单价可用 .env 的 LLM_PRICES(JSON) 覆盖；gpt-5.4 / deepseek 的默认价为估计值，
请上线后对照 OpenAI/DeepSeek 账单校准。
"""
from __future__ import annotations

import json
import logging

from . import db, feishu
from .config import get_settings

log = logging.getLogger(__name__)

# 默认单价 $/1M tokens (input, output)
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # 均为官方公开价（$/1M tokens，input, output）
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
    "gpt-5.4": (2.5, 15.0),
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
    "deepseek": (0.14, 0.28),
}


def _prices() -> dict[str, tuple[float, float]]:
    p = dict(DEFAULT_PRICES)
    raw = get_settings().llm_prices.strip()
    if raw:
        try:
            for k, v in json.loads(raw).items():
                p[k] = (float(v[0]), float(v[1]))
        except Exception as e:
            log.warning("LLM_PRICES 解析失败: %s", e)
    return p


def _price_for(model: str, prices: dict) -> tuple[float, float]:
    if model in prices:
        return prices[model]
    for prefix, v in prices.items():
        if model.startswith(prefix):
            return v
    return (0.0, 0.0)


def compute(days: int) -> dict:
    prices = _prices()
    rows = db.usage_between(days)
    per_model = []
    llm_total = 0.0
    for r in rows:
        pin, pout = _price_for(r["model"], prices)
        cost = (r["pt"] or 0) / 1e6 * pin + (r["ct"] or 0) / 1e6 * pout
        llm_total += cost
        per_model.append(
            {"model": r["model"], "calls": r["calls"], "pt": r["pt"], "ct": r["ct"], "cost": cost}
        )
    per_model.sort(key=lambda x: -x["cost"])

    s = get_settings()
    tweets = db.tweets_count_since(days)
    tweet_cost = tweets / 1000 * s.twitterapi_tweet_price

    # 检查成本（估算）：各激活规则 checks = days*86400/interval，× 每次 credits × credit 单价
    checks = 0.0
    try:
        from .twitterapi import TwitterAPIClient

        client = TwitterAPIClient()
        for r in client.list_rules():
            if r.get("is_effect", 1):
                iv = float(r.get("interval_seconds") or 600)
                checks += days * 86400.0 / iv
        client.close()
    except Exception as e:
        log.warning("获取规则间隔失败，检查成本按 0 计: %s", e)
    check_credits = checks * s.twitterapi_check_credits
    check_cost = check_credits * s.credit_usd

    twitter_cost = tweet_cost + check_cost
    total = llm_total + twitter_cost
    monthly = total / days * 30
    return {
        "days": days,
        "per_model": per_model,
        "llm_total": llm_total,
        "tweets": tweets,
        "tweet_cost": tweet_cost,
        "checks": int(checks),
        "check_cost": check_cost,
        "twitter_cost": twitter_cost,
        "total": total,
        "monthly": monthly,
    }


def record_daily_snapshot() -> None:
    """每天记一笔当天成本快照（含余额），便于长期追踪。"""
    from datetime import datetime, timezone, timedelta

    day = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    c = compute(1)
    from .twitterapi import TwitterAPIClient

    bal = None
    try:
        client = TwitterAPIClient()
        bal = client.balance()
        client.close()
    except Exception:
        pass
    db.record_cost(day, rule_count=0, api_calls=0, tweets_returned=c["tweets"], balance=bal)


def weekly_report_and_push() -> dict:
    """周期成本报告：算最近 N 天成本，推送到告警群。"""
    s = get_settings()
    c = compute(s.cost_report_days)

    lines = [f"💰 成本报告（最近 {c['days']} 天）", ""]
    lines.append("TwitterAPI：")
    lines.append(f"  · 规则检查 {c['checks']} 次 ≈ ${c['check_cost']:.2f}")
    lines.append(f"  · 返回推文 {c['tweets']} 条 ≈ ${c['tweet_cost']:.2f}")
    lines.append("OpenAI / DeepSeek：")
    for m in c["per_model"]:
        lines.append(
            f"  · {m['model']}：{m['calls']} 次，in {m['pt']:,} / out {m['ct']:,} tok ≈ ${m['cost']:.2f}"
        )
    lines += [
        "",
        f"合计 {c['days']} 天 ≈ ${c['total']:.2f}",
        f"折合月度 ≈ ${c['monthly']:.2f}",
        "",
        "注：gpt-5.4/deepseek 单价为估计值，可用 .env LLM_PRICES 校准。",
    ]
    text = "\n".join(lines)

    if s.alert_webhook and "CHANGE_ME" not in s.alert_webhook:
        feishu.send_text(s.alert_webhook, text, s.alert_secret)
    db.log_event("cost", True, f"{c['days']}天 ${c['total']:.2f} 月度${c['monthly']:.2f}")
    log.info("成本报告已推送：%s", text.replace("\n", " | "))
    return c
