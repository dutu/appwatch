"""Wasabi environment-based directory evidence precedes command-line evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app_watch.apps.base import DirectoryResult, option_value, resolve

if TYPE_CHECKING:
    from app_watch.process import ProcessInfo, ProcessProvider


class WasabiAdapter:
    blocks_shutdown_on_identity = False
    env_keys: frozenset[str] = frozenset({"WASABI_DATADIR"})

    def resolve_directory(self, proc: ProcessInfo, provider: ProcessProvider) -> DirectoryResult:
        env = provider.read_env(proc.pid, self.env_keys)
        if env is None:
            return None, "inaccessible"
        value = env.get("WASABI_DATADIR") or option_value(proc.cmdline, ("--datadir",))
        return resolve(value, proc, provider)
