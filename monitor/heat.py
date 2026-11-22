"""热度判定：话题聚类 + 评分 + 通用词过滤（借鉴 TrendScout 的做法）。

核心：
1. 同一话题在 2 个以上社区出现才算热点——所以聚类必须按「内容像不像同一个话题」，
   不能按「是不是同一个关键词」；否则同一个关键词的所有帖子会挤成一个假话题。
2. 公共热门榜里的条目排前面，搜索结果按名次排后面。
3. 连续多轮都出现比只出现一次更热。
4. trend/news/best/today 这类通用词不算数，必须有具体实词。
5. 关键词匹配用完整词边界（sec 不会命中 second）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "with", "is",
    "are", "was", "were", "be", "been", "being", "my", "your", "our", "we", "i",
    "it", "that", "this", "these", "those", "at", "by", "from", "as", "has", "have",
    "had", "not", "but", "if", "so", "do", "does", "did", "can", "will", "just",
    "now", "then", "than", "there", "here", "about", "into", "over", "after",
    "before", "up", "down", "out", "off", "again", "some", "any", "all", "you",
    "he", "she", "they", "them", "his", "her", "their", "its", "s", "t",
}

#: 站点名/来源标记：标题里带的这些词不代表讨论内容，不能拿来做「同一话题」的证据。
SITE_TOKENS = {
    "v2ex", "reddit", "linux", "hacker", "news", "appinn", "sspai", "twitter",
    "zhihu", "juejin", "少数派", "小众软件", "推特", "知乎", "掘金", "讨论区",
    "com", "www", "http", "https", "html", "amp",
}

#: 标题里常见的站点后缀/前缀，剥掉后剩下的才是帖子标题本身。
_SITE_SUFFIX_RE = re.compile(
    r"\s*[-–—|｜:：·]?\s*"
    r"(v2ex|hacker\s*news|小众软件|少数派|reddit|r/[A-Za-z0-9_]+|linux\.?do|twitter|推特)"
    r"\s*$",
    re.IGNORECASE,
)
_SITE_PREFIX_RE = re.compile(
    r"^\s*(v2ex|hacker\s*news|小众软件|少数派|reddit|linux\.?do|twitter|推特)"
    r"\s*[›>|:：·\-–—]\s*",
    re.IGNORECASE,
)

_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_LATIN_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9+#.]*$")


def word_match(text: str, keyword: str) -> bool:
    """完整词边界匹配；英文允许复数（Agent 命中 agents），但 sec 不命中 second。"""
    kw = (keyword or "").strip()
    if not kw or not text:
        return False
    if re.search(r"[一-鿿]", kw):
        return kw.lower() in text.lower()
    pattern = r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?:s|es)?(?![A-Za-z0-9])"
    return re.search(pattern, text, re.I) is not None


def normalize_title(title: str) -> str:
    """剥掉标题里的站点后缀/前缀，只留帖子标题本身。"""
    text = (title or "").strip()
    text = _SITE_PREFIX_RE.sub("", text)
    text = _SITE_SUFFIX_RE.sub("", text)
    return text.strip()


def _tokens(text: str, generic: Set[str]) -> Set[str]:
    """标题切成实词集合：通用词、停用词、站点名一律不算。"""
    tokens: Set[str] = set()
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.]*", text or ""):
        low = word.lower().strip(".")
        if len(low) < 3 or low in generic or low in STOPWORDS or low in SITE_TOKENS:
            continue
        if low.isdigit():
            continue
        tokens.add(low)
    # 中文：2-3 字的词整段保留，更长的按 3 字滑窗切，便于跨平台对齐。
    for run in re.findall(r"[一-鿿]{2,}", text or ""):
        if len(run) <= 3:
            tokens.add(run)
        else:
            for i in range(len(run) - 2):
                tokens.add(run[i:i + 3])
    return tokens


def _is_specific(token: str) -> bool:
    """实词判定：拉丁词要够长（>=4），带数字的型号词（4.6、k3）2 字起算；站点名、停用词不算。"""
    if not token or token in STOPWORDS or token in SITE_TOKENS:
        return False
    if _LATIN_TOKEN_RE.fullmatch(token):
        if any(ch.isdigit() for ch in token):
            return len(token) >= 2
        return len(token) >= 4
    return len(token) >= 2


def _version_tokens(tokens: Set[str]) -> Set[str]:
    """带数字的型号词：4.6、k3、gpt5。版本不一样就是不一样的话题。"""
    return {t for t in tokens if any(ch.isdigit() for ch in t) and len(t) >= 2}


def _topic_key(tokens: Set[str], keyword: str) -> str:
    """话题标识：取最长的几个拉丁实词做指纹，跨轮稳定一点。"""
    latin = sorted((t for t in tokens if _LATIN_TOKEN_RE.fullmatch(t)), key=lambda t: (-len(t), t))
    basis = latin[:3] if latin else sorted(tokens)[:4]
    raw = "|".join(sorted(basis))
    return hashlib.sha1(f"{keyword.lower()}|{raw}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Topic:
    key: str
    title: str
    url: str
    keywords: Set[str] = field(default_factory=set)
    platforms: Set[str] = field(default_factory=set)
    links: List[Tuple[str, str, str]] = field(default_factory=list)   # (title, url, source)
    best_rank: int = 99
    from_list: bool = False
    hits: int = 0
    # 来自状态
    rounds: int = 0
    consecutive: int = 0
    is_new: bool = True
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title, "url": self.url, "score": round(self.score, 1),
            "platforms": sorted(self.platforms), "keywords": sorted(self.keywords),
            "rounds": self.rounds, "consecutive_rounds": self.consecutive,
            "reasons": self.reasons,
        }


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _containment(a: Set[str], b: Set[str]) -> float:
    """小的那个标题里有多大比例的词被大的那个覆盖。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _same_topic(a: Set[str], b: Set[str]) -> bool:
    """两个标题是不是在说同一件事。

    光共享产品名不算（人人都提 Claude Code）。要求：
    - 共享 ≥2 个实词，且短的那个标题有一半以上被覆盖；或者
    - 共享 ≥3 个实词，且短标题有三分之一以上被覆盖。
    """
    shared = a & b
    if len(shared) < 2:
        return False
    specific = {t for t in shared if _is_specific(t)}
    if len(specific) < 2:
        return False
    versions_a = _version_tokens(a)
    versions_b = _version_tokens(b)
    if versions_a and versions_b and not (versions_a & versions_b):
        return False  # 版本号对不上（4.6 vs 4.7），不是同一件事
    containment = _containment(a, b)
    if len(specific) >= 2 and containment >= 0.45:
        return True
    return len(specific) >= 3 and containment >= 0.35


def cluster_hits(hits: Sequence, generic: Set[str], overlap: float = 0.3) -> List[Topic]:
    """把命中聚成话题：内容重合才算同一话题，同一个关键词不算证据。

    以每组第一个（名次最靠前的）命中为锚点，后面的命中必须跟锚点像才进组，
    避免 A~B、B~C 把不相干的三条粘成一条。
    """
    entries = []
    for hit in hits:
        title = normalize_title(getattr(hit, "title", "") or "")
        tokens = _tokens(title, generic)
        if not tokens:
            continue  # 只有通用词的标题不算具体话题
        entries.append((hit, title, tokens))

    entries.sort(key=lambda item: (int(item[0].extra.get("rank", 99) or 99),
                                    -int(item[0].extra.get("discussions", 0) or 0)))

    groups: List[dict] = []
    for hit, title, tokens in entries:
        placed = None
        for group in groups:
            if _same_topic(tokens, group["anchor_tokens"]):
                placed = group
                break
        if placed is None:
            groups.append({"anchor_tokens": tokens, "members": [(hit, title, tokens)]})
        else:
            placed["members"].append((hit, title, tokens))

    topics: List[Topic] = []
    for group in groups:
        members = [item[0] for item in group["members"]]
        tokens = set()
        for _, _, member_tokens in group["members"]:
            tokens |= member_tokens
        best = min(members, key=lambda h: int(h.extra.get("rank", 99) or 99))
        best_title = normalize_title(best.title)
        keyword = (best.keyword or "").lower()
        ordered = sorted(members, key=lambda h: int(h.extra.get("rank", 99) or 99))
        links = []
        seen_links = set()
        for h in ordered:
            if h.url in seen_links:
                continue  # 同一个链接被多个词搜到，只留一次
            seen_links.add(h.url)
            links.append((normalize_title(h.title) or h.title, h.url, h.source))
        topics.append(Topic(
            key=_topic_key(tokens, keyword),
            title=best_title,
            url=best.url,
            keywords={h.keyword for h in members},
            platforms={h.source for h in members},
            links=links,
            best_rank=min(int(h.extra.get("rank", 99) or 99) for h in members),
            from_list=any(h.extra.get("from_list") == "1" for h in members),
            hits=len(members),
        ))
    return topics


def score_topic(topic: Topic, cfg: dict, history: Optional[dict] = None) -> Topic:
    """打分只看两件事（用户 2026-11-18 定）：跨几个社区 + 连续几轮都在。

    评论数、回复数、点赞数一律不参与——中文源拿不到这些数，拿它排等于随机，
    用户自己点进链接看内容。
    """
    score_cfg = (cfg.get("hot") or {}).get("score") or {}
    platform_weight = float(score_cfg.get("platform_weight", 10))
    consecutive_bonus = float(score_cfg.get("consecutive_bonus", 2))
    min_rounds = int((cfg.get("hot") or {}).get("min_consecutive_rounds", 2))

    platforms = len(topic.platforms)
    topic.score = platform_weight * platforms
    topic.reasons = [f"跨 {platforms} 个社区出现"]

    if history:
        topic.rounds = int(history.get("rounds", 0))
        topic.consecutive = int(history.get("consecutive", 0))
        topic.is_new = topic.rounds == 0
    if topic.consecutive >= min_rounds:
        topic.score += consecutive_bonus * (topic.consecutive - 1)
        topic.reasons.append(f"连续 {topic.consecutive} 轮都在")
    elif topic.rounds:
        topic.reasons.append(f"只出现 {topic.rounds} 轮")
    return topic


def is_sustained(topic: Topic, cfg: dict) -> bool:
    """连续出现达到阈值才算持续热点。"""
    hot_cfg = cfg.get("hot") or {}
    min_rounds = int(hot_cfg.get("min_consecutive_rounds", 2))
    return topic.consecutive >= min_rounds


def is_hot(topic: Topic, cfg: dict) -> bool:
    """热点判定：默认跨平台达标即可；配置里打开 require_sustained 才额外要求连续多轮。"""
    hot_cfg = cfg.get("hot") or {}
    min_platforms = int(hot_cfg.get("min_platforms", 2))
    if len(topic.platforms) < min_platforms:
        return False
    if hot_cfg.get("require_sustained"):
        return is_sustained(topic, cfg)
    return True
