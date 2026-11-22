"""Refactor scenario: "UrlService mixes input validation with persistence and
repeats its active-row lookup. Clean it up without changing behavior."

A refactor's whole claim is "nothing observable changed", so this scenario
is built around proving that claim, not just making the change:

- **Baseline first.** `baseline_tests` runs the full suite on the untouched
  code, records the test count, and snapshots UrlService's public method
  signatures. Every node that writes files depends on it, so the baseline
  is guaranteed to describe the pre-refactor code.
- **Behavior check after.** `behavior_check` fails the run if UrlService's
  public API changed, if any pre-existing test file was modified (a refactor
  that has to edit old tests to pass is a behavior change), or if the
  test count is not exactly baseline + the new tests.
- **Test and documentation improvements** ride along: the extracted
  UrlValidator gets its own focused unit tests (validation was previously
  only tested indirectly through UrlService), and the README documents the
  new structure.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

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
    count_maven_tests,
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
    "UrlService mixes input validation with persistence logic and repeats its "
    "active-row lookup. Clean it up without changing any behavior."
)
BRANCH_NAME = "orchestrator-demo/refactor-url-service"
SERVICE_PATH = "src/main/java/com/urlshortener/service/UrlService.java"
VALIDATOR_PATH = "src/main/java/com/urlshortener/service/UrlValidator.java"
VALIDATOR_TEST_PATH = "src/test/java/com/urlshortener/service/UrlValidatorTest.java"

_PUBLIC_SIGNATURE = re.compile(r"^\s*public\s+[^=;{]*\([^)]*\)", re.MULTILINE)


def public_signatures(java_source: str) -> List[str]:
    """Normalized public method/constructor signatures, used to prove a
    refactor left a class's public API untouched."""
    return sorted(" ".join(m.group(0).split()) for m in _PUBLIC_SIGNATURE.finditer(java_source))


def modified_existing_test_files(porcelain_status: str) -> List[str]:
    """From `git status --porcelain -- src/test`, the pre-existing test files
    that were modified (new untracked files are allowed, edits are not)."""
    modified = []
    for line in porcelain_status.splitlines():
        code, path = line[:2], line[3:]
        if "M" in code or "D" in code or "R" in code:
            modified.append(path)
    return modified


# --------------------------------------------------------------------------
# DAG structure: agent-agnostic, unit-tested with a stub agent.
# --------------------------------------------------------------------------

def build_dag(agent: Agent, repo_root: str) -> DAG:
    dag = DAG()

    dag.add_node(build_requirements_node(
        node_id="intake_requirement",
        agent=agent,
        payload_fn=lambda ctx: {"raw": RAW_REQUIREMENT},
        decision_summary_fn=lambda out: f"Normalized: {out.get('normalized_requirement', '(n/a)')}",
    ))

    dag.add_node(build_codebase_analysis_node(
        node_id="analyze_codebase",
        agent=agent,
        payload_fn=lambda ctx: {"repo_root": repo_root},
        depends_on=["intake_requirement"],
        decision_summary_fn=lambda out: f"Smells found: {out.get('smells', [])}",
    ))

    dag.add_node(build_design_node(
        node_id="design",
        agent=agent,
        payload_fn=lambda ctx: {"analysis": ctx.get_output("analyze_codebase")},
        depends_on=["analyze_codebase"],
        decision_summary_fn=lambda out: f"Plan: {out.get('approach', '(n/a)')}",
    ))

    async def _baseline_tests(node: Node, context) -> NodeResult:
        service_before = (Path(repo_root) / SERVICE_PATH).read_text()
        success, output = run_maven_test(repo_root)
        if not success:
            return NodeResult(success=False, error="Baseline suite is already failing; a refactor "
                                                   "can't be verified against a red baseline.")
        baseline = {
            "test_count": count_maven_tests(repo_root),
            "public_api": public_signatures(service_before),
        }
        context.record_decision(
            stage="baseline",
            summary=f"Baseline: {baseline['test_count']} tests green, "
                    f"{len(baseline['public_api'])} public UrlService signatures recorded",
            rationale="A behavior-preserving claim needs a recorded 'before' to compare against.",
            inputs={},
            outputs=baseline,
        )
        return NodeResult(success=True, output=baseline)

    dag.add_node(Node(id="baseline_tests", run=_baseline_tests, depends_on=["design"]))

    dag.add_node(build_implementation_node(
        node_id="implement_refactor",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design"), "repo_root": repo_root},
        depends_on=["baseline_tests"],
        touches_files=[SERVICE_PATH, VALIDATOR_PATH],
        decision_summary_fn=lambda out: f"Wrote/modified: {out.get('files_written', [])}",
    ))

    dag.add_node(build_test_node(
        node_id="write_new_tests",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design"), "repo_root": repo_root},
        depends_on=["baseline_tests"],
        decision_summary_fn=lambda out: f"Wrote: {out.get('files_written', [])} "
                                        f"({out.get('new_test_count', 0)} new tests)",
    ))

    dag.add_node(build_docs_node(
        node_id="update_docs",
        agent=agent,
        payload_fn=lambda ctx: {"design": ctx.get_output("design"), "repo_root": repo_root},
        depends_on=["baseline_tests"],
        decision_summary_fn=lambda out: f"Wrote: {out.get('files_written', [])}",
    ))

    async def _run_tests(node: Node, context) -> NodeResult:
        success, output = run_maven_test(repo_root)
        return NodeResult(
            success=success,
            output={"test_count": count_maven_tests(repo_root), "maven_output_tail": output[-2000:]},
            error=None if success else "mvn test failed after refactor",
        )

    dag.add_node(Node(id="run_tests", run=_run_tests, depends_on=["implement_refactor", "write_new_tests"]))

    async def _behavior_check(node: Node, context) -> NodeResult:
        baseline = context.get_output("baseline_tests", {})
        after_count = context.get_output("run_tests", {}).get("test_count", 0)
        new_tests = context.get_output("write_new_tests", {}).get("new_test_count", 0)
        api_after = public_signatures((Path(repo_root) / SERVICE_PATH).read_text())
        status = run_git(repo_root, "status", "--porcelain", "--", "src/test").stdout
        touched_old_tests = modified_existing_test_files(status)

        problems = []
        if api_after != baseline.get("public_api"):
            problems.append("UrlService public API changed")
        if touched_old_tests:
            problems.append(f"pre-existing tests were edited: {touched_old_tests}")
        expected = baseline.get("test_count", 0) + new_tests
        if after_count != expected:
            problems.append(f"test count {after_count} != baseline {baseline.get('test_count')} + {new_tests} new")
        if problems:
            return NodeResult(success=False, error="Not behavior-preserving: " + "; ".join(problems))

        context.record_decision(
            stage="behavior_check",
            summary=f"Behavior preserved: public API identical, 0 existing tests edited, "
                    f"{baseline.get('test_count')} baseline tests + {new_tests} new = {after_count} green",
            rationale="Same public surface, untouched pre-existing tests, and the full baseline "
                      "suite still green is the evidence a refactor didn't change behavior.",
            inputs={"baseline": baseline},
            outputs={"after_count": after_count},
        )
        return NodeResult(success=True, output={"after_count": after_count})

    dag.add_node(Node(id="behavior_check", run=_behavior_check, depends_on=["run_tests"]))

    policy_engine = PolicyEngine.default()

    async def _policy_check(node: Node, context) -> NodeResult:
        impl_files = context.get_output("implement_refactor", {}).get("files_written", [])
        test_files = context.get_output("write_new_tests", {}).get("files_written", [])
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

    dag.add_node(Node(id="policy_check", run=_policy_check, depends_on=["behavior_check"]))

    dag.add_node(build_release_node(
        node_id="release_readiness",
        agent=agent,
        payload_fn=lambda ctx: {"summary": "UrlService refactor ready for review"},
        depends_on=["policy_check", "update_docs"],
        requires_approval=True,
        decision_summary_fn=lambda out: f"Release status: {out.get('status', '(n/a)')}",
    ))

    async def _finalize(node: Node, context) -> NodeResult:
        commit_all(
            repo_root,
            "refactor: extract UrlValidator and dedupe active-row lookup in UrlService\n\n"
            "No behavior change: public API identical, no existing test edited, full\n"
            "baseline suite green. Adds focused UrlValidator unit tests.\n\n"
            "Orchestrator refactor demo scenario.",
        )
        push = run_git(repo_root, "push", "-u", "origin", BRANCH_NAME)
        return NodeResult(success=True, output={"pushed": push.returncode == 0, "push_stderr": push.stderr})

    dag.add_node(Node(id="finalize", run=_finalize, depends_on=["release_readiness"]))

    return dag


# --------------------------------------------------------------------------
# End-to-end runner (same pattern as the other scenarios).
# --------------------------------------------------------------------------

async def run(repo_root: str, auto_approve_all: bool = True) -> RunResult:
    staging_dir = Path(tempfile.mkdtemp(prefix="orchestrator_refactor_"))
    events_path = staging_dir / "events.jsonl"

    create_and_checkout_branch(repo_root, BRANCH_NAME, base="main")

    agent = build_refactor_agent(repo_root)
    dag = build_dag(agent, repo_root)
    sink = JsonlEventSink(path=events_path)
    approval = ApprovalManager(
        autonomy=AutonomyLevel.ASSISTED,
        decision_fn=auto_approve if auto_approve_all else interactive_prompt,
        event_sink=sink,
    )
    context = ExecutionContext(run_id="refactor-url-service")
    scheduler = Scheduler(event_sink=sink, approval_manager=approval, retry_executor=make_retrying_executor(event_sink=sink))

    result = await scheduler.run(dag, context)

    metrics = compute_metrics(sink.events)
    write_dashboard(staging_dir / "dashboard.html", run_id=context.run_id, dag=dag, metrics=metrics)
    write_run_summary(
        staging_dir / "SUMMARY.md",
        scenario_name="Refactor: extract UrlValidator, dedupe lookups in UrlService",
        requirement=RAW_REQUIREMENT,
        branch_name=BRANCH_NAME,
        context=context,
        metrics=metrics,
        risks=[
            "A refactor can change behavior in ways no test covers. Mitigated by keeping "
            "every moved line verbatim (same checks, same exception types and messages) "
            "and by refusing the change if any pre-existing test needed editing.",
        ],
        assumptions=[
            "UrlValidator is a stateless utility with static methods, matching the "
            "existing UrlMapper and Base62Encoder pattern. Making it a Spring bean would "
            "have changed UrlService's constructor and forced edits to existing tests.",
            "Scope is limited to UrlService. The controller and repository have no "
            "comparable duplication worth touching in the same change.",
        ],
        limitations=[
            "Behavior equivalence is shown by the existing test suite, not formally proven. "
            "Paths the baseline suite doesn't exercise are covered only by the verbatim move.",
        ],
    )

    run_git(repo_root, "checkout", "main")
    dest = Path(repo_root) / "orchestrator" / "runs" / "refactor"
    dest.mkdir(parents=True, exist_ok=True)
    for artifact in ("events.jsonl", "dashboard.html", "SUMMARY.md"):
        shutil.copy(staging_dir / artifact, dest / artifact)
    shutil.rmtree(staging_dir, ignore_errors=True)

    return result


# --------------------------------------------------------------------------
# Real scripted handlers.
# --------------------------------------------------------------------------

def build_refactor_agent(repo_root: str) -> DeterministicAgent:
    async def requirements_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(
            success=True,
            output={
                "normalized_requirement": "Restructure UrlService for single responsibility and "
                                           "no duplication, with zero observable behavior change.",
                "acceptance_criteria": [
                    "UrlService's public API is unchanged",
                    "Every pre-existing test passes without being edited",
                    "Validation logic is unit-testable on its own",
                ],
                "open_questions": [],
            },
            rationale="'Without changing behavior' is the requirement's real constraint, so it "
                      "becomes checkable acceptance criteria rather than a promise.",
        )

    async def analysis_handler(payload: Dict[str, Any]) -> AgentResult:
        service = (Path(payload.get("repo_root", repo_root)) / SERVICE_PATH).read_text()
        smells = []
        if "private void validateOriginalUrl" in service and "private void validateExpiresAt" in service:
            smells.append("validation logic embedded in the persistence service")
        lookup_repeats = service.count(".filter(ShortUrlEntity::isActive)")
        if lookup_repeats > 1:
            smells.append(f"active-row lookup duplicated {lookup_repeats}x")
        return AgentResult(
            success=True,
            output={"smells": smells, "impacted_files": [SERVICE_PATH], "new_files": [VALIDATOR_PATH]},
            rationale="Read UrlService.java and counted the concrete duplication rather than "
                      "refactoring on a hunch.",
        )

    async def design_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(
            success=True,
            output={
                "approach": "Move validateOriginalUrl/validateExpiresAt verbatim into a static "
                            "UrlValidator; route both lookup helpers through one "
                            "findActiveByShortCodeOrThrow.",
                "rejected_alternatives": [
                    "UrlValidator as a Spring bean: changes UrlService's constructor and forces "
                    "edits to existing tests, which defeats the behavior-preservation check.",
                    "Bean Validation annotations instead: changes when and how errors surface "
                    "(400 field errors vs InvalidUrlException), a behavior change.",
                ],
            },
            rationale="Verbatim moves plus a shared private helper keep every error type, message "
                      "and status code identical.",
        )

    async def implementation_handler(payload: Dict[str, Any]) -> AgentResult:
        service = (Path(repo_root) / SERVICE_PATH).read_text()
        written = write_files(repo_root, [
            FileChange(VALIDATOR_PATH, _URL_VALIDATOR_JAVA),
            FileChange(SERVICE_PATH, _apply_service_refactor(service)),
        ])
        return AgentResult(success=True, output={"files_written": written})

    async def test_handler(payload: Dict[str, Any]) -> AgentResult:
        written = write_files(repo_root, [FileChange(VALIDATOR_TEST_PATH, _URL_VALIDATOR_TEST_JAVA)])
        return AgentResult(
            success=True,
            output={"files_written": written, "new_test_count": _URL_VALIDATOR_TEST_JAVA.count("@Test")},
            rationale="Validation was only tested indirectly through UrlService; it now has "
                      "direct, focused tests including edge cases the service tests skip.",
        )

    async def docs_handler(payload: Dict[str, Any]) -> AgentResult:
        readme = (Path(repo_root) / "README.md").read_text()
        written = write_files(repo_root, [FileChange("README.md", _apply_readme_changes(readme))])
        return AgentResult(success=True, output={"files_written": written})

    async def release_handler(payload: Dict[str, Any]) -> AgentResult:
        return AgentResult(success=True, output={"status": "ready", "summary": payload.get("summary", "")})

    return DeterministicAgent(handlers={
        "requirements": requirements_handler,
        "codebase_analysis": analysis_handler,
        "design": design_handler,
        "implementation": implementation_handler,
        "test": test_handler,
        "docs": docs_handler,
        "release": release_handler,
    })


# --------------------------------------------------------------------------
# Java/text templates and precise, idempotent edits to existing files.
# --------------------------------------------------------------------------

_URL_VALIDATOR_JAVA = """package com.urlshortener.service;

import com.urlshortener.exception.InvalidUrlException;

import java.net.URI;
import java.net.URISyntaxException;
import java.time.Instant;

/**
 * Stateless input validation for short-URL requests, extracted from
 * UrlService so it can be tested on its own. Static utility, matching the
 * UrlMapper and Base62Encoder pattern used elsewhere in this codebase.
 */
public final class UrlValidator {

    private UrlValidator() {
    }

    public static void validateOriginalUrl(String url) {
        URI uri;
        try {
            uri = new URI(url);
        } catch (URISyntaxException e) {
            throw new InvalidUrlException("Malformed URL: " + url);
        }
        String scheme = uri.getScheme();
        if (scheme == null || !(scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https"))) {
            throw new InvalidUrlException("URL must use http or https scheme: " + url);
        }
        if (uri.getHost() == null) {
            throw new InvalidUrlException("Malformed URL: " + url);
        }
    }

    public static void validateExpiresAt(Instant expiresAt) {
        if (expiresAt != null && expiresAt.isBefore(Instant.now())) {
            throw new InvalidUrlException("expiresAt must be in the future");
        }
    }
}
"""

_URL_VALIDATOR_TEST_JAVA = """package com.urlshortener.service;

import com.urlshortener.exception.InvalidUrlException;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class UrlValidatorTest {

    @Test
    void acceptsHttpAndHttpsUrls() {
        assertThatCode(() -> UrlValidator.validateOriginalUrl("http://example.com")).doesNotThrowAnyException();
        assertThatCode(() -> UrlValidator.validateOriginalUrl("https://example.com/a/b?c=d")).doesNotThrowAnyException();
    }

    @Test
    void schemeCheckIsCaseInsensitive() {
        assertThatCode(() -> UrlValidator.validateOriginalUrl("HTTPS://example.com")).doesNotThrowAnyException();
    }

    @Test
    void rejectsNonHttpSchemes() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("ftp://example.com"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("http or https");
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("javascript:alert(1)"))
                .isInstanceOf(InvalidUrlException.class);
    }

    @Test
    void rejectsUrlWithoutScheme() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("example.com/page"))
                .isInstanceOf(InvalidUrlException.class);
    }

    @Test
    void rejectsSyntacticallyMalformedUrl() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("http://exa mple.com"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("Malformed URL");
    }

    @Test
    void rejectsUrlWithoutHost() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("http:///path-only"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("Malformed URL");
    }

    @Test
    void nullExpiryMeansNeverExpiresAndIsAllowed() {
        assertThatCode(() -> UrlValidator.validateExpiresAt(null)).doesNotThrowAnyException();
    }

    @Test
    void futureExpiryIsAllowed() {
        assertThatCode(() -> UrlValidator.validateExpiresAt(Instant.now().plus(1, ChronoUnit.DAYS)))
                .doesNotThrowAnyException();
    }

    @Test
    void pastExpiryIsRejected() {
        assertThatThrownBy(() -> UrlValidator.validateExpiresAt(Instant.now().minus(1, ChronoUnit.MINUTES)))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessage("expiresAt must be in the future");
    }
}
"""

_VALIDATION_METHODS = (
    "\n    private void validateOriginalUrl(String url) {\n"
    "        URI uri;\n"
    "        try {\n"
    "            uri = new URI(url);\n"
    "        } catch (URISyntaxException e) {\n"
    "            throw new InvalidUrlException(\"Malformed URL: \" + url);\n"
    "        }\n"
    "        String scheme = uri.getScheme();\n"
    "        if (scheme == null || !(scheme.equalsIgnoreCase(\"http\") || scheme.equalsIgnoreCase(\"https\"))) {\n"
    "            throw new InvalidUrlException(\"URL must use http or https scheme: \" + url);\n"
    "        }\n"
    "        if (uri.getHost() == null) {\n"
    "            throw new InvalidUrlException(\"Malformed URL: \" + url);\n"
    "        }\n"
    "    }\n"
    "\n    private void validateExpiresAt(Instant expiresAt) {\n"
    "        if (expiresAt != null && expiresAt.isBefore(Instant.now())) {\n"
    "            throw new InvalidUrlException(\"expiresAt must be in the future\");\n"
    "        }\n"
    "    }\n"
)

_LOOKUP_DUPLICATE = (
    "        ShortUrlEntity entity = repository.findByShortCode(shortCode)\n"
    "                .filter(ShortUrlEntity::isActive)\n"
    "                .orElseThrow(() -> new UrlNotFoundException(\"Short code not found: \" + shortCode));\n"
)

_LOOKUP_HELPER = (
    "\n    /** Shared by both lookups: the row must exist and not be soft-deleted, else 404. */\n"
    "    private ShortUrlEntity findActiveByShortCodeOrThrow(String shortCode) {\n"
    "        return repository.findByShortCode(shortCode)\n"
    "                .filter(ShortUrlEntity::isActive)\n"
    "                .orElseThrow(() -> new UrlNotFoundException(\"Short code not found: \" + shortCode));\n"
    "    }\n"
)

_PARSE_TOKEN_ANCHOR = "\n    private UUID parseOwnerToken(String ownerTokenHeader) {\n"


def _apply_service_refactor(original: str) -> str:
    if "UrlValidator." in original:
        return original  # already applied: no-op
    required = [_VALIDATION_METHODS, _PARSE_TOKEN_ANCHOR, "import java.net.URI;\n",
                "import com.urlshortener.exception.InvalidUrlException;\n"]
    if any(piece not in original for piece in required) or original.count(_LOOKUP_DUPLICATE) != 2:
        raise ValueError("UrlService no longer matches the analyzed shape; refusing to refactor blindly")

    updated = original.replace(_VALIDATION_METHODS, "")
    updated = updated.replace("import com.urlshortener.exception.InvalidUrlException;\n", "")
    updated = updated.replace("import java.net.URI;\nimport java.net.URISyntaxException;\n", "")
    updated = re.sub(r"(?<![\w.])validateOriginalUrl\(", "UrlValidator.validateOriginalUrl(", updated)
    updated = re.sub(r"(?<![\w.])validateExpiresAt\(", "UrlValidator.validateExpiresAt(", updated)
    updated = updated.replace(
        _LOOKUP_DUPLICATE,
        "        ShortUrlEntity entity = findActiveByShortCodeOrThrow(shortCode);\n",
    )
    return updated.replace(_PARSE_TOKEN_ANCHOR, _LOOKUP_HELPER + _PARSE_TOKEN_ANCHOR, 1)


_README_ARCH_ANCHOR = (
    "- No durability — a restart, crash, or redeploy loses all data. Suitable for\n"
    "  prototyping/demos, not for links that need to survive a deploy.\n"
)

_README_ARCH_ADDITION = (
    "- Code layout — `UrlController` handles HTTP only; `UrlService` owns business\n"
    "  rules and persistence; `UrlValidator` holds stateless input validation\n"
    "  (URL scheme/host, expiry), unit-tested on its own in `UrlValidatorTest`;\n"
    "  `UrlMapper` and `Base62Encoder` are stateless helpers in the same style.\n"
)


def _apply_readme_changes(original: str) -> str:
    if "UrlValidatorTest" in original:
        return original  # already applied: no-op
    return original.replace(_README_ARCH_ANCHOR, _README_ARCH_ANCHOR + _README_ARCH_ADDITION, 1)
