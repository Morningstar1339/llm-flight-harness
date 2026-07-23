# ClaudeHarness Phase 1 — Install

## 0. Verify the receiver first (no DCS needed)

In one terminal:
```
python receiver.py
```
In a second terminal:
```
python test_sender.py
```
You should see a hello, status lines, a red lua error, a GAP warning, a STALL
warning, and a goodbye. This proves Python + the port work on your machine
before DCS enters the picture. Ctrl+C to stop the receiver.

## 1. Install the export script

Copy the `ClaudeHarness` folder to:
```
%USERPROFILE%\Saved Games\DCS\Scripts\ClaudeHarness\
```
(so the file ends up at `...\Scripts\ClaudeHarness\Export.lua`)

## 2. Chain it into Export.lua — ORDER MATTERS

Open (or create) `%USERPROFILE%\Saved Games\DCS\Scripts\Export.lua`.

If Tacview / SRS / anything else is already in there, LEAVE THEIR LINES ALONE.
Add this line at the **very bottom**, after everything else:

```lua
dofile(lfs.writedir()..[[Scripts\ClaudeHarness\Export.lua]])
```

Why bottom: my script saves and calls whatever hooks were installed before it.
Loaded last, it wraps Tacview instead of fighting it — both streams run.

## 3. Fly

1. Start `python receiver.py` **before** the mission.
2. Launch DCS, start any mission (free flight is fine).
3. On mission start you should see `mission export started (hello)` and then
   a status line every 0.5s: heading, altitude, IAS, mach, G, fuel,
   contact/RWR counts, LOCK flag.
4. `--raw` flag prints full JSON per packet instead of the status line.
5. Everything is also appended to `telemetry.jsonl` — send me that file
   (or `Saved Games\DCS\Logs\ClaudeHarness.log`) if anything looks wrong.

## What the warnings mean

- **red `lua:` lines** — a specific DCS export function failed on that frame.
  The stream is fine; that field is just absent this packet. Chronic ones we
  fix by adjusting that one getter.
- **GAP** — UDP packets lost (rare on localhost; interesting if it happens).
- **STALL** — no packets for 2s while a mission is running. With this script
  that should essentially never happen; if it does, grab dcs.log.

## Knobs (top of ClaudeHarness\Export.lua)

- `INTERVAL = 0.5` — packet period in seconds. We'll drop this when the
  autopilot needs fast state; 2 Hz is right for Phase 1 eyeballing.
- `PORT = 27015` — must match `receiver.py --port`.

## Known limits (fine for Phase 1)

- Contact/RWR detail varies by module; the Su-27's export surface is decent
  but some fields will be nil in some radar modes. That's expected — the
  errors channel tells us exactly which, and we tune from evidence.
- In multiplayer, servers can disable sensor/object export. Single player
  is unrestricted. (Matches the phase plan: prove it in SP first.)
