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
from urllib.parse import quote, unquote, urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen


BILIBILI_ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init?id={room}"
BILIBILI_ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}"
BILIBILI_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
BILIBILI_DANMU_HISTORY_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={room}"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_BUVID_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
BILIBILI_GET_BUVID_URL = "https://api.bilibili.com/x/web-frontend/getbuvid"
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
BROWSER_COOKIE_NAMES = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
    "LIVE_BUVID",
    "_uuid",
    "b_lsid",
    "b_nut",
    "bili_ticket",
    "bili_ticket_expires",
    "buvid_fp",
    "buvid_fp_plain",
    "sid",
}
BILIBILI_COOKIE_ENV_PARTS = (
    ("BILIBILI_COOKIE_SESSDATA", "SESSDATA"),
    ("BILIBILI_COOKIE_BILI_JCT", "bili_jct"),
    ("BILIBILI_COOKIE_DEDEUSERID", "DedeUserID"),
    ("BILIBILI_COOKIE_DEDEUSERID_CKMD5", "DedeUserID__ckMd5"),
    ("BILIBILI_COOKIE_BUVID3", "buvid3"),
    ("BILIBILI_COOKIE_BUVID4", "buvid4"),
    ("BILIBILI_COOKIE_LIVE_BUVID", "LIVE_BUVID"),
)
_BROWSER_COOKIE_LOADED = False


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


@dataclass(frozen=True)
class BilibiliAuthConfig:
    anonymous: bool
    cookie_present: bool
    uid: int
    uid_source: str
    buvid: str
    buvid_source: str

    def summary(self) -> str:
        if self.anonymous:
            return "anonymous"
        uid_status = self.uid_source if self.uid else "missing"
        buvid_status = self.buvid_source if self.buvid else "missing"
        cookie_status = "yes" if self.cookie_present else "no"
        return f"logged-in; cookie={cookie_status}; uid={uid_status}; buvid={buvid_status}"


def extract_room_arg(value: str) -> str:
    match = re.search(r"live\.bilibili\.com/(\d+)", value)
    if match:
        return match.group(1)
    return value.strip()


def resolve_room_id(room: str | int, timeout: float = 10.0) -> int:
    room_arg = extract_room_arg(str(room))
    data = http_json(BILIBILI_ROOM_INIT_URL.format(room=room_arg), timeout=timeout, use_cookie=False)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili room_init failed: {data.get('message') or data}")
    room_data = data.get("data") or {}
    return int(room_data.get("room_id") or room_arg)


def get_room_info(room_id: int, timeout: float = 10.0) -> dict:
    data = http_json(BILIBILI_ROOM_INFO_URL.format(room=room_id), timeout=timeout, use_cookie=False)
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
    if use_cookie:
        ensure_bilibili_cookie_loaded()
    cookie = current_bilibili_cookie() if use_cookie else None
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
    logged_in_auth_failures = 0
    anonymous_fallback_until = 0.0
    while True:
        auth_config = resolve_bilibili_auth(timeout=timeout)
        if (
            not auth_config.anonymous
            and time.monotonic() < anonymous_fallback_until
            and not env_disabled("BILIBILI_ANON_FALLBACK")
        ):
            auth_config = anonymous_bilibili_auth_config(cookie_present=auth_config.cookie_present)
        got_command = False
        try:
            ws_url, token = get_danmaku_info(
                room_id,
                timeout=timeout,
                force_default_ws=force_default_ws,
                use_cookie=not auth_config.anonymous,
            )
            if debug:
                print(f"danmaku websocket: {ws_url}")
                print(f"danmaku websocket auth: {auth_config.summary()}")
            async with websockets.connect(
                ws_url,
                origin="https://live.bilibili.com",
                additional_headers=websocket_headers(room_id, include_cookie=not auth_config.anonymous),
                user_agent_header=BROWSER_USER_AGENT,
                ping_interval=None,
                max_size=None,
                open_timeout=timeout,
                compression=None,
                proxy=None,
            ) as websocket:
                reconnect_delay = 2.0
                if debug:
                    print("websocket connected")
                await websocket.send(auth_packet(room_id, token, auth=auth_config))
                if debug:
                    print("auth packet sent")
                heartbeat_task = asyncio.create_task(send_heartbeats(websocket))
                try:
                    async for message in websocket:
                        payload = message if isinstance(message, bytes) else message.encode("utf-8")
                        for command in decode_packets(payload, include_control=debug):
                            got_command = True
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
            if (
                not auth_config.anonymous
                and not got_command
                and not env_disabled("BILIBILI_ANON_FALLBACK")
                and not bilibili_require_login()
            ):
                logged_in_auth_failures += 1
                if logged_in_auth_failures >= 2:
                    anonymous_fallback_until = time.monotonic() + 60.0
                    logged_in_auth_failures = 0
                    print("logged-in danmaku auth failed twice; falling back to anonymous websocket for 60s")
            elif got_command:
                logged_in_auth_failures = 0
            print(f"danmaku websocket disconnected: {exc}; reconnecting in {reconnect_delay:.0f}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)


async def send_heartbeats(websocket: object) -> None:
    while True:
        await websocket.send(pack_packet(OP_HEARTBEAT, b"[object Object]"))
        await asyncio.sleep(30)


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def websocket_headers(room_id: int, *, include_cookie: bool) -> dict[str, str]:
    headers = {
        "Referer": f"https://live.bilibili.com/{room_id}",
    }
    cookie = current_bilibili_cookie() if include_cookie else None
    if cookie:
        headers["Cookie"] = cookie
    return headers


def websocket_anonymous_auth() -> bool:
    if bilibili_require_login():
        return False
    value = os.environ.get("BILIBILI_WS_ANON")
    if value is not None:
        return env_truthy(value)
    return not has_bilibili_login_hint()


def has_bilibili_login_hint() -> bool:
    return bool(
        current_bilibili_cookie()
        or os.environ.get("BILIBILI_UID")
        or os.environ.get("BILIBILI_BUVID")
    )


def env_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_disabled(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"0", "false", "no", "off"}


def bilibili_require_login() -> bool:
    return env_truthy(os.environ.get("BILIBILI_REQUIRE_LOGIN", ""))


def ensure_bilibili_cookie_loaded(*, force: bool = False) -> str:
    global _BROWSER_COOKIE_LOADED
    source = os.environ.get("BILIBILI_COOKIE_SOURCE", "auto").strip().lower()
    if source not in {"browser", "devtools", "auto"}:
        return current_bilibili_cookie()
    if _BROWSER_COOKIE_LOADED and not force:
        return current_bilibili_cookie()
    if source == "auto" and current_bilibili_cookie() and not force:
        _BROWSER_COOKIE_LOADED = True
        return current_bilibili_cookie()

    cookie, source_name = load_bilibili_cookie_from_configured_source(source)
    if cookie:
        os.environ["BILIBILI_COOKIE"] = cookie
        os.environ["BILIBILI_COOKIE_LOADED_FROM"] = source_name
    _BROWSER_COOKIE_LOADED = True
    return current_bilibili_cookie()


def current_bilibili_cookie() -> str:
    parts = []
    for env_name, cookie_name in BILIBILI_COOKIE_ENV_PARTS:
        value = os.environ.get(env_name, "").strip()
        if value:
            parts.append(f"{cookie_name}={normalize_cookie_value(value)}")
    if parts:
        return "; ".join(parts)
    return os.environ.get("BILIBILI_COOKIE", "").strip()


def normalize_cookie_value(value: str) -> str:
    # Chrome can copy cookie values either raw (`%2C`) or URL-decoded (`,`).
    # HTTP Cookie headers should use the raw escaped form for Bilibili auth.
    return quote(value.strip(), safe="%._~-")


def load_bilibili_cookie_from_configured_source(source: str) -> tuple[str, str]:
    if source == "devtools":
        cookie, source_name = load_devtools_bilibili_cookie()
        if not cookie:
            raise RuntimeError("Could not load Bilibili cookies from DevTools. Is the login browser running?")
        return cookie, source_name
    if source == "browser":
        cookie, browser_name = load_browser_bilibili_cookie()
        return cookie, f"browser:{browser_name}" if cookie else ""

    cookie, source_name = load_devtools_bilibili_cookie()
    if cookie:
        return cookie, source_name
    cookie, browser_name = load_browser_bilibili_cookie()
    return (cookie, f"browser:{browser_name}") if cookie else ("", "")


def load_devtools_bilibili_cookie(timeout: float = 5.0) -> tuple[str, str]:
    base_url = os.environ.get("BILIBILI_DEVTOOLS_URL", "http://127.0.0.1:9222").rstrip("/")
    try:
        ws_urls = devtools_websocket_urls(base_url, timeout=timeout)
    except RuntimeError:
        return "", ""

    errors: list[str] = []
    for ws_url in ws_urls:
        try:
            cookies = devtools_get_all_cookies(ws_url, timeout=timeout)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        cookie_header = cookie_header_from_browser_rows(cookies)
        if "SESSDATA=" in cookie_header and "DedeUserID=" in cookie_header:
            return cookie_header, "devtools"
    if os.environ.get("BILIBILI_COOKIE_SOURCE", "").strip().lower() == "devtools" and errors:
        raise RuntimeError(f"Could not load Bilibili cookies from DevTools: {'; '.join(errors[-3:])}")
    return "", ""


def devtools_websocket_urls(base_url: str, *, timeout: float) -> list[str]:
    urls: list[str] = []
    last_error: object = "no websocket targets"
    for endpoint in ("/json/version", "/json/list"):
        try:
            with urlopen(f"{base_url}{endpoint}", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            continue
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            ws_url = str(row.get("webSocketDebuggerUrl") or "")
            if ws_url and ws_url not in urls:
                urls.append(ws_url)
    if not urls:
        raise RuntimeError(f"DevTools is not available at {base_url}: {last_error!r}")
    return urls


def devtools_get_all_cookies(ws_url: str, *, timeout: float) -> list[dict]:
    try:
        import websocket
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "BILIBILI_COOKIE_SOURCE=devtools requires websocket-client. "
            "Install it with `.venv\\Scripts\\python.exe -m pip install websocket-client`."
        ) from exc
    try:
        connection = websocket.create_connection(ws_url, timeout=timeout, origin="http://127.0.0.1")
    except Exception as exc:
        raise RuntimeError(f"DevTools websocket connection failed: {exc}") from exc
    try:
        errors: list[str] = []
        for request_id, method in enumerate(("Network.getAllCookies", "Storage.getCookies"), start=1):
            request = {"id": request_id, "method": method}
            connection.send(json.dumps(request, separators=(",", ":")))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                message = json.loads(connection.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    errors.append(f"{method}: {message['error']}")
                    break
                result = message.get("result") if isinstance(message.get("result"), dict) else {}
                cookies = result.get("cookies")
                return cookies if isinstance(cookies, list) else []
        raise RuntimeError(f"DevTools cookie request failed: {'; '.join(errors)}")
    finally:
        connection.close()


def cookie_header_from_browser_rows(rows: list[dict]) -> str:
    cookies: dict[str, str] = {}
    for row in rows:
        domain = str(row.get("domain") or "")
        name = str(row.get("name") or "")
        value = str(row.get("value") or "")
        if "bilibili.com" not in domain or not name or not value:
            continue
        if name in BROWSER_COOKIE_NAMES or name.startswith("bili_"):
            cookies[name] = value
    return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))


def load_browser_bilibili_cookie() -> tuple[str, str]:
    try:
        import browser_cookie3
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "BILIBILI_COOKIE_SOURCE=browser requires browser-cookie3. "
            "Install it with `.venv\\Scripts\\python.exe -m pip install browser-cookie3`."
        ) from exc

    requested = os.environ.get("BILIBILI_COOKIE_BROWSER", "auto").strip().lower()
    browser_names = [requested] if requested != "auto" else ["edge", "chrome", "firefox"]
    domains = (".bilibili.com", "bilibili.com", "live.bilibili.com")
    errors: list[str] = []
    for browser_name in browser_names:
        getter = getattr(browser_cookie3, browser_name, None)
        if getter is None:
            errors.append(f"{browser_name}: unsupported browser")
            continue
        cookies: dict[str, str] = {}
        for domain in domains:
            try:
                jar = getter(domain_name=domain)
            except Exception as exc:  # browser_cookie3 raises browser-specific errors.
                errors.append(f"{browser_name}:{domain}: {exc}")
                continue
            for cookie in jar:
                if "bilibili.com" not in str(cookie.domain):
                    continue
                if cookie.name in BROWSER_COOKIE_NAMES or cookie.name.startswith("bili_"):
                    cookies[cookie.name] = cookie.value
        if "SESSDATA" in cookies and ("DedeUserID" in cookies or "buvid3" in cookies):
            return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items())), browser_name
    if os.environ.get("BILIBILI_COOKIE_SOURCE", "").strip().lower() == "browser":
        detail = "; ".join(errors[-5:]) if errors else "no Bilibili login cookie found"
        raise RuntimeError(f"Could not load Bilibili cookies from browser: {detail}")
    return "", ""


def resolve_bilibili_auth(*, timeout: float = 10.0) -> BilibiliAuthConfig:
    ensure_bilibili_cookie_loaded()
    anonymous = websocket_anonymous_auth()
    cookie = current_bilibili_cookie()
    if anonymous:
        return anonymous_bilibili_auth_config(cookie_present=bool(cookie))

    uid, uid_source = bilibili_uid(timeout=timeout)
    buvid, buvid_source = bilibili_buvid(timeout=timeout)
    if buvid and not os.environ.get("BILIBILI_BUVID"):
        os.environ["BILIBILI_BUVID"] = buvid
    return BilibiliAuthConfig(
        anonymous=False,
        cookie_present=bool(cookie),
        uid=uid,
        uid_source=uid_source,
        buvid=buvid,
        buvid_source=buvid_source,
    )


def anonymous_bilibili_auth_config(*, cookie_present: bool) -> BilibiliAuthConfig:
    return BilibiliAuthConfig(
        anonymous=True,
        cookie_present=cookie_present,
        uid=0,
        uid_source="anonymous",
        buvid="",
        buvid_source="anonymous",
    )


def bilibili_uid(*, timeout: float = 10.0) -> tuple[int, str]:
    cookie = current_bilibili_cookie()
    uid = parse_int(cookie_value(cookie, "DedeUserID"))
    if uid is not None:
        return uid, "cookie"
    uid = parse_int(os.environ.get("BILIBILI_UID"))
    if uid is not None:
        return uid, "env"
    if cookie:
        uid = fetch_logged_in_uid(timeout=timeout)
        if uid is not None:
            return uid, "nav"
    return 0, "missing"


def fetch_logged_in_uid(*, timeout: float = 10.0) -> int | None:
    try:
        data = http_json(BILIBILI_NAV_URL, timeout=timeout, use_cookie=True)
    except RuntimeError:
        return None
    if data.get("code") != 0:
        return None
    nav_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    return parse_int(nav_data.get("mid"))


def get_bilibili_login_status(*, timeout: float = 10.0) -> dict:
    ensure_bilibili_cookie_loaded()
    data = http_json(BILIBILI_NAV_URL, timeout=timeout, use_cookie=True)
    if not (((data.get("data") or {}) if isinstance(data, dict) else {}).get("isLogin")):
        source = os.environ.get("BILIBILI_COOKIE_SOURCE", "auto").strip().lower()
        loaded_from = os.environ.get("BILIBILI_COOKIE_LOADED_FROM", "")
        if source == "auto" and loaded_from not in {"devtools"} and not loaded_from.startswith("browser:"):
            ensure_bilibili_cookie_loaded(force=True)
            data = http_json(BILIBILI_NAV_URL, timeout=timeout, use_cookie=True)
    if data.get("code") != 0:
        return {"ok": False, "message": data.get("message") or data}
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {
        "ok": bool(payload.get("isLogin")),
        "mid": parse_int(payload.get("mid")) or 0,
        "uname": str(payload.get("uname") or ""),
        "cookie_loaded_from": os.environ.get("BILIBILI_COOKIE_LOADED_FROM") or "env",
    }


def bilibili_buvid(*, timeout: float = 10.0) -> tuple[str, str]:
    cookie = current_bilibili_cookie()
    for name in ("buvid3", "LIVE_BUVID", "buvid4", "buvid_fp"):
        value = cookie_value(cookie, name)
        if value:
            return value, f"cookie:{name}"

    env_value = os.environ.get("BILIBILI_BUVID", "").strip()
    if env_value:
        return env_value, "env"

    if not env_disabled("BILIBILI_AUTO_BUVID"):
        fetched = fetch_buvid(timeout=timeout)
        if fetched:
            return fetched, "api"
    return "", "missing"


def fetch_buvid(*, timeout: float = 10.0) -> str:
    for url, keys in (
        (BILIBILI_BUVID_SPI_URL, ("b_3",)),
        (BILIBILI_GET_BUVID_URL, ("buvid",)),
    ):
        try:
            data = http_json(url, timeout=timeout, use_cookie=False)
        except RuntimeError:
            continue
        if data.get("code") != 0:
            continue
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return ""


def auth_packet(
    room_id: int,
    token: str,
    *,
    auth: BilibiliAuthConfig | None = None,
    anonymous: bool = False,
) -> bytes:
    if auth is None:
        auth = resolve_bilibili_auth() if not anonymous else BilibiliAuthConfig(
            anonymous=True,
            cookie_present=bool(current_bilibili_cookie()),
            uid=0,
            uid_source="anonymous",
            buvid="",
            buvid_source="anonymous",
        )
    body = {
        "uid": 0 if auth.anonymous else auth.uid,
        "roomid": room_id,
        "protover": requested_protover(),
        "buvid": "" if auth.anonymous else auth.buvid,
        "platform": "web",
        "type": 2,
        "key": token,
    }
    return pack_packet(OP_AUTH, json.dumps(body, separators=(",", ":")).encode("utf-8"), protover=PROTO_NORMAL)


def requested_protover() -> int:
    value = os.environ.get("BILIBILI_PROTO_VER") or os.environ.get("BILIBILI_PROTO")
    parsed = parse_int(value)
    if parsed in {PROTO_NORMAL, PROTO_ZLIB, PROTO_BROTLI}:
        return parsed
    return PROTO_ZLIB


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
    return cookie_value(cookie, "buvid3")


def cookie_value(cookie: str, name: str) -> str:
    pattern = rf"(?:^|;\s*){re.escape(name)}=([^;]+)"
    match = re.search(pattern, cookie)
    if not match:
        return ""
    return unquote(match.group(1).strip())
