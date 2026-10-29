"""Model-agnostic agentic loop over any OpenAI-compatible chat API (DeepSeek, Mistral, Qwen, vLLM...)."""

import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openai import AsyncOpenAI


@dataclass
class Tool:
    name: str  # "server__tool", OpenAI-safe
    description: str
    parameters: dict
    call: Callable[[dict], Awaitable[str]]

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class ToolCall:
    tool: str
    args: dict
    result: str


class ToolLimitExceeded(RuntimeError):
    pass


def make_client(model_cfg: dict) -> AsyncOpenAI:
    # Env overrides let you switch provider per client without touching the config.
    model_cfg["base_url"] = os.environ.get("LLM_BASE_URL", model_cfg["base_url"])
    model_cfg["name"] = os.environ.get("LLM_MODEL", model_cfg["name"])
    if model_cfg["base_url"] == "mock":
        from ap_agents.mock_llm import MockLLM
        return MockLLM()
    return AsyncOpenAI(base_url=model_cfg["base_url"], api_key=os.environ.get(model_cfg["api_key_env"], "missing"),
                       timeout=model_cfg.get("timeout_s", 60))


# Called before each tool runs; returning a string blocks the call and sends that string back to the model.
BeforeTool = Callable[[str, dict, list[ToolCall]], str | None]


async def run_loop(client: AsyncOpenAI, model_cfg: dict, messages: list[dict], tools: list[Tool],
                   calls: list[ToolCall], max_tool_calls: int,
                   before_tool: BeforeTool = lambda name, args, calls: None,
                   on_event: Callable[[str, dict], None] = lambda kind, data: None) -> str:
    """Call the model, run the tools it asks for, repeat until it answers.

    `messages` and `calls` are extended in place so a caller can continue the conversation (e.g. after a guard
    rejects the answer). Returns the final text.
    """
    by_name = {t.name: t for t in tools}
    while True:
        started = time.monotonic()
        resp = await client.chat.completions.create(
            model=model_cfg["name"], temperature=model_cfg.get("temperature", 0), messages=messages,
            tools=[t.spec() for t in tools] or None)
        usage = getattr(resp, "usage", None)  # measured per call so a run can report tokens and latency per invoice
        on_event("llm", {"tokens_in": getattr(usage, "prompt_tokens", 0), "tokens_out": getattr(usage, "completion_tokens", 0),
                         "seconds": round(time.monotonic() - started, 2)})
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or ""
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            if len(calls) >= max_tool_calls:
                raise ToolLimitExceeded(f"more than {max_tool_calls} tool calls")
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args, result = {}, "error: arguments were not valid JSON"
            else:
                on_event("tool_call", {"tool": name, "args": args})
                tool = by_name.get(name)
                blocked = before_tool(name, args, calls)
                if tool is None:
                    result = f"error: unknown tool {name}"
                elif blocked:
                    result = f"error: blocked by guardrail: {blocked}"
                    on_event("guard_block", {"tool": name, "reason": blocked})
                else:
                    try:
                        result = await tool.call(args)
                    except Exception as e:  # a failing tool is information for the model, not a crash
                        result = f"error: {type(e).__name__}: {e}"
            calls.append(ToolCall(name, args, result))
            on_event("tool_result", {"tool": name, "result": result[:2000]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
