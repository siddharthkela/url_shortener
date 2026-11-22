"""Bug-fix scenario: "Creating a short URL sometimes fails with 409 Conflict
even though no custom alias was requested."

This is a real defect in the target app, not a planted one. Custom aliases
and generated Base62 codes share a single short-code namespace, and
UrlService assigns Base62(id) to a generated row without checking whether a
custom alias already holds that code. Once an alias like "abc" exists, the
generated create that receives id 140716 (= Base62 "abc") hits the unique
constraint and fails with 409, for a client that never asked for an alias.

What this scenario demonstrates, beyond the other three:
- **Reproduce-before-fix discipline.** A regression test is written first and
  the `reproduce_bug` node runs it against the *unfixed* code. That node
  succeeds only if the test fails for the expected reason (the unique
  constraint violation). A test that passes before the fix proves nothing,
  so the run stops there.
- **Fix only after confirmed reproduction.** `implement_fix` depends on
  `reproduce_bug`, so the DAG makes "no fix without a reproduction" a
  structural rule rather than a convention.
- **Full-suite verification.** After the fix, the whole suite runs, including
  the new regression test and a new unit test for the skip-taken-id path.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict

from orchestrator.agents.base import Agent, AgentResult
from orchestrator.agents.deterministic import DeterministicAgent
from orchestrator.engine.approval import ApprovalManager, AutonomyLevel, auto_approve, interactive_prompt
from orchestrator.engine.context import ExecutionContext
from orchestrator.engine.dag import DAG, Node, NodeResult, RunResult, Scheduler
from orchestrator.engine.policy import PolicyContext, PolicyEngine
from orchestrator.engine.reliability import make_retrying_executor
from orchestrator.observability.dashboard import write_dashboard
from orchestrator.observability.event_log import JsonlEventSink
from orchestrator.observability.metrics import compute_metrics
from orchestrator.scenarios.common import (
    FileChange,
    commit_all,
    create_and_checkout_branch,
    run_git,
    run_maven_test,
    write_files,
    write_run_summary,
)
from orchestrator.stages import (
    build_codebase_analysis_node,
    build_design_node,
    build_docs_node,
    build_implementation_node,
    build_release_node,
    build_requirements_node,
    build_test_node,
)

RAW_REQUIREMENT = (
    "Bug report: creating a short URL sometimes fails with 409 Conflict even though "
    "no custom alias was requested."
)
BRANCH_NAME = "orchestrator-demo/bugfix-alias-collision"
SERVICE_PATH = "src/main/java/com/urlshortener/service/UrlService.java"
SERVICE_TEST_PATH = "src/test/java/com/urlshortener/service/UrlServiceTest.java"
REGRESSION_TEST_PATH = "src/test/java/com/urlshortener/integration/ShortCodeCollisionRegressionTest.java"
REGRESSION_TEST_CLASS = "ShortCodeCollisionRegressionTest"
UNIT_TEST_METHOD = "skipsGeneratedCodeAlreadyTakenByCustomAlias"
# What the unfixed code must fail with. If the reproduction fails for any
# other reason (compile error, wrong assertion), it isn't a reproduction.
EXPECTED_FAILURE_SIGNATURE = "DataIntegrityViolation"


# --------------------------------------------------------------------------
# DAG structure: agent-agnostic, unit-tested with a stub agent.
# --------------------------------------------------------------------------

def build_dag(agent: Agent, repo_root: str) -> DAG:
    dag = DAG()

    dag.add_node(build_requirements_node(
        node_id="intake_bug_report",
        agent=agent,
        payload_fn=lambda ctx: {"raw": RAW_REQUIREMENT},
        decision_summary_fn=lambda out: f"Normalized: {out.get('normalized_requirement', '(n/a)')}",
    ))

    dag.add_node(build_codebase_analysis_node(
        node_id="root_cause_analysis",
        agent=agent,
        payload_fn=lambda ctx: {"repo_root": repo_root, "report": ctx.get_output("intake_bug_report")},
        depends_on=["intake_bug_report"],
        decision_summary_fn=lambda out: f"Root cause: {out.get('root_cause', '(n/a)')}",
    ))

    dag.add_node(build_design_node(
        node_id="design_fix",
        agent=agent,
        payload_fn=lambda ctx: {"analysis": ctx.get_output("root_cause_analysis")},
        depends_on=["root_cause_analysis"],
        decision_summary_fn=lambda out: f"Fix: {out.get('approach', '(n/a)')}",
    ))

    dag.add_node(build_test_node(
        node_id="write_regression_test",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design_fix"), "repo_root": repo_root},
        depends_on=["design_fix"],
        decision_summary_fn=lambda out: f"Wrote/modified: {out.get('files_written', [])}",
    ))

    async def _reproduce_bug(node: Node, context) -> NodeResult:
        """Inverted success: this node succeeds only when the new tests FAIL
        on the unfixed code, for the expected reason."""
        passed, output = run_maven_test(
            repo_root,
            extra_args=[f"-Dtest={REGRESSION_TEST_CLASS},UrlServiceTest#{UNIT_TEST_METHOD}"],
        )
        if passed:
            return NodeResult(
                success=False,
                error="Regression tests PASSED on unfixed code: the bug was not reproduced, "
                      "so there is nothing to fix. Stopping before changing production code.",
            )
        if EXPECTED_FAILURE_SIGNATURE not in output:
            return NodeResult(
                success=False,
                error=f"Regression tests failed, but not with {EXPECTED_FAILURE_SIGNATURE}: "
                      "they fail for some other reason, so this is not a reproduction.",
            )
        context.record_decision(
            stage="reproduce",
            summary=f"Bug reproduced: new tests fail on unfixed code with {EXPECTED_FAILURE_SIGNATURE}",
            rationale="A fix is only meaningful against a reproduction. The test is now known to "
                      "detect the defect, so it turning green after the fix is real evidence.",
            inputs={"tests": [REGRESSION_TEST_CLASS, f"UrlServiceTest#{UNIT_TEST_METHOD}"]},
            outputs={"reproduced": True},
        )
        return NodeResult(success=True, output={"reproduced": True, "maven_output_tail": output[-2000:]})

    dag.add_node(Node(id="reproduce_bug", run=_reproduce_bug, depends_on=["write_regression_test"]))

    dag.add_node(build_implementation_node(
        node_id="implement_fix",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design_fix"), "repo_root": repo_root},
        depends_on=["reproduce_bug"],
        touches_files=[SERVICE_PATH],
        decision_summary_fn=lambda out: f"Wrote/modified: {out.get('files_written', [])}",
    ))

    dag.add_node(build_docs_node(
        node_id="update_docs",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design_fix"), "repo_root": repo_root},
        depends_on=["design_fix"],
        decision_summary_fn=lambda out: f"Wrote: {out.get('files_written', [])}",
    ))

    async def _run_tests(node: Node, context) -> NodeResult:
        success, output = run_maven_test(repo_root)
        return NodeResult(success=success, output={"maven_output_tail": output[-2000:]},
                          error=None if success else "mvn test failed after fix")

    dag.add_node(Node(id="run_tests", run=_run_tests, depends_on=["implement_fix"]))

    policy_engine = PolicyEngine.default()

    async def _policy_check(node: Node, context) -> NodeResult:
        impl_files = context.get_output("implement_fix", {}).get("files_written", [])
        test_files = context.get_output("write_regression_test", {}).get("files_written", [])
        file_contents = {}
        for rel_path in impl_files + test_files:
            full = Path(repo_root) / rel_path
            if full.exists():
                file_contents[rel_path] = full.read_text()
        ctx = PolicyContext(
            repo_root=repo_root,
            file_contents=file_contents,
            touches_files=impl_files + test_files,
            new_endpoints=[],
            test_files_created=test_files,
        )
        violations = policy_engine.evaluate(ctx)
        if PolicyEngine.has_critical(violations):
            messages = "; ".join(f"{v.rule}: {v.message}" for v in violations if v.severity == "CRITICAL")
            return NodeResult(success=False, error=f"policy violation(s): {messages}")
        return NodeResult(success=True, output={"violations": len(violations)})

    dag.add_node(Node(id="policy_check", run=_policy_check, depends_on=["run_tests"]))

    dag.add_node(build_release_node(
        node_id="release_readiness",
        agent=agent,
        payload_fn=lambda ctx: {"summary": "Alias/generated-code collision fix ready for review"},
        depends_on=["policy_check", "update_docs"],
        requires_approval=True,
        decision_summary_fn=lambda out: f"Release status: {out.get('status', '(n/a)')}",
    ))

    async def _finalize(node: Node, context) -> NodeResult:
        commit_all(
            repo_root,
            "fix: skip generated short codes already taken by a custom alias\n\n"
            "Generated creates could fail with 409 when Base62(id) matched an existing\n"
            "custom alias. Reproduced with a failing regression test before the fix.\n\n"
            "Orchestrator bug-fix demo scenario.",
        )
        push = run_git(repo_root, "push", "-u", "origin", BRANCH_NAME)
        return NodeResult(success=True, output={"pushed": push.returncode == 0, "push_stderr": push.stderr})

    dag.add_node(Node(id="finalize", run=_finalize, depends_on=["release_readiness"]))

    return dag


# --------------------------------------------------------------------------
# End-to-end runner (same pattern as the other scenarios).
# --------------------------------------------------------------------------

async def run(repo_root: str, auto_approve_all: bool = True) -> RunResult:
    staging_dir = Path(tempfile.mkdtemp(prefix="orchestrator_bugfix_"))
    events_path = staging_dir / "events.jsonl"

    create_and_checkout_branch(repo_root, BRANCH_NAME, base="main")

    agent = build_bugfix_agent(repo_root)
    dag = build_dag(agent, repo_root)
    sink = JsonlEventSink(path=events_path)
    approval = ApprovalManager(
        autonomy=AutonomyLevel.ASSISTED,
        decision_fn=auto_approve if auto_approve_all else interactive_prompt,
        event_sink=sink,
    )
    context = ExecutionContext(run_id="bugfix-alias-collision")
    scheduler = Scheduler(event_sink=sink, approval_manager=approval, retry_executor=make_retrying_executor(event_sink=sink))

    result = await scheduler.run(dag, context)

    metrics = compute_metrics(sink.events)
    write_dashboard(staging_dir / "dashboard.html", run_id=context.run_id, dag=dag, metrics=metrics)
    write_run_summary(
        staging_dir / "SUMMARY.md",
        scenario_name="Bug fix: generated short code collides with a custom alias",
        requirement=RAW_REQUIREMENT,
        branch_name=BRANCH_NAME,
        context=context,
        metrics=metrics,
        risks=[
            "Skipping a colliding id discards that identity value, leaving a gap in the id "
            "sequence. Ids are internal and never exposed except through their Base62 code, "
            "so a gap has no user-visible effect.",
            "Each collision costs one extra insert and delete inside the same transaction. "
            "Collisions need an alias equal to a future Base62(id), so they are rare, and the "
            "loop always terminates because every alias can block at most one id.",
        ],
        assumptions=[
            "The existing public contract stays: aliases may be any 3-16 character "
            "[a-zA-Z0-9_-] string, including ones that look like generated codes. Rejecting "
            "such aliases would also fix the bug, but would break clients that already rely "
            "on them, so it was rejected as a behavior change.",
            "The reported 409 comes from generated creates only. Alias creates already check "
            "existsByShortCode and return a clear 409 'alias taken', which is correct behavior.",
        ],
        limitations=[
            "The reverse direction (a client requesting an alias equal to an already-generated "
            "code) was already handled correctly before this fix and is unchanged.",
        ],
    )

    run_git(repo_root, "checkout", "main")
    dest = Path(repo_root) / "orchestrator" / "runs" / "bugfix"
    dest.mkdir(parents=True, exist_ok=True)
    for artifact in ("events.jsonl", "dashboard.html", "SUMMARY.md"):
        shutil.copy(staging_dir / artifact, dest / artifact)
    shutil.rmtree(staging_dir, ignore_errors=True)

    return result


# --------------------------------------------------------------------------
# Real scripted handlers.
# --------------------------------------------------------------------------

def build_bugfix_agent(repo_root: str) -> DeterministicAgent:
    async def requirements_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(
            success=True,
            output={
                "normalized_requirement": "POST /api/v1/urls without a customAlias must never fail "
                                           "because its generated code is already held by an alias.",
                "acceptance_criteria": [
                    "A generated create succeeds and returns a code different from any existing alias",
                    "The existing alias keeps resolving to its own target URL",
                    "No change to the public API, alias rules, or response formats",
                ],
                "open_questions": [],
            },
            rationale="The report names the symptom (409 with no alias). The acceptance criteria "
                      "pin down the invariant to restore without widening scope.",
        )

    async def root_cause_handler(payload: Dict[str, Any]) -> AgentResult:
        repo_root_str = payload.get("repo_root", repo_root)
        service = (Path(repo_root_str) / SERVICE_PATH).read_text()
        request_dto = (Path(repo_root_str) / "src/main/java/com/urlshortener/dto/CreateUrlRequest.java").read_text()

        generated_path_unchecked = (
            "entity.setShortCode(Base62Encoder.encode(entity.getId()));" in service
        )
        alias_regex_overlaps_base62 = "[a-zA-Z0-9_-]" in request_dto

        return AgentResult(
            success=True,
            output={
                "root_cause": "UrlService assigns Base62(id) to generated rows without checking "
                              "whether a custom alias already holds that code; aliases and generated "
                              "codes share one unique short_code column.",
                "evidence": {
                    "generated_code_assigned_without_existence_check": generated_path_unchecked,
                    "alias_pattern_allows_base62_strings": alias_regex_overlaps_base62,
                },
                "why_409": "The unique-constraint violation is a DataIntegrityViolationException, which "
                           "GlobalExceptionHandler maps to 409 'short code or alias already in use'.",
                "impacted_files": [SERVICE_PATH],
            },
            rationale="Read UrlService.java and CreateUrlRequest.java to confirm both halves of the "
                      "defect: the unchecked generated path and the overlapping alias pattern.",
        )

    async def design_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(
            success=True,
            output={
                "approach": "In the generated path, if Base62(id) is already taken, delete that row "
                            "and insert a fresh one to take the next id; repeat until the code is free.",
                "rejected_alternatives": [
                    "Reject aliases that are valid Base62: breaks existing clients (API change).",
                    "Prefix generated codes: changes every new short URL's format (API change).",
                    "Catch the exception and retry the request: hides the cause and is racy.",
                ],
            },
            rationale="Smallest change that restores the invariant, stays inside the existing "
                      "transaction, and leaves the public API and URL formats untouched.",
        )

    async def test_handler(payload: Dict[str, Any]) -> AgentResult:
        service_test = (Path(repo_root) / SERVICE_TEST_PATH).read_text()
        written = write_files(repo_root, [
            FileChange(REGRESSION_TEST_PATH, _REGRESSION_TEST_JAVA),
            FileChange(SERVICE_TEST_PATH, _apply_service_test_changes(service_test)),
        ])
        return AgentResult(success=True, output={"files_written": written},
                           rationale="Integration test reproduces the defect against real H2; the unit "
                                     "test pins down the skip-and-retry behavior with mocks.")

    async def implementation_handler(payload: Dict[str, Any]) -> AgentResult:
        service = (Path(repo_root) / SERVICE_PATH).read_text()
        written = write_files(repo_root, [FileChange(SERVICE_PATH, _apply_service_fix(service))])
        return AgentResult(success=True, output={"files_written": written})

    async def docs_handler(payload: Dict[str, Any]) -> AgentResult:
        readme = (Path(repo_root) / "README.md").read_text()
        written = write_files(repo_root, [FileChange("README.md", _apply_readme_changes(readme))])
        return AgentResult(success=True, output={"files_written": written})

    async def release_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(success=True, output={"status": "ready", "summary": payload.get("summary", "")})

    return DeterministicAgent(handlers={
        "requirements": requirements_handler,
        "codebase_analysis": root_cause_handler,
        "design": design_handler,
        "test": test_handler,
        "implementation": implementation_handler,
        "docs": docs_handler,
        "release": release_handler,
    })


# --------------------------------------------------------------------------
# Java/text templates and precise, idempotent edits to existing files.
# --------------------------------------------------------------------------

_REGRESSION_TEST_JAVA = """package com.urlshortener.integration;

import com.urlshortener.dto.CreateUrlRequest;
import com.urlshortener.dto.UrlResponse;
import com.urlshortener.service.UrlService;
import com.urlshortener.util.Base62Encoder;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Regression test for a real collision bug: custom aliases and generated
 * Base62 codes share one namespace. If a custom alias equals Base62(id) of a
 * row created later, that generated create used to fail with a unique
 * constraint violation (returned to the client as 409 Conflict on a request
 * that never asked for an alias).
 */
@SpringBootTest
class ShortCodeCollisionRegressionTest {

    @Autowired
    private UrlService urlService;

    @Autowired
    private JdbcTemplate jdbc;

    @Test
    void generatedCodeNeverCollidesWithAnExistingCustomAlias() {
        // Move the identity sequence to a range where Base62(id) is a valid
        // 3+ character alias, so the collision can be set up deterministically.
        Long maxId = jdbc.queryForObject("SELECT COALESCE(MAX(id), 0) FROM short_urls", Long.class);
        long start = Math.max(maxId + 1, 200_000L);
        jdbc.execute("ALTER TABLE short_urls ALTER COLUMN id RESTART WITH " + start);

        // The alias row takes id `start`; the next generated row gets id
        // `start + 1`, whose Base62 encoding is exactly this alias.
        String alias = Base62Encoder.encode(start + 1);
        urlService.createShortUrl(new CreateUrlRequest("https://example.com/alias-target", alias, null), null);

        UrlResponse generated = urlService.createShortUrl(
                new CreateUrlRequest("https://example.com/generated-target", null, null), null);

        assertThat(generated.shortCode()).isNotEqualTo(alias);
        assertThat(urlService.resolve(alias)).isEqualTo("https://example.com/alias-target");
        assertThat(urlService.resolve(generated.shortCode())).isEqualTo("https://example.com/generated-target");
    }
}
"""

_UNFIXED_GENERATED_BRANCH = (
    "        } else {\n"
    "            String placeholder = UUID.randomUUID().toString().substring(0, 16);\n"
    "            entity = new ShortUrlEntity(placeholder, request.originalUrl(), false, ownerToken, now, request.expiresAt());\n"
    "            entity = repository.save(entity);\n"
    "            entity.setShortCode(Base62Encoder.encode(entity.getId()));\n"
    "            entity = repository.save(entity);\n"
    "        }\n"
)

_FIXED_GENERATED_BRANCH = (
    "        } else {\n"
    "            entity = repository.save(newPlaceholderEntity(request, ownerToken, now));\n"
    "            String code = Base62Encoder.encode(entity.getId());\n"
    "            // Custom aliases share the short-code namespace with generated codes, so\n"
    "            // Base62(id) may already be taken by an alias. Skip that id (discard the\n"
    "            // row, take the next one) instead of failing the client's request.\n"
    "            while (repository.existsByShortCode(code)) {\n"
    "                repository.delete(entity);\n"
    "                entity = repository.save(newPlaceholderEntity(request, ownerToken, now));\n"
    "                code = Base62Encoder.encode(entity.getId());\n"
    "            }\n"
    "            entity.setShortCode(code);\n"
    "            entity = repository.save(entity);\n"
    "        }\n"
)

_PLACEHOLDER_HELPER_ANCHOR = (
    "        return UrlMapper.toResponse(entity, baseUrl);\n"
    "    }\n\n"
    "    public UrlResponse getDetails("
)

_PLACEHOLDER_HELPER = (
    "        return UrlMapper.toResponse(entity, baseUrl);\n"
    "    }\n\n"
    "    private ShortUrlEntity newPlaceholderEntity(CreateUrlRequest request, UUID ownerToken, Instant now) {\n"
    "        String placeholder = UUID.randomUUID().toString().substring(0, 16);\n"
    "        return new ShortUrlEntity(placeholder, request.originalUrl(), false, ownerToken, now, request.expiresAt());\n"
    "    }\n\n"
    "    public UrlResponse getDetails("
)


def _apply_service_fix(original: str) -> str:
    if "newPlaceholderEntity" in original:
        return original  # already applied: no-op
    if _UNFIXED_GENERATED_BRANCH not in original or _PLACEHOLDER_HELPER_ANCHOR not in original:
        raise ValueError("UrlService no longer matches the analyzed shape; refusing to patch blindly")
    updated = original.replace(_UNFIXED_GENERATED_BRANCH, _FIXED_GENERATED_BRANCH)
    return updated.replace(_PLACEHOLDER_HELPER_ANCHOR, _PLACEHOLDER_HELPER, 1)


_UNIT_TEST_ANCHOR = (
    "        assertThat(response.shortUrl()).isEqualTo(\"http://localhost:8080/\" + Base62Encoder.encode(42L));\n"
    "        verify(repository, times(2)).save(any(ShortUrlEntity.class));\n"
    "    }\n"
)

_UNIT_TEST = (
    "\n    @Test\n"
    f"    void {UNIT_TEST_METHOD}() {{\n"
    "        when(repository.countByActiveTrue()).thenReturn(0L);\n"
    "        ShortUrlEntity collidingRow = entityWithId(140716L, \"placeholder\", false);\n"
    "        ShortUrlEntity nextRow = entityWithId(140717L, \"placeholder\", false);\n"
    "        when(repository.save(any(ShortUrlEntity.class))).thenReturn(collidingRow, nextRow, nextRow);\n"
    "        // Base62(140716) is \"abc\": pretend a client already holds it as a custom alias.\n"
    "        when(repository.existsByShortCode(Base62Encoder.encode(140716L))).thenReturn(true);\n"
    "        when(repository.existsByShortCode(Base62Encoder.encode(140717L))).thenReturn(false);\n\n"
    "        UrlResponse response = urlService.createShortUrl(new CreateUrlRequest(\"https://example.com/page\", null, null));\n\n"
    "        assertThat(response.shortCode()).isEqualTo(Base62Encoder.encode(140717L));\n"
    "        verify(repository).delete(collidingRow);\n"
    "    }\n"
)


def _apply_service_test_changes(original: str) -> str:
    if UNIT_TEST_METHOD in original:
        return original  # already applied: no-op
    if _UNIT_TEST_ANCHOR not in original:
        raise ValueError("UrlServiceTest no longer matches the expected shape")
    return original.replace(_UNIT_TEST_ANCHOR, _UNIT_TEST_ANCHOR + _UNIT_TEST, 1)


_README_ALIAS_ANCHOR = (
    "  -d '{\"originalUrl\": \"https://example.com\", \"customAlias\": \"my-alias\"}'\n"
    "```\n"
)

_README_ALIAS_NOTE = (
    "\nCustom aliases and generated codes share one namespace. If the next generated\n"
    "code would equal an existing alias (for example, alias `abc` is Base62 for id\n"
    "140716), the service skips that id and uses the next free one, so a create\n"
    "without an alias never fails because of someone else's alias.\n"
)


def _apply_readme_changes(original: str) -> str:
    if "share one namespace" in original:
        return original  # already applied: no-op
    return original.replace(_README_ALIAS_ANCHOR, _README_ALIAS_ANCHOR + _README_ALIAS_NOTE, 1)
