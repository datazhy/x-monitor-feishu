"""冒烟测试：解析、幂等、去重、飞书签名、推送 worker（mock 飞书）。

运行： python -m pytest -q   或   python -m tests.test_smoke
"""
from __future__ import annotations

import json
import os
import tempfile


def _setup_env():
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test.db")
    os.environ["WEBHOOK_SECRET"] = "testsecret"
    os.environ["FEISHU_WEBHOOK_URL"] = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    from app.config import get_settings

    get_settings.cache_clear()
    from app import db

    # 重置线程本地连接，让每个测试用各自的临时库（生产环境 DB_PATH 固定，无此问题）
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    db.init_db()
    return db


def test_parse_and_dedup():
    db = _setup_env()
    from app import webhook

    body = json.dumps(
        {
            "tweets": [
                {
                    "id": "1001",
                    "text": "hello world",
                    "author": {"userName": "Alice", "name": "Alice A", "id": "999"},
                    "createdAt": "2026-06-29T00:00:00Z",
                }
            ]
        }
    ).encode()
    tweets = webhook.parse_tweets(body)
    assert len(tweets) == 1
    t = tweets[0]
    assert t["tweet_id"] == "1001"
    assert t["author_handle"] == "alice"
    assert t["tweet_url"].endswith("/status/1001")

    assert db.insert_tweet(t) is True   # 首次
    assert db.insert_tweet(t) is False  # 去重
    print("✅ parse + dedup")


def test_delivery_idempotency():
    db = _setup_env()
    assert db.record_delivery("d-1", "received") is True
    assert db.record_delivery("d-1", "received") is False
    print("✅ delivery 幂等")


def test_feishu_sign():
    from app.feishu import _gen_sign

    s = _gen_sign("secret", 1700000000)
    assert isinstance(s, str) and len(s) > 10
    print("✅ feishu 签名生成")


def test_push_worker_retry(monkeypatch=None):
    db = _setup_env()
    from app import notifier, push_worker

    db.insert_tweet(
        {
            "tweet_id": "2002",
            "author_handle": "bob",
            "text": "x",
            "tweet_url": "https://x.com/bob/status/2002",
            "created_at": "now",
        }
    )

    calls = {"n": 0}

    def fake_fail(_t):
        calls["n"] += 1
        raise RuntimeError("boom")

    orig = notifier.notify_tweet
    notifier.notify_tweet = fake_fail
    try:
        push_worker.process_due_jobs()
    finally:
        notifier.notify_tweet = orig

    job = db.get_conn().execute("SELECT * FROM push_jobs WHERE tweet_id='2002'").fetchone()
    assert calls["n"] == 1
    assert job["retry_count"] == 1
    assert job["status"] == "pending"  # 还没到 max，回到 pending 等重试
    print("✅ 推送失败重试入队")


if __name__ == "__main__":
    test_parse_and_dedup()
    test_delivery_idempotency()
    test_feishu_sign()
    test_push_worker_retry()
    print("\n🎉 全部冒烟测试通过")
