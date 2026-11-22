# Final Engineering Summary

## Plan and rationale

The assignment ("Build an Agentic Software Engineering System — URL
Shortener") asks for an **agentic orchestration engine** that automates the
SDLC lifecycle with a real dependency graph, parallel execution, governance,
and reliability controls — demonstrated against a target system, not the
target system itself. So the URL shortener is built as a real, tested
service for the orchestrator to work on, and the orchestrator is the main
deliverable.

**Sequencing**: the URL shortener app was built first, in isolated phases
(persistence, validation, analytics, expiration, integration tests,
observability, Docker), each committed only once its own tests passed —
17 commits. That work stands as the orchestrator's *target system*: real
code with real tests and real architectural decisions worth reasoning
about, rather than a toy stub. The orchestrator was then built the same
way — 14 further commits, each a working, tested increment: DAG engine
core → gates/policy → reliability → approval → replanning → observability
→ agents → stage wiring → three real scenario demonstrations → this
documentation. Every commit's tests were green before the next commit
started; where a real scenario run surfaced a bug, the fix and a
regression test landed in their own commit before re-running.

**Key design decisions** (see `ARCHITECTURE.md` for the mechanics):

1. **Python for the orchestrator, Java for the target** — deliberately two
   stacks in one repo, mirroring how a real control-plane/target-system
   split works, rather than forcing the orchestration engine into Java
   just because the target app is Java.
2. **Four narrow extension points on the scheduler** (gate evaluator,
   retry executor, approval manager, event sink), each with a
   pass-through default — every later engine phase is a real
   implementation of an existing seam, not a scheduler rewrite. This is
   why 8 engine-phase commits could each be small and independently
   testable against the same scheduler.
3. **`DeterministicAgent` for every demo run, `ClaudeAgent` unused** —
   the assignment's scenario runs needed to be reproducible without a paid
   API key or network access. The `Agent` protocol keeps live-LLM
   reasoning a config change away, not a redesign, and `ClaudeAgent`'s
   prompt-building and response-parsing are unit-tested via an injected
   fake client so it isn't unverified code.
4. **Scenario branches, never merged to `main`** — matches the
   assignment's "humans own... final quality" principle: nothing is
   merged without a human decision. Each scenario's real Java change is
   on `orchestrator-demo/<scenario>`, pushed, left for explicit review.

## Artifacts produced

- **The orchestrator itself**: `orchestrator/src/orchestrator/` —
  engine (7 modules), observability (3), agents (3), stages (2),
  scenarios (6) — 125 tests, offline, ~1 second.
- **Five real, verified scenario runs**, each on its own branch with its
  own `events.jsonl` / `dashboard.html` / `SUMMARY.md` under
  `orchestrator/runs/<scenario>/` on `main`:
  - `orchestrator-demo/greenfield-qr-code` — new `GET /{shortCode}/qrcode`
    endpoint. 84 Java tests pass on the branch (81 pre-existing + 3 new).
  - `orchestrator-demo/brownfield-rate-limit` — rate limiting on URL
    creation, extending the existing 429 error-handling pattern. 83 Java
    tests pass (79 + 4 new), plus a real injected-and-recovered transient
    failure (1 retry, MTTR 10.10s, both visible in that run's own
    `SUMMARY.md`).
  - `orchestrator-demo/ambiguous-analytics` — `daysActive` /
    `averageClicksPerDay` on the analytics endpoint, reached via a real
    mid-run replan (the initial charting-library idea was discarded for a
    dependency-free redesign). 78 Java tests pass (77 + assertions on 1
    existing test).
  - `orchestrator-demo/bugfix-alias-collision` — a real defect: a generated
    code could equal an existing custom alias, failing a plain create with
    409. The run wrote a regression test first, proved it fails on the
    unfixed code with the expected `DataIntegrityViolation`, and only then
    applied the fix. 80 Java tests pass (78 + 2 new).
  - `orchestrator-demo/refactor-url-service` — validation extracted into
    `UrlValidator`, duplicated lookup removed, with no behavior change:
    the run recorded a baseline (78 tests, UrlService's public signatures)
    and verified afterwards that the API was identical, no existing test
    was edited, and 78 + 9 new = 87 tests pass.
- **This documentation**: `README.md` (setup/run instructions),
  `ARCHITECTURE.md` (component/control-flow diagrams), this file.

## Scope coverage

| Assignment scope item | Covered by |
|---|---|
| Greenfield (new systems/features) | `greenfield` — new QR-code endpoint, new service and dependency |
| Brownfield: enhancements | `brownfield` — rate limiting added to the existing create endpoint |
| Brownfield: refactors | `refactor` — behavior-preserving UrlService cleanup, verified against a recorded baseline |
| Brownfield: bug fixes | `bugfix` — real collision defect, reproduced by a failing test before the fix |
| Test improvements | every scenario writes tests; `refactor` adds direct `UrlValidator` tests where validation was only tested indirectly; `bugfix` adds an integration regression test |
| Documentation improvements | every scenario has an `update_docs` stage; `refactor` documents the code layout, `bugfix` documents alias/code namespace behavior |
| Well-defined requirements | `greenfield`, `brownfield`, `bugfix`, `refactor` |
| Ambiguous requirements | `ambiguous` — "better analytics" resolved through a real mid-run replan |

## Risks and trade-offs

- **Single-machine execution.** `asyncio`-based concurrency, not
  distributed workers — appropriate for this assignment's scope; a
  production version orchestrating many concurrent large changes would
  need a real task queue.
- **No live LLM reasoning in any demonstrated run.** The `Agent`
  abstraction and `ClaudeAgent` exist and are tested, but every actual
  demo used scripted logic. This was an explicit scope choice (see
  Assumptions) made for reproducibility, not a technical limitation of the
  design.
- **String-anchor-based file patching** (`_apply_*` functions in each
  scenario) is simple and fully idempotent (verified — see "What went
  wrong" below) but is not a general-purpose code-modification engine; it
  works because each scenario's target files and edits were known in
  advance. A production system generating truly arbitrary changes would
  need AST-aware editing or an LLM performing the edit directly.
- **Policy rules are a representative starter set** (secrets, path
  escape, tests-required, destructive-git), not an exhaustive security/
  compliance suite — deliberately scoped to what's demonstrable and
  testable in this timeframe, with `PolicyEngine` built to accept more
  rules without changing any calling code.

## Assumptions

- No `ANTHROPIC_API_KEY` is available or required; all five scenarios run
  entirely offline against `DeterministicAgent`.
- Feature branches are the deliverable, not merges to `main` — reviewing
  and merging is left to a human reviewer, matching the assignment's
  human-oversight principle for actions that change shared state.
- "Better analytics" (the ambiguous scenario) was resolved to the
  narrowest, dependency-free, schema-change-free reading under genuine
  ambiguity, with the rejected alternatives documented rather than
  silently discarded — see that scenario's own `SUMMARY.md` for the full
  reasoning.

## What went wrong, and what that proved

Three real bugs were found by actually executing the scenarios end-to-end
against the real target repo, not just by passing unit tests — each is
documented in detail in its fixing commit's message:

1. **Idempotency gap**: re-running the greenfield scenario against a
   branch it had already modified duplicated an import and a method,
   breaking compilation. Fixed by making every `_apply_*` function check
   whether its change is already present; added a regression test that
   applies each function twice and asserts the second call is a no-op.
2. **Missing retry wiring**: both scenarios' real `run()` functions
   constructed their `Scheduler` without a real `retry_executor`, so
   `RetryPolicy` was declared but never actually honored outside of tests.
   Found when brownfield's deliberately-injected transient failure had
   nowhere to recover and failed the whole run. Fixed in both scenarios.
3. **Policy false positive**: the secret-detection rule flagged
   `UUID ownerToken = UUID.randomUUID();` in real Java test source as a
   possible hardcoded secret (camelCase has no regex word boundary before
   "Token", and the old pattern allowed an unquoted value). Fixed by
   requiring the value to be an actual quoted string literal, and a
   regression test locks in both the fix and the original true-positive
   case it must still catch.

None of these were caught by the 106-test offline suite alone — each
needed the real execution against the real repo to surface, which is the
concrete argument for why "working prototype, runnable end-to-end" was
worth doing rather than treating the test suite as sufficient proof on its
own.

## Limitations

- Rollback (`reliability.git_rollback`) is scoped to files a node declares
  it touched via `Node.touches_files` — not a full transactional
  filesystem snapshot. Sufficient for this app's scale; would need a more
  general mechanism (e.g. a full worktree snapshot) for larger changes.
- The observability dashboard's DAG layout is a simple longest-path
  layering, not a general graph-layout algorithm — reads clearly for the
  ~9-12 node DAGs these scenarios produce; would need a real layout
  algorithm (e.g. Sugiyama) for much larger graphs.
- No distributed tracing across multiple orchestrator processes — this is
  a single-run, single-process audit trail, matching the single-machine
  scope above.
