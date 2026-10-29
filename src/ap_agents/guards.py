"""Code guardrails around the LLM agents.

The model proposes; these checks verify against what the agent actually did (its real tool calls), never against
what it says it did. A failed check is sent back to the agent once to fix; if it still fails, the task fails
and the invoice is escalated to a human.
"""

import json

from ap_agents.llm import ToolCall
from ap_agents.prompts import FINANCE_EMAIL


def parse_json(text: str) -> dict:
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def policy(findings: list[dict]) -> str:
    severities = {f.get("severity") for f in findings}
    return "escalate" if "blocking" in severities else "hold" if "warning" in severities else "approve"


def _findings_from(calls: list[ToolCall], tool: str) -> list[dict] | None:
    """Findings from the last successful call to a delegate, or None if it was never answered."""
    for c in reversed(calls):
        if c.tool == tool:
            data = parse_json(c.result)
            if "findings" in data and "error" not in data:
                return data["findings"]
    return None


def _actual_findings(calls: list[ToolCall]) -> list[dict] | None:
    matched, validated = _findings_from(calls, "ask_matcher"), _findings_from(calls, "ask_validator")
    return None if matched is None or validated is None else matched + validated


def _emails(calls: list[ToolCall]) -> list[str]:
    return [c.args.get("to", "") for c in calls if c.tool == "mail__draft_email" and not c.result.startswith("error")]


def _queued(calls: list[ToolCall]) -> bool:
    return any(c.tool == "erp__queue_payment" and not c.result.startswith("error") for c in calls)


# --- before a tool runs -------------------------------------------------------------------------------------

def before_tool(agent: str, task_input: str):
    decision = parse_json(task_input).get("decision") if agent == "communicator" else None

    def check(name: str, args: dict, calls: list[ToolCall]) -> str | None:
        if agent == "communicator":
            if name == "erp__queue_payment" and decision != "approve":
                return f"payment can only be queued for an approved invoice (decision is {decision!r})"
            if name == "mail__draft_email" and decision == "escalate" and args.get("to") != FINANCE_EMAIL:
                return "a suspicious invoice is escalated to finance only; do not contact the supplier"
        if agent == "coordinator" and name == "ask_communicator":
            found = _actual_findings(calls)
            if found is None:
                return "ask_matcher and ask_validator must both answer before deciding"
            expected, proposed = policy(found), parse_json(args.get("message", "")).get("decision")
            if proposed != expected:
                return f"decision {proposed!r} does not follow policy for these findings: it must be {expected!r}"
        return None
    return check


# --- after the agent answers --------------------------------------------------------------------------------

def check_answer(agent: str, task_input: str, answer: str, calls: list[ToolCall]) -> tuple[str | None, str]:
    """Return (problem, answer). The answer is rewritten from real tool calls where that is possible."""
    if agent == "communicator":
        decision = parse_json(task_input).get("decision")
        emails = _emails(calls)
        if decision == "approve" and not (_queued(calls) and FINANCE_EMAIL in emails):
            return "an approved invoice needs erp__queue_payment and an approval email draft to finance", answer
        if decision == "hold" and not any(e != FINANCE_EMAIL for e in emails):
            return "a held invoice needs an email draft to the supplier (look it up with erp__get_vendor)", answer
        if decision == "escalate" and FINANCE_EMAIL not in emails:
            return f"an escalated invoice needs an alert email draft to {FINANCE_EMAIL}", answer
        done = [f"{c.tool}({json.dumps(c.args, ensure_ascii=False)[:120]}) -> {c.result[:120]}"
                for c in calls if not c.result.startswith("error")]
        return None, json.dumps({"actions": done}, ensure_ascii=False)  # what happened, not what was claimed

    if agent == "coordinator":
        found = _actual_findings(calls)
        if found is None or not any(c.tool == "ask_communicator" for c in calls):
            return "every step is required: ask_extractor, ask_matcher, ask_validator, ask_communicator", answer
        data = parse_json(answer)
        if data.get("decision") != policy(found):
            return f"final decision must be {policy(found)!r} for these findings", answer
        data["findings"] = found  # exactly what the checks returned
        data["actions"] = parse_json(next(c.result for c in reversed(calls) if c.tool == "ask_communicator")).get("actions", [])
        return None, json.dumps(data, ensure_ascii=False)

    return None, answer
