"""Model adapters for the Phase 3 pilot loop.

The agent loop (agent.py) never imports an SDK. It talks to a
`PilotClient`: give it a state snapshot, get back the model's raw text.
Everything that decides what the aircraft does — parsing, validation,
authority, the fire interlock — lives on our side of that seam and is
tested with no model in the loop.

Two implementations:

  ScriptedClient        — canned or callback-driven responses. This is
                          what the whole test suite flies against.
  ClaudeAgentSDKClient  — the real thing: `claude-agent-sdk`, which
                          drives the Claude Code CLI already installed
                          on this box (Node 22). Tools from the registry
                          are exposed to the model as an in-process SDK
                          MCP server, so the model can call
                          search_manual mid-decision.

Why the Agent SDK and not the raw Messages API: it is the officially
supported path that runs on **plan auth** through the local CLI, which
is how this box is set up. The seam above means swapping it for a direct
`anthropic` client is a one-file change — nothing else imports it.

  ANTHROPIC_API_KEY MUST BE UNSET. With the key set, the SDK bills the
  key instead of using plan auth, silently. The adapter refuses to
  construct if it sees one; pass allow_api_key=True only if you mean it.
"""
from __future__ import annotations

import os
from typing import Callable, Protocol

MCP_SERVER_NAME = "harness"
DEFAULT_MODEL = "claude-opus-5"


class PilotClient(Protocol):
    """Snapshot in, raw model text out. The only seam to a model."""

    name: str

    def decide(self, snapshot: str) -> str:
        ...


# ------------------------------------------------------------- scripted --
class ScriptedClient:
    """Deterministic stand-in. No network, no SDK, no cost.

    responses:  strings handed out in order; the last one repeats.
    callback:   fn(snapshot, call_index) -> str, for tests that need to
                react to what the loop actually sent.
    """

    def __init__(self, responses=None, callback: Callable[[str, int], str] | None = None,
                 name: str = "scripted"):
        if (responses is None) == (callback is None):
            raise ValueError("pass exactly one of responses= or callback=")
        self.responses = list(responses or [])
        self.callback = callback
        self.name = name
        self.snapshots: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.snapshots)

    def decide(self, snapshot: str) -> str:
        i = len(self.snapshots)
        self.snapshots.append(snapshot)
        if self.callback is not None:
            return self.callback(snapshot, i)
        if not self.responses:
            raise RuntimeError("ScriptedClient ran out of responses")
        return self.responses[min(i, len(self.responses) - 1)]


# ------------------------------------------------------------ Agent SDK --
def _build_sdk_tools(registry):
    """Wrap every ToolSpec in the registry as an in-process MCP tool."""
    from claude_agent_sdk import tool

    def make_handler(tool_name):
        async def handler(args):
            res = registry.call(tool_name, args)
            return {"content": [{"type": "text", "text": res.text}],
                    "isError": not res.ok}
        return handler

    out = []
    for adv in registry.schemas():
        decorate = tool(adv["name"], adv["description"], adv["input_schema"])
        out.append(decorate(make_handler(adv["name"])))
    return out


class ClaudeAgentSDKClient:
    """Real model adapter over claude-agent-sdk (plan auth via the CLI).

    Deliberately narrow: no built-in tools (no Bash, no file access — a
    flight agent has no business with either), only the harness registry,
    and a turn cap so one decision cannot run away.
    """

    name = "claude-agent-sdk"

    def __init__(self, registry, system_prompt: str, model: str = DEFAULT_MODEL,
                 max_turns: int = 6, cwd: str | None = None,
                 allow_api_key: bool = False):
        if os.environ.get("ANTHROPIC_API_KEY") and not allow_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is set. The Agent SDK would bill that key "
                "instead of using plan auth, silently. Unset it, or pass "
                "allow_api_key=True if billing the key is what you want.")
        # Imported here, not at module scope: the daemon imports this module
        # at boot and must start on a box with no SDK installed.
        from claude_agent_sdk import ClaudeAgentOptions, query

        self._query = query
        self._registry = registry
        self._tool_names = [f"mcp__{MCP_SERVER_NAME}__{n}" for n in registry.names()]
        self._options_factory = lambda server: ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            max_turns=max_turns,
            tools=[],                       # no built-in tools, on purpose
            allowed_tools=list(self._tool_names),
            mcp_servers={MCP_SERVER_NAME: server},
            permission_mode="dontAsk",      # no console prompt mid-flight
            cwd=cwd,
        )

    def _server(self):
        from claude_agent_sdk import create_sdk_mcp_server
        return create_sdk_mcp_server(
            name=MCP_SERVER_NAME, version="1.0.0",
            tools=_build_sdk_tools(self._registry))

    def decide(self, snapshot: str) -> str:
        import asyncio
        return asyncio.run(self._decide_async(snapshot))

    async def _decide_async(self, snapshot: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        options = self._options_factory(self._server())
        chunks: list[str] = []
        result_text = None
        async for msg in self._query(prompt=snapshot, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(
                        f"agent SDK error: {msg.subtype} {msg.result or ''}".strip())
                result_text = msg.result
        return (result_text or "\n".join(chunks)).strip()
