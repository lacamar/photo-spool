"""ImportInhibitor never touches a real D-Bus connection in these tests --
backend.inhibit.dbus is fully mocked throughout, unlike the QObject-based
D-Bus modules (notifications.py, device_watch.py) that connect eagerly in
their constructor and are unsafe to import in tests at all. No XDG
isolation needed either: this module touches no filesystem or DB."""
from __future__ import annotations

import unittest
from unittest import mock

from backend.inhibit import ImportInhibitor


class ImportInhibitorTests(unittest.TestCase):
    def _mock_dbus(self):
        patcher = mock.patch("backend.inhibit.dbus")
        mock_dbus = patcher.start()
        self.addCleanup(patcher.stop)
        mock_bus = mock.Mock()
        mock_dbus.SessionBus.return_value = mock_bus
        mock_portal = mock.Mock()
        mock_bus.get_object.return_value = mock_portal
        mock_portal.Inhibit.return_value = "/org/freedesktop/portal/desktop/request/1/handle"
        return mock_dbus, mock_bus, mock_portal

    def test_begin_calls_inhibit_with_suspend_and_idle_flags(self):
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.begin()

        mock_portal.Inhibit.assert_called_once()
        _args, kwargs = mock_portal.Inhibit.call_args
        self.assertEqual(kwargs["dbus_interface"], "org.freedesktop.portal.Inhibit")
        # suspend (4) | idle (8) = 12
        mock_dbus.UInt32.assert_called_once_with(12)

    def test_end_closes_the_request_handle(self):
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.begin()
        inhibitor.end()

        mock_bus.get_object.assert_called_with(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop/request/1/handle"
        )
        mock_request = mock_bus.get_object.return_value
        mock_request.Close.assert_called_once_with(dbus_interface="org.freedesktop.portal.Request")

    def test_reentrant_begin_only_calls_inhibit_once(self):
        # ImportWorker can have several sessions queued/running back to
        # back -- only the *first* begin() should actually call the portal.
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.begin()
        inhibitor.begin()
        inhibitor.begin()

        mock_portal.Inhibit.assert_called_once()

    def test_end_only_releases_after_matching_number_of_begins(self):
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.begin()
        inhibitor.begin()
        inhibitor.end()  # one still outstanding -- must not release yet

        mock_request = mock_bus.get_object.return_value
        mock_request.Close.assert_not_called()

        inhibitor.end()  # now released
        mock_request.Close.assert_called_once()

    def test_extra_end_without_a_begin_is_a_no_op(self):
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.end()  # never began -- must not raise or touch dbus
        mock_dbus.SessionBus.assert_not_called()

    def test_portal_failure_on_begin_is_swallowed(self):
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        mock_portal.Inhibit.side_effect = RuntimeError("no portal implementation")
        inhibitor = ImportInhibitor()
        inhibitor.begin()  # must not raise
        inhibitor.end()  # must also not raise -- nothing to release

    def test_no_dbus_module_is_a_silent_no_op(self):
        with mock.patch("backend.inhibit.dbus", None):
            inhibitor = ImportInhibitor()
            inhibitor.begin()  # must not raise
            inhibitor.end()  # must not raise

    def test_begin_end_begin_inhibits_again(self):
        # A second, later import after the first fully finished must
        # re-inhibit, not silently stay released.
        mock_dbus, mock_bus, mock_portal = self._mock_dbus()
        inhibitor = ImportInhibitor()
        inhibitor.begin()
        inhibitor.end()
        inhibitor.begin()

        self.assertEqual(mock_portal.Inhibit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
