"""Deterministic process evidence for detector tests."""

from app_watch.process import Entrypoint, MatcherSpec, ProcessInfo


class FakeProvider:
    """Deterministic process provider for tests."""

    def __init__(
        self,
        processes: list[ProcessInfo] | None = None,
        env: dict[int, dict[str, str]] | None = None,
        alive: bool = True,
        env_readable: bool = True,
    ) -> None:
        self.processes = processes or []
        self.env = env or {}
        self.alive_result = alive
        self.env_readable = env_readable

    def snapshot(self) -> list[ProcessInfo]:
        return list(self.processes)

    def read_env(self, pid: int, keys) -> dict[str, str] | None:
        if not self.env_readable:
            return None
        return {key: self.env.get(pid, {})[key] for key in keys if key in self.env.get(pid, {})}

    def resolve_directory(self, pid: int, value: str) -> str | None:
        return None  # These fakes provide no verified home/cwd context.

    def alive(self, pid: int, create_time: float) -> bool:
        return self.alive_result and any(
            proc.pid == pid and proc.create_time == create_time for proc in self.processes
        )


def make_matcher(
    *,
    script: str = "/opt/app/run_app",
    env_key: str | None = None,
    env_value: str | None = None,
    argv_tokens: tuple[str, ...] = (),
) -> MatcherSpec:
    env_eq = {env_key: env_value} if env_key and env_value else {}
    return MatcherSpec(
        entrypoints=(Entrypoint(kind="script", value=script),),
        argv_tokens=argv_tokens,
        env_eq=env_eq,
    )
