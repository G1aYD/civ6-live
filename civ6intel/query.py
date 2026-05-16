from __future__ import annotations

import json
import re

from .model import GameSnapshot, PlayerTurnStats


METRIC_WORDS = {
    "production": "production",
    "science": "science",
    "culture": "culture",
    "gold": "gold",
    "food": "food",
    "faith": "faith",
    "tourism": "tourism",
    "score": "score",
    "cities": "cities",
    "city": "cities",
    "population": "population",
    "pop": "population",
    "techs": "techs",
    "technology": "techs",
    "civics": "civics",
}


def answer_question(snapshot: GameSnapshot, question: str) -> str:
    normalized = question.lower()
    turn = extract_turn(normalized)
    if "current" in normalized or "latest" in normalized:
        turn = snapshot.latest_turn

    if "culture" in normalized and "victory" in normalized:
        return answer_culture_victory(snapshot, turn or snapshot.latest_turn)

    if "most" in normalized or "highest" in normalized or "leader" in normalized:
        for word, metric in METRIC_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", normalized):
                return answer_metric_leader(snapshot, metric, turn or snapshot.latest_turn)

    return (
        "I do not have a deterministic parser for that question yet. "
        "Use `python -m civ6intel.cli context --turn latest` to send compact game state to an LLM, "
        "or add a new rule in civ6intel/query.py."
    )


def answer_metric_leader(snapshot: GameSnapshot, metric: str, turn: int) -> str:
    rows = ranked_metric(snapshot, metric, turn)
    if not rows:
        return f"I could not find {metric} data for turn {turn}."

    winner_stats, winner_value = rows[0]
    winner = describe_player(snapshot, winner_stats)
    runner_text = ""
    if len(rows) > 1:
        second_stats, second_value = rows[1]
        runner_text = f" Second: {describe_player(snapshot, second_stats)} with {format_number(second_value)}."
    return f"Turn {turn}: {winner} has the most {metric}, at {format_number(winner_value)}.{runner_text}"


def answer_culture_victory(snapshot: GameSnapshot, turn: int) -> str:
    rows = ranked_metric(snapshot, "tourism", turn)
    if not rows:
        return (
            f"I could not find tourism data for turn {turn}. "
            "The exact culture victory screen requires domestic/visiting tourist counts, which are not present in these logs."
        )

    lines = [
        f"Turn {turn} culture victory proxy from logs:",
        "Exact visiting/domestic tourist thresholds are not in the parsed files, so this ranks tourism output plus culture/civics context.",
    ]
    for stats, tourism in rows[:5]:
        lines.append(
            f"- {describe_player(snapshot, stats)}: tourism {format_number(tourism)}, "
            f"culture/turn {format_optional(stats.culture_yield)}, civics {format_optional(stats.civics)}"
        )
    return "\n".join(lines)


def build_llm_context(snapshot: GameSnapshot, turn: int | None = None) -> dict:
    selected_turn = snapshot.latest_turn if turn is None else turn
    players = []
    for stats in snapshot.stats_for_turn(selected_turn):
        identity = snapshot.identity_for(stats.slot)
        if identity is None:
            continue
        players.append(
            {
                "slot": stats.slot,
                "player_name": identity.player_name,
                "network_name": identity.network_name,
                "display": describe_player(snapshot, stats),
                "civilization": identity.short_civ,
                "leader": identity.short_leader,
                "turn": stats.turn,
                "cities": stats.cities,
                "population": stats.population,
                "techs": stats.techs,
                "civics": stats.civics,
                "gold_balance": stats.gold_balance,
                "science_per_turn": stats.science_yield,
                "culture_per_turn": stats.culture_yield,
                "gold_per_turn": stats.gold_yield,
                "faith_per_turn": stats.faith_yield,
                "production_per_turn": stats.production_yield,
                "food_per_turn": stats.food_yield,
                "tourism": stats.tourism,
                "diplo_victory_points": stats.diplo_victory_points,
                "score": stats.score,
            }
        )
    return {
        "latest_turn": snapshot.latest_turn,
        "selected_turn": selected_turn,
        "known_limitations": [
            "Culture victory tourist thresholds are not available from Player_Stats logs.",
            "Duplicate civilizations are matched to slots by per-turn row order.",
        ],
        "players": players,
        "warnings": snapshot.warnings,
    }


def context_json(snapshot: GameSnapshot, turn: int | None = None) -> str:
    return json.dumps(build_llm_context(snapshot, turn), ensure_ascii=False, indent=2)


def ranked_metric(snapshot: GameSnapshot, metric: str, turn: int) -> list[tuple[PlayerTurnStats, float]]:
    rows: list[tuple[PlayerTurnStats, float]] = []
    for stats in snapshot.stats_for_turn(turn):
        if stats.slot is None:
            continue
        value = stats.value_for(metric)
        if value is None:
            continue
        rows.append((stats, value))
    rows.sort(key=lambda item: item[1], reverse=True)
    return rows


def describe_player(snapshot: GameSnapshot, stats: PlayerTurnStats) -> str:
    identity = snapshot.identity_for(stats.slot)
    if identity is None:
        return stats.raw_player
    civ = identity.short_civ or "unknown civ"
    if identity.player_name:
        return f"{identity.player_name} ({civ})"
    return civ


def extract_turn(question: str) -> int | None:
    match = re.search(r"\bturn\s+(\d+)\b", question)
    if not match:
        return None
    return int(match.group(1))


def format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def format_optional(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return format_number(value)
    return str(value)
