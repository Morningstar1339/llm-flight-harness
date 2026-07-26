"""Agent tool registry — the callable surface the Phase 3 model gets.

The registry is the ONLY way a tool reaches the model. Phase 3's loop
(agent.py) reads schemas from here to advertise tools, and routes every
model tool call back through `ToolRegistry.call`. Nothing else in the
daemon hands the model a callable.

Design rules, in priority order:

1. **A tool call never raises into the flight loop.** Unknown tool, bad
   arguments, missing index, handler blowing up — every one of them
   comes back as a ToolResult with ok=False and a string the model can
   read. An exception escaping here would propagate into the agent loop
   while the jet is flying.
2. **Arguments are validated strictly.** Unknown keys are rejected
   rather than dropped: a model that misspells `quesion=` should be told
   so, not silently handed a default.
3. **Availability is checked per call, not at registration.** The manual
   index (chroma_db/) is gitignored and often absent. The tool then
   reports unavailable and the daemon still boots and still flies.
4. **read_only is metadata that must stay true here.** Every tool in
   this registry is a *lookup*. Aircraft state changes go through the
   command schema in agent.py, which is gated by the authority state
   machine and (for weapons) a human authorization. Do not add a tool
   that actuates anything — the gating lives on the other path.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable

# phase2/daemon/agent_tools.py -> phase2/daemon -> phase2 -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_retrieval = None
_retrieval_lock = threading.Lock()


def _import_retrieval():
    """Import retrieval.py from the repo root (it is not in this package)."""
    global _retrieval
    with _retrieval_lock:
        if _retrieval is None:
            if _REPO_ROOT not in sys.path:
                sys.path.insert(0, _REPO_ROOT)
            import retrieval  # noqa: PLC0415 — deferred on purpose (see rule 3)
            _retrieval = retrieval
        return _retrieval


# --------------------------------------------------------------- results --
@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call. `text` is what the model sees."""
    tool: str
    ok: bool
    text: str

    def __str__(self) -> str:
        return self.text


# ----------------------------------------------------------------- specs --
def _always_available() -> tuple[bool, str]:
    return True, "ok"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]
    availability: Callable[[], tuple[bool, str]] = _always_available
    read_only: bool = True

    def status(self) -> tuple[bool, str]:
        try:
            return self.availability()
        except Exception as e:                      # a probe must not raise
            return False, f"availability probe failed: {type(e).__name__}: {e}"


# ------------------------------------------------------------ validation --
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_arguments(schema: dict, arguments: dict) -> str | None:
    """Return an error string, or None if `arguments` satisfies `schema`.

    A deliberately small subset of JSON Schema: required keys, declared
    types, and (unless additionalProperties is true) rejection of keys
    the schema does not declare. Enough to be a real gate without adding
    a dependency to the flight-side environment.
    """
    if not isinstance(arguments, dict):
        return f"arguments must be an object, got {type(arguments).__name__}"
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            return f"missing required argument {key!r}"
    if not schema.get("additionalProperties", False):
        unknown = sorted(set(arguments) - set(props))
        if unknown:
            return (f"unknown argument(s) {', '.join(repr(u) for u in unknown)}; "
                    f"expected {sorted(props) or 'none'}")
    for key, value in arguments.items():
        want = props.get(key, {}).get("type")
        if want is None:
            continue
        py = _JSON_TYPES.get(want)
        if py is None:
            continue
        # bool is an int subclass in Python; don't let True satisfy "integer"
        if want in ("integer", "number") and isinstance(value, bool):
            return f"argument {key!r} must be {want}, got boolean"
        if not isinstance(value, py):
            return f"argument {key!r} must be {want}, got {type(value).__name__}"
    return None


# -------------------------------------------------------------- registry --
class ToolRegistry:
    """Name -> ToolSpec, plus the call path the agent loop uses."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.Lock()
        self._transcript: list[ToolResult] = []

    # ---- per-decision transcript -----------------------------------------
    # The FOX cite-or-label gate has to know what the manual actually returned
    # during the decision it is gating. Every tool call funnels through
    # `call`, so recording here is the one place that sees all of them,
    # including the ones the model makes from inside the SDK's own loop.
    def begin_turn(self) -> None:
        with self._lock:
            self._transcript = []

    def transcript(self) -> list:
        with self._lock:
            return list(self._transcript)

    def register(self, spec: ToolSpec) -> ToolSpec:
        with self._lock:
            if spec.name in self._tools:
                raise ValueError(f"tool {spec.name!r} is already registered")
            self._tools[spec.name] = spec
        return spec

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def schemas(self) -> list[dict]:
        """Tool advertisements for the model adapter (name/desc/schema)."""
        return [
            {"name": s.name,
             "description": s.description,
             "input_schema": s.input_schema}
            for s in (self.get(n) for n in self.names()) if s is not None
        ]

    def describe(self) -> str:
        """Human-readable listing for the REPL (`agent tools`)."""
        lines = []
        for name in self.names():
            spec = self._tools[name]
            ok, reason = spec.status()
            mark = "ok" if ok else f"UNAVAILABLE — {reason}"
            lines.append(f"  {name:16s} {mark}")
            lines.append(f"  {'':16s} {spec.description.splitlines()[0]}")
        return "\n".join(lines) if lines else "  (no tools registered)"

    def call(self, name: str, arguments: dict | None = None) -> ToolResult:
        """Invoke a tool. Never raises — see rule 1 in the module docstring."""
        res = self._call(name, arguments)
        with self._lock:
            self._transcript.append(res)
        return res

    def _call(self, name: str, arguments: dict | None = None) -> ToolResult:
        arguments = dict(arguments or {})
        spec = self.get(name)
        if spec is None:
            return ToolResult(name, False,
                              f"TOOL ERROR: unknown tool {name!r}; "
                              f"available: {self.names() or 'none'}")
        err = validate_arguments(spec.input_schema, arguments)
        if err:
            return ToolResult(name, False, f"TOOL ERROR: {name}: {err}")
        ok, reason = spec.status()
        if not ok:
            return ToolResult(name, False, f"TOOL UNAVAILABLE: {name}: {reason}")
        try:
            out = spec.handler(**arguments)
        except Exception as e:
            return ToolResult(name, False,
                              f"TOOL ERROR: {name} raised "
                              f"{type(e).__name__}: {e}")
        return ToolResult(name, True, out if isinstance(out, str) else str(out))


# --------------------------------------------------------- search_manual --
MAX_K = 8

SEARCH_MANUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look up, phrased as a question or topic — "
                           "e.g. 'R-27 employment range' or 'notch geometry'.",
        },
        "k": {
            "type": "integer",
            "description": f"How many passages to return, 1-{MAX_K} (default 3).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

SEARCH_MANUAL_DESCRIPTION = (
    "Look up Su-27 tactics doctrine in the indexed manual. "
    "Returns cited passages, or says plainly that nothing relevant was "
    "found — it does not guess. Use it for employment ranges, sensor "
    "procedures, and evasion doctrine; do not use it for aircraft state, "
    "which is in the snapshot you are given."
)


def _search_manual(query: str, k: int = 3) -> str:
    k = max(1, min(int(k), MAX_K))
    return _import_retrieval().search_manual(query, k)


def _manual_status() -> tuple[bool, str]:
    try:
        return _import_retrieval().manual_status()
    except Exception as e:
        return False, f"retrieval module unavailable: {type(e).__name__}: {e}"


SEARCH_MANUAL = ToolSpec(
    name="search_manual",
    description=SEARCH_MANUAL_DESCRIPTION,
    input_schema=SEARCH_MANUAL_SCHEMA,
    handler=_search_manual,
    availability=_manual_status,
    read_only=True,
)


def build_default_registry() -> ToolRegistry:
    """The registry the daemon boots with."""
    reg = ToolRegistry()
    reg.register(SEARCH_MANUAL)
    return reg


if __name__ == "__main__":
    reg = build_default_registry()
    print(reg.describe())
    if len(sys.argv) > 1:
        print()
        print(reg.call("search_manual", {"query": " ".join(sys.argv[1:])}).text)
