"""离线自检：命中/不命中、跨平台才算热、通用词过滤、热度排序、条数上限、
标题+链接、关闭链接预览、同词当天不重复搜、去重、没有新增就静默。"""
from __future__ import annotations

import json
import os
os.environ.setdefault("TELEGRAM_RETRY_DELAYS", "0")  # 单测不等待重试
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import telegram  # noqa: E402
from monitor import config, heat, run as monitor_run, search, state as state_mod  # noqa: E402

FIXTURE = str(ROOT / "tests/fixtures/sample.json")
SETTINGS = config.load_settings()
GENERIC = {str(w).lower() for w in (SETTINGS.get("generic_words") or [])}
FAKE_ENV = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat"}


def hit(keyword: str, source: str, title: str, url: str, *, rank: int = 1,
        discussions: int = 0, from_list: bool = False) -> search.Hit:
    return search.Hit(keyword, source, title, url,
                      extra={"rank": str(rank), "discussions": str(discussions),
                             "from_list": "1" if from_list else "0"})


class TestKeywordMatch(unittest.TestCase):
    def test_keywords_per_round_is_positive(self):
        settings = config.load_settings()
        per_round = int((settings.get("hot") or {}).get("per_round_keywords", 0))
        self.assertGreater(per_round, 0, "每轮搜 0 个词会让频道变哑巴")

    def test_hit_and_miss(self):
        self.assertTrue(search.keyword_hit("Claude Opus 4.7 发布", "Claude"))
        self.assertTrue(search.keyword_hit("We use agents everywhere", "Agent"))
        self.assertFalse(search.keyword_hit("Claudius is a latin poet", "Claude"))
        self.assertFalse(search.keyword_hit("看看 second 是什么", "sec"))
        self.assertFalse(search.keyword_hit("", "Claude"))

    def test_word_boundary(self):
        self.assertFalse(heat.word_match("看看 second 是什么", "sec"), "sec 不能命中 second")
        self.assertTrue(heat.word_match("We use agents everywhere", "Agent"))
        self.assertFalse(heat.word_match("old manuscripts", "Manus"))

    def test_keyword_file_is_single_source(self):
        words = config.load_keywords()
        self.assertEqual(len(words), 12)
        self.assertIn("ChatGPT", words)
        self.assertIn("MCP", words)


class TestUrlFilter(unittest.TestCase):
    def test_tag_and_profile_pages_rejected(self):
        self.assertFalse(search.is_discussion_url("https://www.v2ex.com/go/claude"))
        self.assertFalse(search.is_discussion_url("https://www.v2ex.com/tag/ChatGPT"))
        self.assertFalse(search.is_discussion_url("https://cn.v2ex.com/tag/Grok"))
        self.assertFalse(search.is_discussion_url("https://x.com/OpenAI"))
        self.assertFalse(search.is_discussion_url("https://www.reddit.com/r/kimi/"))
        self.assertTrue(search.is_discussion_url("https://www.v2ex.com/t/1257729"))
        self.assertTrue(search.is_discussion_url("https://x.com/u/status/123"))
        self.assertTrue(search.is_discussion_url("https://www.reddit.com/r/kimi/comments/1x/abc/"))
        self.assertTrue(search.is_discussion_url("https://linux.do/t/topic/2693184"))
        self.assertTrue(search.is_discussion_url("https://news.ycombinator.com/item?id=1"))

    def test_dedupe_ignores_page_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.MonitorState(str(Path(tmp) / "state.json"))
            st.mark_seen(url="https://linux.do/t/topic/2693184?tl=zh_CN", title="t",
                         source="linux.do", keyword="Cindy")
            self.assertTrue(st.is_seen("https://linux.do/t/topic/2693184?page=2"))
            self.assertTrue(st.is_seen("https://linux.do/t/topic/2693184"))
            self.assertFalse(st.is_seen("https://linux.do/t/topic/9999999"))


class TestHeat(unittest.TestCase):
    def test_single_platform_not_hot(self):
        hits = [hit("Claude", "Hacker News", "Claude Opus 4.7 launched today", "https://x/1")]
        topics = heat.cluster_hits(hits, GENERIC)
        self.assertEqual(len(topics), 1)
        heat.score_topic(topics[0], SETTINGS)
        self.assertFalse(heat.is_hot(topics[0], SETTINGS), "单平台命中不能算热点")

    def test_cross_platform_hot(self):
        hits = [
            hit("Claude", "Hacker News", "Claude Opus 4.7 released", "https://x/1", rank=2),
            hit("Claude", "V2EX", "Claude Opus 4.7 上线了", "https://x/2", rank=1),
        ]
        topics = heat.cluster_hits(hits, GENERIC)
        self.assertEqual(len(topics), 1)
        topic = heat.score_topic(topics[0], SETTINGS)
        self.assertTrue(heat.is_hot(topic, SETTINGS))
        self.assertGreaterEqual(len(topic.platforms), 2)

    def test_same_keyword_different_topics_stay_apart(self):
        """同一个关键词下的不同话题不能挤成一个假话题。"""
        hits = [
            hit("Claude", "Hacker News", "Claude Opus 4.7 released", "https://x/1"),
            hit("Claude", "V2EX", "Claude 额度太贵了 心疼", "https://x/2"),
            hit("Claude", "Reddit", "Claude desktop app crashed again", "https://x/3"),
        ]
        topics = heat.cluster_hits(hits, GENERIC)
        self.assertGreaterEqual(len(topics), 2, "不同话题不该被合并成一条")
        hot = [t for t in topics if len(t.platforms) >= 2]
        self.assertEqual(len(hot), 0, "没有真正跨平台的同话题，不该算热点")

    def test_site_suffix_not_used_as_evidence(self):
        """标题里的站点名（- V2EX、: r/kimi）不能当成同一话题的证据。"""
        hits = [
            hit("Claude", "Hacker News", "Claude Opus 4.7 released", "https://x/1"),
            hit("Claude", "V2EX", "Claude 额度太贵了 心疼 - V2EX", "https://x/2"),
        ]
        topics = heat.cluster_hits(hits, GENERIC)
        self.assertEqual(len(topics), 2, "站点名不能把两个话题粘在一起")

    def test_consecutive_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.MonitorState(str(Path(tmp) / "state.json"))
            r1 = st.begin_round()
            st.record_topic("k", title="t", url="u", platforms=["V2EX"], round_no=r1)
            self.assertEqual(st.topic_history("k")["consecutive"], 1)
            r2 = st.begin_round()
            st.record_topic("k", title="t", url="u", platforms=["V2EX"], round_no=r2)
            self.assertEqual(st.topic_history("k")["consecutive"], 2)
            r4 = st.begin_round() + 1  # 跳过一轮
            st.record_topic("k", title="t", url="u", platforms=["V2EX"], round_no=r4)
            self.assertEqual(st.topic_history("k")["consecutive"], 1, "断档后连续轮数要重置")

    def test_generic_words_filtered(self):
        topics = heat.cluster_hits(
            [hit("MCP", "V2EX", "news today best official trend", "https://x/g1")], GENERIC)
        self.assertEqual(topics, [], "只有通用词的标题不算具体话题")

    def test_discussion_count_does_not_affect_score(self):
        """评论数不参与打分：跨平台数相同、有没有评论数，分数必须一样。"""
        with_comments = [
            hit("Claude", "Hacker News", "Claude Opus 4.7 发布了", "https://x/1",
                discussions=999),
            hit("Claude", "V2EX", "Claude Opus 4.7 上线了", "https://x/2", discussions=0),
        ]
        without_comments = [
            hit("Kimi", "Hacker News", "Kimi K3.1 Pro 发布了", "https://x/3", discussions=0),
            hit("Kimi", "linux.do", "Kimi K3.1 Pro 上线了", "https://x/4", discussions=0),
        ]
        topics = heat.cluster_hits(with_comments + without_comments, GENERIC)
        self.assertEqual(len(topics), 2)
        scored = [heat.score_topic(t, SETTINGS) for t in topics]
        scores = sorted(t.score for t in scored)
        self.assertEqual(scores[0], scores[1], "评论数不能影响排序")
        for topic in scored:
            self.assertFalse(any("讨论" in reason for reason in topic.reasons),
                             "理由里不该再出现讨论量")

    def test_score_ordering_and_push_cap(self):
        strong = [
            hit("ChatGPT", "Hacker News", "ChatGPT new update discussion", "https://x/1",
                rank=1, discussions=100, from_list=True),
            hit("ChatGPT", "V2EX", "ChatGPT 新功能上线讨论", "https://x/2", rank=1, discussions=80),
            hit("ChatGPT", "linux.do", "ChatGPT 使用体验讨论", "https://x/3", rank=2, discussions=20),
        ]
        weak = [
            hit("Grok", "Hacker News", "Grok 使用体验讨论", "https://x/4", rank=8, discussions=1),
            hit("Grok", "V2EX", "Grok 体验讨论", "https://x/5", rank=7),
        ]
        topics = [heat.score_topic(t, SETTINGS) for t in heat.cluster_hits(strong + weak, GENERIC)]
        topics.sort(key=lambda t: -t.score)
        self.assertIn("ChatGPT", " ".join(topics[0].keywords))
        self.assertGreater(topics[0].score, topics[-1].score)

        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            custom = dict(SETTINGS)
            custom["hot"] = dict(SETTINGS["hot"])
            custom["hot"]["max_push"] = 1
            settings_path.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
            fixture_path = Path(tmp) / "fixtures.json"
            fixture_path.write_text(json.dumps({"keywords": {
                "ChatGPT": [{"source": h.source, "title": h.title, "url": h.url, "snippet": "",
                             "extra": h.extra} for h in strong],
                "Grok": [{"source": h.source, "title": h.title, "url": h.url, "snippet": "",
                          "extra": h.extra} for h in weak],
            }}, ensure_ascii=False), encoding="utf-8")
            monitor_run.run(fixtures=str(fixture_path), settings_file=str(settings_path),
                            state_path=str(Path(tmp) / "state.json"),
                            report_path=str(Path(tmp) / "report.json"))
            report = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["candidates"]), 1, "条数上限必须按配置生效")

    def test_thresholds_are_configurable(self):
        hits = [
            hit("Claude", "Hacker News", "Claude Opus 4.7 released", "https://x/1"),
            hit("Claude", "V2EX", "Claude Opus 4.7 上线了", "https://x/2"),
        ]
        topics = heat.cluster_hits(hits, GENERIC)
        strict = dict(SETTINGS)
        strict["hot"] = dict(SETTINGS["hot"])
        strict["hot"]["min_platforms"] = 3
        topic = heat.score_topic(topics[0], strict)
        self.assertFalse(heat.is_hot(topic, strict), "阈值改成 3 个社区时，两个社区不算热点")


class TestMessage(unittest.TestCase):
    def test_title_and_link_only(self):
        message = monitor_run.format_message([
            {"title": "标题A", "url": "https://news.ycombinator.com/item?id=1"},
            {"title": "标题B", "url": "https://x.com/u/status/2"},
        ])
        self.assertIn("标题A\nhttps://news.ycombinator.com/item?id=1", message)
        self.assertIn("标题B\nhttps://x.com/u/status/2", message)
        self.assertNotIn("为什么可疑", message)

    def test_link_preview_disabled(self):
        class Fake:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"ok":true}'
        with patch("collector.telegram.urllib.request.urlopen", return_value=Fake()) as mock:
            telegram.send_message("token", "chat", "标题\n链接", disable_web_page_preview=True)
        payload = json.loads(mock.call_args[0][0].data.decode("utf-8"))
        self.assertIs(payload["disable_web_page_preview"], True)


class TestChineseFilter(unittest.TestCase):
    def test_pure_english_blocked(self):
        hits = [search.Hit("DeepSeek", "Hacker News",
                           "DeepInfer: High-performance inference framework for DeepSeek models",
                           "https://news.ycombinator.com/item?id=1",
                           snippet="Pure english story about inference frameworks")]
        kept, dropped = search.filter_non_chinese(hits, 0.05)
        self.assertEqual(kept, [], "纯英文必须挡掉")
        self.assertEqual(len(dropped), 1)

    def test_chinese_snippet_english_title_blocked(self):
        """摘要里有中文也救不了纯英文标题——用户只看得到标题。"""
        hits = [search.Hit("MCP", "Reddit", "About Personal Agent",
                           "https://www.reddit.com/r/mcp/comments/1",
                           snippet="摘要里夹了几句中文：这是一个关于个人助手的讨论，里面有人回复")]
        kept, dropped = search.filter_non_chinese(hits, 0.05)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, hits)

    def test_mixed_chinese_kept(self):
        hits = [search.Hit("Grok", "小众软件",
                           "Grok 被曝偷传用户代码，Grok 4.5 在 Arena 跌落至第六",
                           "https://meta.appinn.net/t/topic/1",
                           snippet="纯英文摘要也不影响，标题里有中文就留")]
        kept, dropped = search.filter_non_chinese(hits, 0.05)
        self.assertEqual(len(kept), 1, "中英混合必须保留")
        self.assertEqual(dropped, [])

    def test_filter_counted_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "r.json"
            monitor_run.run(fixtures=FIXTURE, state_path=str(Path(tmp) / "state.json"),
                            report_path=str(report_path))
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["filtered_non_chinese"], 1, "挡掉的纯英文要记数")
            titles = [c["title"] for c in data["candidates"]]
            self.assertTrue(all(search.has_chinese(t) for t in titles),
                            "候选里不能有纯英文标题")

    def test_ratio_helper(self):
        self.assertEqual(search.chinese_ratio("Claude Opus 4.7"), 0.0)
        self.assertGreater(search.chinese_ratio("Grok 被曝偷传用户代码"), 0.5)
        self.assertTrue(search.has_chinese("Claude 新功能上线了"))
        self.assertFalse(search.has_chinese("Claude Opus 4.7 pricing"))


class TestAnysearch(unittest.TestCase):
    def test_endpoint_is_com(self):
        self.assertEqual(search.ANYSEARCH_API, "https://api.anysearch.com/v1/search")

    def test_nested_results_parsed(self):
        payload = json.dumps({"code": 0, "data": {"results": [{
            "title": "Claude 4.7 发布讨论",
            "url": "https://www.v2ex.com/t/123",
            "snippet": "聊聊 Claude 的新能力",
        }]}})
        hits = search._parse_search_json(payload, "Claude")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "V2EX")
        self.assertEqual(hits[0].url, "https://www.v2ex.com/t/123")


class TestRun(unittest.TestCase):
    def test_same_keyword_searched_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 只放样例里有的两个词，保证一轮搜完，测的是「当天不重复搜」而不是轮换。
            fixture_kw = str(Path(tmp) / "keywords.txt")
            Path(fixture_kw).write_text("Claude\nMCP\n", encoding="utf-8")
            state_path = str(Path(tmp) / "state.json")
            report1 = str(Path(tmp) / "r1.json")
            report2 = str(Path(tmp) / "r2.json")
            monitor_run.run(fixtures=FIXTURE, state_path=state_path, report_path=report1,
                            keywords_file=fixture_kw)
            first = json.loads(Path(report1).read_text(encoding="utf-8"))
            monitor_run.run(fixtures=FIXTURE, state_path=state_path, report_path=report2,
                            keywords_file=fixture_kw)
            second = json.loads(Path(report2).read_text(encoding="utf-8"))
            self.assertEqual(len(first["searched_keywords"]), 2)
            self.assertEqual(second["skipped_keywords"], first["keywords"],
                             "当天已搜的关键词必须跳过")
            self.assertEqual(second["hits_total"], 0, "跳过的关键词不能再去搜")

    def test_dry_run_does_not_consume_dedupe(self):
        """干跑只是看候选，不能把条目吃进去重库。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            report = str(Path(tmp) / "r.json")
            monitor_run.run(fixtures=FIXTURE, state_path=state_path, report_path=report)
            first = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertTrue(first["candidates"])
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertEqual(saved.get("seen"), {}, "干跑不能写进去重记录")
            self.assertTrue(saved.get("searched"), "干跑也要记当天搜过哪些词")

    def test_dedup_and_silence_after_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            report = str(Path(tmp) / "r.json")
            with patch.object(telegram, "send_message", return_value={"ok": True}), \
                    patch.dict(os.environ, FAKE_ENV):
                code = monitor_run.run(fixtures=FIXTURE, state_path=state_path,
                                       report_path=report, send=True)
            first = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertGreaterEqual(len(first["candidates"]), 1)
            self.assertFalse(first["silent"])

            with patch.object(telegram, "send_message", return_value={"ok": True}), \
                    patch.dict(os.environ, FAKE_ENV):
                monitor_run.run(fixtures=FIXTURE, state_path=state_path,
                                report_path=report, send=True)
            second = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(second["candidates"], [], "推过的条目不能再进候选")
            self.assertTrue(second["silent"], "没有新增必须静默")
            self.assertEqual(second["message"], "")

    def test_failed_push_rolls_back_searched_keywords(self):
        """推送失败时必须回滚「当天已搜」，否则当天剩下的轮次会搜 0 个词。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            report = str(Path(tmp) / "r.json")
            with patch.object(telegram, "send_message", side_effect=telegram.PushError("boom")), \
                    patch.dict(os.environ, FAKE_ENV):
                monitor_run.run(fixtures=FIXTURE, state_path=state_path,
                                report_path=report, send=True)
            first = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertTrue(first["push_failed"], "推送失败要如实报错")
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertEqual(saved.get("searched") or {}, {},
                             "推送失败后不能把关键词记成当天已搜")
            # 下一轮必须能重新搜到这些词，而不是整天哑掉。
            with patch.object(telegram, "send_message", return_value={"ok": True}), \
                    patch.dict(os.environ, FAKE_ENV):
                monitor_run.run(fixtures=FIXTURE, state_path=state_path,
                                report_path=report, send=True)
            second = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertTrue(second["searched_keywords"], "下一轮要重新搜，不能 0 个词")
            self.assertTrue(second["pushed"], "下一轮要真推出去")

    def test_keyword_level_hot_is_configurable(self):
        """产品级热点（同一个词在多个社区都有人聊）是可选开关，默认不开。"""
        rows = [
            {"source": "Hacker News", "title": "WorkBuddy：腾讯的 AI 工作台",
             "url": "https://news.ycombinator.com/item?id=50362428", "snippet": "",
             "extra": {"rank": "1", "discussions": "40", "from_list": "0"}},
            {"source": "V2EX", "title": "workbuddy很好用吗", "url": "https://www.v2ex.com/t/1",
             "snippet": "", "extra": {"rank": "1", "discussions": "10", "from_list": "0"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp) / "fixtures.json"
            fixture_path.write_text(json.dumps({"keywords": {"WorkBuddy": rows}},
                                               ensure_ascii=False), encoding="utf-8")
            off_settings = Path(tmp) / "off.json"
            off_settings.write_text(json.dumps({"hot": {"keyword_level_hot": False}},
                                               ensure_ascii=False), encoding="utf-8")
            on_settings = Path(tmp) / "on.json"
            on_settings.write_text(json.dumps({"hot": {"keyword_level_hot": True}},
                                               ensure_ascii=False), encoding="utf-8")
            for name, settings_path in (("off", off_settings), ("on", on_settings)):
                report_path = Path(tmp) / f"{name}.json"
                fixture_kw = Path(tmp) / f"{name}-keywords.txt"
                fixture_kw.write_text("WorkBuddy\n", encoding="utf-8")
                monitor_run.run(fixtures=str(fixture_path), settings_file=str(settings_path),
                                keywords_file=str(fixture_kw),
                                state_path=str(Path(tmp) / f"{name}-state.json"),
                                report_path=str(report_path))
                data = json.loads(report_path.read_text(encoding="utf-8"))
                if name == "off":
                    self.assertEqual(data["candidates"], [], "默认不开产品级热点")
                else:
                    self.assertTrue(data["candidates"], "打开后应该出候选")
                    self.assertEqual(data["candidates"][0]["kind"], "keyword_level")

    def test_all_sources_failed_raises_alert(self):
        """全源失败要单独报警，并且不能让工作流当成功混过去。"""
        broken = ([], [search.SourceStatus("cloud", False, "provider_error", "boom")])
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            report = str(Path(tmp) / "r.json")
            with patch("monitor.run.search.search_keyword", return_value=broken):
                code = monitor_run.run(state_path=state_path, report_path=report)
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(code, 2, "全源失败必须以失败退出，不能被吞掉")
            self.assertTrue(data["all_sources_failed"])
            self.assertTrue(data["alerts"], "全源失败要有独立告警")
            self.assertEqual(data["candidates"], [])


if __name__ == "__main__":
    unittest.main()
