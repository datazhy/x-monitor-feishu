"""把历史推文补齐中文正文 + AI 分析（text_zh / ai_summary）。

新推文在推送时会自动落库；本脚本用于回填历史数据。
可反复执行：只处理 enriched_at IS NULL 的推文，中断后再跑会接着做。

用法：
  python -m scripts.enrich_backfill            # 回填全部（分批）
  python -m scripts.enrich_backfill 100        # 只处理 100 条（试跑）
"""
from __future__ import annotations

import sys
import time

from app import db, notifier


def main() -> None:
    db.init_db()
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 0  # 0 = 不限
    total = db.get_conn().execute(
        "SELECT COUNT(*) FROM tweets WHERE enriched_at IS NULL"
    ).fetchone()[0]
    print(f"待回填: {total} 条" + (f"（本次限 {budget} 条）" if budget else ""))

    done = failed = 0
    while True:
        batch = db.tweets_needing_enrichment(limit=20)
        if not batch:
            break
        for row in batch:
            if budget and done + failed >= budget:
                print(f"\n达到本次上限。完成 {done}，失败 {failed}，剩余 {total - done - failed}")
                return
            try:
                text_zh, summary, _note = notifier.enrich(row["text"] or "")
                db.save_enrichment(row["tweet_id"], text_zh, summary or None)
                done += 1
                if done % 20 == 0:
                    print(f"  已完成 {done}/{total}")
            except Exception as e:
                # 失败也标记，避免死循环卡在同一条
                db.save_enrichment(row["tweet_id"], row["text"] or "", None)
                failed += 1
                print(f"  ✗ {row['tweet_id']}: {e}")
            time.sleep(0.3)  # 轻微限速，避免打爆 API

    print(f"\n✅ 回填完成：成功 {done}，降级 {failed}")


if __name__ == "__main__":
    main()
