from threading import Event

from app_watch.process import Observation
from app_watch.services.async_controller import AsyncSystemdController
from app_watch.services.systemd import RealSystemdController, SubmissionClass, UnitState
from app_watch.trigger.monitor import Monitor
from tests.helpers import FakeProvider
from tests.test_execution import Manager, running
from tests.test_execution import store as store
from tests.test_execution import watch as watch


def test_start_result_precedes_any_absence_probe():
    entered, release = Event(), Event()

    class SlowManager(Manager):
        def start_unit(self, unit):
            entered.set()
            assert release.wait(3)
            return super().start_unit(unit)

        def unit_state(self, unit):
            assert release.is_set()
            return super().unit_state(unit)

    with AsyncSystemdController(SlowManager()) as controller:
        try:
            result = controller.start_unit("test.service")
            assert result.classification is SubmissionClass.INCONCLUSIVE
            assert entered.wait(3)
            assert controller.poll_start("test.service") is None
            assert controller.unit_state("test.service") is None
            assert not controller.probes
        finally:
            release.set()
        controller.starts["test.service"].result(timeout=3)
        assert controller.poll_start("test.service").classification is SubmissionClass.ACCEPTED
        assert controller.unit_state("test.service") is None
        controller.probes["test.service"].result(timeout=3)
        assert controller.unit_state("test.service").is_active_or_pending()


def test_slow_submission_does_not_prevent_observing_wallet_restart(watch, store):
    entered, release = Event(), Event()

    class SlowManager(Manager):
        def start_unit(self, unit):
            entered.set()
            assert release.wait(3)
            return super().start_unit(unit)

    provider = FakeProvider([running(watch)])
    with AsyncSystemdController(SlowManager()) as controller:
        try:
            monitor = Monitor(watch, store, provider, controller)
            monitor.tick(monotonic_now=0)
            provider.processes.clear()
            monitor.tick(monotonic_now=1)
            monitor.tick(monotonic_now=6)
            assert entered.wait(3)
            provider.processes = [running(watch)]
            monitor.tick(monotonic_now=7)
            assert monitor.state.last_observation.observation is Observation.RUNNING
            assert monitor.state.armed and not monitor.state.blocked_reason
        finally:
            release.set()


def test_manager_queue_is_bounded():
    release = Event()

    class SlowManager(Manager):
        def unit_state(self, unit):
            assert release.wait(3)
            return UnitState("loaded", "inactive", "dead")

    controller = AsyncSystemdController(SlowManager())
    try:
        for index in range(100):
            assert controller.unit_state(f"unit-{index}") is None
        assert len(controller.probes) == 16
        result = controller.start_unit("new.service")
        assert result.classification is SubmissionClass.REJECTED and not result.spawned
    finally:
        release.set()
        controller.close()
        controller.executor.shutdown(wait=True)


def test_absence_fallback_shares_one_deadline(monkeypatch):
    import subprocess

    clock, timeouts = [0.0], []
    monkeypatch.setattr("app_watch.services.systemd.monotonic", lambda: clock[0])

    def run(argv, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(timeout)
        if "list-jobs" in argv:
            clock[0] += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        clock[0] += 6
        return subprocess.CompletedProcess(argv, 1 if "show" in argv else 0, "", "")

    monkeypatch.setattr("app_watch.services.systemd._run", run)
    assert RealSystemdController.unit_state("test.service") is None
    assert timeouts == [15, 9, 3]
