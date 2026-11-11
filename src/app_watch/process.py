"""Same-user process inspection and strong identity matching.

Adapted from VaultKeep; see LICENSE for the original copyright notice.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import psutil

from app_watch.apps.registry import get_adapter


@dataclass(frozen=True, slots=True)
class Entrypoint:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    """Strong process identity plus optional supporting predicates."""

    exe_realpath: str | None = None
    entrypoints: tuple[Entrypoint, ...] = ()
    argv_tokens: tuple[str, ...] = ()
    cmdline_regex: str | None = None
    env_eq: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exe_realpath": self.exe_realpath,
            "entrypoints": [
                {"kind": entrypoint.kind, "value": entrypoint.value}
                for entrypoint in self.entrypoints
            ],
            "argv_tokens": list(self.argv_tokens),
            "cmdline_regex": self.cmdline_regex,
            "env_eq": dict(sorted(self.env_eq.items())),
        }
        return payload


def matcher_from_canonical(data: dict[str, Any]) -> MatcherSpec:
    """Validate and rebuild a matcher from a frozen snapshot."""
    from app_watch.config.models import MatcherConfig

    raw = MatcherConfig.model_validate(data)
    return MatcherSpec(
        exe_realpath=raw.exe_realpath,
        entrypoints=tuple(Entrypoint(item.kind, item.value) for item in raw.entrypoints),
        argv_tokens=tuple(raw.argv_tokens),
        cmdline_regex=raw.cmdline_regex,
        env_eq=dict(raw.env_eq),
    )


class Observation(str, Enum):  # noqa: UP042 - preserve persisted observation semantics
    """One sanitized poll result."""

    RUNNING = "running"
    STOPPED = "stopped"
    AMBIGUOUS = "ambiguous"
    INSPECTION_UNAVAILABLE = "inspection_unavailable"


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A sanitized snapshot of one process."""

    pid: int
    create_time: float
    exe_realpath: str | None
    cmdline: tuple[str, ...]
    env: dict[str, str] | None = None
    cmdline_available: bool = True


@dataclass(frozen=True, slots=True)
class MatchResult:
    matched: bool
    reason: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    unavailable: bool = False


def _canonical(value: str) -> str:
    return os.path.realpath(value)


def _match_entrypoint(spec_value: str, kind: str, cmdline: tuple[str, ...]) -> bool:
    if kind == "script":
        wanted = _canonical(spec_value)
        return any(_canonical(token) == wanted for token in cmdline[:2])
    if kind == "jar":
        wanted = _canonical(spec_value)
        for index, token in enumerate(cmdline):
            if token == "-jar" and index + 1 < len(cmdline):
                if _canonical(cmdline[index + 1]) == wanted:
                    return True
            elif _canonical(token) == wanted:
                return True
        return False
    # java_main: exact entry token.
    return spec_value in cmdline


def match_identity(matcher: MatcherSpec, proc: ProcessInfo) -> MatchResult:
    """Evaluate a matcher's strong identity and supporting predicates."""
    unavailable = False
    if matcher.exe_realpath is not None:
        if proc.exe_realpath is None:
            unavailable = True
        elif _canonical(proc.exe_realpath) != _canonical(matcher.exe_realpath):
            return MatchResult(False, "executable path does not match")

    if matcher.entrypoints:
        entry_matched = False
        entry_unavailable = False
        for entry in matcher.entrypoints:
            if entry.kind == "executable_name":
                if not proc.exe_realpath or not os.path.isabs(proc.exe_realpath):
                    entry_unavailable = True
                elif os.path.basename(proc.exe_realpath) == entry.value:
                    entry_matched = True
                elif proc.exe_realpath.endswith(" (deleted)") and (
                    os.path.basename(proc.exe_realpath.removesuffix(" (deleted)")) == entry.value
                ):
                    entry_unavailable = True
            elif not proc.cmdline_available:
                entry_unavailable = True
            elif _match_entrypoint(entry.value, entry.kind, proc.cmdline):
                entry_matched = True
        if not entry_matched and not entry_unavailable:
            return MatchResult(False, "no configured entrypoint matches")
        if not entry_matched:
            unavailable = True

    if matcher.exe_realpath is None and not matcher.entrypoints:
        return MatchResult(False, "matcher declares no strong identity")

    if not proc.cmdline_available and (matcher.argv_tokens or matcher.cmdline_regex):
        unavailable = True
    missing = (
        [token for token in matcher.argv_tokens if token not in proc.cmdline]
        if proc.cmdline_available
        else []
    )
    if missing:
        return MatchResult(False, "argv tokens not present")

    if matcher.cmdline_regex is not None and proc.cmdline_available:
        pattern = re.compile(matcher.cmdline_regex)
        if not any(pattern.fullmatch(token) for token in proc.cmdline):
            return MatchResult(False, "cmdline regex did not match any token")

    if unavailable:
        return MatchResult(False, "process identity metadata is not readable", unavailable=True)

    if matcher.env_eq:
        if proc.env is None:
            return MatchResult(False, "environment not read", unavailable=True)
        for key, value in matcher.env_eq.items():
            if proc.env.get(key) != value:
                return MatchResult(False, f"environment key {key} does not match")

    return MatchResult(
        True,
        evidence={"pid": proc.pid, "create_time": proc.create_time},
    )


def required_env_keys(matcher: MatcherSpec) -> tuple[str, ...]:
    """Return environment keys the matcher needs to read for a process."""
    return tuple(sorted(matcher.env_eq))


_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def boot_id() -> str | None:
    """Return the kernel boot ID, or ``None`` when it cannot be read."""
    try:
        return _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return None


class ProcessProvider(Protocol):
    """Interface for process inspection (replaceable in tests)."""

    def snapshot(self) -> list[ProcessInfo]:
        """Return a frozen snapshot of the current processes."""

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        """Return the requested environment keys, or ``None`` when unreadable."""

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Resolve a process-reported directory using verified owner/home/cwd metadata."""

    def alive(self, pid: int, create_time: float) -> bool | None:
        """Return whether ``pid`` is still the same process (pid + create time)."""

    def read_text_file(self, pid: int, path: str) -> tuple[str | None, str]:
        """Read bounded regular UTF-8 settings: resolved/missing/inaccessible/unresolved."""


class PsutilProcessProvider:
    """Real process provider using psutil."""

    def __init__(self, *, owner_uid: int | None = None) -> None:
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._last: dict[int, float] = {}

    def snapshot(self) -> list[ProcessInfo]:
        infos: list[ProcessInfo] = []
        seen: dict[int, float] = {}
        for proc in psutil.process_iter(["pid", "uids"]):
            info = proc.info
            pid = proc.pid
            uids = info.get("uids")
            if uids is None:
                raise psutil.AccessDenied(pid)
            if uids.real != self.owner_uid:
                continue
            try:
                create_time = proc.create_time()
                cmdline = tuple(proc.cmdline())
                cmdline_available = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                create_time = proc.create_time()
                cmdline = ()
                cmdline_available = False
            try:
                # Avoid psutil's cached executable path surviving unlink/replacement.
                # Preserve the kernel's deletion marker for conservative matching.
                exe = os.readlink(f"/proc/{pid}/exe")
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                exe = None
            if not self.alive(pid, float(create_time)):
                raise psutil.AccessDenied(pid)
            infos.append(
                ProcessInfo(
                    pid=pid,
                    create_time=float(create_time),
                    exe_realpath=exe,
                    cmdline=cmdline,
                    cmdline_available=cmdline_available,
                )
            )
            seen[pid] = float(create_time)
        self._last = seen
        return infos

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        wanted = set(keys)
        if not wanted:
            return {}
        try:
            expected = self._last.get(pid)
            if expected is None or not self.alive(pid, expected):
                return None
            environ = psutil.Process(pid).environ()
            if not self.alive(pid, expected):
                return None
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return None
        return {key: environ[key] for key in wanted if key in environ}

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Never resolve process paths against the monitor's home or cwd."""
        expected = self._last.get(pid)
        try:
            if expected is None or not self.alive(pid, expected):
                return None
            if value == "~" or value.startswith("~/"):
                home = pwd.getpwuid(self.owner_uid).pw_dir
                if not os.path.isabs(home):
                    return None
                expanded = home + value[1:]
            elif value.startswith("~"):
                return None
            elif os.path.isabs(value):
                expanded = value
            else:
                cwd = psutil.Process(pid).cwd()
                if not os.path.isabs(cwd):
                    return None
                expanded = os.path.join(cwd, value)
            resolved = os.path.realpath(expanded)
            return resolved if self.alive(pid, expected) else None
        except (KeyError, psutil.Error, OSError):
            return None

    def alive(self, pid: int, create_time: float) -> bool | None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            return bool(
                abs(proc.create_time() - create_time) < 0.01 and proc.uids().real == self.owner_uid
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return False
        except psutil.AccessDenied:
            return None

    def read_text_file(self, pid: int, path: str) -> tuple[str | None, str]:
        expected = self._last.get(pid)
        if expected is None or not os.path.isabs(path) or not self.alive(pid, expected):
            return None, "inaccessible"
        limit = 1024 * 1024
        opened = False
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            opened = True
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    return None, "unresolved"
                data = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                current = os.stat(path)
                if (
                    len(data) > limit
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                ):
                    return None, "unresolved"
                text = data.decode("utf-8-sig")
                if "\0" in text:
                    return None, "unresolved"
        except FileNotFoundError:
            return None, "missing" if not opened and self.alive(pid, expected) else "inaccessible"
        except UnicodeError:
            return None, "unresolved"
        except OSError:
            return None, "inaccessible"
        if not self.alive(pid, expected):
            return None, "inaccessible"
        return text, "resolved"


@dataclass(frozen=True, slots=True)
class DetectionResult:
    observation: Observation
    boot_id: str | None = None
    pid: int | None = None
    create_time: float | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    reason: str | None = None


def detect_spec(
    *,
    app: str,
    data_dir: str,
    matcher: MatcherSpec,
    provider: ProcessProvider,
) -> DetectionResult:
    """Return an observation from a frozen app/data-dir/matcher snapshot."""
    adapter = get_adapter(app)
    current_boot = boot_id()
    if not os.path.isabs(data_dir):
        return DetectionResult(
            Observation.AMBIGUOUS,
            boot_id=current_boot,
            reason="configured data directory is not resolved",
        )
    wanted_dir = _canonical(data_dir)
    try:
        snapshot = provider.snapshot()
    except (psutil.Error, OSError):
        return DetectionResult(
            Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason="process snapshot is not readable",
        )

    identity_matches: list[ProcessInfo] = []
    ambiguous_reason: str | None = None
    unavailable_reason: str | None = None

    env_keys = required_env_keys(matcher)
    for proc in snapshot:
        # Reject known unrelated identities before reading any environment.
        identity = match_identity(replace(matcher, env_eq={}), proc)
        if identity.unavailable:
            unavailable_reason = identity.reason
            continue
        if not identity.matched:
            continue
        env: dict[str, str] | None = None
        if env_keys:
            env = provider.read_env(proc.pid, env_keys)
            if env is None:
                unavailable_reason = "process environment is not readable"
                continue
        candidate = replace(proc, env=env)
        result: MatchResult = match_identity(matcher, candidate)
        if result.matched:
            identity_matches.append(candidate)

    resolved_matches: list[ProcessInfo] = []
    for candidate in identity_matches:
        if not provider.alive(candidate.pid, candidate.create_time):
            unavailable_reason = "process identity changed before metadata inspection"
            continue
        directory, status = adapter.resolve_directory(candidate, provider)
        if not provider.alive(candidate.pid, candidate.create_time):
            unavailable_reason = "process identity changed during metadata inspection"
            continue
        if status == "inaccessible":
            unavailable_reason = "data directory metadata is not readable"
            continue
        if status == "unresolved":
            ambiguous_reason = "could not resolve the instance data directory"
            continue
        if directory == wanted_dir:
            resolved_matches.append(candidate)
        elif adapter.blocks_shutdown_on_identity:
            ambiguous_reason = "another Monero wallet/process remains active"

    # Re-validate identity immediately before reporting running.
    for candidate in resolved_matches:
        alive = provider.alive(candidate.pid, candidate.create_time)
        if not alive:
            unavailable_reason = "matching process disappeared, changed, or cannot be revalidated"
        if alive:
            return DetectionResult(
                observation=Observation.RUNNING,
                boot_id=current_boot,
                pid=candidate.pid,
                create_time=candidate.create_time,
                evidence={
                    "matched_pid": candidate.pid,
                    "create_time": candidate.create_time,
                    "data_dir": wanted_dir,
                },
            )

    if unavailable_reason is not None and adapter.blocks_shutdown_on_identity:
        return DetectionResult(
            Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason=unavailable_reason,
        )
    if ambiguous_reason is not None:
        return DetectionResult(
            observation=Observation.AMBIGUOUS,
            boot_id=current_boot,
            reason=ambiguous_reason,
        )
    if unavailable_reason is not None:
        return DetectionResult(
            observation=Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason=unavailable_reason,
        )
    return DetectionResult(
        observation=Observation.STOPPED,
        boot_id=current_boot,
        evidence={"data_dir": wanted_dir},
    )
