import unittest

from monitor import search


class TestAnysearchKeyRotation(unittest.TestCase):
    """第一把 key 配额耗尽（402）时，必须自动换下一把，不能整轮召回归零。"""

    def test_rotates_past_exhausted_key(self):
        calls = []

        def fake(key):
            calls.append(key)
            if key == "key1":
                raise RuntimeError("HTTP Error 402: quota exhausted")
            return ["hit"]

        hits, used = search._try_keys(fake, ["key1", "key2", "key3"])
        self.assertEqual(hits, ["hit"])
        self.assertEqual(used, 1)
        self.assertEqual(calls, ["key1", "key2"])

    def test_falls_back_to_fourth_key(self):
        def fake(key):
            if key in ("key1", "key2", "key3"):
                raise RuntimeError("HTTP 402")
            return ["ok"]

        hits, used = search._try_keys(fake, ["key1", "key2", "key3", "key4"])
        self.assertEqual(hits, ["ok"])
        self.assertEqual(used, 3)

    def test_all_failed_raises(self):
        def fake(key):
            raise RuntimeError("HTTP 402")

        with self.assertRaises(search.AllKeysFailed):
            search._try_keys(fake, ["a", "b"])
