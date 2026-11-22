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
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from app_watch.config.models import WatchConfig
from app_watch.config.runtime import (
    DATA_DIR_PLACEHOLDER,
    JOB_ID_PLACEHOLDER,
    parse_parameters,
    render,
)
from app_watch.errors import ConfigurationError
from app_watch.process import MatcherSpec, matcher_from_canonical


def validate_document(document: object) -> WatchConfig:
    try:
        return WatchConfig.model_validate(document)
    except RecursionError as error:
        raise ConfigurationError.with_summary("Configuration nesting is too deep") from error
    except ValidationError as error:
        issues = error.errors(include_url=False, include_input=False)
        messages = []
        for issue in issues:
            path = ".".join(str(part) for part in issue["loc"]) or "configuration"
            messages.append(f"{path}: {issue['msg']}")

        # Show the first issue to the installer. Extra fields and keys inside the
        # two free-form mappings come from YAML, so omit them from the location.
        issue = issues[0]
        location = issue["loc"]
        if issue["type"] in ("extra_forbidden", "invalid_key"):
            location = location[:-1]
        for mapping in (("trigger", "params"), ("application", "matcher", "env_eq")):
            if location[: len(mapping)] == mapping:
                location = mapping
                break
        path = ".".join(str(part) for part in location) or "configuration"
        cause = issue.get("ctx", {}).get("error")
        if isinstance(cause, ConfigurationError) and cause.summary:
            summary = cause.summary
        elif issue["type"] in ("value_error", "assertion_error"):
            summary = "Invalid setting"
        else:
            summary = issue["msg"]
        raise ConfigurationError.with_summary(
            f"{path}: {summary}", detail="\n".join(messages)
        ) from error


def load_config(path: Path) -> WatchConfig:
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream)
    except (OSError, YAMLError, ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, UnicodeError):
            summary = "Configuration must be UTF-8"
        elif isinstance(error, RecursionError):
            summary = "Configuration nesting is too deep"
        elif isinstance(error, OSError):
            summary = "Configuration file is missing or unreadable"
        else:
            if isinstance(error, DuplicateKeyError):
                summary = "Duplicate YAML key"
            else:
                summary = "Invalid YAML"
            if isinstance(error, MarkedYAMLError) and error.problem_mark is not None:
                mark = error.problem_mark
                summary += f" at line {mark.line + 1}, column {mark.column + 1}"
        raise ConfigurationError.with_summary(
            summary, detail=f"Cannot load {path}: {error}"
        ) from error
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
            raise ConfigurationError.with_summary("Account home must be absolute")
        params = dict(config.trigger.params)
        params.update(
            parse_parameters([f"{key}={value}" for key, value in (runtime_params or {}).items()])
        )
        used: set[str] = set()
        template_params = dict(params)

        def path(value: str, *, canonical: bool = True) -> str:
            value = render(value, template_params, used)
            if value == "~" or value.startswith("~/"):
                value = account.pw_dir + value[1:]
            if not os.path.isabs(value):
                raise ConfigurationError.with_summary("Prepared paths must be absolute")
            # Executable symlinks can select an environment (e.g. venv Python).
            return str(Path(value).resolve()) if canonical else value

        document = config.model_dump()
        app = document["application"]
        app["data_dir"] = path(app["data_dir"])
        template_params[DATA_DIR_PLACEHOLDER] = app["data_dir"]
        matcher = app["matcher"]
        if matcher["exe_realpath"] is not None:
            matcher["exe_realpath"] = path(matcher["exe_realpath"])
        for entry in matcher["entrypoints"]:
            if entry["kind"] not in ("java_main", "executable_name"):
                entry["value"] = path(entry["value"])
        # Environment predicates compare literal process values, not canonical paths.
        matcher["env_eq"] = {
            key: render(value, template_params, used) for key, value in matcher["env_eq"].items()
        }
        template_params[JOB_ID_PLACEHOLDER] = config.job.id
        argv = document["action"]["argv"]
        argv[0] = path(argv[0], canonical=False)
        argv[1:] = [render(value, template_params, used) for value in argv[1:]]
        unused = set(params) - used
        if unused:
            raise ConfigurationError.with_summary(
                "Unused runtime parameters",
                detail="Unused runtime parameters: " + ", ".join(sorted(unused)),
            )
        effective = validate_document(document)
        if check_runtime:
            directory = Path(app["data_dir"])
            if not directory.is_dir() or directory.stat().st_uid != account.pw_uid:
                raise ConfigurationError.with_summary(
                    "application.data_dir must exist and be owned by this user"
                )
            executable = Path(argv[0])
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ConfigurationError.with_summary(
                    "action.argv[0] must name an available executable",
                    detail=f"Action executable is unavailable: {executable}",
                )
        return PreparedWatch(
            effective,
            source.resolve(),
            account.pw_uid,
            Path(account.pw_dir),
            params,
            matcher_from_canonical(matcher),
        )
    except (KeyError, OSError, RuntimeError) as error:
        raise ConfigurationError.with_summary(
            "Cannot resolve or access a configured path",
            detail=f"Cannot prepare watch: {error}",
        ) from error
