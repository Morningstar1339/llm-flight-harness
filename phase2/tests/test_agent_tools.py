"""Tool-registry tests. Zero model calls, zero DCS, zero vJoy.

The manual index (chroma_db/) is gitignored and may be absent, so the
retrieval behaviour that matters — citation format, distance refusal,
graceful degradation — is tested against an injected fake collection.
The one test that touches a real index is skipped when there isn't one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon import agent_tools
from daemon.agent_tools import (
    ToolRegistry,
    ToolSpec,
    build_default_registry,
    validate_arguments,
)

retrieval = agent_tools._import_retrieval()


# ------------------------------------------------------------- fake store --
class FakeCollection:
    """Stands in for a Chroma collection. Records what it was asked."""

    def __init__(self, rows):
        self.rows = rows          # [(document, metadata, distance), ...]
        self.calls = []

    def query(self, query_texts, n_results):
        self.calls.append((list(query_texts), n_results))
        rows = self.rows[:n_results]
        return {"documents": [[r[0] for r in rows]],
                "metadatas": [[r[1] for r in rows]],
                "distances": [[r[2] for r in rows]]}


@pytest.fixture
def fake_manual(monkeypatch):
    """Install a fake collection; yields a setter for its rows."""
    holder = {}

    def install(rows):
        col = FakeCollection(rows)
        holder["col"] = col
        monkeypatch.setattr(retrieval, "_get_collection", lambda: col)
        return col

    return install


@pytest.fixture
def broken_manual(monkeypatch):
    """Simulate a missing/unreadable chroma_db/ (the fresh-clone state)."""
    def boom():
        raise retrieval.ManualUnavailable(
            "no vector store at /nope/chroma_db — run `python ingest.py`")
    monkeypatch.setattr(retrieval, "_get_collection", boom)


# ----------------------------------------------------------- registration --
def test_default_registry_has_search_manual():
    reg = build_default_registry()
    assert reg.names() == ["search_manual"]
    spec = reg.get("search_manual")
    assert spec is not None
    assert spec.read_only, "every registered tool must be a lookup"


def test_schemas_are_advertisable():
    """The agent loop hands these straight to the model adapter."""
    (schema,) = build_default_registry().schemas()
    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["name"] == "search_manual"
    assert schema["description"].strip()
    assert schema["input_schema"]["required"] == ["query"]
    assert schema["input_schema"]["properties"]["query"]["type"] == "string"
    assert schema["input_schema"]["additionalProperties"] is False


def test_duplicate_registration_rejected():
    reg = build_default_registry()
    with pytest.raises(ValueError, match="already registered"):
        reg.register(agent_tools.SEARCH_MANUAL)


def test_registry_starts_empty_and_describes_it():
    assert ToolRegistry().names() == []
    assert "no tools registered" in ToolRegistry().describe()


def test_describe_reports_availability(fake_manual):
    fake_manual([])
    assert "search_manual" in build_default_registry().describe()


# ------------------------------------------------------------- validation --
SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("args,expect", [
    ({"query": "x"}, None),
    ({"query": "x", "k": 3}, None),
    ({}, "missing required argument 'query'"),
    ({"query": "x", "quesion": "typo"}, "unknown argument"),
    ({"query": 7}, "must be string"),
    ({"query": "x", "k": "3"}, "must be integer"),
    ({"query": "x", "k": True}, "must be integer, got boolean"),
])
def test_validate_arguments(args, expect):
    err = validate_arguments(SCHEMA, args)
    if expect is None:
        assert err is None, err
    else:
        assert err is not None and expect in err


def test_validate_rejects_non_object():
    assert "must be an object" in validate_arguments(SCHEMA, ["query"])


# ------------------------------------------------------ call path is safe --
def test_unknown_tool_is_reported_not_raised():
    res = build_default_registry().call("launch_nukes", {})
    assert not res.ok
    assert "unknown tool" in res.text
    assert "search_manual" in res.text        # tells the model what exists


def test_bad_arguments_reported_not_raised(fake_manual):
    fake_manual([])
    res = build_default_registry().call("search_manual", {"k": 3})
    assert not res.ok and "missing required argument" in res.text


def test_handler_exception_is_contained():
    """A tool blowing up must not propagate into the flight loop."""
    reg = ToolRegistry()
    def explode():
        raise RuntimeError("gimbal lock")
    reg.register(ToolSpec("boom", "explodes", {"type": "object",
                                               "properties": {}}, explode))
    res = reg.call("boom", {})
    assert not res.ok
    assert "RuntimeError" in res.text and "gimbal lock" in res.text


def test_availability_probe_exception_is_contained():
    def bad_probe():
        raise OSError("disk gone")
    reg = ToolRegistry()
    reg.register(ToolSpec("t", "d", {"type": "object", "properties": {}},
                          lambda: "never", availability=bad_probe))
    res = reg.call("t", {})
    assert not res.ok and "availability probe failed" in res.text


# ------------------------------------------------------- cited-query path --
def test_cited_query_formats_citations(fake_manual):
    col = fake_manual([
        ("  R-27ER max launch range is 60 km head-on.  ",
         {"source": "Su-27 Operator's Manual", "page": 142}, 0.31),
        ("Notch by placing the threat on the beam.",
         {"source": "Su-27 Operator's Manual", "page": 88}, 0.62),
    ])
    out = build_default_registry().call(
        "search_manual", {"query": "R-27 employment range"})
    assert out.ok
    assert "[Su-27 Operator's Manual p.142] R-27ER max launch range is 60 km head-on." in out.text
    assert "[Su-27 Operator's Manual p.88] Notch by placing the threat on the beam." in out.text
    assert out.text.count("\n\n") == 1, "passages separated by a blank line"
    assert col.calls == [(["R-27 employment range"], 3)], "default k is 3"


def test_weak_matches_are_refused_not_answered(fake_manual):
    """Beyond MAX_DISTANCE the tool must decline, not return noise."""
    fake_manual([("unrelated fuel-system prose",
                  {"source": "INSTALL.md", "page": 1},
                  retrieval.MAX_DISTANCE + 0.01)])
    out = build_default_registry().call("search_manual", {"query": "gun harmonization"})
    assert out.ok
    assert out.text == "MANUAL: no relevant passage found for this query."


def test_distance_threshold_boundary_is_inclusive(fake_manual):
    fake_manual([("borderline passage", {"source": "m", "page": 2},
                  retrieval.MAX_DISTANCE)])
    out = build_default_registry().call("search_manual", {"query": "q"})
    assert "[m p.2] borderline passage" in out.text


def test_k_is_clamped(fake_manual):
    col = fake_manual([("d", {"source": "m", "page": 1}, 0.1)] * 20)
    reg = build_default_registry()
    reg.call("search_manual", {"query": "q", "k": 999})
    reg.call("search_manual", {"query": "q", "k": 0})
    assert [n for _, n in col.calls] == [agent_tools.MAX_K, 1]


# ------------------------------------------------- graceful degradation --
def test_missing_index_reports_unavailable(broken_manual):
    """chroma_db/ is gitignored — absent is a normal state, not a crash."""
    res = build_default_registry().call("search_manual", {"query": "anything"})
    assert not res.ok
    assert "TOOL UNAVAILABLE" in res.text
    assert "ingest.py" in res.text, "tell the operator how to fix it"


def test_search_manual_returns_string_when_index_missing(broken_manual):
    """Direct callers get a string too — retrieval never raises at them."""
    out = retrieval.search_manual("anything")
    assert isinstance(out, str)
    assert out.startswith(retrieval.UNAVAILABLE_PREFIX)


def test_manual_status_when_missing(broken_manual):
    ok, reason = retrieval.manual_status()
    assert ok is False and "ingest.py" in reason
    assert retrieval.manual_available() is False


def test_daemon_can_build_registry_without_index(broken_manual):
    """The boot path must survive a missing index (Ruling 1)."""
    reg = build_default_registry()
    assert reg.names() == ["search_manual"]
    assert "UNAVAILABLE" in reg.describe()


def test_real_get_collection_degrades_when_path_absent(monkeypatch, tmp_path):
    """Exercise the real _get_collection, not a stubbed one."""
    monkeypatch.setattr(retrieval, "_collection", None)
    monkeypatch.setattr(retrieval, "CHROMA_PATH", str(tmp_path / "no_such_store"))
    with pytest.raises(retrieval.ManualUnavailable, match="ingest.py"):
        retrieval._get_collection()
    ok, reason = retrieval.manual_status()
    assert ok is False and "no vector store" in reason
    res = build_default_registry().call("search_manual", {"query": "q"})
    assert not res.ok and "TOOL UNAVAILABLE" in res.text


def test_failed_load_is_not_cached(monkeypatch, tmp_path):
    """Running ingest.py mid-flight must not require a daemon restart."""
    monkeypatch.setattr(retrieval, "_collection", None)
    monkeypatch.setattr(retrieval, "CHROMA_PATH", str(tmp_path / "gone"))
    assert retrieval.manual_available() is False
    sentinel = FakeCollection([("doc", {"source": "m", "page": 3}, 0.1)])
    monkeypatch.setattr(retrieval, "_collection", sentinel)
    assert retrieval.manual_available() is True
    assert "[m p.3] doc" in retrieval.search_manual("q")


def test_store_path_is_repo_anchored_not_cwd():
    """The daemon runs from phase2/; the index lives at the repo root."""
    assert os.path.isabs(retrieval.CHROMA_PATH)
    assert os.path.dirname(retrieval.CHROMA_PATH) == retrieval.REPO_ROOT
    assert os.path.basename(retrieval.REPO_ROOT) != "phase2"


# ------------------------------------------------------------ integration --
@pytest.mark.skipif(not os.path.isdir(retrieval.CHROMA_PATH),
                    reason="chroma_db/ not built on this box (gitignored)")
def test_real_index_answers_or_declines():
    out = build_default_registry().call(
        "search_manual", {"query": "missile employment range", "k": 2})
    assert out.ok
    assert out.text.startswith("[") or out.text.startswith("MANUAL: no relevant")
