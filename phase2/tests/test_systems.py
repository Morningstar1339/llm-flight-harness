"""Systems-executor tests — the discrete combat commands.

This is the layer the Phase 3 model reaches through AgentPilot, and it
had no test. What matters here: the semantic name -> hardware mapping is
data, not code (so rebinding never touches Python), pulses run off the
control thread, hold-type commands are press/release rather than tap, and
release_all_slew actually releases everything a hunt might have left
pressed.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon.output import MockOutput
from daemon.systems import DEFAULT_MAP, PULSE_S, SystemsExecutor


@pytest.fixture
def sysx(tmp_path):
    return SystemsExecutor(MockOutput(), map_path=str(tmp_path / "systems.json"))


def pressed(out, n, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if any(b == n and s for b, s in list(out.button_log)):
            return True
        time.sleep(0.005)
    return False


def settled(out, n, timeout=2.0):
    """Wait for the release edge of a pulse."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if (n, False) in list(out.button_log):
            return True
        time.sleep(0.005)
    return False


# ================================================================= mapping
def test_default_map_covers_the_repl_and_agent_command_set():
    for name in ("radar", "eos", "lock", "unlock", "wpn", "fire",
                 "dispense", "airbrake"):
        assert name in DEFAULT_MAP and "btn" in DEFAULT_MAP[name]


def test_slew_commands_are_hold_type():
    for name in ("slew_up", "slew_down", "slew_left", "slew_right"):
        assert DEFAULT_MAP[name]["hold"] is True


def test_pulse_commands_fit_vjoys_stock_eight_buttons():
    """Reconfiguring vJoy's button count makes DCS treat it as a new device
    and auto-bind defaults (7/20 doctrine). Keep the taps inside 1..8."""
    taps = [b["btn"] for b in DEFAULT_MAP.values() if not b.get("hold")]
    assert max(taps) <= 8 and min(taps) >= 1


def test_button_numbers_are_unique():
    nums = [b["btn"] for b in DEFAULT_MAP.values() if "btn" in b]
    assert len(nums) == len(set(nums))


def test_names_are_sorted(sysx):
    assert sysx.names() == sorted(sysx.names())
    assert "fire" in sysx.names()


def test_user_file_wins_per_command(tmp_path):
    path = tmp_path / "systems.json"
    path.write_text(json.dumps({"fire": {"btn": 15}}))
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    assert s.map["fire"]["btn"] == 15
    assert s.map["radar"] == DEFAULT_MAP["radar"], "others keep the default"


def test_merged_map_is_persisted(tmp_path):
    path = tmp_path / "systems.json"
    SystemsExecutor(MockOutput(), map_path=str(path))
    saved = json.loads(path.read_text())
    assert set(saved) == set(DEFAULT_MAP)


def test_unreadable_map_refuses_to_boot_on_defaults(tmp_path):
    """Falling back to DEFAULT_MAP would silently undo a rebinding — a `fire`
    moved to button 15 would go back to pulsing 6. Fail loudly instead."""
    with pytest.raises(RuntimeError, match="refusing to boot on the default"):
        SystemsExecutor(MockOutput(), map_path=str(tmp_path))    # a directory


def test_corrupt_map_refuses_to_boot_on_defaults(tmp_path):
    path = tmp_path / "systems.json"
    path.write_text("{ this is not json")
    with pytest.raises(RuntimeError, match="cannot read the systems map"):
        SystemsExecutor(MockOutput(), map_path=str(path))


def test_unwritable_map_path_still_boots(tmp_path, monkeypatch):
    """Persisting the merged map is a convenience, not a precondition."""
    path = tmp_path / "systems.json"
    real_open = open

    def deny_write(p, mode="r", *a, **kw):
        if "w" in mode:
            raise OSError("read-only filesystem")
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", deny_write)
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    assert "fire" in s.names()


def test_describe_marks_focus_dependent_key_bindings(tmp_path):
    path = tmp_path / "systems.json"
    path.write_text(json.dumps({"eos": {"key": "o"}}))
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    text = s.describe()
    assert "btn 6" in text and "key 'o' (focus!)" in text


# ================================================================ dispatch
def test_execute_pulses_the_mapped_button(sysx):
    sysx.execute("fire")
    assert pressed(sysx.out, DEFAULT_MAP["fire"]["btn"])
    assert settled(sysx.out, DEFAULT_MAP["fire"]["btn"])


def test_pulse_does_not_block_the_caller(sysx):
    """The 20 Hz control loop and the REPL must not stall for PULSE_S."""
    t0 = time.monotonic()
    sysx.execute("radar")
    assert time.monotonic() - t0 < PULSE_S / 2


def test_pulse_releases_even_if_it_is_never_polled(sysx):
    sysx.execute("lock")
    assert settled(sysx.out, DEFAULT_MAP["lock"]["btn"])
    log = [b for b, s in sysx.out.button_log if not s]
    assert DEFAULT_MAP["lock"]["btn"] in log


def test_unknown_command_is_rejected_loudly(sysx):
    with pytest.raises(KeyError, match="unknown system command"):
        sysx.execute("jettison_wings")


def test_pulse_raw_bypasses_the_map(sysx):
    sysx.pulse_raw(21)
    assert pressed(sysx.out, 21)


def test_hold_and_release_are_not_a_tap(sysx):
    sysx.hold("slew_right")
    assert sysx.out.button_log == [(DEFAULT_MAP["slew_right"]["btn"], True)]
    sysx.release("slew_right")
    assert sysx.out.button_log[-1] == (DEFAULT_MAP["slew_right"]["btn"], False)


def test_hold_rejects_a_non_button_command(tmp_path):
    path = tmp_path / "systems.json"
    path.write_text(json.dumps({"eos": {"key": "o"}}))
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    with pytest.raises(KeyError):
        s.hold("eos")
    with pytest.raises(KeyError):
        s.hold("nonexistent")


def test_release_all_slew_releases_every_hold_command(sysx):
    sysx.hold("slew_up")
    sysx.hold("slew_left")
    sysx.out.button_log.clear()
    sysx.release_all_slew()
    released = {b for b, s in sysx.out.button_log if not s}
    assert released == {DEFAULT_MAP[n]["btn"] for n in
                        ("slew_up", "slew_down", "slew_left", "slew_right")}


def test_release_all_slew_leaves_tap_commands_alone(sysx):
    sysx.release_all_slew()
    touched = {b for b, _ in sysx.out.button_log}
    assert DEFAULT_MAP["fire"]["btn"] not in touched


def test_binding_with_neither_btn_nor_key_is_rejected(tmp_path):
    path = tmp_path / "systems.json"
    path.write_text(json.dumps({"bogus": {"note": "nothing"}}))
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    with pytest.raises(ValueError, match="neither"):
        s.execute("bogus")


def test_missing_keystroke_backend_does_not_raise(tmp_path, monkeypatch, capsys):
    """The keystroke path is an overflow fallback; its absence must not
    take out the button path (and must never actually type in a test)."""
    monkeypatch.setitem(sys.modules, "pydirectinput", None)
    path = tmp_path / "systems.json"
    path.write_text(json.dumps({"eos": {"key": "o"}}))
    s = SystemsExecutor(MockOutput(), map_path=str(path))
    s.execute("eos")
    time.sleep(0.2)
    assert "pydirectinput not installed" in capsys.readouterr().out
