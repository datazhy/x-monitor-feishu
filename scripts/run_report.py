"""手动生成并推送一份昨日信号早报（测试/补发）。

用法：
  python -m scripts.run_report             # 昨天（北京）
  python -m scripts.run_report 2026-06-28  # 指定北京日期
"""
import sys

from app import db, report


def main() -> None:
    db.init_db()
    day = sys.argv[1] if len(sys.argv) > 1 else None
    result = report.generate_and_send(day)
    print("结果:", result)


if __name__ == "__main__":
    main()
