"""Root installation transactions for App-watch (standard library only).

User YAML, recovery inspection and per-user unit changes run as their owner.
Releases are built at their final, unpublished path because venvs are not relocatable.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SOURCE_PARTS = (
    "src",
    "scripts",
    "systemd",
    "examples",
    "docs",
    "install.sh",
    "pyproject.toml",
    "requirements.in",
    "requirements.lock",
    "LICENSE",
    "README.md",
)


class InstallError(RuntimeError):
    pass


def acquire_install_lock(fd: int, *, timeout: float = 15.0) -> None:
    """Allow short runtime reads to finish without waiting indefinitely."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise InstallError(
                    "Software maintenance is busy; retry after it finishes"
                ) from error
            time.sleep(0.05)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, document: dict, mode: int = 0o600) -> None:
    write_file(path, (json.dumps(document, sort_keys=True, indent=2) + "\n").encode(), mode)


def write_link(path: Path, target: str) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def record(path: Path) -> dict:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "link", "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        return {"kind": "file", "sha256": digest(path), "mode": stat.S_IMODE(info.st_mode)}
    raise InstallError(f"Unexpected managed resource type: {path}")


def cached_bytecode(path: Path) -> bool:
    return "__pycache__" in path.parts and path.suffix == ".pyc"


def tree_records(root: Path) -> dict[str, dict]:
    records = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not cached_bytecode(path):
            records[str(path.relative_to(root))] = record(path)
    return records


def safe_directory(path: Path, uid: int) -> None:
    """Reject symlinked roots and untrusted writable ancestors before root writes."""
    if path.resolve() != path:
        raise InstallError(f"Installer path has a symlink or is not canonical: {path}")
    current = path
    while not current.exists():
        current = current.parent
    root_uid = Path("/").lstat().st_uid  # Also supports rootless/user-namespace test filesystems.
    for ancestor in (current, *current.parents):
        info = ancestor.lstat()
        sticky = info.st_uid in (0, root_uid) and bool(info.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in (0, root_uid, uid)
            or (info.st_mode & 0o022 and not sticky)
        ):
            raise InstallError(f"Installer directory is not safely owned: {ancestor}")


@dataclass(frozen=True)
class Layout:
    prefix: Path
    binary: Path
    user_systemd: Path
    sudoers_root: Path = Path("/etc/sudoers.d")

    @classmethod
    def environment(cls) -> Layout:
        return cls(
            Path(os.environ.get("APP_WATCH_INSTALL_PREFIX", "/opt/app-watch")),
            Path(os.environ.get("APP_WATCH_BIN_LINK", "/usr/local/bin/app-watch")),
            Path(os.environ.get("APP_WATCH_USER_SYSTEMD_ROOT", "/etc/systemd/user")),
        )

    def validate(self, uid: int) -> None:
        for path in (self.prefix, self.binary, self.user_systemd, self.sudoers_root):
            value = str(path)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise InstallError(
                    "Installer paths must be absolute and normalized without controls"
                )
        if any(char in str(self.binary) for char in "'\"\\$%"):
            raise InstallError("Stable command path must not contain quotes, backslashes, $ or %")
        if any(char in str(self.prefix) for char in "*?[]\\"):
            raise InstallError(
                "Installation prefix must not contain sudoers wildcards or backslashes"
            )
        roots = (self.prefix, self.binary.parent, self.user_systemd)
        if any(path == Path("/") for path in roots):
            raise InstallError("Installer roots must not be filesystem root")
        for index, left in enumerate(roots):
            for right in roots[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise InstallError("Installer roots must not overlap")
        for root in roots:
            safe_directory(root, uid)
        safe_directory(self.sudoers_root, uid)

    @property
    def manifest(self) -> Path:
        return self.prefix / "install-manifest.json"

    @property
    def journal(self) -> Path:
        return self.prefix / "transaction.json"

    @property
    def current(self) -> Path:
        return self.prefix / "current"

    @property
    def worker(self) -> Path:
        return self.user_systemd / "app-watch-exec@.service"

    @property
    def inspector(self) -> Path:
        return self.prefix / "process-inspect"

    @property
    def sudoers(self) -> Path:
        suffix = hashlib.sha256(os.fsencode(self.prefix)).hexdigest()[:16]
        return self.sudoers_root / f"app-watch-{suffix}"

    @property
    def resources(self) -> tuple[Path, ...]:
        return self.worker, self.binary, self.current, self.inspector, self.sudoers

    def document(self) -> dict:
        return {
            "APP_WATCH_INSTALL_PREFIX": str(self.prefix),
            "APP_WATCH_BIN_LINK": str(self.binary),
            "APP_WATCH_USER_SYSTEMD_ROOT": str(self.user_systemd),
        }


class Host:
    owner_uid = 0
    log: Path | None = None

    def run(
        self, argv: list[str], *, input_text: str | None = None, env: dict | None = None
    ) -> str:
        environment = dict(os.environ if env is None else env)
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "PIP_TARGET",
            "PIP_PREFIX",
            "PIP_ROOT",
            "PIP_USER",
        ):
            environment.pop(name, None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PIP_CONFIG_FILE"] = os.devnull
        with ExitStack() as resources:
            transcript = None
            if self.log is not None:
                fd = os.open(
                    self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
                transcript = resources.enter_context(os.fdopen(fd, "a", encoding="utf-8"))
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != self.owner_uid
                    or info.st_mode & 0o077
                ):
                    raise InstallError("Installer transcript is not private and safely owned")
            result = subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
                env=environment,
            )
            if transcript is not None:
                transcript.write(json.dumps(argv) + "\n" + result.stdout + result.stderr + "\n")
        if result.returncode:
            # Tool/provider output can contain user values. Do not publish it here.
            raise InstallError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
        return result.stdout

    def check_platform(self) -> None:
        values = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            key, _, value = line.partition("=")
            values[key] = value.strip('"')
        major = values.get("VERSION_ID", "0").split(".")[0]
        if values.get("ID") != "debian" or not major.isdigit() or int(major) < 13:
            raise InstallError("Production installation requires Debian 13 or newer")

    def provision(self) -> None:
        self.check_platform()
        self.run(["apt-get", "update"])
        self.run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-upgrade",
                "python3",
                "python3-venv",
                "systemd",
                "dbus-user-session",
                "libnotify-bin",
                "sudo",
            ]
        )

    def build(self, release: Path) -> None:
        self.run([sys.executable, "-m", "venv", str(release / "venv")])
        python = str(release / "venv/bin/python")
        self.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(release / "src/requirements.lock"),
            ]
        )
        self.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                str(release / "src"),
            ]
        )
        self.run([python, "-m", "pip", "check"])

    def ensure_inspection_support(self) -> None:
        if not Path("/usr/bin/sudo").is_file() or not Path("/usr/sbin/visudo").is_file():
            self.run(["apt-get", "update"])
            self.run(["apt-get", "install", "-y", "--no-upgrade", "sudo"])

    def verify_unit(self, content: str) -> None:
        with tempfile.TemporaryDirectory(prefix="app-watch-unit-check-") as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            unit = root / "app-watch-exec@.service"
            content = (
                "\n".join(
                    "ExecStart=/bin/true" if line.startswith("ExecStart=") else line
                    for line in content.splitlines()
                )
                + "\n"
            )
            unit.write_text(content)
            self.run(
                ["systemd-analyze", "--user", "verify", str(unit)],
                env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)),
            )

    def verify_sudoers(self, content: str) -> None:
        with tempfile.TemporaryDirectory(prefix="app-watch-sudoers-check-") as directory:
            policy = Path(directory) / "policy"
            policy.write_text(content)
            self.run(["/usr/sbin/visudo", "-cf", str(policy)])

    def accounts(self) -> list[dict]:
        specs = []
        for entry in pwd.getpwall():
            if entry.pw_uid == 0 or not Path(entry.pw_dir).is_absolute():
                continue
            home = Path(entry.pw_dir)
            runtime = Path(f"/run/user/{entry.pw_uid}")
            relevant = (home / ".local/state/app-watch").exists() or any(
                (home / ".config/systemd/user").glob("app-watch-monitor@*.service")
            )
            if not runtime.exists() and not relevant:
                continue
            environment = {
                "HOME": str(home),
                "USER": entry.pw_name,
                "LOGNAME": entry.pw_name,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "XDG_RUNTIME_DIR": str(runtime),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            }
            exported = self.run(
                self.user_command(
                    entry.pw_name, environment, ["systemctl", "--user", "show-environment"]
                )
            )
            for line in exported.splitlines():
                key, _, value = line.partition("=")
                if key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                    value = self.environment_value(value)
                    # Never evaluate shell output. Nonliteral values require local review.
                    if not value:
                        continue
                    if not value.startswith("/") or any(ord(char) < 32 for char in value):
                        raise InstallError(f"Review nonliteral {key} for account {entry.pw_name}")
                    environment[key] = value
            specs.append({"name": entry.pw_name, "uid": entry.pw_uid, "env": environment})
        return specs

    @staticmethod
    def environment_value(value: str) -> str:
        """Decode systemctl's literal ANSI-C form without evaluating a shell."""
        if value.startswith("$'") and value.endswith("'"):
            raw = value[2:-1]
            if not re.fullmatch(r"(?:[^'\\]|\\(?:[abfnrtv\\'\"]|[0-7]{3}))*", raw):
                raise InstallError("Unrecognized manager environment escaping")
            return os.fsdecode(codecs.escape_decode(os.fsencode(raw))[0])
        return "" if value == '""' else value

    @staticmethod
    def user_command(name: str, environment: dict, argv: list[str]) -> list[str]:
        return [
            "runuser",
            "-u",
            name,
            "--",
            "env",
            "-i",
            *(f"{key}={value}" for key, value in sorted(environment.items())),
            *argv,
        ]

    def user(self, release: Path, account: dict, request: dict) -> dict:
        request = dict(request, protocol_version=1)
        argv = self.user_command(
            account["name"],
            account["env"],
            [str(release / "venv/bin/python"), "-I", "-m", "app_watch.maintenance"],
        )
        try:
            document = json.loads(self.run(argv, input_text=json.dumps(request)))
            return document["plan"]
        except (ValueError, KeyError, InstallError) as error:
            raise InstallError(
                f"Account {account['name']}: maintenance {request['action']} failed; "
                "inspect its watches as their owner"
            ) from error


class Installer:
    def __init__(self, source: Path, layout: Layout, host: Host):
        self.source, self.layout, self.host = source.resolve(), layout, host

    def read_json(self, path: Path) -> dict | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.host.owner_uid
            or info.st_mode & 0o077
        ):
            raise InstallError(f"Unsafe installation record: {path}")
        try:
            document = json.loads(path.read_bytes())
            if (
                not isinstance(document, dict)
                or type(document.get("schema_version")) is not int
                or document["schema_version"] != 1
            ):
                raise ValueError("Unknown installation schema")
            return document
        except ValueError as error:
            raise InstallError(f"Corrupt installation record: {path}") from error

    def validate_manifest(self, manifest: dict | None) -> None:
        overrides = self.layout.worker.with_name(self.layout.worker.name + ".d")
        if overrides.exists() or overrides.is_symlink():
            raise InstallError("Shared worker has unmanaged overrides; review them first")
        if manifest is None:
            for path in self.layout.resources:
                if path.exists() or path.is_symlink():
                    raise InstallError(f"Existing unmanaged installation resource: {path}")
            return
        if (
            set(manifest) != {"schema_version", "layout", "active_release", "releases", "files"}
            or not isinstance(manifest["releases"], dict)
            or not isinstance(manifest["files"], dict)
        ):
            raise InstallError("Malformed installation manifest")
        if manifest.get("layout") != self.layout.document():
            raise InstallError("Installed layout differs; use the original installation paths")
        legacy_paths = {
            str(self.layout.current),
            str(self.layout.binary),
            str(self.layout.worker),
        }
        expected_paths = {str(path) for path in self.layout.resources}
        active = manifest.get("active_release")
        if active is not None and not isinstance(active, str):
            raise InstallError("Malformed active release identity")
        allowed_paths = (expected_paths, legacy_paths) if active is not None else (set(),)
        if set(manifest["files"]) not in allowed_paths:
            raise InstallError("Manifest contains unexpected managed paths")
        # Upgrades from the pre-helper layout must not overwrite unowned resources.
        for path in self.layout.resources:
            if str(path) not in manifest["files"] and (path.exists() or path.is_symlink()):
                raise InstallError(f"Existing unmanaged installation resource: {path}")
        for name, expected in manifest["files"].items():
            path = Path(name)
            if path.lstat().st_uid != self.host.owner_uid or record(path) != expected:
                raise InstallError(f"Managed installation resource changed: {path}")
        for name, expected in manifest["releases"].items():
            self.validate_release(name, expected)
        if active is not None and active not in manifest["releases"]:
            raise InstallError("Active release has no ownership record")
        if active is not None and (
            manifest["files"][str(self.layout.current)]
            != {
                "kind": "link",
                "target": str(self.layout.prefix / "releases" / active),
            }
            or manifest["files"][str(self.layout.binary)]
            != {
                "kind": "link",
                "target": str(self.layout.current / "venv/bin/app-watch"),
            }
        ):
            raise InstallError("Installed command/release selection is inconsistent")

    def validate_release(self, name: str, expected: dict) -> None:
        if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
            raise InstallError("Unsafe release identity")
        path = self.layout.prefix / "releases" / name
        if path.is_symlink() or not path.is_dir() or tree_records(path) != expected:
            raise InstallError(f"Managed release changed: {name}")
        for resource in (path, *path.rglob("*")):
            info = resource.lstat()
            if info.st_uid != self.host.owner_uid or (
                not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
            ):
                raise InstallError(f"Managed release ownership/permissions changed: {name}")

    def source_files(self) -> list[Path]:
        files = []
        for name in SOURCE_PARTS:
            root = self.source / name
            if not root.exists():
                raise InstallError(f"Source is incomplete: {name}")
            for path in [root] if root.is_file() else sorted(root.rglob("*")):
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.is_symlink():
                    raise InstallError("Trusted source must not contain symlinked install inputs")
                if path.is_file():
                    files.append(path)
        return sorted(files)

    def build_release(self) -> Path:
        files = self.source_files()
        hasher = hashlib.sha256()
        for path in files:
            hasher.update(str(path.relative_to(self.source)).encode() + b"\0")
            hasher.update(bytes.fromhex(digest(path)))
        version = tomllib.loads((self.source / "pyproject.toml").read_text())["project"]["version"]
        if not isinstance(version, str) or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.+-"
            for char in version
        ):
            raise InstallError("Unsafe package version")
        release = (
            self.layout.prefix
            / "releases"
            / f"{version}-{hasher.hexdigest()[:12]}-{uuid4().hex[:8]}"
        )
        release.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        safe_directory(release.parent, self.host.owner_uid)
        release.mkdir(parents=True, mode=0o755)
        try:
            for path in files:
                target = release / "src" / path.relative_to(self.source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                target.chmod(0o755 if path.name == "install.sh" else 0o644)
            self.host.build(release)
            write_json(
                release / "deployment.json",
                {
                    "schema_version": 1,
                    "version": version,
                    "source_digest": hasher.hexdigest(),
                    "layout": self.layout.document(),
                },
                0o644,
            )
            result = self.host.run([str(release / "venv/bin/app-watch"), "--version"])
            if result.strip() != f"app-watch {version}":
                raise InstallError("Candidate package version verification failed")
            # Verify all initial examples without resolving user paths or executing commands.
            for path in (release / "src/examples").glob("*.yaml"):
                self.host.run(
                    [
                        str(release / "venv/bin/app-watch"),
                        "--config",
                        str(path),
                        "validate",
                        "--schema-only",
                    ]
                )
            self.host.verify_unit(self.worker_content(release))
            self.host.verify_sudoers(self.sudoers_content())
            for resource in (release, *release.rglob("*")):
                if not resource.is_symlink():
                    resource.chmod(stat.S_IMODE(resource.stat().st_mode) & ~0o022)
            sync_dir(release)
            sync_dir(release.parent)
            return release
        except BaseException:
            shutil.rmtree(release)
            raise

    def snapshots(
        self, release: Path, accounts: list[dict], *, validate_config: bool
    ) -> list[dict]:
        return [
            {
                "account": account,
                "plan": self.host.user(
                    release,
                    account,
                    {
                        "action": "snapshot",
                        "command": str(self.layout.binary),
                        "validate_config": validate_config,
                    },
                ),
            }
            for account in accounts
        ]

    def users(self, release: Path, plans: list[dict], action: str, **options) -> None:
        for item in plans:
            item["plan"] = self.host.user(
                release,
                item["account"],
                {
                    "action": action,
                    "command": str(self.layout.binary),
                    "plan": item["plan"],
                    "worker_path": str(self.layout.worker),
                    **options,
                },
            )

    def acquire_user_locks(self, stack: ExitStack, plans: list[dict], field: str) -> None:
        for item in plans:
            for unit in item["plan"]["units"]:
                path = Path(unit[field])
                runtime = Path(item["account"]["env"]["XDG_RUNTIME_DIR"])
                if not path.is_relative_to(runtime / "app-watch/jobs") or path.name not in (
                    "service.lock",
                    "monitor.lock",
                ):
                    raise InstallError("Unsafe account maintenance lock path")
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != item["account"]["uid"]
                    or info.st_mode & 0o077
                ):
                    raise InstallError("Account maintenance lock is not safely owned")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise InstallError("A user watch operation is already active") from error

    def state_manifest(self, old: dict | None, release: Path | None) -> dict:
        releases = dict(old["releases"]) if old else {}
        if release is not None:
            releases[release.name] = tree_records(release)
        paths = self.layout.resources if release else ()
        return {
            "schema_version": 1,
            "layout": self.layout.document(),
            "active_release": release.name if release else None,
            "releases": releases,
            "files": {str(path): record(path) for path in paths},
        }

    def activate(self, release: Path | None) -> None:
        if release is None:
            # Withdraw root authorization before removing executable resources.
            for path in (self.layout.sudoers, *self.layout.resources[:-1]):
                path.unlink(missing_ok=True)
                sync_dir(path.parent)
            return
        # Render the fixed shared worker template for this installation's stable command.
        worker = self.worker_content(release)
        write_file(
            self.layout.inspector, (release / "src/scripts/process_inspect.py").read_bytes(), 0o755
        )
        write_file(self.layout.sudoers, self.sudoers_content().encode(), 0o440)
        write_file(self.layout.worker, worker.encode(), 0o644)
        write_link(self.layout.current, str(release))
        write_link(self.layout.binary, str(self.layout.current / "venv/bin/app-watch"))

    def sudoers_content(self) -> str:
        # sudoers command paths escape whitespace and grammar metacharacters.
        path = re.sub(r"([\\\s,:=#!])", r"\\\1", str(self.layout.inspector))
        return (
            "# App-watch read-only process identity; no command arguments permitted.\n"
            f'ALL ALL=(root) NOPASSWD: NOSETENV: {path} ""\n'
        )

    def worker_content(self, release: Path) -> str:
        escaped = str(self.layout.binary).replace('"', '\\"')
        return (
            (release / "src/systemd/app-watch-exec@.service")
            .read_text()
            .replace(
                "/usr/local/bin/app-watch",
                f'"{escaped}"',
            )
        )

    def restore_global(self, journal: dict) -> None:
        before = journal["before"]
        for path in self.layout.resources:
            item = before.get(str(path))
            if item is None:
                path.unlink(missing_ok=True)
            elif item["kind"] == "link":
                write_link(path, item["target"])
            else:
                write_file(path, base64.b64decode(item["data"]), item["mode"])
        if journal["old_manifest"] is None:
            self.layout.manifest.unlink(missing_ok=True)
        else:
            write_json(self.layout.manifest, journal["old_manifest"])

    def transaction(
        self,
        action: str,
        manifest: dict | None,
        release: Path | None,
        plans: list[dict],
        *,
        purge: bool = False,
    ) -> None:
        if self.read_json(self.layout.manifest) != manifest:
            raise InstallError("Installation changed while staging the candidate")
        self.validate_manifest(manifest)
        helper = release or self.layout.prefix / "releases" / manifest["active_release"]
        before = {}
        for path in self.layout.resources:
            if path.exists() or path.is_symlink():
                item = record(path)
                if item["kind"] == "file":
                    item["data"] = base64.b64encode(path.read_bytes()).decode()
                before[str(path)] = item
        self.users(helper, plans, "locks")
        with ExitStack() as service_locks:
            self.acquire_user_locks(service_locks, plans, "service_lock")
            self.users(
                helper,
                plans,
                "validate",
                validate_config=action != "uninstall",
                capture_activity=True,
            )
            journal = {
                "schema_version": 1,
                "action": action,
                "old_manifest": manifest,
                "candidate": str(release) if release else None,
                "before": before,
                "candidate_records": tree_records(release) if release else None,
                "plans": plans,
                "phase": "prepared",
                "purge": purge,
            }
            for item in plans:
                for unit in item["plan"]["units"]:
                    unit["remove_intended"] = action == "uninstall"
            write_json(self.layout.journal, journal)
            try:
                self.users(helper, plans, "pause")
                journal["phase"] = "paused"
                write_json(self.layout.journal, journal)
                with ExitStack() as lifetime_locks:
                    self.acquire_user_locks(lifetime_locks, plans, "monitor_lock")
                    self.users(helper, plans, "validate", validate_config=action != "uninstall")
                    if self.read_json(self.layout.manifest) != manifest:
                        raise InstallError("Installation changed before activation")
                    self.validate_manifest(manifest)
                    self.activate(release)
                    journal["phase"] = "activated"
                    write_json(self.layout.journal, journal)
                    self.users(
                        helper,
                        plans,
                        "refresh",
                        remove=action == "uninstall",
                        verify_worker=action != "uninstall",
                    )
                if action != "uninstall":
                    self.users(helper, plans, "resume")
                write_json(self.layout.manifest, self.state_manifest(manifest, release))
                journal["phase"] = "committed"
                write_json(self.layout.journal, journal)
            except BaseException:
                self.validate_transition(journal)
                # The journal survives a failed safety check; do not restore across new work.
                self.users(helper, plans, "pause")
                with ExitStack() as rollback_locks:
                    self.acquire_user_locks(rollback_locks, plans, "monitor_lock")
                    self.users(helper, plans, "validate", validate_config=False, recovery=True)
                    if manifest and manifest["active_release"]:
                        old_helper = self.layout.prefix / "releases" / manifest["active_release"]
                        self.users(
                            old_helper, plans, "validate", validate_config=False, recovery=True
                        )
                    self.restore_global(journal)
                    self.users(
                        helper,
                        plans,
                        "refresh",
                        restore=True,
                        verify_worker=bool(manifest and manifest["active_release"]),
                    )
                self.users(helper, plans, "resume")
                self.layout.journal.unlink()
                sync_dir(self.layout.prefix)
                raise
            if action == "uninstall":
                self.cleanup_uninstalled(journal)
            else:
                self.layout.journal.unlink()
                sync_dir(self.layout.prefix)

    def cleanup_uninstalled(self, journal: dict) -> None:
        """Finish committed software deletion; missing files are allowed after a crash."""
        for path in self.layout.resources:
            if path.exists() or path.is_symlink():
                raise InstallError("Uninstalled resources reappeared; refusing cleanup")
        previous = journal["old_manifest"]
        receipt = self.read_json(self.layout.manifest)
        if receipt is not None and (
            receipt.get("layout") != self.layout.document()
            or receipt.get("active_release") is not None
            or receipt.get("files") != {}
            or receipt.get("releases") not in (previous["releases"], {})
        ):
            raise InstallError("Uninstallation receipt changed during cleanup")
        for name, expected in previous["releases"].items():
            if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                raise InstallError("Unsafe retained release name")
            path = self.layout.prefix / "releases" / name
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_dir():
                raise InstallError("Unsafe release path during cleanup")
            remaining = tree_records(path)
            if any(expected.get(key) != value for key, value in remaining.items()):
                raise InstallError(
                    "Managed software changed during cleanup; preserve it for review"
                )
            for resource in (path, *path.rglob("*")):
                if resource.lstat().st_uid != self.host.owner_uid:
                    raise InstallError("Retained software ownership changed during cleanup")
            shutil.rmtree(path)
        if journal.get("purge"):
            self.layout.manifest.unlink(missing_ok=True)
        else:
            write_json(
                self.layout.manifest,
                {
                    "schema_version": 1,
                    "layout": self.layout.document(),
                    "active_release": None,
                    "releases": {},
                    "files": {},
                },
            )
        self.layout.journal.unlink()
        sync_dir(self.layout.prefix)

    def validate_transition(self, journal: dict) -> None:
        candidate = Path(journal["candidate"]) if journal["candidate"] else None
        after = {}
        if candidate is not None:
            after = {
                str(self.layout.worker): {
                    "kind": "file",
                    "sha256": hashlib.sha256(self.worker_content(candidate).encode()).hexdigest(),
                    "mode": 0o644,
                },
                str(self.layout.current): {"kind": "link", "target": str(candidate)},
                str(self.layout.binary): {
                    "kind": "link",
                    "target": str(self.layout.current / "venv/bin/app-watch"),
                },
            }
            # An interrupted pre-helper release never activated these resources.
            if (candidate / "src/scripts/process_inspect.py").exists():
                after[str(self.layout.inspector)] = {
                    "kind": "file",
                    "sha256": digest(candidate / "src/scripts/process_inspect.py"),
                    "mode": 0o755,
                }
                after[str(self.layout.sudoers)] = {
                    "kind": "file",
                    "sha256": hashlib.sha256(self.sudoers_content().encode()).hexdigest(),
                    "mode": 0o440,
                }
        for path in self.layout.resources:
            current = record(path) if path.exists() or path.is_symlink() else None
            before = journal["before"].get(str(path))
            if before is not None:
                before = {key: value for key, value in before.items() if key != "data"}
            if current not in (before, after.get(str(path))):
                raise InstallError(
                    "Installation resources changed after interruption; refusing to overwrite them"
                )

    def recover(self, journal: dict) -> None:
        """Roll back interrupted activation only after re-establishing ownership and safety."""
        if journal.get("phase") == "committed":
            if journal.get("action") == "uninstall":
                self.cleanup_uninstalled(journal)
                return
            manifest = self.read_json(self.layout.manifest)
            self.validate_manifest(manifest)
            self.layout.journal.unlink()
            sync_dir(self.layout.prefix)
            return
        old = journal["old_manifest"]
        candidate = Path(journal["candidate"]) if journal["candidate"] else None
        if candidate is not None:
            if candidate.parent != self.layout.prefix / "releases" or candidate.is_symlink():
                raise InstallError("Interrupted transaction has an unsafe candidate path")
            self.validate_release(candidate.name, journal["candidate_records"])
        self.validate_transition(journal)
        if old is not None:
            if old["layout"] != self.layout.document():
                raise InstallError("Interrupted transaction belongs to another installation layout")
            for name, expected in old["releases"].items():
                self.validate_release(name, expected)
        helper = candidate or self.layout.prefix / "releases" / old["active_release"]
        plans = journal["plans"]
        self.users(helper, plans, "locks")
        with ExitStack() as service_locks:
            self.acquire_user_locks(service_locks, plans, "service_lock")
            self.users(helper, plans, "validate", validate_config=False, recovery=True)
            self.users(helper, plans, "pause")
            with ExitStack() as lifetime_locks:
                self.acquire_user_locks(lifetime_locks, plans, "monitor_lock")
                if old and old["active_release"]:
                    self.users(
                        self.layout.prefix / "releases" / old["active_release"],
                        plans,
                        "validate",
                        validate_config=False,
                        recovery=True,
                    )
                self.restore_global(journal)
                self.users(
                    helper,
                    plans,
                    "refresh",
                    restore=True,
                    verify_worker=bool(old and old["active_release"]),
                )
            self.users(helper, plans, "resume")
        if candidate is not None:
            self.validate_release(candidate.name, journal["candidate_records"])
            shutil.rmtree(candidate)
        self.layout.journal.unlink()
        sync_dir(self.layout.prefix)

    def execute(self, action: str, *, dry_run: bool = False, purge: bool = False) -> None:
        if action not in {"install", "update", "uninstall"} or (purge and action != "uninstall"):
            raise InstallError("Invalid installation action or purge option")
        if action != "uninstall":
            self.host.check_platform()
            self.source_files()
        self.layout.validate(self.host.owner_uid)
        manifest = self.read_json(self.layout.manifest)
        interrupted = self.read_json(self.layout.journal)
        if interrupted is not None and dry_run:
            raise InstallError(
                "Interrupted installation needs safe rollback; dry-run made no changes"
            )
        if interrupted is None:
            self.validate_manifest(manifest)
        if (
            interrupted is None
            and (manifest is None or (action == "update" and manifest["active_release"] is None))
            and action in ("update", "uninstall")
        ):
            raise InstallError(f"{action} requires a managed installation")
        if dry_run:
            if manifest and manifest["active_release"]:
                helper = self.layout.prefix / "releases" / manifest["active_release"]
                self.snapshots(helper, self.host.accounts(), validate_config=action != "uninstall")
            print(
                f"Would {action} App-watch at {self.layout.prefix}; "
                "user configuration and recovery data remain preserved"
            )
            return
        self.layout.prefix.mkdir(mode=0o755, parents=True, exist_ok=True)
        fd = os.open(
            self.layout.prefix / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644
        )
        saved_source = None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.host.owner_uid
                or info.st_mode & 0o022
            ):
                raise InstallError("Installer lock is not safely owned")
            acquire_install_lock(fd)
            self.host.log = self.layout.prefix / "install.log"
            if interrupted is not None:
                candidate = Path(interrupted["candidate"]) if interrupted.get("candidate") else None
                if candidate is not None and self.source.is_relative_to(candidate):
                    saved_source = tempfile.TemporaryDirectory(prefix="app-watch-recovery-input-")
                    copied = Path(saved_source.name)
                    for path in self.source_files():
                        target = copied / path.relative_to(self.source)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                    self.source = copied
                self.recover(interrupted)
                manifest = self.read_json(self.layout.manifest)
            # Revalidate after taking the cross-process installation lock.
            if self.read_json(self.layout.manifest) != manifest:
                raise InstallError("Installation changed during preflight")
            self.validate_manifest(manifest)
            if action == "uninstall" and (manifest is None or manifest["active_release"] is None):
                if purge:
                    self.layout.manifest.unlink(missing_ok=True)
                    sync_dir(self.layout.prefix)
                print("App-watch is uninstalled. User configuration and recovery data preserved.")
                return
            if action == "update" and (manifest is None or manifest["active_release"] is None):
                raise InstallError("update requires an active managed installation")
            accounts = self.host.accounts()
            if manifest and manifest["active_release"]:
                old_helper = self.layout.prefix / "releases" / manifest["active_release"]
                self.snapshots(old_helper, accounts, validate_config=action != "uninstall")
            release = None
            try:
                if action != "uninstall":
                    if manifest is None:
                        self.host.provision()
                    self.host.ensure_inspection_support()
                    release = self.build_release()
                helper = release or self.layout.prefix / "releases" / manifest["active_release"]
                snapshot_helper = helper
                if manifest and manifest["active_release"]:
                    snapshot_helper = self.layout.prefix / "releases" / manifest["active_release"]
                plans = self.snapshots(
                    snapshot_helper, accounts, validate_config=action != "uninstall"
                )
                self.transaction(action, manifest, release, plans, purge=purge)
            except BaseException:
                current = self.read_json(self.layout.manifest)
                if (
                    release is not None
                    and not self.layout.journal.exists()
                    and (current is None or release.name not in current["releases"])
                ):
                    shutil.rmtree(release)
                raise
            print(f"App-watch {action} complete. User configuration and recovery data preserved.")
        finally:
            if saved_source is not None:
                saved_source.cleanup()
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install, update or uninstall App-watch")
    parser.add_argument("action", choices=("install", "update", "uninstall"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-color", action="store_true", help="output is always plain text")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="remove the installer receipt as well as software; preserve all user data",
    )
    args = parser.parse_args()
    if args.purge and args.action != "uninstall":
        parser.error("--purge is only valid with uninstall")
    if sys.version_info < (3, 13):  # noqa: UP036 - bootstrap must diagnose old system Python
        parser.error("Python 3.13 or newer is required")
    if os.geteuid() != 0:
        parser.error(
            "Software installation, including dry-run, requires root; use sudo ./install.sh"
        )
    try:
        Installer(Path(__file__).resolve().parent.parent, Layout.environment(), Host()).execute(
            args.action,
            dry_run=args.dry_run,
            purge=args.purge,
        )
    except (
        InstallError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
    ) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
