from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import struct
import time
import zlib
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen


BILIBILI_ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init?id={room}"
BILIBILI_ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}"
BILIBILI_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
BILIBILI_DANMU_HISTORY_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={room}"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
DEFAULT_WS_URL = "wss://broadcastlv.chat.bilibili.com/sub"
WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32,
    15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19,
    29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]

HEADER = struct.Struct(">IHHII")
HEADER_LENGTH = 16
PROTO_JSON = 0
PROTO_NORMAL = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8


@dataclass(frozen=True)
class DanmakuEvent:
    uid: int
    uname: str
    text: str
    raw: dict


@dataclass(frozen=True)
class GiftEvent:
    uid: int
    uname: str
    gift_name: str
    gift_id: int | None
    num: int
    price: int
    total_coin: int
    coin_type: str
    raw: dict


@dataclass(frozen=True)
class SuperChatEvent:
    uid: int
    uname: str
    message: str
    price: int
    raw: dict


def extract_room_arg(value: str) -> str:
    match = re.search(r"live\.bilibili\.com/(\d+)", value)
    if match:
        return match.group(1)
    return value.strip()


def resolve_room_id(room: str | int, timeout: float = 10.0) -> int:
    room_arg = extract_room_arg(str(room))
    data = http_json(BILIBILI_ROOM_INIT_URL.format(room=room_arg), timeout=timeout)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili room_init failed: {data.get('message') or data}")
    room_data = data.get("data") or {}
    return int(room_data.get("room_id") or room_arg)


def get_room_info(room_id: int, timeout: float = 10.0) -> dict:
    data = http_json(BILIBILI_ROOM_INFO_URL.format(room=room_id), timeout=timeout)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili get_info failed: {data.get('message') or data}")
    info = data.get("data")
    return info if isinstance(info, dict) else {}


def get_danmaku_history(room_id: int, timeout: float = 10.0) -> list[DanmakuEvent]:
    data = http_json(BILIBILI_DANMU_HISTORY_URL.format(room=room_id), timeout=timeout)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili gethistory failed: {data.get('message') or data}")
    history = data.get("data") if isinstance(data.get("data"), dict) else {}
    events: list[DanmakuEvent] = []
    for bucket in ("admin", "room"):
        rows = history.get(bucket) if isinstance(history.get(bucket), list) else []
        for row in rows:
            event = parse_history_danmaku(row)
            if event is not None:
                events.append(event)
    return events


def get_danmaku_info(
    room_id: int,
    timeout: float = 10.0,
    *,
    force_default_ws: bool = False,
    use_cookie: bool = True,
) -> tuple[str, str]:
    params = sign_wbi_params({"id": room_id, "type": 0}, timeout=timeout)
    data = http_json(f"{BILIBILI_DANMU_INFO_URL}?{urlencode(params)}", timeout=timeout, use_cookie=use_cookie)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili getDanmuInfo failed: {data.get('message') or data}")
    info = data.get("data") or {}
    token = str(info.get("token") or "")
    host_list = info.get("host_list") or []
    if force_default_ws or not host_list:
        return DEFAULT_WS_URL, token
    first = host_list[0]
    host = first.get("host") or "broadcastlv.chat.bilibili.com"
    port = first.get("wss_port") or 443
    return f"wss://{host}:{port}/sub", token


def http_json(url: str, timeout: float = 10.0, *, use_cookie: bool = True) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 civ6interaction/0.1",
        "Referer": "https://live.bilibili.com/",
    }
    cookie = os.environ.get("BILIBILI_COOKIE") if use_cookie else None
    if cookie:
        headers["Cookie"] = cookie
    request = Request(
        url,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"Could not reach Bilibili API: {exc.reason}") from exc


async def iter_live_commands(
    room: str | int,
    *,
    timeout: float = 10.0,
    debug: bool = False,
    force_default_ws: bool = False,
) -> AsyncIterator[dict]:
    try:
        import websockets
        from websockets.exceptions import WebSocketException
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install with `python -m pip install websockets`.") from exc

    room_id = resolve_room_id(room, timeout=timeout)
    if debug:
        print(f"resolved Bilibili room_id: {room_id}")
    reconnect_delay = 2.0
    recoverable_errors = (OSError, ConnectionError, RuntimeError, WebSocketException)
    anonymous_auth = websocket_anonymous_auth()
    while True:
        try:
            ws_url, token = get_danmaku_info(
                room_id,
                timeout=timeout,
                force_default_ws=force_default_ws,
                use_cookie=not anonymous_auth,
            )
            if debug:
                print(f"danmaku websocket: {ws_url}")
                print(f"danmaku websocket auth: {'anonymous' if anonymous_auth else 'logged-in'}")
            async with websockets.connect(
                ws_url,
                origin="https://live.bilibili.com",
                additional_headers=websocket_headers(room_id, include_cookie=not anonymous_auth),
                user_agent_header=BROWSER_USER_AGENT,
                ping_interval=None,
                max_size=None,
                open_timeout=timeout,
                proxy=None,
            ) as websocket:
                reconnect_delay = 2.0
                if debug:
                    print("websocket connected")
                await websocket.send(auth_packet(room_id, token, anonymous=anonymous_auth))
                if debug:
                    print("auth packet sent")
                heartbeat_task = asyncio.create_task(send_heartbeats(websocket))
                try:
                    async for message in websocket:
                        payload = message if isinstance(message, bytes) else message.encode("utf-8")
                        for command in decode_packets(payload, include_control=debug):
                            yield command
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            raise
        except recoverable_errors as exc:
            print(f"danmaku websocket disconnected: {exc}; reconnecting in {reconnect_delay:.0f}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)


async def send_heartbeats(websocket: object) -> None:
    while True:
        await asyncio.sleep(30)
        await websocket.send(pack_packet(OP_HEARTBEAT, b"[object Object]"))


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def websocket_headers(room_id: int, *, include_cookie: bool) -> dict[str, str]:
    headers = {
        "Referer": f"https://live.bilibili.com/{room_id}",
    }
    cookie = os.environ.get("BILIBILI_COOKIE") if include_cookie else None
    if cookie:
        headers["Cookie"] = cookie
    return headers


def websocket_anonymous_auth() -> bool:
    value = os.environ.get("BILIBILI_WS_ANON")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def auth_packet(room_id: int, token: str, *, anonymous: bool = False) -> bytes:
    body = {
        "uid": 0 if anonymous else parse_int(os.environ.get("BILIBILI_UID")) or 0,
        "roomid": room_id,
        "protover": PROTO_BROTLI,
        "buvid": "" if anonymous else os.environ.get("BILIBILI_BUVID") or buvid_from_cookie(os.environ.get("BILIBILI_COOKIE", "")),
        "platform": "web",
        "type": 2,
        "key": token,
    }
    return pack_packet(OP_AUTH, json.dumps(body, separators=(",", ":")).encode("utf-8"), protover=PROTO_NORMAL)


def pack_packet(operation: int, body: bytes = b"", *, protover: int = PROTO_NORMAL, sequence: int = 1) -> bytes:
    length = HEADER_LENGTH + len(body)
    return HEADER.pack(length, HEADER_LENGTH, protover, operation, sequence) + body


def decode_packets(payload: bytes, *, include_control: bool = False) -> list[dict]:
    commands: list[dict] = []
    offset = 0
    while offset + HEADER_LENGTH <= len(payload):
        packet_length, header_length, protover, operation, _sequence = HEADER.unpack_from(payload, offset)
        if packet_length <= 0 or offset + packet_length > len(payload):
            break
        body = payload[offset + header_length : offset + packet_length]
        if operation == OP_MESSAGE:
            commands.extend(decode_message_body(protover, body, include_control=include_control))
        elif include_control and operation == OP_AUTH_REPLY:
            commands.append({"cmd": "__AUTH_REPLY__", "body": decode_control_body(body)})
        elif include_control and operation == OP_HEARTBEAT_REPLY:
            commands.append({"cmd": "__HEARTBEAT_REPLY__", "body": decode_control_body(body)})
        offset += packet_length
    return commands


def decode_message_body(protover: int, body: bytes, *, include_control: bool = False) -> list[dict]:
    if protover in {PROTO_JSON, PROTO_NORMAL}:
        return [data] if isinstance((data := load_command(body)), dict) else []
    if protover == PROTO_ZLIB:
        return decode_packets(zlib.decompress(body), include_control=include_control)
    if protover == PROTO_BROTLI:
        try:
            import brotli
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency for brotli packets: `python -m pip install brotli`.") from exc
        return decode_packets(brotli.decompress(body), include_control=include_control)
    return []


def decode_control_body(body: bytes) -> object:
    if len(body) == 4:
        return struct.unpack(">I", body)[0]
    return load_command(body) or body.decode("utf-8", errors="replace")


def load_command(body: bytes) -> dict | None:
    text = body.decode("utf-8", errors="replace").strip("\x00\r\n ")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def command_name(command: dict) -> str:
    return str(command.get("cmd") or "").split(":", 1)[0]


def parse_danmaku(command: dict) -> DanmakuEvent | None:
    if command_name(command) != "DANMU_MSG":
        return None
    info = command.get("info")
    if not isinstance(info, list) or len(info) < 3:
        return None
    user = info[2] if isinstance(info[2], list) else []
    uid = parse_int(user[0] if len(user) > 0 else None)
    uname = str(user[1] if len(user) > 1 else "")
    text = str(info[1] if len(info) > 1 else "").strip()
    if uid is None or not text:
        return None
    return DanmakuEvent(uid=uid, uname=uname, text=text, raw=command)


def parse_history_danmaku(row: object) -> DanmakuEvent | None:
    if not isinstance(row, dict):
        return None
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    user_base = user.get("base") if isinstance(user.get("base"), dict) else {}
    uid = parse_int(row.get("uid")) or parse_int(user.get("uid"))
    uname = str(row.get("nickname") or user_base.get("name") or "")
    if uid is None:
        uid = 0
    return DanmakuEvent(uid=uid, uname=uname, text=text, raw={"cmd": "DANMU_HISTORY", "data": row})


def parse_gift(command: dict) -> GiftEvent | None:
    cmd = command_name(command)
    if cmd not in {"SEND_GIFT", "COMBO_SEND", "GUARD_BUY", "USER_TOAST_MSG"}:
        return None
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    uid = parse_int(first_present(data, "uid", "user_id"))
    if uid is None:
        return None
    gift_name = str(
        first_present(
            data,
            "giftName",
            "gift_name",
            "role_name",
            "guard_level_name",
            "name",
        )
        or cmd
    )
    gift_id = parse_int(first_present(data, "giftId", "gift_id", "role_id", "guard_level"))
    num = parse_int(first_present(data, "num", "gift_num", "combo_num", "total_num")) or 1
    price = parse_int(first_present(data, "price", "discount_price", "gift_price", "unit_price")) or 0
    total_coin = parse_int(
        first_present(
            data,
            "total_coin",
            "total_price",
            "combo_total_coin",
        )
    )
    if total_coin is None:
        total_coin = price * max(num, 1)
    return GiftEvent(
        uid=uid,
        uname=str(first_present(data, "uname", "username", "user_name") or ""),
        gift_name=gift_name,
        gift_id=gift_id,
        num=num,
        price=price,
        total_coin=total_coin,
        coin_type=str(first_present(data, "coin_type", "pay_type") or ""),
        raw=command,
    )


def parse_super_chat(command: dict) -> SuperChatEvent | None:
    if command_name(command) != "SUPER_CHAT_MESSAGE":
        return None
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    uid = parse_int(data.get("uid"))
    user_info = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
    message = str(data.get("message") or "").strip()
    if uid is None or not message:
        return None
    return SuperChatEvent(
        uid=uid,
        uname=str(user_info.get("uname") or data.get("uname") or ""),
        message=message,
        price=parse_int(data.get("price")) or 0,
        raw=command,
    )


def parse_int(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def first_present(data: dict, *keys: str) -> object | None:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def sign_wbi_params(params: dict[str, object], timeout: float = 10.0) -> dict[str, object]:
    mixin_key = get_wbi_mixin_key(timeout=timeout)
    signed = {key: value for key, value in params.items() if value is not None}
    signed["wts"] = int(time.time())
    query = urlencode(
        [
            (key, clean_wbi_value(signed[key]))
            for key in sorted(signed)
        ]
    )
    signed["w_rid"] = hashlib.md5(f"{query}{mixin_key}".encode("utf-8")).hexdigest()
    return signed


def get_wbi_mixin_key(timeout: float = 10.0) -> str:
    data = http_json(BILIBILI_NAV_URL, timeout=timeout)
    wbi_img = ((data.get("data") or {}).get("wbi_img") or {}) if isinstance(data, dict) else {}
    img_key = file_stem(str(wbi_img.get("img_url") or ""))
    sub_key = file_stem(str(wbi_img.get("sub_url") or ""))
    raw_key = img_key + sub_key
    if len(raw_key) < 64:
        raise RuntimeError("Could not fetch Bilibili WBI keys.")
    return "".join(raw_key[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]


def file_stem(url: str) -> str:
    name = url.rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def clean_wbi_value(value: object) -> str:
    text = str(value)
    return "".join(char for char in text if char not in "!'()*")


def buvid_from_cookie(cookie: str) -> str:
    match = re.search(r"(?:^|;\s*)buvid3=([^;]+)", cookie)
    return match.group(1) if match else ""
