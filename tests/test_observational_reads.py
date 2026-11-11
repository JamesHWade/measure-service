"""Read handlers leave an absent or empty store unchanged."""

import pytest

from measure_service import batch_exports, experiments, server


@pytest.mark.parametrize("existing_directory", [False, True])
@pytest.mark.parametrize(
    "read",
    [
        lambda: server.dataset_for("owner", "absent"),
        lambda: server.source_for("owner", "absent"),
        lambda: server.analysis_for("owner", "absent"),
        lambda: experiments.list_experiments("owner"),
        lambda: server._evidence_store_identity(),
        lambda: batch_exports._store_identity(),
    ],
)
def test_read_does_not_initialize_store(tmp_path, monkeypatch, existing_directory, read):
    root = tmp_path / "store"
    monkeypatch.setenv("MEASURE_MCP_DATA_DIR", str(root))
    if existing_directory:
        root.mkdir()
    with pytest.raises(ValueError):
        read()
    assert root.exists() is existing_directory
    assert not root.exists() or list(root.iterdir()) == []
