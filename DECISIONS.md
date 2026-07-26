# DECISIONS.md

Design decisions taken during the unattended run of 2026-07-25 (MISSION.md,
Tasks 1 and 2). One entry per decision that had more than one reasonable
answer. Each records the alternatives, why this one, and how to reverse it.

Bias throughout: reversible over clever, and refuse over guess.

---

## 1. Model SDK: `claude-agent-sdk` on plan auth (Ruling 2)

**Decision.** `phase2/daemon/model_client.py` uses `claude-agent-sdk` (0.2.128),
which drives the Claude Code CLI already installed on this box (Node 22).

**Alternatives.** (a) The raw `anthropic` Messages API with a hand-written tool
loop. (b) Managed Agents. (c) Shelling out to the `claude` CLI directly.

**Why.** CLAUDE.md already names this path ("Agent SDK on plan auth") and it is
the officially supported one that works on **plan auth**, which is how this box
is set up — the raw Messages API would require an API key, i.e. a second
billing relationship for a hobby project. Managed Agents runs the loop on
Anthropic's infrastructure with a hosted container, which is the wrong shape
for something that must talk to a UDP socket on this PC. Shelling out to the
CLI means parsing human-facing output.

**How to reverse.** Replace `ClaudeAgentSDKClient` in `model_client.py`. Nothing
else imports an SDK — `test_agent_module_does_not_import_an_sdk` asserts this,
and the import is inside `__init__` so the daemon boots on a box with no SDK.
A raw-API implementation needs one method: `decide(snapshot) -> str`.

---

## 2. A `PilotClient` seam between the loop and any model

**Decision.** The agent loop never imports an SDK. It calls
`client.decide(snapshot) -> str`. Two implementations: `ScriptedClient` (tests)
and `ClaudeAgentSDKClient` (real).

**Alternatives.** Call the SDK directly from `AgentPilot` and mock at the
network layer in tests.

**Why.** Everything that decides whether a command reaches the aircraft —
parsing, validation, authority, the fire interlock — is then testable with zero
model calls and zero cost, which is what let the suite grow to a real gate. It
also makes decision 1 a one-file change.

**How to reverse.** Inline the SDK calls into `AgentPilot.step()`. You will lose
the offline test suite; don't.

---

## 3. Command schema: one JSON object (or a short array), with `intent` (Ruling 3)

**Decision.**

```json
{"command": "CRANK", "args": {"direction": "left"}, "intent": "cut closure"}
```

An array of up to 3 executes in order. Commands are declared once in the
`COMMANDS` table in `agent.py`, which also generates the system prompt's command
reference via `command_reference()`.

**Alternatives.** (a) A DSL resembling the REPL line format (`crank l 45`).
(b) One tool call per command through the SDK's tool mechanism. (c) Structured
outputs / a JSON schema enforced by the API.

**Why.** JSON parses unambiguously and the validator can be strict about it. The
REPL format is for humans and is positional, so a dropped argument silently
shifts meaning. Emitting commands as SDK *tool calls* would put actuation behind
the tool registry, which is exactly the boundary the safety design keeps them
out of (see 15). Generating the prompt from the table means the prompt and the
validator cannot drift apart.

**How to reverse.** The table is data. Add, remove, or re-arg a command by
editing `COMMANDS`; `command_reference()`, validation, and the prompt follow.
The dispatch branch in `AgentPilot._execute` is the only other place to touch,
and `test_every_command_has_a_dispatch_path` catches a miss.

---

## 4. Exactly the twelve commands from the Phase 3 preview

**Decision.** FLY, CRANK, NOTCH, PUMP, RECOMMIT, RADAR, LOCK, DROP_LOCK, FOX,
DEFEND, RTB, HOLD — the set CLAUDE.md promised, nothing more.

**Alternatives.** Add EOS, WPN, AIRBRAKE, DISPENSE, raw `btn`, slew control.

**Why.** "Strictly additive on top of the existing MANUAL/AUTO structure" reads
as *don't invent surface*. Every extra verb is another thing to gate and another
way to be surprised in flight. `dispense` is reachable via DEFEND; the rest are
reachable by the human at the console.

**How to reverse.** Add a `CommandSpec` to `COMMANDS` and a branch in
`_execute`. Note that anything touching the flight path needs
`needs_engaged=True`.

---

## 5. The model speaks aviation units; SI conversion happens in one place

**Decision.** Degrees, feet, mach across the model boundary, converted in
`AgentPilot._execute` only.

**Alternatives.** SI across the boundary, matching the daemon's internals.

**Why.** The REPL already speaks aviation units, so the human reading a debrief
log sees the same numbers the model saw. Doctrine text and the manual are in
aviation units too. Mixing them anywhere but the boundary is how you get a
6,000-foot climb command interpreted as 6,000 metres.

**How to reverse.** Delete the `* FT` / `math.radians` calls in `_execute` and
change the range limits in the `ArgSpec`s. Tests assert the conversion
(`test_fly_sets_setpoints_in_si`).

---

## 6. Out-of-range arguments are refused, never clamped

**Decision.** `{"alt": 200000}` is rejected with an explanatory error. It does
not become "climb to 60,000 ft".

**Alternatives.** Clamp to the envelope and fly the clamped value.

**Why.** A number that far off is evidence the model has lost the plot, not a
slightly enthusiastic request. Clamping hides that and flies *something*;
refusing is loud, the model sees the refusal, and the fallback is HOLD — which
is safe. This costs one cycle in the worst case.

**How to reverse.** In `validate_command`, replace the range check's `return
None, ...` with a clamp. Nine tests assert the refusal
(`test_out_of_range_is_refused_not_clamped`).

---

## 7. A missing `intent` is a warning, not a refusal

**Decision.** `intent` defaults to `"(no intent given)"` and the command still
executes, with a warning surfaced to the operator.

**Alternatives.** Refuse the command outright (the strict reading of "each with
an `intent` string logged for debrief").

**Why.** Refusing a defensive maneuver because a log string was missing is worse
than logging a maneuver without a reason. The prompt asks for it clearly and the
warning is visible in the REPL.

**How to reverse.** In `validate_command`, return the error instead of appending
a warning.

---

## 8. Authority is three states, not a boolean

**Decision.** `off` / `advisory` / `active`, orthogonal to MANUAL/AUTO.

**Alternatives.** A single `agent_enabled` flag.

**Why.** `advisory` is how you watch the model think for a whole flight before
you let it touch anything — decisions are parsed, validated, logged, and
displayed, but `dispatch` refuses them. That is the cheapest possible way to
build confidence in a system whose failure mode is a crashed airframe, and a
boolean has no room for it.

**How to reverse.** Treat `advisory` as `off`; the state machine collapses
cleanly. Don't remove the state itself — first live use should be advisory.

---

## 9. `agent fly` requires AUTO; systems commands do not

**Decision.** Promotion to `active` is refused while the daemon is in MANUAL.
Once active, commands marked `needs_engaged` (the ones that move the flight
path) refuse if the human has taken the jet back; RADAR / LOCK / DROP_LOCK still
work.

**Alternatives.** (a) Require AUTO for everything. (b) Require it for nothing.

**Why.** The flight path has exactly one owner at a time, and MANUAL means it is
the human — that is the positive-exchange-of-controls rule this project already
adopted after a control-blending departure. Sensors are not the flight path,
though, and "human flies, agent runs the radar" is a genuinely useful mode
during work-up.

**How to reverse.** `needs_engaged` is a per-command flag in `COMMANDS`. Set it
on every spec to get option (a).

---

## 10. Mid-plan revocation via an authority epoch

**Decision.** Every authority change increments `AgentPilot._epoch`.
`execute_plan` captures the epoch when the plan starts and each `dispatch`
refuses if it no longer matches.

**Alternatives.** (a) Check only `authority != ACTIVE` at dispatch time.
(b) Kill the agent thread on revocation.

**Why.** (a) has a real hole: revoke, then re-arm, and a plan built under the old
authority resumes as if nothing happened — `test_stale_epoch_is_refused_even_if
_authority_is_active_again` covers exactly this. (b) cannot be done safely; you
cannot kill a Python thread mid-call, and a thread killed between "press lock"
and "release slew" leaves a button held down.

**How to reverse.** Pass `epoch=None` to `execute_plan`. You lose the stale-plan
guarantee.

---

## 11. The authority gate and the actuation share one lock

**Decision.** `dispatch` holds `AgentPilot._lock` across both the gate check and
the command's execution.

**Alternatives.** Check the gate, release the lock, then act.

**Why.** Otherwise revocation can land *between* the check and the act, and the
command flies anyway. Holding the lock means a revocation lands between
commands, never inside one. This is only safe because every dispatch action is
non-blocking — setpoint writes, and `sysx.execute` / `lm.lock_contact`, which
both spawn a worker and return immediately.

**How to reverse.** Don't, without re-checking that invariant. If a future
command needs to block, run it on a worker and have the worker re-check the
epoch before it actuates.

---

## 12. Fire authorization: console-typed, single-use, target-specific, expiring (Ruling 4)

**Decision.** `authorize fire <target_id> [seconds]` at the REPL mints one
`FireAuthorization`. The agent's FOX consumes it, and release requires **all**
of: authority `active`, an unconsumed unexpired authorization for that exact
target, and an existing lock on that target. Default TTL 120 s. A refusal does
**not** consume the authorization. `manual`, `deauthorize`, any authority
change to off, and the envelope guard all clear it.

**Alternatives.** (a) A standing "weapons free" mode. (b) A confirm prompt at
the moment the agent asks. (c) Requiring the human to press the physical
trigger, with the agent only recommending.

**Why.** (a) is autonomous fire with extra steps. (b) blocks the REPL thread on
input mid-engagement, which is the worst possible time to make the console
unresponsive, and it makes the authorization a reflex rather than a decision.
(c) is the most conservative reading, but the ruling explicitly says "Design the
arm/authorize command as you see fit; the trigger is a human act" — typing an
authorization for a specific target, valid for a bounded window, *is* that act,
and it keeps the human's hands off the stick during the shot.

Single-use and target-specific are what stop one authorization becoming a
magazine. The TTL is what stops an authorization granted during one merge being
spent three minutes later in a different one.

**How to reverse.** `FireAuthorization` is self-contained. For a standing mode,
stop consuming it in `_fox` and set a long TTL. For (c), delete `_fox`'s release
branch and have it always refuse with a recommendation — the tests for the
refusal path stay valid.

---

## 13. FOX does not require AUTO

**Decision.** `FOX` has `needs_engaged=False`.

**Alternatives.** Require the autopilot engaged to release.

**Why.** The gate that matters is the human authorization, and it is already
strictly stronger. Coupling weapon release to autopilot engagement would mean
the human cannot hand-fly the shot while the agent runs the intercept, which is
a plausible and reasonable way to fly this. Flagged as a judgment call because
the opposite is defensible.

**How to reverse.** Set `needs_engaged=True` on the FOX spec. One flag.

---

## 14. The envelope guard revokes agent authority

**Decision.** `Daemon.emergency_disengage()` (extracted from the control loop,
same message text) revokes the agent before it disengages the autopilot.

**Alternatives.** Leave agent authority alone; only the autopilot drops out.

**Why.** An envelope violation means the inner loop's output is garbage and
often pro-spin. An agent still issuing setpoints into that has nothing
underneath it to fly them, and the human is busy recovering. This is the same
reasoning as the `manual` safety word, applied to the automatic case.

**How to reverse.** Remove the `self.pilot.revoke(...)` line in
`emergency_disengage`.

---

## 15. Tools are lookups; actuation is never a tool

**Decision.** Everything in `ToolRegistry` is `read_only`. The model changes the
aircraft only through the command schema, which is gated by the authority state
machine and the fire interlock.

**Alternatives.** Expose `fly`, `lock`, `fire` as SDK tools and let the model
call them directly.

**Why.** Two paths to actuation means two places to get the gating right, and
the tool path runs inside the SDK's loop where our epoch/authority checks are
not. One gated path is auditable; two are not.

**How to reverse.** Don't. If you must, put the gate inside the tool handler and
make it consult `AgentPilot`, and expect the mid-plan revocation guarantee to
need rethinking.

---

## 16. `retrieval.py`: lazy, repo-anchored, and it never raises (Ruling 1)

**Decision.** The Chroma client opens on first query, at a path derived from
`__file__`, and every entry point returns a string rather than raising. A failed
load is **not** cached.

**Alternatives.** Keep the module-level client; cache the failure.

**Why.** The daemon imports this module through the registry, so an import-time
raise would have taken down the daemon at boot on any box without an index — and
`chroma_db/` is gitignored, so that is the state of every fresh clone. Anchoring
to `__file__` fixes a live bug: the daemon runs from `phase2/`, so the old
relative `"chroma_db"` resolved to `phase2/chroma_db` and silently missed. Not
caching the failure means running `ingest.py` while the daemon is up works on
the next call without a restart.

`MAX_DISTANCE` and the citation format are untouched, as instructed.

**How to reverse.** Restore the module-level `PersistentClient`. You will
re-acquire both bugs.

---

## 17. Strict argument validation, including unknown keys

**Decision.** Tool and command arguments reject keys the schema does not
declare, rather than ignoring them.

**Alternatives.** Ignore unknown keys (the permissive JSON-Schema default).

**Why.** A model that writes `quesion=` should be told, not silently handed the
default for the argument it meant to set. Silent defaulting in a command that
moves an aircraft is how you get a crank with the wrong offset.

**How to reverse.** Set `additionalProperties: true` in the schema, or delete the
check in `validate_arguments`.

---

## 18. A tool call never raises into the flight loop

**Decision.** Unknown tool, bad arguments, unavailable index, handler exception,
even a failing availability probe — all become a `ToolResult(ok=False, text=...)`.

**Alternatives.** Let exceptions propagate and catch them at the loop.

**Why.** The catch-at-the-top approach loses which tool failed and why by the
time it is reported, and one missed catch stops the agent mid-engagement. The
model can read a refusal string and try something else; it cannot read a
traceback.

**How to reverse.** Remove the try/except in `ToolRegistry.call`.

---

## 19. SDK built-in tools are disabled

**Decision.** `ClaudeAgentSDKClient` passes `tools=[]` and an explicit
`allowed_tools` list containing only the harness registry's MCP tool names.

**Alternatives.** Leave the SDK defaults (Read, Write, Edit, Bash, Glob, Grep,
WebSearch, WebFetch).

**Why.** A flight agent has no business with a filesystem or a shell. This is
also the difference between "the model can consult the manual" and "the model
can edit the daemon it is running inside".

**How to reverse.** Pass a `tools` preset in the `ClaudeAgentOptions` factory.
Think hard first.

---

## 20. Registry tools reach the model as an in-process MCP server

**Decision.** `_build_sdk_tools` wraps each `ToolSpec` with the SDK's `@tool`
decorator and serves them from `create_sdk_mcp_server`, so the model can call
`search_manual` mid-decision.

**Alternatives.** Pre-fetch likely manual passages into the snapshot; or run a
separate tool-calling loop ourselves.

**Why.** This is Ruling 1's "Task 2's loop consumes that registry", and
retrieval is only useful if the model can decide *when* it needs doctrine.
Pre-fetching means guessing the question. In-process means no subprocess, no
port, and the same `ToolRegistry.call` path the tests exercise.

**How to reverse.** Drop `mcp_servers` from the options and inject
`registry.call("search_manual", ...)` output into the snapshot instead.

---

## 21. Refuse to construct the SDK client when `ANTHROPIC_API_KEY` is set

**Decision.** `ClaudeAgentSDKClient.__init__` raises unless
`allow_api_key=True`.

**Alternatives.** Warn and continue.

**Why.** CLAUDE.md records that with the key set the SDK bills the key instead
of using plan auth, *silently*. A warning in a scrolling REPL during flight prep
is not going to be read. The escape hatch exists for whoever genuinely wants it.

**How to reverse.** Pass `allow_api_key=True`, or delete the check.

---

## 22. Default model `claude-opus-5`, 8 s cadence, 6 turns per decision

**Decision.** Constants in `model_client.py` / `agent.py`.

**Why.** 8 s is CLAUDE.md's stated cadence. The 6-turn cap bounds one decision
so a tool-call loop cannot run away between cycles. Opus 5 because judgment
quality is the point of the whole exercise; cost is a few decisions per minute.

**How to reverse.** All three are constructor arguments —
`ClaudeAgentSDKClient(model=..., max_turns=...)`, `AgentPilot(cadence_s=...)`,
or `agent run` / `run(cadence_s=)`.

---

## 23. pytest, with the assert-scripts kept runnable

**Decision.** pytest is the runner (approved). `phase2/tests/conftest.py` puts
`phase2/` and the repo root on `sys.path`; `test_convergence.py` keeps its own
`sys.path` insert and its `__main__` block.

**Why.** The mission approved pytest and asked that the assert-scripts stay
runnable. Verified: `python tests/test_convergence.py` still prints
`ALL PHASE 2 TESTS PASSED`.

**How to reverse.** Delete `conftest.py`; the new test files each carry the same
`sys.path` insert.

---

## 24. Retrieval is tested against an injected fake collection

**Decision.** Citation formatting, the distance threshold, and graceful
degradation are tested with a fake Chroma collection. One integration test runs
against a real index and skips when `chroma_db/` is absent.

**Alternatives.** Require a built index for the suite; or skip retrieval tests
entirely without one.

**Why.** `chroma_db/` is gitignored, so a suite that needs it is not a gate on a
fresh clone or in CI. The fake pins the behaviour that is actually ours (the
threshold, the format, the refusal); the skipped test covers the wiring to a
real store when one exists.

**How to reverse.** Delete the fixtures and mark the module `skipif` on
`CHROMA_PATH`.

---

## 25. Tests fly the real `Daemon`, not a stub

**Decision.** `test_agent.py`'s fixture builds `Daemon(mock=True, gains_path=None)`
with `monkeypatch.chdir(tmp_path)`, and several tests drive the real `repl()`
over a scripted stdin.

**Alternatives.** A hand-built harness object exposing the same attributes.

**Why.** A stub drifts from `Daemon` silently, and the `manual` safety word lives
in `repl()` — testing a reimplementation of it would prove nothing. `chdir` keeps
`systems.json` and `logs/` out of the repo.

**How to reverse.** N/A; if `Daemon.__init__` grows something expensive, the
fixture is the place to intervene.

---

## 26. An unreadable `systems.json` is fatal, but legible

**Decision.** `SystemsExecutor.__init__` now raises a `RuntimeError` naming the
file and the fix, instead of a bare `PermissionError` / `JSONDecodeError`.

**Alternatives.** Fall back to `DEFAULT_MAP` and warn.

**Why.** Falling back looks friendlier and is more dangerous: a `fire` the
operator had rebound to button 15 would silently go back to pulsing button 6.
For a bindings file, refusing to boot is the safe answer. Only the legibility
changed.

**How to reverse.** Catch and fall back in `__init__`. Two tests assert the
current behaviour and would need deleting, which is the point.

---

## 27. `DEFEND` beams the threat the short way round

**Decision.** With a bearing, `DEFEND` dispenses countermeasures and then notches
to whichever of threat±90 is the smaller turn from the current heading.

**Alternatives.** Always notch left; or require the model to pick a direction.

**Why.** DEFEND is the one command issued when there is no time to think, so it
should not need an extra decision, and the shorter turn gets to the beam sooner.
Deterministic, so it is testable.

**How to reverse.** Add a `direction` `ArgSpec` to the DEFEND spec and use it.

---

## Known gaps, deliberately left

- **The "at most 3 commands" limit is prompt-only.** `parse_commands` accepts an
  array of any length. Every element is still validated and gated individually,
  so the risk is a long plan, not an ungated one. Enforce in `parse_commands` if
  it ever matters.
- **`extract_json` silently keeps only the first JSON value in a response.**
  Observed live in GT-03: the model emitted a bare object followed by an array;
  `raw_decode` took the object and `parse_commands` returned no error, so a FOX
  and a HOLD the model believed it had issued never reached the human. Fail-safe
  in direction, silent in character. Unfixed and undecided — the options are to
  keep first-value-wins but *report* trailing content as an error, or to decode
  repeatedly and concatenate. Measured, not yet chosen.
- **The model asserts doctrine the manual does not contain.** GT-03 measured
  (a)=0 / (b)=4 / (c)=2, and a second independent run reproduced those counts
  **exactly** on the same scenarios. Root cause is corpus coverage — the indexed
  32-page operator's guide has no employment-range content, so `search_manual`
  correctly returns nothing and the model fills the silence anyway. Pooling five
  samples of the shot-request scenario, **2 of 5 FOX requests carried an uncited
  envelope justification**, 1 of 5 hedged correctly, 2 of 5 were neutral
  readback. Not an agent-layer bug; the fix is some combination of a better
  corpus, a harder prompt rule, and possibly a mechanical one (e.g. refuse a FOX
  whose intent makes a range claim unsupported by a tool result that turn).
  Deliberately undecided — measured, not yet chosen.
- **Retrieval is very sensitive to query phrasing.** Run 1's queries hit p.11
  and p.22; run 2's 14 calls over the same five scenarios returned nothing at
  all. Same corpus, same scenarios. Before tuning the prompt, it is worth
  knowing whether the corpus or the embedding is the weaker link.
- ~~**`ClaudeAgentSDKClient` has never made a real call.**~~ **Closed
  2026-07-25** — GT-02 passed with no prompt changes. See the RESULT block on
  that card.
- **The `run()` loop's cadence is a gap, not a period.** `AgentPilot.run()`
  waits `cadence_s` *after* each decision returns, so with the ~25 s decision
  latency measured in GT-02 the effective period is ~33 s against CLAUDE.md's
  ~8 s intent. Left as-is rather than silently changed: the fix could be a
  lower `effort`, a bigger `cadence_s`, or `wait(cadence - elapsed)`, and which
  one is right depends on how the model actually behaves in the air. FT-08
  decides.
- **No rate or cost ceiling on the agent loop.** `agent run` with no cycle count
  runs until revoked. `max_budget_usd` exists in `ClaudeAgentOptions` and is not
  wired up.
- **The snapshot is not token-counted.** It is built to be compact (~40 lines
  with a busy picture) but nothing enforces the ~800-token target from CLAUDE.md.
