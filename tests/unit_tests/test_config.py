"""Comprehensive unit tests for OpsCloud Config Module.

Tests:
1. Paths & directory helpers (AWS/IaC root detection)
2. Settings dataclass, model property, and type-coercion mutators
3. AWS environment variable synchronization (sync_aws_env_aliases)
4. Canonical manifest registry, OptionKind coercion, and resolution
5. ConfigStore and SQLite adapter (DB -> env -> TOML -> default chain)
6. AWS context and identity discovery
7. LangSmith telemetry synchronization
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from opscloud.config.aws import (
    AWSIdentity,
    get_aws_credentials_dict,
    get_boto3_session,
    resolve_aws_identity,
)
from opscloud.config.manifest import (
    OptionKind,
    coerce_str_value,
    get_config_options,
    get_option,
    get_option_by_db_key,
    iter_groups,
    resolve_scalar,
    serialize_typed_value,
)
from opscloud.config.paths import (
    AWS_PRESERVE_ENV_VARS,
    CONFIG_PATH,
    DATA_DIR,
    ENV_PREFIX,
    GLOBAL_ENV_PATH,
    OPSCLOUD_HOME,
    SESSIONS_DB_PATH,
    STATE_DIR,
    agent_dir,
    find_project_root,
    user_agent_md,
    user_agents_dir,
    user_skills_dir,
)
from opscloud.config.settings import (
    Settings,
    get_glyphs,
    is_ascii_mode,
    newline_shortcut,
    parse_shell_allow_list,
    resolve_env_var,
    sync_aws_env_aliases,
)
from opscloud.config.store import ConfigCategory, ConfigStore
from opscloud.config.adapters.sqlite import SqliteConfigAdapter
from opscloud.config.langsmith import apply_tracing_settings, get_langsmith_project_name


# ── 1. Paths & Root Detection Tests ───────────────────────


def test_paths_constants():
    assert ENV_PREFIX == "OPSCLOUD_"
    assert OPSCLOUD_HOME == DATA_DIR
    assert DATA_DIR.name == ".opscloud"
    assert STATE_DIR == DATA_DIR / ".state"
    assert CONFIG_PATH == DATA_DIR / "config.toml"
    assert GLOBAL_ENV_PATH == DATA_DIR / ".env"
    assert SESSIONS_DB_PATH == STATE_DIR / "sessions.db"


def test_aws_preserve_env_vars():
    assert "AWS_REGION" in AWS_PRESERVE_ENV_VARS
    assert "AWS_DEFAULT_REGION" in AWS_PRESERVE_ENV_VARS
    assert "AWS_PROFILE" in AWS_PRESERVE_ENV_VARS
    assert "AWS_DEFAULT_PROFILE" in AWS_PRESERVE_ENV_VARS
    assert "AWS_ACCESS_KEY_ID" in AWS_PRESERVE_ENV_VARS
    assert "AWS_SECRET_ACCESS_KEY" in AWS_PRESERVE_ENV_VARS
    assert "AWS_SESSION_TOKEN" in AWS_PRESERVE_ENV_VARS
    assert "KUBECONFIG" in AWS_PRESERVE_ENV_VARS


def test_directory_helpers(tmp_path: Path):
    # Custom assistant
    skills_dir = user_skills_dir("aws-ops")
    assert skills_dir.name == "skills"
    agents_dir = user_agents_dir("aws-ops")
    assert agents_dir.name == "agents"
    agent_md = user_agent_md("aws-ops")
    assert agent_md.name == "AGENTS.md"

    # Default assistant ('opscloud')
    from opscloud.config.paths import DATA_DIR
    assert user_skills_dir().resolve() == (DATA_DIR / "skills").resolve()
    assert user_agents_dir().resolve() == (DATA_DIR / "agents").resolve()
    assert user_agent_md().resolve() == (DATA_DIR / "AGENTS.md").resolve()
    assert agent_dir().resolve() == DATA_DIR.resolve()


def test_find_project_root_cdk(tmp_path: Path):
    project_dir = tmp_path / "cdk-infra"
    project_dir.mkdir()
    (project_dir / "cdk.json").write_text('{"app": "npx ts-node bin/infra.ts"}')

    sub_dir = project_dir / "lib" / "constructs"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == project_dir


def test_find_project_root_terraform(tmp_path: Path):
    project_dir = tmp_path / "tf-infra"
    project_dir.mkdir()
    (project_dir / "main.tf").write_text('terraform { required_version = ">= 1.5.0" }')

    sub_dir = project_dir / "modules" / "vpc"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == project_dir


def test_find_project_root_sam(tmp_path: Path):
    project_dir = tmp_path / "sam-app"
    project_dir.mkdir()
    (project_dir / "template.yaml").write_text("AWSTemplateFormatVersion: '2010-09-09'")

    sub_dir = project_dir / "src" / "handlers"
    sub_dir.mkdir(parents=True)

    found = find_project_root(sub_dir)
    assert found == project_dir


# ── 2. Settings Dataclass & Mutation Tests ─────────────────


def test_settings_defaults():
    s = Settings()
    assert s.assistant_id == "opscloud"
    assert s.aws_region == "us-east-1"
    assert s.aws_profile == "default"
    assert s.model_name is None
    assert s.model is None
    assert s.approval_mode == "manual"
    assert s.sandbox_provider == "local"
    assert s.checkpoint_backend == "sqlite"
    assert s.theme == "dark"


def test_settings_model_property_alias():
    s = Settings()
    s.model = "anthropic:claude-3-5-sonnet"
    assert s.model_name == "anthropic:claude-3-5-sonnet"
    assert s.model == "anthropic:claude-3-5-sonnet"


def test_settings_set_and_reset_field(tmp_path: Path):
    s = Settings()

    # String field
    ok, msg = s.set_field("aws_region", "eu-central-1")
    assert ok is True
    assert s.aws_region == "eu-central-1"

    # Boolean field
    ok, msg = s.set_field("verbose_output", "true")
    assert ok is True
    assert s.verbose_output is True

    # Integer field
    ok, msg = s.set_field("model_context_limit", "64000")
    assert ok is True
    assert s.model_context_limit == 64000

    # Path field
    ok, msg = s.set_field("project_root", str(tmp_path))
    assert ok is True
    assert s.project_root == tmp_path.resolve()

    # List field
    ok, msg = s.set_field("shell_allow_list", "aws sts get-caller-identity, terraform plan")
    assert ok is True
    assert s.shell_allow_list == ["aws sts get-caller-identity", "terraform plan"]

    # Invalid field name
    ok, msg = s.set_field("non_existent_key", "val")
    assert ok is False

    # Reset field
    ok, msg = s.reset_field("aws_region")
    assert ok is True
    assert s.aws_region == "us-east-1"


def test_settings_to_display_dict():
    s = Settings(model_name="openai:gpt-4o")
    disp = s.to_display_dict()
    assert "aws_region" in disp
    assert "model_name" in disp
    assert "assistant_id" in disp
    assert disp["aws_region"] == "us-east-1"
    assert disp["model_name"] == "openai:gpt-4o"


def test_parse_shell_allow_list():
    assert parse_shell_allow_list(None) is None
    assert parse_shell_allow_list("") == []
    assert parse_shell_allow_list("  ") == []
    assert parse_shell_allow_list("ls, cat, aws s3 ls") == ["ls", "cat", "aws s3 ls"]
    assert parse_shell_allow_list(["git status", "pwd"]) == ["git status", "pwd"]


# ── 3. AWS Environment Sync Tests ─────────────────────────


def test_sync_aws_env_aliases(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    # Test region sync
    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    sync_aws_env_aliases()
    assert os.environ.get("AWS_DEFAULT_REGION") == "ap-southeast-1"

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    # Test profile sync
    monkeypatch.setenv("AWS_PROFILE", "staging")
    sync_aws_env_aliases()
    assert os.environ.get("AWS_DEFAULT_PROFILE") == "staging"

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    # Test AWS_PAGER is set to empty string
    assert os.environ.get("AWS_PAGER") == ""


def test_resolve_env_var_priority(monkeypatch):
    monkeypatch.setenv("OPSCLOUD_TEST_KEY", "prefixed_val")
    monkeypatch.setenv("TEST_KEY", "plain_val")
    assert resolve_env_var("TEST_KEY") == "prefixed_val"

    monkeypatch.delenv("OPSCLOUD_TEST_KEY", raising=False)
    assert resolve_env_var("TEST_KEY") == "plain_val"


# ── 4. Manifest Registry & Coercion Tests ─────────────────


def test_manifest_options_and_groups():
    options = get_config_options()
    assert len(options) > 10

    groups = iter_groups()
    assert "AWS" in groups
    assert "Credentials" in groups
    assert "Models" in groups
    assert "Security" in groups

    # Verify AWS options exist
    aws_region_opt = get_option("aws.region")
    assert aws_region_opt is not None
    assert aws_region_opt.group == "AWS"
    assert aws_region_opt.default == "us-east-1"
    assert aws_region_opt.db_key == "AWS_REGION"

    aws_prof_opt = get_option_by_db_key("AWS_PROFILE")
    assert aws_prof_opt is not None
    assert aws_prof_opt.key == "aws.profile"


def test_coercion_and_serialization(tmp_path: Path):
    # Bool
    assert coerce_str_value(OptionKind.BOOL, "true") is True
    assert coerce_str_value(OptionKind.BOOL, "0") is False
    assert serialize_typed_value(OptionKind.BOOL, True) == "true"
    assert serialize_typed_value(OptionKind.BOOL, False) == "false"

    # Int & Float
    assert coerce_str_value(OptionKind.INT, "100") == 100
    assert coerce_str_value(OptionKind.FLOAT, "3.14") == 3.14

    # Shell list
    cmds = coerce_str_value(OptionKind.SHELL_LIST, "aws ec2 describe-instances, kubectl get pods")
    assert cmds == ["aws ec2 describe-instances", "kubectl get pods"]
    assert serialize_typed_value(OptionKind.SHELL_LIST, cmds) == "aws ec2 describe-instances,kubectl get pods"

    # JSON
    data = coerce_str_value(OptionKind.JSON, '{"region": "us-east-1", "enabled": true}')
    assert data == {"region": "us-east-1", "enabled": True}
    assert serialize_typed_value(OptionKind.JSON, {"a": 1}) == '{"a": 1}'


def test_resolve_scalar_precedence(monkeypatch):
    opt = get_option("aws.region")
    assert opt is not None

    # Default fallback
    monkeypatch.delenv("OPSCLOUD_AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    val, source = resolve_scalar(opt)
    assert val == "us-east-1"
    assert source == "default"

    # Env override
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    val, source = resolve_scalar(opt)
    assert val == "us-west-2"
    assert "env" in source

    # Settings object override
    mock_settings = Settings(aws_region="eu-west-1")
    val, source = resolve_scalar(opt, settings=mock_settings)
    assert val == "eu-west-1"
    assert source == "settings"


# ── 5. ConfigStore & SQLite Adapter Tests ─────────────────


@pytest.mark.asyncio
async def test_sqlite_config_store(tmp_path: Path):
    db_file = tmp_path / "test_config.db"
    adapter = SqliteConfigAdapter(db_file)
    store = ConfigStore(adapter)
    await store.initialize()

    # Raw entry set and get
    await store.set(key="AWS_REGION", value="sa-east-1", category=ConfigCategory.AWS)
    val = await store.get("AWS_REGION")
    assert val == "sa-east-1"

    # Manifest-typed resolution
    opt = get_option("aws.region")
    assert opt is not None
    res_val, source = await store.resolve(opt)
    assert res_val == "sa-east-1"
    assert source == "db"

    # Typed set and get
    await store.set_typed(opt, "ca-central-1")
    assert await store.get_typed(opt) == "ca-central-1"

    # Settings hydration from store
    s = await Settings.from_store(store)
    assert s.aws_region == "ca-central-1"


@pytest.mark.asyncio
async def test_settings_sync_to_store(tmp_path: Path):
    db_file = tmp_path / "sync_config.db"
    adapter = SqliteConfigAdapter(db_file)
    store = ConfigStore(adapter)
    await store.initialize()

    s = Settings(aws_region="ap-northeast-1", approval_mode="auto")
    written = await s.sync_to_store(store)
    assert written > 0

    assert await store.get("AWS_REGION") == "ap-northeast-1"
    assert await store.get("APPROVAL_MODE") == "auto"


# ── 6. AWS Context & Identity Tests ───────────────────────


def test_aws_identity_dataclass():
    ident = AWSIdentity(
        account_id="123456789012",
        region="us-west-2",
        profile="prod",
        is_authenticated=True,
    )
    assert ident.account_id == "123456789012"
    assert ident.region == "us-west-2"
    assert ident.profile == "prod"
    assert ident.authenticated is True


def test_get_boto3_session():
    session = get_boto3_session(region="us-west-2")
    assert session is not None
    assert session.region_name == "us-west-2"


def test_resolve_aws_identity_safe():
    # Calling in mock/local environment safely returns AWSIdentity without crashing
    ident = resolve_aws_identity(region="us-east-1")
    assert isinstance(ident, AWSIdentity)
    assert ident.region == "us-east-1"


def test_get_aws_credentials_dict():
    creds = get_aws_credentials_dict()
    assert isinstance(creds, dict)


# ── 7. LangSmith Tracing Synchronization Tests ─────────────


def test_langsmith_project_name(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    assert get_langsmith_project_name() is None

    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OPSCLOUD_LANGSMITH_PROJECT", "opscloud-prod")
    assert get_langsmith_project_name() == "opscloud-prod"


def test_apply_tracing_settings(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    s = Settings(
        langchain_api_key="lsv2_pt_test",
        langchain_tracing=True,
        langchain_project="opscloud-tracing-test",
    )
    applied = apply_tracing_settings(s)
    assert applied is True
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
    assert os.environ.get("LANGSMITH_TRACING") == "true"
    assert os.environ.get("LANGCHAIN_PROJECT") == "opscloud-tracing-test"


# ── 8. UI Glyphs & Charset Mode Tests ─────────────────────


def test_ui_glyphs():
    glyphs = get_glyphs()
    assert glyphs is not None
    assert glyphs.checkmark in ("✓", "[OK]")
    assert isinstance(is_ascii_mode(), bool)
    assert isinstance(newline_shortcut(), str)


# ── 9. Project Context & Workspace Working Directory Tests ─


def test_project_cwd_not_in_persistent_manifest():
    """Verify project.cwd is NOT in the persistent config manifest to prevent SQLite CWD pollution."""
    opt = get_option("project.cwd")
    assert opt is None
    # project.root remains a configurable project option
    root_opt = get_option("project.root")
    assert root_opt is not None
    assert root_opt.kind == OptionKind.PATH


def test_settings_cwd_and_server_context(monkeypatch, tmp_path: Path):
    user_dir = tmp_path / "workspace_test"
    user_dir.mkdir()

    from opscloud.project_utils import ProjectContext
    from opscloud.server import SERVER_ENV_PREFIX, ServerConfig

    monkeypatch.setenv(f"{SERVER_ENV_PREFIX}CWD", str(user_dir))
    monkeypatch.setenv(f"{SERVER_ENV_PREFIX}PROJECT_ROOT", str(user_dir))

    s = Settings.from_env()
    assert s.cwd == user_dir
    assert s.effective_cwd == user_dir
    assert s.project_root == user_dir
    assert s.effective_project_root == user_dir

    # Test ServerConfig.from_cli_args
    p_ctx = ProjectContext.from_user_cwd(user_dir)
    sc = ServerConfig.from_cli_args(project_context=p_ctx, model="anthropic:claude-3-5-sonnet-latest")
    assert sc.cwd == str(user_dir)
    assert sc.to_env()[f"{SERVER_ENV_PREFIX}CWD"] == str(user_dir)


def test_find_project_root_excludes_home(monkeypatch, tmp_path: Path):
    home_dir = tmp_path / "fake_home"
    home_dir.mkdir()
    (home_dir / "package.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    # Looking from fake_home directly should skip fake_home
    assert find_project_root(home_dir) is None


def test_resolve_model_context_limit():
    from opscloud.model.config import resolve_model_context_limit

    # 1. Known model in MODEL_PROFILES / langchain
    claude_limit = resolve_model_context_limit("anthropic:claude-3-5-sonnet-20241022")
    assert claude_limit == 200_000

    gpt_limit = resolve_model_context_limit("openai:gpt-4o")
    assert gpt_limit == 128_000

    # 2. genai-prices fallback for model not in local MODEL_PROFILES
    deepseek_limit = resolve_model_context_limit("deepseek:deepseek-chat")
    assert deepseek_limit == 64_000

    # 3. None falls back to settings
    assert resolve_model_context_limit(None) is not None
