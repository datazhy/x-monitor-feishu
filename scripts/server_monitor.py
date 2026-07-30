#!/usr/bin/env python3
"""服务器流量 + 资源占用监控 -> 飞书告警群。

数据源：
  - 月流量：KiwiVM API（getServiceInfo，官方计费口径，与面板一致）
  - 资源占用：本机 /proc（CPU / 内存 / 磁盘 / 负载 / 运行时长）

阈值：流量用量跨过 80% / 95% 时各推一次（不刷屏），新计费周期自动重置。

设计为「零三方依赖」纯标准库脚本，可直接用宿主机 system python3 跑 cron，
不依赖项目 venv / Docker。配置从同目录上层的 .env 读取（也读环境变量）。

用法：
  python3 scripts/server_monitor.py            # 正常巡检：跨阈值才推送
  python3 scripts/server_monitor.py --test     # 强制推送一张当前状态卡（自检用）
  python3 scripts/server_monitor.py --dry-run  # 只打印不推送
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "data" / "server_monitor_state.json"
BEIJING = timezone(timedelta(hours=8))


def load_env() -> dict:
    """读取 .env（不覆盖已存在的真实环境变量），返回合并后的配置字典。"""
    cfg: dict = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items()})  # 环境变量优先
    return cfg


# ---------------------------------------------------------------------------
# KiwiVM API
# ---------------------------------------------------------------------------
def kiwivm_service_info(veid: str, api_key: str, timeout: float = 15.0) -> dict:
    q = urllib.parse.urlencode({"veid": veid, "api_key": api_key})
    url = f"https://api.64clouds.com/v1/getServiceInfo?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "x-monitor/server_monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(f"KiwiVM API error={data.get('error')}: {data.get('message')}")
    return data


# ---------------------------------------------------------------------------
# 本机资源占用（纯 /proc，无三方依赖）
# ---------------------------------------------------------------------------
def _cpu_snapshot() -> tuple[int, int]:
    """返回 (idle, total) 累计 jiffies。"""
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return idle, sum(vals)


def cpu_percent(interval: float = 1.0) -> float:
    try:
        i1, t1 = _cpu_snapshot()
        time.sleep(interval)
        i2, t2 = _cpu_snapshot()
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return round((1 - (i2 - i1) / dt) * 100, 1)
    except Exception:
        return -1.0


def mem_info() -> dict:
    """返回内存 used/total/pct（MiB）。"""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        pct = round(used / total * 100, 1) if total else 0.0
        return {"used_mib": round(used / 1024, 1), "total_mib": round(total / 1024, 1), "pct": pct}
    except Exception:
        return {"used_mib": -1, "total_mib": -1, "pct": -1.0}


def disk_info(path: str = "/") -> dict:
    try:
        import shutil

        total, used, free = shutil.disk_usage(path)
        pct = round(used / total * 100, 1) if total else 0.0
        return {"used_gib": round(used / 1024**3, 1), "total_gib": round(total / 1024**3, 1), "pct": pct}
    except Exception:
        return {"used_gib": -1, "total_gib": -1, "pct": -1.0}


def load_and_uptime() -> dict:
    out = {"load": "n/a", "ncpu": os.cpu_count() or 1, "uptime": "n/a"}
    try:
        out["load"] = ", ".join(f"{x:.2f}" for x in os.getloadavg())
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            secs = float(f.readline().split()[0])
        d, rem = divmod(int(secs), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        out["uptime"] = f"{d}天{h}小时{m}分"
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 流量计算 + 月底预估
# ---------------------------------------------------------------------------
GiB = 1024**3


def human_gib(n: int) -> str:
    return f"{n / GiB:.1f} GiB"


def analyze_traffic(info: dict, limit_override_bytes: int | None) -> dict:
    used = int(info.get("data_counter", 0))
    limit = limit_override_bytes or int(info.get("plan_monthly_data", 0))
    pct = round(used / limit * 100, 2) if limit else 0.0
    next_reset = int(info.get("data_next_reset", 0))

    now = int(time.time())
    # 计费周期近似按 30.44 天（KiwiVM 按月重置）；预估月底用量 = 已用 / 已过比例
    period = 30.44 * 86400
    start = next_reset - period
    elapsed = max(now - start, 1)
    frac = min(max(elapsed / period, 0.001), 1.0)
    projected = used / frac
    proj_pct = round(projected / limit * 100, 1) if limit else 0.0

    days_left = max((next_reset - now) / 86400, 0)
    return {
        "used": used,
        "limit": limit,
        "pct": pct,
        "next_reset": next_reset,
        "days_left": round(days_left, 1),
        "projected": projected,
        "proj_pct": proj_pct,
    }


# ---------------------------------------------------------------------------
# 阈值状态（跨档去重）
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def current_tier(pct: float, warn: float, crit: float) -> int:
    if pct >= crit:
        return 95
    if pct >= warn:
        return 80
    return 0


# ---------------------------------------------------------------------------
# 飞书推送（交互卡片，复用项目同款 HMAC 签名）
# ---------------------------------------------------------------------------
def _sign(secret: str, ts: int) -> str:
    digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def feishu_send_card(webhook: str, secret: str, card: dict, timeout: float = 10.0) -> None:
    payload: dict = {"msg_type": "interactive", "card": card}
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(secret, ts)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    code = data.get("code", data.get("StatusCode", 0))
    if code not in (0, None):
        raise RuntimeError(f"Feishu error code={code}: {data.get('msg') or data}")


def build_card(info: dict, traf: dict, res: dict, tier: int, periodic: bool = False) -> dict:
    if tier == 95:
        template, level = "red", "🔴 严重"
    elif tier == 80:
        template, level = "orange", "🟠 警告"
    elif periodic:
        template, level = "turquoise", "🗓 定期巡检"
    else:
        template, level = "blue", "🔵 状态"

    host = info.get("hostname", "?")
    loc = info.get("node_location", "?")
    reset_str = datetime.fromtimestamp(traf["next_reset"], BEIJING).strftime("%Y-%m-%d %H:%M")
    now_str = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    bar_len = 20
    filled = min(int(traf["pct"] / 100 * bar_len), bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)

    traffic_md = (
        f"**📊 月流量用量：{traf['pct']:.2f}%**\n"
        f"`{bar}`\n"
        f"已用 **{human_gib(traf['used'])}** / 配额 {human_gib(traf['limit'])}\n"
        f"剩余 **{human_gib(traf['limit'] - traf['used'])}**，距重置还有 **{traf['days_left']:.1f} 天**（{reset_str}）\n"
        f"按当前速度月底预估 **{traf['proj_pct']:.0f}%**（{human_gib(int(traf['projected']))}）"
    )

    cpu = res["cpu"]
    mem = res["mem"]
    disk = res["disk"]
    lu = res["lu"]
    res_md = (
        f"**🖥 资源占用**\n"
        f"CPU：{cpu if cpu >= 0 else 'n/a'}%　|　负载：{lu['load']}（{lu['ncpu']} 核）\n"
        f"内存：{mem['pct']}%（{mem['used_mib']:.0f} / {mem['total_mib']:.0f} MiB）\n"
        f"磁盘 /：{disk['pct']}%（{disk['used_gib']:.1f} / {disk['total_gib']:.1f} GiB）\n"
        f"运行时长：{lu['uptime']}"
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"{level} | 服务器巡检 {host}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**主机**：{host}（{loc}）"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": traffic_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": res_md}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"巡检时间 {now_str}（北京）"}]},
        ],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="服务器流量+资源监控 -> 飞书告警群")
    ap.add_argument("--test", action="store_true", help="强制推送一张当前状态卡（自检）")
    ap.add_argument("--report", action="store_true", help="定期巡检：强制推送状态卡，不影响阈值去重")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送、不写状态")
    args = ap.parse_args()
    force = args.test or args.report

    cfg = load_env()
    veid = cfg.get("KIWIVM_VEID", "")
    api_key = cfg.get("KIWIVM_API_KEY", "")
    if not veid or not api_key:
        print("❌ 缺少 KIWIVM_VEID / KIWIVM_API_KEY（请在 .env 配置）", file=sys.stderr)
        return 2

    warn = float(cfg.get("BANDWIDTH_WARN_PCT", "80"))
    crit = float(cfg.get("BANDWIDTH_CRIT_PCT", "95"))
    limit_override = cfg.get("BANDWIDTH_LIMIT_BYTES", "").strip()
    limit_override_bytes = int(limit_override) if limit_override else None

    webhook = cfg.get("FEISHU_ALERT_WEBHOOK_URL") or cfg.get("FEISHU_WEBHOOK_URL", "")
    secret = cfg.get("FEISHU_ALERT_SECRET") or (
        cfg.get("FEISHU_SECRET", "") if not cfg.get("FEISHU_ALERT_WEBHOOK_URL") else ""
    )
    if not webhook or "CHANGE_ME" in webhook:
        print("❌ 未配置飞书告警 webhook", file=sys.stderr)
        return 2

    # 采集
    try:
        info = kiwivm_service_info(veid, api_key)
    except Exception as e:
        print(f"❌ KiwiVM 查询失败：{e}", file=sys.stderr)
        return 1
    traf = analyze_traffic(info, limit_override_bytes)
    res = {
        "cpu": cpu_percent(1.0),
        "mem": mem_info(),
        "disk": disk_info("/"),
        "lu": load_and_uptime(),
    }

    tier = current_tier(traf["pct"], warn, crit)
    print(
        f"[{datetime.now(BEIJING):%F %T}] 流量 {traf['pct']:.2f}% "
        f"({human_gib(traf['used'])}/{human_gib(traf['limit'])}) 档位={tier} "
        f"预估月底={traf['proj_pct']:.0f}% | CPU {res['cpu']}% 内存 {res['mem']['pct']}% 磁盘 {res['disk']['pct']}%"
    )

    if args.dry_run:
        print("（dry-run，不推送）")
        return 0

    # 跨档去重：state 按计费周期(next_reset)记录已告警的最高档
    state = load_state()
    period_key = str(traf["next_reset"])
    if state.get("period") != period_key:  # 新周期，重置
        state = {"period": period_key, "alerted_tier": 0}

    should_push = force or (tier > 0 and tier > int(state.get("alerted_tier", 0)))

    if should_push:
        try:
            tag = "，--report" if args.report else ("，--test" if args.test else "")
            feishu_send_card(webhook, secret, build_card(info, traf, res, tier, periodic=args.report))
            print(f"✅ 已推送飞书告警群（档位 {tier}{tag}）")
        except Exception as e:
            print(f"❌ 飞书推送失败：{e}", file=sys.stderr)
            return 1

    # 记录档位（仅真实跨档时更新；--test/--report 不改变去重状态）
    if not force:
        state["alerted_tier"] = max(int(state.get("alerted_tier", 0)), tier)
        save_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
