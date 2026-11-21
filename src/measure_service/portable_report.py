"""Text, axis and file handling shared by the portable scientific reports."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence


def _json(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _xml_safe(value: str) -> str:
    output = []
    for char in value:
        codepoint = ord(char)
        allowed = (
            codepoint in {9, 10, 13}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        output.append(char if allowed else f"\\u{codepoint:04x}")
    return "".join(output)


def _e(value: Any) -> str:
    if value is None:
        text = "Unavailable"
    elif isinstance(value, str):
        text = value
    else:
        text = _json(value)
    return html.escape(_xml_safe(text), quote=True)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("CSV evidence cannot contain non-finite numbers")
        return _json(value)
    text = _json(value) if isinstance(value, (dict, list)) else _xml_safe(str(value))
    first = text.lstrip(" \t\r\n\v\f\x00\ufeff")[:1]
    return "'" + text if first in {"=", "+", "-", "@"} else text


def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow(_csv_value(row.get(field)) for field in fields)
    return stream.getvalue().encode("utf-8")


def _axis_mapper(domain: Sequence[float], start: float, extent: float):
    low, high = (min(domain), max(domain)) if domain else (0.0, 1.0)
    with localcontext() as context:
        context.prec = 120
        low_decimal = Decimal.from_float(low)
        span = Decimal.from_float(high) - low_decimal
        start_decimal, extent_decimal = Decimal.from_float(start), Decimal.from_float(extent)

    def mapper(value: float) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            fraction = (
                Decimal("0.5") if span == 0 else (Decimal.from_float(value) - low_decimal) / span
            )
            return start_decimal + fraction * extent_decimal

    return mapper


def _ticks(domain: Sequence[float], *, digits: int = 8) -> list[tuple[Decimal, str]]:
    if not domain:
        return []
    low, high = min(domain), max(domain)
    with localcontext() as context:
        context.prec = 120
        low_decimal, high_decimal = Decimal.from_float(low), Decimal.from_float(high)
        if low_decimal == high_decimal:
            values = [low_decimal]
        else:
            span = high_decimal - low_decimal
            raw_step = span / Decimal(4)
            magnitude = Decimal(1).scaleb(raw_step.adjusted())
            candidates = [
                factor * magnitude
                for factor in (Decimal(1), Decimal(2), Decimal(2.5), Decimal(5), Decimal(10))
            ]
            step = min(candidates, key=lambda candidate: abs(span / candidate - Decimal(4)))
            first = int((low_decimal / step).to_integral_value(rounding="ROUND_CEILING"))
            last = int((high_decimal / step).to_integral_value(rounding="ROUND_FLOOR"))
            values = [step * Decimal(index) for index in range(first, last + 1)]
            if not values or len(values) > 12:
                values = [low_decimal, high_decimal]
    return [(value, format(value, f".{digits}g")) for value in values]


def write_report_files(
    artifacts: Mapping[str, bytes], output_dir: str | os.PathLike[str]
) -> dict[str, dict[str, str]]:
    """Publish rendered files in a new directory and return their content digests."""
    destination = Path(output_dir)
    if destination.is_symlink() or destination.exists():
        raise FileExistsError(f"Report output directory already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        for filename, content in artifacts.items():
            path = destination / filename
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            written.append(path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        return {
            name: {"path": str(destination / name), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in artifacts.items()
        }
    except Exception:
        for path in written:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
