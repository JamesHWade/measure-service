"""Integrity checks and atomic publication for flat scientific evidence bundles.

Checksums identify the supplied files; they are not signatures or an independent
validation of their scientific contents. Consumers must validate row identities,
profiles and calculations appropriate to their own analysis.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate evidence JSON key: {key}")
        result[key] = value
    return result


def _json(path, *, maximum_bytes=64 * 1024 * 1024):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
        raise ValueError(f"evidence metadata must be a bounded regular file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evidence JSON: {path.name}") from error


class VerifiedBundle:
    """Read a known bundle schema, preserving its initial file identities.

    Optional regular files, such as plots, are ignored and never presented as
    verified inputs. Every required core file is verified. Call
    :meth:`verify_unchanged` before publishing derived results.
    """

    def __init__(self, root, *, required_files: set[str], profile: str):
        self.root = Path(root)
        self.required_files = frozenset(required_files)
        self.profile = profile
        if "report.json" not in self.required_files or "complete.json" in self.required_files:
            raise ValueError(
                "core evidence files must include report.json and exclude complete.json"
            )
        self.complete = self._verify()
        self._complete_digest = file_sha256(self.root / "complete.json")
        self.report = _json(self.root / "report.json")
        if not isinstance(self.report, dict) or self.report.get("profile") != profile:
            raise ValueError("unsupported evidence report profile")

    def _verify(self):
        if not stat.S_ISDIR(self.root.lstat().st_mode):
            raise ValueError("evidence root must be a real directory")
        complete = _json(self.root / "complete.json", maximum_bytes=1024 * 1024)
        if not isinstance(complete, dict) or complete.get("schema_version") != 1:
            raise ValueError("unsupported evidence complete.json schema")
        files = complete.get("files")
        if not isinstance(files, dict) or set(files) != self.required_files:
            raise ValueError("evidence manifest must name every required core file exactly once")
        for name, item in files.items():
            if not isinstance(name, str) or name in {".", ".."} or Path(name).name != name:
                raise ValueError("unsafe evidence filename")
            if not isinstance(item, dict):
                raise ValueError("evidence checksum entry must be an object")
            expected_size, expected_hash = item.get("size_bytes"), item.get("sha256")
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or not isinstance(expected_hash, str)
                or not _SHA256.fullmatch(expected_hash)
            ):
                raise ValueError("invalid evidence file size or SHA256")
            path = self.root / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"evidence core file must be regular: {name}")
            if info.st_size != expected_size or file_sha256(path) != expected_hash:
                raise ValueError(f"evidence checksum mismatch: {name}")
        return complete

    def file_digest(self, name: str) -> str:
        """Return the initially verified SHA256 for a core file."""
        return self.complete["files"][name]["sha256"]

    def iter_csv(self, name: str):
        """Stream core CSV rows, rejecting duplicate headers and malformed rows."""
        if name not in self.required_files:
            raise ValueError("CSV must be a verified core evidence file")
        try:
            with (self.root / name).open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source, strict=True)
                columns = reader.fieldnames
                if (
                    not columns
                    or any(not column for column in columns)
                    or len(columns) != len(set(columns))
                ):
                    raise ValueError(f"missing or duplicate CSV header: {name}")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"malformed evidence CSV row: {name}")
                    yield row
        except (UnicodeError, csv.Error) as error:
            raise ValueError(f"invalid evidence CSV: {name}") from error

    def verify_unchanged(self):
        """Reject changed evidence, including edits followed by a new manifest."""
        current = self._verify()
        if (
            current != self.complete
            or file_sha256(self.root / "complete.json") != self._complete_digest
        ):
            raise ValueError("evidence bundle changed during analysis")


@contextmanager
def staged_bundle(output_dir, *, input_dirs=()):
    """Publish flat output files and a completion manifest together."""
    target = Path(output_dir)
    resolved = target.resolve()
    for source in input_dirs:
        source = Path(source).resolve()
        if resolved == source or resolved.is_relative_to(source) or source.is_relative_to(resolved):
            raise ValueError("output evidence must be separate from every input directory")

    def check_target():
        if target.exists() or target.is_symlink():
            if not stat.S_ISDIR(target.lstat().st_mode) or any(target.iterdir()):
                raise ValueError(
                    "output directory must be absent or empty; existing files are preserved"
                )

    check_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".measure-evidence-", dir=target.parent))
    try:
        yield stage
        files = {}
        for item in sorted(stage.iterdir()):
            if item.name == "complete.json" or not stat.S_ISREG(item.lstat().st_mode):
                raise ValueError(
                    "evidence writer must produce flat regular files without complete.json"
                )
            files[item.name] = {"sha256": file_sha256(item), "size_bytes": item.stat().st_size}
        if not files:
            raise ValueError("cannot publish an empty evidence bundle")
        (stage / "complete.json").write_text(
            json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        check_target()
        os.replace(stage, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
