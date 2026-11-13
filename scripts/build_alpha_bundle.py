"""Build and install an offline, locally staged alpha bundle for measure-service."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
FORMAT = "measure-local-alpha-v1"
R_MINIMUM = "4.5.0"
DEFAULT_SOURCE_DATE_EPOCH = "315532800"
BUILD_TOOL_PINS = {"pip": "25.3", "build": "1.3.0", "setuptools": "80.9.0", "wheel": "0.45.1"}
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION = (
    r"(?:\d+!)?\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+[A-Za-z0-9.]+)?"
)
_REQUIREMENT = re.compile(
    rf"^(?P<name>{_NAME})==(?P<version>{_VERSION})(?P<hashes>(?:\s+--hash=sha256:[0-9a-fA-F]{{64}})*)$"
)
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_name(name: str) -> str:
    """Apply the distribution name normalization used by Python package indexes."""
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backend_commits(measure_commit: str, sec_commit: str) -> tuple[str, str]:
    """Require full GitHub Git commit IDs for both R backends."""
    if not isinstance(measure_commit, str) or not _GIT_SHA.fullmatch(measure_commit):
        raise ValueError("--measure-commit must be a full 40-character Git commit ID")
    if not isinstance(sec_commit, str) or not _GIT_SHA.fullmatch(sec_commit):
        raise ValueError("--measure-sec-commit must be a full 40-character Git commit ID")
    return measure_commit.lower(), sec_commit.lower()


def read_requirements(path: Path) -> list[dict[str, object]]:
    """Read a complete resolved lock containing only exact, unmarked pins."""
    pins: list[dict[str, object]] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        match = _REQUIREMENT.fullmatch(line)
        if not match:
            raise ValueError(
                f"{path.name}:{line_number} must be one exact name==version pin, "
                "optionally followed by SHA-256 hashes"
            )
        name = match.group("name")
        normalized = canonical_name(name)
        if normalized == "measure-service":
            raise ValueError(
                f"{path.name}:{line_number} pins measure-service; the bundle builds its wheel"
            )
        if normalized in seen:
            raise ValueError(f"{path.name}:{line_number} repeats package {name}")
        seen.add(normalized)
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})", match.group("hashes"))
        pins.append(
            {
                "name": name,
                "normalized_name": normalized,
                "version": match.group("version"),
                "hashes": [value.lower() for value in hashes],
            }
        )
    if not pins:
        raise ValueError(f"{path.name} lists no exact Python dependencies")
    has_hashes = [bool(pin["hashes"]) for pin in pins]
    if any(has_hashes) and not all(has_hashes):
        raise ValueError("either every Python pin must have SHA-256 hashes or none may have hashes")
    return sorted(pins, key=lambda pin: str(pin["normalized_name"]))


def wheel_metadata(path: Path) -> dict[str, str]:
    """Read Name and Version from a wheel's METADATA file."""
    import zipfile

    if path.suffix != ".whl" or path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular wheel file: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(f"{path.name} must contain one wheel METADATA file")
            text = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ValueError(f"cannot read wheel metadata from {path.name}: {error}") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("Name: "):
            fields["name"] = line[6:].strip()
        elif line.startswith("Version: "):
            fields["version"] = line[9:].strip()
        if "name" in fields and "version" in fields:
            break
    if not fields.get("name") or not fields.get("version"):
        raise ValueError(f"{path.name} has no Name and Version in wheel METADATA")
    fields["normalized_name"] = canonical_name(fields["name"])
    return fields


def source_commit(source: Path) -> str:
    """Return a commit only when the selected source checkout is clean."""
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"{source} is not a Git checkout: {result.stderr.strip()}")
    revision = result.stdout.strip()
    if not _GIT_SHA.fullmatch(revision):
        raise ValueError("selected service checkout did not report a full Git commit ID")
    status = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode:
        raise ValueError(f"could not inspect service source status: {status.stderr.strip()}")
    if status.stdout.strip():
        raise ValueError("selected service source must be a clean Git checkout")
    return revision.lower()


def archive_source(source: Path, revision: str, destination: Path) -> Path:
    """Materialize only the committed Git tree, rejecting unsafe archive members."""
    if not _GIT_SHA.fullmatch(revision):
        raise ValueError("service source archive needs a full Git commit ID")
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination.parent / (destination.name + ".tar")
    try:
        with archive_path.open("wb") as archive_file:
            result = subprocess.run(
                ["git", "-C", str(source), "archive", "--format=tar", revision],
                stdout=archive_file,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"could not archive service commit {revision}: {detail}")
        with tarfile.open(archive_path, mode="r:") as archive:
            seen: set[str] = set()
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or member.name in seen
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError(
                        f"unsafe or unsupported file in service Git archive: {member.name}"
                    )
                seen.add(member.name)
                target = destination.joinpath(*relative.parts)
                target.resolve().relative_to(destination.resolve())
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise ValueError(f"service Git archive member is unreadable: {member.name}")
                with source_file, target.open("xb") as output:
                    shutil.copyfileobj(source_file, output)
                target.chmod(member.mode & 0o777)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        archive_path.unlink(missing_ok=True)
    return destination


def python_runtime() -> dict[str, str]:
    return {
        "implementation": sys.implementation.name,
        "version": platform.python_version(),
        "platform": sysconfig.get_platform(),
    }


def build_service_wheel(source: Path, destination: Path) -> Path:
    """Build one wheel using the caller's reviewed build environment."""
    tools = _build_tool_versions()
    mismatches = {
        name: (tools.get(name), expected)
        for name, expected in BUILD_TOOL_PINS.items()
        if tools.get(name) != expected
    }
    if mismatches:
        raise RuntimeError(
            f"builder tool versions must match the documented pins; actual/expected: {mismatches}"
        )
    environment = dict(os.environ)
    environment.setdefault("SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(destination),
            str(source),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "service wheel build failed:\n" + result.stdout[-2000:] + result.stderr[-2000:]
        )
    wheels = sorted(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one service wheel, found {[wheel.name for wheel in wheels]}")
    identity = wheel_metadata(wheels[0])
    if identity["normalized_name"] != "measure-service":
        raise RuntimeError(f"built wheel is {identity['name']}, expected measure-service")
    return wheels[0]


def download_dependencies(
    requirements: Path,
    destination: Path,
    pins: list[dict[str, object]],
    *,
    wheelhouse: Path | None = None,
) -> list[Path]:
    """Resolve a platform-specific exact lock to wheels, optionally from offline files."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--dest",
        str(destination),
    ]
    if wheelhouse is not None:
        if not wheelhouse.is_dir():
            raise ValueError(f"offline wheelhouse is not a directory: {wheelhouse}")
        command.extend(["--no-index", "--find-links", str(wheelhouse.resolve())])
    if pins and pins[0]["hashes"]:
        command.append("--require-hashes")
    command.extend(["--requirement", str(requirements.resolve())])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        mode = "offline " if wheelhouse is not None else ""
        raise RuntimeError(
            f"{mode}Python dependency staging failed:\n"
            + result.stdout[-2000:]
            + result.stderr[-2000:]
        )
    wheels = sorted(destination.glob("*.whl"))
    actual: dict[str, str] = {}
    for wheel in wheels:
        identity = wheel_metadata(wheel)
        name = identity["normalized_name"]
        if name in actual:
            raise ValueError(f"Python staging produced more than one wheel for {name}")
        actual[name] = identity["version"]
    expected = {str(pin["normalized_name"]): str(pin["version"]) for pin in pins}
    if actual != expected:
        raise ValueError(
            "resolved wheel set must exactly match every lock pin (including transitive "
            f"dependencies); expected {expected}, received {actual}"
        )
    return wheels


def _artifact_entry(path: Path, bundle: Path, kind: str) -> dict[str, str]:
    identity = wheel_metadata(path)
    relative = path.relative_to(bundle).as_posix()
    return {
        "name": identity["name"],
        "normalized_name": identity["normalized_name"],
        "version": identity["version"],
        "path": relative,
        "sha256": sha256_file(path),
        "kind": kind,
    }


def _build_tool_versions() -> dict[str, str]:
    try:
        build_version = importlib.metadata.version("build")
    except importlib.metadata.PackageNotFoundError:
        build_version = "not-installed"
    versions = {"build": build_version}
    for name in ("pip", "setuptools", "wheel"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def stage_bundle(
    bundle: Path,
    *,
    source_revision: str,
    measure_commit: str,
    sec_commit: str,
    requirements_file: Path,
    pins: list[dict[str, object]],
    service_wheel: Path,
    dependency_wheels: list[Path],
) -> None:
    """Write the manifest and install metadata into a newly reserved directory."""
    measure_commit, sec_commit = validate_backend_commits(measure_commit, sec_commit)
    if not _GIT_SHA.fullmatch(source_revision):
        raise ValueError("service source revision must be a full 40-character Git commit ID")
    service_identity = wheel_metadata(service_wheel)
    if service_identity["normalized_name"] != "measure-service":
        raise ValueError("service wheel must contain the measure-service distribution")
    if len(dependency_wheels) != len(pins):
        raise ValueError("dependency wheel count does not match the resolved Python lock")

    wheels_dir = bundle / "wheels"
    wheels_dir.mkdir(exist_ok=True)
    service_target = wheels_dir / service_wheel.name
    shutil.copy2(service_wheel, service_target)
    dependency_entries: list[dict[str, str]] = []
    for wheel in dependency_wheels:
        target = wheels_dir / wheel.name
        if target.exists() or target.is_symlink():
            raise ValueError(f"duplicate staged wheel filename: {wheel.name}")
        shutil.copy2(wheel, target)
        dependency_entries.append(_artifact_entry(target, bundle, "dependency"))
    service_entry = _artifact_entry(service_target, bundle, "service")
    by_name = {entry["normalized_name"]: entry["version"] for entry in dependency_entries}
    expected = {str(pin["normalized_name"]): str(pin["version"]) for pin in pins}
    if by_name != expected:
        raise ValueError("staged dependency wheel metadata does not match the supplied lock")

    lock_target = bundle / "python-requirements.lock"
    shutil.copy2(requirements_file, lock_target)
    requirement_lines = [
        f"{entry['normalized_name']}=={entry['version']} --hash=sha256:{entry['sha256']}"
        for entry in sorted(dependency_entries, key=lambda item: item["normalized_name"])
    ]
    requirement_lines.append(
        f"measure-service=={service_identity['version']} --hash=sha256:{service_entry['sha256']}"
    )
    install_requirements = bundle / "requirements.txt"
    install_requirements.write_text(
        "# Generated by scripts/build_alpha_bundle.py; verify the bundle before installing.\n"
        + "\n".join(requirement_lines)
        + "\n",
        encoding="utf-8",
    )
    document = {
        "format": FORMAT,
        "service": {
            "distribution": service_identity["name"],
            "version": service_identity["version"],
            "source_commit": source_revision.lower(),
            "wheel": service_entry,
        },
        "python": {
            **python_runtime(),
            "lock_sha256": sha256_file(lock_target),
            "install_requirements_sha256": sha256_file(install_requirements),
            "dependencies": sorted(dependency_entries, key=lambda item: item["normalized_name"]),
        },
        "r_backends": {
            "minimum_version": R_MINIMUM,
            "measure": {"repository": "JamesHWade/measure", "commit": measure_commit},
            "measure.sec": {"repository": "JamesHWade/measure.sec", "commit": sec_commit},
        },
        "build_environment": {"python": platform.python_version(), "tools": _build_tool_versions()},
    }
    (bundle / "manifest.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _safe_artifact_path(bundle: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith("wheels/"):
        raise ValueError("manifest wheel path must be under wheels/")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
        raise ValueError("manifest contains an unsafe wheel path")
    target = bundle / path
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"manifest wheel is missing or is not a regular file: {relative}")
    return target


def verify_bundle(bundle: Path) -> dict[str, object]:
    """Validate bundle structure, lock files and all staged wheel hashes."""
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("local-alpha manifest is missing or is not a regular file")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read local-alpha manifest: {error}") from error
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError("unsupported or malformed local-alpha manifest")
    service = document.get("service")
    python = document.get("python")
    r_backends = document.get("r_backends")
    if (
        not isinstance(service, dict)
        or not isinstance(python, dict)
        or not isinstance(r_backends, dict)
    ):
        raise ValueError("local-alpha manifest is missing service, python or R backend metadata")
    lock_path = bundle / "python-requirements.lock"
    install_path = bundle / "requirements.txt"
    if (
        lock_path.is_symlink()
        or not lock_path.is_file()
        or sha256_file(lock_path) != python.get("lock_sha256")
    ):
        raise ValueError("Python requirements lock is missing or its SHA-256 does not match")
    if (
        install_path.is_symlink()
        or not install_path.is_file()
        or sha256_file(install_path) != python.get("install_requirements_sha256")
    ):
        raise ValueError("install requirements are missing or their SHA-256 does not match")
    wheels_dir = bundle / "wheels"
    if wheels_dir.is_symlink() or not wheels_dir.is_dir():
        raise ValueError("bundle wheelhouse is missing or is not a directory")
    artifacts: list[dict[str, object]] = []
    service_wheel = service.get("wheel")
    if not isinstance(service_wheel, dict):
        raise ValueError("manifest has no service wheel entry")
    artifacts.append(service_wheel)
    dependencies = python.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("manifest Python dependencies must be a list")
    artifacts.extend(dependencies)
    seen_paths: set[str] = set()
    seen_packages: set[str] = set()
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ValueError("manifest wheel entry is malformed")
        relative = entry.get("path")
        digest = entry.get("sha256")
        name = entry.get("normalized_name")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("manifest wheel SHA-256 is malformed")
        if not isinstance(name, str) or name in seen_packages:
            raise ValueError("manifest repeats or omits a wheel distribution name")
        if not isinstance(relative, str) or relative in seen_paths:
            raise ValueError("manifest repeats or omits a wheel path")
        seen_paths.add(relative)
        seen_packages.add(name)
        path = _safe_artifact_path(bundle, relative)
        if sha256_file(path) != digest:
            raise ValueError(f"wheel SHA-256 mismatch: {relative}")
        identity = wheel_metadata(path)
        if identity["normalized_name"] != name or identity["version"] != entry.get("version"):
            raise ValueError(f"wheel metadata does not match the manifest: {relative}")
    if service_wheel.get("kind") != "service":
        raise ValueError("manifest service wheel has an invalid artifact kind")
    if any(entry.get("kind") != "dependency" for entry in dependencies):
        raise ValueError("manifest dependency wheel has an invalid artifact kind")
    wheel_files = list(wheels_dir.glob("*.whl"))
    if any(path.is_symlink() or not path.is_file() for path in wheel_files):
        raise ValueError("bundle wheelhouse contains a symlink or non-file wheel entry")
    actual_wheels = {path.relative_to(bundle).as_posix() for path in wheel_files}
    if actual_wheels != seen_paths:
        raise ValueError("bundle wheelhouse has missing or unmanifested wheel files")
    try:
        locked_pins = read_requirements(lock_path)
    except (OSError, ValueError) as error:
        raise ValueError(f"Python requirements lock is malformed: {error}") from error
    locked_versions = {str(pin["normalized_name"]): str(pin["version"]) for pin in locked_pins}
    manifest_versions = {
        str(entry.get("normalized_name")): str(entry.get("version")) for entry in dependencies
    }
    if locked_versions != manifest_versions:
        raise ValueError("Python requirements lock does not match manifest dependency pins")
    service_identity = wheel_metadata(_safe_artifact_path(bundle, str(service_wheel["path"])))
    if service_identity["normalized_name"] != "measure-service":
        raise ValueError("manifest service wheel is not measure-service")
    if (
        service.get("distribution") != service_identity["name"]
        or service.get("version") != service_identity["version"]
    ):
        raise ValueError("manifest service identity does not match its wheel")
    source_revision = service.get("source_commit")
    if not isinstance(source_revision, str) or not _GIT_SHA.fullmatch(source_revision):
        raise ValueError("manifest service source is not a full commit ID")
    backend_commits = validate_backend_commits(
        r_backends.get("measure", {}).get("commit")
        if isinstance(r_backends.get("measure"), dict)
        else None,
        r_backends.get("measure.sec", {}).get("commit")
        if isinstance(r_backends.get("measure.sec"), dict)
        else None,
    )
    if r_backends.get("minimum_version") != R_MINIMUM:
        raise ValueError("manifest must require R 4.5.0 or newer")
    expected_pins = [
        f"{entry['normalized_name']}=={entry['version']} --hash=sha256:{entry['sha256']}"
        for entry in sorted(dependencies, key=lambda item: str(item.get("normalized_name")))
    ]
    expected_pins.append(
        f"measure-service=={service_identity['version']} --hash=sha256:{service_wheel['sha256']}"
    )
    actual_requirements = [
        line.strip()
        for line in install_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if actual_requirements != expected_pins:
        raise ValueError("requirements.txt does not match the manifest package pins")
    runtime = python_runtime()
    for key in ("implementation", "version", "platform"):
        if not isinstance(python.get(key), str) or not python[key]:
            raise ValueError(f"manifest Python {key} is missing")
    if not isinstance(python.get("lock_sha256"), str) or not isinstance(
        python.get("install_requirements_sha256"), str
    ):
        raise ValueError("manifest requirements hashes are missing")
    # Return validated metadata; runtime comparison is left to install/verify so
    # that a bundle can be inspected from a different build host.
    _ = backend_commits, runtime
    return document


def verify_python_runtime(document: dict[str, object]) -> None:
    python = document["python"]
    assert isinstance(python, dict)
    actual = python_runtime()
    for key in ("implementation", "version", "platform"):
        if actual[key] != python[key]:
            raise ValueError(
                f"Python {key} mismatch: bundle requires {python[key]}, current interpreter is {actual[key]}"
            )


def _verify_installed_service_files(bundle: Path, document: dict[str, object]) -> None:
    """Compare installed import packages with the exact files in the service wheel."""
    import zipfile

    service = document["service"]
    assert isinstance(service, dict)
    wheel = _safe_artifact_path(bundle, str(service["wheel"]["path"]))
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith(("measure_service/", "measure_methods/")) and not name.endswith("/")
        }
    if not any(name.startswith("measure_service/") for name in expected):
        raise ValueError("service wheel contains no measure_service package files")
    if not any(name.startswith("measure_methods/") for name in expected):
        raise ValueError("service wheel contains no measure_methods package files")
    try:
        distribution = importlib.metadata.distribution("measure-service")
    except importlib.metadata.PackageNotFoundError as error:
        raise ValueError("Python distribution measure-service is not installed") from error
    if distribution.files is None:
        raise ValueError("installed measure-service distribution has no file inventory")
    actual: dict[str, bytes] = {}
    for item in distribution.files:
        relative = item.as_posix()
        if not relative.startswith(("measure_service/", "measure_methods/")):
            continue
        if "/__pycache__/" in f"/{relative}" or relative.endswith((".pyc", ".pyo")):
            continue
        path = distribution.locate_file(item)
        if not path.is_file():
            raise ValueError(f"installed service package file is missing: {relative}")
        actual[relative] = path.read_bytes()
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise ValueError(
            "installed measure-service package files do not match its wheel "
            f"(missing={missing}, unexpected={unexpected})"
        )
    for name, content in expected.items():
        if actual[name] != content:
            raise ValueError(f"installed service package file differs from its wheel: {name}")


def verify_installed_python(bundle: Path, document: dict[str, object]) -> list[str]:
    """Verify every installed distribution's exact version from the manifest."""
    service = document["service"]
    python = document["python"]
    assert isinstance(service, dict) and isinstance(python, dict)
    expected = [(str(service["distribution"]), str(service["version"]))]
    dependencies = python["dependencies"]
    assert isinstance(dependencies, list)
    expected.extend((str(entry["name"]), str(entry["version"])) for entry in dependencies)
    report = []
    for name, version in expected:
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise ValueError(f"Python distribution {name} is not installed") from error
        if actual != version:
            raise ValueError(f"Python distribution {name} is {actual}, expected {version}")
        report.append(f"{name} {actual}")
    _verify_installed_service_files(bundle, document)
    report.append("measure_service/ and measure_methods/ files match the staged service wheel")
    return report


def install_python_bundle(bundle: Path) -> list[str]:
    """Install only verified wheelhouse files into the current Python environment."""
    document = verify_bundle(bundle)
    verify_python_runtime(document)
    command = [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--force-reinstall",
        "--no-index",
        "--require-hashes",
        "--find-links",
        str((bundle / "wheels").resolve()),
        "--requirement",
        str((bundle / "requirements.txt").resolve()),
    ]
    result = subprocess.run(
        command,
        env=_isolated_pip_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "offline Python installation failed:\n" + result.stdout[-2000:] + result.stderr[-2000:]
        )
    check = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "check"],
        env=_isolated_pip_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode:
        raise RuntimeError(
            "installed Python dependency check failed:\n" + check.stdout + check.stderr
        )
    return verify_installed_python(bundle, document)


def _isolated_pip_environment() -> dict[str, str]:
    """Remove inherited pip redirects while disabling pip configuration files."""
    environment = dict(os.environ)
    for key in list(environment):
        if key.upper().startswith("PIP_"):
            del environment[key]
    # pip honors this before reading global/site configuration, including with
    # --isolated. Keep it as the only pip-specific environment variable.
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


_R_VERIFY_EXPRESSION = r"""
minimum <- "4.5.0"
if (getRversion() < numeric_version(minimum)) stop(paste("R", minimum, "or newer is required"))
lib <- normalizePath(Sys.getenv("MEASURE_ALPHA_R_LIBRARY"), mustWork = TRUE)
visible <- normalizePath(.libPaths(), mustWork = TRUE)
if (length(visible) == 0 || visible[[1]] != lib) {
  stop("explicit R library is not first in .libPaths()")
}
expected <- list(measure = Sys.getenv("MEASURE_ALPHA_MEASURE_COMMIT"),
                 `measure.sec` = Sys.getenv("MEASURE_ALPHA_SEC_COMMIT"))
for (package in names(expected)) {
  resolved <- normalizePath(find.package(package, quiet = TRUE), mustWork = TRUE)
  if (!startsWith(resolved, paste0(lib, .Platform$file.sep))) {
    stop(paste(package, "resolves outside the selected R library:", resolved))
  }
  namespace <- loadNamespace(package)
  namespace_path <- normalizePath(getNamespaceInfo(namespace, "path"), mustWork = TRUE)
  if (namespace_path != resolved) stop(paste(package, "namespace path differs from find.package"))
  description <- packageDescription(package, lib.loc = lib)
  if (is.null(description)) stop(paste(package, "is not installed in", lib))
  actual <- description$RemoteSha
  if (is.null(actual) || !identical(tolower(actual), tolower(expected[[package]]))) {
    stop(paste(package, "RemoteSha does not match the selected full commit"))
  }
  cat(package, description$Version, actual, "\n")
}
cat("R", as.character(getRversion()), "\n")
"""


def verify_r_backends(document: dict[str, object], library: Path, rscript: str) -> list[str]:
    """Check R version, explicit library visibility and exact installed RemoteSha values."""
    if not library.is_dir():
        raise ValueError(f"explicit R library does not exist: {library}")
    backends = document["r_backends"]
    assert isinstance(backends, dict)
    measure = backends["measure"]
    sec = backends["measure.sec"]
    assert isinstance(measure, dict) and isinstance(sec, dict)
    environment = dict(os.environ)
    environment["R_LIBS_USER"] = str(library.resolve())
    environment["R_LIBS"] = str(library.resolve())
    environment["MEASURE_ALPHA_R_LIBRARY"] = str(library.resolve())
    environment["MEASURE_ALPHA_MEASURE_COMMIT"] = str(measure["commit"])
    environment["MEASURE_ALPHA_SEC_COMMIT"] = str(sec["commit"])
    result = subprocess.run(
        [rscript, "--vanilla", "-e", _R_VERIFY_EXPRESSION],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("R backend verification failed:\n" + result.stdout + result.stderr)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def build_bundle(
    output: Path,
    requirements_file: Path,
    measure_commit: str,
    sec_commit: str,
    *,
    source: Path = REPO_ROOT,
    wheelhouse: Path | None = None,
) -> Path:
    """Build a service wheel and stage a closed Python wheelhouse without overwriting."""
    measure_commit, sec_commit = validate_backend_commits(measure_commit, sec_commit)
    source = source.resolve()
    output = output.expanduser().absolute()
    if not source.is_dir():
        raise ValueError(f"service source is not a directory: {source}")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists; choose a new output directory")
    if output == source or source in output.parents:
        raise ValueError("bundle output must be outside the selected service source checkout")
    pins = read_requirements(requirements_file)
    revision = source_commit(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="measure-alpha-") as scratch_name:
            scratch = Path(scratch_name)
            immutable_source = archive_source(source, revision, scratch / "source")
            service_dir = scratch / "service"
            service_dir.mkdir()
            service_wheel = build_service_wheel(immutable_source, service_dir)
            dependency_dir = scratch / "dependencies"
            dependency_dir.mkdir()
            dependencies = download_dependencies(
                requirements_file,
                dependency_dir,
                pins,
                wheelhouse=wheelhouse.resolve() if wheelhouse else None,
            )
            stage_bundle(
                output,
                source_revision=revision,
                measure_commit=measure_commit,
                sec_commit=sec_commit,
                requirements_file=requirements_file,
                pins=pins,
                service_wheel=service_wheel,
                dependency_wheels=dependencies,
            )
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build an offline local-alpha bundle")
    build.add_argument("--source", type=Path, default=REPO_ROOT, help="clean service Git checkout")
    build.add_argument(
        "--requirements", required=True, type=Path, help="reviewed exact Python lock"
    )
    build.add_argument(
        "--measure-commit", required=True, help="full 40-character measure Git commit"
    )
    build.add_argument(
        "--measure-sec-commit", required=True, help="full 40-character measure.sec Git commit"
    )
    build.add_argument(
        "--output", required=True, type=Path, help="new bundle directory outside source"
    )
    build.add_argument(
        "--wheelhouse",
        type=Path,
        help="local wheel directory; when set, dependency staging is offline",
    )

    install = subparsers.add_parser(
        "install", help="install verified wheels into the current Python"
    )
    install.add_argument("--bundle", required=True, type=Path)

    verify = subparsers.add_parser("verify", help="verify installed Python and R package versions")
    verify.add_argument("--bundle", required=True, type=Path)
    verify.add_argument("--r-library", required=True, type=Path, help="explicit R package library")
    verify.add_argument("--rscript", default=shutil.which("Rscript") or "Rscript")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(
                args.output,
                args.requirements,
                args.measure_commit,
                args.measure_sec_commit,
                source=args.source,
                wheelhouse=args.wheelhouse,
            )
            print(result)
        elif args.command == "install":
            for line in install_python_bundle(args.bundle.resolve()):
                print(line)
        else:
            document = verify_bundle(args.bundle.resolve())
            verify_python_runtime(document)
            for line in verify_installed_python(args.bundle.resolve(), document):
                print(line)
            for line in verify_r_backends(document, args.r_library.resolve(), args.rscript):
                print(line)
    except (OSError, ValueError, RuntimeError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
