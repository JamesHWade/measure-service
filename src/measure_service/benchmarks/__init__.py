"""Small, offline-friendly benchmark data helpers."""

from .moses_archive import (
    MOSES_ARCHIVE_MD5,
    MOSES_ARCHIVE_SIZE_BYTES,
    MOSES_ARCHIVE_URL,
    MOSES_DOI,
    MOSES_RECORD_ID,
    fetch_archive,
    inspect_archive,
    read_member,
)

__all__ = [
    "MOSES_ARCHIVE_MD5",
    "MOSES_ARCHIVE_SIZE_BYTES",
    "MOSES_ARCHIVE_URL",
    "MOSES_DOI",
    "MOSES_RECORD_ID",
    "fetch_archive",
    "inspect_archive",
    "read_member",
]
