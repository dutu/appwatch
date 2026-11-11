"""Bounded nonblocking manager calls for the monitor's polling loop."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress

from app_watch.services.systemd import (
    SubmissionClass,
    SubmissionResult,
    SystemdController,
    UnitState,
)


class AsyncSystemdController:
    """Keep at most sixteen requests in flight and two manager calls executing.

    Start intent is persisted by the monitor before enqueueing. A pending start
    prevents absence queries from being interpreted before its result is collected.
    Results live only until the next poll; no service evidence is cached as current.
    """

    def __init__(self, controller: SystemdController):
        self.controller = controller
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-watch-systemd")
        self.starts: dict[str, Future] = {}
        self.probes: dict[str, Future] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def manager_reachable(self) -> bool:
        return self.controller.manager_reachable()

    def template_ready(self) -> bool:
        return self.controller.template_ready()

    def start_unit(self, unit: str) -> SubmissionResult:
        if unit in self.starts:
            return SubmissionResult(
                SubmissionClass.INCONCLUSIVE, None, "Start already in flight", True
            )
        if len(self.starts) + len(self.probes) >= 16:
            return SubmissionResult(
                SubmissionClass.REJECTED, None, "Local manager queue is full", False
            )
        try:
            self.starts[unit] = self.executor.submit(self.controller.start_unit, unit)
        except RuntimeError:
            return SubmissionResult(
                SubmissionClass.REJECTED, None, "Local manager queue is closed", False
            )
        return SubmissionResult(SubmissionClass.INCONCLUSIVE, None, "Start result pending", True)

    def poll_start(self, unit: str) -> SubmissionResult | None:
        future = self.starts.get(unit)
        if future is None or not future.done():
            return None
        del self.starts[unit]
        try:
            return future.result()
        except Exception:
            return SubmissionResult(
                SubmissionClass.INCONCLUSIVE, None, "Manager call failed after enqueue", True
            )

    def unit_state(self, unit: str) -> UnitState | None:
        if unit in self.starts:
            return None
        future = self.probes.get(unit)
        if future is not None:
            if not future.done():
                return None
            del self.probes[unit]
            try:
                return future.result()
            except Exception:
                return None
        if len(self.starts) + len(self.probes) < 16:
            with suppress(RuntimeError):
                self.probes[unit] = self.executor.submit(self.controller.unit_state, unit)
        return None

    def close(self) -> None:
        # In-flight calls retain their own subprocess timeouts. Pending start
        # intent remains durable even if shutdown cancels a not-yet-running task.
        self.executor.shutdown(wait=False, cancel_futures=True)
