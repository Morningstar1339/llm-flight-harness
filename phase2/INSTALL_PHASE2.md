# Phase 2 install — vJoy + daemon

## 1. Sanity check with zero installs

From the `phase2` folder:
```
python -m daemon.main --mock
```
Then at the prompt: `fly hdg 270 alt 20000 mach 0.9`, then `watch 10`.
You should see the mock jet bank and climb toward targets. `quit` when done.
This proves the whole stack minus vJoy/DCS.

## 2. Install vJoy (deliberately, this time)

1. Download vJoy from its SourceForge/GitHub release (v2.1.9.1 or later works
   on Win10). Install with defaults.
2. Open "Configure vJoy": Device 1 enabled, axes X, Y, Z ticked. Apply.
3. `pip install pyvjoy`

## 3. Bind vJoy in DCS — by hand, in the UI

Options → Controls → Su-27 → Axis Commands. You'll now see a vJoy Device
column. Bind: **Pitch → vJoy Y, Roll → vJoy X, Thrust → vJoy Z** (click cell →
Axis Assign → nudge the axis via the daemon, or assign from the dropdown).

**Leave your T.Flight bindings exactly as they are.** DCS sums devices on a
shared axis: your stick centered = autopilot flies; grab the stick = you're
blended in immediately. That's the manual-override design, not a mistake.

Check the vJoy Thrust axis curve: if idle/full are reversed, tick "Invert"
on that axis in DCS rather than editing code.

## 4. Bump telemetry to 20 Hz

Top of `Saved Games\DCS\Scripts\ClaudeHarness\Export.lua`:
`INTERVAL = 0.5` → `INTERVAL = 0.05`. The 2 Hz rate was fine for eyeballing;
the control loop wants 20.

## 5. First live flight protocol

1. Close `receiver.py` if running (daemon and receiver share port 27015).
2. Su-27 free flight, climb to ~15,000 ft, trim for level-ish, hands off.
3. From `phase2`: `python -m daemon.main`
4. `status` — confirm live heading/alt/mach match the cockpit.
5. `fly hdg <your current heading>` — nothing dramatic should happen.
   If it snap-rolls or dives: `manual`, then flip AIL_SIGN/ELEV_SIGN in
   `daemon/output.py` per which surface misbehaved, restart, retry.
6. Then `fly hdg <current+30>` and watch it turn. From there, tuning —
   which is Claude Code's project (see CLAUDE.md), with you as test pilot.

`manual` = instant human authority (centers vJoy). It's the safety word.
