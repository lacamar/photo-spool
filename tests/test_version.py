import re
import unittest
from pathlib import Path

import backend


class VersionTests(unittest.TestCase):
    def test_matches_spec(self):
        spec = (Path(__file__).parent.parent / "packaging" / "photo-spool.spec").read_text()
        self.assertEqual(backend.__version__, re.search(r"^Version:\s*(\S+)", spec, re.M).group(1))
