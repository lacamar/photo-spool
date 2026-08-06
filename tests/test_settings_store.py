"""Regression coverage for a real isolation bug this test suite itself
caused: settings_store.DEFAULTS used to embed
str(paths.default_library_root()) as a *module-level* dict literal, so it
was computed exactly once -- whenever Python first imported this module in
the process, before any test had a chance to override XDG_PICTURES_DIR.
The first test run against these tests actually wrote fake .dng files into
the real ~/Pictures because of this. library_root's default must always be
computed fresh, per call."""
from __future__ import annotations

import os

from backend import db, settings_store
from tests.testutil import IsolatedTestCase


class LibraryRootDefaultTests(IsolatedTestCase):
    def test_default_reflects_current_xdg_pictures_dir_not_import_time(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(settings_store.get(conn, "library_root"), os.environ["XDG_PICTURES_DIR"])
        self.assertEqual(settings_store.all_settings(conn)["library_root"], os.environ["XDG_PICTURES_DIR"])

    def test_default_tracks_env_var_changes_within_a_process(self):
        # Simulates exactly the scenario that caused the real bug: this
        # module (and therefore DEFAULTS) was already imported, then the
        # environment changed (a new test's setUp), and the default must
        # still reflect the *current* value, not whatever was true at
        # import time.
        conn = db.connect()
        self.addCleanup(conn.close)
        first = settings_store.get(conn, "library_root")
        os.environ["XDG_PICTURES_DIR"] = str(self.tmp / "a-different-pictures-dir")
        second = settings_store.get(conn, "library_root")
        self.assertNotEqual(first, second)
        self.assertEqual(second, str(self.tmp / "a-different-pictures-dir"))

    def test_explicit_setting_overrides_the_dynamic_default(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        settings_store.set(conn, "library_root", "/custom/path")
        self.assertEqual(settings_store.get(conn, "library_root"), "/custom/path")
        self.assertEqual(settings_store.all_settings(conn)["library_root"], "/custom/path")
