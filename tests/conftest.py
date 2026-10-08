"""Root pytest configuration — markers, environment sandboxing, and path-based marker injection."""

from __future__ import annotations

import os
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """Automatically isolate environment variables, config files, and singletons for each test."""
    old_env = dict(os.environ)
    monkeypatch.delenv("OPSCLOUD_PROJECT_ROOT", raising=False)

    # Sandbox configuration file and state directory in isolated temp location
    # (avoid placing .opscloud in tmp_path directly to prevent project root marker collisions)
    import tempfile
    test_temp_obj = tempfile.TemporaryDirectory(prefix="opscloud_test_data_")
    test_data_dir = Path(test_temp_obj.name) / ".opscloud"
    test_data_dir.mkdir(parents=True, exist_ok=True)
    test_config_path = test_data_dir / "config.toml"
    test_state_dir = test_data_dir / ".state"
    test_state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("opscloud.config.paths.DATA_DIR", test_data_dir)
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config_path)
    monkeypatch.setattr("opscloud.config.paths.CONFIG_FILE_PATH", test_config_path)
    monkeypatch.setattr("opscloud.config.paths.STATE_DIR", test_state_dir)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config_path)
    monkeypatch.setattr("opscloud.config.toml_config.STATE_DIR", test_state_dir)
    monkeypatch.setattr("opscloud.config.toml_config._RECENT_MODELS_FILE", test_state_dir / "recent_models.json")
    monkeypatch.setattr("opscloud.config.toml_config._RECENT_PROFILES_FILE", test_state_dir / "recent_cloud_profiles.json")

    # Baseline mock keys for offline deterministic unit testing
    if not os.environ.get("GOOGLE_API_KEY"):
        monkeypatch.setenv("GOOGLE_API_KEY", "mock-test-key-google")
    if not os.environ.get("TYPESAFE_API_KEY") and not os.environ.get("JEV_API_KEY"):
        monkeypatch.setenv("TYPESAFE_API_KEY", "mock-test-key-typesafe")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-test-key-anthropic")
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "mock-test-key-openai")
    if "AWS_DEFAULT_REGION" not in os.environ:
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if "AWS_REGION" not in os.environ:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    yield
    os.environ.clear()
    os.environ.update(old_env)
    test_temp_obj.cleanup()
    try:
        from opscloud.config.settings import reset_settings_for_testing

        reset_settings_for_testing()
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
