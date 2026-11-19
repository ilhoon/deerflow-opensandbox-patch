"""Tests for deerflow_ext.opensandbox_skills (provider and present_files middleware).

Runs in the Dockerfile ``test`` stage, inside the DeerFlow release image the
provider is built for (``deerflow_ext`` is importable from /app/backend).

The fake remote executes the provider's real shell scripts (tar extraction,
category replacement) on the local machine, with sandbox paths mapped below a
temporary root, so upload and replacement semantics are exercised for real.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import types
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from deerflow.config.paths import Paths
from deerflow_ext.opensandbox_skills import PresentFilesOutputSyncMiddleware, SkillAwareOpenSandboxProvider


@dataclass
class _Message:
    text: str


@dataclass
class _Logs:
    stdout: list[_Message] = field(default_factory=list)
    stderr: list[_Message] = field(default_factory=list)


@dataclass
class _Execution:
    exit_code: int | None = 0
    logs: _Logs = field(default_factory=_Logs)
    result: list = field(default_factory=list)
    error: Any = None


@dataclass
class _RunCommandOpts:
    timeout: timedelta | None = None
    envs: dict[str, str] | None = None


class _Files:
    def __init__(self, remote: _Remote) -> None:
        self._remote = remote

    def write_file(self, path: str, data, *, mode: int = 755) -> None:
        payload = data.encode() if isinstance(data, str) else data if isinstance(data, bytes) else data.read()
        self._remote.uploads.append(path)
        target = self._remote.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def read_bytes_stream(self, path: str):
        yield self._remote.local(path).read_bytes()


class _Commands:
    def __init__(self, remote: _Remote) -> None:
        self._remote = remote

    def run(self, command: str, *, opts: _RunCommandOpts | None = None) -> _Execution:
        if command == "true":
            return _Execution()
        if "__DF_OUTPUT_SYNC__" in command:
            # The provider's listing contract: base64 of NUL-terminated remote paths.
            outputs = self._remote.local("/mnt/user-data/outputs")
            paths = sorted(f"/mnt/user-data/outputs/{p.relative_to(outputs).as_posix()}" for p in outputs.rglob("*") if p.is_file()) if outputs.is_dir() else []
            listing = base64.b64encode("".join(f"{p}\0" for p in paths).encode()).decode()
            return _Execution(logs=_Logs(stdout=[_Message(f"{listing}\n__DF_OUTPUT_SYNC__:0:0:0")]))
        if "tar -xzf" in command and any(f"-C {root} " in command for root in self._remote.fail_extract_into):
            return _Execution(exit_code=1, logs=_Logs(stderr=[_Message("tar: injected failure")]))
        mapped = command.replace("/mnt/", f"{self._remote.root}/mnt/").replace("/tmp/deerflow-upload-", f"{self._remote.root}/tmp/deerflow-upload-")
        completed = subprocess.run(["bash", "-c", mapped], capture_output=True, text=True, check=False)
        return _Execution(
            exit_code=completed.returncode,
            logs=_Logs(stdout=[_Message(completed.stdout)], stderr=[_Message(completed.stderr)]),
        )


class _Remote:
    def __init__(self, remote_id: str, root: Path) -> None:
        self.id = remote_id
        self.root = root
        self.uploads: list[str] = []
        self.fail_extract_into: set[str] = set()
        self.destroy_calls = 0
        self.files = _Files(self)
        self.commands = _Commands(self)

    def local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def renew(self, timeout: timedelta) -> None:
        pass

    def destroy(self) -> None:
        self.destroy_calls += 1

    def tarballs(self) -> int:
        return sum(1 for path in self.uploads if path.endswith(".tar.gz"))


class _SandboxClass:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.remotes: list[_Remote] = []
        self.fail_extract_into: set[str] = set()

    def create(self, image: str, **kwargs: Any) -> _Remote:
        remote = _Remote(f"remote-{len(self.remotes) + 1}", self.base / f"remote-{len(self.remotes) + 1}")
        remote.fail_extract_into = self.fail_extract_into
        self.remotes.append(remote)
        return remote


class _ConnectionConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _projection(root: Path, files: dict[str, str]) -> types.SimpleNamespace:
    categories = {name: root / name for name in ("public", "custom", "legacy", "integrations")}
    for path in categories.values():
        path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return types.SimpleNamespace(**categories)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    paths = Paths(base_dir=tmp_path / "home")
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    state = types.SimpleNamespace(paths=paths, tmp=tmp_path, sdk=_SandboxClass(tmp_path / "remotes"), skills_root="/mnt/skills", providers=[])
    state.projection = _projection(tmp_path / "shared", {"public/research/SKILL.md": "# research"})

    def app_config():
        return types.SimpleNamespace(
            sandbox=types.SimpleNamespace(idle_timeout=0),
            skills=types.SimpleNamespace(container_path=state.skills_root),
        )

    monkeypatch.setattr("deerflow.community.opensandbox.provider.get_app_config", app_config)
    monkeypatch.setattr("deerflow_ext.opensandbox_skills.get_app_config", app_config)
    monkeypatch.setattr("deerflow.community.opensandbox.provider._import_sdk", lambda: (state.sdk, _ConnectionConfig, _RunCommandOpts))
    monkeypatch.setattr("deerflow.skills.storage.get_or_new_user_skill_storage", lambda user_id, app_config=None: object())
    monkeypatch.setattr("deerflow.skills.projection.ensure_skill_projections", lambda storage: state.projection)

    def new_provider() -> SkillAwareOpenSandboxProvider:
        provider = SkillAwareOpenSandboxProvider()
        state.providers.append(provider)
        return provider

    state.new_provider = new_provider
    yield state
    for provider in state.providers:
        provider.shutdown()


def test_new_sandbox_receives_shared_skills_with_modes(env) -> None:
    script = Path(env.projection.public) / "research/scripts/run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')")
    script.chmod(0o755)
    provider = env.new_provider()

    provider.acquire("thread", user_id="owner")

    remote = env.sdk.remotes[0]
    assert remote.local("/mnt/skills/public/research/SKILL.md").read_text() == "# research"
    assert remote.local("/mnt/skills/public/research/scripts/run.py").stat().st_mode & 0o777 == 0o755
    assert not list(remote.local("/tmp").glob("deerflow-upload-*")), "uploaded archive must be removed after extraction"


def test_reacquire_skips_unchanged_projection_and_applies_changes(env) -> None:
    provider = env.new_provider()
    sandbox_id = provider.acquire("thread", user_id="owner")
    remote = env.sdk.remotes[0]
    provider.release(sandbox_id)
    assert provider.acquire("thread", user_id="owner") == sandbox_id
    assert remote.tarballs() == 1

    (Path(env.projection.public) / "research/SKILL.md").unlink()
    (Path(env.projection.public) / "research").rmdir()
    (Path(env.projection.custom) / "mine").mkdir()
    (Path(env.projection.custom) / "mine/SKILL.md").write_text("# mine")
    provider.release(sandbox_id)
    provider.acquire("thread", user_id="owner")

    assert len(env.sdk.remotes) == 1
    assert remote.tarballs() == 2
    assert remote.local("/mnt/skills/custom/mine/SKILL.md").read_text() == "# mine"
    assert not remote.local("/mnt/skills/public/research").exists(), "disabled or removed skills must disappear"


def test_policy_thread_skips_shared_skills_and_sync_replaces_only_managed_categories(env) -> None:
    env.paths.thread_skills_view_dir("thread", user_id="owner").mkdir(parents=True)
    provider = env.new_provider()
    sandbox_id = provider.acquire("thread", user_id="owner")
    remote = env.sdk.remotes[0]
    assert not remote.local("/mnt/skills/public").exists()

    remote.local("/mnt/skills/notes.txt").parent.mkdir(parents=True, exist_ok=True)
    remote.local("/mnt/skills/notes.txt").write_text("unmanaged")
    remote.local("/mnt/skills/public/stale/SKILL.md").parent.mkdir(parents=True)
    remote.local("/mnt/skills/public/stale/SKILL.md").write_text("stale")
    policy = _projection(env.tmp / "policy", {"custom/mine/SKILL.md": "# mine"})

    provider.sync_agent_skills(sandbox_id, thread_id="thread", user_id="owner", projection=policy)

    assert remote.local("/mnt/skills/custom/mine/SKILL.md").read_text() == "# mine"
    assert not remote.local("/mnt/skills/public/stale").exists()
    assert remote.local("/mnt/skills/notes.txt").read_text() == "unmanaged"


def test_sync_on_unknown_sandbox_raises(env) -> None:
    provider = env.new_provider()
    with pytest.raises(RuntimeError, match="not available"):
        provider.sync_agent_skills("missing", thread_id="thread", user_id="owner", projection=env.projection)


def test_shared_upload_failure_warns_but_acquire_succeeds(env, caplog: pytest.LogCaptureFixture) -> None:
    env.sdk.fail_extract_into.add("/mnt/skills")
    provider = env.new_provider()
    with caplog.at_level(logging.WARNING, logger="deerflow_ext.opensandbox_skills"):
        sandbox_id = provider.acquire("thread", user_id="owner")
    assert provider.get(sandbox_id) is not None
    assert "Could not upload the skills projection" in caplog.text


def test_policy_sync_failure_propagates(env) -> None:
    env.paths.thread_skills_view_dir("thread", user_id="owner").mkdir(parents=True)
    provider = env.new_provider()
    sandbox_id = provider.acquire("thread", user_id="owner")
    env.sdk.fail_extract_into.add("/mnt/skills")
    with pytest.raises(RuntimeError, match="upload into /mnt/skills failed"):
        provider.sync_agent_skills(sandbox_id, thread_id="thread", user_id="owner", projection=env.projection)


def test_oversized_projection_keeps_previous_tree(env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.paths.thread_skills_view_dir("thread", user_id="owner").mkdir(parents=True)
    provider = env.new_provider()
    sandbox_id = provider.acquire("thread", user_id="owner")
    provider.sync_agent_skills(sandbox_id, thread_id="thread", user_id="owner", projection=env.projection)
    remote = env.sdk.remotes[0]
    big = _projection(env.tmp / "big", {"custom/a/SKILL.md": "a", "custom/b/SKILL.md": "b"})
    monkeypatch.setattr(SkillAwareOpenSandboxProvider, "_MAX_UPLOAD_FILES", 1)

    with pytest.raises(RuntimeError, match="exceeds the OpenSandbox upload limit"):
        provider.sync_agent_skills(sandbox_id, thread_id="thread", user_id="owner", projection=big)

    assert remote.local("/mnt/skills/public/research/SKILL.md").read_text() == "# research"
    assert not remote.local("/mnt/skills/custom/a").exists()


def test_recreated_sandbox_restores_gateway_thread_files(env) -> None:
    uploads = env.paths.sandbox_uploads_dir("thread", user_id="owner")
    outputs = env.paths.sandbox_outputs_dir("thread", user_id="owner")
    uploads.mkdir(parents=True)
    outputs.mkdir(parents=True)
    (uploads / "data.csv").write_text("a,b\n1,2\n")
    (outputs / "report.md").write_text("# report")

    # A new provider process stands in for a remote that the warm pool reaped.
    env.new_provider().acquire("thread", user_id="owner")

    remote = env.sdk.remotes[0]
    assert remote.local("/mnt/user-data/uploads/data.csv").read_text() == "a,b\n1,2\n"
    assert remote.local("/mnt/user-data/outputs/report.md").read_text() == "# report"
    assert not env.paths.sandbox_uploads_dir("thread", user_id="other").exists()


def test_restore_failure_destroys_remote(env) -> None:
    uploads = env.paths.sandbox_uploads_dir("thread", user_id="owner")
    uploads.mkdir(parents=True)
    (uploads / "data.csv").write_text("x")
    env.sdk.fail_extract_into.add("/mnt/user-data")
    provider = env.new_provider()

    with pytest.raises(RuntimeError, match="upload into /mnt/user-data failed"):
        provider.acquire("thread", user_id="owner")

    assert env.sdk.remotes[0].destroy_calls == 1
    assert provider._sandboxes == {}


@pytest.mark.parametrize("root", ["/", "/mnt", "/tmp", "/etc/skills", "/mnt/user-data/skills", "/mnt//skills", "skills"])
def test_unsafe_skills_root_is_rejected(env, root: str) -> None:
    env.skills_root = root
    with pytest.raises(ValueError, match="skills.container_path"):
        env.new_provider()


def _tool_request(name: str, sandbox_id: str | None) -> types.SimpleNamespace:
    state = {"sandbox": {"sandbox_id": sandbox_id}} if sandbox_id else {}
    return types.SimpleNamespace(tool_call={"name": name, "args": {}, "id": "call-1"}, runtime=types.SimpleNamespace(state=state, context={}))


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_present_files_sees_outputs_on_gateway_before_tool_runs(env, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    provider = env.new_provider()
    sandbox_id = provider.acquire("thread", user_id="owner")
    remote = env.sdk.remotes[0]
    image = remote.local("/mnt/user-data/outputs/images/cat.jpg")
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr("deerflow_ext.opensandbox_skills.get_sandbox_provider", lambda: provider)
    host_image = env.paths.sandbox_outputs_dir("thread", user_id="owner") / "images/cat.jpg"
    seen: list[bytes] = []

    def handler(request):
        seen.append(host_image.read_bytes())
        return "presented"

    async def ahandler(request):
        return handler(request)

    middleware = PresentFilesOutputSyncMiddleware()
    request = _tool_request("present_files", sandbox_id)
    result = middleware.wrap_tool_call(request, handler) if mode == "sync" else asyncio.run(middleware.awrap_tool_call(request, ahandler))

    assert result == "presented"
    assert seen == [b"\xff\xd8jpeg"], "the artifact must exist on the Gateway when present_files publishes it"


def test_other_tools_skip_sync_and_sync_failure_keeps_presenting(env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    class _Provider:
        def sync_outputs(self, sandbox_id: str) -> None:
            calls.append(sandbox_id)
            raise OSError("listing failed")

    monkeypatch.setattr("deerflow_ext.opensandbox_skills.get_sandbox_provider", lambda: _Provider())
    middleware = PresentFilesOutputSyncMiddleware()

    assert middleware.wrap_tool_call(_tool_request("bash", "sb-1"), lambda request: "ran") == "ran"
    assert calls == []
    with caplog.at_level(logging.WARNING, logger="deerflow_ext.opensandbox_skills"):
        assert middleware.wrap_tool_call(_tool_request("present_files", "sb-1"), lambda request: "presented") == "presented"
    assert calls == ["sb-1"]
    assert "Could not mirror OpenSandbox outputs" in caplog.text
