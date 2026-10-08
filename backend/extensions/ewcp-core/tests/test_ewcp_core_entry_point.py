"""Packaged entry-point check — meaningful only once the extension is
installed (`deerflow extensions install backend/extensions/ewcp-core`).
Skipped on a bare source checkout."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution

import pytest


def test_installed_distribution_exposes_deerflow_extension_entry_point() -> None:
    try:
        dist = distribution("ewcp-core")
    except PackageNotFoundError:
        pytest.skip("ewcp-core not installed in this environment")

    entry_points = [ep for ep in dist.entry_points if ep.group == "deerflow.extensions"]

    assert [(ep.name, ep.value) for ep in entry_points] == [("ewcp_core", "ewcp_core:install")]
    install = entry_points[0].load()
    assert install.__deerflow_api__ == "0.2.0"
    assert install.__deerflow_name__ == "ewcp_core"
