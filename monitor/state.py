"""跨轮状态（保存在 state 分支）：去重链接、当天搜过的关键词、源健康。

不用 Actions cache；每轮先恢复、结束再提交。没有新字段以外的额外持久化需求。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from collector import store

UTC = timezone.utc
SEEN_RETENTION_DAYS = 30
SEARCH_RETENTION_DAYS = 7

#: 这些查询参数不影响帖子本身（翻页、语言、来源标记），去重时要忽略。
NOISE_QUERY_KEYS = {
    "page", "p", "tl", "ref", "from", "sort", "order", "start", "offset",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "share_token", "_t",
}


def canonical_url(url: str) -> str:
    """去掉翻页/追踪参数后的规范链接，用来判重。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    kept = []
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in NOISE_QUERY_KEYS or key.startswith("utm_"):
            continue
        kept.append(pair)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(sorted(kept)), ""))


def today_utc(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


class MonitorState:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: Dict[str, dict] = {}
        self.searched: Dict[str, Dict[str, dict]] = {}
        self.topics: Dict[str, dict] = {}
        self.round_no: int = 0
        self.source_failures: Dict[str, dict] = {}
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "MonitorState":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = dict(raw.get("seen") or {})
        self.searched = dict(raw.get("searched") or {})
        self.topics = dict(raw.get("topics") or {})
        self.round_no = int(raw.get("round_no") or 0)
        self.source_failures = dict(raw.get("source_failures") or {})
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()
        payload = {
            "schema_version": "1.0.0",
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "seen": self.seen,
            "searched": self.searched,
            "topics": self.topics,
            "round_no": self.round_no,
            "source_failures": self.source_failures,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def is_seen(self, url: str) -> bool:
        return store.stable_id("keyword", canonical_url(url), "", "") in self.seen

    def mark_seen(self, *, url: str, title: str, source: str, keyword: str) -> None:
        key = store.stable_id("keyword", canonical_url(url), "", "")
        self.seen[key] = {
            "url": url, "title": title[:300], "source": source, "keyword": keyword,
            "first_seen": datetime.now(UTC).isoformat(),
        }

    def keyword_searched(self, keyword: str, day: Optional[str] = None) -> bool:
        return keyword in self.searched.get(day or today_utc(), {})

    def mark_keyword(self, keyword: str, urls: List[str], day: Optional[str] = None) -> None:
        bucket = self.searched.setdefault(day or today_utc(), {})
        bucket[keyword] = {"at": datetime.now(UTC).isoformat(), "hits": urls[:200]}

    def add_source_status(self, name: str, ok: bool, error_type: str = "", error: str = "") -> Optional[str]:
        entry = dict(self.source_failures.get(name) or {})
        if ok:
            self.source_failures.pop(name, None)
            return None
        count = int(entry.get("count") or 0) + 1
        self.source_failures[name] = {
            "count": count, "error_type": error_type,
            "last_error": str(error)[:300], "at": datetime.now(UTC).isoformat(),
        }
        if count == 3:
            return f"来源连续 3 轮失败：{name}（{error_type}）"
        return None

    def begin_round(self) -> int:
        """每轮开始时给一个轮次号，用于统计话题连续出现几轮。"""
        self.round_no += 1
        return self.round_no

    def topic_history(self, key: str) -> dict:
        return dict(self.topics.get(key) or {})

    def record_topic(self, key: str, *, title: str, url: str, platforms: List[str],
                     round_no: int) -> dict:
        """记录话题连续出现的轮数：上一轮也出现过才算连续。"""
        entry = dict(self.topics.get(key) or {})
        last_round = int(entry.get("last_round") or 0)
        consecutive = int(entry.get("consecutive") or 0) + 1 if last_round == round_no - 1 else 1
        entry.update({
            "title": title[:300], "url": url,
            "rounds": int(entry.get("rounds") or 0) + 1,
            "consecutive": consecutive,
            "last_round": round_no,
            "first_round": entry.get("first_round") or round_no,
            "platforms": sorted(set(entry.get("platforms") or []) | set(platforms)),
            "updated_at": datetime.now(UTC).isoformat(),
        })
        self.topics[key] = entry
        return entry

    def prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=SEEN_RETENTION_DAYS)
        for key, meta in list(self.seen.items()):
            try:
                stamp = datetime.fromisoformat(str(meta.get("first_seen") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp < cutoff:
                self.seen.pop(key, None)
        search_cutoff = datetime.now(UTC) - timedelta(days=SEARCH_RETENTION_DAYS)
        for day in list(self.searched):
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                self.searched.pop(day, None)
                continue
            if parsed < search_cutoff:
                self.searched.pop(day, None)
        topic_cutoff = datetime.now(UTC) - timedelta(days=SEEN_RETENTION_DAYS)
        for key, meta in list(self.topics.items()):
            try:
                stamp = datetime.fromisoformat(str(meta.get("updated_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp < topic_cutoff:
                self.topics.pop(key, None)
