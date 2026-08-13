"""昨日信号 · 早报管线。

流程：收集昨日原创推文 → DeepSeek 去噪/分类/主题聚合 → Python 算硬指标(热度/趋势/连续/异常)
→ 筛 30-80 条重点 → DeepSeek 写最终早报 JSON → 渲染飞书交互卡片 → 推送 + 存档。

分工：DeepSeek(flash) 便宜处理全量、出结构化标签；Python 算确定性数字；DeepSeek(pro) 负责判断与写作。
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db, feishu, llm, textutils
from .config import get_settings

log = logging.getLogger(__name__)

_BJ = timezone(timedelta(hours=8))

_DS_SYS = (
    "你是中文资讯预处理引擎。对用户给出的推文数组逐条分析，返回 JSON 对象 "
    '{"items":[...]}。每个元素字段：'
    "tweet_id(原样), author(原样), clean_summary(中文一句话概括), topic(中文主题词，尽量归并同义主题), "
    "entities(关键实体数组), type(news|opinion|product|market|tutorial|casual|duplicate|low_info), "
    "importance(1-5整数，5最重要), reason(中文，为何重要或为何是噪音), is_noise(true/false)。"
    "闲聊/纯互动/无新增信息/重复转述判为 is_noise=true。只输出 JSON。"
)

_WRITER_SYS = (
    "你是个人情报简报撰稿人，产品名叫「昨日信号」。基于用户提供的【已结构化的昨日推文要点与统计事实】，"
    "写一份中文早报，结论先行、有重点有取舍。严格要求：\n"
    "1) 只依据提供的内容，不得编造价格、数字或事实，不给任何买卖/投资建议。\n"
    "2) topic_heat 里的 tweet_count/author_count 必须原样使用我给的数值，不得改动。\n"
    "3) 正文总长控制在 600-1000 字。\n"
    "4) missed_items 从“高价值但未进前三件事”的要点里挑 1-3 条（近似“你可能错过了”）。\n"
    "5) continuous_signals / anomaly_signals / missed_items 三者都是【中文完整句子的字符串数组】，"
    "每个元素是一句可直接展示的中文，禁止输出对象/字段名/英文 key。\n"
    "6) mentioned_stocks：只能使用我在 detected_tickers 里给出的股票代码，逐个补一个简短的中文/英文公司名"
    "（不确定就把 name 留空字符串）；不得新增、改写或杜撰任何代码，也不要改变代码本身。\n"
    "7) 严格输出如下 JSON（不要多余字段、不要 markdown）：\n"
    '{"tldr":"50字内一句话主线","top_events":[{"title":"","what_happened":"","why_it_matters":"",'
    '"related_authors":[],"source_tweet_ids":[]}],'
    '"topic_heat":[{"topic":"","tweet_count":0,"author_count":0,"trend":"rising|stable|falling","summary":""}],'
    '"mentioned_stocks":[{"ticker":"MU","name":"美光"}],'
    '"continuous_signals":["某主题已连续N天出现，讨论重心从X转向Y。"],'
    '"anomaly_signals":["某博主昨日发推N条明显高于平时，集中在X。"],'
    '"missed_items":["一条高价值但未上头条的动态。"]}'
)

# cashtag：$AAPL / $BRK.B 这类。用于确定性提取"提及股票"，不依赖 LLM、不会臆造。
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})(?:\.([A-Za-z]{1,2}))?\b")


def extract_tickers(tweets: list[dict], top: int = 30) -> list[tuple[str, int]]:
    """从原始推文正文提取 $代码，按出现频次降序返回 [(代码, 次数)]。"""
    cnt: dict[str, int] = defaultdict(int)
    for t in tweets:
        for m in _CASHTAG_RE.finditer(t.get("text") or ""):
            sym = m.group(1).upper() + (f".{m.group(2).upper()}" if m.group(2) else "")
            cnt[sym] += 1
    return sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:top]


def yesterday_bj() -> str:
    return (datetime.now(_BJ) - timedelta(days=1)).strftime("%Y-%m-%d")


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def collect(day: str) -> list[dict]:
    """取北京日期 == day 的纯原创推文。"""
    rows = db.get_conn().execute(
        "SELECT tweet_id, author_handle, author_name, text, created_at, tweet_url "
        "FROM tweets WHERE is_reply=0 AND is_retweet=0 AND is_quote=0"
    ).fetchall()
    out = []
    for r in rows:
        if textutils.to_beijing(r["created_at"]).startswith(day):
            out.append(dict(r))
    return out


def classify(tweets: list[dict]) -> list[dict]:
    """DeepSeek 批量去噪/分类/打标签。"""
    items: list[dict] = []
    for chunk in _chunks(tweets, 40):
        payload = [
            {"tweet_id": t["tweet_id"], "author": t["author_handle"], "text": (t["text"] or "")[:600]}
            for t in chunk
        ]
        try:
            raw = llm.deepseek_json(_DS_SYS, json.dumps(payload, ensure_ascii=False), max_tokens=8000)
            data = json.loads(raw)
            items.extend(data.get("items") if isinstance(data, dict) else data)
        except Exception as e:
            log.warning("DeepSeek 分类失败(跳过该批): %s", e)
    return items


def aggregate(classified: list[dict], day: str) -> tuple[list[dict], dict, list[dict]]:
    """算主题热度(含趋势)、噪音统计、筛重点推文。"""
    settings = get_settings()
    prev = db.prev_day_topics(day)

    topic_tw: dict[str, int] = defaultdict(int)
    topic_au: dict[str, set] = defaultdict(set)
    noise = {"casual": 0, "duplicates": 0, "low_information": 0, "replies_or_interactions": 0}

    for c in classified:
        typ = c.get("type")
        if c.get("is_noise") or typ in ("casual", "duplicate", "low_info"):
            if typ == "duplicate":
                noise["duplicates"] += 1
            elif typ == "low_info":
                noise["low_information"] += 1
            else:
                noise["casual"] += 1
            continue
        tp = (c.get("topic") or "其他").strip()
        topic_tw[tp] += 1
        topic_au[tp].add(c.get("author"))

    topic_stats = []
    for tp, n in topic_tw.items():
        p = prev.get(tp)
        trend = "stable" if p is None else ("rising" if n > p else "falling" if n < p else "stable")
        topic_stats.append({"topic": tp, "tweet_count": n, "author_count": len(topic_au[tp]), "trend": trend})
    topic_stats.sort(key=lambda x: (-x["tweet_count"], -x["author_count"]))

    selected = sorted(
        [c for c in classified if not c.get("is_noise") and c.get("type") not in ("casual", "duplicate", "low_info")],
        key=lambda x: -int(x.get("importance", 1) or 1),
    )[: settings.report_select_max]
    return topic_stats, noise, selected


def continuity(day: str, topic_stats: list[dict]) -> list[dict]:
    """连续信号：最近若干天里出现 >=2 天的主题。"""
    settings = get_settings()
    hist = db.topic_history(settings.report_trend_days)
    today_topics = {t["topic"] for t in topic_stats}
    out = []
    for tp in today_topics:
        days = set(hist.get(tp, []))
        days.add(day)
        if len(days) >= 2:
            out.append({"topic": tp, "days": len(days)})
    out.sort(key=lambda x: -x["days"])
    return out[:6]


def anomalies(tweets: list[dict], classified: list[dict], topic_stats: list[dict], day: str) -> list[str]:
    """异常信号候选（Python 算事实，交 DeepSeek 润色）。"""
    hints: list[str] = []
    cnt: dict[str, int] = defaultdict(int)
    for t in tweets:
        cnt[t["author_handle"]] += 1
    for a, n in sorted(cnt.items(), key=lambda x: -x[1]):
        if n >= 4:
            hints.append(f"博主 @{a} 昨日发推 {n} 条，明显高于平时")
    prev = db.prev_day_topics(day)
    for ts in topic_stats:
        if ts["author_count"] >= 3 and ts["topic"] not in prev:
            hints.append(f"主题「{ts['topic']}」昨日突然被 {ts['author_count']} 位博主同时提及（前一日未出现）")
    return hints[:5]


def preference_topics() -> list[str]:
    """近似用户偏好：历史高频主题 Top5。"""
    hist = db.topic_history(get_settings().report_trend_days)
    return [tp for tp, _ in sorted(hist.items(), key=lambda x: -len(x[1]))[:5]]


def write_report(day: str, selected, topic_stats, cont, anom, prefs, id2url, tickers=None) -> dict:
    """DeepSeek 写最终早报 JSON。"""
    facts = {
        "date": day,
        "selected_tweets": [
            {
                "tweet_id": c.get("tweet_id"),
                "author": c.get("author"),
                "summary": c.get("clean_summary"),
                "topic": c.get("topic"),
                "importance": c.get("importance"),
            }
            for c in selected
        ],
        "topic_heat": topic_stats[:8],
        # 已从原文确定性提取的股票代码（按频次降序），DeepSeek 只需补公司名、不得改动
        "detected_tickers": [sym for sym, _ in (tickers or [])],
        "continuity": cont,
        "anomaly_hints": anom,
        "user_preference_topics": prefs,
    }
    user = json.dumps(facts, ensure_ascii=False)
    # LLM 输出 JSON 偶发格式错误，重试 3 次并做基础修复
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = llm.report_json(_WRITER_SYS, user, max_completion_tokens=8000)
            return json.loads(_repair_json(raw))
        except json.JSONDecodeError as e:
            last_err = e
            log.warning("早报 JSON 解析失败(第 %d 次)：%s", attempt + 1, e)
    raise RuntimeError(f"早报 JSON 连续 3 次解析失败：{last_err}")


def _repair_json(raw: str) -> str:
    """去掉 markdown 围栏、截取最外层大括号，修掉常见的尾随逗号。"""
    import re

    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i : j + 1]
    return re.sub(r",(\s*[}\]])", r"\1", s)  # 尾随逗号


# ---------------- 飞书卡片渲染 ----------------
def _stock_line(tickers, rep, cap: int = 15) -> str | None:
    """把提及股票渲染成一行：公司名 $代码，按频次降序。代码来自原文正则，名称来自 DeepSeek。"""
    if not tickers:
        return None
    names: dict[str, str] = {}
    for it in rep.get("mentioned_stocks", []) or []:
        if isinstance(it, dict) and it.get("ticker"):
            names[str(it["ticker"]).upper().lstrip("$")] = (it.get("name") or "").strip()
    parts = []
    for sym, _ in tickers[:cap]:
        nm = names.get(sym, "")
        parts.append(f"{nm} ${sym}" if nm else f"${sym}")
    line = " · ".join(parts)
    if len(tickers) > cap:
        line += f" …等 {len(tickers)} 只"
    return line


def _card(day: str, rep: dict, noise: dict, id2url: dict, tickers=None) -> dict:
    md_title = f"昨日信号 · {_fmt_cn_date(day)}早报"
    elems: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**AI 提炼你关注博主昨日发布的关键信号**\n统计区间：{_fmt_cn_date(day)} 00:00-23:59，北京时间"}},
    ]
    stock_line = _stock_line(tickers or [], rep)
    if stock_line:
        elems.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**📈 提及股票**\n{stock_line}"}})
    elems += [
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**📌 今日一句**\n{rep.get('tldr','')}"}},
    ]

    # 三件最重要的事
    ev_lines = ["**🔥 三件最重要的事**"]
    for i, e in enumerate(rep.get("top_events", [])[:3], 1):
        authors = "、".join(f"@{a}" for a in e.get("related_authors", []))
        srcs = [id2url.get(str(s)) for s in e.get("source_tweet_ids", []) if id2url.get(str(s))]
        src_md = "  ".join(f"[原推{j}]({u})" for j, u in enumerate(srcs[:3], 1))
        ev_lines.append(
            f"**{i}. {e.get('title','')}**\n"
            f"· 发生了什么：{e.get('what_happened','')}\n"
            f"· 为什么重要：{e.get('why_it_matters','')}"
            + (f"\n· 相关博主：{authors}" if authors else "")
            + (f"\n· {src_md}" if src_md else "")
        )
    elems += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(ev_lines)}}]

    # 主题热度
    th = rep.get("topic_heat", [])
    if th:
        tmap = {"rising": "↑升温", "falling": "↓降温", "stable": "→持平"}
        lines = ["**📊 主题热度**"]
        for t in th[:5]:
            lines.append(
                f"· **{t.get('topic','')}**（{t.get('tweet_count',0)}条/{t.get('author_count',0)}位 "
                f"{tmap.get(t.get('trend','stable'),'')}）：{t.get('summary','')}"
            )
        elems += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]

    # 连续信号 / 异常信号 / 你可能错过了
    for title, key, prefix in [
        ("🔁 连续信号", "continuous_signals", "· "),
        ("⚠️ 异常信号", "anomaly_signals", "· "),
        ("👀 你可能错过了", "missed_items", "· "),
    ]:
        vals = rep.get(key, [])
        if vals:
            body = "\n".join(prefix + _fmt_signal(v) for v in vals[:4])
            elems += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{body}"}}]

    # 折叠噪音（footer note）
    elems.append({
        "tag": "note",
        "elements": [{"tag": "lark_md", "content":
            f"已折叠噪音：闲聊 {noise.get('casual',0)} · 重复 {noise.get('duplicates',0)} · "
            f"无新增 {noise.get('low_information',0)} 条"}],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": md_title}},
        "elements": elems,
    }


def _fmt_signal(v) -> str:
    """把信号项渲染成一句中文。兼容模型偶尔返回 dict（提取描述，不露英文字段名）。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        desc = (v.get("description") or v.get("text") or v.get("summary") or v.get("content") or "").strip()
        author = v.get("author")
        topic = v.get("topic") or v.get("title")
        days = v.get("days")
        if author:
            head = f"@{author}"
        elif topic:
            head = f"「{topic}」" + (f"连续{days}天" if days else "")
        else:
            head = ""
        return f"{head}：{desc}" if head and desc else (desc or head or "")
    return str(v)


def _fmt_cn_date(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.month} 月 {d.day} 日"
    except ValueError:
        return day


# ---------------- 编排 ----------------
def generate_and_send(day: str | None = None) -> dict:
    settings = get_settings()
    if not settings.report_enabled:
        return {"skipped": "report_disabled"}
    day = day or yesterday_bj()

    tweets = collect(day)
    id2url = {str(t["tweet_id"]): t["tweet_url"] for t in tweets}

    if not tweets:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"昨日信号 · {_fmt_cn_date(day)}早报"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "昨日没有抓到关注博主的原创推文。"}}],
        }
        feishu.send_card(settings.feishu_webhook_url, card, settings.feishu_secret)
        db.log_event("report", True, f"{day} 无推文")
        return {"day": day, "tweets": 0}

    classified = classify(tweets)
    topic_stats, noise, selected = aggregate(classified, day)
    cont = continuity(day, topic_stats)
    anom = anomalies(tweets, classified, topic_stats, day)
    prefs = preference_topics()
    tickers = extract_tickers(tweets)

    rep = write_report(day, selected, topic_stats, cont, anom, prefs, id2url, tickers)

    card = _card(day, rep, noise, id2url, tickers)
    feishu.send_card(settings.feishu_webhook_url, card, settings.feishu_secret)

    # 存档 + 趋势记忆
    db.save_daily_topics(day, topic_stats)
    full = {"day": day, "report": rep, "topic_stats": topic_stats, "noise": noise}
    db.save_daily_report(day, json.dumps(full, ensure_ascii=False))
    db.log_event("report", True, f"{day} 推文{len(tweets)} 入选{len(selected)} 主题{len(topic_stats)}")
    log.info("早报已生成并推送：%s 推文%d 入选%d", day, len(tweets), len(selected))
    return {"day": day, "tweets": len(tweets), "selected": len(selected), "topics": len(topic_stats)}
