"""Local-alpha staging is offline, versioned and tied to exact source artifacts."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import venv
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_alpha_bundle.py"
spec = importlib.util.spec_from_file_location("build_alpha_bundle", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

MEASURE_COMMIT = "1" * 40
SEC_COMMIT = "2" * 40


def make_wheel(
    path: Path,
    name: str,
    version: str,
    *,
    files: dict[str, bytes] | None = None,
    requirements: tuple[str, ...] = (),
) -> Path:
    """Write a minimal valid pure-Python wheel for offline builder tests."""
    normalized = builder.canonical_name(name).replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    wheel_files = dict(files or {})
    wheel_files[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requirements)
        + "\n"
    ).encode()
    wheel_files[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: alpha-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode()
    record_path = f"{dist_info}/RECORD"
    record_rows = []
    for member, payload in wheel_files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        record_rows.append((member, f"sha256={digest}", str(len(payload))))
    record_rows.append((record_path, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(record_rows)
    wheel_files[record_path] = output.getvalue().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in sorted(wheel_files.items()):
            archive.writestr(member, payload)
    return path


def make_service_wheel(path: Path, marker: bytes = b"committed service source") -> Path:
    return make_wheel(
        path,
        "measure-service",
        "0.1.0.dev0",
        files={
            "measure_service/__init__.py": b"__version__ = '0.1.0.dev0'\n",
            "measure_service/source-marker.txt": marker,
            "measure_methods/__init__.py": b"VALUE = 7\n",
        },
        requirements=("demo-dep==1.0",),
    )


def make_source_repo(root: Path) -> tuple[Path, str]:
    source = root / "service-source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Alpha Test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "alpha@example.invalid"], check=True
    )
    (source / ".gitignore").write_text("ignored-source.py\n")
    (source / "tracked-source.txt").write_text("committed service source\n")
    subprocess.run(
        ["git", "-C", str(source), "add", ".gitignore", "tracked-source.txt"], check=True
    )
    subprocess.run(["git", "-C", str(source), "commit", "-m", "selected source"], check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    return source, revision


@pytest.mark.parametrize("value", [None, "main", "a" * 39, "g" * 40, "a" * 41])
def test_backend_commit_inputs_must_be_full_immutable_git_ids(value):
    with pytest.raises(ValueError, match="full 40-character"):
        builder.validate_backend_commits(value, SEC_COMMIT)
    with pytest.raises(ValueError, match="full 40-character"):
        builder.validate_backend_commits(MEASURE_COMMIT, value)


def test_source_archive_uses_only_selected_commit_and_ignores_live_edits(tmp_path):
    source, revision = make_source_repo(tmp_path)
    (source / "ignored-source.py").write_text("must not enter the archive\n")
    snapshot = builder.archive_source(source, revision, tmp_path / "snapshot")
    assert (snapshot / "tracked-source.txt").read_text() == "committed service source\n"
    assert not (snapshot / "ignored-source.py").exists()

    (source / "tracked-source.txt").write_text("live edit after source selection\n")
    assert (snapshot / "tracked-source.txt").read_text() == "committed service source\n"
    with pytest.raises(ValueError, match="clean Git checkout"):
        builder.source_commit(source)


def test_requirement_lock_requires_a_complete_exact_pin_set(tmp_path):
    requirements = tmp_path / "python.lock"
    requirements.write_text("demo_dep==1.0 --hash=sha256:" + "a" * 64 + "\n")
    assert builder.read_requirements(requirements) == [
        {
            "name": "demo_dep",
            "normalized_name": "demo-dep",
            "version": "1.0",
            "hashes": ["a" * 64],
        }
    ]
    requirements.write_text("demo-dep==1.0\nother==2.0 --hash=sha256:" + "b" * 64 + "\n")
    with pytest.raises(ValueError, match="every Python pin"):
        builder.read_requirements(requirements)
    requirements.write_text("demo-dep>=1.0\n")
    with pytest.raises(ValueError, match="exact name==version"):
        builder.read_requirements(requirements)


@pytest.fixture
def staged_bundle(tmp_path):
    requirements = tmp_path / "python.lock"
    requirements.write_text("demo-dep==1.0\n")
    pins = builder.read_requirements(requirements)
    dependency = make_wheel(
        tmp_path / "inputs" / "demo_dep-1.0-py3-none-any.whl", "demo-dep", "1.0"
    )
    service = make_service_wheel(
        tmp_path / "inputs" / "measure_service-0.1.0.dev0-py3-none-any.whl"
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    builder.stage_bundle(
        bundle,
        source_revision="3" * 40,
        measure_commit=MEASURE_COMMIT,
        sec_commit=SEC_COMMIT,
        requirements_file=requirements,
        pins=pins,
        service_wheel=service,
        dependency_wheels=[dependency],
    )
    return bundle


def test_manifest_rejects_changed_artifacts_and_semantic_lock_drift(staged_bundle):
    document = builder.verify_bundle(staged_bundle)
    dependency_entry = document["python"]["dependencies"][0]
    service_entry = document["service"]["wheel"]
    requirement_lines = [
        line
        for line in (staged_bundle / "requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert requirement_lines == [
        f"demo-dep==1.0 --hash=sha256:{dependency_entry['sha256']}",
        f"measure-service==0.1.0.dev0 --hash=sha256:{service_entry['sha256']}",
    ]
    dependency = next((staged_bundle / "wheels").glob("demo_dep-*.whl"))
    dependency.write_bytes(dependency.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        builder.verify_bundle(staged_bundle)

    dependency.write_bytes(dependency.read_bytes()[:-7])
    requirements = staged_bundle / "python-requirements.lock"
    requirements.write_text("demo-dep==2.0\n")
    manifest = json.loads((staged_bundle / "manifest.json").read_text())
    manifest["python"]["lock_sha256"] = builder.sha256_file(requirements)
    (staged_bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="lock does not match manifest dependency pins"):
        builder.verify_bundle(staged_bundle)


@pytest.mark.parametrize("mutation", ["remove", "change"])
def test_manifest_rejects_install_hash_drift_even_if_file_digest_is_updated(
    staged_bundle, mutation
):
    document = builder.verify_bundle(staged_bundle)
    requirements = staged_bundle / "requirements.txt"
    contents = requirements.read_text()
    dependency_hash = document["python"]["dependencies"][0]["sha256"]
    original = f" --hash=sha256:{dependency_hash}"
    replacement = "" if mutation == "remove" else " --hash=sha256:" + "0" * 64
    requirements.write_text(contents.replace(original, replacement, 1))
    document["python"]["install_requirements_sha256"] = builder.sha256_file(requirements)
    (staged_bundle / "manifest.json").write_text(json.dumps(document))
    with pytest.raises(
        ValueError, match="requirements.txt does not match the manifest package pins"
    ):
        builder.verify_bundle(staged_bundle)


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "taken"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep")
    requirements = tmp_path / "python.lock"
    requirements.write_text("demo-dep==1.0\n")
    with pytest.raises(FileExistsError, match="already exists"):
        builder.build_bundle(output, requirements, MEASURE_COMMIT, SEC_COMMIT, source=tmp_path)
    assert sentinel.read_text() == "keep"


def test_r_verifier_forces_the_selected_library_ahead_of_inherited_libraries(tmp_path, monkeypatch):
    library = tmp_path / "selected-R-library"
    library.mkdir()
    monkeypatch.setenv("R_LIBS", "/inherited/library")
    monkeypatch.setenv("R_LIBS_USER", "/inherited/user-library")
    captured = {}

    def fake_run(command, *, env, capture_output, text, check):
        captured["command"] = command
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, stdout="measure 1.0\n", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    report = builder.verify_r_backends(
        {
            "r_backends": {
                "measure": {"commit": MEASURE_COMMIT},
                "measure.sec": {"commit": SEC_COMMIT},
            }
        },
        library,
        "Rscript",
    )
    assert captured["env"]["R_LIBS"] == str(library.resolve())
    assert captured["env"]["R_LIBS_USER"] == str(library.resolve())
    assert captured["command"][:3] == ["Rscript", "--vanilla", "-e"]
    assert report == ["measure 1.0"]


def test_pip_environment_drops_inherited_redirects_and_uses_no_config(monkeypatch):
    monkeypatch.setenv("PIP_FIND_LINKS", "/external/wheels")
    monkeypatch.setenv("PIP_TARGET", "/external/target")
    monkeypatch.setenv("PIP_PREFIX", "/external/prefix")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/external/pip.conf")
    environment = builder._isolated_pip_environment()
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert "PIP_FIND_LINKS" not in environment
    assert "PIP_TARGET" not in environment
    assert "PIP_PREFIX" not in environment


def _venv_python(path: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(path)
    return path / "bin" / "python"


def test_offline_bundle_installs_exact_wheels_and_checks_package_bytes(tmp_path, monkeypatch):
    source, revision = make_source_repo(tmp_path)
    (source / "ignored-source.py").write_text("not in selected tree\n")
    requirements = tmp_path / "python.lock"
    requirements.write_text("demo-dep==1.0\n")
    source_wheelhouse = tmp_path / "source-wheelhouse"
    make_wheel(
        source_wheelhouse / "demo_dep-1.0-py3-none-any.whl",
        "demo-dep",
        "1.0",
        files={"demo_dep/__init__.py": b"ORIGIN = 'staged'\n"},
    )
    download_python = _venv_python(tmp_path / "download-python")
    monkeypatch.setattr(builder.sys, "executable", str(download_python))

    def build_from_archive(snapshot: Path, destination: Path) -> Path:
        assert (snapshot / "tracked-source.txt").read_text() == "committed service source\n"
        (source / "tracked-source.txt").write_text("changed during wheel build\n")
        return make_service_wheel(
            destination / "measure_service-0.1.0.dev0-py3-none-any.whl",
            (snapshot / "tracked-source.txt").read_bytes(),
        )

    monkeypatch.setattr(builder, "build_service_wheel", build_from_archive)
    bundle = builder.build_bundle(
        tmp_path / "offline-bundle",
        requirements,
        MEASURE_COMMIT,
        SEC_COMMIT,
        source=source,
        wheelhouse=source_wheelhouse,
    )
    document = builder.verify_bundle(bundle)
    assert document["service"]["source_commit"] == revision
    service_wheel = builder._safe_artifact_path(bundle, document["service"]["wheel"]["path"])
    with zipfile.ZipFile(service_wheel) as archive:
        assert archive.read("measure_service/source-marker.txt") == b"committed service source\n"
    assert len(list((bundle / "wheels").glob("*.whl"))) == 2

    external_wheelhouse = tmp_path / "external-wheelhouse"
    make_wheel(
        external_wheelhouse / "demo_dep-1.0-1-py3-none-any.whl",
        "demo-dep",
        "1.0",
        files={"demo_dep/__init__.py": b"ORIGIN = 'external'\n"},
    )
    pip_config = tmp_path / "external-pip.conf"
    pip_config.write_text(f"[global]\nfind-links = {external_wheelhouse}\n")

    install_python = _venv_python(tmp_path / "install-python")
    environment = dict(os.environ)
    environment["PIP_FIND_LINKS"] = str(external_wheelhouse)
    environment["PIP_CONFIG_FILE"] = str(pip_config)
    install = subprocess.run(
        [str(install_python), str(SCRIPT), "install", "--bundle", str(bundle)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert "measure_service/ and measure_methods/ files match" in install.stdout
    dependency_path = subprocess.check_output(
        [str(install_python), "-c", "import demo_dep; print(demo_dep.__file__)"], text=True
    ).strip()
    assert Path(dependency_path).read_bytes() == b"ORIGIN = 'staged'\n"
    assert all(
        "--hash=sha256:" in line
        for line in (bundle / "requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    )

    package_path = subprocess.check_output(
        [str(install_python), "-c", "import measure_service; print(measure_service.__file__)"],
        text=True,
    ).strip()
    Path(package_path).write_text("__version__ = '0.1.0.dev0'\n# edited in place\n")
    verify_code = (
        "import importlib.util, pathlib; "
        f"p={str(SCRIPT)!r}; "
        "s=importlib.util.spec_from_file_location('alpha', p); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"b=pathlib.Path({str(bundle)!r}); "
        "m.verify_installed_python(b, m.verify_bundle(b))"
    )
    checked = subprocess.run(
        [str(install_python), "-c", verify_code], capture_output=True, text=True, check=False
    )
    assert checked.returncode != 0
    assert "measure_service/__init__.py" in checked.stderr
