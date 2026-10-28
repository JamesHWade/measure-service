"""Exercise the upgrade from the former standalone method distribution."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import zipfile
from email import message_from_bytes
from pathlib import Path


def _metadata(archive: zipfile.ZipFile, filename: Path) -> tuple[str, str]:
    matches = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(matches) != 1:
        raise ValueError(f"Expected one METADATA file in {filename.name}")
    message = message_from_bytes(archive.read(matches[0]))
    name = message.get("Name")
    version = message.get("Version")
    if not name or not version:
        raise ValueError(f"Missing project name or version in {filename.name}")
    return name, version


def _create_legacy_wheel(service_wheel: Path, output_dir: Path) -> Path:
    with zipfile.ZipFile(service_wheel) as archive:
        service_name, version = _metadata(archive, service_wheel)
        if service_name.lower().replace("_", "-") != "measure-service":
            raise ValueError("The selected wheel must be for measure-service")
        package_files = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith("measure_methods/") and name.endswith(".py")
        }
    if not package_files:
        raise ValueError("The service wheel does not contain the measure_methods package")

    dist_info = f"measure_methods-{version}.dist-info"
    entries = dict(package_files)
    entries[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        "Name: measure-methods\n"
        f"Version: {version}\n"
        "Summary: Local wheel used to test the historical package collision\n\n"
    ).encode()
    entries[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: measure-service upgrade check\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode()
    entries[f"{dist_info}/top_level.txt"] = b"measure_methods\n"

    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, contents in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(contents)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    entries[f"{dist_info}/RECORD"] = record.getvalue().encode()

    legacy_wheel = output_dir / f"measure_methods-{version}-py3-none-any.whl"
    with zipfile.ZipFile(legacy_wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return legacy_wheel


def _validate_legacy_wheel(legacy_wheel: Path) -> None:
    with zipfile.ZipFile(legacy_wheel) as archive:
        name, _ = _metadata(archive, legacy_wheel)
        if name.lower().replace("_", "-") != "measure-methods":
            raise ValueError("The historical wheel must be for measure-methods")
        members = set(archive.namelist())
        required = {"measure_methods/__init__.py", "measure_methods/volume.py"}
        if not required <= members:
            raise ValueError("The historical wheel does not contain the overlapping method files")


def _run(command: list[str], working_dir: Path) -> None:
    subprocess.run(command, check=True, cwd=working_dir)


def _write_probe(root: Path) -> None:
    (root / "measure_backend_probe.py").write_text(
        """from measure_service.method_backend import method_backend_info


def check_collision():
    from measure_methods import constant_solvent_use

    assert constant_solvent_use(1, 1, {"A": 1}) == {"A": 1.0}
    try:
        method_backend_info()
    except RuntimeError as error:
        message = str(error)
        assert "both claim the 'measure_methods' import files" in message
        assert "Remove 'measure-methods' first" in message
        assert "force-reinstall 'measure-service'" in message
        assert "/" not in message
    else:
        raise AssertionError("Coexisting distributions must block new method calculations")


def check_repaired_install():
    from measure_methods import constant_solvent_use

    assert constant_solvent_use(1, 1, {"A": 1}) == {"A": 1.0}
    evidence = method_backend_info()
    assert evidence["package"] == "measure-service"
    assert evidence["version"]
    assert evidence["source_revision"] is None
    assert evidence["source_ref"] is None


def check_method_package_is_missing():
    try:
        from measure_methods import constant_solvent_use
    except ImportError:
        return
    raise AssertionError("The method package should still be missing")
"""
    )


def _check_upgrade(service_wheel: Path, legacy_wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="measure-service-upgrade-") as directory:
        root = Path(directory)
        environment = root / "venv"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _write_probe(root)
        _run([sys.executable, "-m", "venv", str(environment)], root)

        _run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(legacy_wheel)],
            root,
        )
        _run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(service_wheel)],
            root,
        )
        _run(
            [
                str(python),
                "-c",
                """
from importlib import metadata
from measure_backend_probe import check_collision

metadata.distribution("measure-service")
metadata.distribution("measure-methods")
check_collision()
""",
            ],
            root,
        )
        _run([str(python), "-m", "pip", "uninstall", "--yes", "measure-methods"], root)
        _run(
            [
                str(python),
                "-c",
                """
from importlib import metadata
from measure_backend_probe import check_method_package_is_missing

metadata.distribution("measure-service")
check_method_package_is_missing()
""",
            ],
            root,
        )
        _run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(service_wheel)],
            root,
        )
        _run(
            [
                str(python),
                "-c",
                "from measure_backend_probe import check_method_package_is_missing; check_method_package_is_missing()",
            ],
            root,
        )
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                str(service_wheel),
            ],
            root,
        )
        _run(
            [
                str(python),
                "-c",
                """
from importlib import metadata
from measure_backend_probe import check_repaired_install

try:
    metadata.distribution("measure-methods")
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError("The standalone distribution should be removed")
check_repaired_install()
""",
            ],
            root,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service_wheel", type=Path)
    parser.add_argument(
        "--legacy-wheel",
        type=Path,
        help="use an existing historical measure-methods wheel instead of a local collision fixture",
    )
    arguments = parser.parse_args()
    service_wheel = arguments.service_wheel.resolve()
    if arguments.legacy_wheel is None:
        with tempfile.TemporaryDirectory(prefix="measure-methods-legacy-wheel-") as directory:
            legacy_wheel = _create_legacy_wheel(service_wheel, Path(directory))
            _check_upgrade(service_wheel, legacy_wheel)
    else:
        legacy_wheel = arguments.legacy_wheel.resolve()
        _validate_legacy_wheel(legacy_wheel)
        _check_upgrade(service_wheel, legacy_wheel)
    print("Historical method distribution upgrade sequence passed")


if __name__ == "__main__":
    main()
