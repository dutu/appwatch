"""Inject one post-refresh failure into a real installer transaction in the VM."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.install import Host, Installer, InstallError, Layout  # noqa: E402


class FailingRefresh(Host):
    fail = True

    def user(self, release, account, request):
        result = super().user(release, account, request)
        if request["action"] == "refresh" and self.fail:
            self.fail = False
            raise InstallError("Injected failure after real user-unit refresh")
        return result


def main():
    assert os.geteuid() == 0 and os.uname().nodename == "codex-test"
    layout = Layout.environment()
    before = layout.current.resolve()
    manifest = layout.manifest.read_bytes()
    try:
        Installer(ROOT, layout, FailingRefresh()).execute("update")
    except InstallError as error:
        assert "Injected failure" in str(error), str(error)
    else:
        raise AssertionError("The injected refresh failure did not occur")
    assert layout.current.resolve() == before
    assert layout.manifest.read_bytes() == manifest
    assert not layout.journal.exists()
    print("Real installer activation/refresh rollback passed")


if __name__ == "__main__":
    main()
