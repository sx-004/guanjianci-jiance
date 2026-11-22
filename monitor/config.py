"""关键词配置与热度阈值配置。

- 关键词：monitor/keywords.txt，一行一个，加词只改这个文件。
- 热度阈值：monitor/settings.json，改阈值只改这个文件，不动逻辑代码。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

DEFAULT_KEYWORD_FILE = Path(__file__).with_name("keywords.txt")
DEFAULT_SETTINGS_FILE = Path(__file__).with_name("settings.json")

DEFAULT_SETTINGS: Dict[str, object] = {
    "hot": {
        "min_platforms": 2,
        "min_consecutive_rounds": 2,
        "require_sustained": False,
        "max_push": 5,
        "topic_overlap": 0.3,
        # 只看最近多少天的帖子（0 = 不按时间过滤）。
        "max_age_days": 30,
        # 每轮最多搜几个关键词（0 = 当天待搜的一次全搜完，会导致后面几轮全空；默认 3 = 12 词分 4 轮）。
        "per_round_keywords": 3,
        # 同一话题最多推几条链接（每个社区最多一条）。
        "links_per_topic": 2,
        # 产品级热点：同一个关键词在 min_platforms 个社区都有人聊就算热（默认关）。
        "keyword_level_hot": True,
    "keyword_level_min_platforms": 2,
    "keyword_level_links": 1,
    "keyword_level_max_rank": 15,
        "score": {
            "platform_weight": 10,
            "consecutive_bonus": 2,
        },
    },
    "generic_words": [],
    "filter": {
        # 历史字段，已不再使用：中文过滤现在只看标题里有没有中文字符（标题纯英文一律不推）。
        # 保留这个 key 只是为了兼容旧配置，不要用它做判断。
        "min_chinese_ratio": 0.05,
    },
    # 占位：后期做「关键词黑名单」，命中这些词的帖子直接不推。
    # 现在没有任何代码读它，等用户给定词表再实现。
    "future": {
        "blacklist_keywords": [],
    },
    "runtime": {
        # 一轮最多跑多少秒；超时的关键词不记账，下一轮接着搜。
        "max_seconds": 480,
    },
}


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path=None) -> Dict[str, object]:
    target = Path(path) if path else DEFAULT_SETTINGS_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return _merge(DEFAULT_SETTINGS, raw)


def load_keywords(path=None) -> List[str]:
    target = Path(path) if path else DEFAULT_KEYWORD_FILE
    words = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        word = raw.strip()
        if not word or word.startswith("#"):
            continue
        if word not in words:
            words.append(word)
    return words
