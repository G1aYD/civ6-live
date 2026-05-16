from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlayerIdentity:
    slot: int
    civilization: str
    leader: str
    is_human: bool = True
    player_name: str | None = None
    network_name: str | None = None
    team: int | None = None

    @property
    def short_civ(self) -> str:
        return clean_game_name(self.civilization, "CIVILIZATION_")

    @property
    def short_leader(self) -> str:
        return clean_game_name(self.leader, "LEADER_")

    @property
    def label(self) -> str:
        if self.player_name:
            return f"P{self.slot} {self.player_name} ({self.short_civ})"
        return f"P{self.slot} {self.short_civ}"


@dataclass
class PlayerTurnStats:
    turn: int
    raw_player: str
    slot: int | None = None
    cities: int | None = None
    population: int | None = None
    techs: int | None = None
    civics: int | None = None
    land_units: int | None = None
    naval_units: int | None = None
    gold_balance: float | None = None
    faith_balance: float | None = None
    science_yield: float | None = None
    culture_yield: float | None = None
    gold_yield: float | None = None
    faith_yield: float | None = None
    production_yield: float | None = None
    food_yield: float | None = None
    tourism: float | None = None
    diplo_victory_points: float | None = None
    score: float | None = None
    score_categories: dict[str, float] = field(default_factory=dict)

    def value_for(self, metric: str) -> float | None:
        aliases = {
            "production": "production_yield",
            "science": "science_yield",
            "culture": "culture_yield",
            "gold": "gold_yield",
            "food": "food_yield",
            "faith": "faith_yield",
            "tourism": "tourism",
            "score": "score",
            "cities": "cities",
            "population": "population",
            "techs": "techs",
            "civics": "civics",
        }
        attr = aliases.get(metric, metric)
        value = getattr(self, attr, None)
        return float(value) if value is not None else None


@dataclass
class GameSnapshot:
    latest_turn: int
    players: dict[int, PlayerIdentity]
    turns: dict[int, list[PlayerTurnStats]]
    warnings: list[str] = field(default_factory=list)

    def stats_for_turn(self, turn: int | None = None) -> list[PlayerTurnStats]:
        selected_turn = self.latest_turn if turn is None else turn
        return self.turns.get(selected_turn, [])

    def identity_for(self, slot: int | None) -> PlayerIdentity | None:
        if slot is None:
            return None
        return self.players.get(slot)


def clean_game_name(value: str, prefix: str = "") -> str:
    if not value:
        return value
    if prefix and value.startswith(prefix):
        value = value[len(prefix):]
    roman_numerals = {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"}
    words = []
    for part in value.split("_"):
        if not part:
            continue
        words.append(part if part in roman_numerals else part.capitalize())
    return " ".join(words)
