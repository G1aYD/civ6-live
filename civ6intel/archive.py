from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .model import GameSnapshot
from .parsers import (
    enrich_player_scores,
    enrich_player_stats_2,
    latest_text_session_lines,
    parse_gamecore_players,
    parse_player_stats,
)
from .watch_intel import format_game_token, latest_session_csv_dicts, parse_int


PANTHEON_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\].*GameCore (?:SEND|RECV(?: \(\d+\))?): "
    r"PlayerOperation:.*ePlayer=(?P<player>\d+).*?(?P<belief>BELIEF_[A-Z0-9_]+)"
)
DEAL_TURN_RE = re.compile(r"^Turn (?P<turn>\d+), Enacting Deal id (?P<deal>\d+) for player (?P<a>\d+) and (?P<b>\d+)")
DEAL_GOLD_RE = re.compile(
    r"from player (?P<from>\d+), to player (?P<to>\d+), type Gold, .*?"
    r"value type (?P<value_type>[A-Z_]+), amount (?P<amount>-?\d+), duration (?P<duration>-?\d+)"
)


DEFAULT_WONDER_TYPES = {
    "BUILDING_ALHAMBRA",
    "BUILDING_AMUNDSEN_SCOTT_RESEARCH_STATION",
    "BUILDING_ANGKOR_WAT",
    "BUILDING_APADANA",
    "BUILDING_BIG_BEN",
    "BUILDING_BIOSPHERE",
    "BUILDING_BOLSHOI_THEATRE",
    "BUILDING_BROADWAY",
    "BUILDING_CASA_DE_CONTRATACION",
    "BUILDING_CHICHEN_ITZA",
    "BUILDING_COLOSSUS",
    "BUILDING_COLOSSEUM",
    "BUILDING_CRISTO_REDENTOR",
    "BUILDING_EIFFEL_TOWER",
    "BUILDING_ESTADIO_DO_MARACANA",
    "BUILDING_ETEMENANKI",
    "BUILDING_FORBIDDEN_CITY",
    "BUILDING_GOLDEN_GATE_BRIDGE",
    "BUILDING_GREAT_BATH",
    "BUILDING_GREAT_LIBRARY",
    "BUILDING_GREAT_LIGHTHOUSE",
    "BUILDING_GREAT_ZIMBABWE",
    "BUILDING_HAGIA_SOPHIA",
    "BUILDING_HALICARNASSUS_MAUSOLEUM",
    "BUILDING_HANGING_GARDENS",
    "BUILDING_HERMITAGE",
    "BUILDING_HUEY_TEOCALLI",
    "BUILDING_JEBEL_BARKAL",
    "BUILDING_KILWA_KISIWANI",
    "BUILDING_KOTOKU_IN",
    "BUILDING_MACHU_PICCHU",
    "BUILDING_MAHABODHI_TEMPLE",
    "BUILDING_MEENAKSHI_TEMPLE",
    "BUILDING_MONT_ST_MICHEL",
    "BUILDING_ORACLE",
    "BUILDING_ORSZAGHAZ",
    "BUILDING_OXFORD_UNIVERSITY",
    "BUILDING_PANAMA_CANAL",
    "BUILDING_PETRA",
    "BUILDING_POTALA_PALACE",
    "BUILDING_PYRAMIDS",
    "BUILDING_RUHR_VALLEY",
    "BUILDING_ST_BASILS_CATHEDRAL",
    "BUILDING_STATUE_LIBERTY",
    "BUILDING_STATUE_OF_ZEUS",
    "BUILDING_STONEHENGE",
    "BUILDING_SYDNEY_OPERA_HOUSE",
    "BUILDING_TAJ_MAHAL",
    "BUILDING_TEMPLE_ARTEMIS",
    "BUILDING_TERRACOTTA_ARMY",
    "BUILDING_TORRE_DE_BELEM",
    "BUILDING_UNIVERSITY_SANKORE",
    "BUILDING_VENETIAN_ARSENAL",
}


@dataclass(frozen=True)
class FocusFinding:
    kind: str
    turn: int | None
    description: str
    source: str


def discover_log_dirs(path: Path) -> list[Path]:
    if (path / "GameCore.log").exists() or any(path.glob("*.csv")):
        return [path]
    return sorted(child for child in path.iterdir() if child.is_dir() and ((child / "GameCore.log").exists() or any(child.glob("*.csv"))))


def inspect_logs(root: Path, limit: int = 12) -> str:
    dirs = discover_log_dirs(root)
    if not dirs:
        return f"No Civ 6 log directories found under {root}."

    lines: list[str] = []
    for index, logs_dir in enumerate(dirs):
        if index:
            lines.append("")
        lines.extend(inspect_log_dir(logs_dir, limit=limit))
    return "\n".join(lines)


def inspect_log_dir(logs_dir: Path, limit: int = 12) -> list[str]:
    snapshot = load_snapshot_from_logs(logs_dir)
    human_count = sum(1 for player in snapshot.players.values() if player.is_human)
    ai_count = len(snapshot.players) - human_count

    lines = [f"{logs_dir}"]
    lines.append(f"- players parsed: {human_count} human, {ai_count} AI")
    lines.append(f"- latest parsed turn: {snapshot.latest_turn}")

    lines.extend(section_lines("Pantheon choices", parse_pantheon_choices(logs_dir), limit))
    lines.extend(section_lines("Great person takes", parse_great_person_takes(logs_dir), limit))
    lines.extend(section_lines("Wonder builds/completions", parse_wonder_findings(logs_dir), limit))

    notes = useful_file_notes(logs_dir)
    if notes:
        lines.append("Useful file notes:")
        lines.extend(f"- {note}" for note in notes)

    deals = parse_gold_deals(logs_dir)
    if deals:
        lines.append("Other useful signals:")
        lines.extend(format_findings(deals, limit=limit))

    return lines


def load_snapshot_from_logs(logs_dir: Path) -> GameSnapshot:
    players = parse_gamecore_players(logs_dir)
    turns = parse_player_stats(logs_dir, players)
    enrich_player_stats_2(logs_dir, turns)
    enrich_player_scores(logs_dir, turns)
    latest_turn = latest_turn_from_logs(logs_dir, turns)
    return GameSnapshot(latest_turn=latest_turn, players=players, turns=turns)


def latest_turn_from_logs(logs_dir: Path, turns: dict[int, object]) -> int:
    candidates = list(turns)
    for filename, column in [
        ("Game_PlayerScores.csv", "Game Turn"),
        ("Player_Stats.csv", "Turn"),
        ("Player_Stats_2.csv", "Turn"),
        ("City_BuildQueue.csv", "Game Turn"),
        ("Game_GreatPeople.csv", "Turn"),
    ]:
        for row in latest_session_csv_dicts(logs_dir / filename, column):
            turn = parse_int(row.get(column, ""))
            if turn is not None:
                candidates.append(turn)
    return max(candidates) if candidates else 0


def parse_pantheon_choices(logs_dir: Path) -> list[FocusFinding]:
    path = logs_dir / "net_message_debug.log"
    if not path.exists():
        return []

    findings: list[FocusFinding] = []
    seen: set[str] = set()
    for line in latest_text_session_lines(path):
        if "FOUND_PANTHEON" not in line:
            continue
        match = PANTHEON_RE.search(line)
        if not match:
            continue
        description = (
            f"{match.group('time')}: P{match.group('player')} chose "
            f"{format_game_token(match.group('belief'))}"
        )
        if description in seen:
            continue
        seen.add(description)
        findings.append(
            FocusFinding(
                kind="pantheon",
                turn=None,
                source=path.name,
                description=description,
            )
        )
    return findings


def parse_great_person_takes(logs_dir: Path) -> list[FocusFinding]:
    path = logs_dir / "Game_GreatPeople.csv"
    rows = latest_session_csv_dicts(path, "Turn")
    if not rows:
        return []

    findings: list[FocusFinding] = []
    timeline_rows = 0
    for row in rows:
        turn = parse_int(row.get("Turn", ""))
        event = row.get("Event", "")
        recipient = parse_int(row.get("Recipient Player", ""))
        individual = format_game_token(row.get("GP Individual", ""))
        gp_class = format_game_token(row.get("GP Class", ""))
        is_timeline_only = event.lower() == "added to present timeline" and (recipient is None or recipient < 0)
        if is_timeline_only:
            timeline_rows += 1
            continue
        event_text = event or "great person event"
        if "granted" in event_text.lower():
            recipient_text = "unknown player" if recipient is None or recipient < 0 else f"P{recipient}"
            description = f"turn {turn}: {individual} ({gp_class}) granted to {recipient_text}"
        elif "activated" in event_text.lower():
            description = f"turn {turn}: {individual} ({gp_class}) activated"
        else:
            recipient_text = "unknown player" if recipient is None or recipient < 0 else f"P{recipient}"
            description = f"turn {turn}: {recipient_text} {event_text.lower()} {individual} ({gp_class})"
        findings.append(
            FocusFinding(
                kind="great_person",
                turn=turn,
                source=path.name,
                description=description,
            )
        )

    if findings:
        return findings

    latest_turn = max((parse_int(row.get("Turn", "")) or 0) for row in rows)
    return [
        FocusFinding(
            kind="great_person_note",
            turn=latest_turn,
            source=path.name,
            description=(
                f"no claim/take rows found; {timeline_rows} timeline availability rows "
                f"exist through turn {latest_turn}"
            ),
        )
    ]


def parse_wonder_findings(logs_dir: Path) -> list[FocusFinding]:
    findings: list[FocusFinding] = []
    findings.extend(parse_wonder_score_changes(logs_dir))
    findings.extend(parse_wonder_build_queue(logs_dir))
    return sorted(findings, key=lambda finding: (finding.turn is None, finding.turn or 0, finding.description))


def parse_wonder_score_changes(logs_dir: Path) -> list[FocusFinding]:
    path = logs_dir / "Game_PlayerScores.csv"
    rows = latest_session_csv_dicts(path, "Game Turn")
    if not rows:
        return []

    by_player: dict[int, list[tuple[int, float]]] = {}
    for row in rows:
        turn = parse_int(row.get("Game Turn", ""))
        player = parse_int(row.get("Player", ""))
        value = parse_float(row.get("CATEGORY_WONDER", ""))
        if turn is None or player is None or value is None:
            continue
        by_player.setdefault(player, []).append((turn, value))

    findings: list[FocusFinding] = []
    for player, values in by_player.items():
        last_value: float | None = None
        for turn, value in sorted(values):
            if last_value is not None and value > last_value:
                delta = value - last_value
                findings.append(
                    FocusFinding(
                        kind="wonder_score",
                        turn=turn,
                        source=path.name,
                        description=(
                            f"turn {turn}: P{player} wonder score increased by "
                            f"{format_delta(delta)} to {format_delta(value)}"
                        ),
                    )
                )
            last_value = value
    return findings


def parse_wonder_build_queue(logs_dir: Path) -> list[FocusFinding]:
    path = logs_dir / "City_BuildQueue.csv"
    findings: list[FocusFinding] = []
    for row in latest_session_csv_dicts(path, "Game Turn"):
        item = row.get("Current Item", "")
        if item not in DEFAULT_WONDER_TYPES:
            continue
        turn = parse_int(row.get("Game Turn", ""))
        current = parse_float(row.get("Current Production", ""))
        needed = parse_float(row.get("Production Needed", ""))
        city = format_game_token(row.get("City", ""))
        item_text = format_game_token(item)
        if current is not None and needed is not None and current >= needed:
            description = f"turn {turn}: {city} completed {item_text} ({format_delta(current)}/{format_delta(needed)})"
        else:
            description = f"turn {turn}: {city} building {item_text} ({display_number(current)}/{display_number(needed)})"
        findings.append(FocusFinding(kind="wonder_queue", turn=turn, source=path.name, description=description))
    return findings


def parse_gold_deals(logs_dir: Path) -> list[FocusFinding]:
    path = logs_dir / "DiplomacyDeals.log"
    if not path.exists():
        return []

    findings: list[FocusFinding] = []
    current_turn: int | None = None
    current_deal: str | None = None
    for line in latest_deal_session_lines(path):
            turn_match = DEAL_TURN_RE.search(line)
            if turn_match:
                current_turn = parse_int(turn_match.group("turn"))
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
            findings.append(
                FocusFinding(
                    kind="gold_deal",
                    turn=current_turn,
                    source=path.name,
                    description=(
                        f"turn {current_turn}: P{gold_match.group('from')} sent "
                        f"{amount_label} to P{gold_match.group('to')} "
                        f"(deal {current_deal})"
                    ),
                )
            )
    return findings


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


def useful_file_notes(logs_dir: Path) -> list[str]:
    notes: list[str] = []
    if (logs_dir / "AI_Religious.csv").exists():
        notes.append("AI_Religious.csv is belief scoring/evaluation; it is not reliable evidence of chosen pantheons.")
    if (logs_dir / "UserInterface.log").exists():
        notes.append("UserInterface.log shows WonderBuiltPopup UI loading, but not the actual wonder name or owner.")
    if (logs_dir / "Lua.log").exists():
        notes.append("Lua.log contains natural-wonder map generation; useful for map context, not built world wonders.")
    if (logs_dir / "Game_GreatPeople.csv").exists():
        notes.append("Game_GreatPeople.csv can expose timeline availability; claim/take rows require recipient/event rows that are absent in these samples.")
    return notes


def section_lines(title: str, findings: list[FocusFinding], limit: int) -> list[str]:
    lines = [f"{title}:"]
    if not findings:
        lines.append("- none found")
        return lines
    lines.extend(format_findings(findings, limit=limit))
    return lines


def format_findings(findings: list[FocusFinding], limit: int) -> list[str]:
    lines = [f"- {finding.description} [{finding.source}]" for finding in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"- ... {len(findings) - limit} more")
    return lines


def parse_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None


def display_number(value: float | None) -> str:
    if value is None:
        return "?"
    return format_delta(value)


def format_delta(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"
