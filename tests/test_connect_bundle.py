"""The Connect bundle builder stages a deployable, self-describing directory."""

import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_connect_bundle.py"
spec = importlib.util.spec_from_file_location("build_connect_bundle", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def r_manifest(tmp_path):
    path = tmp_path / "r-manifest.json"
    packages = {"measure": {"Source": "CRAN", "description": {"Package": "measure"}}}
    path.write_text(json.dumps({"platform": "4.5.2", "packages": packages, "files": {}}))
    return path


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / "measure_service-0.0.0-py3-none-any.whl"
    path.write_bytes(b"synthetic wheel")
    return path


def test_bundle_carries_the_wheel_pins_entry_point_and_r_inventory(tmp_path, r_manifest, wheel):
    archive = builder.build_bundle(
        tmp_path / "bundle",
        ["mcp==1.30.0"],
        "3.12.10",
        r_manifest=r_manifest,
        r_requires=">=4.5.0, <5.0",
        wheel=wheel,
    )

    with tarfile.open(archive) as tar:
        members = set(tar.getnames())
        manifest = json.load(tar.extractfile("manifest.json"))
        requirements = tar.extractfile("requirements.txt").read().decode().splitlines()
        app = tar.extractfile("app.py").read().decode()
    wheel_line = requirements[-1]
    assert wheel_line.startswith("./wheels/") and wheel_line.endswith(wheel.name)
    assert wheel_line[2:] in members
    assert requirements[1] == "mcp==1.30.0"
    assert manifest["metadata"] == {"appmode": "python-fastapi", "entrypoint": "app:app"}
    assert manifest["python"]["version"] == "3.12.10"
    assert manifest["environment"] == {
        "python": {"requires": "~=3.12.0"},
        "r": {"requires": ">=4.5.0,<5.0"},
    }
    assert manifest["platform"] == "4.5.2" and list(manifest["packages"]) == ["measure"]
    assert "files" not in manifest
    assert "apply_connect_defaults()" in app
    compile(app, "app.py", "exec")


def test_changed_service_code_changes_the_requirements_text(tmp_path, wheel):
    builder.build_bundle(tmp_path / "one", ["mcp==1.30.0"], "3.12.10", wheel=wheel)
    wheel.write_bytes(b"another synthetic wheel")
    builder.build_bundle(tmp_path / "two", ["mcp==1.30.0"], "3.12.10", wheel=wheel)

    one = (tmp_path / "one" / "requirements.txt").read_text()
    two = (tmp_path / "two" / "requirements.txt").read_text()
    assert one != two
    assert "environment" in json.loads((tmp_path / "one" / "manifest.json").read_text())
    assert "platform" not in json.loads((tmp_path / "one" / "manifest.json").read_text())


@pytest.mark.parametrize(
    "line",
    [
        "mcp>=1.30",
        "mcp",
        "-r other.txt",
        "measure-service==0.1.0",
        "measure_service==0.1.0",
        "mcp==1.30.*",
        "mcp==1.30.0,<2",
        "mcp===1.30.0",
        "mcp==*",
    ],
)
def test_only_exact_third_party_pins_are_accepted(tmp_path, line):
    pins = tmp_path / "pins.txt"
    pins.write_text(f"# resolved for the server\nhttpx==0.28.1\n{line}\n")
    with pytest.raises(ValueError, match="pins.txt:3"):
        builder.read_pins(pins)


def test_pins_keep_markers_and_drop_comments(tmp_path):
    pins = tmp_path / "pins.txt"
    pins.write_text(
        "\nhttpx==0.28.1  # via mcp\n"
        'colorama==0.4.6 ; sys_platform == "win32"\n'
        "example-package[extra]==1!2.0rc1.post3+local.1\n"
    )
    assert builder.read_pins(pins) == [
        "httpx==0.28.1",
        'colorama==0.4.6 ; sys_platform == "win32"',
        "example-package[extra]==1!2.0rc1.post3+local.1",
    ]


@pytest.mark.parametrize("constraint", ["4.5", ">=4", "latest", ">=4.5.0,", ">= 4.5 || <5"])
def test_malformed_r_constraints_are_refused(constraint):
    with pytest.raises(ValueError, match="--r-requires"):
        builder.r_constraint(constraint)


def test_an_r_constraint_needs_an_r_inventory(tmp_path, wheel):
    with pytest.raises(ValueError, match="--r-manifest"):
        builder.build_bundle(
            tmp_path / "bundle", ["mcp==1.30.0"], "3.12.10", r_requires=">=4.5.0", wheel=wheel
        )
    assert not (tmp_path / "bundle").exists()


def test_an_incomplete_r_manifest_or_existing_output_is_refused(tmp_path, wheel):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"platform": "4.5.2", "packages": {}}))
    with pytest.raises(ValueError, match="no packages"):
        builder.r_inventory(empty)
    (tmp_path / "taken").mkdir()
    with pytest.raises(FileExistsError):
        builder.build_bundle(tmp_path / "taken", ["mcp==1.30.0"], "3.12.10", wheel=wheel)
    assert (
        builder.main(
            [
                "--pins",
                str(tmp_path / "absent.txt"),
                "--python-version",
                "3.12.10",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
