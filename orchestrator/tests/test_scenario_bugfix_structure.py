import pytest

import orchestrator.scenarios.bugfix_alias_collision as bugfix
from orchestrator.agents.base import AgentResult, AgentTask
from orchestrator.engine.approval import ApprovalManager, AutonomyLevel, auto_approve, auto_deny
from orchestrator.engine.context import ExecutionContext
from orchestrator.engine.dag import NodeStatus, Scheduler


class _StubAgent:
    async def run(self, task: AgentTask) -> AgentResult:
        if task.stage == "implementation":
            return AgentResult(success=True, output={"files_written": ["fake/Impl.java"]})
        if task.stage == "test":
            return AgentResult(success=True, output={"files_written": ["fake/RegressionTest.java"]})
        return AgentResult(success=True, output={"stage": task.stage})


class _FakeCompletedProcess:
    returncode = 0
    stdout = ""
    stderr = ""


def _fake_maven(regression_passes_before_fix=False, failure_output="DataIntegrityViolationException"):
    """Narrowed runs (extra_args) are the pre-fix reproduction; full runs are post-fix."""
    def _run(repo_root, extra_args=()):
        if extra_args:
            return (True, "BUILD SUCCESS") if regression_passes_before_fix else (False, failure_output)
        return True, "BUILD SUCCESS"
    return _run


@pytest.fixture(autouse=True)
def _fake_git(monkeypatch):
    monkeypatch.setattr(bugfix, "commit_all", lambda repo_root, message: _FakeCompletedProcess())
    monkeypatch.setattr(bugfix, "run_git", lambda repo_root, *args: _FakeCompletedProcess())


async def _run(dag, decision_fn=auto_approve):
    manager = ApprovalManager(autonomy=AutonomyLevel.ASSISTED, decision_fn=decision_fn)
    return await Scheduler(approval_manager=manager).run(dag, ExecutionContext(run_id="bugfix-test"))


def test_bugfix_dag_enforces_reproduce_before_fix_ordering():
    dag = bugfix.build_dag(_StubAgent(), repo_root="/fake/repo")

    assert dag.nodes["root_cause_analysis"].depends_on == ["intake_bug_report"]
    assert dag.nodes["design_fix"].depends_on == ["root_cause_analysis"]
    assert dag.nodes["write_regression_test"].depends_on == ["design_fix"]
    assert dag.nodes["reproduce_bug"].depends_on == ["write_regression_test"]
    # The structural rule: no production change until the bug is reproduced.
    assert dag.nodes["implement_fix"].depends_on == ["reproduce_bug"]
    assert dag.nodes["run_tests"].depends_on == ["implement_fix"]
    assert dag.nodes["policy_check"].depends_on == ["run_tests"]
    assert set(dag.nodes["release_readiness"].depends_on) == {"policy_check", "update_docs"}
    assert dag.nodes["release_readiness"].requires_approval is True
    assert dag.nodes["finalize"].depends_on == ["release_readiness"]


@pytest.mark.asyncio
async def test_bugfix_run_succeeds_when_bug_reproduces_then_suite_passes(monkeypatch):
    monkeypatch.setattr(bugfix, "run_maven_test", _fake_maven())
    dag = bugfix.build_dag(_StubAgent(), repo_root="/fake/repo")

    result = await _run(dag)

    assert result.succeeded
    assert dag.nodes["reproduce_bug"].status == NodeStatus.SUCCEEDED
    assert dag.nodes["finalize"].status == NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_fix_is_never_applied_when_regression_test_passes_on_unfixed_code(monkeypatch):
    monkeypatch.setattr(bugfix, "run_maven_test", _fake_maven(regression_passes_before_fix=True))
    dag = bugfix.build_dag(_StubAgent(), repo_root="/fake/repo")

    result = await _run(dag)

    assert not result.succeeded
    assert dag.nodes["reproduce_bug"].status == NodeStatus.FAILED
    assert dag.nodes["implement_fix"].status == NodeStatus.BLOCKED
    assert dag.nodes["finalize"].status == NodeStatus.BLOCKED


@pytest.mark.asyncio
async def test_failure_for_the_wrong_reason_is_not_a_reproduction(monkeypatch):
    monkeypatch.setattr(bugfix, "run_maven_test", _fake_maven(failure_output="COMPILATION ERROR"))
    dag = bugfix.build_dag(_StubAgent(), repo_root="/fake/repo")

    result = await _run(dag)

    assert not result.succeeded
    assert dag.nodes["reproduce_bug"].status == NodeStatus.FAILED
    assert dag.nodes["implement_fix"].status == NodeStatus.BLOCKED


@pytest.mark.asyncio
async def test_bugfix_blocks_at_release_when_approval_denied(monkeypatch):
    monkeypatch.setattr(bugfix, "run_maven_test", _fake_maven())
    dag = bugfix.build_dag(_StubAgent(), repo_root="/fake/repo")

    result = await _run(dag, decision_fn=auto_deny)

    assert not result.succeeded
    assert dag.nodes["run_tests"].status == NodeStatus.SUCCEEDED
    assert dag.nodes["release_readiness"].status == NodeStatus.BLOCKED
    assert dag.nodes["finalize"].status == NodeStatus.BLOCKED


_UNFIXED_SERVICE = (
    "        return UrlMapper.toResponse(entity, baseUrl);\n    }\n\n    public UrlResponse getDetails(String s) {}\n"
)


def test_service_fix_is_idempotent_and_refuses_unexpected_shapes():
    original = "x\n" + bugfix._UNFIXED_GENERATED_BRANCH + _UNFIXED_SERVICE
    once = bugfix._apply_service_fix(original)
    assert bugfix._apply_service_fix(once) == once
    assert once.count("while (repository.existsByShortCode(code))") == 1
    assert once.count("private ShortUrlEntity newPlaceholderEntity(") == 1

    with pytest.raises(ValueError):
        bugfix._apply_service_fix("class UrlService { /* rewritten since analysis */ }")


def test_test_and_readme_edits_are_idempotent():
    test_once = bugfix._apply_service_test_changes(bugfix._UNIT_TEST_ANCHOR)
    assert bugfix._apply_service_test_changes(test_once) == test_once
    assert test_once.count(bugfix.UNIT_TEST_METHOD) == 1

    readme_once = bugfix._apply_readme_changes("### Custom alias\n\n```bash\n" + bugfix._README_ALIAS_ANCHOR)
    assert bugfix._apply_readme_changes(readme_once) == readme_once
    assert readme_once.count("share one namespace") == 1
