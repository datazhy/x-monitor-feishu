"""初始化数据库表结构。用法： python -m scripts.init_db"""
from app import db


def main() -> None:
    db.init_db()
    print(f"✅ 数据库已初始化")


if __name__ == "__main__":
    main()
