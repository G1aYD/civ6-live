from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import quote

from .config import Civ6Paths
from .llm_context import build_llm_context
from .model import GameSnapshot, PlayerIdentity
from .state import load_snapshot


DEFAULT_NEWS_EMPTY = "等待文明 6 战报..."
DEFAULT_SEPARATOR = "\n"
BBG_LEADER_IMAGE_BASE_URL = "https://raw.githubusercontent.com/civ6bbg/civ6bbg.github.io/main/images/leaders"

CIV_BBG_IMAGE_NAMES = {
    "Babylon Stk": "Babylon",
    "Lime Thule": "Thule",
    "Maori": "Māori",
    "Ottoman": "Ottomans",
    "Suk Swahili": "Swahili",
    "Teotihuacan": "Teotihuacán",
}

LEADER_BBG_IMAGE_NAMES = {
    "Catherine De Medici": "Catherine de Medici (Black Queen)",
    "Cleopatra": "Cleopatra (Egyptian)",
    "Gitarja": "Gitarja",
    "Harald Hardrada": "Harald Hardrada (Konge)",
    "Joao III": "João III",
    "Lady Six Sky": "Lady Six Sky",
    "Lady Trieu": "Bà Triệu",
    "Lime Thule Dave": "Kiviuq",
    "Mvemba A Nzinga": "Mvemba a Nzinga",
    "Pedro": "Pedro II",
    "Qin Shi Huang": "Qin (Mandate of Heaven)",
    "Saladin": "Saladin (Sultan)",
    "Simon Bolivar": "Simón Bolívar",
    "Suk Al Hasan": "Al-Hasan ibn Sulaiman",
    "Suleiman": "Suleiman (Kanuni)",
    "Teddy Roosevelt": "Teddy Roosevelt (Bull Moose)",
}


def build_news_entries(paths: Civ6Paths, *, limit: int = 20) -> list[dict]:
    snapshot = load_snapshot(paths)
    context = build_llm_context(paths, snapshot, turn=None, limit=limit)
    items: list[tuple[tuple[int, str, int], dict]] = []

    for pantheon in context.get("pantheons", []):
        belief = pantheon.get("pantheon_zh") or pantheon.get("pantheon") or "未知万神殿"
        items.append(
            (
                (0, str(pantheon.get("timestamp") or ""), 0),
                news_entry(
                    f"选择万神殿：{belief}",
                    leader_icons_for_slots(snapshot, [pantheon.get("slot")]),
                ),
            )
        )

    for event in context.get("great_people", []):
        event_name = str(event.get("event") or "")
        person = event.get("great_person_zh") or event.get("great_person") or "未知伟人"
        gp_class = event.get("class_zh") or event.get("class") or ""
        label = event.get("turn_label") or turn_label(event.get("turn"))
        if event_name != "Granted to Player":
            continue
        detail = f"获得{gp_class}：{person}" if gp_class else f"获得伟人：{person}"
        items.append(
            (
                (parse_turn(event.get("turn")), "", 1),
                news_entry(
                    f"{label} {detail}" if label else detail,
                    leader_icons_for_slots(snapshot, [event.get("recipient_slot")]),
                ),
            )
        )

    wonders = context.get("wonders", {}) if isinstance(context.get("wonders"), dict) else {}
    for wonder in wonders.get("completed", []):
        player_or_city = wonder.get("city_zh") or wonder.get("city") or "未知城市"
        wonder_name = wonder.get("wonder_zh") or wonder.get("wonder") or "未知奇观"
        label = wonder.get("turn_label") or turn_label(wonder.get("turn"))
        text = f"{label} {player_or_city} 完成奇观：{wonder_name}" if label else f"{player_or_city} 完成奇观：{wonder_name}"
        items.append(
            (
                (parse_turn(wonder.get("turn")), "", 2),
                news_entry(text, leader_icons_for_slots(snapshot, [wonder.get("slot")])),
            )
        )

    gold = context.get("gold", {}) if isinstance(context.get("gold"), dict) else {}
    for transfer in gold.get("transfers", []):
        label = transfer.get("turn_label") or turn_label(transfer.get("turn"))
        amount = format_amount(transfer.get("amount"))
        text = f"{label} 送出 {amount} 金币" if label else f"送出 {amount} 金币"
        items.append(
            (
                (parse_turn(transfer.get("turn")), "", 3),
                news_entry(
                    text,
                    leader_icons_for_slots(snapshot, [transfer.get("from_slot"), transfer.get("to_slot")]),
                ),
            )
        )

    seen: set[str] = set()
    ordered: list[dict] = []
    for _sort_key, entry in sorted(items, key=lambda item: item[0]):
        key = news_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(entry)
    return ordered[-limit:]


def build_news_items(paths: Civ6Paths, *, limit: int = 20) -> list[str]:
    return [entry["text"] for entry in build_news_entries(paths, limit=limit)]


def build_news_ticker(paths: Civ6Paths, *, limit: int = 20, separator: str = DEFAULT_SEPARATOR) -> str:
    return build_current_news_item(paths, limit=limit)


def build_current_news_item(
    paths: Civ6Paths,
    *,
    limit: int = 20,
) -> str:
    items = build_news_items(paths, limit=limit)
    if not items:
        return DEFAULT_NEWS_EMPTY
    return items[-1]


def news_entry(text: str, icons: list[dict]) -> dict:
    return {
        "text": text,
        "icons": icons,
    }


def news_entry_key(entry: dict) -> str:
    icon_key = ",".join(str(icon.get("slot")) for icon in entry.get("icons", []))
    return f"{icon_key}|{entry.get('text', '')}"


def leader_icons_for_slots(snapshot: GameSnapshot, slots: list[object]) -> list[dict]:
    icons = []
    seen: set[int] = set()
    for value in slots:
        try:
            slot = int(value)
        except (TypeError, ValueError):
            continue
        if slot in seen:
            continue
        seen.add(slot)
        identity = snapshot.identity_for(slot)
        if identity is None:
            continue
        icon_url = leader_icon_url(identity)
        if not icon_url:
            continue
        icons.append(
            {
                "slot": slot,
                "url": icon_url,
                "label": player_icon_label(identity),
                "civilization": identity.short_civ,
                "leader": identity.short_leader,
            }
        )
    return icons


def leader_icon_url(identity: PlayerIdentity) -> str | None:
    if identity.leader.startswith("LEADER_MINOR_CIV_") or "Minor Civ" in identity.short_leader:
        return None
    civ = CIV_BBG_IMAGE_NAMES.get(identity.short_civ, identity.short_civ)
    leader = LEADER_BBG_IMAGE_NAMES.get(identity.short_leader, identity.short_leader)
    if not civ or not leader:
        return None
    filename = f"{civ} {leader}.webp"
    return f"{BBG_LEADER_IMAGE_BASE_URL}/{quote(filename)}"


def player_icon_label(identity: PlayerIdentity) -> str:
    civ = CIV_BBG_IMAGE_NAMES.get(identity.short_civ, identity.short_civ)
    if identity.player_name:
        return f"{civ}（{identity.player_name}）"
    return civ


def run_obs_news(
    paths: Civ6Paths,
    *,
    obs_text: Path,
    interval: float = 2.0,
    limit: int = 20,
    separator: str = DEFAULT_SEPARATOR,
    once: bool = False,
    duration: float | None = None,
) -> None:
    stop_at = time.monotonic() + duration if duration else None
    while True:
        ticker = build_news_ticker(paths, limit=limit, separator=separator)
        write_obs_text(obs_text, ticker)
        print(ticker)
        if once:
            return
        if stop_at is not None and time.monotonic() >= stop_at:
            return
        time.sleep(interval)


def parse_turn(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return -1


def turn_label(value: object) -> str | None:
    turn = parse_turn(value)
    if turn < 0:
        return None
    return f"{turn}T"


def format_amount(value: object) -> str:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return "?"
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def short_player_label(value: object) -> str:
    text = str(value)
    if text.startswith("P"):
        parts = text.split(" ", 1)
        if len(parts) == 2 and parts[0][1:].isdigit():
            text = parts[1]
    text = re.sub(r"（([^，）]+)，[^）]+）", r"（\1）", text)
    text = re.sub(r"\(([^,)]+),[^)]+\)", r"(\1)", text)
    return text


def write_obs_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
