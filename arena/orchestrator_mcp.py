"""Built-in Antigravity-style Orchestrator MCP server.

Uses Google AI Mode (Gemini 4 Hydrogen) as the **Senior / Architect model**
while the model invoking this MCP server acts as the **Junior / Executor model**.

Exposed tools (registered under server name ``orchestrator``):
- ``plan``:    Decompose a task into concrete steps, architecture decisions,
               and acceptance criteria.
- ``review``:  Review the junior model's implementation or output, identify
               bugs/gaps, and return APPROVED or concrete fixes.
- ``consult``: Ask the senior model a focused architectural or debugging question.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

LOG = logging.getLogger("arena.orchestrator_mcp")

SERVER_NAME = "orchestrator"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "plan",
        "description": (
            "Ask the Senior Architect model (Google AI Mode / Gemini) to decompose a "
            "task into a clear, step-by-step execution plan with architecture choices "
            "and acceptance criteria before you start implementing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The user's goal or engineering task to plan.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional codebase/workspace context, constraints, or relevant files.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "review",
        "description": (
            "Ask the Senior Architect model (Google AI Mode / Gemini) to review your "
            "completed work, diff, or solution against the task requirements and return "
            "either APPROVED or specific actionable fixes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The original task or requirements.",
                },
                "result": {
                    "type": "string",
                    "description": "Summary of what you implemented or produced.",
                },
                "details": {
                    "type": "string",
                    "description": "Optional code snippets, diffs, or test outputs to inspect.",
                },
            },
            "required": ["task", "result"],
        },
    },
    {
        "name": "consult",
        "description": (
            "Ask the Senior Architect model (Google AI Mode / Gemini) a focused "
            "architectural, design, or debugging question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question for the senior model.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional error logs, code context, or constraints.",
                },
            },
            "required": ["question"],
        },
    },
]


def _ask_senior_google(prompt: str, hl: str = "en") -> str:
    """Query Google AI Mode (plain HTTP with automatic WebView fallback)."""
    from arena import google_ai, google_http

    # 1. Try plain HTTP first if cookies are available.
    try:
        from arena.web import app as web_app
        cookies = web_app._google_cookies_for_http()
        if cookies:
            client = google_http.GoogleHTTP(cookies, hl=hl)
            res = client.ask(prompt)
            text = (res.get("text") or "").strip() if isinstance(res, dict) else ""
            if text:
                return text
    except Exception as exc:
        LOG.info("[orchestrator] google http failed, falling back to webview: %s", exc)

    # 2. Automatic WebView fallback (solves Google's JS interstitial seamlessly).
    eng = getattr(google_ai, "ENGINE", None)
    if eng is None or getattr(eng, "window", None) is None:
        return (
            "[Orchestrator error: Google AI Mode is not available. "
            "Sign in to Google in Arena Desktop so the senior model can answer.]"
        )
    try:
        eng.load(eng.search_url(prompt, hl))
        time.sleep(3.5)
        deadline = time.time() + 35.0
        last_seen = ""
        stable_since = 0.0
        while time.time() < deadline:
            time.sleep(1.0)
            cur = ""
            try:
                raw = eng._answer_text()
                if raw:
                    cur = eng.clean_answer(raw, prompt)
            except Exception:
                cur = ""
            if cur and cur != last_seen:
                last_seen = cur
                stable_since = time.time()
            elif cur and stable_since and (time.time() - stable_since) >= 2.0:
                return cur.strip()
        if last_seen:
            return last_seen.strip()
    except Exception as exc:
        LOG.info("[orchestrator] google webview fallback failed: %s", exc)

    return "[Orchestrator error: Google AI Mode did not return a response in time.]"


def call_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute one Orchestrator MCP tool call using Google AI Mode as senior model."""
    args = arguments if isinstance(arguments, dict) else {}
    name = (tool_name or "").strip().lower()

    if name == "plan":
        task = str(args.get("task") or "").strip()
        context = str(args.get("context") or "").strip()
        if not task:
            return "Error: 'task' is required for orchestrator.plan"
        prompt = (
            "You are the Senior Architect in an Antigravity-style orchestrator. "
            "A junior executor model will implement your plan. "
            "Produce a concise, numbered execution plan with key files/steps, "
            "edge cases, and verification criteria.\n\n"
            f"Task: {task}"
        )
        if context:
            prompt += f"\n\nContext:\n{context[:1200]}"
        return _ask_senior_google(prompt)

    if name == "review":
        task = str(args.get("task") or "").strip()
        result = str(args.get("result") or "").strip()
        details = str(args.get("details") or "").strip()
        if not task or not result:
            return "Error: 'task' and 'result' are required for orchestrator.review"
        prompt = (
            "You are the Senior Reviewer in an Antigravity-style orchestrator. "
            "Review the junior executor's work against the task. "
            "Start with APPROVED if complete and correct, or CHANGES_REQUESTED "
            "followed by concise, actionable fixes.\n\n"
            f"Task: {task}\n\nJunior result:\n{result[:1200]}"
        )
        if details:
            prompt += f"\n\nDetails/Diff:\n{details[:1200]}"
        return _ask_senior_google(prompt)

    if name == "consult":
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()
        if not question:
            return "Error: 'question' is required for orchestrator.consult"
        prompt = (
            "You are the Senior Architect in an Antigravity-style orchestrator. "
            "Give a direct, technically sound answer to the junior model's question.\n\n"
            f"Question: {question}"
        )
        if context:
            prompt += f"\n\nContext:\n{context[:1200]}"
        return _ask_senior_google(prompt)

    return f"Unknown orchestrator tool: {tool_name}"
