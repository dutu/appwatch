"""Bisq 1 desktop and API-daemon data-directory evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app_watch.apps.base import DirectoryResult, option_value, resolve

if TYPE_CHECKING:
    from app_watch.process import ProcessInfo, ProcessProvider


class BisqAdapter:
    blocks_shutdown_on_identity = False
    env_keys: frozenset[str] = frozenset()

    def resolve_directory(self, proc: ProcessInfo, provider: ProcessProvider) -> DirectoryResult:
        return resolve(option_value(proc.cmdline, ("--appDataDir",)), proc, provider)
