from __future__ import annotations

import plistlib
import socket
import struct
import threading
from pathlib import Path
from unittest import mock

from backend import wifi_iphone
from tests.testutil import IsolatedTestCase


class WifiIphoneTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.lockdown = Path(self.enterContext(mock.patch.object(wifi_iphone, "LOCKDOWN_DIR", Path(self.tmp) / "lockdown")))
        self.lockdown.mkdir()
        (self.lockdown / "UDID1.plist").write_bytes(plistlib.dumps({"WiFiMACAddress": "AA:BB:CC:DD:EE:FF"}))
        (self.lockdown / "SystemConfiguration.plist").write_bytes(plistlib.dumps({"SystemBUID": "BUID"}))
        self.muxer = wifi_iphone.Muxer(Path(self.tmp) / "mux.sock", {"UDID1": "192.168.1.7"}, threading.Lock())
        self.addCleanup(self.muxer.close)

    def test_sockaddr_is_native_linux_layout(self):
        self.assertEqual(wifi_iphone.sockaddr("192.168.1.7")[:8], struct.pack("=H", socket.AF_INET) + bytes([0, 0, 192, 168, 1, 7]))
        self.assertEqual(struct.unpack("=H", wifi_iphone.sockaddr("fe80::1")[:2])[0], socket.AF_INET6)

    def test_mac_of_service_name(self):
        self.assertEqual(wifi_iphone.mac_of("AA:bb:cc:dd:ee:ff@fe80::1-supportsRP-24._apple-mobdev2._tcp.local."), "aa:bb:cc:dd:ee:ff")

    def test_muxer_protocol_over_socket(self):
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(str(self.muxer.sock_path))
        self.addCleanup(conn.close)

        def ask(req):
            body = plistlib.dumps(req)
            conn.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 5) + body)
            length, _, msg, tag = struct.unpack("<IIII", conn.recv(16, socket.MSG_WAITALL))
            self.assertEqual((msg, tag), (8, 5))
            return plistlib.loads(conn.recv(length - 16, socket.MSG_WAITALL))

        [dev] = ask({"MessageType": "ListDevices"})["DeviceList"]
        self.assertEqual(dev["Properties"]["SerialNumber"], "UDID1")
        self.assertEqual(dev["Properties"]["ConnectionType"], "Network")
        self.assertIn(b"WiFiMACAddress", ask({"MessageType": "ReadPairRecord", "PairRecordID": "UDID1"})["PairRecordData"])
        self.assertEqual(ask({"MessageType": "ReadPairRecord", "PairRecordID": "../x"})["Number"], 1)
        self.assertEqual(ask({"MessageType": "ReadBUID"})["BUID"], "BUID")
