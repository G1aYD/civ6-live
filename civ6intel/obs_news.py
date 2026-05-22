from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import Civ6Paths
from .llm_context import build_llm_context, gold_transfer_amount_zh
from .model import GameSnapshot, PlayerIdentity
from .state import load_snapshot


DEFAULT_NEWS_EMPTY = "等待文明 6 战报..."
DEFAULT_SEPARATOR = "\n"
BBG_LEADER_IMAGE_BASE_URL = "https://raw.githubusercontent.com/civ6bbg/civ6bbg.github.io/main/images/leaders"
BBG_LEADER_IMAGE_API_URL = "https://api.github.com/repos/civ6bbg/civ6bbg.github.io/contents/images/leaders?ref=main"

CIV_BBG_IMAGE_NAMES = {
    "Babylon Stk": "Babylon",
    "Lime Thule": "Thule",
    "Maori": "Māori",
    "Ottoman": "Ottomans",
    "Lime Teotihuacan": "Teotihuacán",
    "Suk Swahili": "Swahili",
    "Suk Tibet": "Tibet",
    "Teotihuacan": "Teotihuacán",
}

LEADER_BBG_IMAGE_NAMES = {
    "Barbarossa": "Frederick Barbarossa",
    "Basil": "Basil II",
    "Canada Laurier": "Wilfrid Laurier",
    "Catherine De Medici": "Catherine de Medici (Black Queen)",
    "Catherine De Medici Alt": "Catherine de Medici (Magnificence)",
    "Catherine De Medici Expanded": "Catherine de Medici (Black Queen)",
    "Cleopatra": "Cleopatra (Egyptian)",
    "Cleopatra Alt": "Cleopatra (Ptolemaic)",
    "Eleanor England": "Eleanor of Aquitaine (England)",
    "Eleanor France": "Eleanor of Aquitaine (France)",
    "Elizabeth": "Elizabeth I",
    "Gitarja": "Gitarja",
    "Hardrada": "Harald Hardrada (Konge)",
    "Harald Alt": "Harald Hardrada (Varangian)",
    "Harald Hardrada": "Harald Hardrada (Konge)",
    "Hojo": "Hojo Tokimune",
    "Jayavarman": "Jayavarman VII",
    "Joao III": "João III",
    "Jfd Olympias": "Olympias",
    "Kublai Khan China": "Kublai Khan (China)",
    "Kublai Khan C": "Kublai Khan (China)",
    "Kublai Khan Mongolia": "Kublai Khan (Mongolia)",
    "Lady Six Sky": "Lady Six Sky",
    "Lady Trieu": "Bà Triệu",
    "Laurier": "Wilfrid Laurier",
    "Lime Teo Owl": "Spearthrower Owl",
    "Ll Tekinich II": "Te' K'inich II",
    "Lime Phoe Ahiram": "Ahiram",
    "Lime Thule Dave": "Kiviuq",
    "Ludwig": "Ludwig II",
    "Menelik": "Menelik II",
    "Mvemba": "Mvemba a Nzinga",
    "Mvemba A Nzinga": "Mvemba a Nzinga",
    "Pedro": "Pedro II",
    "Peter Great": "Peter",
    "Qin Shi Huang": "Qin (Mandate of Heaven)",
    "Qin": "Qin (Mandate of Heaven)",
    "Qin Alt": "Qin (Unifier)",
    "Ramses": "Ramses II",
    "Robert The Bruce": "Robert the Bruce",
    "Saladin": "Saladin (Sultan)",
    "Saladin Alt": "Saladin (Vizier)",
    "Simon Bolivar": "Simón Bolívar",
    "Suk Al Hasan": "Al-Hasan ibn Sulaiman",
    "Suk Trisong Detsen": "Trisong Detsen",
    "Suleiman": "Suleiman (Kanuni)",
    "Suleiman Alt": "Suleiman (Muhteşem)",
    "Suk Vercingetorix Dlc": "Vercingetorix",
    "Teddy Roosevelt": "Teddy Roosevelt (Bull Moose)",
    "T Roosevelt": "Teddy Roosevelt (Bull Moose)",
    "T Roosevelt Original": "Teddy Roosevelt (Bull Moose)",
    "T Roosevelt Roughrider": "Teddy Roosevelt (Rough Rider)",
    "Victoria": "Victoria (Age of Empire)",
    "Victoria Alt": "Victoria (Age of Steam)",
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
        duration = parse_turn(transfer.get("duration")) or 0
        amount = transfer.get("amount_zh") or gold_transfer_amount_zh(
            transfer.get("amount"),
            duration,
            transfer.get("timing"),
        )
        text = f"{label} 送出 {amount}" if label else f"送出 {amount}"
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
    filename = leader_icon_filename(identity)
    if not filename:
        return None
    return f"{BBG_LEADER_IMAGE_BASE_URL}/{quote(filename)}"


def leader_icon_filename(identity: PlayerIdentity) -> str | None:
    if identity.leader.startswith("LEADER_MINOR_CIV_") or "Minor Civ" in identity.short_leader:
        return None
    civ = CIV_BBG_IMAGE_NAMES.get(identity.short_civ, identity.short_civ)
    leader = LEADER_BBG_IMAGE_NAMES.get(identity.short_leader, identity.short_leader)
    if not civ or not leader:
        return None
    return f"{civ} {leader}.webp"


def player_icon_label(identity: PlayerIdentity) -> str:
    civ = CIV_BBG_IMAGE_NAMES.get(identity.short_civ, identity.short_civ)
    if identity.player_name:
        return f"{civ}（{identity.player_name}）"
    return civ


def check_leader_icons(
    paths: Civ6Paths,
    *,
    timeout: float = 5.0,
    offline: bool = False,
    all_known: bool = False,
) -> list[dict]:
    identities = all_known_icon_identities(paths) if all_known else current_game_icon_identities(paths)
    available_names: set[str] | None = None
    index_error: str | None = None
    if not offline:
        try:
            available_names = fetch_bbg_leader_image_names(timeout=timeout)
        except (OSError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            index_error = str(exc)

    rows = []
    for identity in identities:
        filename = leader_icon_filename(identity)
        if not filename:
            rows.append(
                {
                    "slot": identity.slot,
                    "player": player_icon_label(identity),
                    "civilization": identity.short_civ,
                    "leader": identity.short_leader,
                    "filename": None,
                    "url": None,
                    "status": "skip",
                    "note": "city-state or missing leader image",
                    "suggestions": [],
                }
            )
            continue
        url = f"{BBG_LEADER_IMAGE_BASE_URL}/{quote(filename)}"
        status = "unchecked"
        note = "offline check only" if offline else ""
        suggestions: list[str] = []
        if available_names is not None:
            if filename in available_names:
                status = "ok"
            else:
                status = "missing"
                suggestions = suggest_icon_filenames(filename, available_names, identity)
        elif not offline:
            status = "index_error"
            note = index_error or "could not fetch BBG leader image index"
        rows.append(
            {
                "slot": identity.slot,
                "player": player_icon_label(identity),
                "civilization": identity.short_civ,
                "leader": identity.short_leader,
                "filename": filename,
                "url": url,
                "status": status,
                "note": note,
                "suggestions": suggestions,
            }
        )
    return rows


def current_game_icon_identities(paths: Civ6Paths) -> list[PlayerIdentity]:
    snapshot = load_snapshot(paths)
    return [
        identity
        for slot, identity in sorted(snapshot.players.items())
        if identity is not None
    ]


def all_known_icon_identities(paths: Civ6Paths) -> list[PlayerIdentity]:
    path = paths.cache_dir / "DebugConfiguration.sqlite"
    if not path.exists():
        return current_game_icon_identities(paths)
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                """
                select CivilizationType, LeaderType
                from Players
                where HumanPlayable = 1
                  and CivilizationType is not null
                  and LeaderType is not null
                  and CivilizationType != 'CIVILIZATION_SPECTATOR'
                  and LeaderType != 'LEADER_SPECTATOR'
                order by Domain, CivilizationType, LeaderType
                """
            ).fetchall()
    except sqlite3.Error:
        return current_game_icon_identities(paths)

    seen: set[tuple[str, str]] = set()
    identities: list[PlayerIdentity] = []
    for civ, leader in rows:
        key = (str(civ), str(leader))
        if key in seen:
            continue
        seen.add(key)
        identities.append(
            PlayerIdentity(
                slot=len(identities),
                civilization=key[0],
                leader=key[1],
                is_human=True,
            )
        )
    return identities


def fetch_bbg_leader_image_names(*, timeout: float) -> set[str]:
    request = Request(
        BBG_LEADER_IMAGE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "civ6interaction-icon-check",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected GitHub API response for leader image index")
    return {str(item.get("name")) for item in data if isinstance(item, dict) and str(item.get("name", "")).endswith(".webp")}


def suggest_icon_filenames(filename: str, available_names: set[str], identity: PlayerIdentity, *, limit: int = 5) -> list[str]:
    civ = CIV_BBG_IMAGE_NAMES.get(identity.short_civ, identity.short_civ)
    leader = LEADER_BBG_IMAGE_NAMES.get(identity.short_leader, identity.short_leader)
    related = [
        name for name in sorted(available_names)
        if civ.casefold() in name.casefold() or leader.casefold() in name.casefold()
    ]
    if related:
        return related[:limit]
    return difflib.get_close_matches(filename, sorted(available_names), n=limit, cutoff=0.45)


def format_icon_check_report(rows: list[dict]) -> str:
    if not rows:
        return "No player identities found for icon check."
    lines = ["Leader icon check:"]
    for row in rows:
        status = str(row.get("status") or "unknown").upper()
        player = row.get("player") or f"slot {row.get('slot')}"
        filename = row.get("filename") or "-"
        line = f"- {status}: {player} -> {filename}"
        if row.get("note"):
            line += f" ({row['note']})"
        suggestions = row.get("suggestions") or []
        if suggestions:
            line += f"; suggestions: {', '.join(suggestions)}"
        lines.append(line)
    return "\n".join(lines)


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
