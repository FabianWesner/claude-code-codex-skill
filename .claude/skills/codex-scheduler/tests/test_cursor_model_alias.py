import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cursor_client

AVAILABLE = ["grok-4.7-high", "grok-4.7-xhigh", "cursor-grok-4.6-high", "composer-2.5"]


class CursorModelAlias(unittest.TestCase):
    def test_prefix_is_optional_either_way(self):
        r = cursor_client.resolve_model
        self.assertEqual(r("cursor-grok-4.7", "high", available=AVAILABLE), "grok-4.7-high")
        self.assertEqual(r("grok-4.7", "high", available=AVAILABLE), "grok-4.7-high")
        self.assertEqual(r("grok-4.6", "high", available=AVAILABLE), "cursor-grok-4.6-high")
        self.assertEqual(r("composer-2.5", "high", available=AVAILABLE), "composer-2.5")


if __name__ == "__main__":
    unittest.main()
