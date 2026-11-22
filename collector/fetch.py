"""采集内核：HTTP 抓取 + RSS 解析（零第三方依赖，Python 3.9+ 标准库）。

设计要点（沿用 A408 已验证的做法，并针对 GitHub Actions 无状态环境改造）：
- 单源失败只标 provider_error，绝不影响其它源；全部失败必须显式报错，0 条 ≠ 正常。
- 每个响应有硬上限，避免异常源撑爆内存。
- 只允许公网 HTTP(S)；DNS 解析结果必须为公网 IP，防 SSRF。
- 时间质量：缺失/非法发布时间标 missing，超前标 low_quality，不伪装成最新。
"""
from __future__ import annotations

import email.utils
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

CST = timezone(timedelta(hours=8))
UTC = timezone.utc

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TITLE_CHARS = 512
MAX_SUMMARY_CHARS = 2000
MAX_LINK_CHARS = 2048
MAX_GUID_CHARS = 2048
MAX_ITEMS_PER_FETCH = 50
MAX_FUTURE_SKEW = timedelta(minutes=15)
MAX_TITLE_AGE = timedelta(days=30)
DEFAULT_TIMEOUT = 15.0
USER_AGENT = "CindyCollector/1.0 (+github actions)"
_UNSAFE_XML_ENTITY = re.compile(br"<!\s*ENTITY\b", re.IGNORECASE)

#: 仅这些内置可信 HTTPS 新闻源在 VPN Fake-IP 环境下允许走本机代理隧道。
#: 与 A408 v0.3.58 的口径一致：自定义来源绝不自动信任。
TRUSTED_PROXY_SOURCES = frozenset({
    "cointelegraph", "coindesk", "decrypt", "theblock", "cointelegraph-btc",
    "ethereum-foundation", "sec-official", "cftc-general-official",
    "cftc-enforcement-official", "coinbase-status", "kraken-status",
    "fed-monetary", "trailofbits-security", "odaily", "blockbeats",
    "techflow", "the-defiant", "bitcoinist", "cryptoslate",
})
LOCAL_PROXY_ENV = "CINDY_LOCAL_HTTP_PROXY"
DEFAULT_LOCAL_PROXY = "http://127.0.0.1:7890"


class FetchError(Exception):
    """单源抓取失败。error_type 用于区分 provider_error 的子类。"""

    def __init__(self, message: str, error_type: str = "provider_error"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class RawItem:
    title: str
    link: str
    published_at: Optional[str] = None
    summary: str = ""
    guid: str = ""
    source: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class SourceResult:
    name: str
    url: str
    ok: bool
    items: List[RawItem] = field(default_factory=list)
    error_type: str = ""
    error: str = ""
    duration_ms: int = 0


# ---------------------------------------------------------------- 抓取

def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise FetchError(f"协议不受支持: {url}", "config_error")
    if parsed.username or parsed.password:
        raise FetchError("URL 不允许包含用户名或密码", "config_error")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError(f"URL 缺少主机名: {url}", "config_error")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise FetchError(f"不能指向本机/局域网: {host}", "config_error")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise FetchError(f"不能指向非公网 IP: {literal}", "config_error")


def _resolve_addresses(host: str, port: int) -> List[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"DNS 解析失败: {host} ({exc})", "dns_error")
    return [str(a[4][0]).split("%", 1)[0] for a in answers if a[4]]


def _is_fake_ip_only(addresses: Sequence[str]) -> bool:
    """VPN Fake-IP 段（198.18.0.0/15）；必须全部命中才算。"""
    if not addresses:
        return False
    for raw in addresses:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not (isinstance(addr, ipaddress.IPv4Address) and str(addr).startswith("198.18.")):
            return False
    return True


def _local_proxy_url(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    raw = (env.get(LOCAL_PROXY_ENV) or DEFAULT_LOCAL_PROXY).strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "http":
        return ""
    if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
        return ""
    if parsed.username or parsed.password or parsed.path not in ("", "/"):
        return ""
    return raw


def _validate_public(host: str, port: int, *, trusted_proxy: bool, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """校验目标。返回要使用的代理 URL（或 None 表示直连）。"""
    addresses = _resolve_addresses(host, port)
    if not addresses:
        raise FetchError(f"DNS 未返回可用地址: {host}", "dns_error")
    parsed_ips = []
    for raw in addresses:
        try:
            parsed_ips.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise FetchError(f"DNS 返回非法地址: {raw}", "dns_error") from exc
    if all(not addr.is_global for addr in parsed_ips):
        if trusted_proxy and _is_fake_ip_only(addresses):
            proxy = _local_proxy_url(env)
            if proxy:
                return proxy
        raise FetchError(
            f"DNS 解析到非公网地址，已拒绝: {host} -> {', '.join(addresses[:3])}",
            "dns_error",
        )
    for addr in parsed_ips:
        if not addr.is_global:
            raise FetchError(f"DNS 返回混合地址且含非公网 IP，已拒绝: {host} -> {addr}", "dns_error")
    return None


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    headers: Optional[Dict[str, str]] = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    user_agent: str = USER_AGENT,
    trusted_proxy: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> bytes:
    """GET 一个公开 URL；有界读取；失败重试；返回原始字节。"""
    _validate_public_url(url)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    proxy_url = _validate_public(parsed.hostname or "", port, trusted_proxy=trusted_proxy, env=env)
    if proxy_url and parsed.scheme.lower() != "https":
        raise FetchError("Fake-IP 代理隧道只用于 HTTPS 源", "config_error")

    request_headers = {"User-Agent": user_agent}
    request_headers.update(headers or {})
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
        )
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with opener.open(req, timeout=timeout) as resp:
                declared = resp.headers.get("Content-Length")
                if declared:
                    try:
                        if int(str(declared).strip()) > max_bytes:
                            raise FetchError(f"响应超过上限 {max_bytes} 字节", "too_large")
                    except ValueError:
                        pass
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise FetchError(f"响应超过上限 {max_bytes} 字节", "too_large")
                return data
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (400, 401, 403, 404, 410):
                raise FetchError(f"HTTP {exc.code}", f"http_{exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 网络异常种类多，统一按可重试处理
            last_error = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    raise FetchError(f"{type(last_error).__name__}: {last_error}")


# ---------------------------------------------------------------- RSS

def _parse_datetime(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    text = raw.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    return None


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", text).strip()


def _first_text(node: ET.Element, tags: Sequence[str]) -> str:
    for tag in tags:
        found = node.find(tag)
        if found is not None:
            if found.text:
                return found.text.strip()
            href = found.attrib.get("href")
            if href:
                return href.strip()
    return ""


def parse_rss(payload: bytes, *, source: str = "") -> List[RawItem]:
    """解析 RSS 2.0 / Atom，输出归一后的条目。"""
    if _UNSAFE_XML_ENTITY.search(payload[:4096]):
        raise FetchError("XML 含自定义 ENTITY 声明，已拒绝解析", "unsafe_xml")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FetchError(f"XML 解析失败: {exc}", "parse_error")

    tag = root.tag.split("}")[-1].lower()
    if tag == "rss":
        nodes = root.findall("./channel/item")
    elif tag == "feed":
        nodes = root.findall("./{http://www.w3.org/2005/Atom}entry") or root.findall("./entry")
    else:
        raise FetchError(f"不是 RSS/Atom 格式（根节点 {tag}）", "not_rss")

    items: List[RawItem] = []
    for node in nodes[:MAX_ITEMS_PER_FETCH]:
        title = _strip_tags(_first_text(node, ("title", "{http://www.w3.org/2005/Atom}title")))
        link = _first_text(node, ("link", "{http://www.w3.org/2005/Atom}link"))
        if not link:
            link = node.findtext("guid", "").strip()
        published = _first_text(
            node,
            ("pubDate", "published", "{http://www.w3.org/2005/Atom}published", "updated",
             "{http://www.w3.org/2005/Atom}updated"),
        )
        summary = _strip_tags(
            _first_text(
                node,
                ("description", "summary", "{http://www.w3.org/2005/Atom}summary",
                 "{http://www.w3.org/2005/Atom}content"),
            )
        )
        guid = node.findtext("guid", "").strip()
        if not title or not link:
            continue
        if len(link) > MAX_LINK_CHARS:
            continue
        items.append(
            RawItem(
                title=title[:MAX_TITLE_CHARS],
                link=link,
                published_at=published,
                summary=summary[:MAX_SUMMARY_CHARS],
                guid=guid[:MAX_GUID_CHARS] if guid else "",
                source=source,
            )
        )
    return items


def fetch_feed(
    name: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> SourceResult:
    """抓取并解析一个 RSS 源；失败返回带错误类型的结果，不抛异常。"""
    started = time.time()
    try:
        payload = http_get(
            url, timeout=timeout, headers=headers, trusted_proxy=name in TRUSTED_PROXY_SOURCES
        )
        items = parse_rss(payload, source=name)
        return SourceResult(
            name=name,
            url=url,
            ok=True,
            items=items,
            duration_ms=int((time.time() - started) * 1000),
        )
    except FetchError as exc:
        return SourceResult(
            name=name,
            url=url,
            ok=False,
            error_type=exc.error_type,
            error=str(exc),
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 任何异常都必须被隔离成单源故障
        return SourceResult(
            name=name,
            url=url,
            ok=False,
            error_type="provider_error",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )


def fetch_json(
    name: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> tuple:
    """抓取 JSON 接口，返回 (ok, data_or_error, error_type)。"""
    try:
        payload = http_get(
            url, timeout=timeout, headers=headers, trusted_proxy=name in TRUSTED_PROXY_SOURCES
        )
        return True, json.loads(payload.decode("utf-8")), ""
    except FetchError as exc:
        return False, str(exc), exc.error_type
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", "provider_error"


# ---------------------------------------------------------------- 时间质量

def normalize_time(raw: str) -> tuple:
    """返回 (ISO 字符串, 质量标签)。质量标签：ok / missing / low_quality。"""
    parsed = _parse_datetime(raw)
    if parsed is None:
        return "", "missing"
    now = datetime.now(UTC)
    if parsed > now + MAX_FUTURE_SKEW:
        return parsed.isoformat(), "low_quality"
    if parsed < now - MAX_TITLE_AGE:
        return parsed.isoformat(), "low_quality"
    return parsed.isoformat(), "ok"


def to_cst(iso: str) -> str:
    parsed = _parse_datetime(iso)
    if parsed is None:
        return ""
    return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M")
