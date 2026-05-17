from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import Civ6Paths
from .llm_context import build_llm_context
from .obs_news import build_current_news_item, build_news_entries
from .state import load_snapshot


DEFAULT_ANSWER = "AI 顾问会在这里给出简洁解释、战况分析或下一步建议。"
DEFAULT_NEWS = "等待文明 6 信息处理程序推送实时新闻……"


class OverlayJsonWriter:
    def __init__(self, paths: Civ6Paths, path: Path, *, max_barrage: int = 4, news_limit: int = 20) -> None:
        self.paths = paths
        self.path = path
        self.max_barrage = max_barrage
        self.news_limit = news_limit
        self._lock = threading.Lock()
        self._payload = self.initial_payload()

    def initial_payload(self) -> dict:
        payload = {
            "newsTitle": "世界新闻快报",
            "qaTitle": "弹幕问政厅",
            "meta": "游戏信息实时更新",
            "aiStatus": "询问 AI 当局游戏",
            "status": "CIVILIZATION VI LIVE OVERLAY",
            "newsText": DEFAULT_NEWS,
            "barrage": [],
            "questionMeta": "等待弹幕 ID",
            "question": "等待弹幕提问……",
            "answer": DEFAULT_ANSWER,
        }
        payload.update(game_status_payload(self.paths, news_limit=self.news_limit))
        return payload

    def write_initial(self) -> None:
        with self._lock:
            self._payload.update(game_status_payload(self.paths, news_limit=self.news_limit))
            self._write_locked()

    def refresh_game(self, news_text: str | None = None) -> None:
        with self._lock:
            self._payload.update(game_status_payload(self.paths, news_limit=self.news_limit, news_text=news_text))
            self._write_locked()

    def set_question(self, uname: str, question: str, *, paid: bool = False) -> None:
        prefix = "【付费】" if paid else ""
        barrage = [*self._payload.get("barrage", []), f"{prefix}{uname}：{question}"]
        with self._lock:
            self._payload["barrage"] = barrage[-self.max_barrage :]
            self._payload["questionMeta"] = question_meta(uname, paid=paid)
            self._payload["question"] = question
            self._payload["answer"] = "思考中..."
            self._payload["aiStatus"] = "询问 AI 当局游戏"
            self._payload.update(game_status_payload(self.paths, news_limit=self.news_limit))
            self._write_locked()

    def set_answer(self, uname: str, question: str, answer: str, *, paid: bool = False) -> None:
        prefix = "【付费】" if paid else ""
        barrage = [*self._payload.get("barrage", []), f"{prefix}{uname}：{question}"]
        with self._lock:
            self._payload["barrage"] = dedupe_tail(barrage, self.max_barrage)
            self._payload["questionMeta"] = question_meta(uname, paid=paid)
            self._payload["question"] = question
            self._payload["answer"] = answer
            self._payload["aiStatus"] = "询问 AI 当局游戏"
            self._payload.update(game_status_payload(self.paths, news_limit=self.news_limit))
            self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2), encoding="utf-8")


def game_status_payload(paths: Civ6Paths, *, news_limit: int, news_text: str | None = None) -> dict:
    snapshot = load_snapshot(paths)
    context = build_llm_context(paths, snapshot, turn=None, limit=news_limit)
    turn = context.get("latest_turn_label") or "?"
    players = context.get("players") or []
    player_names = [
        str(player.get("display_zh") or player.get("display") or "")
        for player in players[:4]
        if player.get("display_zh") or player.get("display")
    ]
    news_items = build_news_entries(paths, limit=news_limit)
    current_news_text = news_text or build_current_news_item(paths, limit=news_limit)
    if news_text:
        has_news_item = any(item.get("text") == news_text for item in news_items)
        if not has_news_item:
            news_items = [*news_items, {"text": news_text, "icons": []}]
    return {
        "meta": "游戏信息实时更新",
        "status": f"LIVE · {turn}",
        "newsText": current_news_text,
        "news": news_items or [DEFAULT_NEWS],
    }


def run_overlay_json(
    paths: Civ6Paths,
    *,
    overlay_json: Path,
    interval: float = 2.0,
    news_limit: int = 20,
    once: bool = False,
    duration: float | None = None,
) -> None:
    writer = OverlayJsonWriter(paths, overlay_json, news_limit=news_limit)
    stop_at = time.monotonic() + duration if duration else None
    while True:
        writer.refresh_game()
        print(f"wrote {overlay_json}")
        if once:
            return
        if stop_at is not None and time.monotonic() >= stop_at:
            return
        time.sleep(interval)


def dedupe_tail(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in reversed(values):
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return list(reversed(result))


def question_meta(uname: str, *, paid: bool = False) -> str:
    prefix = "付费弹幕" if paid else "弹幕"
    return f"{prefix} · {uname or '匿名观众'}"
