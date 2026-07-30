"""把 config/rules.yml 同步到 TwitterAPI.io，并维护本地 rules/handles 表。

用法：
  python -m scripts.manage_rules build       # 只本地构建规则文本，不调 API
  python -m scripts.manage_rules sync        # 创建/更新远端规则 + is_effect=1 激活 + 试绑 webhook
  python -m scripts.manage_rules list        # 列出远端规则
  python -m scripts.manage_rules test-rule   # 临时加高频规则 from:elonmusk(60s) 验证端到端
  python -m scripts.manage_rules delete-test # 删除 tag 含 'test' 的规则
  python -m scripts.manage_rules userids     # 节流补抓各 handle 的 user_id（防 429）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

from app import db
from app.config import get_settings
from app.twitterapi import TwitterAPIClient

RULES_FILE = Path(__file__).resolve().parent.parent / "config" / "rules.yml"


def load_config() -> dict:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))


def check_duplicates(cfg: dict) -> list[str]:
    """跨规则重复 handle 检测（小写，from: 不区分大小写）。返回重复项。"""
    seen: dict[str, str] = {}
    dups: list[str] = []
    for rule in cfg["rules"]:
        for h in rule["handles"]:
            key = h.lstrip("@").lower()
            if key in seen:
                dups.append(f"{key}（在 {seen[key]} 和 {rule['name']}）")
            else:
                seen[key] = rule["name"]
    return dups


# 服务端前置过滤算子（计费看 API 返回量，前置最省）
_EXCLUDE_OP = {"retweet": "-is:retweet", "reply": "-is:reply", "quote": "-is:quote"}
_DEFAULT_EXCLUDE = ["retweet", "reply", "quote"]


def build_rule_value(handles: list[str], exclude: list[str]) -> str:
    """(from:a OR from:b ...) + 按 exclude 追加 -is:xxx  全部小写、去 @。"""
    parts = [f"from:{h.lstrip('@').lower()}" for h in handles]
    base = "(" + " OR ".join(parts) + ")"
    ex = " ".join(_EXCLUDE_OP[e] for e in exclude if e in _EXCLUDE_OP)
    return f"{base} {ex}".strip()


def webhook_url() -> str:
    return get_settings().webhook_url  # 从 .env 的 DOMAIN/WEBHOOK_PORT/WEBHOOK_SECRET 拼出


def cmd_build() -> None:
    cfg = load_config()
    dups = check_duplicates(cfg)
    if dups:
        print("⚠️  重复 handle：", "; ".join(dups))
    default_interval = cfg.get("interval_seconds", 600)
    default_ex = cfg.get("default_exclude", _DEFAULT_EXCLUDE)
    for rule in cfg["rules"]:
        value = build_rule_value(rule["handles"], rule.get("exclude", default_ex))
        interval = rule.get("interval_seconds", default_interval)
        active = rule.get("active", True)
        print(f"\n# {rule['name']}  active={active} interval={interval}s  ({len(value)} 字符)")
        print(value)
        if len(value) > 255:
            print(f"⚠️  规则字符数 {len(value)} 超过 255 上限，需拆分该组")


def cmd_sync() -> None:
    db.init_db()
    cfg = load_config()
    dups = check_duplicates(cfg)
    if dups:
        print("❌ 发现重复 handle，已中止同步（先在 rules.yml 去重）：")
        for d in dups:
            print("  -", d)
        return
    default_interval = cfg.get("interval_seconds", 600)
    default_ex = cfg.get("default_exclude", _DEFAULT_EXCLUDE)
    client = TwitterAPIClient()
    try:
        existing = {r.get("tag"): r for r in client.list_rules()}
        for rule in cfg["rules"]:
            name = rule["name"]
            handles = rule["handles"]
            value = build_rule_value(handles, rule.get("exclude", default_ex))
            interval = rule.get("interval_seconds", default_interval)
            active = rule.get("active", True)

            remote = existing.get(name)
            if remote:
                rid = str(remote.get("rule_id") or remote.get("id"))
                # 无改动则跳过：TwitterAPI.io 对原样更新会返回 "update rule failed"
                same = (
                    (remote.get("value") or "").strip() == value.strip()
                    and int(float(remote.get("interval_seconds") or 0)) == int(interval)
                    and int(remote.get("is_effect", remote.get("is_active", 0))) == int(active)
                )
                if same:
                    print(f"✓ {name} 已是最新，跳过 (id={rid})")
                    db.upsert_rule(name, value, interval, active, rid)
                    for h in handles:
                        db.upsert_handle(h.lstrip("@").lower(), None, None)
                    continue
                action = "🔄 更新"
            else:
                rid = client.add_rule(value, interval, webhook_url(), tag=name)
                action = "➕ 新建"
            # is_effect=1 激活 + 尝试用 API 绑定 webhook（不支持则被忽略，需 dashboard 核对）
            resp = client.update_rule(rid, name, value, interval, is_effect=active, webhook_url=webhook_url())
            status = resp.get("status", resp) if isinstance(resp, dict) else resp
            print(f"{action}并激活 {name} (id={rid}) is_effect={int(active)} -> {status}")
            db.upsert_rule(name, value, interval, active, rid)
            # 只存 handle（user_id 用 userids 命令节流补抓，避免 429）
            for h in handles:
                db.upsert_handle(h.lstrip("@").lower(), None, None)
        print("\n✅ 同步完成。webhook =", webhook_url())
        print("⚠️  请到 TwitterAPI.io dashboard 核对两条规则的 Webhook URL 字段 = 上面地址。")
    finally:
        client.close()


def cmd_test_rule() -> None:
    """临时加一条高频规则验证端到端链路，验证完用 delete-test 删除。"""
    client = TwitterAPIClient()
    try:
        value = "from:elonmusk"
        rid = client.add_rule(value, 60, webhook_url(), tag="test_e2e")
        resp = client.update_rule(rid, "test_e2e", value, 60, is_effect=True, webhook_url=webhook_url())
        print(f"➕ 临时测试规则 test_e2e (id={rid}) interval=60s 已激活 -> {resp}")
        print("   等几分钟看 app 日志是否收到 webhook，再 delete-test 删除。")
    finally:
        client.close()


def cmd_userids() -> None:
    """节流逐个补抓 user_id（user/info 端点限流严，需间隔 + 重试）。"""
    db.init_db()
    cfg = load_config()
    client = TwitterAPIClient()
    ok = 0
    total = 0
    try:
        for rule in cfg["rules"]:
            for h in rule["handles"]:
                total += 1
                handle = h.lstrip("@").lower()
                user = None
                for _ in range(3):
                    user = client.get_user(handle)
                    if user:
                        break
                    time.sleep(3)
                if user:
                    db.upsert_handle(
                        user.get("userName") or handle,
                        str(user.get("id") or user.get("rest_id") or ""),
                        user.get("name"),
                    )
                    ok += 1
                    print(f"  ✓ {handle} -> {user.get('id') or user.get('rest_id')}")
                else:
                    print(f"  ✗ {handle} 仍失败（限流/不存在）")
                time.sleep(2)
        print(f"\n✅ user_id 补抓：{ok}/{total}")
    finally:
        client.close()


def cmd_list() -> None:
    client = TwitterAPIClient()
    try:
        for r in client.list_rules():
            print(r)
    finally:
        client.close()


def cmd_delete_test() -> None:
    client = TwitterAPIClient()
    try:
        for r in client.list_rules():
            tag = (r.get("tag") or "").lower()
            if "test" in tag:
                rid = str(r.get("rule_id") or r.get("id"))
                client.delete_rule(rid)
                print(f"🗑️  删除测试规则 {tag} (id={rid})")
    finally:
        client.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {
        "build": cmd_build,
        "sync": cmd_sync,
        "list": cmd_list,
        "test-rule": cmd_test_rule,
        "delete-test": cmd_delete_test,
        "userids": cmd_userids,
    }.get(cmd, cmd_build)()


if __name__ == "__main__":
    main()
