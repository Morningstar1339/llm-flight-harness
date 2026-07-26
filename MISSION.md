MISSION: Complete all remaining CODE work for this harness in ONE
unattended run. This file supersedes all earlier instructions. Do not
stop to ask questions — every decision is either ruled on below or
delegated to your judgment under the DECISIONS.md policy.

── RULINGS on your four questions ──
1. Task 1 target: Option 1. Create the tool-registration module
   (phase2/daemon/agent_tools.py or similar), register search_manual
   as its first tool, test registration/cited-query/refusal with zero
   model calls. Task 2's loop consumes that registry. Your retrieval.py
   plumbing fix is approved: lazy, repo-root-anchored Chroma init,
   MAX_DISTANCE and citation format untouched. Graceful degradation if
   chroma_db/ is absent (it's gitignored) — tool reports unavailable,
   daemon still boots.
2. SDK choice: your call. Prefer the officially supported Agent SDK
   path viable on this box (Node 22 + Claude Code CLI present).
   Optimize for reversibility.
3. Command schema: your call. Constraints: agent commands are strictly
   additive on top of existing MANUAL/AUTO structure; the `manual`
   safety word instantly and unconditionally revokes all agent
   authority and restores current MANUAL behavior.
4. Weapons authorization: the agent NEVER fires autonomously. It may
   recommend or request fire; actual release requires an explicit
   per-engagement authorization entered by the human at the console
   (extend the existing FOX interlock). Design the arm/authorize
   command as you see fit; the trigger is a human act. Include a test
   proving an unauthorized agent fire request is refused.

── ENVIRONMENT RULINGS ──
- System Python 3.14 is the project environment on this box. Installing
  pytest, the SDK, and anything else the tasks require into it is
  approved. List every install in the final report.
- pytest as test runner is approved; keep existing assert-scripts
  runnable.

── TASKS (in order, commit after each goes green) ──
TASK 1: Tool registry + search_manual wiring per Ruling 1.
TASK 2: Phase 3 agent loop per Rulings 2–4, consuming the registry,
tested against mocked telemetry, including: `manual` strips agent
authority mid-command, and unauthorized fire refusal.

Green = full test suite under phase2/tests with --mock (no DCS, no
vJoy, no live telemetry). Strengthen the suite wherever it's too weak
to be a real gate.

── DECISIONS.md POLICY (replaces stop-and-ask) ──
For every design decision with more than one reasonable answer, decide
yourself, then log in DECISIONS.md at repo root: the decision, the
alternatives, why, and how to reverse it. Bias toward reversible
choices. This file is how I review your judgment afterward.

── HARD CONSTRAINTS (you run unattended; these ARE the safety system) ──
1. FILESYSTEM PERIMETER: read/write ONLY inside this repo. Never touch
   Saved Games, the DCS install, vJoy config, or any outside path —
   not even to read. I deploy to the sim manually.
2. Never launch the daemon against live DCS/vJoy. --mock only.
3. Do not alter existing MANUAL/AUTO control behavior or the
   flight-critical path beyond minimum required plumbing.
4. Git: commit freely on feature/agent-retrieval. Never push, merge,
   touch main, or run destructive git ops.
5. Out of scope: anything requiring a live flight. Write those as
   test cards (precondition / procedure / pass-fail) in
   FLIGHT_TEST_PLAN.md at repo root.

── FINAL REPORT ──
Files touched (one line each); packages installed; commits; what I
restart to pick it up; DECISIONS.md summary; honest uncertainty list —
what you'd watch most closely on first live run.