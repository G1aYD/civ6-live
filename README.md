# Civ 6 Interaction

Local parser for Civilization VI live logs. The goal is to turn game files into compact, reliable state that can be used by deterministic queries or passed to an LLM.

## Current Data Sources

- `GameCore.log`: player slot, civilization, leader.
- `Player_Stats.csv`: per-turn cities, population, techs, civics, unit counts, balances, and yields.
- `Player_Stats_2.csv`: per-turn tourism, diplomatic victory points, favor, CO2.
- `Game_PlayerScores.csv`: per-turn score categories.

The paths are configured in `config.json`; no live game folders are linked into the repo.

## Try It

```powershell
$env:PYENV_VERSION='3.11.9'
python -m civ6intel.cli ask "Who has the most production on turn 20?"
python -m civ6intel.cli ask "What is current culture victory progress?"
python -m civ6intel.cli llm-ask "现在谁的文化胜利进度最好？"
python -m civ6intel.cli context --turn latest
python -m civ6intel.cli llm-context --format prompt
python -m civ6intel.cli watch
python -m civ6intel.cli events
python -m civ6intel.cli inspect-logs test_logs
```

To verify live updates without waiting forever:

```powershell
python -m civ6intel.cli watch --once
```

The watcher polls only high-signal files by default: autosaves, player stats, score files, build queue, game identity, selected event CSVs, `net_message_debug.log`, `AStar_APP.log`, and `UnitOperations.log`. When one changes, it prints the changed path list, newly parsed human events if available, then reloads the parser.

The `Intel` section adds compact live context for:

- Victory proxies from score/stats logs.
- Great people currently on the timeline.
- Wonder/build queue rows when `City_BuildQueue.csv` is current.
- Human diplomacy/deal signals, including influence and future gold/deal patterns.

For a richer LLM input, use:

```powershell
python -m civ6intel.cli llm-context --format prompt
```

This emits Chinese answering instructions plus grounded JSON covering player names, current victory
proxies, pantheons, gold sent totals, completed wonders, great people, and recent important events.
LLM responses should prefer Chinese name fields and turn labels such as `32T`.

To ask the configured OpenAI model directly:

```powershell
python -m civ6intel.cli llm-ask "现在谁奇观最多？"
```

The API key is loaded from `.env` or the shell environment. Optional `.env` fields:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.5
OPENAI_MAX_OUTPUT_TOKENS=900
OPENAI_REASONING_EFFORT=low
OPENAI_PROMPT_CACHE_KEY=civ6-live
OPENAI_LOG_USAGE=false
BILIBILI_COOKIE_SESSDATA=...
BILIBILI_COOKIE_BILI_JCT=...
BILIBILI_COOKIE_DEDEUSERID=...
BILIBILI_COOKIE_DEDEUSERID_CKMD5=...
BILIBILI_COOKIE_BUVID3=...
BILIBILI_COOKIE_BUVID4=...
BILIBILI_COOKIE_LIVE_BUVID=...
BILIBILI_COOKIE_SOURCE=env
BILIBILI_REQUIRE_LOGIN=true
BILIBILI_WS_ANON=false
BILIBILI_ANON_FALLBACK=false
BILIBILI_PROTO_VER=2
CIV6_TEAM_1_NAME=蓝队
CIV6_TEAM_2_NAME=红队
CIV6_BILI_ROOM=https://live.bilibili.com/8555868
CIV6_QUESTION_MODE=bang
CIV6_ANSWER_CHAR_LIMIT=300
CIV6_GIFT_QUESTION_COST=100
CIV6_HISTORY_INTERVAL=1
CIV6_NO_HISTORY_POLL=false
CIV6_WEBSOCKET_DANMAKU=false
```

Prompt caching is automatic for long OpenAI prompts. Keep `OPENAI_PROMPT_CACHE_KEY` stable for one
stream, and set `OPENAI_LOG_USAGE=true` temporarily to print `cached_tokens` after each API response.

For normal live streaming, use the preset PowerShell launcher instead of passing every argument:

```powershell
.\scripts\live.ps1
```

Useful temporary overrides:

```powershell
.\scripts\live.ps1 -Room https://live.bilibili.com/8555868 -Debug
.\scripts\live.ps1 -NoLlm -NoGiftGate
```

The preset enables the OBS browser overlay, gift-credit question charging, `!`/`！` question mode,
and a 300-character answer limit. Edit `.env` for values you reuse often.

To listen to Bilibili live danmaku and update an OBS text source:

```powershell
python -m pip install -r requirements.txt
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --obs-text obs\answer.txt
```

Normal danmaku questions use Bilibili's recent danmaku history endpoint by default because anonymous
WebSocket `DANMU_MSG` payloads can mask usernames and may only include a subset of messages. The
WebSocket is still used for gifts, super chats, and live room events. For diagnostics:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --question-mode any --no-llm --debug-danmaku
```

Use `--websocket-danmaku` only if lower latency matters more than complete usernames. Use
`--no-history-poll` to disable the history fallback entirely.

If BUVID/cookie makes WebSocket danmaku usernames reliable, disable history polling to reduce
background polling:

```text
CIV6_NO_HISTORY_POLL=true
```

The preset launcher will automatically enable WebSocket danmaku when history polling is disabled.

To check the Bilibili auth path without printing secrets:

```powershell
python -m civ6intel.cli bili-check
```

For production, `BILIBILI_COOKIE_SOURCE=env` is the quietest setup: paste the important logged-in
Bilibili cookie values into the split `BILIBILI_COOKIE_*` fields in `.env`, keep
`BILIBILI_REQUIRE_LOGIN=true`, and keep history polling enabled so normal danmaku questions come
from logged-in history polling with full uid/usernames. The important browser cookie names are
`SESSDATA`, `bili_jct`, `DedeUserID`, `DedeUserID__ckMd5`, `buvid3`, `buvid4`, and `LIVE_BUVID`.

If you prefer a no-copy setup later, launch a dedicated login browser:

```powershell
.\scripts\bili_login_browser.ps1
```

Log in to Bilibili in that browser window, keep it open while streaming, and set
`BILIBILI_COOKIE_SOURCE=devtools`. The tool will read the live browser cookie through
`BILIBILI_DEVTOOLS_URL` without printing it. `BILIBILI_COOKIE_SOURCE=browser` is also supported
for direct Chrome/Edge/Firefox cookie extraction, but Chromium cookie encryption can be less stable
on some Windows installs.

When the split cookie fields are set, the websocket uses logged-in auth by default. If no buvid is
available, the tool falls back to Bilibili's public buvid endpoint, but full login still requires
valid `SESSDATA`.
Some large public rooms reject logged-in websocket auth but allow anonymous auth. In that case, the
tool automatically falls back to anonymous websocket for 60 seconds; set `BILIBILI_ANON_FALLBACK=false`
to disable this diagnostic fallback.
For paid-question production, keep `BILIBILI_ANON_FALLBACK=false`; otherwise anonymous fallback can
produce masked usernames and `uid=0` in websocket diagnostics.

In OBS, add a Text source, enable "Read from file", and select `C:\Git\civ6interaction\obs\answer.txt`.

For a separate scrolling game-news ticker, add another Text source that reads `obs\news.txt`,
then add OBS's Scroll filter to that source:

```powershell
python -m civ6intel.cli obs-news --obs-text obs\news.txt
```

The ticker includes pantheon choices, great people taken, completed wonders, and gold transfers. You can
also run it together with the Bilibili listener:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --obs-text obs\answer.txt --news-text obs\news.txt
```

For the browser overlay HTML, set its `DATA_URL` to:

```js
const DATA_URL = "http://127.0.0.1:8787/overlay.json";
```

Then run the Bilibili listener with JSON output and a local JSON server:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --overlay-json obs\overlay.json --serve-overlay
```

For an overlay-only test without Bilibili:

```powershell
python -m civ6intel.cli obs-overlay --overlay-json obs\overlay.json
python -m civ6intel.cli overlay-server --overlay-json obs\overlay.json
```

Before a new game, verify that the current players map to existing BBG leader images:

```powershell
python -m civ6intel.cli check-icons
```

Use `--all` to check every human-playable leader in Civ VI's merged Base/DLC/Mod configuration, or
`--offline` to print the computed image filenames without calling GitHub.

Point an OBS Browser Source at your HTML file. The HTML will poll the JSON endpoint and update
`meta`, `newsText`, `barrage`, `question`, `answer`, `aiStatus`, and `status`.

Gift-gated questions are supported by matching the danmaku `uid` with recent gift sender `uid`:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --gift-name 辣条 --gift-window 600 --consume-gift
```

Use `--gift-id` when you know the numeric gift id, or `--min-gift-value` to allow any gift at/above a coin value. Super chats are accepted by default and are also added to the persistent gift ledger; add `--no-super-chat` to disable that.
`BILIBILI_COOKIE` is optional, but it can help keep user ids/names available if Bilibili masks anonymous live-message data.

For coin-based paid questions, keep a persistent gift ledger and charge the configured coin value per danmaku
question. Set `CIV6_GIFT_QUESTION_COST` in `.env` or pass `--gift-question-cost`:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --overlay-json obs\overlay.json --serve-overlay --require-gift-credit --gift-question-cost 100 --gift-obs-text obs\gifts.txt
```

This writes every gift and super chat to `obs\gifts.jsonl`, user totals/spent balance to
`obs\gift_totals.json`, and an OBS-friendly status file to `obs\gifts.txt`. The status file shows
the current conversion, such as `1问 = 5电池`, and recalculates remaining questions from the
configured cost. Super chat `price` is converted to internal coin value with
`CIV6_SUPER_CHAT_COIN_MULTIPLIER`, which defaults to `100`.
Add `obs\gifts.txt` as a separate OBS Text source with "Read from file" enabled.
The logged-in Bilibili account is allowed to ask questions without spending gift balance. Add extra
free users with `CIV6_FREE_QUESTION_UIDS=uid1,uid2` in `.env`.

For a bounded live test during a turn transition:

```powershell
python -m civ6intel.cli watch --duration 120
```

To investigate noisy startup/cache/UI logs too:

```powershell
python -m civ6intel.cli watch --all-files
```

For within-turn actions, `events` reads high-signal live logs:

- `net_message_debug.log`: unit operations, found-city operations, research/civic selections.
- `AStar_APP.log`: local-player unit movement coordinates.

For ended-game log folders, `inspect-logs` scans one `Logs` directory or a parent folder containing
`Logs_*` fixtures. It focuses on high-signal archive events: pantheon choices, great person claim/take
rows, wonder score/build evidence, and useful deal signals such as gold transfers.

## Design

The parser produces structured state first, then the query layer answers questions from that state. For LLM use, prefer sending the grounded prompt from `llm-context` instead of raw CSV/log files. That keeps answers grounded and makes hallucinations easier to catch.
