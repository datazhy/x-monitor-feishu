"""后台推送 worker：消费 push_jobs，递增退避重试，最终进 dead-letter。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, notifier
from .config import get_settings

log = logging.getLogger(__name__)


def _next_retry_at(retry_count: int) -> str:
    backoff = get_settings().retry_backoff
    # retry_count 是“已失败次数”，取对应档位（超出用最后一档）
    idx = min(retry_count, len(backoff)) - 1
    minutes = backoff[idx] if idx >= 0 else 1
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def process_due_jobs() -> int:
    """处理一批到点任务。返回成功推送数。被 scheduler 周期调用。"""
    settings = get_settings()
    jobs = db.claim_due_jobs(limit=25)
    sent = 0
    for job in jobs:
        tweet_id = job["tweet_id"]
        tweet = db.get_tweet(tweet_id)
        if tweet is None:
            db.set_job_dead(tweet_id, "tweet 行缺失")
            continue

        db.set_job_sending(tweet_id)
        try:
            notifier.notify_tweet(tweet)
            db.set_job_done(tweet_id)
            db.mark_pushed(tweet_id)
            sent += 1
        except Exception as e:
            retry_count = job["retry_count"] + 1
            if retry_count > settings.push_max_retries:
                db.set_job_dead(tweet_id, str(e))
                db.log_event("alert", False, f"推送彻底失败 tweet={tweet_id}: {e}")
                log.error("tweet %s 进入 dead-letter: %s", tweet_id, e)
            else:
                nxt = _next_retry_at(retry_count)
                db.set_job_retry(tweet_id, retry_count, nxt, str(e))
                log.warning("tweet %s 第 %d 次推送失败，%s 后重试: %s", tweet_id, retry_count, nxt, e)
    return sent


def alert_dead_letters() -> None:
    """对尚未告警的 dead-letter 发管理员告警。"""
    for dl in db.unalerted_dead_letters():
        notifier.notify_admin(f"推文 {dl['tweet_id']} 推送彻底失败：{dl['last_error']}")
        db.mark_dead_alerted(dl["tweet_id"])
