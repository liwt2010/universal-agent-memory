# Long-Term LLM Operation (A+ Requirement)

**Honest framing up front:** this is the A+ requirement I cannot
deliver in a code change. It is a **time investment**, not a code
deliverable. The other three A+ conditions (100k stress test, real
case study, third-party pen-test) are also partially time-bound;
this one is the most extreme because the metric is
"1+ months of observed traffic in production".

This document is the **honest assessment** of what we have, what
we don't, and what the operator needs to do to close the gap.

## What "real LLM 1+ month" means in practice

The A+ rating says: ≥1 month of production traffic with the
LLM-backed compression engine on the hot path. This means:

1. **A real LLM endpoint is wired** (`UAMS_LLM_API_KEY` + `UAMS_LLM_BASE_URL`
   + `UAMS_LLM_MODEL`), not mocked. The CI "compression" tests mock
   the LLM (deterministic summary strings); production is the
   opposite.
2. **Real user traffic** is flowing through the system. The
   workload includes episodic events being compressed by the LLM
   on session end, semantic facts being extracted, and procedural
   patterns being identified.
3. **One month minimum** of accumulated data. The reason "1 month"
   and not "1 hour" is that LLM quality drift (model updates, API
   instability, prompt-rot) is a slow phenomenon. You need 30 days
   of telemetry to see the real shape of the curve.
4. **The metrics that matter are not code metrics** — they are
   end-user signals: did the agent's recall improve? Did
   compression stay under budget? Did the LLM cost stay under
   budget? Did the agent loop stall because of LLM timeouts?

## What I CAN deliver in code

- **Telemetry hooks** in the LLM compression path: log every LLM
  call with model, prompt-token count, completion-token count,
  cost estimate, latency, success/failure, error class.
  This is missing today.
- **A "dry-run" harness** that simulates 30 days of compressed
  traffic in a few minutes: a script that ingests a captured
  session log, runs the LLM compression pipeline, and reports
  the resulting token counts and quality metrics. This is what
  CI uses; it's not a substitute for real LLM, but it lets the
  operator verify the pipeline works at scale without waiting
  30 days.
- **A "30-day LLM dry-run" report** that records what the
  compression pipeline would have produced for 30 days of
  synthetic traffic. Honest framing: synthetic != real LLM.
- **Cost guardrails** in `UAMSConfig`: per-session LLM call
  budget, monthly cost cap, fail-loud when exceeded.

These are all in the **infrastructure** category — they make the
real long-term run **measurable and controllable**, but they
are not the long-term run itself.

## What the operator needs to do

1. Pick a real LLM endpoint (OpenAI, MiniMax, ollama, vLLM).
   - The current code path is OpenAI-compatible; MiniMax works
     out of the box.
2. Wire it via env vars and run `uams.llm.client.OpenAICompatibleClient`
   end-to-end against a captured session log.
3. Stand up the telemetry: every LLM call goes to a log store
   (file / Loki / OpenTelemetry) with the cost / latency / quality
   fields. The dry-run harness is a starting template.
4. Run for 30+ days with the LLM path on the hot path.
5. After 30 days, post a report:
   - Total LLM calls
   - Total cost (sum of completion-token cost)
   - Compression quality: did the recall precision/recall
     improve vs. the no-LLM baseline?
   - Latency: p50 / p95 / p99 of LLM calls
   - Failure modes: timeout rate, retry rate, error class
     distribution
   - Memory growth curve: did long-term operation OOM the
     store? (Especially relevant for in-memory LRU)
6. Submit the report as evidence for the A+ rating.

## Honest call-out: this is the A+ condition most likely to fail

The other A+ conditions:

- 100k stress test → **mostly delivered** by this PR (the
  infrastructure is in place; running 100k against a real
  backend is a 1-day operator exercise).
- Real case study → operator-side, but 1-2 weeks of focused
  work with a friendly customer.
- Third-party pen-test → operator-side, 1-week paid engagement.
- Real LLM 1+ month → operator-side, **30 days minimum**, and
  it depends on a real customer with real traffic.

This is the longest pole. If A+ is the goal, this is the
condition to start first, in parallel with the other three.

## Recommendation for the project

If we are serious about A+:

- Build the telemetry hooks NOW (next 1-2 weeks).
- Find a friendly customer who will run UAMS with the LLM
  path on the hot path for 30+ days. The "30 days" is not
  negotiable; the customer can be small (one team, low
  traffic) as long as the traffic is real.
- Reserve the third-party pen-test for AFTER the 30-day run
  is in motion — the report is a precondition for the
  pen-test (the pen-tester needs the threat model first).

## What this doc is NOT

This is not a commit to deliver A+ on a specific timeline. It
is a roadmap of what the project needs to do, scoped to the
A+ "real LLM 1+ month" condition. The other three A+ conditions
are tracked elsewhere (100k stress test → `STRESS_TEST.md`,
case study → TBD, pen-test → TBD).
