from __future__ import annotations

import time
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Civ6Paths
from .model import clean_game_name


CHUNK_SIZE = 65536
END_COMPRESSED = b"\x00\x00\xff\xff\x02\x00\x00\x00"
TERRITORY_BUILDER = b"TerritoryBuilder"
MAP_BEGIN = bytes(
    [
        0x0A,
        0,
        0,
        0,
        0x0B,
        0,
        0,
        0,
        0x0C,
        0,
        0,
        0,
        0x0D,
        0,
        0,
        0,
        0x0E,
        0,
        0,
        0,
        0x0F,
        0,
        0,
        0,
        0x06,
        0,
        0,
        0,
    ]
)

PLAYER_INFO_MARKERS = {
    "slot": 0x1A96522F,
    "type_player": 0xCE42B995,
    "team": 0x0D8AB454,
    "color": 0xCFAF60EF,
    "leader_hash": 0x4C7FBA58,
    "civilization_hash": 0xC7D7515B,
    "type_civ": 0x830FFC46,
    "civilization": 0x9D5E5C2F,
    "leader": 0xE8CD5E5F,
    "player_name": 0xDAB96BFD,
    "player_name_steam": 0x8E72249A,
    "loc_civ_name": 0xB3EA1140,
}

GAME_SEED_PACKET = 0x7DD11279
GAME_SEED_MARKER = 0x96C5C77C

TYPE_PLAYER_NAMES = {
    0: "unknown",
    1: "ai",
    2: "reserved",
    3: "human",
    4: "reserved",
    5: "none",
}

TYPE_CIV_FULL = 0x253718B0
TYPE_CIV_CITY_STATE = 0xC2577075
TYPE_CIV_FREE_CITIES = 0x04E6B0A0
TYPE_CIV_TRIBE = 0xD959EC24

PRODUCTION_TYPES = {
    0: "Unit",
    1: "Building",
    2: "District",
    3: "Wonder",
    4: "Project",
}


class SaveParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SavePlayerInfo:
    slot: int
    type_player: int | None
    type_player_name: str | None
    type_civ: int | None
    team: int | None
    civilization: str | None
    leader: str | None
    player_name: str | None
    steam_id: str | None
    loc_civ_name: str | None

    @property
    def short_civ(self) -> str | None:
        if not self.civilization:
            return None
        return clean_game_name(self.civilization, "CIVILIZATION_")

    @property
    def short_leader(self) -> str | None:
        if not self.leader:
            return None
        return clean_game_name(self.leader, "LEADER_")

    @property
    def is_major(self) -> bool:
        return (
            self.type_civ == TYPE_CIV_FULL
            and self.type_player in {1, 3}
            and self.civilization not in {"CIVILIZATION_SPECTATOR", "CIVILIZATION_FREE_CITIES"}
        )

    @property
    def is_active_city_state(self) -> bool:
        return self.type_civ == TYPE_CIV_CITY_STATE and self.type_player == 1


@dataclass
class Packet:
    marker: int
    type: int
    length: int
    value: Any = None
    children: dict[int, "Packet"] | None = None

    def get_int(self, marker: int) -> int | None:
        child = self.children.get(marker) if self.children else None
        if child is None or not isinstance(child.value, int):
            return None
        return child.value

    def get_string(self, marker: int) -> str | None:
        child = self.children.get(marker) if self.children else None
        if child is None or not isinstance(child.value, str):
            return None
        return child.value


class PacketParser:
    def __init__(self, data: bytes, *, decompress_packets: bool) -> None:
        self.data = data
        self.offset = 0
        self.decompress_packets = decompress_packets
        self.compressed_data: list[bytes] = []

    def read_uint(self, size: int = 4) -> int:
        self.require(size)
        value = int.from_bytes(self.data[self.offset : self.offset + size], "little", signed=False)
        self.offset += size
        return value

    def require(self, size: int) -> None:
        if self.offset + size > len(self.data):
            raise SaveParseError("Unexpected end of Civ VI save while parsing packet data.")

    def parse_entry(self, array_index: int | None = None) -> Packet:
        marker = array_index if array_index is not None else self.read_uint()
        packet_type = self.read_uint()
        self.require(8)
        length = int.from_bytes(self.data[self.offset : self.offset + 3], "little", signed=False)
        self.offset += 4
        _info = self.read_uint()

        value: Any = None
        children: dict[int, Packet] | None = None
        if packet_type == 1:
            value = self.data[self.offset]
            self.offset += 4
        elif packet_type in {2, 3}:
            value = self.read_uint()
        elif packet_type in {0x0D, 0x15}:
            self.offset += 8
        elif packet_type in {4, 5}:
            if length:
                value = c_string(self.data[self.offset : self.offset + length].decode("utf-8", "replace"))
                self.offset += length
            else:
                value = ""
                self.offset += 4
        elif packet_type == 6:
            raw = self.data[self.offset : self.offset + length * 2]
            value = c_string(raw.decode("utf-16le", "replace"))
            self.offset += length * 2
        elif packet_type == 10:
            count = self.read_uint()
            children = {}
            for _ in range(count):
                child = self.parse_entry()
                children[child.marker] = child
            value = count
        elif packet_type == 11:
            count = self.read_uint()
            children = {}
            for index in range(count):
                child = self.parse_entry(index)
                children[child.marker] = child
            value = count
        elif packet_type == 20:
            value = self.read_uint(8)
        elif packet_type == 24:
            self.offset += 12
            if self.decompress_packets:
                payload, self.offset = read_compressed(self.data, self.offset, length)
                self.compressed_data.append(payload)
                value = {"compressed_index": len(self.compressed_data) - 1, "size": len(payload)}
            else:
                self.offset += length - 12
                value = {"compressed_index": None, "size": None}
        else:
            raise SaveParseError(
                f"Unsupported Civ VI save packet type {packet_type} at offset {self.offset}."
            )

        return Packet(marker=marker, type=packet_type, length=length, value=value, children=children)

    def parse_packet_array(self) -> dict[int, Packet]:
        count = self.read_uint()
        packets: dict[int, Packet] = {}
        for _ in range(count):
            packet = self.parse_entry()
            packets[packet.marker] = packet
        return packets

    def parse_all(self) -> tuple[list[dict[int, Packet]], bytes | None]:
        if not self.data.startswith(b"CIV6"):
            raise SaveParseError("Not a Civ VI save file.")
        self.offset = 8
        arrays = [self.parse_packet_array()]

        self.offset += 8
        arrays.append(self.parse_packet_array())

        self.offset += 4
        arrays.append(self.parse_packet_array())

        self.offset += 4
        arrays.append(self.parse_packet_array())

        extra_count = self.read_uint()
        for _ in range(extra_count):
            self.offset += 4
            arrays.append(self.parse_packet_array())

        arrays.append(self.parse_packet_array())

        final_payload = None
        if self.decompress_packets:
            self.offset += 4
            end = self.data.find(END_COMPRESSED, self.offset)
            if end == -1:
                raise SaveParseError("Could not find final compressed Civ VI save block.")
            final_payload, self.offset = read_compressed(
                self.data,
                self.offset,
                end - self.offset + 4 + 12,
            )
            self.compressed_data.append(final_payload)
        return arrays, final_payload


def latest_save_path(autosaves: Path) -> Path | None:
    saves = list(autosaves.glob("*.Civ6Save"))
    if not saves:
        return None
    return max(saves, key=lambda path: path.stat().st_mtime_ns)


def read_latest_save_summary(paths: Civ6Paths, *, include_map: bool = True) -> dict[str, Any] | None:
    save = latest_save_path(paths.autosaves)
    if save is None:
        return None
    stat = save.stat()
    return _read_save_summary_cached(str(save), stat.st_mtime_ns, stat.st_size, include_map)


@lru_cache(maxsize=8)
def _read_save_summary_cached(path: str, mtime_ns: int, size: int, include_map: bool) -> dict[str, Any]:
    started = time.perf_counter()
    save_path = Path(path)
    data = save_path.read_bytes()
    parser = PacketParser(data, decompress_packets=include_map)
    arrays, final_payload = parser.parse_all()
    players = parse_players_info(arrays[2] if len(arrays) > 2 else {})
    seed = parse_game_seed(arrays[2] if len(arrays) > 2 else {})

    turn = None
    map_summary = None
    if final_payload is not None:
        turn = parse_turn(final_payload)
        if include_map:
            try:
                map_summary = parse_map_summary(final_payload)
            except SaveParseError as exc:
                map_summary = {"error": str(exc)}
            try:
                player_details = parse_player_details(final_payload)
            except SaveParseError as exc:
                player_details = {"error": str(exc)}
        else:
            player_details = None
    else:
        player_details = None

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    major_players = [player for player in players if player.is_major]
    active_city_states = [player for player in players if player.is_active_city_state]
    return {
        "source": "CivVIReplay-compatible save header parser",
        "path": str(save_path),
        "file": save_path.name,
        "mtime_ns": mtime_ns,
        "size_bytes": size,
        "parse_ms": elapsed_ms,
        "game_seed": seed,
        "turn": turn,
        "major_players": [player_to_dict(player) for player in sorted(major_players, key=lambda item: item.slot)],
        "active_city_states": [
            player_to_dict(player) for player in sorted(active_city_states, key=lambda item: item.slot)
        ],
        "teams": save_teams_context(major_players),
        "map": map_summary,
        "player_details": player_details,
    }


def parse_game_seed(player_packets: dict[int, Packet]) -> int | None:
    packet = player_packets.get(GAME_SEED_PACKET)
    if packet is None:
        return None
    return packet.get_int(GAME_SEED_MARKER)


def parse_players_info(player_packets: dict[int, Packet]) -> list[SavePlayerInfo]:
    players = []
    for packet in player_packets.values():
        slot = packet.get_int(PLAYER_INFO_MARKERS["slot"])
        if slot is None or slot == 0xFFFFFFFF:
            continue
        type_player = packet.get_int(PLAYER_INFO_MARKERS["type_player"])
        steam_text = packet.get_string(PLAYER_INFO_MARKERS["player_name_steam"])
        steam_id = None
        if steam_text and "@" in steam_text:
            steam_id = steam_text.split("@", 1)[1]
        players.append(
            SavePlayerInfo(
                slot=slot,
                type_player=type_player,
                type_player_name=TYPE_PLAYER_NAMES.get(type_player) if type_player is not None else None,
                type_civ=packet.get_int(PLAYER_INFO_MARKERS["type_civ"]),
                team=packet.get_int(PLAYER_INFO_MARKERS["team"]),
                civilization=packet.get_string(PLAYER_INFO_MARKERS["civilization"]),
                leader=packet.get_string(PLAYER_INFO_MARKERS["leader"]),
                player_name=packet.get_string(PLAYER_INFO_MARKERS["player_name"]),
                steam_id=steam_id,
                loc_civ_name=packet.get_string(PLAYER_INFO_MARKERS["loc_civ_name"]),
            )
        )
    return players


def player_to_dict(player: SavePlayerInfo) -> dict[str, Any]:
    return {
        "slot": player.slot,
        "type_player": player.type_player_name,
        "type_civ": hex(player.type_civ) if player.type_civ is not None else None,
        "team": player.team,
        "civilization": player.short_civ,
        "civilization_token": player.civilization,
        "leader": player.short_leader,
        "leader_token": player.leader,
        "player_name": player.player_name,
        "steam_id": player.steam_id,
        "loc_civ_name": player.loc_civ_name,
    }


def save_teams_context(players: list[SavePlayerInfo]) -> dict[str, Any]:
    teams: dict[int, list[SavePlayerInfo]] = defaultdict(list)
    for player in players:
        if player.team is not None:
            teams[player.team].append(player)
    sizes = [len(members) for _, members in sorted(teams.items())]
    return {
        "detected_format": "v".join(str(size) for size in sizes) if sizes else None,
        "teams": [
            {
                "team": team,
                "size": len(members),
                "players": [player_to_dict(player) for player in sorted(members, key=lambda item: item.slot)],
            }
            for team, members in sorted(teams.items())
        ],
    }


def parse_turn(data: bytes) -> int | None:
    if len(data) < 381:
        return None
    return int.from_bytes(data[377:381], "little", signed=False)


class SavePayloadReader:
    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    def require(self, size: int) -> None:
        if self.offset + size > len(self.data):
            raise SaveParseError("Unexpected end of Civ VI save payload.")

    def uint(self, size: int = 4) -> int:
        self.require(size)
        value = int.from_bytes(self.data[self.offset : self.offset + size], "little", signed=False)
        self.offset += size
        return value

    def peek(self, size: int = 4) -> int:
        self.require(size)
        return int.from_bytes(self.data[self.offset : self.offset + size], "little", signed=False)

    def skip(self, size: int) -> None:
        self.require(size)
        self.offset += size

    def string(self, size_count: int = 4) -> str:
        count = self.uint(size_count)
        self.require(count)
        value = c_string(self.data[self.offset : self.offset + count].decode("utf-8", "replace")) or ""
        self.offset += count
        return value

    def map(self, size_value: int = 4) -> dict[int, int]:
        count = self.uint()
        if count > 200000:
            raise SaveParseError(f"Unreasonable save map count {count} at offset {self.offset}.")
        values = {}
        for _ in range(count):
            key = self.uint()
            values[key] = self.uint(size_value)
        return values

    def array(self, size: int = 4, sep: int = 1, size_count: int = 4) -> list[int]:
        count = self.uint(size_count)
        if count > 200000:
            raise SaveParseError(f"Unreasonable save array count {count} at offset {self.offset}.")
        values = []
        for _ in range(count):
            value = self.uint(size)
            values.append(value)
            if value != 0:
                self.skip(sep)
        return values


def parse_player_details(data: bytes) -> dict[str, Any]:
    offset = data.find(TERRITORY_BUILDER)
    if offset == -1:
        raise SaveParseError("Could not find CivVIReplay TerritoryBuilder marker in save payload.")
    reader = SavePayloadReader(data, offset + len(TERRITORY_BUILDER))
    reader.skip(16)

    religion_count = reader.uint()
    for _ in range(religion_count):
        reader.skip(4)
        reader.skip(4)
        reader.skip(4)
        reader.skip(12)
        reader.string()
        reader.skip(reader.uint() * 4)
        reader.skip(reader.uint() * 4)
        reader.skip(reader.uint() * 4)
        reader.skip(11)

    if reader.peek() != 1:
        raise SaveParseError(f"Unexpected player details marker {reader.peek():#x}.")
    reader.skip(4)
    player_count = reader.uint()
    players = []
    errors = []

    for record_index in range(player_count):
        record_start = reader.offset
        try:
            details = parse_player_record_to_districts(reader)
            players.append(details)
            next_slot = details["slot"] + 1
            if next_slot < player_count:
                next_offset = find_player_record_boundary(data, reader.offset, next_slot)
                if next_offset is None:
                    break
                reader.offset = next_offset
        except SaveParseError as exc:
            errors.append(
                {
                    "record_index": record_index,
                    "offset": record_start,
                    "error": str(exc),
                }
            )
            next_offset = find_any_player_record_boundary(data, record_start + 4, player_count)
            if next_offset is None or next_offset <= record_start:
                break
            reader.offset = next_offset

    asset_lookup = build_asset_lookup()
    for player in players:
        city_by_id = {city["id"]: city for city in player["cities"]}
        for city in player["cities"]:
            for current in city.get("current_production", []):
                asset = asset_lookup.get(current["hash"])
                current["asset"] = asset
                current["display"] = asset.get("display") if asset else hex(current["hash"])
        for district in player["districts"]:
            asset = asset_lookup.get(district["hash"])
            city = city_by_id.get(district["city_id"])
            district["asset"] = asset
            district["display"] = asset.get("display") if asset else hex(district["hash"])
            district["city"] = city["display_name"] if city else None
            district["city_raw_name"] = city["raw_name"] if city else None

    return {
        "source": "CivVIReplay ParsePlayers city/district subset",
        "player_count": player_count,
        "players": players,
        "errors": errors[:8],
    }


def parse_player_record_to_districts(reader: SavePayloadReader) -> dict[str, Any]:
    slot = reader.uint()
    if reader.peek() != 47:
        raise SaveParseError(f"Unexpected player record marker {reader.peek():#x} for slot {slot}.")
    reader.skip(8)
    reader.skip(4)
    reader.skip(4)
    reader.skip(56)
    reader.skip(10)
    reader.skip(4)
    reader.skip(7 * 4)

    unit_count = reader.uint()
    for _ in range(unit_count):
        reader.skip(12)
        reader.skip(4)
        reader.skip(4)
        reader.skip(8)
        reader.skip(12)
        count = reader.uint()
        reader.skip(8 + count * 20)
        count = reader.uint()
        reader.skip(count * 4)

    reader.skip(4)
    reader.skip(reader.uint() * 4)
    reader.skip(12)
    reader.map()
    reader.skip(4)
    reader.map()
    reader.skip(4)
    reader.skip(4)
    reader.skip(20)
    reader.map()
    reader.map()
    reader.map()
    reader.array(size=4, sep=0)
    reader.map()

    reader.skip(33)
    if reader.peek() != 0x10:
        raise SaveParseError(f"Could not find city section for slot {slot} at offset {reader.offset}.")
    reader.skip(8)
    city_count = reader.uint()
    cities = [parse_city_record(reader) for _ in range(city_count)]

    skip_after_cities_to_districts(reader, slot)
    district_count = reader.uint()
    districts = [parse_district_record(reader) for _ in range(district_count)]

    return {
        "slot": slot,
        "city_count": city_count,
        "cities": cities,
        "district_count": district_count,
        "districts": districts,
    }


def parse_city_record(reader: SavePayloadReader) -> dict[str, Any]:
    city_id = reader.uint()
    if reader.peek() != 0x33:
        raise SaveParseError(f"Unexpected city marker {reader.peek():#x} at offset {reader.offset}.")
    reader.skip(8)
    x = reader.uint()
    y = reader.uint()
    reader.skip(12)
    reader.skip(4)
    reader.skip(4)
    population = reader.uint()
    reader.skip(2)
    reader.skip(4)
    reader.skip(8)
    reader.skip(4)
    reader.skip(64 * 4)
    reader.skip(1)
    reader.skip(18)

    for _ in range(13):
        if reader.peek() != 6:
            raise SaveParseError(f"Unexpected city map marker {reader.peek():#x} at offset {reader.offset}.")
        reader.map()
    reader.skip(reader.uint() * 20)
    reader.skip(reader.uint() * 16)
    reader.skip(reader.uint() * 8)
    reader.skip(reader.uint() * 12)
    reader.skip(8)
    count = reader.uint()
    for _ in range(count):
        reader.skip(4 + 4 + 8 + 4 + 6 + 4 + 8)
        reader.string()

    reader.skip(reader.uint() * 16)
    reader.skip(17)
    if reader.peek() != 6:
        raise SaveParseError(f"Could not find city-name map at offset {reader.offset}.")
    reader.map()
    reader.skip(13)
    raw_name = reader.string()
    reader.skip(8)
    reader.skip(4)
    reader.skip(reader.uint() * 4)
    reader.skip(8)

    reader.array(size=4, sep=0)
    reader.array(size=4, sep=0)
    reader.array()
    reader.array()
    reader.skip(9)
    reader.skip(36)
    reader.skip(reader.uint() * 22)
    reader.skip(reader.uint() * 20)
    reader.skip(4)
    reader.skip(25)

    for _ in range(4):
        reader.map()
    reader.skip(4)
    reader.map()
    reader.map()
    reader.skip(5)
    reader.array(size=4, sep=0)
    reader.skip(33)
    reader.skip(reader.uint() * 8)

    reader.skip(4)
    religion = reader.uint()
    reader.skip(reader.uint() * 17)
    reader.skip(8)
    reader.skip(18)
    reader.skip(4)
    reader.skip(16)
    reader.map()
    reader.map()
    reader.skip(12)
    reader.map()
    reader.map()

    reader.skip(4)
    current_production = []
    for _ in range(reader.uint()):
        if reader.peek() != 0x2C0F4A46:
            raise SaveParseError(f"Unexpected production marker {reader.peek():#x} at offset {reader.offset}.")
        reader.skip(12)
        production_type = reader.uint()
        reader.skip(4)
        production_hash = reader.uint()
        if production_type == 0:
            reader.skip(4)
        reader.skip(12)
        current_production.append(
            {
                "production_type": PRODUCTION_TYPES.get(production_type, str(production_type)),
                "hash": production_hash,
            }
        )

    reader.skip(40)
    reader.map()
    reader.map()
    reader.map()
    reader.map(size_value=2)
    reader.map()
    reader.map()
    reader.map()
    reader.map(size_value=2)
    reader.map()
    reader.map()
    reader.map()
    reader.map()
    reader.array()
    reader.skip(8)
    reader.skip(reader.uint() * 12)
    reader.map()
    reader.map()
    reader.skip(9)
    reader.skip(reader.uint() * 12)
    reader.skip(4)
    built = reader.map(size_value=2)
    reader.array()
    reader.map(size_value=2)
    reader.map(size_value=2)

    for size in (12, 12, 12, 8):
        reader.skip(reader.uint() * size)
    count = reader.uint()
    for _ in range(count):
        reader.skip(4)
        reader.skip(reader.uint() * 12)
    reader.skip(reader.uint() * 12)
    reader.skip(reader.uint() * 16)
    reader.skip(reader.uint() * 4)

    reader.skip(4)
    reader.skip(37)
    reader.skip(reader.uint())
    reader.skip(4)
    for _ in range(14):
        reader.map()
    reader.skip(4)
    reader.skip(1)
    reader.skip(4)
    reader.skip(3)
    reader.skip(reader.uint() * 13)
    reader.skip(12)
    reader.skip(49)
    reader.skip(12)
    reader.skip(4)
    reader.skip(reader.uint() * 24)
    reader.skip(reader.uint() * 20)
    reader.skip(reader.uint() * 16)
    reader.skip(reader.uint() * 32)
    reader.skip(4)

    plot_property_count = reader.uint(3)
    reader.skip(1)
    for _ in range(plot_property_count):
        reader.skip(8)
        text_len = reader.uint(2)
        reader.skip(text_len)
        reader.skip(12)
        nested_count = reader.uint(3)
        reader.skip(1)
        for _ in range(nested_count):
            reader.skip(reader.uint() * 22)
    reader.skip(8)
    reader.skip(reader.uint() * 4)
    reader.array(size=4, sep=0)
    reader.map()

    return {
        "id": city_id,
        "x": x,
        "y": y,
        "population": population,
        "raw_name": raw_name,
        "display_name": clean_save_city_name(raw_name),
        "religion_hash": religion,
        "current_production": current_production,
        "built_hash_count": len(built),
    }


def skip_after_cities_to_districts(reader: SavePayloadReader, slot: int) -> None:
    reader.skip(4)
    reader.skip(reader.uint() * 4)
    reader.skip(8)
    reader.skip(5)
    reader.skip(4)
    reader.skip(reader.uint() * 4)
    reader.skip(4)
    reader.skip(8)
    reader.skip(reader.uint() * 4)
    reader.skip(4)
    reader.skip(reader.uint() * 4)
    reader.skip(4)
    reader.skip(reader.uint() * 8)
    if reader.peek() != 0x32:
        raise SaveParseError(f"Unexpected post-city marker {reader.peek():#x} for slot {slot}.")
    reader.skip(4)

    reader.skip(3137)
    reader.map()
    reader.map()
    reader.skip(53)
    reader.skip(reader.uint() * 264)
    reader.skip(reader.uint() * 72)

    tooltip_count = reader.uint()
    if tooltip_count != 64:
        raise SaveParseError(f"Unexpected tooltip count {tooltip_count} for slot {slot}.")
    for _ in range(tooltip_count):
        count = reader.uint()
        for _ in range(count):
            reader.skip(4)
            reader.skip(4)
            reader.skip(33)
            reader.string()
            reader.skip(4)

    reader.skip(12)
    reader.skip(64 * 16)
    reader.skip(64 * 4)
    reader.skip(64 * 4)
    reader.skip(768)
    reader.skip(reader.uint() * 8)
    for _ in range(64):
        if reader.peek() != 0x32:
            raise SaveParseError(f"Unexpected fixed marker {reader.peek():#x} for slot {slot}.")
        reader.skip(13)

    reader.skip(reader.uint() * 21)
    reader.skip(reader.uint() * 16)
    reader.skip(reader.uint() * 4)
    reader.skip(reader.uint() * 16)
    reader.skip(reader.uint() * 12)
    reader.skip(86)
    reader.skip(reader.uint() * 4)
    reader.skip(reader.uint() * 4)
    reader.skip(reader.uint() * 16)

    count = reader.uint()
    for _ in range(count):
        reader.skip(8)
        reader.skip(reader.uint() * 8)

    reader.skip(reader.uint() * 14)
    count = reader.uint()
    for _ in range(count):
        reader.skip(8)
        reader.skip(reader.uint() * 20)

    count = reader.uint()
    for _ in range(count):
        reader.skip(8)
        count2 = reader.uint()
        for _ in range(count2):
            reader.skip(4)
            reader.skip(reader.uint() * 4)

    count = reader.uint()
    for _ in range(count):
        reader.skip(8)
        reader.skip(reader.uint() * 20)

    reader.array()
    reader.array(size=4, sep=0)
    reader.map()
    reader.skip(16)


def parse_district_record(reader: SavePayloadReader) -> dict[str, Any]:
    global_id = reader.uint()
    if reader.peek() != 0x0F:
        raise SaveParseError(f"Unexpected district marker {reader.peek():#x} at offset {reader.offset}.")
    reader.skip(4)
    district_id = reader.uint(2)
    reader.skip(2)
    x = reader.uint()
    y = reader.uint()
    city_id = reader.uint(2)
    reader.skip(2)
    district_hash = reader.uint()
    damage = reader.uint()
    wall_damage = reader.uint()
    reader.skip(4)
    wall = reader.uint()
    cost = reader.uint()
    built = reader.uint()
    reader.skip(1)
    for _ in range(3):
        reader.map()
    reader.skip(12)
    reader.map()
    reader.map()
    reader.skip(4)
    reader.skip(reader.uint() * 16)
    reader.map()
    reader.skip(17)
    pillaged = reader.uint(1)
    return {
        "global_id": global_id,
        "id": district_id,
        "x": x,
        "y": y,
        "city_id": city_id,
        "hash": district_hash,
        "damage": damage,
        "wall_damage": wall_damage,
        "wall": wall,
        "cost": cost,
        "built": built,
        "pillaged": bool(pillaged),
    }


def find_player_record_boundary(data: bytes, offset: int, slot: int) -> int | None:
    pattern = slot.to_bytes(4, "little") + (47).to_bytes(4, "little") + slot.to_bytes(4, "little")
    found = data.find(pattern, offset)
    return found if found != -1 else None


def find_any_player_record_boundary(data: bytes, offset: int, player_count: int) -> int | None:
    candidates = []
    for slot in range(player_count):
        found = find_player_record_boundary(data, offset, slot)
        if found is not None:
            candidates.append(found)
    return min(candidates) if candidates else None


def clean_save_city_name(value: str) -> str:
    if value.startswith("LOC_CITY_NAME_"):
        return clean_game_name(value[len("LOC_CITY_NAME_") :])
    if value.startswith("LOC_CITY_"):
        return clean_game_name(value[len("LOC_CITY_") :])
    return clean_game_name(value)


def parse_map_summary(data: bytes) -> dict[str, Any]:
    offset = data.find(MAP_BEGIN)
    if offset == -1:
        raise SaveParseError("Could not find CivVIReplay MAP_BEGIN marker in save payload.")
    offset += len(MAP_BEGIN)
    tile_count = read_uint(data, offset)
    offset += 4

    asset_lookup = build_asset_lookup()
    owners: Counter[int] = Counter()
    city_tiles: Counter[int] = Counter()
    district_tiles: Counter[int] = Counter()
    terrain_counts: Counter[int] = Counter()
    feature_counts: Counter[int] = Counter()
    resource_counts: Counter[int] = Counter()
    improvement_counts: Counter[int] = Counter()
    wonder_tiles: list[dict[str, Any]] = []
    owner_locations: dict[int, list[int]] = defaultdict(list)

    for tile_index in range(tile_count):
        offset += 8
        offset += 4
        terrain = read_uint(data, offset)
        feature = read_uint(data, offset + 4)
        offset += 8
        offset += 2
        offset += 4
        offset += 1
        resource = read_uint(data, offset)
        offset += 4
        offset += 2
        improvement = read_uint(data, offset)
        offset += 4
        offset += 1
        offset += 2
        offset += 2
        offset += 3
        offset += 1
        offset += 1
        offset += 1
        offset += 1
        found = read_uint(data, offset, 1)
        offset += 1
        offset += 1
        overlay = read_uint(data, offset)
        offset += 4

        terrain_counts[terrain] += 1
        if feature != 0xFFFFFFFF:
            feature_counts[feature] += 1
        if resource != 0xFFFFFFFF:
            resource_counts[resource] += 1
        if improvement != 0xFFFFFFFF:
            improvement_counts[improvement] += 1

        if overlay:
            count = read_uint(data, offset)
            offset += 4
            for _ in range(count):
                offset += 11
                value = read_uint(data, offset)
                offset += 4
                offset += 1
                count2 = read_uint(data, offset)
                offset += 4
                if value:
                    offset += count2 * 20

        if found & 0x40:
            city_id = read_uint(data, offset, 2)
            offset += 2
            offset += 2
            offset += 4
            district_id = read_uint(data, offset, 2)
            offset += 2
            offset += 2
            owner = read_uint(data, offset, 1)
            offset += 1
            wonder = read_uint(data, offset)
            offset += 4

            owners[owner] += 1
            owner_locations[owner].append(tile_index)
            if city_id != 0xFFFF:
                city_tiles[owner] += 1
            if district_id != 0xFFFF:
                district_tiles[owner] += 1
            if wonder != 0xFFFFFFFF:
                wonder_tiles.append(
                    {
                        "tile": tile_index,
                        "owner": owner,
                        "hash": hex(wonder),
                        "asset": asset_lookup.get(wonder),
                    }
                )

    offset += 4
    width = read_uint(data, offset)
    height = tile_count // width if width else None
    for item in wonder_tiles:
        if width:
            item["x"] = item["tile"] % width
            item["y"] = item["tile"] // width
    owned_tiles = []
    for owner, count in owners.most_common():
        locations = owner_locations.get(owner, [])
        xs = [tile % width for tile in locations] if width else []
        ys = [tile // width for tile in locations] if width else []
        owned_tiles.append(
            {
                "slot": owner,
                "owned_tiles": count,
                "city_or_owned_plot_tiles": city_tiles.get(owner, 0),
                "district_tiles": district_tiles.get(owner, 0),
                "approx_bounds": {
                    "min_x": min(xs) if xs else None,
                    "max_x": max(xs) if xs else None,
                    "min_y": min(ys) if ys else None,
                    "max_y": max(ys) if ys else None,
                },
            }
        )

    return {
        "source": "CivVIReplay ParseMap port",
        "width": width,
        "height": height,
        "tile_count": tile_count,
        "owned_tile_summary": owned_tiles,
        "wonders_on_map": wonder_tiles,
        "top_terrain": counter_context(terrain_counts, asset_lookup, limit=12),
        "top_features": counter_context(feature_counts, asset_lookup, limit=12),
        "top_resources": counter_context(resource_counts, asset_lookup, limit=12),
        "top_improvements": counter_context(improvement_counts, asset_lookup, limit=12),
    }


def counter_context(counter: Counter[int], asset_lookup: dict[int, dict[str, str]], limit: int) -> list[dict[str, Any]]:
    return [
        {
            "hash": hex(key),
            "count": count,
            "asset": asset_lookup.get(key),
        }
        for key, count in counter.most_common(limit)
    ]


@lru_cache(maxsize=1)
def build_asset_lookup() -> dict[int, dict[str, str]]:
    root = Path("run_logs") / "CivVIReplay_tmp" / "assets" / "image"
    lookup: dict[int, dict[str, str]] = {}
    if not root.exists():
        return lookup
    for image in root.glob("*/*.png"):
        folder = image.parent.name
        name = image.stem
        asset_hash = civ6_asset_hash(folder, name)
        lookup[asset_hash] = {
            "folder": folder,
            "name": name,
            "display": clean_game_name(name),
        }
    return lookup


def civ6_asset_hash(folder: str, name: str) -> int:
    text = f"{folder}_{name}".upper().encode("utf-8")
    return (~zlib.crc32(text)) & 0xFFFFFFFF


def read_compressed(data: bytes, offset: int, packet_length: int) -> tuple[bytes, int]:
    remaining = packet_length - 12
    if remaining < 0:
        raise SaveParseError("Invalid compressed packet length.")
    decompressor = zlib.decompressobj()
    output = bytearray()
    while remaining > 0:
        take = min(remaining, CHUNK_SIZE)
        output.extend(decompressor.decompress(data[offset : offset + take]))
        offset += take
        remaining -= take
        if remaining > 0:
            offset += 4
            remaining -= 4
    output.extend(decompressor.flush())
    return bytes(output), offset


def read_uint(data: bytes, offset: int, size: int = 4) -> int:
    if offset + size > len(data):
        raise SaveParseError("Unexpected end of Civ VI save payload.")
    return int.from_bytes(data[offset : offset + size], "little", signed=False)


def c_string(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split("\x00", 1)[0]
