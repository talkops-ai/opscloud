"""Root pytest configuration — markers, environment sandboxing, and path-based marker injection."""

from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Automatically isolate environment variables and singletons for each test."""
    old_env = dict(os.environ)
    monkeypatch.delenv("OPSCLOUD_PROJECT_ROOT", raising=False)
    # Baseline mock keys for offline deterministic unit testing
    if not os.environ.get("GOOGLE_API_KEY"):
        monkeypatch.setenv("GOOGLE_API_KEY", "mock-test-key-google")
    if not os.environ.get("TYPESAFE_API_KEY") and not os.environ.get("JEV_API_KEY"):
        monkeypatch.setenv("TYPESAFE_API_KEY", "mock-test-key-typesafe")
    if "AWS_DEFAULT_REGION" not in os.environ:
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if "AWS_REGION" not in os.environ:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    yield
    os.environ.clear()
    os.environ.update(old_env)
    try:
        from opscloud.config import settings as _settings_mod

        with _settings_mod._settings_lock:
            _settings_mod._settings = None
            _settings_mod._bootstrap_state.done = False
    except Exception:
        pass
    try:
        from opscloud.model import pool as _pool_mod

        _pool_mod._GLOBAL_POOL_MANAGER = None
    except Exception:
        pass


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
