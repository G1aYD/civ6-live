from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from .config import Civ6Paths
from .model import GameSnapshot, PlayerIdentity, clean_game_name
from .query import describe_player, format_number


DEAL_TURN_RE = re.compile(r"^Turn (?P<turn>\d+), Enacting Deal id (?P<deal>\d+) for player (?P<a>\d+) and (?P<b>\d+)")
DEAL_GOLD_RE = re.compile(
    r"from player (?P<from>\d+), to player (?P<to>\d+), type Gold, .*?"
    r"value type (?P<value_type>[A-Z_]+), amount (?P<amount>-?\d+), duration (?P<duration>-?\d+)"
)


def baseline_intel(paths: Civ6Paths, snapshot: GameSnapshot) -> list[str]:
    lines: list[str] = []
    lines.extend(victory_progress_lines(snapshot))
    lines.extend(great_people_lines(paths, limit=8))
    lines.extend(build_queue_lines(paths, snapshot.latest_turn, limit=5))
    lines.extend(diplomacy_lines(paths, snapshot, limit=5))
    return lines


def change_intel(paths: Civ6Paths, snapshot: GameSnapshot, changed_names: set[str]) -> list[str]:
    lines: list[str] = []
    if changed_names & {"Player_Stats.csv", "Player_Stats_2.csv", "Game_PlayerScores.csv"}:
        lines.extend(victory_progress_lines(snapshot))
    if "Game_GreatPeople.csv" in changed_names:
        lines.extend(great_people_lines(paths, limit=8))
    if "City_BuildQueue.csv" in changed_names:
        lines.extend(build_queue_lines(paths, snapshot.latest_turn, limit=8))
    if changed_names & {"DiplomacySummary.csv", "DiplomacyDeals.log"}:
        lines.extend(diplomacy_lines(paths, snapshot, limit=8))
    if "DiplomacyDeals.log" in changed_names:
        lines.extend(gold_deal_lines(paths, limit=8))
    return lines


def victory_progress_lines(snapshot: GameSnapshot) -> list[str]:
    rows = [
        stats for stats in snapshot.stats_for_turn(snapshot.latest_turn)
        if stats.slot is not None and snapshot.identity_for(stats.slot) is not None
    ]
    if not rows:
        return []

    lines = ["Victory progress proxies:"]
    for stats in sorted(rows, key=lambda item: item.slot or 9999):
        score_bits = []
        for key, label in [
            ("CATEGORY_TECH", "tech score"),
            ("CATEGORY_CIVICS", "civic score"),
            ("CATEGORY_WONDER", "wonder score"),
            ("CATEGORY_RELIGION", "religion score"),
        ]:
            value = stats.score_categories.get(key)
            if value is not None:
                score_bits.append(f"{label} {format_number(value)}")
        score_text = f"; {', '.join(score_bits)}" if score_bits else ""
        lines.append(
            f"- {describe_player(snapshot, stats)}: "
            f"score {display(stats.score)}, techs {display(stats.techs)}, civics {display(stats.civics)}, "
            f"tourism {display(stats.tourism)}, diplo VP {display(stats.diplo_victory_points)}{score_text}"
        )
    return lines


def great_people_lines(paths: Civ6Paths, limit: int = 8) -> list[str]:
    path = paths.logs_dir / "Game_GreatPeople.csv"
    rows = latest_session_csv_dicts(path, "Turn")
    if not rows:
        return []

    latest_turn = max((parse_int(row.get("Turn", "")) or 0) for row in rows)
    latest = [row for row in rows if parse_int(row.get("Turn", "")) == latest_turn]
    if not latest:
        return []

    lines = [f"Great people timeline, turn {latest_turn}:"]
    for row in latest[:limit]:
        event = row.get("Event", "")
        individual = format_game_token(row.get("GP Individual", ""))
        gp_class = format_game_token(row.get("GP Class", ""))
        era = format_game_token(row.get("GP Era", ""))
        cost = row.get("GP Cost", "")
        recipient = parse_int(row.get("Recipient Player", ""))
        recipient_text = "" if recipient is None or recipient < 0 else f", recipient P{recipient}"
        if "activated" in event.lower():
            lines.append(f"- {individual}, {gp_class}, activated")
        else:
            lines.append(f"- {individual}, {era} {gp_class}, {event.lower()}, cost {cost}{recipient_text}")
    if len(latest) > limit:
        lines.append(f"- ... {len(latest) - limit} more")
    return lines


def build_queue_lines(paths: Civ6Paths, latest_turn: int, limit: int = 8) -> list[str]:
    path = paths.logs_dir / "City_BuildQueue.csv"
    rows = latest_session_csv_dicts(path, "Game Turn")
    if not rows:
        return []

    build_turn = max((parse_int(row.get("Game Turn", "")) or 0) for row in rows)
    latest = [row for row in rows if parse_int(row.get("Game Turn", "")) == build_turn]
    if not latest:
        return []

    lines = []
    if build_turn != latest_turn:
        return [f"Build queue last logged turn {build_turn}; latest parsed turn is {latest_turn}."]

    lines.append(f"Build queue, turn {build_turn}:")

    wonder_types = load_wonder_types(paths)
    wonder_rows = [row for row in latest if row.get("Current Item", "") in wonder_types]
    if wonder_rows:
        lines.append("Wonder builds:")
        rows_to_show = wonder_rows[:limit]
    else:
        lines.append("No active wonder builds found in latest build queue rows.")
        rows_to_show = latest[:limit]

    for row in rows_to_show:
        city = format_game_token(row.get("City", ""))
        item = format_game_token(row.get("Current Item", ""))
        current = row.get("Current Production", "?")
        needed = row.get("Production Needed", "?")
        added = row.get("Production Added", "?")
        lines.append(f"- {city}: {item}, {current}/{needed} production (+{added})")
    if len(latest) > len(rows_to_show) and not wonder_rows:
        lines.append(f"- ... {len(latest) - len(rows_to_show)} more build rows")
    return lines


def diplomacy_lines(paths: Civ6Paths, snapshot: GameSnapshot, limit: int = 8) -> list[str]:
    rows = latest_session_csv_dicts(paths.logs_dir / "DiplomacySummary.csv", "Game Turn")
    if not rows:
        return []

    human = human_slots(snapshot)
    interesting = []
    for row in rows:
        initiator = parse_int(row.get("Initiator", ""))
        recipient = parse_int(row.get("Recipient", ""))
        if initiator not in human and recipient not in human:
            continue
        action = row.get("Action", "").strip()
        details = row.get("Details", "").strip()
        if action == "Met" and not details:
            continue
        interesting.append(row)

    if not interesting:
        return []

    lines = ["Recent human diplomacy/deal signals:"]
    for row in interesting[-limit:]:
        turn = row.get("Game Turn", "?")
        initiator = format_player(snapshot.players, parse_int(row.get("Initiator", "")))
        recipient = format_player(snapshot.players, parse_int(row.get("Recipient", "")))
        action = row.get("Action", "").strip()
        details = row.get("Details", "").strip()
        mayhem = row.get("Mayhem", "").strip()
        detail_text = f" ({details})" if details else ""
        mayhem_text = f", mayhem {mayhem}" if mayhem else ""
        lines.append(f"- turn {turn}: {initiator} -> {recipient}: {action}{detail_text}{mayhem_text}")
    return lines


def gold_deal_lines(paths: Civ6Paths, limit: int = 8) -> list[str]:
    path = paths.logs_dir / "DiplomacyDeals.log"
    if not path.exists():
        return []

    deals: list[str] = []
    current_turn = "?"
    current_deal = "?"
    for line in latest_deal_session_lines(path):
            turn_match = DEAL_TURN_RE.search(line)
            if turn_match:
                current_turn = turn_match.group("turn")
                current_deal = turn_match.group("deal")
                continue
            if "Enacting Deal Item" not in line:
                continue
            gold_match = DEAL_GOLD_RE.search(line)
            if not gold_match:
                continue
            duration = parse_int(gold_match.group("duration")) or 0
            amount = gold_match.group("amount")
            amount_label = f"{amount} GPT for {duration} turns" if duration > 0 else f"{amount} gold"
            deals.append(
                f"turn {current_turn}: P{gold_match.group('from')} sent "
                f"{amount_label} to P{gold_match.group('to')} "
                f"(deal {current_deal})"
            )

    if not deals:
        return []
    return ["Gold deals:"] + [f"- {deal}" for deal in deals[-limit:]]


def latest_deal_session_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        lines = handle.readlines()

    start = 0
    previous_deal: int | None = None
    for index, line in enumerate(lines):
        turn_match = DEAL_TURN_RE.search(line)
        if not turn_match:
            continue
        deal = parse_int(turn_match.group("deal"))
        if deal is None:
            continue
        if previous_deal is not None and deal <= previous_deal:
            start = index
        previous_deal = deal
    return lines[start:]


def read_csv_dicts(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield {str(key).strip(): str(value).strip() for key, value in row.items() if key is not None}


def latest_session_csv_dicts(path: Path, turn_column: str) -> list[dict[str, str]]:
    rows = list(read_csv_dicts(path))
    start = 0
    previous_turn: int | None = None
    for index, row in enumerate(rows):
        turn = parse_int(row.get(turn_column, ""))
        if turn is None:
            continue
        if previous_turn is not None and turn < previous_turn:
            start = index
        previous_turn = turn
    return rows[start:]


def load_wonder_types(paths: Civ6Paths) -> set[str]:
    db_path = paths.cache_dir / "DebugGameplay.sqlite"
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute("select BuildingType from Buildings where IsWonder = 1").fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows}


def changed_file_names(paths: list[Path]) -> set[str]:
    return {path.name for path in paths}


def human_slots(snapshot: GameSnapshot) -> set[int]:
    return {
        slot for slot, player in snapshot.players.items()
        if player.is_human and player.civilization != "CIVILIZATION_SPECTATOR"
    }


def format_player(players: dict[int, PlayerIdentity], slot: int | None) -> str:
    if slot is None:
        return "unknown"
    player = players.get(slot)
    return player.label if player is not None else f"P{slot}"


def parse_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except (AttributeError, ValueError):
        return None


def display(value: object) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return format_number(value)
    return str(value)


def format_game_token(value: str) -> str:
    prefixes = [
        "GREAT_PERSON_INDIVIDUAL_",
        "GREAT_PERSON_CLASS_",
        "ERA_",
        "LOC_CITY_NAME_",
        "BUILDING_",
        "UNIT_",
        "PROJECT_",
        "TECH_",
        "CIVIC_",
        "BELIEF_",
    ]
    for prefix in prefixes:
        if value.startswith(prefix):
            return fix_acronyms(clean_game_name(value, prefix))
    return fix_acronyms(clean_game_name(value))


def fix_acronyms(value: str) -> str:
    replacements = {
        "Hg": "HG",
        "Jfk": "JFK",
    }
    return " ".join(replacements.get(part, part) for part in value.split())
