"""文本工具：中英文判定、去裸链、非空白计数、北京时间换算。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CJK = re.compile(r"[一-鿿]")
_EN = re.compile(r"[A-Za-z]")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_WS = re.compile(r"\s+")

_BEIJING = timezone(timedelta(hours=8))


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def has_english(text: str) -> bool:
    return bool(_EN.search(text or ""))


# 含至少一个小写字母的英文词：区分"英文散文"(the/dip/earnings) 与"纯 ticker/符号"($MU/$AAPL)
_EN_WORD = re.compile(r"[A-Za-z]*[a-z][A-Za-z]*")


def should_translate(text: str) -> bool:
    """是否需要 AI 翻译：只有『全英文』才翻译。

    - 含任意中文 → False（覆盖"大量中文 + 几个英文词"，直接推原文）
    - 无中文但只是零星 ticker/emoji/数字（不足 2 个英文散文词）→ False
    - 无中文且是成句英文 → True
    """
    if has_chinese(text):
        return False
    return len(_EN_WORD.findall(text or "")) >= 2


def strip_urls(text: str) -> str:
    """去掉正文里的裸链接，但保留原推的换行/段落排版。

    - 行内多余空格压成一个；逐行去首尾空白
    - 连续空行折叠为最多一个空行（保留段落分隔）
    """
    text = _URL.sub("", text or "")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue  # 跳过开头空行 + 折叠连续空行
        out.append(ln)
    return "\n".join(out).strip()


def nonspace_len(text: str) -> int:
    return len(_WS.sub("", text or ""))


def to_beijing(raw: str | None) -> str:
    """把推文时间换算成北京时间 'YYYY-MM-DD HH:MM:SS'。无法解析则原样返回。"""
    if not raw:
        return ""
    raw = str(raw).strip()
    dt = None
    # 1) Twitter 经典格式： 'Tue Dec 10 07:00:30 +0000 2024'
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    # 2) ISO8601： '2026-06-01T12:34:56Z' / '+00:00'
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
