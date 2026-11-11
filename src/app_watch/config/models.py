"""Strict trigger-only YAML schema, adapted from VaultKeep's applicable settings."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app_watch.apps.registry import get_adapter

type NonEmpty = Annotated[str, StringConstraints(min_length=1)]
type Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def no_nul(cls, value: object) -> object:
        active: set[int] = set()

        def inspect(item: object) -> None:
            if isinstance(item, str) and "\0" in item:
                raise ValueError("NUL characters are not allowed")
            if isinstance(item, (dict, list)):
                identity = id(item)
                if identity in active:
                    raise ValueError("Recursive YAML aliases are not allowed")
                active.add(identity)
                if isinstance(item, dict):
                    for key, child in item.items():
                        inspect(key)
                        inspect(child)
                else:
                    for child in item:
                        inspect(child)
                active.remove(identity)

        inspect(value)
        return value


def absolute_or_home(value: str) -> str:
    if value != "~" and not value.startswith(("/", "~/")):
        raise ValueError("path must be absolute or start with ~/")
    return value


class JobConfig(StrictModel):
    id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class EntrypointConfig(StrictModel):
    kind: Literal["script", "jar", "java_main", "executable_name"]
    value: NonEmpty

    @model_validator(mode="after")
    def validate_path(self) -> EntrypointConfig:
        if self.kind == "executable_name":
            if (
                self.value in (".", "..")
                or any(char in self.value for char in "/\\*?[]{}")
                or any(char.isspace() or not char.isprintable() for char in self.value)
            ):
                raise ValueError("executable_name must be a literal basename")
        elif self.kind != "java_main":
            absolute_or_home(self.value)
        return self


class MatcherConfig(StrictModel):
    entrypoints: list[EntrypointConfig] = Field(min_length=1)
    exe_realpath: NonEmpty | None = None
    argv_tokens: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(
        default_factory=list
    )
    cmdline_regex: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    env_eq: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)

    @field_validator("exe_realpath")
    @classmethod
    def executable_path(cls, value: str | None) -> str | None:
        return absolute_or_home(value) if value is not None else None

    @field_validator("cmdline_regex")
    @classmethod
    def regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid process regex: {error}") from error
        return value

    @model_validator(mode="after")
    def unique_entrypoints(self) -> MatcherConfig:
        identities = [(item.kind, item.value) for item in self.entrypoints]
        if len(identities) != len(set(identities)):
            raise ValueError("matcher entrypoints must be unique")
        return self


class ApplicationConfig(StrictModel):
    name: NonEmpty
    data_dir: NonEmpty
    matcher: MatcherConfig

    @field_validator("data_dir")
    @classmethod
    def directory_path(cls, value: str) -> str:
        return absolute_or_home(value)

    @model_validator(mode="after")
    def adapter_settings(self) -> ApplicationConfig:
        adapter = get_adapter(self.name)
        if self.name == "monero":
            identities = {(item.kind, item.value) for item in self.matcher.entrypoints}
            if identities != {
                ("executable_name", "monero-wallet-gui"),
                ("executable_name", "monero-wallet-rpc"),
            }:
                raise ValueError("Monero requires GUI and RPC executable_name entrypoints")
            if (
                self.matcher.exe_realpath is not None
                or self.matcher.argv_tokens
                or self.matcher.cmdline_regex is not None
                or self.matcher.env_eq
            ):
                raise ValueError("Monero matcher must not narrow the shared process guard")
        unknown = set(self.matcher.env_eq) - adapter.env_keys
        if unknown:
            raise ValueError("unsupported matcher environment keys: " + ", ".join(sorted(unknown)))
        return self


class ActionConfig(StrictModel):
    argv: list[str] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def executable_path(cls, value: list[str]) -> list[str]:
        if not value[0] or value[0] == "~":
            raise ValueError("action.argv[0] must name an executable")
        absolute_or_home(value[0])
        return value


class TriggerConfig(StrictModel):
    enabled: bool = False
    poll_seconds: Seconds = 2.0
    confirm_seconds: Seconds = 5.0
    deadline_seconds: Seconds | None = None
    params: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def parameters(cls, value: dict[str, str]) -> dict[str, str]:
        from app_watch.config.runtime import parse_parameters

        return parse_parameters([f"{key}={item}" for key, item in value.items()])


class NotificationCategory(StrictModel):
    enabled: bool = False


class DesktopNotifications(StrictModel):
    command: NonEmpty = "notify-send"
    timeout_ms: Annotated[int, Field(gt=0)] = 5000
    trigger: NotificationCategory = Field(default_factory=NotificationCategory)


class ExternalNotifier(StrictModel):
    command: NonEmpty
    timeout_seconds: Annotated[float, Field(gt=0, le=3600, allow_inf_nan=False)] = 15.0

    @field_validator("command")
    @classmethod
    def single_executable(cls, value: str) -> str:
        if (
            not value.strip()
            or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "|;&<>`$'\"\\(){}[]*?!#~"
                for c in value
            )
            or (not value.startswith("/") and "/" in value)
            or value in (".", "..")
            or value.endswith("/")
            or value.startswith("-")
        ):
            raise ValueError(
                "Must be one executable name or absolute path, without shell syntax or arguments."
            )
        return value


class LoggingConfig(StrictModel):
    level: Literal["error", "warning", "info", "debug"] = "info"
    include_command_output: bool = False


class WatchConfig(StrictModel):
    config_version: Literal[1]
    job: JobConfig
    application: ApplicationConfig
    action: ActionConfig
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    desktop_notifications: DesktopNotifications = Field(default_factory=DesktopNotifications)
    external_notifier: ExternalNotifier | None = None
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("config_version", mode="before")
    @classmethod
    def version_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("config_version must be an integer")
        return value
