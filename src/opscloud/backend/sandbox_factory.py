# pyright: reportMissingImports=false
"""Factory for creating sandbox backends (LangSmith, Daytona, Modal, Runloop).

Note: Sandbox providers (LangSmith, Daytona, Modal, Runloop, Vercel) are optional
dependencies and are imported lazily inside their respective creation functions
to avoid hard dependency requirements at startup.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shlex
import string
import threading
from typing import Any

from opscloud.backend.local import LocalShellBackend
from opscloud.backend.sandbox_config import SandboxConfig
from opscloud.config.paths import find_project_root
from opscloud.utils.logger import AgentLogger

logger = AgentLogger("SandboxFactory")

# Thread-safe global cache for active sandbox backends, keyed by thread_id
_active_sandboxes: dict[str, Any] = {}
_active_sandboxes_lock = threading.Lock()

# Registry of active context managers or providers to delete at exit
_active_providers: dict[str, Any] = {}

# Default working directories inside the sandbox containers
WORKING_DIRS = {
    "langsmith": "/root",
    "modal": "/workspace",
    "daytona": "/home/daytona",
    "runloop": "/home/user",
    "vercel": "/vercel/sandbox",
    "agentcore": "/workspace",
}

# Directories to exclude from remote sandbox file synchronization
WORKSPACE_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".gemini",
        ".agents",
        ".opscloud",
        "tests",
        "scratch",
        ".terraform",
        ".terragrunt-cache",
        "cdk.out",
        "node_modules",
        "dist",
        "build",
    }
)

WORKSPACE_IGNORE_EXTS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".bak",
        ".tmp",
    }
)


def get_sandbox_working_dir(provider: str) -> str:
    """Return default working directory for a given provider."""
    return WORKING_DIRS.get(provider, "/workspace")


def get_project_root() -> Path:
    """Return the resolved project root or current working directory."""
    root = find_project_root()
    if root is not None:
        return root
    try:
        from opscloud.project_utils import get_server_project_context

        ctx = get_server_project_context()
        if ctx is not None:
            return ctx.user_cwd
    except Exception:
        pass
    return Path.cwd().resolve()


def _sync_workspace_to_sandbox(backend: Any, provider: str) -> None:
    """Upload project workspace files to the remote sandbox container."""
    root = get_project_root()
    working_dir = get_sandbox_working_dir(provider)
    logger.info(f"Syncing workspace {root} to sandbox {working_dir}...")

    files_to_upload: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if d not in WORKSPACE_IGNORE_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in WORKSPACE_IGNORE_EXTS:
                continue

            local_path = Path(dirpath) / fname
            try:
                # Calculate relative path and target absolute path in the sandbox
                rel_path = local_path.relative_to(root)
                remote_path = f"{working_dir}/{rel_path}"

                # Check file size (skip large files > 10MB)
                file_size = local_path.stat().st_size
                if file_size > 10 * 1024 * 1024:
                    logger.debug(f"Skipping large file during sync: {rel_path} ({file_size} bytes)")
                    continue

                content = local_path.read_bytes()
                files_to_upload.append((remote_path, content))
            except Exception as e:
                logger.warning(f"Failed to prepare file {local_path} for upload: {e}")

    # Upload files in batches to prevent hitting request payload or token limits
    batch_size = 20
    logger.info(f"Uploading {len(files_to_upload)} files to sandbox...")
    for i in range(0, len(files_to_upload), batch_size):
        batch = files_to_upload[i : i + batch_size]
        try:
            responses = backend.upload_files(batch)
            for resp in responses:
                if getattr(resp, "error", None):
                    logger.warning(f"Failed to upload {resp.path}: {resp.error}")
        except Exception as e:
            logger.error(f"Error uploading batch {i // batch_size + 1}: {e}")

    logger.info("Workspace synchronization completed.")


def _run_sandbox_setup(backend: Any, setup_script_path: str) -> None:
    """Run user's setup script inside the sandbox after expanding environment variables."""
    script_path = Path(setup_script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"Setup script not found at {setup_script_path}")

    logger.info(f"Executing setup script: {setup_script_path}...")
    script_content = script_path.read_text(encoding="utf-8")

    # Substitute environment variables
    template = string.Template(script_content)
    expanded_script = template.safe_substitute(os.environ)

    # Run in sandbox
    result = backend.execute(f"bash -c {shlex.quote(expanded_script)}")
    if result.exit_code != 0:
        logger.error(f"Setup script failed with exit code {result.exit_code}:\n{result.output}")
        raise RuntimeError(f"Sandbox setup script execution failed: exit {result.exit_code}")

    logger.info("Sandbox setup complete.")


def _create_langsmith_sandbox(config: SandboxConfig) -> Any:
    """Create a LangSmith sandbox backend."""
    try:
        from deepagents.backends.langsmith import (
            LangSmithSandbox,  # Lazy import: optional dependency
        )
        from langsmith.sandbox import SandboxClient  # Lazy import: optional dependency
    except ImportError as e:
        raise ImportError(
            "LangSmith sandbox dependencies not installed. Install them to use LangSmith sandboxes."
        ) from e

    api_key = (
        os.getenv("OPSCLOUD_LANGSMITH_API_KEY")
        or os.getenv("LANGSMITH_SANDBOX_API_KEY")
        or os.getenv("LANGSMITH_API_KEY")
        or os.getenv("LANGCHAIN_API_KEY")
    )
    if not api_key:
        raise ValueError("No LangSmith API key found. Set LANGSMITH_API_KEY.")

    client = SandboxClient(api_key=api_key)

    if config.sandbox_id:
        logger.info(f"Connecting to existing LangSmith sandbox ID: {config.sandbox_id}")
        sandbox = client.get_sandbox(name=config.sandbox_id)
        return LangSmithSandbox(sandbox)

    snapshot_name = config.snapshot_name or "opscloud-aws-agent"
    image = config.image or "python:3.12-slim"

    logger.info(f"Ensuring LangSmith snapshot ready: {snapshot_name} (image: {image})")
    snapshots = client.list_snapshots()
    snapshot_id = None
    for snap in snapshots:
        if snap.name == snapshot_name and snap.status == "ready":
            snapshot_id = snap.id
            break

    if not snapshot_id:
        logger.info(f"Building new LangSmith snapshot: {snapshot_name}")
        snapshot = client.create_snapshot(
            name=snapshot_name,
            docker_image=image,
            fs_capacity_bytes=16 * 1024**3,
        )
        snapshot_id = snapshot.id

    logger.info(f"Creating fresh LangSmith sandbox from snapshot {snapshot_name}...")
    sandbox = client.create_sandbox(snapshot_id=snapshot_id)
    return LangSmithSandbox(sandbox)


def _import_provider_module(
    module_name: str,
    *,
    provider: str,
    package: str,
) -> Any:
    """Import an optional provider module with a clear install command.

    Args:
        module_name: Python module name to import.
        provider: Sandbox provider name (e.g. 'daytona').
        package: PyPI package name to install if missing.

    Returns:
        The imported module object.

    Raises:
        ImportError: If the optional dependency is not installed.
    """
    import importlib

    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"The '{provider}' sandbox provider requires the '{package}' package. "
            f"Install it with: uv pip install {package}"
        )
        raise ImportError(msg) from exc


def _create_other_sandbox(config: SandboxConfig) -> Any:
    """Dynamic fallback provider loader for Daytona, Modal, Runloop, Vercel."""
    provider = config.provider
    if provider == "daytona":
        daytona = _import_provider_module("daytona", provider="daytona", package="daytona")
        langchain_daytona = _import_provider_module("langchain_daytona", provider="daytona", package="langchain-daytona")
        DaytonaSandbox = getattr(langchain_daytona, "DaytonaSandbox")

        api_key = os.getenv("DAYTONA_API_KEY")
        if not api_key:
            raise ValueError("No Daytona API key found. Set DAYTONA_API_KEY.")
        client = daytona.Daytona(
            daytona.DaytonaConfig(
                api_key=api_key,
                api_url=os.getenv("DAYTONA_API_URL"),
            )
        )
        sandbox = client.create()
        return DaytonaSandbox(sandbox=sandbox)

    elif provider == "modal":
        modal = _import_provider_module("modal", provider="modal", package="modal")
        langchain_modal = _import_provider_module("langchain_modal", provider="modal", package="langchain-modal")
        ModalSandbox = getattr(langchain_modal, "ModalSandbox")

        app = modal.App.lookup(name="opscloud-sandbox", create_if_missing=True)
        if config.sandbox_id:
            sandbox = modal.Sandbox.from_id(sandbox_id=config.sandbox_id)
        else:
            sandbox = modal.Sandbox.create(app=app, workdir="/workspace")
        return ModalSandbox(sandbox=sandbox)

    elif provider == "runloop":
        langchain_runloop = _import_provider_module("langchain_runloop", provider="runloop", package="langchain-runloop")
        RunloopProvider = getattr(langchain_runloop, "RunloopProvider")

        api_key = os.getenv("RUNLOOP_API_KEY")
        if not api_key:
            raise ValueError("No Runloop API key found. Set RUNLOOP_API_KEY.")
        prov = RunloopProvider(api_key=api_key)
        return prov.get_or_create(sandbox_id=config.sandbox_id, blueprint=config.snapshot_name)

    elif provider == "agentcore":
        from opscloud.config.aws import get_boto3_session

        agentcore_module = _import_provider_module(
            "bedrock_agentcore.tools.code_interpreter_client",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )
        agentcore_backend = _import_provider_module(
            "langchain_agentcore_codeinterpreter",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )
        sess = get_boto3_session()
        region = sess.region_name or "us-west-2"
        CodeInterpreter = getattr(agentcore_module, "CodeInterpreter")
        AgentCoreSandbox = getattr(agentcore_backend, "AgentCoreSandbox")
        interpreter = CodeInterpreter(region=region, session=sess)
        interpreter.start()
        return AgentCoreSandbox(interpreter=interpreter)

    else:
        raise NotImplementedError(f"Unsupported sandbox provider: {provider}")


def get_sandbox_backend(thread_id: str | None = None) -> Any:
    """Resolve, create, or fetch the cached sandbox backend for the given session thread."""
    config = SandboxConfig.from_env()

    # If provider is local, bypass the cache and return a local shell backend immediately
    if config.provider == "local":
        root = get_project_root()
        return LocalShellBackend(
            root_dir=root,
            virtual_mode=False,
            inherit_env=True,
        )

    # Use thread_id or fallback to a global key for execution loops without a session graph context
    cache_key = thread_id or "__global__"

    with _active_sandboxes_lock:
        if cache_key in _active_sandboxes:
            return _active_sandboxes[cache_key]

        logger.info(f"Initializing new sandbox backend for key {cache_key} (provider: {config.provider})...")

        backend = (
            _create_langsmith_sandbox(config)
            if config.provider == "langsmith"
            else _create_other_sandbox(config)
        )

        # Synchronize workspace if enabled
        if config.sync_workspace:
            try:
                _sync_workspace_to_sandbox(backend, config.provider)
            except Exception as e:
                logger.error(f"Workspace sync to sandbox failed: {e}")

        # Run setup script if specified
        if config.setup_script_path:
            try:
                _run_sandbox_setup(backend, config.setup_script_path)
            except Exception as e:
                logger.error(f"Sandbox setup script failed: {e}")

        # Cache the initialized backend
        _active_sandboxes[cache_key] = backend
        _active_providers[cache_key] = (config.provider, getattr(backend, "id", str(cache_key)))

        logger.info(f"Sandbox backend successfully cached. Key: {cache_key}")
        return backend


def cleanup_sandbox(thread_id: str) -> None:
    """Terminate the sandbox backend associated with a thread ID."""
    with _active_sandboxes_lock:
        backend = _active_sandboxes.pop(thread_id, None)
        provider_info = _active_providers.pop(thread_id, None)

        if backend and provider_info:
            provider, sandbox_id = provider_info
            logger.info(f"Terminating sandbox {sandbox_id} ({provider}) for thread {thread_id}...")
            try:
                if provider == "langsmith":
                    from langsmith.sandbox import SandboxClient

                    client = SandboxClient()
                    client.delete_sandbox(sandbox_id)
                elif provider == "daytona":
                    daytona = _import_provider_module("daytona", provider="daytona", package="daytona")
                    client = daytona.Daytona()
                    sandbox = client.get(sandbox_id)
                    client.delete(sandbox)
                elif provider == "modal":
                    modal = _import_provider_module("modal", provider="modal", package="modal")
                    sandbox = modal.Sandbox.from_id(sandbox_id=sandbox_id)
                    sandbox.terminate()
                elif provider == "runloop":
                    langchain_runloop = _import_provider_module("langchain_runloop", provider="runloop", package="langchain-runloop")
                    RunloopProvider = getattr(langchain_runloop, "RunloopProvider")
                    api_key = os.getenv("RUNLOOP_API_KEY")
                    if not api_key:
                        logger.warning("No Runloop API key found during cleanup. Runloop sandbox cannot be deleted.")
                    else:
                        prov = RunloopProvider(api_key=api_key)
                        prov.delete(sandbox_id=sandbox_id)
                logger.info(f"Sandbox {sandbox_id} terminated.")
            except Exception as e:
                logger.warning(f"Error during sandbox termination for {sandbox_id}: {e}")


def cleanup_all_sandboxes() -> None:
    """Cleanup exit handler to shut down all active sandboxes on process shutdown."""
    keys = list(_active_sandboxes.keys())
    if keys:
        logger.info("Process exit detected. Cleaning up all active sandboxes...")
        for key in keys:
            cleanup_sandbox(key)


# Register exit handler
atexit.register(cleanup_all_sandboxes)

create_sandbox_backend = get_sandbox_backend
clear_sandbox_cache = cleanup_all_sandboxes

__all__ = [
    "WORKSPACE_IGNORE_DIRS",
    "WORKSPACE_IGNORE_EXTS",
    "clear_sandbox_cache",
    "cleanup_all_sandboxes",
    "cleanup_sandbox",
    "create_sandbox_backend",
    "get_project_root",
    "get_sandbox_backend",
    "get_sandbox_working_dir",
]
