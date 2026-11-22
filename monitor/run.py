"""编排：关键词 → 社区搜索 → 话题聚类 → 热度判定 → 合成一条消息 → 干跑/推送 → 状态。

只推最热的话题：跨平台、连续多轮、讨论量高的才进候选；冷门命中不推。
干跑不写去重库（否则真推送时会被自己吃掉），推送成功才写。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from collector import telegram

from . import config, heat, search, state as state_mod

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ALL_FAILED = 2
EXIT_CONFIG = 5


def format_message(items: List[dict]) -> str:
    """只要标题 + 原链接，多条合成一条；没有解读、没有摘要。"""
    blocks = []
    for item in items:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url:
            continue
        blocks.append(f"{title}\n{url}")
    return "\n\n".join(blocks)


def _load_fixtures(path: str) -> Tuple[List[search.SourceStatus], List[search.Hit]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    hits: List[search.Hit] = []
    for keyword, rows in (raw.get("keywords") or {}).items():
        for row in rows:
            extra = dict(row.get("extra") or {})
            extra.setdefault("rank", "1")
            extra.setdefault("discussions", "0")
            extra.setdefault("from_list", "0")
            hits.append(search.Hit(keyword=keyword, source=row.get("source", "Hacker News"),
                                   title=row.get("title", ""), url=row.get("url", ""),
                                   snippet=row.get("snippet", ""), extra=extra))
    return [search.SourceStatus("fixture", True)], hits


def _pick_links(topic, seen_fn, limit: int) -> List[dict]:
    """同一个话题里挑链接：每个社区最多一条，名次靠前的优先，且没推过。"""
    picked: List[dict] = []
    used_sources = set()
    for title, url, source in topic.links:
        if not title or not url or seen_fn(url) or source in used_sources:
            continue
        used_sources.add(source)
        picked.append({"title": title, "url": url, "source": source})
        if len(picked) >= limit:
            break
    return picked


def _keyword_level_candidates(hits, st, min_platforms: int, links_per_keyword: int,
                              score_cfg: dict, platform_weight: float,
                              consecutive_bonus: float, min_rounds: int) -> List[dict]:
    """产品级热点（宽松口径）：同一个关键词在几个社区都有人聊就算热。

    打分同样只看两条：跨几个社区 + 连续几轮都在。评论数不参与。
    """
    by_keyword: Dict[str, List] = {}
    for hit in hits:
        by_keyword.setdefault(hit.keyword, []).append(hit)
    out: List[dict] = []
    for keyword, keyword_hits in by_keyword.items():
        platforms = {hit.source for hit in keyword_hits}
        if len(platforms) < min_platforms:
            continue
        key = f"kw:{keyword.lower()}"
        history = st.topic_history(key)
        rounds = int(history.get("rounds", 0))
        consecutive = int(history.get("consecutive", 0))
        score = platform_weight * len(platforms)
        reasons = [f"关键词 {keyword} 在 {len(platforms)} 个社区都有人聊"]
        if consecutive >= min_rounds:
            score += consecutive_bonus * (consecutive - 1)
            reasons.append(f"连续 {consecutive} 轮都在")
        elif rounds:
            reasons.append(f"只出现 {rounds} 轮")
        picked = []
        for hit in keyword_hits:
            if st.is_seen(hit.url):
                continue
            picked.append(hit)
        picked.sort(key=lambda h: int(h.extra.get("rank", 99) or 99))
        for hit in picked[:links_per_keyword]:
            out.append({
                "title": heat.normalize_title(hit.title), "url": hit.url,
                "score": round(score, 1),
                "source": hit.source, "platforms": sorted(platforms),
                "keywords": [keyword],
                "rounds": rounds, "consecutive_rounds": consecutive,
                "reasons": reasons,
                "topic_key": key, "hits": len(keyword_hits),
                "kind": "keyword_level",
            })
    return out


def run(*, state_path: str = "state/state.json", report_path: str = "",
        send: bool = False, fixtures: str = "", keywords_file: str = "",
        settings_file: str = "") -> int:
    settings = config.load_settings(settings_file or None)
    hot_cfg = settings.get("hot") or {}
    max_push = int(hot_cfg.get("max_push", 5))
    overlap = float(hot_cfg.get("topic_overlap", 0.3))
    max_age_days = int(hot_cfg.get("max_age_days", 0))
    per_round = int(hot_cfg.get("per_round_keywords", 0))
    links_per_topic = int(hot_cfg.get("links_per_topic", 2))
    generic = {str(w).lower() for w in (settings.get("generic_words") or [])}

    keywords = config.load_keywords(keywords_file or None)
    if not keywords:
        print("关键词配置为空，拒绝运行。", file=sys.stderr)
        return EXIT_CONFIG

    st = state_mod.MonitorState(state_path).load()
    round_no = st.begin_round()
    day = state_mod.today_utc()
    started = datetime.now(timezone.utc).isoformat()
    print(f"本轮开始：{started}｜关键词 {len(keywords)} 个｜第 {round_no} 轮｜日期 {day}")

    token = chat_id = ""
    if send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
            return EXIT_CONFIG
        print("推送凭据：已读取（不回显）")

    statuses: List[search.SourceStatus] = []
    all_hits: List[search.Hit] = []
    filtered_non_chinese = 0
    filtered_examples: List[str] = []
    min_chinese_ratio = float((settings.get("filter") or {}).get("min_chinese_ratio", 0.05))
    budget = int((settings.get("runtime") or {}).get("max_seconds", 480))
    deadline = time.monotonic() + budget
    already = [k for k in keywords if st.keyword_searched(k, day)]
    pending = [k for k in keywords if k not in already]
    if per_round > 0 and len(pending) > per_round:
        planned = pending[:per_round]
        print(f"本轮按配置只搜前 {per_round} 个待搜关键词（per_round_keywords={per_round}）")
    else:
        planned = pending
    searched_now: List[str] = []
    if fixtures:
        fixture_statuses, fixture_hits = _load_fixtures(fixtures)
        statuses.extend(fixture_statuses)
        for keyword in planned:
            keyword_hits = [h for h in fixture_hits if h.keyword == keyword]
            kept, dropped = search.filter_non_chinese(keyword_hits, min_chinese_ratio)
            filtered_non_chinese += len(dropped)
            filtered_examples.extend(h.title[:80] for h in dropped[:3])
            all_hits.extend(kept)
            st.mark_keyword(keyword, [h.url for h in kept], day)
            searched_now.append(keyword)
    else:
        for keyword in planned:
            if time.monotonic() > deadline:
                print(f"本轮搜索超时（{budget} 秒），剩下的词留给下一轮。")
                break
            hits, keyword_statuses = search.search_keyword(keyword, max_age_days)
            kept, dropped = search.filter_non_chinese(hits, min_chinese_ratio)
            filtered_non_chinese += len(dropped)
            if len(filtered_examples) < 5:
                filtered_examples.extend(h.title[:80] for h in dropped[:5 - len(filtered_examples)])
            all_hits.extend(kept)
            statuses.extend(keyword_statuses)
            st.mark_keyword(keyword, [hit.url for hit in kept], day)
            searched_now.append(keyword)
    skipped = [k for k in keywords if k not in searched_now]
    print(f"本轮搜索关键词 {len(searched_now)} 个｜当天已搜跳过 {len(already)} 个｜"
          f"命中 {len(all_hits)}｜纯英文挡掉 {filtered_non_chinese}｜源状态 {len(statuses)}")

    alerts: List[str] = []
    # 一个源在一轮里可能报错多次（每个关键词一次），先按源名归并，再记轮次。
    merged_status: Dict[str, search.SourceStatus] = {}
    for status in statuses:
        existing = merged_status.get(status.name)
        if existing is None:
            merged_status[status.name] = status
        elif not existing.ok and status.ok:
            merged_status[status.name] = status
    for name, status in merged_status.items():
        alert = st.add_source_status(name, status.ok, status.error_type, status.error)
        if alert:
            alerts.append(alert)

    topics = heat.cluster_hits(all_hits, generic, overlap)
    hot_topics = []
    cold_count = 0
    for topic in topics:
        history = st.topic_history(topic.key)
        heat.score_topic(topic, settings, history)
        st.record_topic(topic.key, title=topic.title, url=topic.url,
                       platforms=sorted(topic.platforms), round_no=round_no)
        if heat.is_hot(topic, settings):
            hot_topics.append(topic)
        else:
            cold_count += 1
    hot_topics.sort(key=lambda t: (-t.score, t.best_rank, t.title))
    print(f"话题聚类 {len(topics)}｜热点 {len(hot_topics)}｜冷门不推 {cold_count}")

    # 去重：同一话题里只推还没推过的链接；整条都推过就跳过。
    candidates: List[dict] = []
    for topic in hot_topics:
        for link in _pick_links(topic, st.is_seen, links_per_topic):
            candidates.append({
                "title": link["title"], "url": link["url"], "score": round(topic.score, 1),
                "source": link["source"],
                "platforms": sorted(topic.platforms), "keywords": sorted(topic.keywords),
                "rounds": topic.rounds, "consecutive_rounds": topic.consecutive,
                "reasons": topic.reasons,
                "topic_key": topic.key, "hits": topic.hits,
                "kind": "topic",
            })
    candidates.sort(key=lambda c: -c["score"])
    keyword_level = []
    if hot_cfg.get("keyword_level_hot"):
        score_cfg = hot_cfg.get("score") or {}
        keyword_level = _keyword_level_candidates(
            all_hits, st, int(hot_cfg.get("keyword_level_min_platforms",
                                         hot_cfg.get("min_platforms", 2))),
            int(hot_cfg.get("keyword_level_links", 1)),
            score_cfg, float(score_cfg.get("platform_weight", 10)),
            float(score_cfg.get("consecutive_bonus", 2)),
            int(hot_cfg.get("min_consecutive_rounds", 2)))
        # 严格口径的同话题热点优先；链接重复的只留一条。
        chosen = list(candidates)
        seen_urls = {c["url"] for c in chosen}
        for item in keyword_level:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            chosen.append(item)
        chosen.sort(key=lambda c: -c["score"])
        candidates = chosen
        # 产品级话题也要记轮次，才能算出「连续几轮都在」。
        for key in {item["topic_key"] for item in keyword_level}:
            first = next(item for item in keyword_level if item["topic_key"] == key)
            st.record_topic(key, title=first["title"], url=first["url"],
                            platforms=first["platforms"], round_no=round_no)
    selected = candidates[:max_push]
    rest = candidates[max_push:]
    print(f"热点候选 {len(candidates)}｜本轮选入 {len(selected)}｜其余进汇总 {len(rest)}")

    spread: Dict[str, dict] = {}
    for hit in all_hits:
        entry = spread.setdefault(hit.keyword, {"platforms": set(), "hits": 0})
        entry["platforms"].add(hit.source)
        entry["hits"] += 1

    all_failed = bool(statuses) and all(not s.ok for s in statuses)
    if all_failed:
        alerts.append("⚠️ 关键词监控：本轮所有社区源都失败，没有采集到任何内容。")
        print("全源同时失败，单独报警。", file=sys.stderr)

    message = format_message(selected)
    pushed = []
    failed = []
    if selected and send:
        try:
            telegram.send_message(token, chat_id, message, disable_web_page_preview=True)
            for item in selected:
                st.mark_seen(url=item["url"], title=item["title"],
                             source=(item["platforms"] or [""])[0], keyword=(item["keywords"] or [""])[0])
            pushed = [f"{item['platforms'][0]}｜{item['title'][:60]}" for item in selected]
        except telegram.PushError as exc:
            failed.append(str(exc)[:160])
            print(f"推送失败，保持未推送下轮重试：{exc}", file=sys.stderr)
            # 关键词在搜索时就记了「当天已搜」；推送失败了就得摸掉，
            # 否则当天剩下的轮次会全部搜 0 个词，频道一整天哑掉。
            bucket = st.searched.get(day) or {}
            for keyword in searched_now:
                bucket.pop(keyword, None)
            if bucket:
                st.searched[day] = bucket
            else:
                st.searched.pop(day, None)
            print(f"已回滚本轮 {len(searched_now)} 个关键词的当日已搜标记，下轮重试。", file=sys.stderr)
    elif selected:
        print("\n--- 干跑：不会发送 ---\n" + message)
        pushed = [f"{item['platforms'][0]}｜{item['title'][:60]}" for item in selected]

    # 告警单独一条消息发（跟内容消息分开），不跟内容混在一起。
    if alerts and send:
        try:
            telegram.send_message(token, chat_id, "\n".join(alerts), disable_web_page_preview=True)
        except telegram.PushError as exc:
            failed.append(f"告警发送失败: {str(exc)[:120]}")
            print(f"告警发送失败：{exc}", file=sys.stderr)
    elif alerts:
        print("\n--- 告警（干跑不发送）---\n" + "\n".join(alerts))

    st.prune()
    st.save()

    report = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "round_no": round_no,
        "mode": "send" if send else "dry-run",
        "keywords": keywords,
        "searched_keywords": searched_now,
        "skipped_keywords": skipped,
        "already_searched_keywords": already,
        "thresholds": hot_cfg,
        "source_statuses": [s.to_dict() for s in statuses],
        "all_sources_failed": all_failed,
        "alerts": alerts,
        "hits_total": len(all_hits),
        "filtered_non_chinese": filtered_non_chinese,
        "filtered_pure_english_title": filtered_non_chinese,
        "filtered_non_chinese_examples": filtered_examples[:5],
        "topics_total": len(topics),
        "hot_topics": len(hot_topics),
        "cold_topics_skipped": cold_count,
        "keyword_spread": {kw: {"platforms": sorted(v["platforms"]), "hits": v["hits"]}
                           for kw, v in sorted(spread.items())},
        "keyword_level_candidates": keyword_level,
        "topics": [
            {
                "title": topic.title, "platforms": sorted(topic.platforms),
                "keywords": sorted(topic.keywords), "hits": topic.hits,
                "links": [{"title": title, "url": url, "source": source}
                          for title, url, source in topic.links[:6]],
            }
            for topic in topics[:200]
        ],
        "candidates": selected,
        "message": message,
        "silent": not selected,
        "pushed": pushed,
        "push_failed": failed,
        "summary_rest": rest,
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if failed:
        return EXIT_ALL_FAILED
    if all_failed:
        return EXIT_ALL_FAILED
    if any(not s.ok for s in statuses):
        return EXIT_PARTIAL
    return EXIT_OK
