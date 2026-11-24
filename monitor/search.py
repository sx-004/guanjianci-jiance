"""社区搜索：直读 Hacker News / 小众软件，云端搜索覆盖 X / V2EX / 少数派 / Reddit / linux.do。

只返回社区讨论帖的标题和原链接。评论数字段只留在 extra 里作参考，不参与筛选、不进推送。
官方站、新闻站、榜单一律过滤掉；标签页、搜索页、用户主页这类「不是帖子」的链接也要过滤掉。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from collector import fetch

from .heat import word_match

EXA_MCP = "https://mcp.exa.ai/mcp"
ANYSEARCH_API = "https://api.anysearch.com/v1/search"
HN_API = "https://hn.algolia.com/api/v1/search"
APPINN_SEARCH = "https://meta.appinn.net/search.json"
APPINN_FAXIAN = "https://meta.appinn.net/c/faxian/10.json"
MAX_BYTES = 5 * 1024 * 1024
UTC = timezone.utc
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": BROWSER_UA,
}

COMMUNITY_DOMAINS = {
    "x.com": "X", "twitter.com": "X",
    "news.ycombinator.com": "Hacker News",
    "meta.appinn.net": "小众软件",
    "v2ex.com": "V2EX",
    "sspai.com": "少数派",
    "reddit.com": "Reddit",
    "linux.do": "linux.do",
}

#: 每个社区单独搜一次（走 Exa/Anysearch），比一把搜七个站更容易拿到每个社区的内容。
PLATFORM_QUERIES = [
    ("X", "site:x.com"),
    ("Hacker News", "site:news.ycombinator.com"),
    ("小众软件", "site:meta.appinn.net"),
    ("V2EX", "site:v2ex.com"),
    ("少数派", "site:sspai.com"),
    ("Reddit", "site:reddit.com"),
    ("linux.do", "site:linux.do"),
]


@dataclass
class Hit:
    keyword: str
    source: str
    title: str
    url: str
    snippet: str = ""
    published_at: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    error_type: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "error_type": self.error_type,
                "error": self.error[:200]}


CJK_RE = re.compile(r"[一-鿿]")


def has_chinese(text: str) -> bool:
    """有没有中文字符。"""
    return bool(CJK_RE.search(text or ""))


def chinese_ratio(text: str) -> float:
    """中文字符占比（去掉空白后算）。"""
    chars = [c for c in (text or "") if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars if CJK_RE.match(c))
    return cjk / len(chars)


def filter_non_chinese(hits, min_ratio: float = 0.05):
    """挡掉纯英文标题的帖子（用户 2026-11-18 拍板）。

    只看标题：标题里没有任何中文字符就丢，摘要里有没有中文都不算。
    用户在 Telegram 只看得到标题，标题是英文就是看不懂。
    """
    kept = []
    dropped = []
    for hit in hits:
        if has_chinese(hit.title):
            kept.append(hit)
        else:
            dropped.append(hit)
    return kept, dropped


def community_source(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    for domain, name in COMMUNITY_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def is_discussion_url(url: str) -> bool:
    """只认「帖子页」：标签页、搜索页、用户主页、导航页一律不算讨论帖。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").lower()

    def under(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if under("x.com") or under("twitter.com"):
        return "/status/" in path
    if under("news.ycombinator.com"):
        return "/item" in path
    if under("meta.appinn.net"):
        return path.startswith("/t/")
    if under("v2ex.com"):
        return bool(re.match(r"^/t/\d+", path))
    if under("sspai.com"):
        return "/post/" in path
    if under("reddit.com"):
        return "/comments/" in path
    if under("linux.do"):
        return "/t/topic/" in path
    return True


def keyword_hit(text: str, keyword: str) -> bool:
    """完整词边界命中：sec 不命中 second；Agent 可命中 agents。"""
    return word_match(text, keyword)


def _keyword_evidence(title: str, snippet: str, source: str, keyword: str) -> bool:
    """关键词必须出现在帖子里。

    普通社区看标题即可；X 没有标题字段，用正文开头一段（不拿评论区文字当证据）。
    """
    if keyword_hit(title, keyword):
        return True
    if source == "X":
        head = "\n".join(snippet.splitlines()[:3])[:300]
        return keyword_hit(head, keyword)
    return False


def _strip_markup(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_time(raw: str) -> str:
    """把 various 时间格式统一成 ISO 字符串；解析不出来就返回空串。"""
    text = (raw or "").strip()
    if not text or text.upper() in ("N/A", "NA", "NONE", "-"):
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def too_old(published_at: str, max_age_days: int) -> bool:
    """发布时间太旧的不算「新的帖子」；没有时间信息的保留（无法判断）。"""
    if not max_age_days or max_age_days <= 0 or not published_at:
        return False
    try:
        stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp < datetime.now(UTC) - timedelta(days=max_age_days)


def _post_json(url: str, payload: dict, headers: Optional[Dict[str, str]] = None,
               timeout: float = 25.0) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise RuntimeError("只允许 HTTPS 搜索接口")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = dict(JSON_HEADERS)
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("搜索接口响应超过上限")
    return data.decode("utf-8", "replace")


def _parse_sse_or_json(payload: str):
    if payload.lstrip().startswith("{"):
        return json.loads(payload)
    messages = []
    for line in payload.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                try:
                    messages.append(json.loads(raw))
                except ValueError:
                    continue
    return messages[-1] if messages else {}


def _mcp_call(endpoint: str, tool_name: str, arguments: dict, origin: str) -> str:
    init = _post_json(endpoint, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "guanjianci-jiance", "version": "1.0.0"}},
    }, {"Origin": origin})
    _parse_sse_or_json(init)
    result = _post_json(endpoint, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }, {"Origin": origin})
    data = _parse_sse_or_json(result)
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"MCP {tool_name} 返回错误")
    blocks = (data.get("result") or {}).get("content") or []
    return "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))


_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.M)
_URL_RE = re.compile(r"^URL:\s*(https?://\S+)", re.M)
_PUBLISHED_RE = re.compile(r"^Published:\s*(.+)$", re.M)
_SNIPPET_RE = re.compile(r"^Highlights:\s*(.*)$", re.M | re.S)


def _parse_exa_text(text: str, keyword: str, max_age_days: int = 0) -> List[Hit]:
    hits: List[Hit] = []
    for rank, chunk in enumerate(re.split(r"\n(?=Title:)", text), start=1):
        url_match = _URL_RE.search(chunk)
        if not url_match:
            continue
        url = url_match.group(1).rstrip(".,;)")
        title_match = _TITLE_RE.search(chunk)
        snippet_match = _SNIPPET_RE.search(chunk)
        published_match = _PUBLISHED_RE.search(chunk)
        raw_title = _strip_markup(title_match.group(1) if title_match else "")
        snippet = _strip_markup(snippet_match.group(1) if snippet_match else "")[:600]
        published = _parse_time(published_match.group(1) if published_match else "")
        source = community_source(url)
        if not source or not is_discussion_url(url):
            continue
        # X 没有标题字段：取正文第一行当标题；其它站点用搜索返回的标题。
        if source == "X":
            title = next((line.strip() for line in snippet.splitlines() if line.strip()), raw_title)
        else:
            title = raw_title or next((line.strip() for line in snippet.splitlines() if line.strip()), "")
        if not title:
            continue
        if not _keyword_evidence(title, snippet, source, keyword):
            continue
        if too_old(published, max_age_days):
            continue
        hits.append(Hit(keyword, source, title[:300], url, snippet, published,
                        extra={"rank": str(rank), "from_list": "0"}))
    return hits


def _parse_search_json(text: str, keyword: str, max_age_days: int = 0) -> List[Hit]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    hits: List[Hit] = []
    rows = []
    if isinstance(data, dict):
        rows = data.get("results") or data.get("hits") or data.get("topics") or []
        # Anysearch /v1/search 把结果包在 data.data.results 里。
        if not rows and isinstance(data.get("data"), dict):
            inner = data["data"]
            rows = inner.get("results") or inner.get("hits") or inner.get("topics") or []
    elif isinstance(data, list):
        rows = data
    for rank, row in enumerate(rows if isinstance(rows, list) else [], start=1):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("link") or "")
        if not url.startswith("http"):
            continue
        source = community_source(url)
        if not source or not is_discussion_url(url):
            continue
        snippet = _strip_markup(str(row.get("snippet") or row.get("content") or ""))[:600]
        title = _strip_markup(str(row.get("title") or ""))
        if source == "X" and not title:
            title = next((line.strip() for line in snippet.splitlines() if line.strip()), "")
        if not title:
            continue
        if not _keyword_evidence(title, snippet, source, keyword):
            continue
        published = _parse_time(str(row.get("publishedDate") or row.get("published_at") or ""))
        if too_old(published, max_age_days):
            continue
        hits.append(Hit(keyword, source, title[:300], url, snippet, published,
                        extra={"rank": str(rank), "from_list": "0"}))
    return hits


def _anysearch_keys() -> List[str]:
    raw = os.environ.get("ANYSEARCH_KEYS", "")
    return [key.strip() for key in raw.replace(";", ",").split(",") if key.strip()]


class AllKeysFailed(RuntimeError):
    """所有 Anysearch key 都失败（含配额耗尽）。"""


def _try_keys(attempt, keys):
    """按顺序尝试多把 key：402/配额耗尽自动换下一把，其他错误也依次尝试。

    背景（2026-11-25）：第一把 key 当日配额耗尽返回 402，但代码只取 keys[0]，
    导致六仓同时召回归零；#2~#4 明明可用却被闲置。
    """
    failures = []
    for index, key in enumerate(keys):
        try:
            return attempt(key), index
        except Exception as exc:  # noqa: BLE001 单把失败继续试下一把
            failures.append(f"key{index + 1}: {type(exc).__name__} {str(exc)[:100]}")
    raise AllKeysFailed("；".join(failures))


def cloud_search(keyword: str, max_age_days: int = 0,
                results_per_platform: int = 5) -> Tuple[List[Hit], SourceStatus]:
    """每个社区单独搜一次：Exa 公共 MCP 优先，失败或空结果时用 Anysearch 兜底。"""
    objective = (
        f"只返回社区里讨论 {keyword} 的帖子，必须有标题和原文链接；"
        "只要社区论坛、社交平台的讨论帖；"
        "排除官方站、新闻站、排行榜、帮助文档、产品介绍页；不要摘要评论内容。"
    )
    hits: List[Hit] = []
    errors: List[str] = []
    ok_platforms = 0
    for _, scope in PLATFORM_QUERIES:
        query = f"{keyword} {scope} 讨论 帖子"
        try:
            text = _mcp_call(EXA_MCP, "web_search_exa",
                             {"query": query, "objective": objective,
                              "numResults": results_per_platform},
                             "https://mcp.exa.ai")
            found = _parse_exa_text(text, keyword, max_age_days)
            if found:
                ok_platforms += 1
                hits.extend(found)
        except Exception as exc:  # noqa: BLE001 单个社区失败不能拖垮整轮
            errors.append(f"{scope}: {type(exc).__name__}: {str(exc)[:60]}")
    if ok_platforms:
        return hits, SourceStatus("exa", True)

    keys = _anysearch_keys()
    if not keys:
        detail = "；".join(errors[:3]) or "exa 返回 0 条社区命中"
        return [], SourceStatus("exa", False, "provider_error", detail)
    try:
        payload = {"query": f"{keyword} 社区讨论帖 site:x.com OR site:news.ycombinator.com "
                            f"OR site:meta.appinn.net OR site:v2ex.com OR site:sspai.com "
                            f"OR site:reddit.com OR site:linux.do", "max_results": 8}
        def _ask(key):
            raw = _post_json(ANYSEARCH_API, payload,
                             {"Authorization": f"Bearer {key}", "X-API-Key": key})
            return _parse_search_json(raw, keyword, max_age_days)

        hits, _used = _try_keys(_ask, keys)
        return hits, SourceStatus("anysearch", True)
    except Exception as exc:  # noqa: BLE001
        return [], SourceStatus("anysearch", False, "provider_error",
                                f"{'；'.join(errors[:2])}；兜底失败 {type(exc).__name__}: {str(exc)[:100]}")


def hackernews_search(keyword: str, max_age_days: int = 0) -> Tuple[List[Hit], SourceStatus]:
    """Hacker News：搜索接口 + 公共热门榜（front_page），榜上的条目名次更靠前。"""
    hits: List[Hit] = []
    failures: List[str] = []
    seen_urls = set()

    def collect(url: str, from_list: bool) -> None:
        ok, data, error = fetch.fetch_json(
            "hackernews", url, timeout=15,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
        if not ok or not isinstance(data, dict):
            failures.append(str(error or "low_quality"))
            return
        for rank, row in enumerate(data.get("hits") or [], start=1):
            if not isinstance(row, dict):
                continue
            title = _strip_markup(str(row.get("title") or ""))
            if not keyword_hit(title, keyword):
                continue
            object_id = str(row.get("objectID") or "")
            story_link = str(row.get("url") or "").strip()
            # HN 上讨论的是那个帖（item），外链常常是官方公告页；推原链接要推讨论帖。
            link = f"https://news.ycombinator.com/item?id={object_id}" if object_id else story_link
            if not link.startswith("http") or link in seen_urls:
                continue
            seen_urls.add(link)
            published = _parse_time(str(row.get("created_at") or ""))
            if too_old(published, max_age_days):
                continue
            hits.append(Hit(keyword, "Hacker News", title[:300], link,
                            extra={"rank": str(rank),
                                   "story_url": story_link,
                                   "from_list": "1" if from_list else "0"},
                            published_at=published))

    collect(f"{HN_API}?query={quote(keyword)}&tags=story&hitsPerPage=20", False)
    collect(f"{HN_API}?tags=front_page&hitsPerPage=30", True)
    if failures and not hits:
        return [], SourceStatus("hackernews", False, "provider_error",
                                "HN 搜索与热门榜都读取失败：" + failures[0][:120])
    return hits, SourceStatus("hackernews", True)


def appinn_search(keyword: str, max_age_days: int = 0) -> Tuple[List[Hit], SourceStatus]:
    """小众软件：同站搜索 + 发现频道，两个都试；评论数字段只留参考。"""
    hits: List[Hit] = []
    statuses: List[SourceStatus] = []

    def collect(url: str, name: str, from_list: bool) -> None:
        ok, data, error = fetch.fetch_json(
            name, url, timeout=15,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
        if not ok or not isinstance(data, dict):
            statuses.append(SourceStatus(name, False, str(error or "low_quality"), "读取失败"))
            return
        rows = data.get("topics") or []
        if not rows:
            rows = ((data.get("topic_list") or {}).get("topics") or [])
        for rank, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            title = _strip_markup(str(row.get("title") or ""))
            if not keyword_hit(title, keyword):
                continue
            topic_id = str(row.get("id") or "")
            slug = str(row.get("slug") or "")
            link = f"https://meta.appinn.net/t/{slug}/{topic_id}" if topic_id else ""
            if not link or not is_discussion_url(link):
                continue
            published = _parse_time(str(row.get("created") or row.get("created_at") or ""))
            if too_old(published, max_age_days):
                continue
            hits.append(Hit(keyword, "小众软件", title[:300], link,
                            extra={"rank": str(rank),
                                   "from_list": "1" if from_list else "0"},
                            published_at=published))
        statuses.append(SourceStatus(name, True))

    collect(f"{APPINN_SEARCH}?q={quote(keyword)}", "appinn_search", False)
    collect(APPINN_FAXIAN, "appinn_faxian", True)

    if all(status.ok for status in statuses):
        return hits, SourceStatus("appinn", True)
    failed = [s for s in statuses if not s.ok]
    return hits, SourceStatus("appinn", False, failed[0].error_type,
                              "; ".join(s.error for s in failed))


def search_keyword(keyword: str, max_age_days: int = 0) -> Tuple[List[Hit], List[SourceStatus]]:
    """跑一个关键词的全部社区源；单源失败隔离，返回状态和命中。"""
    hits: List[Hit] = []
    statuses: List[SourceStatus] = []
    for name, func in (("cloud", cloud_search), ("hackernews", hackernews_search),
                       ("appinn", appinn_search)):
        try:
            found, status = func(keyword, max_age_days)
        except Exception as exc:  # noqa: BLE001 单源异常必须隔离
            found, status = [], SourceStatus(name, False, "provider_error",
                                             f"{type(exc).__name__}: {str(exc)[:120]}")
        hits.extend(found)
        statuses.append(status)
    return hits, statuses
