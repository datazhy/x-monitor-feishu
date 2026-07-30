"""发送一条飞书测试消息（验收清单第 1 项）。

用法： python -m scripts.send_test [自定义文本]
"""
import sys

from app import notifier
from app.config import get_settings


def main() -> None:
    s = get_settings()
    text = sys.argv[1] if len(sys.argv) > 1 else "✅ X 推文监控测试消息：飞书机器人连通正常。"
    if not s.feishu_webhook_url or "CHANGE_ME" in s.feishu_webhook_url:
        print("❌ 请先在 .env 配置 FEISHU_WEBHOOK_URL")
        sys.exit(1)
    notifier.notify_tweet(
        {
            "author_name": "测试机器人",
            "author_handle": "test_bot",
            "text": text,
            "tweet_url": "https://x.com/test_bot/status/0",
            "created_at": "now",
            "is_reply": 0,
            "is_retweet": 0,
            "is_quote": 0,
            "media_count": 0,
        }
    )
    print("✅ 已发送，请检查飞书群")


if __name__ == "__main__":
    main()
