"""Electrum explicit directory arguments override ELECTRUMDIR."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app_watch.apps.base import DirectoryResult, option_value, resolve

if TYPE_CHECKING:
    from app_watch.process import ProcessInfo, ProcessProvider


class ElectrumAdapter:
    env_keys: frozenset[str] = frozenset({"ELECTRUMDIR"})

    def resolve_directory(self, proc: ProcessInfo, provider: ProcessProvider) -> DirectoryResult:
        value = option_value(proc.cmdline, ("-D", "--dir"))
        if value:
            return resolve(value, proc, provider)
        env = provider.read_env(proc.pid, self.env_keys)
        if env is None:
            return None, "inaccessible"
        return resolve(env.get("ELECTRUMDIR"), proc, provider)
