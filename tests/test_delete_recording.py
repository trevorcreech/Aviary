import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "avian" / "scripts" / "delete_recording.py"
SPEC = importlib.util.spec_from_file_location("aviary_delete_recording", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DeleteRecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "birds.db"
        self.recordings = self.root / "By_Date"
        self.trash = self.root / ".AviaryTrash"
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE detections ("
                "Date TEXT, Time TEXT, Sci_Name TEXT, Com_Name TEXT, "
                "Confidence REAL, File_Name TEXT)"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def add_detection(self, filename="Mallard-82-2026-08-09-birdnet-21:48:47.mp3"):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-08-09", "21:48:47", "Anas platyrhynchos", "Mallard", 0.8174, filename),
            )
        species = self.recordings / "2026-08-09" / "Mallard"
        species.mkdir(parents=True, exist_ok=True)
        audio = species / filename
        audio.write_bytes(b"audio" * 32)
        (species / (filename + ".png")).write_bytes(b"png" * 32)
        return audio

    def count_rows(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]

    def test_delete_quarantines_files_row_and_database_backup(self):
        audio = self.add_detection()
        result = MODULE.delete_recording(self.db, self.recordings, self.trash, audio.name)

        self.assertTrue(result["ok"])
        self.assertEqual(self.count_rows(), 0)
        self.assertFalse(audio.exists())
        entries = list(self.trash.iterdir())
        self.assertEqual(len(entries), 1)
        quarantined = entries[0]
        self.assertTrue((quarantined / audio.name).is_file())
        self.assertTrue((quarantined / (audio.name + ".png")).is_file())
        metadata = json.loads((quarantined / "deletion.json").read_text())
        self.assertEqual(metadata["detection"]["Com_Name"], "Mallard")
        with sqlite3.connect(quarantined / "birds-before-delete.db") as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM detections").fetchone()[0], 1)

    def test_missing_audio_keeps_database_row(self):
        self.add_detection()
        audio = next(self.recordings.rglob("*.mp3"))
        audio.unlink()
        with self.assertRaises(MODULE.DeleteError) as raised:
            MODULE.delete_recording(self.db, self.recordings, self.trash, audio.name)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertEqual(self.count_rows(), 1)

    def test_duplicate_database_rows_are_rejected(self):
        audio = self.add_detection()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO detections SELECT * FROM detections WHERE File_Name = ?", (audio.name,)
            )
        with self.assertRaises(MODULE.DeleteError) as raised:
            MODULE.delete_recording(self.db, self.recordings, self.trash, audio.name)
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(self.count_rows(), 2)
        self.assertTrue(audio.exists())

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(MODULE.DeleteError) as raised:
            MODULE.delete_recording(self.db, self.recordings, self.trash, "../Mallard.mp3")
        self.assertEqual(raised.exception.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
