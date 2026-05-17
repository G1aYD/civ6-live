from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .bilibili_live import (
    DanmakuEvent,
    GiftEvent,
    SuperChatEvent,
    command_name,
    get_danmaku_history,
    get_room_info,
    iter_live_commands,
    parse_danmaku,
    parse_gift,
    parse_super_chat,
    resolve_room_id,
)
from .config import Civ6Paths
from .llm_client import LLMError, ask_openai, load_env_file
from .llm_context import direct_game_answer, llm_context_prompt
from .obs_news import build_current_news_item, build_news_ticker
from .overlay_state import OverlayJsonWriter
from .state import load_snapshot


DEFAULT_ANSWER_CHAR_LIMIT = 300
DEFAULT_GIFT_QUESTION_COST = 100
GIFT_COIN_PER_BATTERY = 100
OVERLAY_FINAL_ANSWER_POLL_GRACE_SECONDS = 1.5
WEBSOCKET_RESTART_DELAY_SECONDS = 3.0


@dataclass(frozen=True)
class ChatQuestion:
    uid: int
    uname: str
    text: str
    source: str
    paid: bool = False


class GiftGate:
    def __init__(
        self,
        *,
        gift_name: str | None = None,
        gift_id: int | None = None,
        min_value: int | None = None,
        window_seconds: float = 600.0,
        consume: bool = False,
    ) -> None:
        self.gift_name = gift_name
        self.gift_id = gift_id
        self.min_value = min_value
        self.window_seconds = window_seconds
        self.consume = consume
        self._credits: dict[int, tuple[float, int]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.gift_name or self.gift_id is not None or self.min_value is not None)

    def observe(self, gift: GiftEvent) -> bool:
        if not self.enabled or not self.matches(gift):
            return False
        expires_at = time.time() + self.window_seconds
        old_expires, old_credits = self._credits.get(gift.uid, (0.0, 0))
        self._credits[gift.uid] = (max(expires_at, old_expires), old_credits + max(gift.num, 1))
        return True

    def matches(self, gift: GiftEvent) -> bool:
        if self.gift_name and gift.gift_name != self.gift_name:
            return False
        if self.gift_id is not None and gift.gift_id != self.gift_id:
            return False
        if self.min_value is not None and gift.total_coin < self.min_value:
            return False
        return True

    def allow(self, uid: int) -> bool:
        if not self.enabled:
            return True
        now = time.time()
        expires_at, credits = self._credits.get(uid, (0.0, 0))
        if expires_at < now or credits <= 0:
            self._credits.pop(uid, None)
            return False
        if self.consume:
            next_credits = credits - 1
            if next_credits <= 0:
                self._credits.pop(uid, None)
            else:
                self._credits[uid] = (expires_at, next_credits)
        return True


class GiftLedger:
    def __init__(
        self,
        *,
        log_path: Path,
        totals_path: Path,
        obs_text: Path | None,
        question_cost: int = DEFAULT_GIFT_QUESTION_COST,
    ) -> None:
        self.log_path = log_path
        self.totals_path = totals_path
        self.obs_text = obs_text
        self.question_cost = max(question_cost, 1)
        self.accounts: dict[str, dict] = {}
        self.seen_gifts: set[str] = set()
        self.latest_line = "等待礼物记录..."
        self.load()
        self.latest_line = self.restore_summary()
        self.write_obs_text()

    def load(self) -> None:
        if not self.totals_path.exists():
            return
        try:
            data = json.loads(self.totals_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, dict):
            self.accounts = {str(uid): account for uid, account in accounts.items() if isinstance(account, dict)}
        seen_gifts = data.get("seen_gifts") if isinstance(data, dict) else None
        if isinstance(seen_gifts, list):
            self.seen_gifts = {str(key) for key in seen_gifts}

    def record_gift(self, gift: GiftEvent) -> str:
        gift_key = gift_event_key(gift)
        if gift_key in self.seen_gifts:
            self.latest_line = f"重复礼物已忽略：{gift.uname or gift.uid}"
            self.write_obs_text()
            return self.latest_line
        self.seen_gifts.add(gift_key)
        uid = str(gift.uid)
        amount = max(gift.total_coin, 0)
        account = self.accounts.setdefault(
            uid,
            {
                "uid": gift.uid,
                "uname": gift.uname or f"uid {gift.uid}",
                "total_coin": 0,
                "spent_coin": 0,
                "gift_count": 0,
                "last_gift": "",
                "last_gift_at": "",
            },
        )
        if gift.uname:
            account["uname"] = gift.uname
        account["total_coin"] = int(account.get("total_coin") or 0) + amount
        account["gift_count"] = int(account.get("gift_count") or 0) + 1
        account["last_gift"] = gift.gift_name
        account["last_gift_at"] = current_timestamp()
        self.append_log(gift, amount)
        self.save()
        balance = self.balance_for(gift.uid)
        credits = balance // self.question_cost
        self.latest_line = f"{account['uname']} +礼物，剩余 {credits}问"
        self.write_obs_text()
        return self.latest_line

    def append_log(self, gift: GiftEvent, amount: int) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "time": current_timestamp(),
            "source": command_name(gift.raw),
            "uid": gift.uid,
            "uname": gift.uname,
            "gift_name": gift.gift_name,
            "gift_id": gift.gift_id,
            "num": gift.num,
            "price": gift.price,
            "total_coin": amount,
            "coin_type": gift.coin_type,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def save(self) -> None:
        self.totals_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "question_cost": self.question_cost,
            "updated_at": current_timestamp(),
            "accounts": self.accounts,
            "seen_gifts": sorted(self.seen_gifts)[-5000:],
        }
        self.totals_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def balance_for(self, uid: int) -> int:
        account = self.accounts.get(str(uid))
        if not account:
            return 0
        return max(int(account.get("total_coin") or 0) - int(account.get("spent_coin") or 0), 0)

    def can_spend(self, uid: int) -> bool:
        return self.balance_for(uid) >= self.question_cost

    def spend_question(self, uid: int, uname: str) -> tuple[bool, int]:
        account = self.accounts.get(str(uid))
        if not account or self.balance_for(uid) < self.question_cost:
            return False, self.balance_for(uid)
        if uname:
            account["uname"] = uname
        account["spent_coin"] = int(account.get("spent_coin") or 0) + self.question_cost
        balance = self.balance_for(uid)
        credits = balance // self.question_cost
        self.latest_line = f"{account.get('uname') or uid} -1问，剩余 {credits}问"
        self.save()
        self.write_obs_text()
        return True, balance

    def write_obs_text(self) -> None:
        if self.obs_text is None:
            return
        lines = [self.latest_line, f"规则：1问 = {format_gift_question_cost(self.question_cost)}"]
        top = self.top_accounts(limit=len(self.accounts))
        if top:
            lines.append("可提问：")
            lines.extend(top)
        write_obs_text(self.obs_text, "\n".join(lines))

    def top_accounts(self, *, limit: int) -> list[str]:
        ranked = sorted(
            self.accounts.values(),
            key=lambda account: int(account.get("total_coin") or 0) - int(account.get("spent_coin") or 0),
            reverse=True,
        )
        lines: list[str] = []
        for account in ranked[:limit]:
            balance = max(int(account.get("total_coin") or 0) - int(account.get("spent_coin") or 0), 0)
            if balance <= 0:
                continue
            credits = balance // self.question_cost
            if credits < 1:
                continue
            lines.append(f"{account.get('uname') or account.get('uid')} {credits}问")
        return lines

    def restore_summary(self) -> str:
        total_credits = 0
        user_count = 0
        for account in self.accounts.values():
            balance = max(int(account.get("total_coin") or 0) - int(account.get("spent_coin") or 0), 0)
            credits = balance // self.question_cost
            if credits <= 0:
                continue
            user_count += 1
            total_credits += credits
        if user_count <= 0:
            return "等待礼物记录..."
        return f"已恢复 {user_count} 名用户，共 {total_credits} 次提问"


async def run_bilibili_obs_bot(
    paths: Civ6Paths,
    *,
    room: str,
    obs_text: Path,
    question_mode: str = "heuristic",
    gift_name: str | None = None,
    gift_id: int | None = None,
    min_gift_value: int | None = None,
    gift_window: float = 600.0,
    consume_gift: bool = False,
    allow_super_chat: bool = True,
    no_llm: bool = False,
    model: str | None = None,
    env_file: str | Path = ".env",
    llm_timeout: float = 60.0,
    context_limit: int = 30,
    news_text: Path | None = None,
    news_interval: float = 2.0,
    news_limit: int = 20,
    overlay_json: Path | None = None,
    duration: float | None = None,
    debug_danmaku: bool = False,
    debug_commands: bool = False,
    debug_command_json: bool = False,
    force_default_ws: bool = True,
    history_poll: bool = True,
    history_interval: float = 1.0,
    websocket_danmaku: bool = False,
    require_gift_credit: bool = False,
    gift_question_cost: int | None = None,
    gift_log: Path = Path("obs/gifts.jsonl"),
    gift_totals: Path = Path("obs/gift_totals.json"),
    gift_obs_text: Path | None = Path("obs/gifts.txt"),
    answer_char_limit: int = DEFAULT_ANSWER_CHAR_LIMIT,
) -> None:
    load_env_file(env_file)
    gift_question_cost = resolve_gift_question_cost(gift_question_cost)
    resolved_room_id: int | None = None
    try:
        resolved_room_id = resolve_room_id(room)
        room_info = get_room_info(resolved_room_id)
        print(room_status_description(resolved_room_id, room_info))
        if room_info.get("live_status") != 1:
            print("warning: room is not live; Bilibili may not push DANMU_MSG to the live websocket.")
    except RuntimeError as exc:
        print(f"warning: could not check Bilibili room status: {exc}")
    gate = GiftGate(
        gift_name=gift_name,
        gift_id=gift_id,
        min_value=min_gift_value,
        window_seconds=gift_window,
        consume=consume_gift,
    )
    gift_ledger = GiftLedger(
        log_path=gift_log,
        totals_path=gift_totals,
        obs_text=gift_obs_text,
        question_cost=gift_question_cost,
    )
    queue: asyncio.Queue[ChatQuestion] = asyncio.Queue()
    seen_danmaku: set[str] = set()
    write_obs_text(obs_text, "等待弹幕问题...")
    overlay_writer = OverlayJsonWriter(paths, overlay_json, news_limit=news_limit) if overlay_json else None
    if overlay_writer is not None:
        overlay_writer.write_initial()
    worker = asyncio.create_task(
        answer_worker(
            paths,
            queue,
            obs_text=obs_text,
            overlay_writer=overlay_writer,
            no_llm=no_llm,
            model=model,
            env_file=env_file,
            llm_timeout=llm_timeout,
            context_limit=context_limit,
            answer_char_limit=answer_char_limit,
        )
    )
    news_worker_task = None
    if news_text is not None or overlay_writer is not None:
        news_worker_task = asyncio.create_task(
            obs_news_worker(paths, obs_text=news_text, overlay_writer=overlay_writer, interval=news_interval, limit=news_limit)
        )
    history_worker_task = None
    if history_poll and resolved_room_id is not None:
        seed_danmaku_history(resolved_room_id, seen_danmaku, debug=debug_danmaku)
        history_worker_task = asyncio.create_task(
            danmaku_history_worker(
                resolved_room_id,
                seen_danmaku=seen_danmaku,
                queue=queue,
                gate=gate,
                gift_ledger=gift_ledger,
                require_gift_credit=require_gift_credit,
                question_mode=question_mode,
                interval=history_interval,
                debug_danmaku=debug_danmaku,
            )
        )
    stop_at = time.monotonic() + duration if duration else None

    command_iter = None
    try:
        print(f"Listening to Bilibili room {room}. OBS text: {obs_text}")
        if gate.enabled:
            print(gift_gate_description(gate))
        while True:
            if stop_at is not None and time.monotonic() >= stop_at:
                break
            reconnect_socket = False
            command_iter = iter_live_commands(
                resolved_room_id or room,
                debug=debug_commands or debug_danmaku,
                force_default_ws=force_default_ws,
            ).__aiter__()
            try:
                while True:
                    try:
                        command = await next_command(command_iter, stop_at)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        print(
                            "Bilibili websocket command loop stopped unexpectedly: "
                            f"{exc}; reconnecting in {WEBSOCKET_RESTART_DELAY_SECONDS:.0f}s"
                        )
                        reconnect_socket = True
                        break
                    if command is None:
                        break
                    cmd_name = command_name(command)
                    if debug_commands:
                        print(f"received command: {command_debug_line(command, include_json=debug_command_json)}")
                    if cmd_name.startswith("__"):
                        continue
                    gift = parse_gift(command)
                    if gift:
                        print(format_gift_event(gift))
                        print(f"gift ledger: {gift_ledger.record_gift(gift)}")
                        if gate.observe(gift):
                            print(f"gift gate: {gift.uname} sent {gift.gift_name} x{gift.num}; question credit enabled")
                        continue
                    super_chat = parse_super_chat(command)
                    if super_chat and allow_super_chat:
                        await queue.put(super_chat_question(super_chat))
                        print(f"accepted super chat: {super_chat.uname}: {super_chat.message}")
                        continue
                    danmaku = parse_danmaku(command)
                    if not danmaku:
                        continue
                    if history_poll and not websocket_danmaku:
                        if debug_danmaku:
                            print(
                                "ignored websocket danmaku because history polling is enabled "
                                f"and has better usernames: {danmaku.uname}({danmaku.uid}): {danmaku.text}"
                            )
                        continue
                    await handle_danmaku(
                        danmaku,
                        seen_danmaku=seen_danmaku,
                        queue=queue,
                        gate=gate,
                        gift_ledger=gift_ledger,
                        require_gift_credit=require_gift_credit,
                        question_mode=question_mode,
                        debug_danmaku=debug_danmaku,
                    )
            finally:
                if command_iter is not None:
                    await command_iter.aclose()
                    command_iter = None
            if not reconnect_socket:
                break
            if stop_at is not None and time.monotonic() >= stop_at:
                break
            await asyncio.sleep(WEBSOCKET_RESTART_DELAY_SECONDS)
    finally:
        if command_iter is not None:
            await command_iter.aclose()
        if history_worker_task is not None:
            history_worker_task.cancel()
            try:
                await history_worker_task
            except asyncio.CancelledError:
                pass
        if news_worker_task is not None:
            news_worker_task.cancel()
            try:
                await news_worker_task
            except asyncio.CancelledError:
                pass
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


async def danmaku_history_worker(
    room_id: int,
    *,
    seen_danmaku: set[str],
    queue: asyncio.Queue[ChatQuestion],
    gate: GiftGate,
    gift_ledger: GiftLedger,
    require_gift_credit: bool,
    question_mode: str,
    interval: float,
    debug_danmaku: bool,
) -> None:
    while True:
        try:
            history = await asyncio.to_thread(get_danmaku_history, room_id)
            for danmaku in history:
                await handle_danmaku(
                    danmaku,
                    seen_danmaku=seen_danmaku,
                    queue=queue,
                    gate=gate,
                    gift_ledger=gift_ledger,
                    require_gift_credit=require_gift_credit,
                    question_mode=question_mode,
                    debug_danmaku=debug_danmaku,
                )
        except RuntimeError as exc:
            if debug_danmaku:
                print(f"danmaku history poll failed: {exc}")
        await asyncio.sleep(interval)


def seed_danmaku_history(room_id: int, seen_danmaku: set[str], *, debug: bool) -> None:
    try:
        history = get_danmaku_history(room_id)
    except RuntimeError as exc:
        if debug:
            print(f"danmaku history seed failed: {exc}")
        return
    for danmaku in history:
        seen_danmaku.add(danmaku_key(danmaku))
    if debug:
        print(f"seeded {len(history)} existing danmaku from history")


async def handle_danmaku(
    danmaku: DanmakuEvent,
    *,
    seen_danmaku: set[str],
    queue: asyncio.Queue[ChatQuestion],
    gate: GiftGate,
    gift_ledger: GiftLedger,
    require_gift_credit: bool,
    question_mode: str,
    debug_danmaku: bool,
) -> None:
    key = danmaku_key(danmaku)
    if key in seen_danmaku:
        return
    seen_danmaku.add(key)
    if len(seen_danmaku) > 2000:
        seen_danmaku.clear()
        seen_danmaku.add(key)
    source = command_name(danmaku.raw)
    source_text = "history" if source == "DANMU_HISTORY" else "websocket"
    if debug_danmaku:
        print(f"danmaku[{source_text}]: {danmaku.uname}({danmaku.uid}): {danmaku.text}")
    if not is_question(danmaku.text, question_mode):
        if debug_danmaku:
            print(f"ignored non-question danmaku from {danmaku.uname}: {danmaku.text}")
        return
    if not gate.allow(danmaku.uid):
        print(f"ignored gated question from {danmaku.uname}: {danmaku.text}")
        return
    if require_gift_credit:
        spent, balance = gift_ledger.spend_question(danmaku.uid, danmaku.uname)
        if not spent:
            print(
                f"ignored unpaid question from {danmaku.uname}: {danmaku.text} "
                f"(balance {balance}/{gift_ledger.question_cost} coin)"
            )
            return
        print(
            f"gift credit accepted: {danmaku.uname} spent {gift_ledger.question_cost} coin; "
            f"balance {balance} coin"
        )
    await queue.put(danmaku_question(danmaku))
    print(f"accepted question: {danmaku.uname}: {danmaku.text}")


def danmaku_key(danmaku: DanmakuEvent) -> str:
    raw = danmaku.raw
    if command_name(raw) == "DANMU_HISTORY":
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        stable_id = data.get("id_str") or data.get("rnd") or data.get("timeline")
        return f"history:{stable_id}:{danmaku.uid}:{danmaku.uname}:{danmaku.text}"
    info = raw.get("info") if isinstance(raw.get("info"), list) else []
    meta = info[0] if info and isinstance(info[0], list) else []
    timestamp = meta[4] if len(meta) > 4 else ""
    nonce = meta[7] if len(meta) > 7 else ""
    return f"ws:{timestamp}:{nonce}:{danmaku.uid}:{danmaku.uname}:{danmaku.text}"


def gift_event_key(gift: GiftEvent) -> str:
    data = gift.raw.get("data") if isinstance(gift.raw.get("data"), dict) else {}
    nested_keys = []
    for nested_name in ("batch_combo_send", "combo_send"):
        nested = data.get(nested_name) if isinstance(data.get(nested_name), dict) else {}
        for key in ("batch_combo_id", "combo_id"):
            value = nested.get(key)
            if value:
                nested_keys.append(str(value))
    for key in ("tid", "gift_tid", "batch_combo_id", "combo_id", "payflow_id", "rnd"):
        value = data.get(key)
        if value:
            nested_keys.append(str(value))
    if nested_keys:
        return f"{command_name(gift.raw)}:{gift.uid}:{gift.gift_id}:{':'.join(nested_keys)}"
    return f"{command_name(gift.raw)}:{gift.uid}:{gift.gift_id}:{gift.gift_name}:{gift.num}:{gift.total_coin}:{int(time.time())}"


async def obs_news_worker(
    paths: Civ6Paths,
    *,
    obs_text: Path | None,
    overlay_writer: OverlayJsonWriter | None,
    interval: float,
    limit: int,
) -> None:
    while True:
        ticker = await asyncio.to_thread(build_news_ticker, paths, limit=limit)
        if obs_text is not None:
            write_obs_text(obs_text, ticker)
        if overlay_writer is not None:
            current_item = await asyncio.to_thread(build_current_news_item, paths, limit=limit)
            overlay_writer.refresh_game(news_text=current_item)
        await asyncio.sleep(interval)


async def next_command(command_iter: object, stop_at: float | None) -> dict | None:
    if stop_at is None:
        try:
            return await command_iter.__anext__()
        except StopAsyncIteration:
            return None
    remaining = stop_at - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return await asyncio.wait_for(command_iter.__anext__(), timeout=remaining)
    except (asyncio.TimeoutError, StopAsyncIteration):
        return None


async def answer_worker(
    paths: Civ6Paths,
    queue: asyncio.Queue[ChatQuestion],
    *,
    obs_text: Path,
    overlay_writer: OverlayJsonWriter | None,
    no_llm: bool,
    model: str | None,
    env_file: str | Path,
    llm_timeout: float,
    context_limit: int,
    answer_char_limit: int,
) -> None:
    while True:
        question = await queue.get()
        write_obs_text(obs_text, format_obs_text(question, "思考中..."))
        if overlay_writer is not None:
            overlay_writer.set_question(question.uname, question.text, paid=question.paid)
        try:
            if no_llm:
                answer = "已收到问题。当前为测试模式，没有调用 LLM。"
            else:
                answer = await asyncio.to_thread(
                    answer_question_with_llm,
                    paths,
                    question.text,
                    model,
                    env_file,
                    llm_timeout,
                    context_limit,
                )
        except LLMError as exc:
            answer = f"暂时无法回答：{exc}"
        answer = sanitize_llm_output(answer, max_chars=answer_char_limit)
        write_obs_text(obs_text, format_obs_text(question, answer))
        if overlay_writer is not None:
            overlay_writer.set_answer(question.uname, question.text, answer, paid=question.paid)
            if not queue.empty():
                await asyncio.sleep(OVERLAY_FINAL_ANSWER_POLL_GRACE_SECONDS)
        print(f"answered: {question.uname}: {question.text}")


def answer_question_with_llm(
    paths: Civ6Paths,
    question: str,
    model: str | None,
    env_file: str | Path,
    llm_timeout: float,
    context_limit: int,
) -> str:
    load_env_file(env_file)
    snapshot = load_snapshot(paths)
    direct_answer = direct_game_answer(paths, snapshot, question)
    if direct_answer:
        return direct_answer
    prompt = llm_context_prompt(paths, snapshot, turn=None, limit=context_limit)
    return ask_openai(
        prompt,
        question,
        model=model,
        env_file=env_file,
        timeout=llm_timeout,
    ).text


def resolve_gift_question_cost(value: int | None) -> int:
    if value is not None and value > 0:
        return value
    env_value = os.getenv("CIV6_GIFT_QUESTION_COST", "").strip()
    if env_value:
        try:
            return max(int(env_value), 1)
        except ValueError:
            print(
                f"warning: invalid CIV6_GIFT_QUESTION_COST={env_value!r}; "
                f"using {DEFAULT_GIFT_QUESTION_COST}"
            )
    return DEFAULT_GIFT_QUESTION_COST


def format_gift_question_cost(coin_cost: int) -> str:
    batteries = coin_cost / GIFT_COIN_PER_BATTERY
    return f"{batteries:g}电池"


def danmaku_question(event: DanmakuEvent) -> ChatQuestion:
    return ChatQuestion(uid=event.uid, uname=event.uname, text=clean_question_text(event.text), source="danmaku")


def super_chat_question(event: SuperChatEvent) -> ChatQuestion:
    return ChatQuestion(uid=event.uid, uname=event.uname, text=event.message, source="super_chat", paid=True)


def is_question(text: str, mode: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if mode == "bang":
        return text.startswith(("!", "！"))
    if mode == "any":
        return True
    marker = text.endswith(("?", "？")) or text.startswith(("?", "？", "问:", "问：", "Q:", "q:"))
    if mode == "marker":
        return marker
    question_words = (
        "谁",
        "什么",
        "多少",
        "哪里",
        "哪",
        "怎么",
        "怎样",
        "为什么",
        "吗",
        "能不能",
        "可不可以",
        "进度",
        "胜利",
        "奇观",
        "伟人",
        "万神殿",
        "金币",
        "文化",
        "科技",
        "生产",
    )
    return marker or any(word in text for word in question_words)


def clean_question_text(text: str) -> str:
    text = text.strip()
    for prefix in ("!", "！", "?", "？", "问:", "问：", "Q:", "q:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def sanitize_llm_output(text: str, *, max_chars: int = DEFAULT_ANSWER_CHAR_LIMIT) -> str:
    text = strip_markdown(text)
    text = strip_player_slot_labels(text)
    text = strip_internal_context_terms(text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip("，。；、：,.;: ") + "…"
    return text


def strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", text)
    text = re.sub(r"(^|\n)\s*[-*+]\s+", r"\1", text)
    text = re.sub(r"(^|\n)\s*\d+[.)]\s+", r"\1", text)
    text = re.sub(r"(^|\n)\s*>\s*", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("<strong>", "").replace("</strong>", "").replace("<b>", "").replace("</b>", "")
    return text


def strip_player_slot_labels(text: str) -> str:
    text = re.sub(r"\bP\d+\s*", "", text)
    text = re.sub(r"（([^，）]+)，[^）]+）", r"（\1）", text)
    text = re.sub(r"\(([^,)]+),[^)]+\)", r"(\1)", text)
    return text


def strip_internal_context_terms(text: str) -> str:
    replacements = {
        "gold.transfers": "金币转账记录",
        "gold.totals_sent": "金币转账总计",
        "totals_sent": "金币转账总计",
        "transfers": "转账记录",
        "save_file": "存档信息",
        "players[]": "玩家信息",
        "players": "玩家信息",
        "display_zh": "中文名称",
        "turn_label": "回合",
        "*_zh": "中文名称",
        "JSON": "当前信息",
        "json": "当前信息",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("根据日志", "从当前信息看")
    text = text.replace("日志未提供", "当前信息暂缺")
    text = text.replace("日志里", "当前信息里")
    text = text.replace("日志中", "当前信息中")
    text = text.replace("这份上下文里", "当前信息里")
    text = text.replace("上下文里", "当前信息里")
    text = re.sub(
        r"当前信息里\s*金币转账记录\s*和\s*金币转账总计\s*都(?:是)?空的",
        "当前暂时没有玩家间金币转账记录",
        text,
    )
    text = text.replace("都是空的", "暂时没有记录")
    return text


def format_obs_text(question: ChatQuestion, answer: str) -> str:
    paid = "【付费】" if question.paid else ""
    return f"{paid}Q：{question.uname}：{question.text}\nA：{answer}"


def write_obs_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def current_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def gift_gate_description(gate: GiftGate) -> str:
    parts = ["Gift gate enabled"]
    if gate.gift_name:
        parts.append(f"name={gate.gift_name}")
    if gate.gift_id is not None:
        parts.append(f"id={gate.gift_id}")
    if gate.min_value is not None:
        parts.append(f"min_total_coin={gate.min_value}")
    parts.append(f"window={gate.window_seconds:.0f}s")
    if gate.consume:
        parts.append("consume=one question per gift")
    return "; ".join(parts)


def format_gift_event(gift: GiftEvent) -> str:
    source = command_name(gift.raw)
    user = gift.uname or f"uid {gift.uid}"
    value = f"; total_coin={gift.total_coin}" if gift.total_coin else ""
    gift_id = f"; id={gift.gift_id}" if gift.gift_id is not None else ""
    return f"gift[{source}]: {user}({gift.uid}) sent {gift.gift_name} x{gift.num}{gift_id}{value}"


def room_status_description(room_id: int, room_info: dict) -> str:
    status = {
        0: "offline",
        1: "live",
        2: "rounding",
    }.get(room_info.get("live_status"), str(room_info.get("live_status")))
    title = str(room_info.get("title") or "").strip()
    online = room_info.get("online")
    parts = [f"Bilibili room {room_id}: {status}"]
    if online is not None:
        parts.append(f"online={online}")
    if title:
        parts.append(f"title={title}")
    return "; ".join(parts)


def command_debug_line(command: dict, *, include_json: bool = False) -> str:
    cmd_name = command_name(command) or "<unknown>"
    parts = [cmd_name]
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    if data:
        for key in ("uname", "uid", "msg_type", "msg", "message", "text", "giftName", "num"):
            if key in data and data.get(key) not in (None, ""):
                parts.append(f"{key}={data.get(key)}")
    info = command.get("info")
    if isinstance(info, list) and len(info) > 2:
        user = info[2] if isinstance(info[2], list) else []
        uname = user[1] if len(user) > 1 else ""
        text = info[1] if len(info) > 1 else ""
        if uname:
            parts.append(f"uname={uname}")
        if text:
            parts.append(f"text={text}")
    if include_json:
        parts.append(f"raw={compact_command(command)}")
    return "; ".join(str(part) for part in parts)


def compact_command(command: dict, *, limit: int = 900) -> str:
    text = repr(command)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
