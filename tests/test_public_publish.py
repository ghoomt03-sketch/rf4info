from __future__ import annotations

import unittest

from publish_public_snapshot import _snapshot_signature, _validate_staged_paths


class PublicPublishTests(unittest.TestCase):
    def test_signature_ignores_generation_time_only(self):
        first = {"generated_at": "a", "files": [{"path": "core.sqlite3", "sha256": "1"}]}
        second = {"generated_at": "b", "files": [{"path": "core.sqlite3", "sha256": "1"}]}
        changed = {"generated_at": "b", "files": [{"path": "core.sqlite3", "sha256": "2"}]}
        self.assertEqual(_snapshot_signature(first), _snapshot_signature(second))
        self.assertNotEqual(_snapshot_signature(first), _snapshot_signature(changed))

    def test_staged_paths_must_remain_inside_public_data(self):
        _validate_staged_paths(["public_data/manifest.json", "public_data/weeks/a.sqlite3"])
        with self.assertRaises(RuntimeError):
            _validate_staged_paths(["public_data/manifest.json", "data/rf4.sqlite3"])


if __name__ == "__main__":
    unittest.main()
