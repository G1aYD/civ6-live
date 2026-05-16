from __future__ import annotations

from dataclasses import replace

from .config import Civ6Paths
from .model import GameSnapshot, PlayerIdentity
from .parsers import enrich_player_scores, enrich_player_stats_2, parse_gamecore_players, parse_player_stats
from .save_reader import SaveParseError, read_latest_save_summary


def load_snapshot(paths: Civ6Paths) -> GameSnapshot:
    players = parse_gamecore_players(paths.logs_dir)
    warnings: list[str] = []
    try:
        enrich_players_from_save(paths, players)
    except (OSError, SaveParseError) as exc:
        warnings.append(f"Could not parse latest Civ VI save header: {exc}")

    turns = parse_player_stats(paths.logs_dir, players)
    enrich_player_stats_2(paths.logs_dir, turns)
    enrich_player_scores(paths.logs_dir, turns)

    if not players:
        warnings.append("No player identities found in GameCore.log or net_message_debug.log.")
    if not turns:
        warnings.append("No player turn stats found in Player_Stats.csv.")

    latest_turn = max(turns.keys(), default=0)
    return GameSnapshot(latest_turn=latest_turn, players=players, turns=dict(turns), warnings=warnings)


def enrich_players_from_save(paths: Civ6Paths, players: dict[int, PlayerIdentity]) -> None:
    summary = read_latest_save_summary(paths, include_map=False)
    if not summary:
        return
    for save_player in summary.get("major_players", []):
        slot = save_player.get("slot")
        if slot is None:
            continue
        civilization = save_player.get("civilization_token") or save_player.get("civilization") or ""
        leader = save_player.get("leader_token") or save_player.get("leader") or ""
        player_name = save_player.get("player_name")
        team = save_player.get("team")
        existing = players.get(slot)
        if existing is None:
            players[slot] = PlayerIdentity(
                slot=slot,
                civilization=civilization,
                leader=leader,
                is_human=save_player.get("type_player") == "human",
                player_name=player_name,
                network_name=player_name,
                team=team,
            )
            continue
        players[slot] = replace(
            existing,
            civilization=existing.civilization or civilization,
            leader=existing.leader or leader,
            player_name=existing.player_name or player_name,
            network_name=existing.network_name or player_name,
            team=existing.team if existing.team is not None else team,
        )
