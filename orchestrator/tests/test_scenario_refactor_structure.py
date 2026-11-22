import pytest

import orchestrator.scenarios.refactor_url_service as refactor
from orchestrator.agents.base import AgentResult, AgentTask
from orchestrator.engine.approval import ApprovalManager, AutonomyLevel, auto_approve, auto_deny
from orchestrator.engine.context import ExecutionContext
from orchestrator.engine.dag import NodeStatus, Scheduler

_SERVICE = "public class UrlService {\n    public String resolve(String shortCode) {\n    }\n}\n"


class _StubAgent:
    async def run(self, task: AgentTask) -> AgentResult:
        if task.stage == "implementation":
            return AgentResult(success=True, output={"files_written": ["fake/Impl.java"]})
        if task.stage == "test":
            return AgentResult(success=True, output={"files_written": ["fake/NewTest.java"], "new_test_count": 9})
        return AgentResult(success=True, output={"stage": task.stage})


class _Proc:
    def __init__(self, stdout=""):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Fake repo whose UrlService/test-count/git-status can be changed mid-run."""
    service = tmp_path / refactor.SERVICE_PATH
    service.parent.mkdir(parents=True)
    service.write_text(_SERVICE)
    state = {"counts": [78, 87], "git_status": "?? src/test/java/com/urlshortener/service/UrlValidatorTest.java\n"}

    monkeypatch.setattr(refactor, "run_maven_test", lambda repo_root, extra_args=(): (True, "BUILD SUCCESS"))
    monkeypatch.setattr(refactor, "count_maven_tests", lambda repo_root: state["counts"].pop(0))
    monkeypatch.setattr(refactor, "commit_all", lambda repo_root, message: _Proc())
    monkeypatch.setattr(refactor, "run_git", lambda repo_root, *args: _Proc(state["git_status"]))
    return tmp_path, state


async def _run(dag, decision_fn=auto_approve):
    manager = ApprovalManager(autonomy=AutonomyLevel.ASSISTED, decision_fn=decision_fn)
    return await Scheduler(approval_manager=manager).run(dag, ExecutionContext(run_id="refactor-test"))


def test_refactor_dag_takes_a_baseline_before_any_file_is_written():
    dag = refactor.build_dag(_StubAgent(), repo_root="/fake/repo")

    assert dag.nodes["baseline_tests"].depends_on == ["design"]
    for writer in ("implement_refactor", "write_new_tests", "update_docs"):
        assert dag.nodes[writer].depends_on == ["baseline_tests"]
    assert set(dag.nodes["run_tests"].depends_on) == {"implement_refactor", "write_new_tests"}
    assert dag.nodes["behavior_check"].depends_on == ["run_tests"]
    assert dag.nodes["policy_check"].depends_on == ["behavior_check"]
    assert set(dag.nodes["release_readiness"].depends_on) == {"policy_check", "update_docs"}
    assert dag.nodes["release_readiness"].requires_approval is True


@pytest.mark.asyncio
async def test_refactor_succeeds_when_behavior_is_preserved(fake_repo):
    repo, _ = fake_repo
    dag = refactor.build_dag(_StubAgent(), repo_root=str(repo))

    result = await _run(dag)

    assert result.succeeded
    assert dag.nodes["behavior_check"].status == NodeStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_behavior_check_fails_when_a_test_goes_missing(fake_repo):
    repo, state = fake_repo
    state["counts"] = [78, 86]  # one baseline test vanished
    dag = refactor.build_dag(_StubAgent(), repo_root=str(repo))

    result = await _run(dag)

    assert not result.succeeded
    assert dag.nodes["behavior_check"].status == NodeStatus.FAILED
    assert dag.nodes["finalize"].status == NodeStatus.BLOCKED


@pytest.mark.asyncio
async def test_behavior_check_fails_when_an_existing_test_was_edited(fake_repo):
    repo, state = fake_repo
    state["git_status"] = " M src/test/java/com/urlshortener/service/UrlServiceTest.java\n"
    dag = refactor.build_dag(_StubAgent(), repo_root=str(repo))

    result = await _run(dag)

    assert not result.succeeded
    assert dag.nodes["behavior_check"].status == NodeStatus.FAILED


@pytest.mark.asyncio
async def test_behavior_check_fails_when_public_api_changes(fake_repo):
    repo, _ = fake_repo

    class _ApiChangingAgent(_StubAgent):
        async def run(self, task):
            if task.stage == "implementation":
                (repo / refactor.SERVICE_PATH).write_text(_SERVICE.replace("resolve(String shortCode)", "resolve(String code, boolean x)"))
            return await super().run(task)

    dag = refactor.build_dag(_ApiChangingAgent(), repo_root=str(repo))

    result = await _run(dag)

    assert not result.succeeded
    assert dag.nodes["behavior_check"].status == NodeStatus.FAILED


@pytest.mark.asyncio
async def test_refactor_blocks_at_release_when_approval_denied(fake_repo):
    repo, _ = fake_repo
    dag = refactor.build_dag(_StubAgent(), repo_root=str(repo))

    result = await _run(dag, decision_fn=auto_deny)

    assert not result.succeeded
    assert dag.nodes["behavior_check"].status == NodeStatus.SUCCEEDED
    assert dag.nodes["release_readiness"].status == NodeStatus.BLOCKED


def test_public_signatures_ignore_formatting_and_private_methods():
    a = "public class X {\n    public int foo(int a,\n               int b) {}\n    private void bar() {}\n}"
    b = "public class X {\n    public int foo(int a, int b) {}\n}"
    assert refactor.public_signatures(a) == refactor.public_signatures(b) == ["public int foo(int a, int b)"]


def test_modified_existing_test_files_allows_new_files_only():
    status = "?? src/test/New.java\n M src/test/Old.java\nD  src/test/Gone.java\n"
    assert refactor.modified_existing_test_files(status) == ["src/test/Old.java", "src/test/Gone.java"]


def test_refactor_edits_are_idempotent_and_refuse_unexpected_shapes():
    service = (
        "import com.urlshortener.exception.InvalidUrlException;\n"
        "import java.net.URI;\nimport java.net.URISyntaxException;\n"
        "    void a() {\n        validateOriginalUrl(u);\n" + refactor._LOOKUP_DUPLICATE + "    }\n"
        "    void b() {\n        validateExpiresAt(e);\n" + refactor._LOOKUP_DUPLICATE + "    }\n"
        + refactor._PARSE_TOKEN_ANCHOR + "    }\n" + refactor._VALIDATION_METHODS + "}\n"
    )
    once = refactor._apply_service_refactor(service)
    assert refactor._apply_service_refactor(once) == once
    assert "UrlValidator.validateOriginalUrl(u)" in once and "UrlValidator.validateExpiresAt(e)" in once
    assert "private void validateOriginalUrl" not in once
    assert once.count("findActiveByShortCodeOrThrow(shortCode)") == 2  # two callers; helper uses repository
    assert "import java.net.URI;" not in once

    with pytest.raises(ValueError):
        refactor._apply_service_refactor("class UrlService {}")

    readme_once = refactor._apply_readme_changes(refactor._README_ARCH_ANCHOR)
    assert refactor._apply_readme_changes(readme_once) == readme_once
