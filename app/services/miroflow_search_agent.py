# SPDX-FileCopyrightText: 2026 LongCat
#
# SPDX-License-Identifier: Apache-2.0
#
# Adapted from MiroFlow's sub-agent prompt/orchestrator/parsing flow
# (Apache-2.0) and aligned with MindSearch-style search->select page reading.

from __future__ import annotations

import concurrent.futures
import datetime
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import json5  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    json5 = None


LOGGER = logging.getLogger(__name__)


def _smart_json_truncate(json_str: str) -> str:
    if not json_str:
        return json_str
    start = 0
    while start < len(json_str) and json_str[start].isspace():
        start += 1
    if start >= len(json_str):
        return json_str
    first_char = json_str[start]
    if first_char not in ("{", "["):
        return json_str
    open_char = "{" if first_char == "{" else "["
    close_char = "}" if first_char == "{" else "]"
    depth = 0
    in_string = False
    escape_next = False
    for index in range(start, len(json_str)):
        ch = json_str[index]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return json_str[: index + 1]
    return json_str


def _fix_unterminated_string_values(json_str: str) -> str:
    try:
        pattern = re.compile(r'"(?:[^"\\]|\\.)*"\s*:\s*"', re.DOTALL)
        for match in pattern.finditer(json_str):
            value_start = match.end()
            index = value_start
            escaped = False
            closed = False
            while index < len(json_str):
                ch = json_str[index]
                if escaped:
                    escaped = False
                    index += 1
                    continue
                if ch == "\\":
                    escaped = True
                    index += 1
                    continue
                if ch == '"':
                    next_index = index + 1
                    while next_index < len(json_str) and json_str[next_index].isspace():
                        next_index += 1
                    if next_index >= len(json_str) or json_str[next_index] in (",", "}", "]"):
                        closed = True
                        break
                index += 1
            if not closed:
                end = len(json_str) - 1
                while end >= 0 and json_str[end].isspace():
                    end -= 1
                if end >= 0 and json_str[end] in ("}", "]"):
                    fixed = json_str[:end] + '"' + json_str[end:]
                    open_curly = close_curly = open_square = close_square = 0
                    in_str = False
                    esc = False
                    for ch in fixed:
                        if esc:
                            esc = False
                            continue
                        if ch == "\\":
                            esc = True
                            continue
                        if ch == '"':
                            in_str = not in_str
                            continue
                        if in_str:
                            continue
                        if ch == "{":
                            open_curly += 1
                        elif ch == "}":
                            close_curly += 1
                        elif ch == "[":
                            open_square += 1
                        elif ch == "]":
                            close_square += 1
                    if open_curly > close_curly:
                        fixed += "}" * (open_curly - close_curly)
                    if open_square > close_square:
                        fixed += "]" * (open_square - close_square)
                    return fixed
        return json_str
    except Exception:
        return json_str


def preprocess_json_string(json_str: str) -> str:
    if not json_str or not isinstance(json_str, str):
        return json_str
    return _smart_json_truncate(_fix_unterminated_string_values(json_str))


def robust_json_loads(json_str: str, apply_preprocessing: bool = True) -> Any:
    if apply_preprocessing:
        json_str = preprocess_json_string(json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as error:
        if json5 is not None:
            try:
                return json5.loads(json_str)
            except Exception:
                pass
        raise error


def parse_llm_response_for_tool_calls(response_text: Any) -> Tuple[List[dict], List[dict]]:
    tool_calls: List[dict] = []
    bad_tool_calls: List[dict] = []
    if not isinstance(response_text, str):
        return tool_calls, bad_tool_calls

    tool_call_patterns = re.findall(
        r"<use_mcp_tool[^>]*?>\s*<server_name[^>]*?>(.*?)</server_name>\s*<tool_name[^>]*?>(.*?)</tool_name>\s*<arguments[^>]*?>\s*([\s\S]*?)\s*</arguments>\s*</use_mcp_tool>",
        response_text,
        re.DOTALL | re.IGNORECASE,
    )
    for pattern in (
        r"<use_mcp_tool[^>]*?>(?:(?!</use_mcp_tool>).)*?(?:</use_mcp_tool>|$)",
        r"<server_name[^>]*?>(?:(?!</server_name>).)*?(?:</server_name>|$)",
        r"<tool_name[^>]*?>(?:(?!</tool_name>).)*?(?:</tool_name>|$)",
        r"<arguments[^>]*?>(?:(?!</arguments>).)*?(?:</arguments>|$)",
    ):
        for match in re.findall(pattern, response_text, re.DOTALL | re.IGNORECASE):
            if pattern.endswith("</server_name>|$)") and not re.search(r"</server_name>\s*$", match, re.IGNORECASE):
                bad_tool_calls.append({"error": "Unclosed server_name tag", "content": match})
            elif pattern.endswith("</tool_name>|$)") and not re.search(r"</tool_name>\s*$", match, re.IGNORECASE):
                bad_tool_calls.append({"error": "Unclosed tool_name tag", "content": match})
            elif pattern.endswith("</arguments>|$)") and not re.search(r"</arguments>\s*$", match, re.IGNORECASE):
                bad_tool_calls.append({"error": "Unclosed arguments tag", "content": match})
            elif pattern.endswith("</use_mcp_tool>|$)") and not re.search(r"</use_mcp_tool>\s*$", match, re.IGNORECASE):
                bad_tool_calls.append({"error": "Unclosed use_mcp_tool tag", "content": match})

    for server_name, tool_name, arguments_str in tool_call_patterns:
        server_name = server_name.strip()
        tool_name = tool_name.strip()
        arguments_str = arguments_str.strip()
        try:
            arguments = robust_json_loads(arguments_str)
        except json.JSONDecodeError:
            try:
                arguments = robust_json_loads(
                    arguments_str.replace("'", '"')
                    .replace("None", "null")
                    .replace("True", "true")
                    .replace("False", "false")
                )
            except json.JSONDecodeError:
                arguments = {"error": "Failed to parse arguments", "raw": arguments_str}
        tool_calls.append(
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "id": None,
            }
        )
    return tool_calls, bad_tool_calls


def strip_tool_markup(text: str) -> str:
    cleaned = re.sub(
        r"<use_mcp_tool[^>]*?>[\s\S]*?</use_mcp_tool>",
        "",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


class MiroFlowSearchPrompt:
    def generate_system_prompt_with_mcp_tools(
        self, mcp_servers: List[dict], chinese_context: bool = False
    ) -> str:
        formatted_date = datetime.datetime.today().strftime("%Y-%m-%d")
        prompt = f"""In this environment you have access to a set of tools you can use to answer the user's question.

You only have access to the tools provided below. You can only use one tool per message, and will receive the result of that tool in the user's next response. You use tools step-by-step to accomplish a given task, with each tool-use informed by the result of the previous tool-use. Today is: {formatted_date}

# Tool-Use Formatting Instructions

Tool-use is formatted using XML-style tags. The tool-use is enclosed in <use_mcp_tool></use_mcp_tool> and each parameter is similarly enclosed within its own set of tags.

Description:
Request to use a tool provided by a MCP server. Each server can provide multiple tools with different capabilities.

Usage:
<use_mcp_tool>
<server_name>server name here</server_name>
<tool_name>tool name here</tool_name>
<arguments>
{{
  "param1": "value1"
}}
</arguments>
</use_mcp_tool>

Here are the functions available in JSONSchema format:

"""
        for server in mcp_servers:
            prompt += f"## Server name: {server['name']}\n"
            for tool in server.get("tools") or []:
                if "error" in tool and "name" not in tool:
                    continue
                prompt += f"### Tool name: {tool['name']}\n"
                prompt += f"Description: {tool['description']}\n"
                prompt += f"Input JSON schema: {json.dumps(tool['schema'], ensure_ascii=False)}\n"

        prompt += """
# General Objective

You accomplish the task iteratively, breaking it down into clear steps and working through them methodically.

## Task Strategy

1. Analyze the request and set clear, achievable sub-goals.
2. Start with a concise numbered plan before taking action.
3. Work through the sub-goals sequentially. After each step, carefully extract all useful information from the tool result before proceeding.
4. Revise your plan when new information appears.

## Tool-Use Guidelines

1. IMPORTANT: Each step must involve exactly ONE tool call only, unless the task is already solved.
2. Before each tool call:
   - Briefly summarize what is currently known.
   - Identify what is missing, uncertain, or unreliable.
   - Choose the most relevant tool for the current sub-goal.
3. All tool queries must include full, self-contained context.
4. Avoid broad, vague, or speculative queries.
5. Thoroughly extract useful details from every tool result.

## Search-Specific Guidance

1. Call dual_search first to get search results with titles, URLs, and snippets.
2. After receiving search results, **evaluate whether the snippets alone are sufficient** to answer the query:
   - For simple factual questions (dates, names, short answers), snippets may be enough — call evaluate_evidence_pack to confirm, then answer directly.
   - For complex, multi-faceted, or detail-heavy questions, select the most promising URLs and call fetch_webpage with a list of URLs to read their full content concurrently.
3. fetch_webpage accepts a single URL string OR an array of URL strings. When you need to read multiple pages, **always pass them as an array in one call** for concurrent fetching — do NOT call fetch_webpage multiple times sequentially.
4. Favor official docs, authoritative media, primary sources, and technical repositories over low-quality aggregators.
5. After fetching pages, call evaluate_evidence_pack to check if evidence is sufficient before producing the final answer.
6. Do not claim facts unless they are supported by the snippets or pages you actually read.

## Tool-Use Communication Rules

1. After issuing exactly ONE tool call, stop immediately.
2. Do not present the final answer until the task is complete.
3. Do not mention tool names in the final answer.
4. Unless otherwise requested, respond in the same language as the user.
"""
        if chinese_context:
            prompt += """

## 中文语境处理指导

1. 搜索关键词优先使用中文，但当主题明显是英文产品、英文论文、英文仓库时，可混用英文关键词。
2. 所有分析、过程说明、中间判断和最终回答都使用中文。
3. 搜索时优先保留原始中文信息，不要无谓翻译。
4. 对时效性事实、参数、发布信息、技术文档、仓库实现，必须基于抓取到的网页正文回答。
5. 简单事实性问题（如日期、人名、简短定义），如果搜索摘要已经足够回答，可以不抓取正文直接回答。
"""
        prompt += """

# Agent Specific Objective

You are a focused search worker. Your task is to gather evidence, judge evidence quality, and then produce a precise answer grounded in search snippets or fetched pages.
- For simple queries: search snippets may suffice. Evaluate and answer quickly.
- For complex queries: fetch multiple pages concurrently via fetch_webpage(url=[url1, url2, ...]), then evaluate and answer.
If evidence is incomplete or conflicting, say so clearly.
"""
        return prompt

    def generate_summarize_prompt(
        self,
        task_description: str,
        task_failed: bool = False,
        chinese_context: bool = False,
    ) -> str:
        prompt = (
            "This is a direct instruction to you, not the result of a tool call.\n\n"
            + (
                "Important: you either exhausted the turn budget or failed to reach a conclusive answer, so you must explicitly mention that the task is incomplete.\n\n"
                if task_failed
                else ""
            )
            + "We are ending this search-worker session. You must NOT initiate any further tool use.\n\n"
            + "Please produce the FINAL ANSWER to the original task, using only the evidence collected in this session.\n"
            + "If the evidence is partial or conflicting, keep the uncertainty explicit.\n"
            + "The original task is:\n\n"
            + f"---\n{task_description}\n---\n\n"
            + "Requirements:\n"
            + "- Give a direct final answer first.\n"
            + "- Then provide a compact evidence summary grounded in fetched pages.\n"
            + "- Mention the main sources or domains that support the answer.\n"
            + "- Do not call tools.\n"
        )
        if chinese_context:
            prompt += "\n请使用中文输出。\n"
        return prompt


@dataclass
class MiroFlowSearchAgentConfig:
    max_turns: int = 10
    max_tool_calls_per_turn: int = 1
    max_parallel_fetches: int = 3
    default_fetch_budget: int = 3
    chinese_context: bool = True
    page_evidence_max_chars: int = 12000
    page_raw_max_chars: int = 60000
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2400
    llm_timeout_seconds: int = 60


@dataclass
class MiroFlowSearchAgentHooks:
    longcat_request: Callable[[dict, str, int], Any]
    default_api_key: Callable[[], str]
    dual_search: Callable[..., Tuple[List[dict], str, dict]]
    fetch_clean_page: Callable[[str, Optional[float]], dict]
    build_candidate_pool: Callable[[str, dict, List[dict]], List[dict]]
    candidate_pool_summary: Callable[[List[dict], List[dict]], dict]
    build_document_capsules: Callable[[str, dict, List[dict], str], List[dict]]
    build_evidence_matrix: Callable[[str, dict, List[dict], List[dict]], List[dict]]
    build_compressed_evidence_brief: Callable[[str, dict, List[dict], List[dict], str], str]
    evaluate_search_sufficiency: Callable[[str, str, int, int, str], Optional[dict]]
    search_stop_state: Callable[[dict, dict, str, List[dict], float], Tuple[bool, str]]
    build_document_source_evidence: Callable[[List[dict]], List[dict]]
    clean_documents_for_output: Callable[[List[dict]], List[dict]]
    page_evidence_text: Callable[[str, str, Optional[dict], Optional[int]], str]
    url_domain: Callable[[str], str]
    compact_text: Callable[[str, int], str]
    goal_profile: Callable[[str], dict]
    search_depth: Callable[[Optional[str], Optional[dict]], str]
    budget_for_profile: Callable[[str, dict], dict]
    select_fetch_candidates: Callable[[List[dict], List[dict], Optional[set]], List[dict]]


class MiroFlowSearchSubAgent:
    MAX_TURNS = 10

    @staticmethod
    def tool_compat_definitions() -> List[dict]:
        return [
            {"type": "function", "function": {"name": "dual_search"}},
            {"type": "function", "function": {"name": "select_search_results"}},
            {"type": "function", "function": {"name": "fetch_webpage"}},
            {"type": "function", "function": {"name": "evaluate_evidence_pack"}},
        ]

    def __init__(
        self,
        goal: str,
        hooks: MiroFlowSearchAgentHooks,
        config: Optional[MiroFlowSearchAgentConfig] = None,
        api_key: str = "",
        event_queue: Any = None,
    ):
        self.goal = str(goal or "").strip()
        self.hooks = hooks
        self.config = config or MiroFlowSearchAgentConfig()
        self.api_key = api_key or hooks.default_api_key()
        self.event_queue = event_queue
        self.prompt = MiroFlowSearchPrompt()
        self.profile = self.hooks.goal_profile(self.goal)
        self.depth = self.hooks.search_depth(
            self.profile.get("expectedDepth") or self.profile.get("expected_depth"),
            self.profile,
        )
        self.budget = self.hooks.budget_for_profile(self.depth, self.profile)
        self.system_prompt = self.prompt.generate_system_prompt_with_mcp_tools(
            mcp_servers=self._tool_server_definitions(),
            chinese_context=self.config.chinese_context,
        )
        self.message_history: List[dict] = [
            {"role": "user", "content": self.goal},
        ]
        self.refs_all: List[dict] = []
        self.pages: List[dict] = []
        self.fetch_trace: List[dict] = []
        self.queries_tried: List[dict] = []
        self.answers: List[dict] = []
        self.search_evidence: List[dict] = []
        self.trace: List[dict] = []
        self.document_capsules: List[dict] = []
        self.evidence_matrix: List[dict] = []
        self.compressed_evidence: str = ""
        self.current_candidate_pool: List[dict] = []
        self.current_candidate_summary: dict = {}
        self.latest_results_by_id: Dict[int, dict] = {}
        self.attempted_urls: set[str] = set()
        self.last_quality_gate: dict = {}
        self.last_stop_reason: str = "not_started"
        self.last_direct_answer: str = ""
        self.turn_count = 0

    def _tool_server_definitions(self) -> List[dict]:
        return [
            {
                "name": "searching-mcp-server",
                "tools": [
                    {
                        "name": "dual_search",
                        "description": "Run providerized web search, return ranked result IDs, and preserve search snippets as search evidence for selection and final synthesis.",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "num_results": {"type": "integer"},
                                "reason": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "select_search_results",
                        "description": "MindSearch-style selection step. Use result IDs from the latest dual_search call, choose pages based on snippets and task needs, fetch only the most promising pages, and return page previews.",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "select_ids": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["select_ids"],
                        },
                    },
                    {
                        "name": "fetch_webpage",
                        "description": "Fetch web content from a specific URL or a list of URLs concurrently.",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "anyOf": [
                                        {"type": "string", "description": "A single webpage URL"},
                                        {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "A list of URLs to fetch concurrently"
                                        }
                                    ]
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["url"],
                        },
                    },
                    {
                        "name": "evaluate_evidence_pack",
                        "description": "Evaluate whether the currently fetched pages are sufficient, using quality gating and evidence compression.",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "assessment": {"type": "string"},
                            },
                            "required": ["assessment"],
                        },
                    },
                ],
            }
        ]

    def _emit_trace(self, event_name: str, payload: dict) -> None:
        self.trace.append({"event": event_name, "payload": payload or {}})
        runtime = None
        try:
            runtime = self.context.get("_session_runtime") if isinstance(getattr(self, "context", None), dict) else None
        except Exception:
            runtime = None
        if runtime is not None and hasattr(runtime, "event_bus"):
            try:
                node = getattr(self, "_op_node", None)
                runtime.event_bus.emit(
                    "subagent.trace",
                    "running",
                    op_id=getattr(node, "op_id", "") or runtime.current_op_id(),
                    parent_op_id=getattr(node, "parent_op_id", "") or runtime.current_op_id(),
                    trajectory_id=getattr(node, "trajectory_id", "") or "",
                    cascade_id=getattr(node, "cascade_id", "") or getattr(runtime, "run_id", ""),
                    lane="subagent",
                    payload={"event": event_name, "trace": payload or {}},
                )
            except Exception:
                pass
        if self.event_queue is not None:
            try:
                self.event_queue.put((event_name, payload or {}))
            except Exception:
                pass

    def _llm_completion(self, messages: List[dict], max_tokens: Optional[int] = None) -> Optional[str]:
        payload = {
            "messages": [{"role": "system", "content": self.system_prompt}] + messages,
            "max_tokens": int(max_tokens or self.config.llm_max_tokens),
            "temperature": float(self.config.llm_temperature),
            "stream": False,
        }
        try:
            upstream = self.hooks.longcat_request(
                payload,
                self.api_key,
                self.config.llm_timeout_seconds,
            )
            raw = upstream.read().decode("utf-8")
            try:
                upstream.close()
            except Exception:
                pass
            data = json.loads(raw)
            return (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except Exception as exc:
            LOGGER.warning("MiroFlowSearchSubAgent LLM call failed: %s", exc)
            self._emit_trace(
                "search_subagent_error",
                {"goal": self.goal, "error": str(exc), "phase": "llm"},
            )
            return None

    def _format_tool_result_for_user(self, result: dict) -> dict:
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if len(text) > 12000:
            text = text[:12000] + "\n...[truncated]"
        return {"type": "text", "text": text}

    def _update_message_history(
        self,
        message_history: List[dict],
        tool_call_info: List[Tuple[str, dict]],
        tool_calls_exceeded: bool = False,
    ) -> List[dict]:
        valid_tool_calls = [(tool_id, content) for tool_id, content in tool_call_info if tool_id != "FAILED"]
        bad_tool_calls = [(tool_id, content) for tool_id, content in tool_call_info if tool_id == "FAILED"]
        output_parts: List[str] = []
        total_calls = len(valid_tool_calls) + len(bad_tool_calls)
        if total_calls > 1:
            if tool_calls_exceeded:
                output_parts.append(
                    f"You made too many tool calls. I can only afford to process {len(valid_tool_calls)} valid tool calls in this turn."
                )
            else:
                output_parts.append(
                    f"I have processed {len(valid_tool_calls)} valid tool calls in this turn."
                )
            for index, (_tool_id, content) in enumerate(valid_tool_calls, 1):
                output_parts.append(f"Valid tool call {index} result:\n{content['text']}")
            for index, (_tool_id, content) in enumerate(bad_tool_calls, 1):
                output_parts.append(f"Failed tool call {index} result:\n{content['text']}")
        else:
            for _tool_id, content in valid_tool_calls:
                output_parts.append(content["text"])
            for _tool_id, content in bad_tool_calls:
                output_parts.append(content["text"])
        message_history.append({"role": "user", "content": "\n\n".join(output_parts)})
        return message_history

    def _normalize_page_record(self, url: str, clean_page: dict, result_meta: Optional[dict] = None) -> Optional[dict]:
        content = str(clean_page.get("text") or "").strip()
        if len(content) <= 120:
            return None
        result_meta = result_meta or {}
        page_url = clean_page.get("url") or url
        return {
            "title": clean_page.get("title") or result_meta.get("title") or page_url,
            "url": page_url,
            "originalUrl": url,
            "canonicalUrl": clean_page.get("canonicalUrl") or page_url,
            "domain": result_meta.get("domain") or self.hooks.url_domain(page_url),
            "sourceType": result_meta.get("sourceType") or "general",
            "content": self.hooks.page_evidence_text(
                content,
                self.goal,
                self.profile,
                self.config.page_evidence_max_chars,
            )[: self.config.page_evidence_max_chars],
            "fullContent": content[: self.config.page_raw_max_chars],
            "rawChars": clean_page.get("rawChars") or len(content),
            "textChars": clean_page.get("textChars") or len(content),
            "fromPage": True,
            "sourceMode": "full_page",
            "fetchStatus": clean_page.get("fetchStatus") or "unknown",
            "cleanMethod": clean_page.get("cleanMethod") or "",
            "cleanScore": clean_page.get("cleanScore") or 0,
            "dateHint": clean_page.get("dateHint") or result_meta.get("dateHint") or "",
            "author": clean_page.get("author") or "",
            "images": clean_page.get("images") or result_meta.get("imageRefs") or [],
            "rank": result_meta.get("rank"),
            "triageScore": result_meta.get("score"),
            "searchEngine": result_meta.get("sourceEngine") or result_meta.get("searchEngine") or "",
        }

    def _tool_dual_search(self, arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        num_results = max(4, min(20, int(arguments.get("num_results") or 10)))
        refs, answer, search_meta = self.hooks.dual_search(
            query,
            use_cache=None,
            merged_limit=num_results,
        )
        self.refs_all.extend(refs)
        self.current_candidate_pool = self.hooks.build_candidate_pool(
            self.goal,
            self.profile,
            self.refs_all,
        )
        self.current_candidate_summary = self.hooks.candidate_pool_summary(
            self.current_candidate_pool,
            self.pages,
        )
        self.latest_results_by_id = {}
        top_results = []
        for index, item in enumerate(self.current_candidate_pool[:num_results]):
            snippet = item.get("snippet") or item.get("abstract") or item.get("content") or ""
            result = {
                "id": index,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "domain": item.get("domain") or "",
                "sourceType": item.get("sourceType") or "",
                "score": item.get("score") or 0,
                "selectionReason": item.get("selectionReason") or "",
                "abstract": self.hooks.compact_text(snippet, 420),
                "sourceEngine": item.get("sourceEngine") or item.get("searchEngine") or "",
                "engineMatches": item.get("engineMatches") or [],
            }
            self.latest_results_by_id[index] = item
            top_results.append(result)
        search_evidence = []
        for index, ref in enumerate(refs[:num_results]):
            snippet = ref.get("abstract") or ref.get("content") or ref.get("snippet") or ""
            search_evidence.append(
                {
                    "id": index,
                    "query": query,
                    "title": ref.get("title") or "",
                    "url": ref.get("url") or "",
                    "domain": ref.get("domain") or self.hooks.url_domain(ref.get("url")),
                    "snippet": self.hooks.compact_text(snippet, 700),
                    "sourceEngine": ref.get("sourceEngine") or ref.get("searchEngine") or "",
                    "engineMatches": ref.get("engineMatches") or [],
                }
            )
        self.search_evidence.extend(search_evidence)
        self.queries_tried.append(
            {
                "query": query,
                "refs": len(refs),
                "validPages": 0,
                "engineMeta": search_meta,
                "searchEvidence": search_evidence,
            }
        )
        if answer:
            self.answers.append({"query": query, "content": str(answer)[:8000]})

        # Auto-fetch is disabled to give the Agent full autonomy.
        fetched_pages = []
        auto_selected = []

        self.current_candidate_summary = self.hooks.candidate_pool_summary(
            self.current_candidate_pool,
            self.pages,
        )
        if self.queries_tried:
            self.queries_tried[-1]["validPages"] = len([p for p in self.pages if p.get("fromPage")])
        # Build per-engine result counts for front-end trace visualization
        engine_stats = {}
        if isinstance(search_meta, dict):
            engine_stats["baidu"] = search_meta.get("engines", {}).get("baidu", {}).get("count", 0)
            engine_stats["searxng"] = search_meta.get("engines", {}).get("searxng", {}).get("count", 0)
            engine_stats["searxngEngineCounts"] = search_meta.get("searxngEngineCounts") or {}
        payload = {
            "query": query,
            "reason": reason,
            "results_found": len(refs),
            "top_results": top_results,
            "answer_box": str(answer or "")[:1200],
            "search_evidence": search_evidence,
            "default_fetch_budget": self.config.default_fetch_budget,
            "selection_guidance": (
                "Review the search results and snippets. If you need to read the full content of any webpages "
                "to answer the query, call fetch_webpage with a single URL or a list of URLs concurrently. "
                "Otherwise, if the search results and snippets are already sufficient, you can directly evaluate "
                "the evidence pack or produce your final answer."
            ),
            "candidate_pool_summary": self.current_candidate_summary,
            "engine_meta": search_meta,
            "engine_stats": engine_stats,
            "auto_fetched_pages": fetched_pages,
            "auto_selected_count": len(auto_selected),
        }
        # Emit a clear status update to indicate which engine was actually used
        engines_used = []
        if isinstance(search_meta, dict):
            engines_used = search_meta.get("availableProviders") or []
        engine_str = "+".join(engines_used).upper() if engines_used else "UNKNOWN"
        self._emit_trace("status", {
            "title": f"网页搜索 ({engine_str})",
            "detail": f"使用 {engine_str} 搜索引擎检索关键词：\"{query}\"，找到 {len(refs)} 个候选结果。"
        })
        self._emit_trace("search_subagent_search", payload)
        return payload

    def _tool_select_search_results(self, arguments: dict) -> dict:
        select_ids = arguments.get("select_ids") or []
        reason = str(arguments.get("reason") or "").strip()
        if not isinstance(select_ids, list):
            return {"error": "select_ids must be a list"}
        selected_candidates = []
        read_urls = {
            str(value or "")
            for page in self.pages or []
            for value in (page.get("url"), page.get("originalUrl"))
            if value
        }
        for raw_id in select_ids[: max(1, self.config.max_parallel_fetches)]:
            try:
                candidate = self.latest_results_by_id.get(int(raw_id))
            except Exception:
                candidate = None
            url = candidate.get("url") if candidate else ""
            if candidate and url and url not in read_urls and url not in self.attempted_urls:
                selected_candidates.append(candidate)
        if not selected_candidates:
            payload = {
                "reason": reason,
                "selected_ids": select_ids[: max(1, self.config.max_parallel_fetches)],
                "pages_collected": len(self.pages),
                "fetched_pages": [],
                "candidate_pool_summary": self.current_candidate_summary,
                "note": "Selected pages were already fetched or no valid result IDs were provided.",
            }
            self._emit_trace("search_subagent_fetch", payload)
            return payload

        fetched_pages = []
        workers = min(self.config.max_parallel_fetches, len(selected_candidates))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self.hooks.fetch_clean_page, item.get("url"), None): item
                for item in selected_candidates
            }
            for future in concurrent.futures.as_completed(future_map):
                item = future_map[future]
                url = item.get("url") or ""
                try:
                    clean_page = future.result() or {}
                except Exception as exc:
                    clean_page = {"url": url, "fetchStatus": "failed", "error": str(exc)}
                record = self._normalize_page_record(url, clean_page, item)
                self.fetch_trace.append(
                    {
                        "url": url,
                        "title": clean_page.get("title") or item.get("title") or "",
                        "domain": item.get("domain") or self.hooks.url_domain(url),
                        "sourceType": item.get("sourceType") or "unknown",
                        "fetchStatus": clean_page.get("fetchStatus") or "unknown",
                        "textChars": len(str(clean_page.get("text") or "").strip()),
                        "cleanMethod": clean_page.get("cleanMethod") or "",
                        "error": clean_page.get("error") or "",
                        "methodTrace": clean_page.get("fetchTrace") or [],
                    }
                )
                if record and not any(page.get("url") == record.get("url") for page in self.pages):
                    self.pages.append(record)
                    fetched_pages.append(
                        {
                            "title": record.get("title") or "",
                            "url": record.get("url") or "",
                            "domain": record.get("domain") or "",
                            "chars": record.get("textChars") or 0,
                            "cleanMethod": record.get("cleanMethod") or "",
                            "preview": self.hooks.compact_text(record.get("content") or "", 700),
                        }
                    )
                self.attempted_urls.add(url)
        self.current_candidate_summary = self.hooks.candidate_pool_summary(
            self.current_candidate_pool,
            self.pages,
        )
        payload = {
            "reason": reason,
            "selected_ids": select_ids[: max(1, self.config.max_parallel_fetches)],
            "pages_collected": len(self.pages),
            "fetched_pages": fetched_pages,
            "candidate_pool_summary": self.current_candidate_summary,
        }
        self._emit_trace("search_subagent_fetch", payload)
        return payload

    def _tool_fetch_webpage(self, arguments: dict) -> dict:
        url_arg = arguments.get("url")
        reason = str(arguments.get("reason") or "").strip()
        if not url_arg:
            return {"error": "url is required"}

        if isinstance(url_arg, list):
            urls = [str(u).strip() for u in url_arg if str(u).strip()]
        else:
            urls = [str(url_arg).strip()]

        if not urls:
            return {"error": "No valid URLs provided"}

        for url in urls:
            self._emit_trace("search_subagent_fetch_start", {"url": url, "title": "", "domain": self.hooks.url_domain(url)})

        fetched_results = []
        workers = min(self.config.max_parallel_fetches, len(urls))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self.hooks.fetch_clean_page, url, None): url
                for url in urls
            }
            for future in concurrent.futures.as_completed(future_map):
                url = future_map[future]
                try:
                    clean_page = future.result() or {}
                except Exception as exc:
                    clean_page = {"url": url, "fetchStatus": "failed", "error": str(exc)}
                
                fetch_method = clean_page.get("cleanMethod") or "unknown"
                fetch_trace_detail = clean_page.get("fetchTrace") or []
                record = self._normalize_page_record(url, clean_page, {"url": url, "domain": self.hooks.url_domain(url)})
                
                self.fetch_trace.append(
                    {
                        "url": url,
                        "title": clean_page.get("title") or url,
                        "domain": self.hooks.url_domain(url),
                        "sourceType": "direct_fetch",
                        "fetchStatus": clean_page.get("fetchStatus") or "unknown",
                        "textChars": len(str(clean_page.get("text") or "").strip()),
                        "cleanMethod": fetch_method,
                        "error": clean_page.get("error") or "",
                        "methodTrace": fetch_trace_detail,
                    }
                )
                
                if record and not any(page.get("url") == record.get("url") for page in self.pages):
                    self.pages.append(record)
                    
                fetched_results.append({
                    "url": url,
                    "success": bool(record),
                    "title": clean_page.get("title") or "",
                    "clean_method": fetch_method,
                    "fetchMethods": [t.get("method") for t in fetch_trace_detail],
                    "content_preview": self.hooks.compact_text(clean_page.get("text") or "", 900),
                })
                
                self._emit_trace(
                    "search_subagent_fetch_done",
                    {
                        "url": url,
                        "fetchStatus": clean_page.get("fetchStatus") or "unknown",
                        "fetchMethod": fetch_method,
                        "fetchMethods": [t.get("method") for t in fetch_trace_detail],
                        "textChars": len(str(clean_page.get("text") or "").strip()),
                        "error": clean_page.get("error") or "",
                    },
                )
                self.attempted_urls.add(url)

        payload = {
            "urls_requested": urls,
            "reason": reason,
            "success_count": sum(1 for r in fetched_results if r["success"]),
            "pages_collected": len(self.pages),
            "fetched_results": fetched_results,
        }
        self._emit_trace("search_subagent_fetch", payload)
        return payload

    def _compute_quality_gate(self) -> dict:
        if self.pages:
            self.current_candidate_summary = self.hooks.candidate_pool_summary(
                self.current_candidate_pool,
                self.pages,
            )
            self.document_capsules = self.hooks.build_document_capsules(
                self.goal,
                self.profile,
                self.pages,
                self.depth,
            )
            self.evidence_matrix = self.hooks.build_evidence_matrix(
                self.goal,
                self.profile,
                self.pages,
                self.current_candidate_pool,
            )
            self.compressed_evidence = self.hooks.build_compressed_evidence_brief(
                self.goal,
                self.profile,
                self.document_capsules,
                self.evidence_matrix,
                self.depth,
            )
        read_pages = len([page for page in self.pages if page.get("fromPage")])
        read_domains = len(set(page.get("domain") for page in self.pages if page.get("domain")))
        llm_eval = None
        if self.compressed_evidence:
            evidence_for_eval = self.compressed_evidence
            if self.search_evidence:
                search_lines = [
                    f"- {item.get('title')} ({item.get('domain')}): {item.get('snippet')}"
                    for item in self.search_evidence[:10]
                    if item.get("snippet") or item.get("title")
                ]
                if search_lines:
                    evidence_for_eval = (
                        self.compressed_evidence
                        + "\n\n## Search Snippet Evidence\n"
                        + "\n".join(search_lines)
                    )
            llm_eval = self.hooks.evaluate_search_sufficiency(
                self.goal,
                evidence_for_eval,
                read_pages,
                read_domains,
                self.depth,
            )
        evidence_score = float(read_pages) + min(2.0, read_domains * 0.4)
        strong, reason = self.hooks.search_stop_state(
            self.profile,
            self.budget,
            self.depth,
            self.pages,
            evidence_score,
        )
        if llm_eval:
            action = str(llm_eval.get("suggested_action") or "").lower()
            score = float(llm_eval.get("sufficiency_score") or 0)
            if action == "stop" and score >= 8:
                strong = True
                reason = "llm_sufficient"
            elif action in {"continue", "refocus"} and score < 6:
                strong = False
                reason = "llm_needs_more" if action == "continue" else "llm_refocus"
        quality_gate = {
            "strong": strong,
            "reason": reason,
            "evidenceScore": round(evidence_score, 3),
            "readPages": read_pages,
            "readDomains": sorted(set(page.get("domain") for page in self.pages if page.get("domain"))),
            "documentCapsules": len(self.document_capsules),
            "candidatePoolSummary": self.current_candidate_summary,
            "llmEvaluation": llm_eval or {},
        }
        self.last_quality_gate = quality_gate
        self.last_stop_reason = reason
        return quality_gate

    def _tool_evaluate_evidence_pack(self, arguments: dict) -> dict:
        assessment = str(arguments.get("assessment") or "").strip()

        # When no pages have been fetched, evaluate based on search snippets alone
        if not self.pages:
            snippet_count = len(self.search_evidence)
            if snippet_count == 0:
                payload = {
                    "assessment": assessment,
                    "sufficiency_score": 1,
                    "suggested_action": "continue",
                    "reasoning": "尚未执行任何搜索，需要先调用 dual_search 获取候选结果。",
                    "pages_collected": 0,
                    "snippet_count": 0,
                    "candidate_pool_summary": self.current_candidate_summary,
                }
                self._emit_trace("search_subagent_evaluate", payload)
                return payload

            # Build snippet-based evidence for LLM evaluation
            search_lines = [
                f"- {item.get('title')} ({item.get('domain')}): {item.get('snippet')}"
                for item in self.search_evidence[:15]
                if item.get("snippet") or item.get("title")
            ]
            snippet_evidence = (
                f"目标问题：{self.goal}\n"
                f"搜索模式：仅搜索摘要评估（尚未抓取正文）\n"
                f"摘要条数：{snippet_count}\n\n"
                "## 搜索摘要证据\n" + "\n".join(search_lines)
            )

            # Try LLM evaluation on snippets
            llm_eval = None
            try:
                llm_eval = self.hooks.evaluate_search_sufficiency(
                    self.goal,
                    snippet_evidence,
                    0,  # read_pages
                    0,  # read_domains
                    self.depth,
                )
            except Exception:
                pass

            if llm_eval:
                suf_score = float(llm_eval.get("sufficiency_score") or 0)
                action = str(llm_eval.get("suggested_action") or "").lower()
            else:
                suf_score = min(6.0, snippet_count * 0.8)
                action = "stop" if suf_score >= 7 else "fetch_pages"

            payload = {
                "assessment": assessment,
                "sufficiency_score": suf_score,
                "suggested_action": action,
                "reasoning": (llm_eval or {}).get("reasoning") or (
                    f"基于 {snippet_count} 条搜索摘要评估。"
                    + ("摘要信息充分，可以直接回答。" if action == "stop" else "摘要信息不足，建议通过 fetch_webpage 抓取关键页面正文。")
                ),
                "missing_aspects": (llm_eval or {}).get("missing_aspects") or [],
                "suggested_queries": (llm_eval or {}).get("suggested_queries") or [],
                "pages_collected": 0,
                "snippet_count": snippet_count,
                "candidate_pool_summary": self.current_candidate_summary,
                "snippet_evidence_preview": self.hooks.compact_text(snippet_evidence, 2000),
            }
            self._emit_trace("search_subagent_evaluate", payload)
            return payload

        quality_gate = self._compute_quality_gate()
        llm_eval = quality_gate.get("llmEvaluation") or {}
        payload = {
            "assessment": assessment,
            "pages_collected": len(self.pages),
            "domains_collected": len(quality_gate.get("readDomains") or []),
            "sufficiency_score": llm_eval.get("sufficiency_score", quality_gate.get("evidenceScore", 0)),
            "suggested_action": llm_eval.get("suggested_action", "stop" if quality_gate.get("strong") else "continue"),
            "reasoning": llm_eval.get("reasoning") or quality_gate.get("reason") or "",
            "missing_aspects": llm_eval.get("missing_aspects") or [],
            "suggested_queries": llm_eval.get("suggested_queries") or [],
            "quality_gate": quality_gate,
            "compressed_evidence": self.hooks.compact_text(self.compressed_evidence, 3000),
        }
        self._emit_trace("search_subagent_evaluate", payload)
        return payload

    def _execute_tool_call(self, tool_call: dict) -> dict:
        server_name = tool_call.get("server_name") or ""
        tool_name = tool_call.get("tool_name") or ""
        arguments = tool_call.get("arguments") or {}
        self._emit_trace(
            "search_subagent_tool_call",
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "turn": self.turn_count,
            },
        )
        if tool_name == "dual_search":
            return self._tool_dual_search(arguments)
        if tool_name == "select_search_results":
            return self._tool_select_search_results(arguments)
        if tool_name == "fetch_webpage":
            return self._tool_fetch_webpage(arguments)
        if tool_name == "evaluate_evidence_pack":
            return self._tool_evaluate_evidence_pack(arguments)
        return {"error": f"Unknown tool: {tool_name}"}

    def _run_summary_pass(self, task_failed: bool = False) -> str:
        summarize_prompt = self.prompt.generate_summarize_prompt(
            self.goal,
            task_failed=task_failed,
            chinese_context=self.config.chinese_context,
        )
        summary_history = list(self.message_history) + [{"role": "user", "content": summarize_prompt}]
        response_text = self._llm_completion(summary_history, max_tokens=max(self.config.llm_max_tokens, 2800))
        if response_text:
            cleaned = strip_tool_markup(response_text)
            if cleaned:
                return cleaned
        return ""

    def _finalize_result(self, answer_text: str, task_failed: bool = False) -> dict:
        if self.pages and not self.document_capsules:
            self.document_capsules = self.hooks.build_document_capsules(
                self.goal,
                self.profile,
                self.pages,
                self.depth,
            )
        if self.pages and not self.evidence_matrix:
            self.evidence_matrix = self.hooks.build_evidence_matrix(
                self.goal,
                self.profile,
                self.pages,
                self.current_candidate_pool,
            )
        if self.pages and not self.compressed_evidence:
            self.compressed_evidence = self.hooks.build_compressed_evidence_brief(
                self.goal,
                self.profile,
                self.document_capsules,
                self.evidence_matrix,
                self.depth,
            )
        quality_gate = self.last_quality_gate or self._compute_quality_gate()
        if not answer_text:
            answer_text = self.compressed_evidence or self.last_direct_answer or "未能生成可靠的最终回答。"
        source_evidence = self.hooks.build_document_source_evidence(self.document_capsules)
        strong = bool(quality_gate.get("strong"))
        answerability = "answerable" if self.document_capsules and strong else ("partial" if self.document_capsules else "insufficient")
        result = {
            "success": not task_failed,
            "runtime": {
                "source": "MiroFlow search sub-agent",
                "controlTier": "baidu+searxng-dual-route-fusion",
                "agentTier": "miroflow-sub-worker-loop",
                "foundationTier": [
                    "miroflow-sub-worker-prompt",
                    "miroflow-tool-tag-parser",
                    "mindsearch-search-select-fetch",
                    "miroflow-smart-request-style-fetch",
                    "quality-gate-evidence-eval",
                ],
            },
            "query": self.goal,
            "depth": self.depth,
            "taskProfile": self.profile,
            "budget": self.budget,
            "refs": self.refs_all,
            "searchEvidence": self.search_evidence,
            "pages": self.pages,
            "cleanDocuments": self.hooks.clean_documents_for_output(self.pages),
            "selectedDocuments": [
                {
                    "title": capsule.get("title"),
                    "url": capsule.get("url"),
                    "domain": capsule.get("domain"),
                    "sourceType": capsule.get("sourceType"),
                    "documentId": capsule.get("documentId"),
                }
                for capsule in self.document_capsules
            ],
            "documentCapsules": self.document_capsules,
            "compressedEvidenceBrief": answer_text,
            "answers": self.answers,
            "candidatePool": self.current_candidate_pool,
            "candidatePoolSummary": self.current_candidate_summary,
            "readingPlan": {},
            "retrievedChunks": [],
            "sourceEvidence": source_evidence,
            "imageEvidence": [],
            "evidenceMatrix": self.evidence_matrix,
            "qualityGate": quality_gate,
            "answerability": answerability,
            "fetchTrace": self.fetch_trace,
            "queriesTried": self.queries_tried,
            "trace": self.trace,
            "evidenceScore": quality_gate.get("evidenceScore", 0),
            "weak": not strong,
            "stopReason": self.last_stop_reason,
            "repoContexts": [],
        }
        self._emit_trace(
            "search_subagent_done",
            {
                "goal": self.goal,
                "stopReason": self.last_stop_reason,
                "pages": len(self.pages),
                "queries": len(self.queries_tried),
                "answerability": answerability,
                "task_failed": task_failed,
            },
        )
        return result

    def run(self) -> dict:
        import time
        self.started_at = time.monotonic()
        self.is_done = False
        self.should_stop = False
        runtime = None
        token = None
        subagent_budget = None
        self._op_node = None
        if hasattr(self, "context") and isinstance(self.context, dict):
            runtime = self.context.get("_session_runtime")
            token = self.context.get("_cancellation_token")
            subagent_budget = self.context.get("_subagent_budget")
        if runtime is not None and hasattr(runtime, "create_node"):
            try:
                parent_op_id = runtime.current_op_id()
                self._op_node = runtime.create_node(
                    "SearchSubAgent",
                    "subagent",
                    parent_op_id=parent_op_id,
                    trajectory_id=f"SearchSubAgent:{uuid.uuid4().hex[:8]}",
                    cascade_id=getattr(runtime, "run_id", ""),
                )
                runtime.event_bus.emit(
                    "subagent.started",
                    "running",
                    op_id=self._op_node.op_id,
                    parent_op_id=parent_op_id,
                    trajectory_id=self._op_node.trajectory_id,
                    cascade_id=self._op_node.cascade_id,
                    lane="subagent",
                    payload={"goal": self.goal, "depth": self.depth},
                )
            except Exception:
                self._op_node = None

        if hasattr(self, "context") and isinstance(self.context, dict):
            def _local_context_lock(ctx):
                import threading
                if not isinstance(ctx, dict):
                    return threading.RLock()
                lock = ctx.get("_runtime_lock")
                if lock is None or not hasattr(lock, "acquire"):
                    lock = threading.RLock()
                    ctx["_runtime_lock"] = lock
                return lock
            
            lock = _local_context_lock(self.context)
            with lock:
                if "_running_subagents" not in self.context:
                    self.context["_running_subagents"] = {}
                self.context["_running_subagents"][id(self)] = self
            parent_context = getattr(self.context, "parent_context", None)
            if isinstance(parent_context, dict):
                parent_lock = _local_context_lock(parent_context)
                with parent_lock:
                    if "_running_subagents" not in parent_context:
                        parent_context["_running_subagents"] = {}
                    parent_context["_running_subagents"][id(self)] = self

        if not self.api_key:
            if runtime is not None and getattr(self, "_op_node", None) is not None:
                try:
                    self._op_node.status = "failed"
                    self._op_node.error = "missing_api_key"
                    runtime.event_bus.emit(
                        "subagent.failed",
                        "failed",
                        op_id=self._op_node.op_id,
                        parent_op_id=self._op_node.parent_op_id,
                        trajectory_id=self._op_node.trajectory_id,
                        cascade_id=self._op_node.cascade_id,
                        lane="subagent",
                        payload={"goal": self.goal},
                        error="missing_api_key",
                    )
                except Exception:
                    pass
            if hasattr(self, "context") and isinstance(self.context, dict):
                lock = _local_context_lock(self.context)
                with lock:
                    if "_running_subagents" in self.context:
                        self.context["_running_subagents"].pop(id(self), None)
                parent_context = getattr(self.context, "parent_context", None)
                if isinstance(parent_context, dict):
                    parent_lock = _local_context_lock(parent_context)
                    with parent_lock:
                        if "_running_subagents" in parent_context:
                            parent_context["_running_subagents"].pop(id(self), None)
            return {
                "success": False,
                "error": "缺少 API Key",
                "query": self.goal,
                "pages": [],
                "documentCapsules": [],
                "compressedEvidenceBrief": "",
                "sourceEvidence": [],
                "evidenceScore": 0,
                "stopReason": "missing_api_key",
                "answerability": "insufficient",
                "weak": True,
            }

        self._emit_trace(
            "search_subagent_start",
            {
                "goal": self.goal,
                "depth": self.depth,
                "budget": self.budget,
                "profile": self.profile,
            },
        )
        task_failed = False
        fast_exit = False
        cancelled_exit = False
        try:
            while self.turn_count < max(1, self.config.max_turns):
                if token is not None and getattr(token, "cancelled", False):
                    self.should_stop = True
                    self.last_stop_reason = getattr(token, "reason", "") or "cancelled"
                    fast_exit = True
                    cancelled_exit = True
                    break
                if getattr(self, "should_stop", False):
                    fast_exit = True
                    break
                if subagent_budget is not None and hasattr(subagent_budget, "consume") and not subagent_budget.consume(1):
                    self.should_stop = True
                    self.last_stop_reason = "subagent_budget_exhausted"
                    task_failed = True
                    fast_exit = True
                    cancelled_exit = True
                    break
                self.turn_count += 1
                response_text = self._llm_completion(self.message_history)
                if response_text is None:
                    task_failed = True
                    break
                self.message_history.append({"role": "assistant", "content": response_text})
                tool_calls, bad_tool_calls = parse_llm_response_for_tool_calls(response_text)
                if not tool_calls:
                    cleaned = strip_tool_markup(response_text)
                    if cleaned:
                        self.last_direct_answer = cleaned
                    if bad_tool_calls and not cleaned:
                        self.message_history = self._update_message_history(
                            self.message_history,
                            [
                                (
                                    "FAILED",
                                    self._format_tool_result_for_user(
                                        {
                                            "error": bad_tool_calls[0].get("error") or "Invalid tool call format",
                                            "content": bad_tool_calls[0].get("content") or "",
                                        }
                                    ),
                                )
                            ],
                        )
                        continue
                    break
                tool_calls_exceeded = len(tool_calls) > max(1, self.config.max_tool_calls_per_turn)
                tool_call = tool_calls[0]
                tool_result = self._execute_tool_call(tool_call)
                self.message_history = self._update_message_history(
                    self.message_history,
                    [(tool_call.get("id") or str(uuid.uuid4()), self._format_tool_result_for_user(tool_result))],
                    tool_calls_exceeded=tool_calls_exceeded,
                )
                # Fast-exit: after first dual_search with enough fetched pages, skip further turns
                if tool_call.get("tool_name") == "dual_search" and len(self.pages) >= 3:
                    total_chars = sum(len(str(p.get("content") or "").strip()) for p in self.pages)
                    if total_chars > 4000:
                        fast_exit = True
                        self.last_stop_reason = "fast_exit_sufficient"
                        break
            if cancelled_exit:
                final_answer = self.last_direct_answer or self.compressed_evidence or ""
                result = self._finalize_result(final_answer, task_failed=True)
                if runtime is not None and getattr(self, "_op_node", None) is not None:
                    try:
                        self._op_node.status = "cancelled"
                        runtime.event_bus.emit(
                            "subagent.cancelled",
                            "cancelled",
                            op_id=self._op_node.op_id,
                            parent_op_id=self._op_node.parent_op_id,
                            trajectory_id=self._op_node.trajectory_id,
                            cascade_id=self._op_node.cascade_id,
                            lane="subagent",
                            payload={"goal": self.goal, "stopReason": result.get("stopReason")},
                        )
                    except Exception:
                        pass
                return result
            if fast_exit:
                # Inject a direct instruction to summarize instead of continuing tool use
                self.message_history.append(
                    {
                        "role": "user",
                        "content": (
                            "已自动抓取足够证据（"
                            + str(len(self.pages))
                            + " 个网页，总内容 "
                            + str(sum(len(str(p.get("content") or "").strip()) for p in self.pages))
                            + " 字符）。"
                            + "请直接基于已抓取的内容生成最终回答，不要再调用任何工具。"
                        ),
                    }
                )
                response_text = self._llm_completion(self.message_history, max_tokens=max(self.config.llm_max_tokens, 2800))
                if response_text:
                    cleaned = strip_tool_markup(response_text)
                    if cleaned:
                        self.last_direct_answer = cleaned
            if self.turn_count >= max(1, self.config.max_turns) and not self.last_direct_answer:
                task_failed = True
                self.last_stop_reason = "max_turns"
            final_answer = self._run_summary_pass(task_failed=task_failed)
            if not final_answer:
                final_answer = self.last_direct_answer or self.compressed_evidence
            result = self._finalize_result(final_answer, task_failed=task_failed)
            if runtime is not None and getattr(self, "_op_node", None) is not None:
                try:
                    self._op_node.status = "completed"
                    runtime.event_bus.emit(
                        "subagent.completed",
                        "completed",
                        op_id=self._op_node.op_id,
                        parent_op_id=self._op_node.parent_op_id,
                        trajectory_id=self._op_node.trajectory_id,
                        cascade_id=self._op_node.cascade_id,
                        lane="subagent",
                        payload={"goal": self.goal, "stopReason": result.get("stopReason")},
                    )
                except Exception:
                    pass
            return result
        except Exception as exc:
            if runtime is not None and getattr(self, "_op_node", None) is not None:
                try:
                    self._op_node.status = "failed"
                    self._op_node.error = str(exc)
                    runtime.event_bus.emit(
                        "subagent.failed",
                        "failed",
                        op_id=self._op_node.op_id,
                        parent_op_id=self._op_node.parent_op_id,
                        trajectory_id=self._op_node.trajectory_id,
                        cascade_id=self._op_node.cascade_id,
                        lane="subagent",
                        payload={"goal": self.goal},
                        error=str(exc),
                    )
                except Exception:
                    pass
            raise
        finally:
            self.is_done = True
            if hasattr(self, "context") and isinstance(self.context, dict):
                def _local_context_lock(ctx):
                    import threading
                    if not isinstance(ctx, dict):
                        return threading.RLock()
                    lock = ctx.get("_runtime_lock")
                    if lock is None or not hasattr(lock, "acquire"):
                        lock = threading.RLock()
                        ctx["_runtime_lock"] = lock
                    return lock
                lock = _local_context_lock(self.context)
                with lock:
                    if "_running_subagents" in self.context:
                        self.context["_running_subagents"].pop(id(self), None)
                parent_context = getattr(self.context, "parent_context", None)
                if isinstance(parent_context, dict):
                    parent_lock = _local_context_lock(parent_context)
                    with parent_lock:
                        if "_running_subagents" in parent_context:
                            parent_context["_running_subagents"].pop(id(self), None)
