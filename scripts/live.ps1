param(
    [string]$Room = "",
    [string]$QuestionMode = "",
    [string]$Model = "",
    [int]$AnswerCharLimit = 0,
    [int]$GiftQuestionCost = 0,
    [double]$HistoryInterval = 0,
    [switch]$NoLlm,
    [switch]$Debug,
    [switch]$NoGiftGate,
    [switch]$NoHistoryPoll,
    [switch]$WebsocketDanmaku,
    [switch]$BiliHostWs
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($Line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $Trimmed = $Line.Trim()
        if (-not $Trimmed -or $Trimmed.StartsWith("#") -or -not $Trimmed.Contains("=")) {
            continue
        }

        if ($Trimmed.StartsWith("export ")) {
            $Trimmed = $Trimmed.Substring(7).Trim()
        }

        $Parts = $Trimmed.Split("=", 2)
        $Name = $Parts[0].Trim()
        $Value = $Parts[1].Trim()

        if (-not $Name) {
            continue
        }

        if (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or ($Value.StartsWith("'") -and $Value.EndsWith("'"))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Pick-Value {
    param(
        [string]$ArgValue,
        [string]$EnvName,
        [string]$Fallback
    )

    if ($ArgValue) {
        return $ArgValue
    }

    $EnvValue = [Environment]::GetEnvironmentVariable($EnvName, "Process")
    if ($EnvValue) {
        return $EnvValue
    }

    return $Fallback
}

function Test-EnvTrue {
    param([string]$EnvName)

    $Value = [Environment]::GetEnvironmentVariable($EnvName, "Process")
    return $Value -match "^(1|true|yes|on)$"
}

foreach ($Name in @(
    "BILIBILI_COOKIE",
    "BILIBILI_UID",
    "BILIBILI_BUVID",
    "BILIBILI_AUTO_BUVID",
    "BILIBILI_COOKIE_BROWSER",
    "BILIBILI_DEVTOOLS_URL"
)) {
    [Environment]::SetEnvironmentVariable($Name, $null, "Process")
}

Import-DotEnv ".env"

$RoomValue = Pick-Value $Room "CIV6_BILI_ROOM" "https://live.bilibili.com/8555868"
$QuestionModeValue = Pick-Value $QuestionMode "CIV6_QUESTION_MODE" "bang"
$ModelValue = Pick-Value $Model "OPENAI_MODEL" ""

if ($AnswerCharLimit -le 0) {
    $EnvAnswerLimit = [Environment]::GetEnvironmentVariable("CIV6_ANSWER_CHAR_LIMIT", "Process")
    $AnswerCharLimit = if ($EnvAnswerLimit) { [int]$EnvAnswerLimit } else { 300 }
}

if ($HistoryInterval -le 0) {
    $EnvHistoryInterval = [Environment]::GetEnvironmentVariable("CIV6_HISTORY_INTERVAL", "Process")
    $HistoryInterval = if ($EnvHistoryInterval) { [double]$EnvHistoryInterval } else { 1.0 }
}

if ($GiftQuestionCost -le 0) {
    $EnvGiftQuestionCost = [Environment]::GetEnvironmentVariable("CIV6_GIFT_QUESTION_COST", "Process")
    $GiftQuestionCost = if ($EnvGiftQuestionCost) { [int]$EnvGiftQuestionCost } else { 100 }
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$ArgsList = @(
    "-m", "civ6intel.cli", "bili-obs",
    "--room", $RoomValue,
    "--obs-text", "obs\answer.txt",
    "--overlay-json", "obs\overlay.json",
    "--serve-overlay",
    "--question-mode", $QuestionModeValue,
    "--gift-question-cost", "$GiftQuestionCost",
    "--gift-obs-text", "obs\gifts.txt",
    "--answer-char-limit", "$AnswerCharLimit",
    "--history-interval", "$HistoryInterval"
)

if (-not $NoGiftGate) {
    $ArgsList += "--require-gift-credit"
}

if ($ModelValue) {
    $ArgsList += @("--model", $ModelValue)
}

if ($NoLlm -or (Test-EnvTrue "CIV6_NO_LLM")) {
    $ArgsList += "--no-llm"
}

if ($Debug -or (Test-EnvTrue "CIV6_DEBUG_DANMAKU")) {
    $ArgsList += "--debug-danmaku"
}

$UseWebsocketDanmaku = $WebsocketDanmaku -or (Test-EnvTrue "CIV6_WEBSOCKET_DANMAKU")
$DisableHistoryPoll = $NoHistoryPoll -or (Test-EnvTrue "CIV6_NO_HISTORY_POLL")

if ($DisableHistoryPoll) {
    $ArgsList += "--no-history-poll"
    $UseWebsocketDanmaku = $true
}

if ($UseWebsocketDanmaku) {
    $ArgsList += "--websocket-danmaku"
}

if ($BiliHostWs -or (Test-EnvTrue "CIV6_BILI_HOST_WS")) {
    $ArgsList += "--bili-host-ws"
}

Write-Host "Starting Civ 6 Bilibili OBS bot..."
Write-Host "Room: $RoomValue"
Write-Host "Question mode: $QuestionModeValue"
Write-Host "Gift question cost: $GiftQuestionCost"
Write-Host "Answer limit: $AnswerCharLimit"

& $Python @ArgsList
