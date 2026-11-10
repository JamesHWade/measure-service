"""Stage a Posit Connect bundle that serves the HTTP app as python-fastapi content.

Connect installs ``requirements.txt`` itself and cannot build this checkout, so
the service ships inside the bundle as a wheel. The staged directory holds:

* ``wheels/<sha256 prefix>/`` -- the service wheel. Connect reuses a cached
  Python environment whenever a bundle's requirements text matches an earlier
  one; the content-addressed path makes a code change produce a new text.
* ``requirements.txt`` -- the operator's pinned third-party requirements, then
  the bundle-relative wheel path.
* ``app.py`` -- the ASGI entry point, ``app:app``.
* ``manifest.json`` -- a version 1 manifest, optionally carrying the R
  ``platform`` and ``packages`` inventory that ``rsconnect::writeManifest()``
  recorded for the numerical backends, and an R version constraint.

The script reads no deployment settings and contacts no server. Run it from an
environment with the ``dev`` extra installed; see docs/connect-hosting.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = '''"""Posit Connect entry point for measure-service."""

from measure_service import connect_hosting

connect_hosting.apply_connect_defaults()

from measure_service.server import app  # noqa: E402

__all__ = ["app"]
'''
# Wheel archives record file timestamps. A fixed default keeps an unchanged
# tree at an unchanged wheel digest, and therefore at a reusable environment.
DEFAULT_SOURCE_DATE_EPOCH = "315532800"
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[^\s;]+(\s*;.+)?$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CONSTRAINT = re.compile(r"^(>=|<=|==|~=|>|<)\d+(\.\d+){1,2}$")


def read_pins(path: Path) -> list[str]:
    """Return exact third-party pins, rejecting anything Connect could resolve differently."""
    pins = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if not _PIN.match(line):
            raise ValueError(f"{path.name}:{number} is not an exact name==version pin")
        if re.split(r"[\[=]", line, maxsplit=1)[0].lower().replace("_", "-") == "measure-service":
            raise ValueError(f"{path.name}:{number} pins the service; the bundle ships its wheel")
        pins.append(line)
    if not pins:
        raise ValueError(f"{path.name} lists no pinned requirements")
    return pins


def build_wheel(output: Path) -> Path:
    """Build the service wheel from this checkout."""
    environment = dict(os.environ)
    environment.setdefault("SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH)
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output), str(REPO_ROOT)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("wheel build failed:\n" + result.stdout[-2000:] + result.stderr[-2000:])
    wheels = sorted(output.glob("measure_service-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one service wheel, found {[w.name for w in wheels]}")
    return wheels[0]


def stage_wheel(wheel: Path, bundle: Path) -> str:
    """Copy the wheel under a content-addressed directory; return its requirement line."""
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    target = bundle / "wheels" / digest[:16] / wheel.name
    target.parent.mkdir(parents=True)
    shutil.copy2(wheel, target)
    return "./" + target.relative_to(bundle).as_posix()


def r_inventory(path: Path) -> dict:
    """Return the R runtime and package inventory from an rsconnect manifest."""
    source = json.loads(path.read_text(encoding="utf-8"))
    platform, packages = source.get("platform"), source.get("packages")
    if not isinstance(platform, str) or not _VERSION.match(platform):
        raise ValueError("the R manifest records no concrete R version in 'platform'")
    if not isinstance(packages, dict) or not packages:
        raise ValueError("the R manifest records no packages")
    for name, entry in packages.items():
        description = entry.get("description") if isinstance(entry, dict) else None
        if not isinstance(description, dict) or description.get("Package") != name:
            raise ValueError(f"the R manifest entry for {name} has no matching DESCRIPTION")
    return {"platform": platform, "packages": packages}


def r_constraint(text: str) -> str:
    """Return a comma-separated R version constraint, such as ``>=4.5.0,<5.0``."""
    clauses = [clause.strip() for clause in text.split(",")]
    if not all(_CONSTRAINT.match(clause) for clause in clauses):
        raise ValueError("--r-requires must look like '>=4.5.0' or '>=4.5.0,<5.0'")
    return ",".join(clauses)


def manifest(python_version: str, r_parts: dict | None, r_requires: str | None = None) -> dict:
    """Describe python-fastapi content pinned to one Python minor release."""
    match = _VERSION.match(python_version)
    if not match:
        raise ValueError("--python-version must be a concrete X.Y.Z version")
    if r_requires is not None and r_parts is None:
        raise ValueError("--r-requires needs the package inventory from --r-manifest")
    document = {
        "version": 1,
        "locale": "en_US.UTF-8",
        "metadata": {"appmode": "python-fastapi", "entrypoint": "app:app"},
        "python": {
            "version": python_version,
            "package_manager": {"name": "pip", "package_file": "requirements.txt"},
        },
        # The pins were resolved for this minor release; any patch of it may run them.
        "environment": {"python": {"requires": f"~={match.group(1)}.{match.group(2)}.0"}},
    }
    if r_parts is not None:
        document.update(r_parts)
    if r_requires is not None:
        # Without this Connect matches `platform` to the nearest installed R,
        # which may be older than the backends support.
        document["environment"]["r"] = {"requires": r_constraint(r_requires)}
    return document


def archive(bundle: Path) -> Path:
    """Write ``<bundle>.tar.gz`` with the manifest at the archive root."""
    target = bundle.with_name(bundle.name + ".tar.gz")
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(bundle.rglob("*")):
            tar.add(path, arcname=path.relative_to(bundle).as_posix(), recursive=False)
    return target


def build_bundle(
    bundle: Path,
    pins: list[str],
    python_version: str,
    *,
    app: Path | None = None,
    r_manifest: Path | None = None,
    r_requires: str | None = None,
    wheel: Path | None = None,
) -> Path:
    """Stage ``bundle`` and return the archive beside it."""
    document = manifest(python_version, r_inventory(r_manifest) if r_manifest else None, r_requires)
    if bundle.exists():
        raise FileExistsError(f"{bundle} already exists; choose a new output directory")
    bundle.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as scratch:
        wheel_line = stage_wheel(wheel or build_wheel(Path(scratch)), bundle)
    (bundle / "requirements.txt").write_text(
        "\n".join(
            ["# Generated by scripts/build_connect_bundle.py; do not edit.", *pins, wheel_line]
        )
        + "\n",
        encoding="utf-8",
    )
    if app is None:
        (bundle / "app.py").write_text(DEFAULT_APP, encoding="utf-8")
    else:
        shutil.copy2(app, bundle / "app.py")
    (bundle / "manifest.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return archive(bundle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pins", required=True, type=Path, help="exact third-party pins")
    parser.add_argument("--python-version", required=True, help="server Python, as X.Y.Z")
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory")
    parser.add_argument(
        "--app", type=Path, help="entry module exposing 'app' (default: the server)"
    )
    parser.add_argument(
        "--r-manifest", type=Path, help="manifest.json written by rsconnect::writeManifest()"
    )
    parser.add_argument(
        "--r-requires", help="R versions the server may use, such as '>=4.5.0' (with --r-manifest)"
    )
    args = parser.parse_args(argv)
    try:
        target = build_bundle(
            args.output.resolve(),
            read_pins(args.pins),
            args.python_version,
            app=args.app,
            r_manifest=args.r_manifest,
            r_requires=args.r_requires,
        )
    except (OSError, ValueError, RuntimeError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    sys.stdout.write(f"{target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
