"""A configurable agent: an LLM loop with its own MCP tools, exposed over A2A, able to delegate to other agents over A2A."""

import json
import re
import sys
from contextvars import ContextVar
from contextlib import AsyncExitStack
from datetime import datetime, timezone

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Role, SendMessageRequest
from mcp import Client, StdioServerParameters
from starlette.applications import Starlette

from ap_agents.config import ROOT, STATE
from ap_agents import guards
from ap_agents.llm import Tool, ToolCall, make_client, run_loop
from ap_agents.prompts import system_prompt

HOST = "127.0.0.1"
current_ref: ContextVar[str | None] = ContextVar("current_ref", default=None)  # invoice file this task is about


def agent_url(cfg: dict, name: str) -> str:
    return f"http://{HOST}:{cfg['agents'][name]['port']}/"


async def send_a2a(url: str, text: str, timeout_s: float) -> str:
    """Send one message to an agent and return its text answer. The timeout covers the agent's whole task."""
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        client = await create_client(url, client_config=ClientConfig(httpx_client=http))
        request = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))
        return "\n".join([get_stream_response_text(r) async for r in client.send_message(request)])


def audit(agent: str, kind: str, data: dict) -> None:
    """Append one line per agent step: the trail an auditor (or the dashboard) reads."""
    STATE.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(), "agent": agent, "kind": kind, "ref": current_ref.get(), **data}
    with (STATE / "audit.jsonl").open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    print(f"[{agent}] {kind}: {json.dumps(data, ensure_ascii=False, default=str)[:160]}", flush=True)


class Agent(AgentExecutor):
    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.spec = cfg["agents"][name]
        self.llm = make_client(cfg["model"])
        self.tools: list[Tool] = []
        self.system = system_prompt(name, self.spec["role"], "Atelier Moreau SAS")
        self._stack = AsyncExitStack()
        self._failures: dict[str, int] = {}  # consecutive failures per delegate (circuit breaker)

    async def start(self) -> None:
        """Open MCP connections for the tools this agent is allowed, and A2A links to its delegates."""
        wanted: dict[str, set[str]] = {}
        for t in self.spec.get("tools", []):
            server, tool = t.split(".", 1)
            wanted.setdefault(server, set()).add(tool)
        for server, names in wanted.items():
            params = StdioServerParameters(command=sys.executable, args=["-m", self.cfg["mcp_servers"][server]["module"]],
                                           cwd=str(ROOT), env={"PYTHONPATH": str(ROOT / "src")})
            client = await self._stack.enter_async_context(Client(params))
            for t in (await client.list_tools()).tools:
                if t.name in names:  # least privilege: other tools on the server stay invisible
                    self.tools.append(Tool(f"{server}__{t.name}", t.description or "", t.input_schema,
                                           self._mcp_caller(client, server, t.name)))
        for other in self.spec.get("delegates_to", []):
            self.tools.append(Tool(
                f"ask_{other}", f"Delegate to the {other} agent over A2A. {self.cfg['agents'][other]['role']}",
                {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
                self._a2a_caller(other)))

    async def stop(self) -> None:
        await self._stack.aclose()

    def _mcp_caller(self, client: Client, server: str, tool: str):
        async def call(args: dict) -> str:
            result = await client.call_tool(tool, args)
            return "\n".join(getattr(c, "text", "") for c in result.content)
        return call

    def _a2a_caller(self, other: str):
        url, timeout = agent_url(self.cfg, other), self.cfg["limits"]["a2a_timeout_s"]
        threshold = self.cfg["limits"]["circuit_breaker_failures"]

        async def call(args: dict) -> str:
            if self._failures.get(other, 0) >= threshold:
                return f"error: circuit open, {other} failed {threshold} times in a row; escalate to a human"
            try:
                result = await send_a2a(url, args.get("message", ""), timeout)
            except Exception as e:
                result = json.dumps({"error": f"{type(e).__name__}: {e}"})
            failed = "error" in guards.parse_json(result)
            self._failures[other] = self._failures.get(other, 0) + 1 if failed else 0
            return result
        return call

    async def execute(self, context: RequestContext, event_queue) -> None:
        user = context.get_user_input()
        ref = re.search(r"\d\d_\w+\.pdf", user)  # every task in the chain names its source file
        current_ref.set(ref.group(0) if ref else None)
        audit(self.name, "task", {"input": user[:500]})
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        calls: list[ToolCall] = []
        try:
            for attempt in range(2):  # one chance to fix a failed guardrail check
                answer = await run_loop(self.llm, self.cfg["model"], messages, self.tools, calls,
                                        self.cfg["limits"]["max_tool_calls_per_task"],
                                        before_tool=guards.before_tool(self.name, user),
                                        on_event=lambda kind, data: audit(self.name, kind, data))
                problem, answer = guards.check_answer(self.name, user, answer, calls)
                if not problem:
                    break
                audit(self.name, "guard_fail", {"attempt": attempt + 1, "problem": problem})
                messages.append({"role": "user", "content": f"Guardrail check failed: {problem}. "
                                 "Use your tools to complete the task, then give your final JSON answer again."})
            else:
                answer = json.dumps({"error": f"guardrail: {problem}"})
        except Exception as e:  # surfaced to the caller as data, so the coordinator can react
            answer = json.dumps({"error": f"{type(e).__name__}: {e}"})
        audit(self.name, "answer", {"output": answer[:2000]})
        await event_queue.enqueue_event(new_text_message(answer, context_id=context.context_id, task_id=context.task_id))

    async def cancel(self, context: RequestContext, event_queue) -> None:
        pass

    def card(self) -> AgentCard:
        return AgentCard(
            name=self.name, description=self.spec["role"], version="0.1.0",
            supported_interfaces=[AgentInterface(url=agent_url(self.cfg, self.name), protocol_binding="JSONRPC",
                                                 protocol_version="1.0")],
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=["text/plain"], default_output_modes=["application/json"],
            skills=[AgentSkill(id=self.name, name=self.name, description=self.spec["role"], tags=["accounts-payable"])])

    def app(self) -> Starlette:
        card = self.card()
        handler = DefaultRequestHandler(agent_executor=self, task_store=InMemoryTaskStore(), agent_card=card)
        return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))
