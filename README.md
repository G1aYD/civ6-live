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
BILIBILI_COOKIE=buvid3=...; SESSDATA=...
BILIBILI_UID=...
BILIBILI_BUVID=...
CIV6_TEAM_1_NAME=蓝队
CIV6_TEAM_2_NAME=红队
```

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

Point an OBS Browser Source at your HTML file. The HTML will poll the JSON endpoint and update
`meta`, `newsText`, `barrage`, `question`, `answer`, `aiStatus`, and `status`.

Gift-gated questions are supported by matching the danmaku `uid` with recent gift sender `uid`:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --gift-name 辣条 --gift-window 600 --consume-gift
```

Use `--gift-id` when you know the numeric gift id, or `--min-gift-value` to allow any gift at/above a coin value. Super chats are accepted by default because they are already paid messages; add `--no-super-chat` to disable that.
`BILIBILI_COOKIE` is optional, but it can help keep user ids/names available if Bilibili masks anonymous live-message data.

For coin-based paid questions, keep a persistent gift ledger and charge 100 coin per danmaku
question:

```powershell
python -m civ6intel.cli bili-obs --room https://live.bilibili.com/8555868 --overlay-json obs\overlay.json --serve-overlay --require-gift-credit --gift-question-cost 100 --gift-obs-text obs\gifts.txt
```

This writes every gift to `obs\gifts.jsonl`, user totals/spent balance to `obs\gift_totals.json`,
and an OBS-friendly status file to `obs\gifts.txt`. Add `obs\gifts.txt` as a separate OBS Text
source with "Read from file" enabled.

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
