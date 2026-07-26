"""Phase 3 agent-loop tests. --mock only: no DCS, no vJoy, no model calls.

Everything here flies a real Daemon(mock=True) with a ScriptedClient in
place of the model, so the whole path — snapshot, parse, validate, gate,
dispatch — is exercised without a single API call.

The two gates the mission called out by name:
  * test_repl_manual_revokes_agent_authority  and
    test_manual_strips_authority_mid_plan     — `manual` strips agent
    authority instantly, including partway through a multi-command plan.
  * the whole "fire interlock" section        — an unauthorized agent fire
    request is refused and never reaches the systems executor.
"""
import builtins
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from daemon import agent as ag
from daemon.agent import (
    ACTIVE,
    ADVISORY,
    COMMANDS,
    OFF,
    AgentPilot,
    Command,
    FireAuthorization,
    build_snapshot,
    command_reference,
    extract_json,
    parse_commands,
    validate_command,
)
from daemon.agent_tools import build_default_registry
from daemon.main import Daemon, envelope_violation
from daemon.model_client import ScriptedClient

FIRE_BTN = 6


# ------------------------------------------------------------- fixtures --
@pytest.fixture
def d(tmp_path, monkeypatch):
    """A real Daemon in mock mode, no control thread, cwd sandboxed."""
    monkeypatch.chdir(tmp_path)
    daemon = Daemon(mock=True, gains_path=None)
    daemon.tele.inject(daemon._sim.packet(1))
    yield daemon
    daemon.pilot.stop()
    daemon.rec_stop()


def engage(d):
    """What `auto` does, minus the recorder."""
    d.engaged = True


def active(d):
    engage(d)
    d.pilot.set_authority(ACTIVE, "test")


def cmd(name, args=None, intent="test"):
    c, err = validate_command({"command": name, "args": args or {}, "intent": intent})
    assert err is None, err
    return c


def inject(d, **fields):
    d.tele.inject(fields)


def contact(cid, az=10.0, nm=25.0, ft=20000.0, mach=0.9):
    return {"id": cid, "az_rad": math.radians(az), "dist_m": nm * 1852.0,
            "alt_m": ft * 0.3048, "mach": mach}


def button_pressed(out, n, timeout=1.5):
    """Systems pulses run on a worker thread — poll for the press edge."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if any(b == n and state for b, state in list(out.button_log)):
            return True
        time.sleep(0.01)
    return False


def drive_repl(d, monkeypatch, lines):
    """Run the real REPL over a scripted stdin."""
    from daemon.main import repl
    it = iter(list(lines) + ["quit"])
    monkeypatch.setattr(builtins, "input", lambda *_a: next(it))
    repl(d)


# ========================================================== command schema
def test_every_command_from_the_phase3_plan_exists():
    """The set CLAUDE.md promised. Additive only — nothing else."""
    assert set(COMMANDS) == {
        "FLY", "CRANK", "NOTCH", "PUMP", "RECOMMIT", "RADAR",
        "LOCK", "DROP_LOCK", "FOX", "DEFEND", "RTB", "HOLD"}


def test_only_fox_needs_authorization():
    needs = [n for n, s in COMMANDS.items() if s.needs_authorization]
    assert needs == ["FOX"]


def test_agent_has_no_command_that_grants_itself_authority():
    """Authorization is a console act; there must be no command for it."""
    text = command_reference().lower()
    for spec in COMMANDS.values():
        assert "authoriz" not in spec.name.lower()
    assert "authorize fire" not in text.replace("human authorization", "")


@pytest.mark.parametrize("raw,expect", [
    ({"command": "HOLD"}, None),
    ({"command": "hold"}, None),                        # case-insensitive
    ({"command": "FLY", "args": {"hdg": 270}}, None),
    ({"command": "CRANK", "args": {"direction": "l"}}, None),   # abbreviation
    ({}, "missing 'command'"),
    ({"command": "SELF_DESTRUCT"}, "unknown command"),
    ({"command": "FLY"}, "at least one of"),
    ({"command": "FLY", "args": {"heading": 270}}, "unknown argument"),
    ({"command": "NOTCH", "args": {"direction": "left"}}, "missing required"),
    ({"command": "FOX"}, "missing required argument 'target_id'"),
    ({"command": "FOX", "args": {"target_id": 1.5}}, "whole number"),
    ({"command": "FLY", "args": {"hdg": "270"}}, "must be a number"),
    ({"command": "FLY", "args": {"hdg": True}}, "must be a number"),
    ({"command": "CRANK", "args": {"direction": "sideways"}}, "not one of"),
    ("not a dict", "must be an object"),
])
def test_validate_command(raw, expect):
    c, err = validate_command(raw)
    if expect is None:
        assert err is None and c is not None
    else:
        assert c is None and expect in err


@pytest.mark.parametrize("args", [
    {"hdg": 361}, {"hdg": -1}, {"alt": 200000}, {"alt": 10}, {"mach": 9.0},
])
def test_out_of_range_is_refused_not_clamped(args):
    """A hallucinated number must not become a command to climb to FL2000."""
    c, err = validate_command({"command": "FLY", "args": args})
    assert c is None
    assert "outside" in err and "refusing rather than clamping" in err


def test_missing_intent_is_a_warning_not_a_refusal():
    """Refusing a defensive maneuver over a missing log string is worse."""
    c, err = validate_command({"command": "PUMP"})
    assert err is None
    assert c.intent == "(no intent given)"
    assert any("intent" in w for w in c.warnings)


# ================================================================= parsing
@pytest.mark.parametrize("text", [
    '{"command": "HOLD", "intent": "x"}',
    '```json\n{"command": "HOLD", "intent": "x"}\n```',
    '```\n{"command": "HOLD", "intent": "x"}\n```',
    'Thinking about it...\n{"command": "HOLD", "intent": "x"}\nDone.',
])
def test_parse_tolerates_fences_and_prose(text):
    cmds, errors = parse_commands(text)
    assert not errors and [c.name for c in cmds] == ["HOLD"]


def test_parse_accepts_an_array_in_order():
    cmds, errors = parse_commands(
        '[{"command":"RADAR","intent":"a"},{"command":"HOLD","intent":"b"}]')
    assert not errors
    assert [c.name for c in cmds] == ["RADAR", "HOLD"]


@pytest.mark.parametrize("text,expect", [
    ("", "empty response"),
    ("I would like to crank left.", "no JSON"),
    ('{"command": "HOLD"', "malformed JSON"),
])
def test_parse_reports_garbage_without_raising(text, expect):
    cmds, errors = parse_commands(text)
    assert cmds == [] and expect in errors[0]


def test_bad_command_in_a_batch_does_not_drop_the_good_ones():
    cmds, errors = parse_commands(
        '[{"command":"HOLD","intent":"a"},{"command":"NOPE"},'
        '{"command":"RADAR","intent":"c"}]')
    assert [c.name for c in cmds] == ["HOLD", "RADAR"]
    assert len(errors) == 1 and "[1]" in errors[0]


def test_extract_json_returns_error_not_exception():
    value, err = extract_json("]]] nonsense")
    assert value is None and err


# ================================================================ snapshot
def test_snapshot_carries_what_the_model_needs(d):
    engage(d)
    d.mm.fly(hdg_rad=math.radians(270), alt_m=6000, mach=0.9)
    inject(d, contacts=[contact(3, az=12.4, nm=32.1)],
           locked=[{"id": 3}],
           rwr=[{"type": 19, "az_rad": math.radians(-40), "priority": 2}])
    snap = build_snapshot(d, d.pilot)
    assert "MODE: AUTO" in snap
    assert "AGENT AUTHORITY: off" in snap
    assert "id 3" in snap and "[LOCKED]" in snap
    assert "RWR:" in snap and "type 19" in snap
    assert "tgt hdg 270" in snap
    assert "FOX authorization: NONE" in snap
    assert "LOCK MANAGER" in snap


def test_snapshot_says_clean_and_none_when_empty(d):
    snap = build_snapshot(d, d.pilot)
    assert "(none)" in snap and "(clean)" in snap
    assert "MODE: MANUAL" in snap


def test_snapshot_shows_live_fire_authorization(d):
    d.pilot.authorize_fire(7, ttl_s=60)
    assert "target 7 — valid" in build_snapshot(d, d.pilot)


def test_snapshot_is_read_only(d):
    engage(d)
    before = (d.engaged, d.pilot.authority, d.ap.tgt_hdg, d.mm.active)
    build_snapshot(d, d.pilot)
    assert (d.engaged, d.pilot.authority, d.ap.tgt_hdg, d.mm.active) == before


def test_system_prompt_lists_every_command():
    for name in COMMANDS:
        assert name in ag.SYSTEM_PROMPT


# ======================================================= authority machine
def test_authority_starts_off(d):
    assert d.pilot.authority == OFF


def test_active_requires_auto(d):
    msg = d.pilot.set_authority(ACTIVE, "test")
    assert "refused" in msg and "MANUAL" in msg
    assert d.pilot.authority == OFF


def test_advisory_is_allowed_in_manual(d):
    d.pilot.set_authority(ADVISORY, "test")
    assert d.pilot.authority == ADVISORY


def test_authority_change_bumps_epoch(d):
    e0 = d.pilot.epoch
    d.pilot.set_authority(ADVISORY, "test")
    assert d.pilot.epoch > e0


def test_off_authority_dispatches_nothing(d):
    engage(d)
    res = d.pilot.dispatch(cmd("FLY", {"hdg": 90}))
    assert not res.ok and "authority is off" in res.detail
    assert d.ap.tgt_hdg is None


def test_advisory_never_touches_the_aircraft(d):
    engage(d)
    d.pilot.set_authority(ADVISORY, "test")
    res = d.pilot.dispatch(cmd("FLY", {"hdg": 123}))
    assert not res.ok and "ADVISORY" in res.detail
    assert d.ap.tgt_hdg is None


def test_flight_path_commands_refused_in_manual(d):
    """Authority active but the human took the jet back mid-engagement."""
    active(d)
    d.engaged = False
    res = d.pilot.dispatch(cmd("CRANK", {"direction": "left"}))
    assert not res.ok and "the human has the jet" in res.detail


def test_systems_commands_still_work_in_manual(d):
    """Radar/lock don't touch the flight path — the human can fly and let
    the agent run the sensors."""
    active(d)
    d.engaged = False
    res = d.pilot.dispatch(cmd("RADAR"))
    assert res.ok


def test_invalid_authority_level_rejected(d):
    with pytest.raises(ValueError):
        d.pilot.set_authority("supreme", "nope")


# =============================== `manual` strips authority, mid-command ===
def test_revoke_is_instant_and_unconditional(d):
    active(d)
    d.pilot.authorize_fire(1)
    msg = d.pilot.revoke("manual safety word")
    assert d.pilot.authority == OFF
    assert d.pilot.fire_auth is None
    assert "fire authorization cleared" in msg


def test_manual_strips_authority_mid_plan(d):
    """The safety word lands between commands of a plan already running."""
    active(d)
    plan = [cmd("FLY", {"hdg": 270}),
            cmd("CRANK", {"direction": "left"}),
            cmd("RADAR")]

    real_fly = d.mm.fly

    def fly_then_manual(*a, **kw):
        real_fly(*a, **kw)
        d.pilot.revoke("manual safety word")   # human types `manual` right here
        d.engaged = False

    d.mm.fly = fly_then_manual
    results = d.pilot.execute_plan(plan)

    assert results[0].ok, "the first command had already run"
    assert not results[1].ok and "authority changed mid-plan" in results[1].detail
    assert not results[2].ok, "and nothing after it runs either"
    assert d.pilot.authority == OFF


def test_stale_epoch_is_refused_even_if_authority_is_active_again(d):
    """Revoke, re-arm, then finish an old plan: it must still be refused."""
    active(d)
    stale = d.pilot.epoch
    d.pilot.revoke("manual")
    active(d)
    res = d.pilot.dispatch(cmd("FLY", {"hdg": 10}), epoch=stale)
    assert not res.ok and "authority changed mid-plan" in res.detail
    assert d.ap.tgt_hdg is None


def test_repl_manual_revokes_agent_authority(d, monkeypatch, capsys):
    """Drive the real REPL: this gates main.py's `manual` branch itself."""
    drive_repl(d, monkeypatch, ["auto", "agent fly", "agent status", "manual",
                                "agent status"])
    out = capsys.readouterr().out
    assert "authority off -> active" in out or "authority=active" in out
    assert "manual safety word" in out
    assert "your stick has the jet" in out, "original MANUAL behaviour intact"
    assert d.pilot.authority == OFF
    assert d.engaged is False


def test_repl_manual_clears_fire_authorization(d, monkeypatch, capsys):
    drive_repl(d, monkeypatch, ["auto", "agent fly", "authorize fire 3", "manual"])
    assert d.pilot.fire_auth is None
    assert "FIRE AUTHORIZED" in capsys.readouterr().out


def test_emergency_disengage_takes_the_agent_with_it(d, capsys):
    """Envelope guard is an emergency. The agent does not survive it."""
    active(d)
    d.pilot.authorize_fire(2)
    msg = d.emergency_disengage("bank 140deg — inverted/departed")
    assert d.pilot.authority == OFF and d.pilot.fire_auth is None
    assert d.engaged is False
    assert "ENVELOPE GUARD" in msg and "YOUR JET" in msg


# ============================================================ fire interlock
def test_unauthorized_agent_fire_is_refused(d):
    """THE test. Agent asks for FOX with everything else perfect except a
    human authorization; nothing reaches the systems executor."""
    active(d)
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 3}, "shot available"))
    assert not res.ok
    assert "FOX REFUSED" in res.detail
    assert "no human fire authorization" in res.detail
    assert "authorize fire 3" in res.detail, "tell the human how to grant it"
    assert not button_pressed(d.out, FIRE_BTN, timeout=0.4), \
        "the weapon-release button must never have been pulsed"


def test_authorized_agent_fire_with_lock_is_released(d):
    active(d)
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    d.pilot.authorize_fire(3)
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 3}, "in range, locked"))
    assert res.ok and "release commanded" in res.detail
    assert button_pressed(d.out, FIRE_BTN)


def test_authorization_is_single_use(d):
    active(d)
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    d.pilot.authorize_fire(3)
    assert d.pilot.dispatch(cmd("FOX", {"target_id": 3})).ok
    second = d.pilot.dispatch(cmd("FOX", {"target_id": 3}))
    assert not second.ok and "already used" in second.detail


def test_authorization_is_target_specific(d):
    active(d)
    inject(d, contacts=[contact(3), contact(4)], locked=[{"id": 4}])
    d.pilot.authorize_fire(3)
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 4}))
    assert not res.ok and "authorization is for target 3" in res.detail
    assert not button_pressed(d.out, FIRE_BTN, timeout=0.4)


def test_authorization_expires(d):
    active(d)
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    d.pilot.authorize_fire(3, ttl_s=0.05)
    time.sleep(0.1)
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 3}))
    assert not res.ok and "expired" in res.detail
    assert not button_pressed(d.out, FIRE_BTN, timeout=0.4)


def test_lock_interlock_still_applies_under_authorization(d):
    """A human authorization does not bypass the existing FOX interlock."""
    active(d)
    inject(d, contacts=[contact(3)], locked=[])
    d.pilot.authorize_fire(3)
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 3}))
    assert not res.ok and "no lock on target 3" in res.detail
    assert not button_pressed(d.out, FIRE_BTN, timeout=0.4)
    assert d.pilot.fire_auth.consumed_at is None, "a refusal must not burn it"


def test_fox_refused_when_authority_is_advisory(d):
    engage(d)
    d.pilot.set_authority(ADVISORY, "test")
    d.pilot.authorize_fire(3)
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    res = d.pilot.dispatch(cmd("FOX", {"target_id": 3}))
    assert not res.ok and "ADVISORY" in res.detail
    assert not button_pressed(d.out, FIRE_BTN, timeout=0.4)


def test_deauthorize_cancels(d):
    active(d)
    d.pilot.authorize_fire(3)
    assert "revoked" in d.pilot.deauthorize("test")
    inject(d, contacts=[contact(3)], locked=[{"id": 3}])
    assert not d.pilot.dispatch(cmd("FOX", {"target_id": 3})).ok


def test_fire_authorization_check_order():
    """Consumed beats expired beats target mismatch — clearest message wins."""
    now = 1000.0
    a = FireAuthorization(target_id=3, ttl_s=10, issued_at=now)
    assert a.check(3, now) is None
    assert "expired" in a.check(3, now + 11)
    assert "not target 9" in a.check(9, now)
    a.consumed_at = now
    assert "already used" in a.check(3, now)


def test_authorize_fire_via_repl_is_the_only_grant_path(d, monkeypatch, capsys):
    drive_repl(d, monkeypatch, ["auto", "agent fly", "authorize fire 5 45"])
    assert d.pilot.fire_auth is not None
    assert d.pilot.fire_auth.target_id == 5
    assert 44 < d.pilot.fire_auth.ttl_s < 46
    assert "FIRE AUTHORIZED" in capsys.readouterr().out


# ================================================================ dispatch
def test_fly_sets_setpoints_in_si(d):
    active(d)
    res = d.pilot.dispatch(cmd("FLY", {"hdg": 270, "alt": 20000, "mach": 0.9}))
    assert res.ok
    assert abs(math.degrees(d.ap.tgt_hdg) - 270) < 0.01
    assert abs(d.ap.tgt_alt - 20000 * 0.3048) < 0.5
    assert abs(d.ap.tgt_mach - 0.9) < 1e-9


def test_crank_uses_current_target_heading_as_default_reference(d):
    active(d)
    d.pilot.dispatch(cmd("FLY", {"hdg": 0}))
    d.pilot.dispatch(cmd("CRANK", {"direction": "left"}))
    assert abs(math.degrees(d.ap.tgt_hdg) - 310) < 0.01


def test_crank_honours_explicit_reference_and_offset(d):
    active(d)
    d.pilot.dispatch(cmd("CRANK", {"direction": "right", "ref_hdg": 90,
                                   "offset_deg": 30}))
    assert abs(math.degrees(d.ap.tgt_hdg) - 120) < 0.01


def test_notch_puts_the_threat_on_the_beam(d):
    active(d)
    d.pilot.dispatch(cmd("NOTCH", {"direction": "left", "threat_brg": 0}))
    assert abs(math.degrees(d.ap.tgt_hdg) - 270) < 0.01


def test_recommit_without_a_reference_is_refused(d):
    active(d)
    res = d.pilot.dispatch(cmd("RECOMMIT"))
    assert not res.ok and "no commit reference" in res.detail


def test_pump_recommit_round_trip(d):
    active(d)
    d.pilot.dispatch(cmd("FLY", {"hdg": 0, "alt": 20000, "mach": 0.85}))
    d.pilot.dispatch(cmd("PUMP", {"threat_brg": 0}))
    assert abs(math.degrees(d.ap.tgt_hdg) - 180) < 0.01
    assert d.pilot.dispatch(cmd("RECOMMIT")).ok
    assert abs(math.degrees(d.ap.tgt_hdg) % 360) < 0.01


def test_rtb_defaults_and_labels_the_mode(d):
    active(d)
    assert d.pilot.dispatch(cmd("RTB", {"hdg": 180})).ok
    assert abs(math.degrees(d.ap.tgt_hdg) - 180) < 0.01
    assert abs(d.ap.tgt_alt - 20000 * 0.3048) < 0.5
    assert d.mm.active == "rtb"


def test_defend_dispenses_and_beams_the_short_way(d):
    active(d)
    inject(d, ownship={"hdg_rad": math.radians(350)})
    res = d.pilot.dispatch(cmd("DEFEND", {"threat_brg": 90}))
    assert res.ok and "beaming left" in res.detail
    # threat 090, beam left = 000 — 10 deg away, vs 180 which is 170 away
    assert abs(math.degrees(d.ap.tgt_hdg) % 360) < 0.01
    assert button_pressed(d.out, 7), "countermeasures"


def test_defend_without_a_bearing_only_dispenses(d):
    active(d)
    d.pilot.dispatch(cmd("FLY", {"hdg": 45}))
    res = d.pilot.dispatch(cmd("DEFEND"))
    assert res.ok and "heading unchanged" in res.detail
    assert abs(math.degrees(d.ap.tgt_hdg) - 45) < 0.01


def test_hold_changes_nothing(d):
    active(d)
    d.pilot.dispatch(cmd("FLY", {"hdg": 90}))
    before = (d.ap.tgt_hdg, d.ap.tgt_alt, d.ap.tgt_mach, d.mm.active)
    assert d.pilot.dispatch(cmd("HOLD")).ok
    assert (d.ap.tgt_hdg, d.ap.tgt_alt, d.ap.tgt_mach, d.mm.active) == before


def test_lock_delegates_to_the_lock_manager(d):
    active(d)
    calls = []
    d.lm.lock_contact = lambda tid: (calls.append(tid), True)[1]
    assert d.pilot.dispatch(cmd("LOCK", {"target_id": 4})).ok
    assert calls == [4]


def test_lock_reports_the_managers_refusal(d):
    active(d)
    d.lm.lock_contact = lambda tid: False
    d.lm.status = "busy — abort first"
    res = d.pilot.dispatch(cmd("LOCK", {"target_id": 4}))
    assert not res.ok and "busy" in res.detail


def test_drop_lock_aborts_the_hunt(d):
    active(d)
    aborted = []
    d.lm.abort = lambda: aborted.append(True)
    assert d.pilot.dispatch(cmd("DROP_LOCK")).ok
    assert aborted


def test_dispatch_exception_is_contained(d):
    """A blown maneuver must not take down the agent loop."""
    active(d)
    def boom(*a, **kw):
        raise ZeroDivisionError("bad geometry")
    d.mm.fly = boom
    res = d.pilot.dispatch(cmd("FLY", {"hdg": 90}))
    assert not res.ok and "ZeroDivisionError" in res.detail


def test_every_command_has_a_dispatch_path(d):
    """Nothing in COMMANDS may fall through to the harness-bug branch."""
    minimal = {"FLY": {"hdg": 90}, "CRANK": {"direction": "left"},
               "NOTCH": {"direction": "left", "threat_brg": 0},
               "FOX": {"target_id": 1}, "LOCK": {"target_id": 1},
               "RTB": {"hdg": 90}}
    active(d)
    d.lm.lock_contact = lambda tid: True
    for name in COMMANDS:
        res = d.pilot.dispatch(cmd(name, minimal.get(name)))
        assert "harness bug" not in res.detail, name


# ============================================================ decision loop
def test_step_flies_a_scripted_decision(d):
    active(d)
    d.pilot.client = ScriptedClient(
        ['{"command":"FLY","args":{"hdg":225,"alt":25000},"intent":"commit"}'])
    results, errors = d.pilot.step()
    assert not errors and len(results) == 1 and results[0].ok
    assert abs(math.degrees(d.ap.tgt_hdg) - 225) < 0.01


def test_step_sends_the_snapshot_to_the_model(d):
    active(d)
    client = ScriptedClient(['{"command":"HOLD","intent":"x"}'])
    d.pilot.client = client
    d.pilot.step()
    assert "HARNESS STATE" in client.snapshots[0]
    assert "AGENT AUTHORITY: active" in client.snapshots[0]


def test_step_without_a_client_is_reported(d):
    active(d)
    results, errors = d.pilot.step()
    assert results == [] and "no model client" in errors[0]


def test_step_with_authority_off_does_nothing(d):
    d.pilot.client = ScriptedClient(['{"command":"FLY","args":{"hdg":1}}'])
    results, errors = d.pilot.step()
    assert results == [] and "authority is off" in errors[0]
    assert d.pilot.client.calls == 0, "no model call when we have no authority"


def test_model_exception_does_not_escape(d):
    active(d)

    class Broken:
        name = "broken"
        def decide(self, snapshot):
            raise ConnectionError("CLI not found")

    d.pilot.client = Broken()
    results, errors = d.pilot.step()
    assert results == [] and "model call failed" in errors[0]
    assert "ConnectionError" in d.pilot.last_error


def test_model_garbage_does_not_fly_the_jet(d):
    active(d)
    d.pilot.client = ScriptedClient(["I think we should probably bug out?"])
    results, errors = d.pilot.step()
    assert results == [] and errors
    assert d.ap.tgt_hdg is None


def test_advisory_step_records_but_does_not_execute(d):
    engage(d)
    d.pilot.set_authority(ADVISORY, "test")
    d.pilot.client = ScriptedClient(
        ['{"command":"FLY","args":{"hdg":180},"intent":"look"}'])
    results, _ = d.pilot.step()
    assert results and results[0].ok and "ADVISORY" in results[0].detail
    assert d.ap.tgt_hdg is None, "advisory must never touch the setpoints"


def test_decisions_are_logged_for_debrief(d):
    active(d)
    d.pilot.client = ScriptedClient(
        ['{"command":"CRANK","args":{"direction":"left"},'
         '"intent":"cut closure at gimbal"}'])
    d.pilot.step()
    assert any("cut closure at gimbal" in str(r) for r in d.pilot.log)


def test_multi_command_plan_executes_in_order(d):
    active(d)
    d.pilot.client = ScriptedClient(
        ['[{"command":"RADAR","intent":"a"},'
         '{"command":"FLY","args":{"hdg":30},"intent":"b"},'
         '{"command":"CRANK","args":{"direction":"right"},"intent":"c"}]'])
    results, errors = d.pilot.step()
    assert not errors and [r.ok for r in results] == [True, True, True]
    assert abs(math.degrees(d.ap.tgt_hdg) - 80) < 0.01   # crank right off 030


def test_run_loop_stops_on_revocation(d):
    active(d)
    d.pilot.client = ScriptedClient(callback=lambda s, i: '{"command":"HOLD","intent":"x"}')
    d.pilot.run(cycles=None, cadence_s=0.01)
    time.sleep(0.08)
    d.pilot.revoke("manual")
    calls = d.pilot.client.calls
    time.sleep(0.08)
    assert d.pilot.client.calls == calls, "no model calls after revocation"


def test_run_loop_honours_cycle_count(d):
    active(d)
    d.pilot.client = ScriptedClient(callback=lambda s, i: '{"command":"HOLD","intent":"x"}')
    d.pilot.run(cycles=3, cadence_s=0.01)
    time.sleep(0.4)
    assert d.pilot.client.calls == 3


# ========================================================== daemon wiring
def test_daemon_boots_with_the_agent_layer(d):
    assert d.registry.names() == ["search_manual"]
    assert isinstance(d.pilot, AgentPilot)
    assert d.pilot.authority == OFF
    assert d.pilot.client is None, "no model attached until the human asks"


def test_agent_loop_can_reach_the_tool_registry(d):
    """Task 2's loop consumes Task 1's registry (MISSION Ruling 1)."""
    assert d.pilot.registry is d.registry
    assert [a["name"] for a in d.pilot.registry.schemas()] == ["search_manual"]
    assert "search_manual" in d.pilot.status_line()


def test_repl_agent_subcommands_do_not_crash(d, monkeypatch, capsys):
    drive_repl(d, monkeypatch,
               ["agent", "agent tools", "agent status", "agent snapshot",
                "agent log", "agent advise", "agent step", "agent nonsense",
                "agent client none", "deauthorize", "authorize fire"])
    out = capsys.readouterr().out
    assert "unknown agent subcommand" in out
    assert "usage: authorize fire" in out
    assert "no fire authorization outstanding" in out
    assert "error:" not in out.lower().replace("last model error", "")


def test_repl_agent_fly_refused_while_manual(d, monkeypatch, capsys):
    drive_repl(d, monkeypatch, ["agent fly"])
    assert "Type 'auto' first" in capsys.readouterr().out
    assert d.pilot.authority == OFF


def test_existing_repl_commands_are_unchanged(d, monkeypatch, capsys):
    """Phase 2 behaviour must survive the additive Phase 3 wiring."""
    drive_repl(d, monkeypatch,
               ["fly hdg 270 alt 20000 mach 0.9", "status", "auto", "status",
                "manual", "contacts", "gains"])
    out = capsys.readouterr().out
    assert "autopilot ON" in out
    assert "[AUTO]" in out and "[MANUAL]" in out
    assert "no radar contacts" in out
    assert "hdg_kp" in out


def test_envelope_violation_still_detects_departure():
    from daemon.telemetry import State
    ok = State(bank=0.0, ias=200.0, vv=5.0, aoa=math.radians(5))
    assert envelope_violation(ok) is None
    assert "inverted" in envelope_violation(State(bank=math.radians(120), ias=200.0))
    assert "stall margin" in envelope_violation(State(ias=50.0))
    assert "outside envelope" in envelope_violation(State(ias=200.0, vv=90.0))
    assert "stall regime" in envelope_violation(State(ias=200.0, aoa=math.radians(25)))


# ======================================================== model adapter seam
def test_scripted_client_requires_exactly_one_mode():
    with pytest.raises(ValueError):
        ScriptedClient()
    with pytest.raises(ValueError):
        ScriptedClient(responses=["x"], callback=lambda s, i: "y")


def test_agent_module_does_not_import_an_sdk():
    """The decision logic must be testable, and boot, with no SDK present."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "daemon", "agent.py"),
               encoding="utf-8").read()
    assert "claude_agent_sdk" not in src
    assert "anthropic" not in src.lower()


def test_sdk_client_refuses_to_bill_an_api_key(monkeypatch):
    """CLAUDE.md: with the key set the SDK silently bills it instead of
    using plan auth. Refuse rather than surprise the operator."""
    from daemon.model_client import ClaudeAgentSDKClient
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeAgentSDKClient(build_default_registry(), "prompt")
