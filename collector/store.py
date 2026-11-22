"""去重、状态持久化与事件结构。

GitHub Actions 是无状态环境：去重状态和历史基线必须落到文件并跨轮次恢复。
本模块提供两条路径：
- `StateStore`：单一 JSON 文件（推荐配合 actions/cache 或发布到公开仓）。
- `events.json` 合并：保留最近 N 条，按稳定 id 去重，跨源同链接合并。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

CST = timezone(timedelta(hours=8))
UTC = timezone.utc

STATE_RETENTION_DAYS = 7
MAX_EVENTS_HISTORY = 1000
DEFAULT_STATE_FILE = "state.json"


def normalize_url(url: str) -> str:
    """URL 归一：去协议差异、去 utm 参数、统一主机大小写；path/query 保留大小写。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^www\.", "", raw)
    raw = raw.split("#", 1)[0]
    if "?" in raw:
        path, query = raw.split("?", 1)
        kept = [
            part
            for part in query.split("&")
            if part and not part.lower().startswith(("utm_", "ref=", "spm="))
        ]
        raw = path + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/").lower() if raw.count("/") == 0 else raw.rstrip("/")


def stable_id(source: str, url: str, title: str, published: str) -> str:
    """稳定事件 id：优先用 URL；URL 缺失时用来源+标题+时间的哈希。"""
    key = normalize_url(url)
    if key:
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    else:
        raw = f"{source}|{(title or '').strip().lower()}|{(published or '')[:16]}"
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return digest


def title_key(title: str) -> str:
    """标题归一：去标点与空白、转小写，用于同标题跨源合并。"""
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Event:
    id: str
    title: str
    url: str
    source: str
    published_at: str = ""
    summary: str = ""
    category: str = ""
    importance: str = "low"
    time_quality: str = "ok"
    reason: str = ""
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "summary": self.summary,
            "category": self.category,
            "importance": self.importance,
            "time_quality": self.time_quality,
            "reason": self.reason,
            "tags": self.tags,
        }

    @staticmethod
    def from_dict(raw: Dict[str, object]) -> "Event":
        return Event(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            url=str(raw.get("url") or ""),
            source=str(raw.get("source") or ""),
            published_at=str(raw.get("published_at") or ""),
            summary=str(raw.get("summary") or ""),
            category=str(raw.get("category") or ""),
            importance=str(raw.get("importance") or "low"),
            time_quality=str(raw.get("time_quality") or "ok"),
            reason=str(raw.get("reason") or ""),
            tags=list(raw.get("tags") or []),
        )


class StateStore:
    """跨轮次持久化的去重状态。

    结构：{"seen": {id: {"first_seen": iso, "published_at": iso}}, "provider_errors": {name: {...}}}
    """

    def __init__(self, path: str = DEFAULT_STATE_FILE, retention_days: int = STATE_RETENTION_DAYS):
        self.path = Path(path)
        self.retention_days = retention_days
        self.seen: Dict[str, Dict[str, str]] = {}
        self.provider_errors: Dict[str, Dict[str, str]] = {}

    def load(self) -> "StateStore":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = dict(raw.get("seen") or {})
        self.provider_errors = dict(raw.get("provider_errors") or {})
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "seen": self.seen,
            "provider_errors": self.provider_errors,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def is_seen(self, event_id: str) -> bool:
        return event_id in self.seen

    def mark(self, event_id: str, published_at: str = "") -> None:
        self.seen[event_id] = {
            "first_seen": datetime.now(UTC).isoformat(),
            "published_at": published_at or "",
        }

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        stale = []
        for key, meta in self.seen.items():
            stamp = _parse_iso(meta.get("first_seen", ""))
            if stamp is not None and stamp < cutoff:
                stale.append(key)
        for key in stale:
            self.seen.pop(key, None)
        return len(stale)

    def set_provider_error(self, name: str, error_type: str, error: str) -> None:
        self.provider_errors[name] = {
            "error_type": error_type,
            "error": error[:500],
            "at": datetime.now(UTC).isoformat(),
        }

    def clear_provider_error(self, name: str) -> None:
        self.provider_errors.pop(name, None)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def merge_events(
    existing: Sequence[Event],
    incoming: Sequence[Event],
    *,
    cap: int = MAX_EVENTS_HISTORY,
) -> List[Event]:
    """合并事件：按 id 去重，同 id 保留信息更完整的一条，按发布时间倒序，截断到 cap。"""
    by_id: Dict[str, Event] = {}
    for event in list(existing) + list(incoming):
        if not event.id:
            continue
        old = by_id.get(event.id)
        if old is None:
            by_id[event.id] = event
            continue
        keep = old
        if len(event.summary) > len(old.summary):
            keep = event
        elif len(event.summary) == len(old.summary) and event.published_at > old.published_at:
            keep = event
        merged = Event(
            id=event.id,
            title=keep.title or old.title,
            url=keep.url or old.url,
            source=old.source if keep is event else keep.source,
            published_at=max(old.published_at, event.published_at),
            summary=keep.summary or old.summary,
            category=keep.category or old.category,
            importance=old.importance if old.importance != "low" else keep.importance,
            time_quality=old.time_quality if old.time_quality == "ok" else keep.time_quality,
            reason=keep.reason or old.reason,
            tags=list(dict.fromkeys((old.tags or []) + (keep.tags or []))),
        )
        by_id[event.id] = merged

    ordered = sorted(by_id.values(), key=lambda e: e.published_at or "", reverse=True)
    return ordered[:cap]


def load_events(path: str) -> List[Event]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw if isinstance(raw, list) else raw.get("events") or []
    return [Event.from_dict(x) for x in items if isinstance(x, dict)]


def save_events(path: str, events: Sequence[Event]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [e.to_dict() for e in events]
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
