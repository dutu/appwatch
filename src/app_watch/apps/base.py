"""Small application adapter contract and shared directory-evidence helpers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app_watch.process import ProcessInfo, ProcessProvider

# (canonical directory, resolved/unresolved/inaccessible). Uncertainty must never
# be interpreted as evidence that the watched instance has stopped.
type DirectoryResult = tuple[str | None, str]


class AppAdapter(Protocol):
    env_keys: frozenset[str]

    def resolve_directory(
        self, proc: ProcessInfo, provider: ProcessProvider
    ) -> DirectoryResult: ...


def option_value(cmdline: tuple[str, ...], options: tuple[str, ...]) -> str | None:
    for index, token in enumerate(cmdline):
        for option in options:
            if token == option and index + 1 < len(cmdline):
                return cmdline[index + 1]
            if token.startswith(f"{option}="):
                return token[len(option) + 1 :]
    return None


def resolve(value: str | None, proc: ProcessInfo, provider: ProcessProvider) -> DirectoryResult:
    if not value:
        return None, "unresolved"
    if os.path.isabs(value):
        return os.path.realpath(value), "resolved"
    directory = provider.resolve_directory(proc.pid, value)
    return (directory, "resolved") if directory is not None else (None, "inaccessible")
