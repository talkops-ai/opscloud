"""Root pytest configuration — markers, environment sandboxing, and path-based marker injection."""

from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Automatically isolate environment variables for each test."""
    old_env = dict(os.environ)
    monkeypatch.delenv("OPSCLOUD_PROJECT_ROOT", raising=False)
    yield
    os.environ.clear()
    os.environ.update(old_env)


def pytest_collection_modifyitems(config, items):
    """Automatically tag tests with markers based on their location."""
    for item in items:
        path_str = str(item.fspath)
        if "/unit_tests/" in path_str:
            item.add_marker(pytest.mark.unit)
        elif "/integration_tests/" in path_str:
            item.add_marker(pytest.mark.integration)
        elif "/evals/" in path_str:
            item.add_marker(pytest.mark.eval)
        else:
            item.add_marker(pytest.mark.unit)
