from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Civ6Paths
from .events import LiveEvent, format_events, human_live_events
from .model import GameSnapshot, PlayerTurnStats
from .query import describe_player, format_number, ranked_metric
from .state import load_snapshot
from .watch_intel import baseline_intel, change_intel, changed_file_names

IMPORTANT_LOG_FILES = [
    "GameCore.log",
    "net_message_debug.log",
    "AStar_APP.log",
    "UnitOperations.log",
    "Player_Stats.csv",
    "Player_Stats_2.csv",
    "Game_PlayerScores.csv",
    "City_BuildQueue.csv",
    "DiplomacySummary.csv",
    "DiplomacyDeals.log",
    "Game_Boosts.csv",
    "Game_GreatPeople.csv",
    "Game_Influence.csv",
    "Game_RandomEvents.csv",
    "Game_Religion.csv",
    "World_Congress.csv",
    "Game_Emergencies.csv",
    "Governors.csv",
    "DynamicEmpires.csv",
]
EVENT_LOG_FILES = {"net_message_debug.log", "AStar_APP.log"}


@dataclass(frozen=True)
class FileStamp:
    modified_ns: int
    size: int


@dataclass(frozen=True)
class FileChanges:
    added: list[Path]
    modified: list[Path]
    deleted: list[Path]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def run_watch(
    paths: Civ6Paths,
    interval: float = 1.0,
    max_files: int = 12,
    once: bool = False,
    duration: float | None = None,
    all_files: bool = False,
    event_limit: int = 8,
    event_mode: str = "important",
) -> None:
    roots = [paths.autosaves, paths.civ6_user_data]
    previous = scan_files(roots) if all_files else scan_important_files(paths)
    print_watch_header(paths, previous, all_files)
    print()
    snapshot = load_snapshot(paths)
    print_snapshot(snapshot)
    print_intel(baseline_intel(paths, snapshot))
    important_only = event_mode == "important"
    seen_events = set(human_live_events(paths, limit=2000, important_only=important_only))

    if once:
        return

    started = time.monotonic()
    try:
        while True:
            if duration is not None and time.monotonic() - started >= duration:
                print(f"\nStopped watcher after {format_number(duration)} seconds.")
                return
            time.sleep(interval)
            current = scan_files(roots) if all_files else scan_important_files(paths)
            changes = diff_files(previous, current)
            previous = current
            if not changes.has_changes:
                continue

            print()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] File update detected")
            print_changes(changes, roots, max_files)
            snapshot = load_snapshot(paths)
            if event_mode != "none" and event_limit > 0 and has_event_log_change(changes):
                seen_events = print_new_events(paths, seen_events, event_limit, important_only)
            changed_names = changed_file_names(changes.added + changes.modified + changes.deleted)
            print_intel(change_intel(paths, snapshot, changed_names))
            print_snapshot(snapshot)
    except KeyboardInterrupt:
        print("\nStopped watcher.")


def print_watch_header(paths: Civ6Paths, files: dict[Path, FileStamp], all_files: bool) -> None:
    if all_files:
        roots = [paths.autosaves, paths.civ6_user_data]
        print(f"Watching {len(files)} files under:")
        for root in roots:
            print(f"- {root}")
        return

    print(f"Watching {len(files)} important Civ 6 files.")
    print(f"- autosaves: {paths.autosaves}\\*.Civ6Save")
    print(f"- logs: {paths.logs_dir}")
    for name in IMPORTANT_LOG_FILES:
        print(f"  - {name}")


def scan_important_files(paths: Civ6Paths) -> dict[Path, FileStamp]:
    files: dict[Path, FileStamp] = {}
    for path in important_paths(paths):
        stamp = file_stamp(path)
        if stamp is not None:
            files[path] = stamp
    return files


def important_paths(paths: Civ6Paths) -> list[Path]:
    exact_paths = [paths.logs_dir / name for name in IMPORTANT_LOG_FILES]
    autosaves = sorted(paths.autosaves.glob("*.Civ6Save")) if paths.autosaves.exists() else []
    return exact_paths + autosaves


def file_stamp(path: Path) -> FileStamp | None:
    try:
        if not path.is_file():
            return None
        stat = path.stat()
    except OSError:
        return None
    return FileStamp(modified_ns=stat.st_mtime_ns, size=stat.st_size)


def has_event_log_change(changes: FileChanges) -> bool:
    return any(path.name in EVENT_LOG_FILES for path in changes.added + changes.modified)


def print_new_events(paths: Civ6Paths, seen_events: set[LiveEvent], limit: int, important_only: bool) -> set[LiveEvent]:
    current_events = human_live_events(paths, limit=2000, important_only=important_only)
    new_events = [event for event in current_events if event not in seen_events]
    seen_events.update(current_events)
    if new_events:
        print("New human events:")
        print(format_events(new_events[-limit:]))
    return seen_events


def print_intel(lines: list[str]) -> None:
    if not lines:
        return
    print("Intel:")
    for line in lines:
        print(line)


def scan_files(roots: list[Path]) -> dict[Path, FileStamp]:
    files: dict[Path, FileStamp] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in iter_files(root):
            stamp = file_stamp(path)
            if stamp is not None:
                files[path] = stamp
    return files


def iter_files(root: Path):
    try:
        for path in root.rglob("*"):
            if path.is_file():
                yield path
    except OSError:
        return


def diff_files(previous: dict[Path, FileStamp], current: dict[Path, FileStamp]) -> FileChanges:
    previous_paths = set(previous)
    current_paths = set(current)
    added = sorted(current_paths - previous_paths)
    deleted = sorted(previous_paths - current_paths)
    modified = sorted(
        path
        for path in previous_paths & current_paths
        if previous[path] != current[path]
    )
    return FileChanges(added=added, modified=modified, deleted=deleted)


def print_changes(changes: FileChanges, roots: list[Path], max_files: int) -> None:
    sections = [
        ("modified", changes.modified),
        ("added", changes.added),
        ("deleted", changes.deleted),
    ]
    for label, paths in sections:
        if not paths:
            continue
        shown = paths[:max_files]
        print(f"{label}: {len(paths)}")
        for path in shown:
            print(f"  - {short_path(path, roots)}")
        if len(paths) > len(shown):
            print(f"  ... {len(paths) - len(shown)} more")


def print_snapshot(snapshot: GameSnapshot) -> None:
    print(f"Latest parsed turn: {snapshot.latest_turn}")
    for warning in snapshot.warnings:
        print(f"warning: {warning}")

    rows = [
        stats for stats in snapshot.stats_for_turn(snapshot.latest_turn)
        if stats.slot is not None
    ]
    if not rows:
        print("No player stats parsed yet.")
        return

    print("Leaders:")
    for metric in ["score", "production", "science", "culture", "tourism"]:
        ranked = ranked_metric(snapshot, metric, snapshot.latest_turn)
        if not ranked:
            continue
        stats, value = ranked[0]
        print(f"- {metric}: {describe_player(snapshot, stats)} {format_number(value)}")

    print("Players:")
    for stats in sorted(rows, key=player_sort_key):
        print(
            f"- {describe_player(snapshot, stats)} | "
            f"cities {display(stats.cities)}, pop {display(stats.population)}, "
            f"prod {display(stats.production_yield)}, sci {display(stats.science_yield)}, "
            f"cult {display(stats.culture_yield)}, tourism {display(stats.tourism)}, "
            f"score {display(stats.score)}"
        )


def player_sort_key(stats: PlayerTurnStats) -> int:
    return stats.slot if stats.slot is not None else 9999


def display(value: object) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return format_number(value)
    return str(value)


def short_path(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)
