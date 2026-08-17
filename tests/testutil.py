"""Shared test scaffolding. NEVER import backend.app_controller or
backend.device_watch from a test -- both touch the real system D-Bus and
(for DeviceWatcher) real attached hardware regardless of XDG overrides, a
documented hazard in this project (see CLAUDE.md's "Testing discipline").
Every other backend module reads XDG_* via os.environ.get(...) fresh on
each call (never cached at import time), so IsolatedTestCase's per-test
env override is sufficient without any import-order gymnastics."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

_XDG_VARS = ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_PICTURES_DIR")


class IsolatedTestCase(unittest.TestCase):
    """Points every XDG_* var backend/paths.py reads at a fresh temp
    directory for the duration of one test, and restores the previous
    environment afterward -- so tests can freely create/import/hash/place
    files without ever touching the real $HOME."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="photo-spool-test-"))
        self._old_env = {var: os.environ.get(var) for var in _XDG_VARS}
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        os.environ["XDG_CACHE_HOME"] = str(self.tmp / "cache")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        os.environ["XDG_PICTURES_DIR"] = str(self.tmp / "pictures")

    def tearDown(self) -> None:
        for var, value in self._old_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()
