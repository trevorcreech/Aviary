#!/usr/bin/env python3
"""Remove one BirdNET detection and quarantine its generated files.

The web API invokes this through a tightly scoped sudoers rule. The command
accepts only a recording basename; database and filesystem locations are
derived from the local BirdNET-Pi configuration and cannot be supplied by the
caller.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import sys
import uuid


FILENAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]+\.mp3$", re.IGNORECASE)


class DeleteError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 5):
        super().__init__(message)
        self.exit_code = exit_code


def validate_filename(filename: str) -> None:
    if (
        not filename
        or len(filename.encode("utf-8")) > 255
        or ".." in filename
        or not FILENAME_RE.fullmatch(filename)
        or Path(filename).name != filename
    ):
        raise DeleteError("invalid recording filename", 2)


def find_recording(recordings_root: Path, date: str, filename: str) -> Path:
    day_dir = recordings_root / date
    if not day_dir.is_dir():
        raise DeleteError("recording directory not found", 3)

    matches: list[Path] = []
    for species_dir in day_dir.iterdir():
        if not species_dir.is_dir() or species_dir.is_symlink():
            continue
        candidate = species_dir / filename
        if candidate.is_file() and not candidate.is_symlink():
            matches.append(candidate)

    if not matches:
        raise DeleteError("recording file not found", 3)
    if len(matches) != 1:
        raise DeleteError("recording filename is not unique", 4)
    return matches[0]


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for child in path.rglob("*"):
        os.chown(child, uid, gid)


def delete_recording(
    db_path: Path,
    recordings_root: Path,
    quarantine_root: Path,
    filename: str,
    owner: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Delete one exact DB row and atomically move its files out of service."""

    validate_filename(filename)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    rows = conn.execute(
        "SELECT rowid, * FROM detections WHERE File_Name = ?", (filename,)
    ).fetchall()
    if not rows:
        conn.close()
        raise DeleteError("detection not found", 3)
    if len(rows) != 1:
        conn.close()
        raise DeleteError("detection filename is not unique", 4)

    row = rows[0]
    date = str(row["Date"])
    audio_path = find_recording(recordings_root, date, filename)
    companions = [audio_path]
    png_path = audio_path.with_name(audio_path.name + ".png")
    if png_path.is_file() and not png_path.is_symlink():
        companions.append(png_path)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trash_dir = quarantine_root / f"{stamp}-{uuid.uuid4().hex[:10]}"
    trash_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

    backup_path = trash_dir / "birds-before-delete.db"
    backup = sqlite3.connect(str(backup_path))
    conn.backup(backup)
    backup.close()

    moved: list[tuple[Path, Path]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for source in companions:
            destination = trash_dir / source.name
            shutil.move(str(source), str(destination))
            moved.append((source, destination))

        result = conn.execute("DELETE FROM detections WHERE rowid = ?", (row["rowid"],))
        if result.rowcount != 1:
            raise DeleteError("database row was not deleted", 5)

        metadata = {
            "deleted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "recording": filename,
            "original_directory": str(audio_path.parent),
            "detection": dict(row),
        }
        (trash_dir / "deletion.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        shutil.rmtree(trash_dir, ignore_errors=True)
        conn.close()
        raise

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    if integrity != "ok":
        raise DeleteError("database integrity check failed; backup retained", 5)

    if owner is not None:
        _chown_tree(trash_dir, owner[0], owner[1])

    return {
        "ok": True,
        "file": filename,
        "common_name": row["Com_Name"],
        "scientific_name": row["Sci_Name"],
        "recoverable": True,
    }


def read_birdnet_user(conf_path: Path) -> str:
    if not conf_path.is_file():
        raise DeleteError("BirdNET configuration not found", 5)
    for raw in conf_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^BIRDNET_USER\s*=\s*(.*)$", raw)
        if not match:
            continue
        value = match.group(1).strip().strip('"').strip("'")
        if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
            return value
        break
    raise DeleteError("invalid BIRDNET_USER", 5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--file", required=True)
    args = parser.parse_args(argv)

    try:
        user = read_birdnet_user(Path("/etc/birdnet/birdnet.conf"))
        account = pwd.getpwnam(user)
        home = Path(account.pw_dir)
        result = delete_recording(
            home / "BirdNET-Pi/scripts/birds.db",
            home / "BirdSongs/Extracted/By_Date",
            home / "BirdSongs/.AviaryTrash",
            args.file,
            owner=(account.pw_uid, account.pw_gid),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except DeleteError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return exc.exit_code
    except Exception:
        print(json.dumps({"ok": False, "error": "recording deletion failed"}))
        return 5


if __name__ == "__main__":
    sys.exit(main())
