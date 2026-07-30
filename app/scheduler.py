"""APScheduler：后台推送轮询 + 每日补漏/心跳/成本检查。

单进程内嵌调度（无需额外 cron/systemd timer）。如果你更想用 systemd timer，
deploy/systemd 下有等价单元文件，把对应 job 关掉即可。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import cost, db, push_worker, report, tasks
from .config import get_settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _push_tick() -> None:
    try:
        push_worker.process_due_jobs()
        push_worker.alert_dead_letters()
    except Exception:
        log.exception("push tick 失败")


def _backfill_hours(times_per_day: int) -> str:
    """把每天 N 次均匀映射到小时列表。"""
    times_per_day = max(1, min(times_per_day, 24))
    step = 24 // times_per_day
    return ",".join(str((i * step) % 24) for i in range(times_per_day))


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    s = get_settings()
    sched = BackgroundScheduler(timezone="UTC")

    # 推送 worker：每 15 秒扫一次到点任务（webhook 进来后近实时推送）
    sched.add_job(_push_tick, IntervalTrigger(seconds=15), id="push_tick", max_instances=1, coalesce=True)

    # 补漏：BACKFILL_TIMES_PER_DAY=0 即关闭（节省成本，webhook 已足够可靠）
    if s.backfill_times_per_day > 0:
        sched.add_job(
            tasks.backfill,
            CronTrigger(hour=_backfill_hours(s.backfill_times_per_day), minute=20),
            id="backfill",
            max_instances=1,
        )

    # 心跳：每天一次
    sched.add_job(
        tasks.heartbeat,
        CronTrigger(hour=s.heartbeat_cron_hour, minute=s.heartbeat_cron_minute),
        id="heartbeat",
        max_instances=1,
    )

    # 成本检查：每天一次（心跳后 5 分钟）
    sched.add_job(
        tasks.cost_check,
        CronTrigger(hour=s.heartbeat_cron_hour, minute=(s.heartbeat_cron_minute + 5) % 60),
        id="cost_check",
        max_instances=1,
    )

    # handle 变更检查：每月一次（每月 1 号 04:30 UTC），异常推送告警群
    sched.add_job(
        tasks.check_handle_changes, CronTrigger(day=1, hour=4, minute=30), id="handle_check", max_instances=1
    )

    # 去重账本裁剪：每天一次，tweets 表保留最近 pushed_retention 条
    sched.add_job(
        lambda: db.prune_tweets(get_settings().pushed_retention),
        CronTrigger(hour=4, minute=50),
        id="prune",
        max_instances=1,
    )

    # 昨日信号早报：每天北京 9:00（= UTC 1:00）
    report_utc_hour = (s.report_hour_beijing - 8) % 24
    if s.report_enabled:
        sched.add_job(
            report.generate_and_send,
            CronTrigger(hour=report_utc_hour, minute=0),
            id="daily_report",
            max_instances=1,
        )

    # 成本报告：每 7 天一次（每月 1/8/15/22/29 号）北京 9:30，推送告警群
    sched.add_job(
        cost.weekly_report_and_push,
        CronTrigger(day="1,8,15,22,29", hour=report_utc_hour, minute=30),
        id="cost_report",
        max_instances=1,
    )

    sched.start()
    _scheduler = sched
    log.info("调度器已启动：补漏=%s 次/天, 心跳=%02d:%02d", s.backfill_times_per_day, s.heartbeat_cron_hour, s.heartbeat_cron_minute)
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
