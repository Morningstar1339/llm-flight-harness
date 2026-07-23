# Claude Flight Harness

A model-in-the-loop control system that lets an LLM fly a combat aircraft
(Su-27) in the DCS World simulator.

Telemetry flows out of DCS via a Lua export hook, into a Python control
daemon running a PID cascade autopilot with virtual-joystick actuation,
with an LLM agent layer issuing high-level commands on top. A human pilot
retains physical override at all times.

Built solo as an exercise in the discipline that matters when deploying
AI agents anywhere real: reliability engineering, observability, safety
architecture, and evaluation in a domain where failure is unambiguous —
the aircraft crashes.

## Architecture

    DCS World ──Export.lua──▶ telemetry stream
                                   │
                             Python daemon
                      (PID cascade autopilot, vJoy
                       actuation, systems executor)
                                   │
                             agent layer
                    (LLM issues high-level commands;
                     manual-lookup RAG tool available)

- **Telemetry export** — Lua hook streaming aircraft state, sensor
  contacts, and RWR data out of DCS.
- **Control daemon** (`phase2/daemon/`) — PID cascade autopilot
  (attitude/altitude holds, captures, combat maneuvers), vJoy
  virtual-joystick output, and a systems executor (radar, EOS,
  lock/unlock, weapons with fire interlock, countermeasures).
  Run with `python -m daemon.main` from `phase2/`. Starts in MANUAL;
  `auto` engages, `manual` is the safety word.
- **Agent layer** — model-in-the-loop command interface, including an
  autonomous lock-hunt routine (timed slew + verify + retry).
- **Retrieval tool** (`retrieval.py`) — RAG pipeline over the aircraft's
  tactics manual: documents chunked and embedded into a local Chroma
  vector store (`ingest.py`), exposed as an agent-callable
  `search_manual()` with citation-formatted output and a distance
  threshold that refuses rather than answers from weak matches.

## Safety architecture

Failure mode here is a crashed airframe, so safety was a day-one
constraint rather than a retrofit:

- **Physical-override control summing** — the human stick is always live;
  physical and virtual inputs are summed so the pilot can overpower the
  automation at any moment.
- **Positive exchange of controls** — explicit handoff protocol between
  human and automation, adopted after mid-engagement control blending
  caused a departure from controlled flight.
- **Interlocked weapons commands** — fire commands gated behind an
  explicit authorization interlock.
- **Flight-recorder telemetry logging** — every flight produces analyzable
  logs; the recorder is the primary debugging instrument.

This is a young system: these mechanisms have held in the testing
performed so far, not been exhaustively proven.

## Flight-test methodology

Changes are validated through structured acceptance campaigns (targeted
holds, altitude captures, maneuver sequences), not trial and error.
Representative case: a persistent pitch oscillation in AUTO was
root-caused via flight-recorder analysis to a positive-feedback term in
the pitch cascade (raw measured-AoA feedforward swinging the pitch
target with the aircraft); the fix (slow-filtered AoA) was validated in
flight.

## Status

- Phase 1 — telemetry export: complete, combat-validated
- Phase 2 — inner-loop autopilot and systems executor: complete and
  flight-accepted
- Phase 3 — full model-in-the-loop agent flight: in progress; current
  focus is autonomous target locking (agent-directed radar slew and
  lock verification)
- Manual-retrieval RAG tool: built; daemon integration pending

## Requirements

DCS World (Su-27 module), Python 3.x, vJoy, `chromadb`, `pymupdf`.
See `INSTALL.md` and `phase2/INSTALL_PHASE2.md`.

Note: the indexed manual is copyrighted material and is not included in
this repository; point `rag_docs/` at your own documents and run
`ingest.py`. This project is not affiliated with Eagle Dynamics.

## Author

David Shaffer — [github.com/Morningstar1339](https://github.com/Morningstar1339)