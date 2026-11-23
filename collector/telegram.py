"""Telegram 推送：纯标准库调用 Bot API。

一条内容一条消息，文本用纯文本（不解析 Markdown），避免解析错误导致推送失败。
发送失败要显式报错，不能假装已推送。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Sequence

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_CHARS = 4096


class PushError(Exception):
    pass


def _api(token: str, method: str, payload: dict, timeout: float = 20.0) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise PushError(f"Telegram {method} HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PushError(f"Telegram {method} 失败: {type(exc).__name__}: {exc}") from exc
    if not body.get("ok"):
        raise PushError(f"Telegram {method} 返回失败: {body.get('description') or body}")
    return body


def send_message(*args, **kwargs) -> bool:
    """带重试的推送：只重试这一条消息，不重跑整轮。

    401/403 属于配置错误（token 无效、聊天未授权），重试没有意义，直接抛出；
    网络异常、429、5xx 这类临时故障才重试，重试仍失败就抛错，
    由上层写进 pending 队列，下一轮补推。
    """
    attempts = 3
    try:
        attempts = max(1, int(os.environ.get("TELEGRAM_RETRY_ATTEMPTS", "3") or "3"))
    except ValueError:
        attempts = 3
    delays = (1, 3)
    raw_delays = (os.environ.get("TELEGRAM_RETRY_DELAYS") or "").strip()
    if raw_delays:
        try:
            delays = tuple(max(0, int(x)) for x in raw_delays.split(",") if x.strip()) or delays
        except ValueError:
            delays = (1, 3)
    last_exc: Optional[PushError] = None
    for attempt in range(attempts):
        try:
            return _send_once(*args, **kwargs)
        except PushError as exc:
            last_exc = exc
            detail = str(exc)
            if "401" in detail or "403" in detail:
                raise
            if attempt + 1 >= attempts:
                break
            wait = delays[min(attempt, len(delays) - 1)]
            print(f"Telegram 推送失败，{wait} 秒后重试（{attempt + 1}/{attempts - 1}）：{detail}",
                  file=sys.stderr)
            time.sleep(wait)
    raise last_exc


def _send_once(token: str, chat_id: str, text: str, *, timeout: float = 20.0,
                 disable_web_page_preview: bool = False) -> dict:
    """发一条纯文本消息；超长自动按段落切分。"""
    if not token or not chat_id:
        raise PushError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    text = (text or "").strip()
    if not text:
        raise PushError("消息内容为空")
    chunks = _split(text, MAX_MESSAGE_CHARS)
    last: dict = {}
    for chunk in chunks:
        last = _api(token, "sendMessage",
                    {"chat_id": chat_id, "text": chunk,
                     "disable_web_page_preview": disable_web_page_preview},
                    timeout)
    return last


def _split(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    buf = ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        buf = line
    if buf:
        chunks.append(buf)
    return chunks


def test_connection(token: str, *, timeout: float = 20.0) -> dict:
    """校验 token 是否有效并取回机器人身份（只读，不发消息）。"""
    return _api(token, "getMe", {}, timeout)


def get_chat(token: str, chat_id: str, *, timeout: float = 20.0) -> dict:
    """查询目标会话信息（只读）。"""
    return _api(token, "getChat", {"chat_id": chat_id}, timeout)
