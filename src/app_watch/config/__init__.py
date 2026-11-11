"""Safe YAML loading and trigger-only configuration preparation."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from app_watch.config.models import WatchConfig
from app_watch.config.runtime import parse_parameters, render
from app_watch.errors import ConfigurationError
from app_watch.process import MatcherSpec, matcher_from_canonical


def validate_document(document: object) -> WatchConfig:
    try:
        return WatchConfig.model_validate(document)
    except RecursionError as error:
        raise ConfigurationError("Configuration nesting is too deep") from error
    except ValidationError as error:
        messages = []
        for issue in error.errors(include_url=False, include_input=False, include_context=False):
            path = ".".join(str(part) for part in issue["loc"]) or "configuration"
            messages.append(f"{path}: {issue['msg']}")
        raise ConfigurationError("\n".join(messages)) from error


def load_config(path: Path) -> WatchConfig:
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream)
    except (OSError, UnicodeError, YAMLError, RecursionError) as error:
        raise ConfigurationError(f"Cannot load {path}: {error}") from error
    return validate_document(document)


@dataclass(frozen=True, slots=True)
class PreparedWatch:
    config: WatchConfig
    source_path: Path
    owner_uid: int
    home: Path
    params: dict[str, str]
    matcher: MatcherSpec

    @property
    def scope(self) -> str:
        # Config edits keep their namespace so pending/active work stays visible.
        return self.identity.scope

    @property
    def identity(self):
        from app_watch.config.identity import WatchIdentity

        return WatchIdentity(
            source_path=str(self.source_path),
            job_id=self.config.job.id,
            params=dict(self.params),
        )

    def fingerprint(self) -> str:
        value = {
            "job_id": self.config.job.id,
            "application": self.config.application.model_dump(),
            "action": self.config.action.model_dump(),
            "enabled": self.config.trigger.enabled,
            "deadline_seconds": self.config.trigger.deadline_seconds,
            "owner_uid": self.owner_uid,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare_config(
    config: WatchConfig,
    source: Path,
    *,
    runtime_params: Mapping[str, str] | None = None,
    check_runtime: bool = True,
) -> PreparedWatch:
    try:
        account = pwd.getpwuid(os.geteuid())
        if not os.path.isabs(account.pw_dir):
            raise ConfigurationError("Account home must be absolute")
        params = dict(config.trigger.params)
        params.update(
            parse_parameters([f"{key}={value}" for key, value in (runtime_params or {}).items()])
        )
        used: set[str] = set()

        def path(value: str, *, canonical: bool = True) -> str:
            value = render(value, params, used)
            if value == "~" or value.startswith("~/"):
                value = account.pw_dir + value[1:]
            if not os.path.isabs(value):
                raise ConfigurationError("Prepared paths must be absolute")
            # Executable symlinks can select an environment (e.g. venv Python).
            return str(Path(value).resolve()) if canonical else value

        document = config.model_dump()
        app = document["application"]
        app["data_dir"] = path(app["data_dir"])
        matcher = app["matcher"]
        if matcher["exe_realpath"] is not None:
            matcher["exe_realpath"] = path(matcher["exe_realpath"])
        for entry in matcher["entrypoints"]:
            if entry["kind"] != "java_main":
                entry["value"] = path(entry["value"])
        # Environment predicates compare literal process values, not canonical paths.
        matcher["env_eq"] = {
            key: render(value, params, used) for key, value in matcher["env_eq"].items()
        }
        argv = document["action"]["argv"]
        argv[0] = path(argv[0], canonical=False)
        argv[1:] = [render(value, params, used) for value in argv[1:]]
        unused = set(params) - used
        if unused:
            raise ConfigurationError("Unused runtime parameters: " + ", ".join(sorted(unused)))
        effective = validate_document(document)
        if check_runtime:
            directory = Path(app["data_dir"])
            if not directory.is_dir() or directory.stat().st_uid != account.pw_uid:
                raise ConfigurationError(
                    "application.data_dir must exist and be owned by this user"
                )
            executable = Path(argv[0])
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ConfigurationError(f"Action executable is unavailable: {executable}")
        return PreparedWatch(
            effective,
            source.resolve(),
            account.pw_uid,
            Path(account.pw_dir),
            params,
            matcher_from_canonical(matcher),
        )
    except (KeyError, OSError, RuntimeError) as error:
        raise ConfigurationError(f"Cannot prepare watch: {error}") from error
