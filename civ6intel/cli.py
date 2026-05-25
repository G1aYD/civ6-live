from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .archive import inspect_logs
from .bilibili_live import get_bilibili_login_status, get_danmaku_info, resolve_bilibili_auth, resolve_room_id
from .config import load_paths
from .events import format_events, human_live_events
from .llm_client import DEFAULT_OPENAI_MODEL, LLMError, ask_openai, load_env_file
from .llm_context import direct_game_answer, llm_context_json, llm_context_prompt
from .obs_news import check_leader_icons, format_icon_check_report, run_obs_news
from .overlay_http import run_overlay_server, start_overlay_server
from .overlay_state import run_overlay_json
from .query import answer_question, context_json
from .save_reader import read_latest_save_summary
from .state import load_snapshot
from .stream_bot import DEFAULT_ANSWER_CHAR_LIMIT, run_bilibili_obs_bot, sanitize_llm_output
from .watch import run_watch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse Civ 6 logs and answer grounded stat questions.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="Answer a question from parsed log state.")
    ask_parser.add_argument("question", nargs="+")

    llm_ask_parser = subparsers.add_parser("llm-ask", help="Answer a question with the configured OpenAI model.")
    llm_ask_parser.add_argument("question", nargs="+")
    llm_ask_parser.add_argument("--turn", default="latest", help="Turn number or 'latest'.")
    llm_ask_parser.add_argument("--limit", type=int, default=30, help="Maximum event rows per context section.")
    llm_ask_parser.add_argument(
        "--model",
        default=None,
        help=f"Model override. Defaults to OPENAI_MODEL or {DEFAULT_OPENAI_MODEL}.",
    )
    llm_ask_parser.add_argument("--env-file", default=".env", help="File containing OPENAI_API_KEY.")
    llm_ask_parser.add_argument("--timeout", type=float, default=60.0, help="OpenAI request timeout in seconds.")
    llm_ask_parser.add_argument("--answer-char-limit", type=int, default=DEFAULT_ANSWER_CHAR_LIMIT, help="Maximum characters printed for one LLM answer.")
    llm_ask_parser.add_argument("--dry-run", action="store_true", help="Print the prompt without calling the API.")

    context_parser = subparsers.add_parser("context", help="Print compact LLM-ready game state JSON.")
    context_parser.add_argument("--turn", default="latest", help="Turn number or 'latest'.")

    save_context_parser = subparsers.add_parser(
        "save-context",
        help="Print CivVIReplay-style information parsed from the latest autosave.",
    )
    save_context_parser.add_argument(
        "--no-map",
        action="store_true",
        help="Only parse the save header. Skips decompressing the large map/state block.",
    )

    llm_context_parser = subparsers.add_parser("llm-context", help="Print rich grounded context for an LLM.")
    llm_context_parser.add_argument("--turn", default="latest", help="Turn number or 'latest'.")
    llm_context_parser.add_argument("--limit", type=int, default=30, help="Maximum event rows per section.")
    llm_context_parser.add_argument(
        "--format",
        choices=["json", "prompt"],
        default="prompt",
        help="Output raw JSON or a Chinese instruction prompt containing JSON.",
    )

    bili_parser = subparsers.add_parser(
        "bili-obs",
        help="Listen to Bilibili live chat, answer questions with the LLM, and write OBS text.",
    )
    bili_parser.add_argument("--room", default="https://live.bilibili.com/8555868", help="Bilibili room URL or id.")
    bili_parser.add_argument("--obs-text", default="obs/answer.txt", help="UTF-8 text file for OBS Text source.")
    bili_parser.add_argument("--news-text", default=None, help="Optional UTF-8 ticker file for OBS scrolling news.")
    bili_parser.add_argument("--news-interval", type=float, default=2.0, help="Seconds between OBS news ticker refreshes.")
    bili_parser.add_argument("--news-limit", type=int, default=20, help="Maximum news items in the OBS ticker.")
    bili_parser.add_argument("--overlay-json", default=None, help="Optional JSON file for the HTML OBS overlay.")
    bili_parser.add_argument("--serve-overlay", action="store_true", help="Serve --overlay-json at /overlay.json.")
    bili_parser.add_argument("--overlay-host", default="127.0.0.1", help="Overlay JSON server host.")
    bili_parser.add_argument("--overlay-port", type=int, default=8787, help="Overlay JSON server port.")
    bili_parser.add_argument(
        "--question-mode",
        choices=["heuristic", "marker", "any", "bang"],
        default="heuristic",
        help="How to decide whether a danmaku is a question.",
    )
    bili_parser.add_argument("--gift-name", help="Only accept normal danmaku questions from users who sent this gift.")
    bili_parser.add_argument("--gift-id", type=int, help="Only accept normal danmaku questions from users who sent this gift id.")
    bili_parser.add_argument(
        "--min-gift-value",
        type=int,
        help="Only accept normal danmaku questions from users whose gift total_coin is at least this value.",
    )
    bili_parser.add_argument("--gift-window", type=float, default=600.0, help="Seconds a matching gift enables questions.")
    bili_parser.add_argument("--consume-gift", action="store_true", help="Use one matching gift for one question.")
    bili_parser.add_argument(
        "--require-gift-credit",
        action="store_true",
        help="Only accept normal danmaku questions from users with gift coin balance.",
    )
    bili_parser.add_argument(
        "--gift-question-cost",
        type=int,
        default=None,
        help="Gift coin cost for one danmaku question. Defaults to CIV6_GIFT_QUESTION_COST or 100.",
    )
    bili_parser.add_argument("--gift-log", default="obs/gifts.jsonl", help="JSONL log of every received gift.")
    bili_parser.add_argument("--gift-totals", default="obs/gift_totals.json", help="Persistent gift coin totals by user.")
    bili_parser.add_argument("--gift-obs-text", default="obs/gifts.txt", help="UTF-8 gift status text file for OBS.")
    bili_parser.add_argument("--no-super-chat", action="store_true", help="Do not auto-accept super chats.")
    bili_parser.add_argument("--no-llm", action="store_true", help="Listen and update OBS without calling the LLM.")
    bili_parser.add_argument("--model", default=None, help=f"Model override. Defaults to OPENAI_MODEL or {DEFAULT_OPENAI_MODEL}.")
    bili_parser.add_argument("--env-file", default=".env", help="File containing OPENAI_API_KEY.")
    bili_parser.add_argument("--timeout", type=float, default=60.0, help="OpenAI request timeout in seconds.")
    bili_parser.add_argument("--limit", type=int, default=30, help="Maximum event rows per LLM context section.")
    bili_parser.add_argument("--answer-char-limit", type=int, default=DEFAULT_ANSWER_CHAR_LIMIT, help="Maximum characters shown for one LLM answer.")
    bili_parser.add_argument("--duration", type=float, help="Stop after this many seconds.")
    bili_parser.add_argument("--debug-danmaku", action="store_true", help="Print every received danmaku and why it was accepted or ignored.")
    bili_parser.add_argument("--debug-commands", action="store_true", help="Print every raw Bilibili command name received.")
    bili_parser.add_argument("--debug-command-json", action="store_true", help="Print compact raw command payloads for Bilibili diagnostics.")
    bili_parser.add_argument("--bili-default-ws", action="store_true", help="Use Bilibili's default broadcast websocket. This is now the default for bili-obs.")
    bili_parser.add_argument("--bili-host-ws", action="store_true", help="Use the first websocket host returned by getDanmuInfo instead of the default broadcast websocket.")
    bili_parser.add_argument("--no-history-poll", action="store_true", help="Disable polling Bilibili's recent danmaku history as a fallback.")
    bili_parser.add_argument("--history-interval", type=float, default=1.0, help="Seconds between recent danmaku history polls.")
    bili_parser.add_argument(
        "--websocket-danmaku",
        action="store_true",
        help="Also process websocket DANMU_MSG. Faster, but anonymous Bilibili websocket data may mask usernames.",
    )

    news_parser = subparsers.add_parser("obs-news", help="Write a scrolling Civ 6 news ticker text file for OBS.")
    news_parser.add_argument("--obs-text", default="obs/news.txt", help="UTF-8 text file for OBS Text source.")
    news_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds.")
    news_parser.add_argument("--limit", type=int, default=20, help="Maximum news items in the ticker.")
    news_parser.add_argument("--separator", default="\n", help="Separator between ticker items. Default: newline.")
    news_parser.add_argument("--once", action="store_true", help="Write one ticker snapshot and exit.")
    news_parser.add_argument("--duration", type=float, help="Stop after this many seconds.")

    icon_parser = subparsers.add_parser("check-icons", help="Check BBG leader image URLs for the current game.")
    icon_parser.add_argument("--timeout", type=float, default=5.0, help="GitHub API timeout in seconds.")
    icon_parser.add_argument("--offline", action="store_true", help="Only print computed filenames and URLs; do not call GitHub.")
    icon_parser.add_argument("--all", action="store_true", help="Check all human-playable leaders in Civ 6's merged configuration database.")

    bili_check_parser = subparsers.add_parser("bili-check", help="Check Bilibili room, auth, and danmaku websocket setup.")
    bili_check_parser.add_argument("--room", default="https://live.bilibili.com/8555868", help="Bilibili room URL or id.")
    bili_check_parser.add_argument("--env-file", default=".env", help="File containing Bilibili cookie settings.")
    bili_check_parser.add_argument("--timeout", type=float, default=10.0, help="Bilibili request timeout in seconds.")
    bili_check_parser.add_argument("--bili-default-ws", action="store_true", help="Use the default broadcast websocket when checking.")
    bili_check_parser.add_argument("--bili-host-ws", action="store_true", help="Use the first websocket host returned by getDanmuInfo.")

    overlay_parser = subparsers.add_parser("obs-overlay", help="Write an OBS browser overlay JSON file.")
    overlay_parser.add_argument("--overlay-json", default="obs/overlay.json", help="JSON file consumed by overlay HTML.")
    overlay_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds.")
    overlay_parser.add_argument("--limit", type=int, default=20, help="Maximum news items in the overlay.")
    overlay_parser.add_argument("--once", action="store_true", help="Write one JSON snapshot and exit.")
    overlay_parser.add_argument("--duration", type=float, help="Stop after this many seconds.")

    overlay_server_parser = subparsers.add_parser("overlay-server", help="Serve overlay JSON to OBS/browser sources.")
    overlay_server_parser.add_argument("--overlay-json", default="obs/overlay.json", help="JSON file to serve.")
    overlay_server_parser.add_argument("--host", default="127.0.0.1", help="HTTP host.")
    overlay_server_parser.add_argument("--port", type=int, default=8787, help="HTTP port.")

    watch_parser = subparsers.add_parser("watch", help="Watch Civ 6 files and print parsed state after updates.")
    watch_parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    watch_parser.add_argument("--max-files", type=int, default=12, help="Changed files to print per section.")
    watch_parser.add_argument("--once", action="store_true", help="Print one snapshot and exit.")
    watch_parser.add_argument("--duration", type=float, help="Stop after this many seconds.")
    watch_parser.add_argument("--all-files", action="store_true", help="Watch every configured file, including noisy logs.")
    watch_parser.add_argument("--event-limit", type=int, default=8, help="New human events to print per update.")
    watch_parser.add_argument(
        "--event-mode",
        choices=["important", "all", "none"],
        default="important",
        help="Which live events to print. Important mode hides routine movement.",
    )

    events_parser = subparsers.add_parser("events", help="Print recent human live-action events from logs.")
    events_parser.add_argument("--limit", type=int, default=40, help="Maximum events to print.")
    events_parser.add_argument(
        "--event-mode",
        choices=["important", "all"],
        default="important",
        help="Which live events to print.",
    )

    inspect_parser = subparsers.add_parser("inspect-logs", help="Inspect archived Civ 6 log folders.")
    inspect_parser.add_argument("path", help="A Logs folder or a parent folder containing Logs_* folders.")
    inspect_parser.add_argument("--limit", type=int, default=12, help="Maximum findings to print per section.")

    args = parser.parse_args(argv)

    if args.command == "inspect-logs":
        print(inspect_logs(Path(args.path), limit=args.limit))
        return 0

    paths = load_paths(args.config)

    if args.command == "ask":
        snapshot = load_snapshot(paths)
        print(answer_question(snapshot, " ".join(args.question)))
        return 0

    if args.command == "llm-ask":
        load_env_file(args.env_file)
        snapshot = load_snapshot(paths)
        turn = None if args.turn == "latest" else int(args.turn)
        question = " ".join(args.question)
        direct_answer = direct_game_answer(paths, snapshot, question)
        if direct_answer and not args.dry_run:
            print(sanitize_llm_output(direct_answer, max_chars=args.answer_char_limit))
            return 0
        prompt = llm_context_prompt(paths, snapshot, turn=turn, limit=args.limit, question=question)
        if args.dry_run:
            print(f"{prompt}\n\n观众问题：{question}")
            return 0
        try:
            answer = ask_openai(
                prompt,
                question,
                model=args.model,
                env_file=args.env_file,
                timeout=args.timeout,
            )
        except LLMError as exc:
            print(f"LLM request failed: {exc}", file=sys.stderr)
            return 1
        print(sanitize_llm_output(answer.text, max_chars=args.answer_char_limit))
        return 0

    if args.command == "context":
        snapshot = load_snapshot(paths)
        turn = None if args.turn == "latest" else int(args.turn)
        print(context_json(snapshot, turn))
        return 0

    if args.command == "save-context":
        summary = read_latest_save_summary(paths, include_map=not args.no_map)
        print(json.dumps(summary or {"available": False}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "llm-context":
        load_env_file(".env")
        snapshot = load_snapshot(paths)
        turn = None if args.turn == "latest" else int(args.turn)
        if args.format == "json":
            print(llm_context_json(paths, snapshot, turn=turn, limit=args.limit))
        else:
            print(llm_context_prompt(paths, snapshot, turn=turn, limit=args.limit))
        return 0

    if args.command == "bili-obs":
        overlay_json = Path(args.overlay_json) if args.overlay_json else (Path("obs/overlay.json") if args.serve_overlay else None)
        server = None
        if args.serve_overlay:
            server = start_overlay_server(overlay_json, host=args.overlay_host, port=args.overlay_port)
        try:
            asyncio.run(
                run_bilibili_obs_bot(
                    paths,
                    room=args.room,
                    obs_text=Path(args.obs_text),
                    question_mode=args.question_mode,
                    gift_name=args.gift_name,
                    gift_id=args.gift_id,
                    min_gift_value=args.min_gift_value,
                    gift_window=args.gift_window,
                    consume_gift=args.consume_gift,
                    require_gift_credit=args.require_gift_credit,
                    gift_question_cost=args.gift_question_cost,
                    gift_log=Path(args.gift_log),
                    gift_totals=Path(args.gift_totals),
                    gift_obs_text=Path(args.gift_obs_text) if args.gift_obs_text else None,
                    allow_super_chat=not args.no_super_chat,
                    no_llm=args.no_llm,
                    model=args.model,
                    env_file=args.env_file,
                    llm_timeout=args.timeout,
                    context_limit=args.limit,
                    answer_char_limit=args.answer_char_limit,
                    news_text=Path(args.news_text) if args.news_text else None,
                    news_interval=args.news_interval,
                    news_limit=args.news_limit,
                    overlay_json=overlay_json,
                    duration=args.duration,
                    debug_danmaku=args.debug_danmaku,
                    debug_commands=args.debug_commands,
                    debug_command_json=args.debug_command_json,
                    force_default_ws=args.bili_default_ws or not args.bili_host_ws,
                    history_poll=not args.no_history_poll,
                    history_interval=args.history_interval,
                    websocket_danmaku=args.websocket_danmaku,
                )
            )
        except KeyboardInterrupt:
            print("Stopped Bilibili OBS bot.")
            return 130
        except RuntimeError as exc:
            print(f"Bilibili OBS bot failed: {exc}", file=sys.stderr)
            return 1
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
        return 0

    if args.command == "obs-news":
        run_obs_news(
            paths,
            obs_text=Path(args.obs_text),
            interval=args.interval,
            limit=args.limit,
            separator=args.separator,
            once=args.once,
            duration=args.duration,
        )
        return 0

    if args.command == "check-icons":
        rows = check_leader_icons(paths, timeout=args.timeout, offline=args.offline, all_known=args.all)
        print(console_safe_text(format_icon_check_report(rows)))
        return 1 if any(row.get("status") == "missing" for row in rows) else 0

    if args.command == "bili-check":
        load_env_file(args.env_file)
        room_id = resolve_room_id(args.room, timeout=args.timeout)
        try:
            auth = resolve_bilibili_auth(timeout=args.timeout)
            login = get_bilibili_login_status(timeout=args.timeout)
        except RuntimeError as exc:
            print(console_safe_text(f"room_id: {room_id}\nlogin: failed\nerror: {exc}"))
            return 1
        ws_url, token = get_danmaku_info(
            room_id,
            timeout=args.timeout,
            force_default_ws=args.bili_default_ws or not args.bili_host_ws,
            use_cookie=not auth.anonymous,
        )
        lines = [
            f"room_id: {room_id}",
            f"login: {'ok' if login.get('ok') else 'not logged in'}; cookie_source={login.get('cookie_loaded_from') or 'env'}",
            f"auth: {auth.summary()}",
            f"websocket: {ws_url}",
            f"danmaku token: {'ok' if token else 'missing'}",
        ]
        if login.get("ok") and login.get("mid") and auth.uid and login.get("mid") != auth.uid:
            lines.append("warning: cookie login uid and websocket uid do not match.")
        if not auth.anonymous and (not auth.uid or not auth.buvid):
            lines.append("warning: logged-in auth is missing uid or buvid; websocket may be unstable.")
        if not login.get("ok"):
            lines.append("warning: Bilibili cookie is missing, expired, or not readable.")
        print(console_safe_text("\n".join(lines)))
        return 0 if token and login.get("ok") and (auth.anonymous or (auth.uid and auth.buvid)) else 1

    if args.command == "obs-overlay":
        run_overlay_json(
            paths,
            overlay_json=Path(args.overlay_json),
            interval=args.interval,
            news_limit=args.limit,
            once=args.once,
            duration=args.duration,
        )
        return 0

    if args.command == "overlay-server":
        run_overlay_server(Path(args.overlay_json), host=args.host, port=args.port)
        return 0

    if args.command == "watch":
        run_watch(
            paths,
            interval=args.interval,
            max_files=args.max_files,
            once=args.once,
            duration=args.duration,
            all_files=args.all_files,
            event_limit=args.event_limit,
            event_mode=args.event_mode,
        )
        return 0

    if args.command == "events":
        print(format_events(human_live_events(paths, limit=args.limit, important_only=args.event_mode == "important")))
        return 0

    parser.error(f"Unknown command {args.command}")
    return 2


def console_safe_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
