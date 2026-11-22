"""collector-kit：GitHub Actions 上跑定时采集与 Telegram 推送的共用内核。

本仓只用其中三块：
- fetch.py   HTTP/RSS 抓取、失败隔离、时间质量
- store.py   去重用的稳定 ID
- telegram.py 纯文本推送（关链接预览）
"""
from . import fetch, store, telegram  # noqa: F401

__all__ = ["fetch", "store", "telegram"]
__version__ = "1.0.0"
