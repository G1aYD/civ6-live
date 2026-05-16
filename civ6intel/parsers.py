from __future__ import annotations

import csv
import re
from collections import defaultdict, deque
from pathlib import Path

from .model import PlayerIdentity, PlayerTurnStats


GAMECORE_PLAYER_RE = re.compile(
    r"Player\s+(?P<slot>\d+):\s+Civilization\s+-\s+"
    r"(?P<civ>CIVILIZATION_[A-Z0-9_]+).*?"
    r"Leader\s+-\s+(?P<leader>LEADER_[A-Z0-9_]+).*?"
    r"SlotStatus\s+-\s+(?P<status>\w+)"
)
NET_PLAYER_INFO_RE = re.compile(
    r"NetPlayerInfo :.*?PLAYER_ID=(?:Int|UInt)\((?P<slot>-?\d+)\).*?"
    r"NETWORK_NAME=Char\{(?P<network_name>[^}]*)\}.*?"
    r"NICK_NAME=Char\{(?P<player_name>[^}]*)\}"
)
NET_PLAYER_BLOCK_RE = re.compile(r"PLAYER_\d+=TypedVariantMap\(")
NET_PLAYER_ID_RE = re.compile(r"PLAYER_ID=(?:Int|UInt)\((?P<slot>-?\d+)\)")
NET_FIELD_RE = re.compile(r"(?P<key>[A-Z_]+)=Char\{(?P<value>[^}]*)\}")
NET_INT_FIELD_RE = re.compile(r"(?P<key>[A-Z_]+)=(?:Int|UInt)\((?P<value>-?\d+)\)")


def parse_gamecore_players(logs_dir: Path) -> dict[int, PlayerIdentity]:
    path = logs_dir / "GameCore.log"
    players: dict[int, PlayerIdentity] = {}

    network_players = parse_network_player_names(logs_dir)
    if path.exists():
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                match = GAMECORE_PLAYER_RE.search(line)
                if not match:
                    continue
                slot = int(match.group("slot"))
                network_info = network_players.get(slot, {})
                players[slot] = PlayerIdentity(
                    slot=slot,
                    civilization=match.group("civ"),
                    leader=match.group("leader"),
                    is_human=match.group("status").lower() == "human",
                    player_name=network_info.get("player_name") or None,
                    network_name=network_info.get("network_name") or None,
                    team=parse_int(network_info.get("team", "")),
                )
    for slot, network_identity in parse_network_player_identities(logs_dir).items():
        if slot not in players:
            players[slot] = network_identity
            continue
        current = players[slot]
        if (
            not current.player_name
            or not current.network_name
            or current.team is None
            or current.civilization == "CIVILIZATION_SPECTATOR"
        ):
            players[slot] = PlayerIdentity(
                slot=slot,
                civilization=network_identity.civilization if current.civilization == "CIVILIZATION_SPECTATOR" else current.civilization,
                leader=network_identity.leader if current.leader == "LEADER_SPECTATOR" else current.leader,
                is_human=current.is_human or network_identity.is_human,
                player_name=current.player_name or network_identity.player_name,
                network_name=current.network_name or network_identity.network_name,
                team=current.team if current.team is not None else network_identity.team,
            )
    return players


def parse_network_player_identities(logs_dir: Path) -> dict[int, PlayerIdentity]:
    path = logs_dir / "net_message_debug.log"
    if not path.exists():
        return {}

    players: dict[int, PlayerIdentity] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if "NetPlayerInfo" in line:
                identity = parse_network_player_block(line)
                if identity is not None:
                    players[identity.slot] = identity
                continue
            if "NetGameConfig" not in line:
                continue
            for block in split_net_player_blocks(line):
                identity = parse_network_player_block(block)
                if identity is not None:
                    players[identity.slot] = identity
    return players


def split_net_player_blocks(line: str) -> list[str]:
    matches = list(NET_PLAYER_BLOCK_RE.finditer(line))
    blocks: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        blocks.append(line[start:end])
    return blocks


def parse_network_player_block(block: str) -> PlayerIdentity | None:
    fields = {match.group("key"): match.group("value") for match in NET_FIELD_RE.finditer(block)}
    int_fields = {match.group("key"): match.group("value") for match in NET_INT_FIELD_RE.finditer(block)}
    level = fields.get("CIVILIZATION_LEVEL_TYPE_NAME")
    civ = fields.get("CIVILIZATION_TYPE_NAME")
    leader = fields.get("LEADER_TYPE_NAME")
    network_name = fields.get("NETWORK_NAME", "").strip()
    player_name = fields.get("NICK_NAME", "").strip()
    slot_match = NET_PLAYER_ID_RE.search(block)
    slot = parse_int(slot_match.group("slot")) if slot_match else None
    if slot is None or slot < 0:
        return None
    if level != "CIVILIZATION_LEVEL_FULL_CIV":
        return None
    if not civ or not leader or civ == "CIVILIZATION_SPECTATOR":
        return None
    if not player_name and not network_name:
        return None
    return PlayerIdentity(
        slot=slot,
        civilization=civ,
        leader=leader,
        is_human=True,
        player_name=player_name or None,
        network_name=network_name or None,
        team=parse_int(int_fields.get("TEAM", "")),
    )


def parse_network_player_names(logs_dir: Path) -> dict[int, dict[str, str]]:
    path = logs_dir / "net_message_debug.log"
    if not path.exists():
        return {}

    players: dict[int, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            match = NET_PLAYER_INFO_RE.search(line)
            if not match:
                continue
            slot = parse_int(match.group("slot"))
            if slot is None or slot < 0:
                continue
            player_name = match.group("player_name").strip()
            network_name = match.group("network_name").strip()
            if not player_name and not network_name:
                continue
            players[slot] = {
                "player_name": player_name,
                "network_name": network_name,
                "team": next(
                    (
                        int_match.group("value")
                        for int_match in NET_INT_FIELD_RE.finditer(line)
                        if int_match.group("key") == "TEAM"
                    ),
                    "",
                ),
            }
    return players


def parse_player_stats(logs_dir: Path, players: dict[int, PlayerIdentity]) -> dict[int, list[PlayerTurnStats]]:
    path = logs_dir / "Player_Stats.csv"
    turns: dict[int, list[PlayerTurnStats]] = defaultdict(list)
    if not path.exists():
        return turns

    slot_matcher = SlotMatcher(players)
    for row in latest_session_rows(path, turn_index=0):
        if len(row) < 20:
            continue
        turn = parse_int(row[0])
        if turn is None:
            continue
        raw_player = row[1].strip()
        slot = slot_matcher.match(turn, raw_player)
        turns[turn].append(
            PlayerTurnStats(
                turn=turn,
                raw_player=raw_player,
                slot=slot,
                cities=parse_int(row[2]),
                population=parse_int(row[3]),
                techs=parse_int(row[4]),
                civics=parse_int(row[5]),
                land_units=parse_int(row[6]),
                naval_units=parse_int(row[9]),
                gold_balance=parse_float(row[12]),
                faith_balance=parse_float(row[13]),
                science_yield=parse_float(row[14]),
                culture_yield=parse_float(row[15]),
                gold_yield=parse_float(row[16]),
                faith_yield=parse_float(row[17]),
                production_yield=parse_float(row[18]),
                food_yield=parse_float(row[19]),
            )
        )
    return turns


def enrich_player_stats_2(logs_dir: Path, turns: dict[int, list[PlayerTurnStats]]) -> None:
    path = logs_dir / "Player_Stats_2.csv"
    if not path.exists():
        return

    indexes = index_stats_by_turn_and_player(turns)
    for row in latest_session_rows(path, turn_index=0):
        if len(row) < 12:
            continue
        turn = parse_int(row[0])
        if turn is None:
            continue
        target = pop_next(indexes, turn, row[1].strip())
        if target is None:
            continue
        target.tourism = parse_float(row[7])
        target.diplo_victory_points = parse_float(row[8])


def enrich_player_scores(logs_dir: Path, turns: dict[int, list[PlayerTurnStats]]) -> None:
    path = logs_dir / "Game_PlayerScores.csv"
    if not path.exists():
        return

    by_slot = {
        (stats.turn, stats.slot): stats
        for stats_list in turns.values()
        for stats in stats_list
        if stats.slot is not None
    }
    header = csv_header(path)
    if not header:
        return
    for row in latest_session_rows(path, turn_index=0):
        if len(row) < 3:
            continue
        turn = parse_int(row[0])
        slot = parse_int(row[1])
        if turn is None or slot is None:
            continue
        stats = by_slot.get((turn, slot))
        if stats is None:
            continue
        stats.score = parse_float(row[2])
        for idx, name in enumerate(header[3:], start=3):
            if idx < len(row):
                value = parse_float(row[idx])
                if value is not None:
                    stats.score_categories[name.strip()] = value


class SlotMatcher:
    def __init__(self, players: dict[int, PlayerIdentity]) -> None:
        self.major_slots = [
            slot for slot, player in sorted(players.items())
            if player.is_human and player.civilization != "CIVILIZATION_SPECTATOR"
        ]
        self.by_civ: dict[str, deque[int]] = defaultdict(deque)
        for slot in self.major_slots:
            self.by_civ[players[slot].civilization].append(slot)
        self.turn_seen: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def match(self, turn: int, raw_player: str) -> int | None:
        candidates = self.by_civ.get(raw_player)
        if not candidates:
            return None
        index = self.turn_seen[turn][raw_player]
        self.turn_seen[turn][raw_player] += 1
        if index < len(candidates):
            return list(candidates)[index]
        return candidates[-1]


def index_stats_by_turn_and_player(
    turns: dict[int, list[PlayerTurnStats]],
) -> dict[tuple[int, str], deque[PlayerTurnStats]]:
    index: dict[tuple[int, str], deque[PlayerTurnStats]] = defaultdict(deque)
    for turn, stats_list in turns.items():
        for stats in stats_list:
            index[(turn, stats.raw_player)].append(stats)
    return index


def pop_next(indexes: dict[tuple[int, str], deque[PlayerTurnStats]], turn: int, raw_player: str) -> PlayerTurnStats | None:
    queue = indexes.get((turn, raw_player))
    if not queue:
        return None
    return queue.popleft()


def latest_session_rows(path: Path, turn_index: int) -> list[list[str]]:
    rows = csv_rows(path)
    start = 0
    previous_turn: int | None = None
    for index, row in enumerate(rows):
        if len(row) <= turn_index:
            continue
        turn = parse_int(row[turn_index])
        if turn is None:
            continue
        if previous_turn is not None and turn < previous_turn:
            start = index
        previous_turn = turn
    return rows[start:]


def latest_text_session_lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        lines = handle.readlines()

    start = 0
    for index, line in enumerate(lines):
        if is_text_session_marker(line):
            start = index
    return lines[start:]


def is_text_session_marker(line: str) -> bool:
    if "Validate App Game Configuration" in line or "NetGameConfig" in line:
        return True
    return re.search(r"\]\s+Game Turn:\s+1\s*$", line) is not None


def csv_rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return list(reader)


def csv_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader, []) or []


def parse_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def parse_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None
