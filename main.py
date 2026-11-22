#!/usr/bin/env python3
"""关键词监控入口。默认干跑；只有显式 --send 且配置了 Secrets 才推送。"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collector import telegram  # noqa: E402
from monitor import config, heat, run as monitor_run, search  # noqa: E402

ROOT = Path(__file__).resolve().parent
FIXTURE = str(ROOT / "tests/fixtures/sample.json")


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，标题里有特殊字符会直接崩；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.buffer  # type: ignore[attr-defined]
            except AttributeError:
                pass


def _self_check() -> int:
    """离线自检：关键词命中/不命中、跨平台才算热、通用词过滤、标题+链接、
    关闭链接预览、同词当天不重复搜、去重、没有新增就静默。"""
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  OK   " if ok else "  FAIL ") + name + (f"（{detail}）" if detail else ""))
        if not ok:
            failures.append(name)

    print("离线自检：")
    words = config.load_keywords()
    check("关键词配置读得到 12 个词", len(words) == 12, f"实际 {len(words)} 个")
    check("关键词文件独立存在", (ROOT / "monitor/keywords.txt").exists())

    settings = config.load_settings()
    hot_cfg = settings.get("hot") or {}
    per_round = int(hot_cfg.get("per_round_keywords", 0))
    check("每轮搜索词数不能是 0", per_round > 0, f"当前 {per_round}")

    # 1. 命中 / 不命中
    check("关键词命中（Claude）", search.keyword_hit("Claude Opus 4.7 发布", "Claude"))
    check("关键词不命中（Claude vs Claudius）",
          not search.keyword_hit("Claudius is a latin poet", "Claude"))
    check("词边界（sec 不命中 second）", not heat.word_match("看看 second 是什么", "sec"))
    check("词边界（Agent 命中 agents）", heat.word_match("We use agents everywhere", "Agent"))

    # 2. 中文过滤：纯英文挡掉、中英混合保留
    english_hit = search.Hit("DeepSeek", "Hacker News",
                              "DeepInfer: High-performance inference framework for DeepSeek models",
                              "https://news.ycombinator.com/item?id=1",
                              snippet="Pure english story about inference frameworks")
    mixed_hit = search.Hit("Grok", "小众软件",
                            "Grok 被曝偷传用户代码，Grok 4.5 在 Arena 跌落至第六",
                            "https://meta.appinn.net/t/topic/1", snippet="纯英文摘要也不影响")
    kept, dropped = search.filter_non_chinese([english_hit, mixed_hit], 0.05)
    check("纯英文标题被挡掉（只看标题）", dropped == [english_hit] and kept == [mixed_hit])
    check("中英混合标题保留", mixed_hit in kept)
    snippet_cn_hit = search.Hit("MCP", "Reddit", "About Personal Agent",
                                "https://www.v2ex.com/t/1257814",
                                snippet="摘要里有几句中文字，旧规则会放过，现在必须挡掉")
    kept2, dropped2 = search.filter_non_chinese([snippet_cn_hit], 0.05)
    check("摘要含中文但标题纯英文 → 挡掉", dropped2 == [snippet_cn_hit] and not kept2)

    # 3. 通用词过滤
    generic = {str(w).lower() for w in (settings.get("generic_words") or [])}
    generic_topics = heat.cluster_hits(
        [search.Hit("MCP", "V2EX", "news today best official trend", "https://v2ex.com/t/1",
                    extra={"rank": "1", "discussions": "0", "from_list": "0"})], generic)
    check("只有通用词的标题不算话题", generic_topics == [])

    # 4. 跨社区才算热
    def hit(source: str, title: str, url: str, rank: int = 1, discussions: int = 0):
        return search.Hit("Claude", source, title, url,
                           extra={"rank": str(rank), "discussions": str(discussions),
                                  "from_list": "0"})

    one = heat.cluster_hits([hit("Hacker News", "Claude Opus 4.7 发布了", "https://news.ycombinator.com/item?id=1")], generic)
    heat.score_topic(one[0], settings)
    check("单社区命中不算热点", not heat.is_hot(one[0], settings))

    two = heat.cluster_hits([
        hit("Hacker News", "Claude Opus 4.7 released", "https://news.ycombinator.com/item?id=1"),
        hit("V2EX", "Claude Opus 4.7 发布讨论", "https://www.v2ex.com/t/123"),
    ], generic)
    heat.score_topic(two[0], settings)
    check("跨社区同一话题算热点", len(two) == 1 and heat.is_hot(two[0], settings))

    # 5. 评论数不参与打分：跨平台数相同、有没有评论数，分数应相同
    noisy = heat.cluster_hits([
        hit("Hacker News", "Claude Opus 4.7 发布了", "https://x/1", discussions=999),
        hit("V2EX", "Claude Opus 4.7 上线了", "https://x/2", discussions=0),
    ], generic)
    quiet = heat.cluster_hits([
        search.Hit("Kimi", "Hacker News", "Kimi K3.1 Pro 发布了", "https://x/3",
                   extra={"rank": "1", "discussions": "0", "from_list": "0"}),
        search.Hit("Kimi", "linux.do", "Kimi K3.1 Pro 上线了", "https://x/4",
                   extra={"rank": "1", "discussions": "0", "from_list": "0"}),
    ], generic)
    noisy_score = heat.score_topic(noisy[0], settings).score
    quiet_score = heat.score_topic(quiet[0], settings).score
    check("评论数不影响排序", noisy_score == quiet_score,
          f"{noisy_score} vs {quiet_score}")

    # 5. 标题 + 链接
    message = monitor_run.format_message([
        {"title": "标题A", "url": "https://news.ycombinator.com/item?id=1"}])
    check("推送内容只有标题+链接",
          "标题A\nhttps://news.ycombinator.com/item?id=1" in message and "为什么可疑" not in message)

    # 6. 关闭链接预览
    class Fake:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"ok":true}'
    with patch("collector.telegram.urllib.request.urlopen", return_value=Fake()) as mock:
        telegram.send_message("token", "chat", "标题\n链接", disable_web_page_preview=True)
    payload = json.loads(mock.call_args[0][0].data.decode("utf-8"))
    check("推送关闭链接预览", payload.get("disable_web_page_preview") is True)

    with tempfile.TemporaryDirectory() as tmp:
        # 样例里只有这两个词，避免「每轮只搜 3 个」把样例词留到下一轮。
        fixture_kw = str(Path(tmp) / "keywords-fixture.txt")
        Path(fixture_kw).write_text("Claude\nMCP\n", encoding="utf-8")
        state_path = str(Path(tmp) / "state.json")
        report1 = str(Path(tmp) / "r1.json")
        report2 = str(Path(tmp) / "r2.json")

        # 7. 同一关键词当天不重复搜
        code1 = monitor_run.run(fixtures=FIXTURE, state_path=state_path, report_path=report1,
                                keywords_file=fixture_kw)
        first = json.loads(Path(report1).read_text(encoding="utf-8"))
        code2 = monitor_run.run(fixtures=FIXTURE, state_path=state_path, report_path=report2,
                                keywords_file=fixture_kw)
        second = json.loads(Path(report2).read_text(encoding="utf-8"))
        check("同词当天不重复搜",
              code1 == 0 and code2 == 0
              and second["skipped_keywords"] == first["keywords"]
              and second["hits_total"] == 0,
              f"第一轮搜 {len(first['searched_keywords'])} 个词，第二轮跳过 {len(second['skipped_keywords'])} 个")

        # 8. 推过的不再推 + 9. 没有新增就静默
        with tempfile.TemporaryDirectory() as tmp2:
            send_state = str(Path(tmp2) / "state.json")
            send_report = str(Path(tmp2) / "r.json")
            with patch.object(telegram, "send_message", return_value={"ok": True}), \
                    patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token",
                                            "TELEGRAM_CHAT_ID": "test-chat"}):
                code = monitor_run.run(fixtures=FIXTURE, state_path=send_state,
                                       report_path=send_report, send=True,
                                       keywords_file=fixture_kw)
            sent = json.loads(Path(send_report).read_text(encoding="utf-8"))
            check("推送模式写入去重记录", code == 0 and sent["pushed"], f"推了 {len(sent['pushed'])} 条")
            with patch.object(telegram, "send_message", return_value={"ok": True}), \
                    patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token",
                                            "TELEGRAM_CHAT_ID": "test-chat"}):
                monitor_run.run(fixtures=FIXTURE, state_path=send_state,
                                report_path=send_report, send=True,
                                keywords_file=fixture_kw)
            again = json.loads(Path(send_report).read_text(encoding="utf-8"))
            check("推过的条目不再进候选", again["candidates"] == [])
            check("没有新增就静默", again["silent"] and again["message"] == "")

    if failures:
        print(f"自检失败 {len(failures)} 项：" + "、".join(failures), file=sys.stderr)
        return 5
    print(f"自检通过：关键词 {len(words)} 个、热度只看跨平台≥{hot_cfg.get('min_platforms')} 个社区"
          f"+连续多轮、不看评论数、每轮≤{hot_cfg.get('max_push')} 条、标题+链接、关闭链接预览、"
          f"同词当天只搜一次、去重、无新增静默")
    return 0


def main() -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(description="关键词监控：只推社区帖子标题和链接")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选，不推送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送")
    parser.add_argument("--self-check", action="store_true", help="离线自检")
    parser.add_argument("--list-keywords", action="store_true", help="列出当前关键词配置")
    parser.add_argument("--fixtures", default="", help="本地 JSON 样例，替代真实搜索")
    parser.add_argument("--keywords", default="", help="关键词文件路径")
    parser.add_argument("--settings", default="", help="热度阈值配置文件路径")
    parser.add_argument("--state", default="", help="状态文件路径（默认按模式区分）")
    parser.add_argument("--report", default="report.json", help="运行报告路径")
    args = parser.parse_args()

    if args.self_check:
        return _self_check()

    if args.list_keywords:
        for word in config.load_keywords(args.keywords or None):
            print(word)
        return 0

    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5

    # 推送开关：仓里没有 monitor/push.enabled 就不许真推送（用户确认后才放行）。
    if args.send and not (ROOT / "monitor/push.enabled").exists():
        print("推送开关未打开（缺 monitor/push.enabled），拒绝推送。", file=sys.stderr)
        return 5

    # 干跑用单独的状态文件：干跑不能吃掉真推送的去重记录。
    state_path = args.state or ("state/state.json" if args.send else "state/dryrun-state.json")
    return monitor_run.run(
        state_path=state_path, report_path=args.report, send=args.send,
        fixtures=args.fixtures, keywords_file=args.keywords, settings_file=args.settings,
    )


if __name__ == "__main__":
    sys.exit(main())
