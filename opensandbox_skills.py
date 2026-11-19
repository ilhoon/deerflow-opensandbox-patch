"""OpenSandboxProvider that also ships skills and thread files into the remote.

Upstream ``deerflow.community.opensandbox.OpenSandboxProvider`` never uploads
the skills projection, so ``/mnt/skills`` is empty inside the sandbox, and a
freshly created remote (after the warm pool reaps one) starts without the
thread's uploads and outputs. This subclass adds both, mirroring
``BoxdSandboxProvider``:

- a new remote receives the Gateway-side ``uploads``/``workspace``/``outputs``;
- every acquire refreshes the user's shared skills projection (skipped when the
  projection is unchanged, and for threads with an Agent skill policy view);
- ``sync_agent_skills`` replaces the managed skill categories with a per-Agent
  projection, so the provider can enforce Agent skill isolation.

Select it with::

    sandbox:
      use: deerflow_ext.opensandbox_skills:SkillAwareOpenSandboxProvider

It overrides private hooks of the upstream provider (``_load_config``,
``_create_sandbox``, ``_acquire_scope_locked``); the image build runs
``test_opensandbox_skills.py`` against every DeerFlow release it targets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import shlex
import stat
import tarfile
import tempfile
import threading
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

from deerflow.community.opensandbox.provider import OpenSandboxProvider
from deerflow.community.opensandbox.sandbox import OpenSandboxSandbox, execution_stdout
from deerflow.config import get_app_config
from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH

logger = logging.getLogger(__name__)

SKILL_CATEGORIES = ("public", "custom", "legacy", "integrations")
USER_DATA_ROOT = "/mnt/user-data"
_EXTRACT_TIMEOUT = 300.0
_PROTECTED_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var")


def _validate_skills_root(container_path: str) -> str:
    """Skill categories are deleted recursively, so the root must be a dedicated path."""
    normalized = posixpath.normpath(container_path or "")
    if not normalized.startswith("/") or normalized.startswith("//") or normalized == "/" or normalized != (container_path or "").rstrip("/"):
        raise ValueError(f"skills.container_path {container_path!r} is not a safe OpenSandbox skills root: use a normalized absolute path such as /mnt/skills")
    if normalized in {"/home", "/mnt", "/tmp"} or any(normalized == root or normalized.startswith(f"{root}/") for root in _PROTECTED_ROOTS):
        raise ValueError(f"skills.container_path {container_path!r} is not a safe OpenSandbox skills root: it is a system directory")
    if normalized == USER_DATA_ROOT or normalized.startswith(f"{USER_DATA_ROOT}/") or USER_DATA_ROOT.startswith(f"{normalized}/"):
        raise ValueError(f"skills.container_path {container_path!r} must not overlap {USER_DATA_ROOT}")
    return normalized


@dataclass(frozen=True)
class _Entry:
    path: Path
    arcname: str
    size: int
    mode: int
    mtime_ns: int


def _collect(sources: list[tuple[Path, str]]) -> list[_Entry]:
    """Regular files below each source directory; links and special files are skipped."""
    entries: list[_Entry] = []

    def scan_error(error: OSError) -> None:
        raise error

    for source, prefix in sources:
        for part in (source, *source.parents):
            if part.is_symlink():
                raise ValueError(f"Refusing symlinked upload root for {prefix}")
        if not source.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(source, followlinks=False, onerror=scan_error):
            parent = Path(directory)
            dirnames[:] = sorted(name for name in dirnames if not (parent / name).is_symlink())
            for name in sorted(filenames):
                path = parent / name
                st = path.lstat()
                if stat.S_ISREG(st.st_mode):
                    rel = path.relative_to(source).as_posix()
                    entries.append(_Entry(path, f"{prefix}/{rel}", st.st_size, st.st_mode & 0o755, st.st_mtime_ns))
    return entries


def _digest(sources: list[tuple[Path, str]], entries: list[_Entry]) -> str:
    """Identify a projection by its roots and file metadata (projections publish via atomic replace)."""
    h = hashlib.sha256()
    for source, prefix in sources:
        h.update(f"root\0{prefix}\0{source}\0".encode())
    for entry in entries:
        h.update(f"file\0{entry.arcname}\0{entry.size}\0{entry.mode}\0{entry.mtime_ns}\0".encode())
    return h.hexdigest()


class SkillAwareOpenSandboxProvider(OpenSandboxProvider):
    """OpenSandboxProvider plus skills projection upload and thread-file restore."""

    # The remote sees only the skills uploaded into it, so a per-Agent
    # projection from sync_agent_skills cannot be bypassed from the shell.
    supports_agent_skill_isolation = True
    _MAX_UPLOAD_FILES = 5000
    _MAX_UPLOAD_BYTES = 128 * 1024 * 1024

    def __init__(self) -> None:
        self._skills_digests: weakref.WeakKeyDictionary[OpenSandboxSandbox, str] = weakref.WeakKeyDictionary()
        self._skills_digests_lock = threading.Lock()
        super().__init__()

    def _load_config(self) -> dict:
        config = super()._load_config()
        skills_config = getattr(get_app_config(), "skills", None)
        config["skills_container_path"] = _validate_skills_root(getattr(skills_config, "container_path", None) or DEFAULT_SKILLS_CONTAINER_PATH)
        return config

    # ── Lifecycle hooks ──────────────────────────────────────────────────

    def _create_sandbox(self, sandbox_id: str, *, thread_id: str | None, user_id: str | None) -> OpenSandboxSandbox:
        sandbox = super()._create_sandbox(sandbox_id, thread_id=thread_id, user_id=user_id)
        if thread_id is None or not user_id:
            return sandbox
        try:
            self._restore_thread_files(sandbox, thread_id=thread_id, user_id=user_id)
        except BaseException:
            # Never hand out a half-restored remote, and never leave one running.
            self._destroy_quietly(sandbox, context="thread file restore failure")
            raise
        return sandbox

    def _acquire_scope_locked(self, key: tuple[str, str], sandbox_id: str, *, thread_id: str, user_id: str | None) -> str:
        acquired = super()._acquire_scope_locked(key, sandbox_id, thread_id=thread_id, user_id=user_id)
        if user_id:
            with self._lock:
                sandbox = self._sandboxes.get(acquired)
            if sandbox is not None:
                self._upload_shared_skills(sandbox, thread_id=thread_id, user_id=user_id)
        return acquired

    def sync_agent_skills(self, sandbox_id: str, *, thread_id: str, user_id: str, projection) -> None:
        """Replace the remote's skill categories with this thread's projection.

        Holds the same scope serializer as acquire and release, so no other
        lifecycle step sees a half-written skills tree. Failures propagate: the
        middleware then releases the sandbox instead of running unrestricted.
        """
        with self._acquire_serializer.hold(sandbox_id):
            with self._lock:
                sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is None:
                raise RuntimeError(f"OpenSandbox {sandbox_id} is not available for skill synchronization")
            self._upload_skills(sandbox, projection)

    # ── Skills ───────────────────────────────────────────────────────────

    def _upload_shared_skills(self, sandbox: OpenSandboxSandbox, *, thread_id: str, user_id: str) -> None:
        """Best effort: upload the user's shared skills projection.

        A thread with an Agent skill policy view gets its projection from the
        sandbox middleware through :meth:`sync_agent_skills` instead.
        """
        from deerflow.config.paths import get_paths
        from deerflow.skills.projection import ensure_skill_projections
        from deerflow.skills.storage import get_or_new_user_skill_storage

        try:
            if get_paths().thread_skills_view_dir(thread_id, user_id=user_id).exists():
                return
            storage = get_or_new_user_skill_storage(user_id, app_config=get_app_config())
            self._upload_skills(sandbox, ensure_skill_projections(storage))
        except Exception as exc:
            logger.warning("Could not upload the skills projection for OpenSandbox %s: %s", sandbox.id, exc)

    def _upload_skills(self, sandbox: OpenSandboxSandbox, projection) -> None:
        sources = [(Path(getattr(projection, category)), category) for category in SKILL_CATEGORIES]
        entries = _collect(sources)
        digest = _digest(sources, entries)
        with self._skills_digests_lock:
            if self._skills_digests.get(sandbox) == digest:
                return
        if not self._within_budget(entries):
            # Keep the remote's previous complete tree rather than a truncated one.
            raise RuntimeError(f"Skills projection exceeds the OpenSandbox upload limit ({self._MAX_UPLOAD_FILES} files / {self._MAX_UPLOAD_BYTES} bytes)")
        self._upload_entries(sandbox, entries, dest_root=self._config["skills_container_path"], replace=SKILL_CATEGORIES)
        with self._skills_digests_lock:
            self._skills_digests[sandbox] = digest

    # ── Thread files ─────────────────────────────────────────────────────

    def _restore_thread_files(self, sandbox: OpenSandboxSandbox, *, thread_id: str, user_id: str) -> None:
        """Give a fresh remote the thread's Gateway-side files.

        Uploads go first, then the workspace, then outputs. Files past the size
        limit are skipped with a warning so a large thread still gets a sandbox.
        """
        from deerflow.config.paths import get_paths

        paths = get_paths()
        sources = [
            (paths.sandbox_uploads_dir(thread_id, user_id=user_id), "uploads"),
            (paths.sandbox_work_dir(thread_id, user_id=user_id), "workspace"),
            (paths.sandbox_outputs_dir(thread_id, user_id=user_id), "outputs"),
        ]
        entries = _collect(sources)
        admitted = self._admit(entries)
        if len(admitted) < len(entries):
            logger.warning(
                "Thread files for OpenSandbox %s exceed %d files or %d bytes; %d of %d files were not restored",
                sandbox.id,
                self._MAX_UPLOAD_FILES,
                self._MAX_UPLOAD_BYTES,
                len(entries) - len(admitted),
                len(entries),
            )
        if admitted:
            self._upload_entries(sandbox, admitted, dest_root=USER_DATA_ROOT, replace=())

    # ── Transfer ─────────────────────────────────────────────────────────

    def _admit(self, entries: list[_Entry]) -> list[_Entry]:
        admitted: list[_Entry] = []
        total = 0
        for entry in entries:
            if len(admitted) + 1 > self._MAX_UPLOAD_FILES or total + entry.size > self._MAX_UPLOAD_BYTES:
                break
            admitted.append(entry)
            total += entry.size
        return admitted

    def _within_budget(self, entries: list[_Entry]) -> bool:
        return len(self._admit(entries)) == len(entries)

    def _upload_entries(self, sandbox: OpenSandboxSandbox, entries: list[_Entry], *, dest_root: str, replace: tuple[str, ...]) -> None:
        """Ship ``entries`` as one tar.gz and extract it below ``dest_root``.

        ``replace`` lists sub-paths of ``dest_root`` removed before extraction.
        """
        remote_tar = f"/tmp/deerflow-upload-{uuid.uuid4().hex}.tar.gz"
        with tempfile.TemporaryFile() as spool:
            with tarfile.open(fileobj=spool, mode="w:gz") as archive:
                for entry in entries:
                    info = tarfile.TarInfo(entry.arcname)
                    info.size = entry.size
                    info.mode = entry.mode
                    info.mtime = entry.mtime_ns // 1_000_000_000
                    with entry.path.open("rb") as fh:
                        archive.addfile(info, fh)
            spool.seek(0)
            # A seekable binary stream lets the SDK send Content-Length, which
            # the OpenSandbox server proxy requires for multipart uploads.
            sandbox._file_op(lambda files: files.write_file(remote_tar, spool, mode=600))
        root = shlex.quote(dest_root)
        tar = shlex.quote(remote_tar)
        removals = " ".join(shlex.quote(f"{dest_root}/{name}") for name in replace)
        script = (
            f"set -e; trap 'rm -f {tar}' EXIT; "
            f"if [ -L {root} ]; then echo 'refusing symlinked destination' >&2; exit 2; fi; "
            + (f"rm -rf -- {removals}; " if removals else "")
            + f"mkdir -p -- {root}; tar -xzf {tar} -C {root} --no-same-owner; echo UPLOAD_OK"
        )
        execution = sandbox._run(script, timeout=_EXTRACT_TIMEOUT)
        exit_code = getattr(execution, "exit_code", None)
        if exit_code != 0 or "UPLOAD_OK" not in execution_stdout(execution):
            raise RuntimeError(f"OpenSandbox upload into {dest_root} failed with exit code {exit_code}")


__all__ = ["SkillAwareOpenSandboxProvider"]
