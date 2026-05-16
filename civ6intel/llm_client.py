from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MAX_OUTPUT_TOKENS = 900
DEFAULT_OPENAI_REASONING_EFFORT = "low"


class LLMError(RuntimeError):
    """Raised when the LLM request cannot be completed."""


@dataclass(frozen=True)
class LLMAnswer:
    text: str
    model: str
    response_id: str | None = None


def ask_openai(
    prompt: str,
    question: str,
    *,
    model: str | None = None,
    env_file: str | Path = ".env",
    timeout: float = 60.0,
) -> LLMAnswer:
    load_env_file(env_file)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set. Add it to .env or your shell environment.")

    selected_model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/")
    payload = {
        "model": selected_model,
        "store": False,
        "instructions": (
            "你是文明 6 直播问答助手。默认用中文回答。"
            "回答必须是纯文本，不使用 Markdown、标题、列表、代码块或 **加粗** 标记。"
            "回答尽量控制在 180 个中文字符以内，先给结论，再给必要理由。"
            "优先使用 *_zh、display_zh、turn_label 字段。"
            "不要把中文和英文专名并列成中英双语；没有中文名时才保留英文原文。"
            "文明、领袖、城邦、奇观、伟人、万神殿、宗教、单位、建筑、科技、市政等专名都尽量使用通行中文译名。"
            "称呼玩家时必须优先使用文明名（玩家ID/昵称），例如 韩国（QFENG），不要写 P0、P1、P2 等槽位编号；除非问题明确问领袖，否则不要附带领袖名。"
            "回合格式使用“32T”，不要写“T32”。"
            "以输入中的游戏上下文为主要依据回答；允许结合文明 6 常识和 BBG 规则做合理推断，给出更像直播解说的判断。"
            "不要反复强调“根据日志”“日志未提供”等技术来源；只有关键精确数据确实缺失时，才简短说明“当前信息不足”。"
            "如果观众询问文明、领袖、总督、单位、奇观、宗教或 BBG 规则/能力细节，可以参考 https://civ6bbg.github.io/ 的 BBG 资料；"
            "如果不能确认版本或页面内容，就说明需要核对 BBG 页面。"
        ),
        "input": f"{prompt}\n\n观众问题：{question}",
        "max_output_tokens": env_int("OPENAI_MAX_OUTPUT_TOKENS", DEFAULT_OPENAI_MAX_OUTPUT_TOKENS),
    }
    add_reasoning_config(payload, selected_model)
    using_bbg_web_search = add_bbg_web_search_tool(payload, question)

    data = request_openai_response(
        base_url,
        api_key,
        payload,
        timeout,
        allow_bbg_retry=using_bbg_web_search,
    )
    text = extract_response_text(data)
    if not text and using_bbg_web_search and "tools" in payload:
        data = retry_without_bbg_web_search(base_url, api_key, payload, timeout)
        text = extract_response_text(data)
    if not text:
        raise LLMError(f"OpenAI API returned no output text ({summarize_empty_response(data)}).")
    return LLMAnswer(
        text=text,
        model=str(data.get("model") or selected_model),
        response_id=data.get("id"),
    )


def request_openai_response(
    base_url: str,
    api_key: str,
    payload: dict,
    timeout: float,
    *,
    allow_bbg_retry: bool,
) -> dict:
    try:
        return post_response(base_url, api_key, payload, timeout)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if allow_bbg_retry:
            return retry_without_bbg_web_search(base_url, api_key, payload, timeout)
        raise LLMError(f"OpenAI API error {exc.code}: {extract_error_message(body)}") from exc
    except URLError as exc:
        raise LLMError(f"Could not reach OpenAI API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError("OpenAI API request timed out.") from exc


def retry_without_bbg_web_search(base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    payload["instructions"] += (
        "BBG 网页检索工具当前不可用或未产出最终文本时，仍需基于已有上下文和常识给出判断；"
        "不要反复强调日志或工具限制。无法确认的关键规则细节才简短说明需要核对 BBG 页面。"
    )
    try:
        return post_response(base_url, api_key, payload, timeout)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"OpenAI API error {exc.code}: {extract_error_message(body)}") from exc
    except URLError as exc:
        raise LLMError(f"Could not reach OpenAI API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError("OpenAI API request timed out.") from exc


def post_response(base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
    request = Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise LLMError("OpenAI API returned an unexpected response.")
    return data


def load_env_file(env_file: str | Path = ".env") -> None:
    path = Path(env_file)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = parse_env_value(value)


def parse_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = value.replace(r"\"", '"').replace(r"\n", "\n")
        return value
    comment_index = value.find(" #")
    if comment_index >= 0:
        value = value[:comment_index]
    return value.strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def add_reasoning_config(payload: dict, model: str) -> None:
    if not model_supports_reasoning_config(model):
        return
    effort = os.environ.get("OPENAI_REASONING_EFFORT", DEFAULT_OPENAI_REASONING_EFFORT).strip().lower()
    if effort in {"", "0", "false", "no", "off"}:
        return
    payload["reasoning"] = {"effort": effort}


def model_supports_reasoning_config(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def add_bbg_web_search_tool(payload: dict, question: str) -> bool:
    if os.environ.get("OPENAI_BBG_WEB_SEARCH", "1").lower() in {"0", "false", "no", "off"}:
        return False
    if not looks_like_bbg_detail_question(question):
        return False
    payload["tools"] = [
        {
            "type": "web_search",
            "filters": {
                "allowed_domains": ["civ6bbg.github.io"],
            },
        }
    ]
    payload["tool_choice"] = "auto"
    return True


def looks_like_bbg_detail_question(question: str) -> bool:
    text = question.lower()
    hints = (
        "bbg",
        "能力",
        "技能",
        "特性",
        "特色",
        "效果",
        "加成",
        "机制",
        "有什么用",
        "是什么",
        "改动",
        "总督",
        "城邦",
        "城邦能力",
        "宗主",
        "科邦",
        "文邦",
        "军邦",
        "工邦",
        "商邦",
        "宗邦",
        "文明能力",
        "领袖能力",
        "governor",
        "city state",
        "city-state",
        "suzerain",
        "ability",
        "bonus",
        "leader ability",
        "civ ability",
    )
    return any(hint in text for hint in hints)


def extract_response_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    collect_response_text(data.get("output"), parts)
    if not parts:
        collect_response_text(data.get("message"), parts)
    return "\n".join(parts).strip()


def collect_response_text(value: object, parts: list[str]) -> None:
    if isinstance(value, str):
        if value.strip():
            parts.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            collect_response_text(item, parts)
        return
    if not isinstance(value, dict):
        return

    for key in ("output_text", "text", "refusal"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())

    for key in ("content", "message", "output"):
        child = value.get(key)
        if child is not None:
            collect_response_text(child, parts)


def summarize_empty_response(data: object) -> str:
    if not isinstance(data, dict):
        return f"unexpected response type {type(data).__name__}"

    pieces: list[str] = []
    for key in ("status", "model", "id"):
        value = data.get(key)
        if value:
            pieces.append(f"{key}={value}")

    incomplete = data.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = incomplete.get("reason") or incomplete.get("message")
        if reason:
            pieces.append(f"incomplete={reason}")

    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or error.get("type")
        if message:
            pieces.append(f"error={message}")

    output = data.get("output")
    if isinstance(output, list):
        output_items = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type") or item.get("role") or "unknown"
            item_status = item.get("status")
            output_items.append(f"{item_type}:{item_status}" if item_status else str(item_type))
        if output_items:
            pieces.append(f"output={','.join(output_items[:8])}")

    return "; ".join(pieces) if pieces else "no message content found"


def extract_error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip() or "unknown error"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return body.strip() or "unknown error"
