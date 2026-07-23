# CLAUDE.md — ClaudeHarness Phase 2 handoff

You are picking up on-box integration for a working project. Read this fully
before changing anything.

## Project

An AI-flies-DCS harness, built incrementally. End state: Claude (via Agent
SDK) flies a Su-27 in BVR combat against AI opponents, issuing doctrine-level
commands at ~8 s cadence while a deterministic inner loop does the flying.

**Phase plan:** 1) telemetry ✅ COMPLETE AND COMBAT-VALIDATED (July 18 —
contacts, RWR, LOCK all verified in a live intercept, missile fired) →
2) inner-loop autopilot (THIS PHASE) → 3) model in loop via Agent SDK →
4) BVR vs Veteran F-15C.

## Architecture (this repo)

- `../ClaudeHarness/Export.lua` — Phase 1. Runs inside DCS, streams JSON over
  UDP :27015. Every field pcall-guarded; errors ship in-packet. Chains prior
  export hooks (Tacview). DO NOT restructure it; it is proven.
- `daemon/telemetry.py` — UDP listener → `State` (SI units).
- `daemon/controllers.py` — cascaded PID autopilot. Gains in `gains.json`.
- `daemon/output.py` — vJoy device 1: X=aileron, Y=elevator, Z=throttle.
  `ELEV_SIGN`/`AIL_SIGN` flip constants if surface response is reversed.
- `daemon/maneuvers.py` — crank/notch/pump/recommit as setpoint programs.
- `daemon/main.py` — 20 Hz control loop + REPL. `--mock` flies a point-mass
  model with no DCS/vJoy needed. `python -m daemon.main` from `phase2/`.
- `tests/test_convergence.py` — closed-loop tests, all passing on the mock.

## Your job (in order)

1. **Environment**: verify vJoy driver + `pip install pyvjoy` work; smoke-test
   `python -m daemon.main --mock` first (no DCS needed), then live.
2. **Surface sign check**: jet on autopilot at safe altitude, `fly hdg <current>`
   only. If it rolls/pitches away hard, flip AIL_SIGN/ELEV_SIGN in output.py.
3. **Gain tuning on the real Su-27** (mock-tuned gains are starting points;
   the real jet has different response). Tune ONE loop at a time, inner
   before outer:
   a. Bank first: `fly hdg <current+30>` — oscillating roll → lower `bank_kp`
      or raise `bank_kd`; sluggish → raise `bank_kp`.
   b. VV next: `fly alt <current+2000>` wings-level — porpoising → lower
      `vv_kp` / raise `vv_kd`; steady-state miss → raise `vv_ki`
      (its authority is clamped ±0.5 elevator; widen i_lo/i_hi if truly needed).
   c. Altitude outer: overshoot → lower `alt_kp` or `vv_cap`.
   d. Mach: hunting throttle → lower `mach_ki`.
   e. Turn compensation: if it sinks in hard turns raise `turn_comp`; balloons
      → lower it.
   Use `set <gain> <value>` live, `savegains` when a loop behaves.
4. **Maneuver validation**: crank/notch/pump/recommit at altitude; confirm
   headings/altitudes make sense and recommit restores the commit reference.
5. **Acceptance** (Phase 2 done when): holds hdg ±2°, alt ±100 ft, mach ±0.02
   in cruise; survives a full crank→pump→recommit cycle hands-off; `manual`
   instantly returns authority to the human stick.

## Operational rules — do not violate

- **NEVER run `install_hook.ps1`** (leftover from an earlier session; it
  corrupted the Su-27 input profile once already and re-injects a dead
  export hook). Quarantine or delete it if found.
- **Do not modify `Saved Games\DCS\Config\Input\`** programmatically. Ever.
  Binding vJoy axes in DCS is done BY THE HUMAN in the DCS UI.
- **Do not touch the Tacview line** in Saved Games\DCS\Scripts\Export.lua.
- The human's physical stick stays bound in DCS alongside vJoy — inputs sum,
  centered stick = autopilot authority, grabbing the stick = human override.
  This is deliberate. Do not "clean up" the dual binding.
- Phase 1 telemetry INTERVAL should be 0.05 (20 Hz) for this phase — top of
  ClaudeHarness/Export.lua. That's the ONLY sanctioned edit to Phase 1.
- First live engage: >10,000 ft AGL, wings near level, `manual` command
  explained to the pilot first. It centers vJoy instantly.

## Debug assets

- Receiver from Phase 1 (`../receiver.py`) still works alongside the daemon?
  NO — both bind :27015. Run one or the other. The daemon's `status`/`watch`
  replaces the receiver during Phase 2 work.
- Telemetry stalls: red errors appear in-packet (`State.errors`); dcs.log
  is the fallback.
- If pyvjoy can't find the DLL: it needs vJoy's install dir on PATH or the
  `VJOY_DLL` hint; check vJoy "Configure" shows device 1 with X/Y/Z axes.

## Phase 3 preview (context, not tasking)

Model adapter: Agent SDK on plan auth (ANTHROPIC_API_KEY must be UNSET or it
silently bills the key). Stateless calls: system prompt + ~800-token state
snapshot → one JSON command. 8 s cadence. Command schema: FLY / CRANK / NOTCH /
PUMP / RECOMMIT / RADAR / LOCK / DROP_LOCK / FOX / DEFEND / RTB / HOLD, each
with an `intent` string logged for debrief. The maneuvers module was shaped
so these map 1:1 onto ManeuverManager methods.
