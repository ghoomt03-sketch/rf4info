from __future__ import annotations

import unittest

from publish_public_snapshot import _snapshot_signature, _source_fingerprint, _validate_staged_paths


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

    def test_source_fingerprint_changes_with_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.sqlite3"
            path.write_bytes(b"a")
            first = _source_fingerprint(path)
            path.write_bytes(b"more data")
            self.assertNotEqual(first, _source_fingerprint(path))


if __name__ == "__main__":
    unittest.main()
