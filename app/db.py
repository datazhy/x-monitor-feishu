"""SQLite 访问层：连接、建表、推文/任务/规则 的增删查。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import get_settings

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_file, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def get_conn() -> sqlite3.Connection:
    """每线程一个连接（SQLite 连接非线程安全）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_conn()
    conn.executescript(sql)
    conn.commit()


# ---------------- webhook 幂等 ----------------
def record_delivery(delivery_id: str, status: str, tweet_count: int = 0, note: str = "") -> bool:
    """记录一次 webhook 投递。返回 True 表示首次（应处理），False 表示重复。"""
    with tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO webhook_deliveries(delivery_id, received_at, status, tweet_count, note) "
            "VALUES (?,?,?,?,?)",
            (delivery_id, now_iso(), status, tweet_count, note),
        )
        return cur.rowcount > 0


def update_delivery(delivery_id: str, status: str, tweet_count: int, note: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE webhook_deliveries SET status=?, tweet_count=?, note=? WHERE delivery_id=?",
            (status, tweet_count, note, delivery_id),
        )


# ---------------- 推文 ----------------
def insert_tweet(t: dict[str, Any]) -> bool:
    """插入推文。返回 True=新推文，False=已存在（去重）。"""
    with tx() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO tweets
               (tweet_id, author_handle, author_name, author_user_id, text, tweet_url,
                is_reply, is_retweet, is_quote, media_count, created_at, received_at, source)
               VALUES (:tweet_id, :author_handle, :author_name, :author_user_id, :text, :tweet_url,
                       :is_reply, :is_retweet, :is_quote, :media_count, :created_at, :received_at, :source)""",
            {
                "received_at": now_iso(),
                "source": t.get("source", "webhook"),
                "author_name": t.get("author_name"),
                "author_user_id": t.get("author_user_id"),
                "is_reply": int(bool(t.get("is_reply"))),
                "is_retweet": int(bool(t.get("is_retweet"))),
                "is_quote": int(bool(t.get("is_quote"))),
                "media_count": int(t.get("media_count", 0)),
                "created_at": t.get("created_at"),
                **{k: t.get(k) for k in ("tweet_id", "author_handle", "text", "tweet_url")},
            },
        )
        is_new = cur.rowcount > 0
        # 去重规则：记录该博主"最后看到的 tweet_id"（无论是否推送，重启不丢）
        conn.execute(
            """INSERT INTO author_state(handle, last_tweet_id, updated_at) VALUES(?,?,?)
               ON CONFLICT(handle) DO UPDATE SET
                 last_tweet_id = CASE
                   WHEN CAST(excluded.last_tweet_id AS INTEGER) > CAST(author_state.last_tweet_id AS INTEGER)
                   THEN excluded.last_tweet_id ELSE author_state.last_tweet_id END,
                 updated_at = excluded.updated_at""",
            (t["author_handle"], t["tweet_id"], now_iso()),
        )
        # 推送范围：默认只推纯原创；放宽名单里的博主允许引用+回复（仍不推纯转推）
        relaxed = t["author_handle"] in get_settings().relaxed_authors
        pushable = not t.get("is_retweet") and (relaxed or not (t.get("is_reply") or t.get("is_quote")))
        if is_new and pushable:
            conn.execute(
                "INSERT OR IGNORE INTO push_jobs(tweet_id, status, created_at, updated_at, next_retry_at) "
                "VALUES (?, 'pending', ?, ?, ?)",
                (t["tweet_id"], now_iso(), now_iso(), now_iso()),
            )
        return is_new


def prune_tweets(keep: int = 5000) -> int:
    """去重账本上限：只保留最近 keep 条 tweets（含 push_jobs），超出按 received_at 裁剪。"""
    with tx() as conn:
        rows = conn.execute(
            "SELECT tweet_id FROM tweets ORDER BY received_at DESC LIMIT -1 OFFSET ?", (keep,)
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return 0
        qm = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM push_jobs WHERE tweet_id IN ({qm})", ids)
        conn.execute(f"DELETE FROM tweets WHERE tweet_id IN ({qm})", ids)
        return len(ids)


def mark_pushed(tweet_id: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE tweets SET pushed_at=? WHERE tweet_id=?", (now_iso(), tweet_id))


def get_tweet(tweet_id: str) -> Optional[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()


def tweet_exists(tweet_id: str) -> bool:
    return get_conn().execute("SELECT 1 FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone() is not None


# ---------------- 推送任务 ----------------
def claim_due_jobs(limit: int = 20) -> list[sqlite3.Row]:
    """取到点可推送的任务（pending 且 next_retry_at <= now）。"""
    return get_conn().execute(
        "SELECT * FROM push_jobs WHERE status='pending' AND next_retry_at <= ? "
        "ORDER BY next_retry_at LIMIT ?",
        (now_iso(), limit),
    ).fetchall()


def set_job_sending(tweet_id: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE push_jobs SET status='sending', updated_at=? WHERE tweet_id=?",
            (now_iso(), tweet_id),
        )


def set_job_done(tweet_id: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE push_jobs SET status='done', last_error=NULL, updated_at=? WHERE tweet_id=?",
            (now_iso(), tweet_id),
        )


def set_job_retry(tweet_id: str, retry_count: int, next_retry_at: str, error: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE push_jobs SET status='pending', retry_count=?, next_retry_at=?, last_error=?, updated_at=? "
            "WHERE tweet_id=?",
            (retry_count, next_retry_at, error[:1000], now_iso(), tweet_id),
        )


def set_job_dead(tweet_id: str, error: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE push_jobs SET status='dead', last_error=?, updated_at=? WHERE tweet_id=?",
            (error[:1000], now_iso(), tweet_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO dead_letters(tweet_id, last_error, failed_at) VALUES (?,?,?)",
            (tweet_id, error[:1000], now_iso()),
        )


def unalerted_dead_letters() -> list[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM dead_letters WHERE alerted=0").fetchall()


def mark_dead_alerted(tweet_id: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE dead_letters SET alerted=1 WHERE tweet_id=?", (tweet_id,))


# ---------------- 规则 / handle ----------------
def upsert_rule(name: str, value: str, interval_seconds: int, active: bool, remote_id: str | None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO rules(rule_name, remote_rule_id, value, interval_seconds, active, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(rule_name) DO UPDATE SET
                 remote_rule_id=COALESCE(excluded.remote_rule_id, rules.remote_rule_id),
                 value=excluded.value, interval_seconds=excluded.interval_seconds,
                 active=excluded.active, updated_at=excluded.updated_at""",
            (name, remote_id, value, interval_seconds, int(active), now_iso()),
        )


def get_rules() -> list[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM rules ORDER BY rule_name").fetchall()


def upsert_handle(handle: str, user_id: str | None, display_name: str | None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO handles(handle, user_id, display_name, last_checked_at, active)
               VALUES (?,?,?,?,1)
               ON CONFLICT(handle) DO UPDATE SET
                 user_id=COALESCE(excluded.user_id, handles.user_id),
                 display_name=COALESCE(excluded.display_name, handles.display_name),
                 last_checked_at=excluded.last_checked_at""",
            (handle, user_id, display_name, now_iso()),
        )


def active_handles() -> list[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM handles WHERE active=1").fetchall()


# ---------------- 成本 / 运维事件 ----------------
def record_cost(day: str, rule_count: int, api_calls: int, tweets_returned: int, balance: float | None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO cost_snapshots(day, rule_count, api_calls, tweets_returned, balance_usd, recorded_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                 rule_count=excluded.rule_count, api_calls=excluded.api_calls,
                 tweets_returned=excluded.tweets_returned, balance_usd=excluded.balance_usd,
                 recorded_at=excluded.recorded_at""",
            (day, rule_count, api_calls, tweets_returned, balance, now_iso()),
        )


def log_event(kind: str, ok: bool, detail: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO ops_events(kind, ok, detail, created_at) VALUES (?,?,?,?)",
            (kind, int(ok), detail[:2000], now_iso()),
        )


def _beijing_day() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def record_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """累加一次 LLM 调用的实际 token 用量（按北京日期+模型聚合）。失败不影响主流程。"""
    try:
        with tx() as conn:
            conn.execute(
                """INSERT INTO llm_usage(day, model, calls, prompt_tokens, completion_tokens)
                   VALUES (?,?,1,?,?)
                   ON CONFLICT(day, model) DO UPDATE SET
                     calls = calls + 1,
                     prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                     completion_tokens = completion_tokens + excluded.completion_tokens""",
                (_beijing_day(), model, int(prompt_tokens or 0), int(completion_tokens or 0)),
            )
    except Exception:
        pass


def usage_between(days: int) -> list[sqlite3.Row]:
    """最近 days 天的 LLM 用量（按模型聚合）。"""
    return get_conn().execute(
        """SELECT model, SUM(calls) calls, SUM(prompt_tokens) pt, SUM(completion_tokens) ct
           FROM llm_usage WHERE day >= date('now','+8 hours', ?) GROUP BY model""",
        (f"-{days} days",),
    ).fetchall()


def tweets_count_since(days: int) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) c FROM tweets WHERE received_at >= datetime('now', ?)", (f"-{days} days",)
    ).fetchone()["c"]


def save_daily_topics(day: str, topic_stats: list[dict]) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM daily_topics WHERE day=?", (day,))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_topics(day, topic, tweet_count, author_count) VALUES (?,?,?,?)",
            [(day, t.get("topic", ""), int(t.get("tweet_count", 0)), int(t.get("author_count", 0))) for t in topic_stats],
        )


def topic_history(days: int) -> dict[str, list[str]]:
    """返回最近 days 天里每个 topic 出现过的日期列表（用于连续信号）。"""
    rows = get_conn().execute(
        "SELECT day, topic FROM daily_topics WHERE day >= date('now', ?) ORDER BY day",
        (f"-{days} days",),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["topic"], []).append(r["day"])
    return out


def prev_day_topics(day: str) -> dict[str, int]:
    """前一天各 topic 的推文数（用于升温/降温判断）。"""
    rows = get_conn().execute(
        "SELECT topic, tweet_count FROM daily_topics WHERE day = date(?, '-1 day')", (day,)
    ).fetchall()
    return {r["topic"]: r["tweet_count"] for r in rows}


def save_daily_report(day: str, report_json: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_reports(day, report_json, created_at) VALUES (?,?,?)",
            (day, report_json, now_iso()),
        )


def last_event(kind: str) -> Optional[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM ops_events WHERE kind=? ORDER BY created_at DESC LIMIT 1", (kind,)
    ).fetchone()


def count_recent_failures(kind: str, minutes: int = 60) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) AS c FROM ops_events WHERE kind=? AND ok=0 "
        "AND created_at >= datetime('now', ?)",
        (kind, f"-{minutes} minutes"),
    ).fetchone()["c"]
