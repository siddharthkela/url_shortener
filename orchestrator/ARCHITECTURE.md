# Architecture

## Component overview

```mermaid
graph TB
    subgraph engine["engine/"]
        DAG["dag.py<br/>Node, DAG, Scheduler"]
        GATES["gates.py<br/>entry/exit gate builders"]
        POLICY["policy.py<br/>PolicyEngine, rules"]
        RELIABILITY["reliability.py<br/>RetryPolicy, CircuitBreaker,<br/>git_rollback"]
        APPROVAL["approval.py<br/>ApprovalManager,<br/>AutonomyLevel"]
        REPLAN["replan.py<br/>insert_nodes,<br/>redirect_existing_node"]
        CONTEXT["context.py<br/>ExecutionContext,<br/>decision lineage"]
    end

    subgraph observability["observability/"]
        EVENTLOG["event_log.py<br/>JsonlEventSink"]
        METRICS["metrics.py<br/>compute_metrics"]
        DASHBOARD["dashboard.py<br/>HTML + inline SVG DAG"]
    end

    subgraph agents["agents/"]
        BASE["base.py<br/>Agent protocol"]
        DETERMINISTIC["deterministic.py<br/>DeterministicAgent"]
        CLAUDE["claude.py<br/>ClaudeAgent (unused by demos)"]
    end

    subgraph stages["stages/"]
        FACTORY["factory.py<br/>build_agent_node()"]
    end

    subgraph scenarios["scenarios/"]
        COMMON["common.py<br/>write_files, git ops,<br/>run_maven_test"]
        GREENFIELD["greenfield_qr_code.py"]
        BROWNFIELD["brownfield_rate_limit.py"]
        AMBIGUOUS["ambiguous_analytics.py"]
        BUGFIX["bugfix_alias_collision.py"]
        REFACTOR["refactor_url_service.py"]
    end

    DAG -->|constructor params| GATES
    DAG --> RELIABILITY
    DAG --> APPROVAL
    DAG --> EVENTLOG
    DAG --> RELIABILITY
    RELIABILITY -->|circuit_breaker| DAG
    DAG --> CONTEXT
    CONTEXT -->|replan_hook| REPLAN
    REPLAN --> DAG

    GATES --> POLICY

    FACTORY --> BASE
    FACTORY --> DAG
    DETERMINISTIC --> BASE
    CLAUDE --> BASE

    GREENFIELD --> FACTORY
    GREENFIELD --> DETERMINISTIC
    GREENFIELD --> COMMON
    GREENFIELD --> GATES
    BROWNFIELD --> FACTORY
    BROWNFIELD --> DETERMINISTIC
    BROWNFIELD --> COMMON
    BROWNFIELD --> POLICY
    AMBIGUOUS --> FACTORY
    AMBIGUOUS --> DETERMINISTIC
    AMBIGUOUS --> COMMON
    AMBIGUOUS --> REPLAN
    BUGFIX --> FACTORY
    BUGFIX --> DETERMINISTIC
    BUGFIX --> COMMON
    BUGFIX --> POLICY
    REFACTOR --> FACTORY
    REFACTOR --> DETERMINISTIC
    REFACTOR --> COMMON
    REFACTOR --> POLICY

    METRICS -.reads.-> EVENTLOG
    DASHBOARD -.reads.-> METRICS
    DASHBOARD -.reads.-> DAG
```

**Why this shape**: the scheduler (`dag.py`) is built around four narrow
extension points — a gate evaluator, a retry executor, an approval manager,
and an event sink — each with a trivial pass-through default. Every other
subsystem (`gates.py`, `reliability.py`, `approval.py`, the observability
package) is a real implementation of one of those extension points, plugged
in via constructor arguments. Nothing about the scheduler's core loop
changed after its first commit; every later phase added a real
implementation for an already-existing seam. That's not incidental — it's
what let each phase land as an independently testable, independently
reviewable commit against the same scheduler its own tests and every later
phase's tests exercise.

## Control flow: one node's execution

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant G as GateEvaluator
    participant AM as ApprovalManager
    participant RE as RetryExecutor
    participant N as Node.run()
    participant CB as CircuitBreaker

    S->>G: evaluate(entry_gate)
    alt entry gate fails
        G-->>S: not passed
        S->>S: mark BLOCKED
    else entry gate passes
        S->>AM: request approval
        alt denied
            AM-->>S: False
            S->>S: mark BLOCKED
        else approved / not required
            AM-->>S: True
            S->>RE: execute(node)
            loop up to max_attempts
                RE->>N: run(node, context)
                N-->>RE: NodeResult
                alt success
                    RE-->>S: success result
                else failure, attempts remain
                    RE->>RE: emit node_retry_attempt, backoff
                end
            end
            alt all attempts failed and fallback exists
                RE->>N: fallback(node, context)
            end
            RE-->>S: final result
            alt result.success
                S->>G: evaluate(exit_gate)
                alt exit gate fails
                    S->>S: mark FAILED, rollback if declared
                else exit gate passes
                    S->>S: mark SUCCEEDED, set context output
                end
            else result failed
                S->>S: mark FAILED, rollback if declared
            end
            S->>CB: record_result(success)
        end
    end
```

## Non-linear execution: the brownfield DAG

This is the shape from the assignment's own "non-linear, stateful
execution" requirement — a join, a genuine fan-out into parallel branches,
a second join, and a human-approval gate before release:

```mermaid
graph LR
    IR[intake_requirement] --> AC[analyze_codebase]
    AC --> D[design]
    D --> IC[implement_code]
    D --> DT[draft_tests]
    D --> UD[update_docs]
    IC --> RT[run_tests]
    DT --> RT
    RT -->|"1 retry: injected<br/>transient failure"| RT
    RT --> PC[policy_check]
    PC --> RR["release_readiness<br/>(human approval)"]
    UD --> RR
    RR --> F[finalize]

    style RR fill:#fff3cd,stroke:#856404
```

`implement_code`, `draft_tests`, and `update_docs` all become ready in the
same tick once `design` succeeds, and the scheduler runs them concurrently
via `asyncio.gather` — `run_tests` is a real synchronization barrier
(it only becomes ready once *both* parallel branches finish), and
`release_readiness` is a second one (waiting on `policy_check` *and*
`update_docs`).

## Dynamic re-planning: the ambiguous scenario

```mermaid
graph LR
    IR[intake_requirement] --> AC[analyze_codebase]
    AC --> D["design<br/>(proposes charting lib)"]
    D --> CC[check_constraints]
    CC -.->|"conflict found:<br/>insert_nodes()"| RD["redesign<br/>(inserted at runtime)"]
    CC --> IC[implement_code]
    RD -.->|"redirect_existing_node()"| IC
    CC --> DT[draft_tests]
    RD -.-> DT
    CC --> UD[update_docs]
    RD -.-> UD
    IC --> RT[run_tests]
    DT --> RT
    RT --> PC[policy_check]
    PC --> RR["release_readiness<br/>(approval)"]
    UD --> RR
    RR --> F[finalize]

    style RD fill:#d1ecf1,stroke:#0c5460
    style CC fill:#fff3cd,stroke:#856404
```

`check_constraints` sits between `design` and the implementation fan-out
specifically so it resolves *before* `implement_code`/`draft_tests`/
`update_docs` become ready — if it ran in parallel with them (e.g. if it
also just depended on `design`), the replan could lose the race against
work that should have been redirected. When it detects the conflict, it
calls `context.replan()` to insert `redesign` and redirect the three
downstream nodes onto it in addition to their original dependency —
conditional on what `design` actually produced, not scripted to always
fire (a stub design with no conflict produces zero replan activity, which
is what one of the structural tests asserts).

## Reproduce-before-fix: the bug-fix scenario

```mermaid
graph LR
    I[intake_bug_report] --> RCA[root_cause_analysis] --> D[design_fix]
    D --> T[write_regression_test] --> R[reproduce_bug] --> F[implement_fix] --> RT[run_tests] --> P[policy_check] --> REL[release_readiness] --> FIN[finalize]
    D --> UD[update_docs] --> REL
```

`reproduce_bug` inverts the usual success condition: it runs only the new
tests (`run_maven_test(extra_args=["-Dtest=..."])`) against the unfixed code
and succeeds only if they fail with the expected `DataIntegrityViolation`.
If they pass, there's no reproduction, and if they fail for another reason
(say, a compile error), it isn't the bug, so in both cases the node fails
and `implement_fix` is blocked. "No fix without a reproduction" is
therefore an edge in the graph, not a convention.

## Behavior preservation: the refactor scenario

```mermaid
graph LR
    I[intake_requirement] --> A[analyze_codebase] --> D[design] --> B[baseline_tests]
    B --> IR[implement_refactor] --> RT[run_tests]
    B --> NT[write_new_tests] --> RT
    B --> UD[update_docs] --> REL[release_readiness]
    RT --> BC[behavior_check] --> P[policy_check] --> REL --> FIN[finalize]
```

Every node that writes files depends on `baseline_tests`, so the baseline
(full suite green, test count from the surefire reports, UrlService's
public signatures) always describes the untouched code. `behavior_check`
then fails the run if the public API changed, if any pre-existing test file
shows as modified in `git status` (a refactor that needs old tests edited
is a behavior change), or if the test count isn't exactly baseline + new.

## Observability

Every scheduler event — node start/success/failure/block, retry attempts,
rollbacks, approval requests/decisions, replans — is written as one JSON
line to `runs/<scenario>/events.jsonl` as it happens. `metrics.py` computes
success rate, retry/rollback frequency, MTTR, and per-node/total latency
**entirely from that log**, after the fact — there's no separate runtime
counters that could drift from what the log says happened. `dashboard.html`
renders the executed DAG (topologically layered, colored by final status,
join edges drawn as multi-parent arrows) plus those metrics as stat tiles,
as a single self-contained file — no server, no external JS.

## Reliability controls in one place

| Control | Where | Proven by |
|---|---|---|
| Bounded retries | `reliability.RetryPolicy` + `make_retrying_executor` | `test_reliability.py`; brownfield's real injected-failure run (1 retry, MTTR 10.10s in its actual `SUMMARY.md`) |
| Fallback | `Node.fallback`, run after retries exhaust | `test_reliability.py` |
| Rollback | `Node.rollback`, `reliability.git_rollback` | `test_reliability.py` against a throwaway git repo |
| Safe-stop | `reliability.CircuitBreaker` + `Scheduler._safe_stop` | `test_reliability.py`; deliberately separated into two ticks so the test proves in-flight work isn't retroactively cancelled, only new work is prevented |
| Policy guardrails | `policy.PolicyEngine`, four default rules | `test_policy.py`, including a regression test for a real false-positive found running the ambiguous scenario |
| Human approval | `approval.ApprovalManager`, three autonomy levels | `test_approval.py`; every real scenario run pauses at `release_readiness` |
| Audit trail | `observability.event_log.JsonlEventSink` | Every real run's `events.jsonl` |
| Dynamic re-planning | `engine.replan` + `context.replan_hook` | `test_replan.py`; the ambiguous scenario's real `redesign` node |

## Known limitations (see `FINAL_SUMMARY.md` for the full list)

- Single-machine `asyncio` concurrency, not distributed workers.
- No live LLM calls in any demo run (by explicit choice — see README).
- Rollback is scoped to files a node declares it touched, not a full
  transactional filesystem snapshot.
