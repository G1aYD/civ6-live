from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Civ6Paths
from .model import PlayerIdentity, clean_game_name
from .parsers import latest_text_session_lines, parse_gamecore_players


NET_PREFIX = r"GameCore (?:SEND|RECV(?: \(\d+\))?):"
NET_UNIT_RE = re.compile(
    rf"^\[(?P<time>[^\]]+)\]\s+{NET_PREFIX} UnitOperation: "
    r"Unit=(?P<unit>\d+), Owner=(?P<owner>\d+).*?"
    r"eOperation: (?P<operation>UNITOPERATION_[A-Z0-9_]+)"
)
DEST_RE = re.compile(r"dest: (?P<x>-?\d+), (?P<y>-?\d+)")
NET_PLAYER_RE = re.compile(
    rf"^\[(?P<time>[^\]]+)\]\s+{NET_PREFIX} PlayerOperation: "
    r"ePlayer=(?P<player>\d+),.*?, (?P<operation>[A-Z_]+)(?:, (?P<target>[A-Z0-9_]+))?"
)
NET_CITY_RE = re.compile(
    rf"^\[(?P<time>[^\]]+)\]\s+{NET_PREFIX} CityOperation: "
    r"ePlayer=(?P<player>\d+), eCity=(?P<city>\d+), eOperation=(?P<operation>0x[0-9A-Fa-f]+)"
)
NET_PAYLOAD_RE = re.compile(rf"^\[(?P<time>[^\]]+)\]\s+{NET_PREFIX} (?P<payload>.*)")
NET_DEAL_TERMS = ("GOLD", "YIELD_GOLD", "DEAL", "DIPLOMACY", "TRADE")
PLAYER_ID_RE = re.compile(r"(?:ePlayer|Owner|Player|m_playerID)[= (](\d+)")
IMPORTANT_PLAYER_OPERATIONS = {
    "ACCEPT_EMERGENCY",
    "ASSIGN_GOVERNOR",
    "CHANGE_GOVERNMENT",
    "FOUND_PANTHEON",
    "FOUND_RELIGION",
    "GIVE_INFLUENCE_TOKEN",
    "PROGRESS_CIVIC",
    "PROMOTE_GOVERNOR",
    "RECRUIT_GREAT_PERSON",
    "RESEARCH",
}
IMPORTANT_UNIT_OPERATIONS = {
    "UNITOPERATION_BUILD_IMPROVEMENT",
    "UNITOPERATION_FOUND_CITY",
    "UNITOPERATION_REMOVE_FEATURE",
}


@dataclass(frozen=True)
class LiveEvent:
    source: str
    turn: int | None
    timestamp: str | None
    description: str
    important: bool = True


def human_live_events(paths: Civ6Paths, limit: int = 40, important_only: bool = True) -> list[LiveEvent]:
    players = parse_gamecore_players(paths.logs_dir)
    human_slots = {
        slot for slot, player in players.items()
        if player.is_human and player.civilization != "CIVILIZATION_SPECTATOR"
    }
    events: list[LiveEvent] = []
    events.extend(parse_net_messages(paths.logs_dir / "net_message_debug.log", human_slots))
    events.extend(parse_astar_app(paths.logs_dir / "AStar_APP.log", players, human_slots))
    events = dedupe_events(events)
    if important_only:
        events = [event for event in events if event.important]
    events.sort(key=event_sort_key)
    return events[-limit:]


def parse_net_messages(path: Path, human_slots: set[int]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    if not path.exists():
        return events

    for line in latest_text_session_lines(path):
        unit_match = NET_UNIT_RE.search(line)
        if unit_match:
            owner = int(unit_match.group("owner"))
            if owner not in human_slots:
                continue
            operation = unit_match.group("operation")
            unit = unit_match.group("unit")
            dest_match = DEST_RE.search(line)
            x = dest_match.group("x") if dest_match else None
            y = dest_match.group("y") if dest_match else None
            dest = f" to ({x}, {y})" if x is not None and y is not None else ""
            events.append(
                LiveEvent(
                    source="net_message_debug.log",
                    turn=None,
                    timestamp=unit_match.group("time"),
                    description=describe_unit_operation(owner, operation, unit, dest),
                    important=operation in IMPORTANT_UNIT_OPERATIONS,
                )
            )
            continue

        player_match = NET_PLAYER_RE.search(line)
        if player_match:
            player = int(player_match.group("player"))
            if player not in human_slots:
                continue
            operation = clean_game_name(player_match.group("operation"))
            raw_operation = player_match.group("operation")
            target_value = player_match.group("target")
            events.append(
                LiveEvent(
                    source="net_message_debug.log",
                    turn=None,
                    timestamp=player_match.group("time"),
                    description=describe_player_operation(player, raw_operation, target_value),
                    important=raw_operation in IMPORTANT_PLAYER_OPERATIONS,
                )
            )
            continue

        city_match = NET_CITY_RE.search(line)
        if city_match:
            player = int(city_match.group("player"))
            if player not in human_slots:
                continue
            events.append(
                LiveEvent(
                    source="net_message_debug.log",
                    turn=None,
                    timestamp=city_match.group("time"),
                    description=(
                        f"P{player} city operation {city_match.group('operation')} "
                        f"on city {city_match.group('city')}"
                    ),
                    important=False,
                )
            )
            continue

        deal_event = parse_deal_like_net_message(line, human_slots)
        if deal_event is not None:
            events.append(deal_event)
    return events


def parse_deal_like_net_message(line: str, human_slots: set[int]) -> LiveEvent | None:
    payload_match = NET_PAYLOAD_RE.search(line)
    if not payload_match:
        return None
    upper = line.upper()
    if not any(term in upper for term in NET_DEAL_TERMS):
        return None

    players = {int(match.group(1)) for match in PLAYER_ID_RE.finditer(line)}
    if players and players.isdisjoint(human_slots):
        return None

    payload = payload_match.group("payload").strip()
    if len(payload) > 180:
        payload = payload[:177] + "..."
    return LiveEvent(
        source="net_message_debug.log",
        turn=None,
        timestamp=payload_match.group("time"),
        description=f"possible deal/gold event: {payload}",
        important=True,
    )


def describe_player_operation(player: int, operation: str, target_value: str | None) -> str:
    target = format_event_token(target_value) if target_value else ""
    if operation == "FOUND_PANTHEON" and target:
        return f"P{player} chose pantheon {target}"
    if operation == "FOUND_RELIGION" and target:
        return f"P{player} founded religion {target}"
    if operation == "RECRUIT_GREAT_PERSON" and target:
        return f"P{player} recruited great person {target}"
    if operation == "RESEARCH" and target:
        return f"P{player} selected research {target}"
    if operation == "PROGRESS_CIVIC" and target:
        return f"P{player} selected civic {target}"
    if operation == "GIVE_INFLUENCE_TOKEN":
        return f"P{player} sent influence token"
    if operation == "ACCEPT_EMERGENCY":
        return f"P{player} accepted emergency"
    if operation in {"ASSIGN_GOVERNOR", "PROMOTE_GOVERNOR"}:
        return f"P{player} {clean_game_name(operation).lower()}"
    target_text = f" {target}" if target else ""
    return f"P{player} selected {clean_game_name(operation).lower()}{target_text}"


def describe_unit_operation(owner: int, operation: str, unit: str, dest: str) -> str:
    if operation == "UNITOPERATION_FOUND_CITY":
        return f"P{owner} founded city with unit {unit}"
    if operation == "UNITOPERATION_BUILD_IMPROVEMENT":
        return f"P{owner} built improvement with unit {unit}"
    if operation == "UNITOPERATION_REMOVE_FEATURE":
        return f"P{owner} removed feature with unit {unit}"
    return f"P{owner} {operation} unit {unit}{dest}"


def format_event_token(value: str | None) -> str:
    if not value:
        return ""
    for prefix in [
        "GREAT_PERSON_INDIVIDUAL_",
        "GREAT_PERSON_CLASS_",
        "BELIEF_",
        "TECH_",
        "CIVIC_",
        "GOVERNOR_",
        "RELIGION_",
    ]:
        if value.startswith(prefix):
            return clean_game_name(value, prefix)
    return clean_game_name(value)


def parse_astar_app(
    path: Path,
    players: dict[int, PlayerIdentity],
    human_slots: set[int],
) -> list[LiveEvent]:
    if not path.exists():
        return []

    human_civ_names = {
        f"LOC_{players[slot].civilization}_NAME"
        for slot in human_slots
        if slot in players
    }
    events: list[LiveEvent] = []
    seen: set[tuple[object, ...]] = set()
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            if len(row) < 8:
                continue
            player = row[1].strip()
            if player not in human_civ_names:
                continue
            to_x = parse_int(row[5])
            to_y = parse_int(row[6])
            if to_x is None or to_y is None or (to_x == -9999 and to_y == -9999):
                continue
            turn = parse_int(row[0])
            from_x = parse_int(row[3])
            from_y = parse_int(row[4])
            key = (turn, player, row[2].strip(), from_x, from_y, to_x, to_y)
            if key in seen:
                continue
            seen.add(key)
            unit = clean_unit_name(row[2].strip())
            civ = player.removeprefix("LOC_CIVILIZATION_").removesuffix("_NAME").title()
            events.append(
                LiveEvent(
                    source="AStar_APP.log",
                    turn=turn,
                    timestamp=None,
                    description=f"{civ} {unit} moved from ({from_x}, {from_y}) to ({to_x}, {to_y})",
                    important=False,
                )
            )
    return events


def dedupe_events(events: list[LiveEvent]) -> list[LiveEvent]:
    seen: set[tuple[object, ...]] = set()
    unique: list[LiveEvent] = []
    for event in events:
        key = (event.source, event.turn, event.timestamp, event.description)
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def format_events(events: list[LiveEvent]) -> str:
    if not events:
        return "No human live events found."
    lines = []
    for event in events:
        prefix = event.timestamp or (f"turn {event.turn}" if event.turn is not None else event.source)
        lines.append(f"- {prefix}: {event.description} [{event.source}]")
    return "\n".join(lines)


def event_sort_key(event: LiveEvent) -> tuple[int, str]:
    if event.timestamp is not None:
        return (1, event.timestamp)
    if event.turn is not None:
        return (0, f"{event.turn:06d}")
    return (0, "")


def parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


def clean_unit_name(value: str) -> str:
    unit_name = value.split("(", 1)[0].strip()
    return clean_game_name(unit_name, "UNIT_")
