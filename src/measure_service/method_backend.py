"""Read the containing distribution's provenance without inferring a source revision."""

import json
import re
from importlib import metadata


def method_backend_info() -> dict:
    distribution = metadata.distribution("measure-service")
    revision = reference = None
    try:
        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (TypeError, ValueError):
        direct_url = {}
    vcs = direct_url.get("vcs_info", {}) if isinstance(direct_url, dict) else {}
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        candidate = vcs.get("commit_id")
        if isinstance(candidate, str) and re.fullmatch(
            r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", candidate
        ):
            revision = candidate.lower()
        requested = vcs.get("requested_revision")
        if isinstance(requested, str) and len(requested) <= 256:
            reference = requested
    return {
        "package": "measure-service",
        "version": distribution.version,
        "source_revision": revision,
        "source_ref": reference,
    }
