"""Retain the calculation backend with immutable method evidence."""

import json
from pathlib import Path

from measure_service import method_registry, methods


def declaration():
    return methods.MethodDeclaration.model_validate(
        {
            "schema_version": "1.0",
            "time_unit": "min",
            "flow_unit": "mL/min",
            "solvents": ["A"],
            "program": {
                "kind": "isocratic",
                "acquisition_duration": 2,
                "state": {"flow": 1, "solvent_fractions": {"A": 1}},
            },
        }
    )


def test_backend_evidence_stays_with_the_original_method_and_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    first_backend = {
        "package": "measure-methods",
        "version": "0.1.0.dev0",
        "source_revision": "1" * 40,
        "source_ref": "initial",
    }
    next_backend = {
        "package": "measure-service",
        "version": "0.1.0.dev1",
        "source_revision": "2" * 40,
        "source_ref": "co-located-methods",
    }
    monkeypatch.setattr(methods, "method_backend_info", lambda: first_backend)
    first = method_registry.register_method("owner", "Method", declaration(), None, "first")
    monkeypatch.setattr(methods, "method_backend_info", lambda: next_backend)
    retried = method_registry.register_method("owner", "Method", declaration(), None, "first")
    second = method_registry.register_method("owner", "Method", declaration(), None, "second")

    assert retried == first
    assert method_registry.get_method("owner", first["method_id"]) == first
    assert first["evaluation"]["calculation_backend"] == first_backend
    assert second["evaluation"]["calculation_backend"] == next_backend
    assert first["canonical_program_sha256"] == second["canonical_program_sha256"]


def test_older_evaluations_do_not_acquire_current_backend_evidence():
    old = methods.evaluate_method(declaration()).model_dump(mode="json")
    old.pop("calculation_backend")
    retained = methods.MethodEvaluation.model_validate(old)

    assert retained.calculation_backend is None


def test_new_evaluations_name_the_containing_service_distribution():
    evaluation = methods.evaluate_method(declaration())

    assert evaluation.calculation_backend.package == "measure-service"


def test_legacy_backend_without_package_keeps_its_historical_default():
    old = methods.evaluate_method(declaration()).model_dump(mode="json")
    old["calculation_backend"].pop("package")
    retained = methods.MethodEvaluation.model_validate(old)

    assert retained.calculation_backend.package == "measure-methods"
    assert (
        methods.MethodCalculationBackend.model_json_schema()["properties"]["package"]["default"]
        == "measure-service"
    )


def test_installed_backend_evidence_does_not_emit_private_install_paths(monkeypatch):
    from measure_service import method_backend

    class InstalledDistribution:
        version = "0.1.0.dev0"

        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(
                {"url": "file:///private/local/backend", "dir_info": {"editable": True}}
            )

    monkeypatch.setattr(method_backend.metadata, "distribution", lambda _: InstalledDistribution())
    evidence = method_backend.method_backend_info()

    assert evidence["package"] == "measure-service"
    assert evidence["version"] == "0.1.0.dev0"
    assert evidence["source_revision"] is None
    assert evidence["source_ref"] is None
    assert "private" not in json.dumps(evidence)


def test_backend_provenance_uses_the_containing_distribution_pep610_metadata(monkeypatch):
    from measure_service import method_backend

    revision = "a" * 40

    class InstalledDistribution:
        version = "0.1.2"

        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(
                {
                    "url": "https://example.invalid/private-source",
                    "vcs_info": {
                        "vcs": "git",
                        "requested_revision": "release-candidate",
                        "commit_id": revision,
                    },
                }
            )

    def distribution(name):
        assert name == "measure-service"
        return InstalledDistribution()

    monkeypatch.setattr(method_backend.metadata, "distribution", distribution)
    evidence = method_backend.method_backend_info()

    assert evidence == {
        "package": "measure-service",
        "version": "0.1.2",
        "source_revision": revision,
        "source_ref": "release-candidate",
    }


def test_existing_ordinary_method_keeps_its_entire_canonical_program_and_digest(
    tmp_path, monkeypatch
):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "method-constant-overhead-canonical.json").read_text()
    )
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(tmp_path))
    spec = methods.MethodDeclaration.model_validate(fixture["declaration"])
    record = method_registry.register_method("owner", "Method", spec, None, "original-math")

    assert record["evaluation"]["canonical"] == fixture["canonical"]
    assert record["canonical_program_sha256"] == fixture["canonical_program_sha256"]
