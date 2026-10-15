"""Multi-tier custom command discovery, colon-namespacing, conditional path activation,
usage priority scoring, and content-hash hot-reload.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from opscloud.commands.custom.frontmatter import parse_custom_command_frontmatter
from opscloud.commands.custom.models import CustomCommandDefinition, CustomCommandSource
from opscloud.config.paths import (
    COMMAND_USAGE_PATH,
    DEFAULT_AGENT_NAME,
    ensure_state_dir,
    find_project_root,
    get_project_hierarchy_command_dirs,
    get_user_claude_commands_dir,
    get_user_commands_dir,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

MAX_COMMAND_FILE_BYTES = 256 * 1024  # 256 KB safety cap

# Module-level caches & conditional command store
_DISCOVERED_CACHE: dict[str, tuple[list[CustomCommandDefinition], list[CustomCommandDefinition]]] = {}
_ACTIVATED_CONDITIONAL_NAMES: set[str] = set()


def _is_skill_md_name(name: str) -> bool:
    """Return True if filename is `SKILL.md` (case-insensitive)."""
    return name.lower() == "skill.md"


def _file_identity_key(file_path: Path) -> str:
    """Return canonical inode (`dev:ino`) or resolved path key for deduplication."""
    try:
        resolved = file_path.resolve()
        st = resolved.stat()
        if st.st_ino and st.st_dev:
            return f"{st.st_dev}:{st.st_ino}"
        return str(resolved)
    except OSError:
        return str(file_path)


def derive_command_name_from_path(
    file_path: Path,
    base_dir: Path,
    *,
    namespace_prefix: str | None = None,
) -> str:
    """Derive colon-namespaced command name from `file_path` relative to `base_dir`.

    Examples:
        - `commands/review.md` -> `review`
        - `commands/frontend/component.md` -> `frontend:component`
        - `commands/devops/k8s/SKILL.md` -> `devops:k8s`
        - With `namespace_prefix="my-plugin"`: `my-plugin:frontend:component`
    """
    is_skill = _is_skill_md_name(file_path.name)
    if is_skill:
        skill_folder = file_path.parent
        try:
            rel_folder = skill_folder.relative_to(base_dir)
        except ValueError:
            rel_folder = Path(skill_folder.name)
        rel_str = "" if str(rel_folder) in (".", "") else str(rel_folder).replace("\\", "/")
        base_name = rel_str.replace("/", ":") if rel_str else skill_folder.name
    else:
        try:
            rel_file = file_path.relative_to(base_dir)
        except ValueError:
            rel_file = Path(file_path.name)
        rel_dir = rel_file.parent
        stem = file_path.stem
        dir_str = "" if str(rel_dir) in (".", "") else str(rel_dir).replace("\\", "/").replace("/", ":")
        base_name = f"{dir_str}:{stem}" if dir_str else stem

    if namespace_prefix:
        clean_prefix = namespace_prefix.strip(":")
        if clean_prefix and not base_name.startswith(f"{clean_prefix}:"):
            return f"{clean_prefix}:{base_name}"
    return base_name


def _scan_markdown_files_in_dir(base_dir: Path, max_depth: int = 8) -> list[Path]:
    """Recursively scan `base_dir` for `.md` files.

    If a directory contains `SKILL.md`, only `SKILL.md` is taken from that
    directory and sibling `.md` files in that directory are skipped.
    """
    results: list[Path] = []
    if not base_dir.exists() or not base_dir.is_dir():
        return results

    def _walk(current_dir: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return

        md_files: list[Path] = []
        subdirs: list[Path] = []
        skill_md_file: Path | None = None

        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:
                continue
            if is_dir:
                subdirs.append(entry)
            elif is_file and entry.suffix.lower() == ".md":
                if _is_skill_md_name(entry.name) and skill_md_file is None:
                    skill_md_file = entry
                md_files.append(entry)

        if skill_md_file is not None:
            results.append(skill_md_file)
            return

        results.extend(md_files)
        for sub in subdirs:
            _walk(sub, depth + 1)

    _walk(base_dir, 0)
    return results


def load_custom_command_file(
    file_path: Path,
    base_dir: Path,
    *,
    source: CustomCommandSource = "userSettings",
    namespace_prefix: str | None = None,
    plugin_name: str | None = None,
    plugin_root: Path | None = None,
    plugin_data_dir: Path | None = None,
    project_dir: Path | None = None,
) -> CustomCommandDefinition | None:
    """Load a single `.md` or `SKILL.md` file into a `CustomCommandDefinition`."""
    try:
        st = file_path.stat()
        if st.st_size > MAX_COMMAND_FILE_BYTES:
            logger.warning("Skipping custom command %s: exceeds %d bytes", file_path, MAX_COMMAND_FILE_BYTES)
            return None
        raw_content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Failed to read custom command %s: %s", file_path, exc)
        return None

    is_skill = _is_skill_md_name(file_path.name)
    skill_base_dir: Path | None = file_path.parent if is_skill else None

    fallback_label = "Plugin command" if source == "plugin" else f"Custom command ({source})"
    frontmatter, body, description, has_user_desc = parse_custom_command_frontmatter(
        raw_content,
        skill_dir=skill_base_dir or file_path.parent,
        project_dir=project_dir,
        fallback_label=fallback_label,
    )

    derived_name = derive_command_name_from_path(
        file_path,
        base_dir,
        namespace_prefix=namespace_prefix,
    )

    aliases: list[str] = []
    if namespace_prefix and derived_name.startswith(f"{namespace_prefix}:"):
        short_name = derived_name[len(namespace_prefix) + 1 :]
        if short_name and short_name not in aliases:
            aliases.append(short_name)
    if frontmatter.name and frontmatter.name != derived_name:
        clean_fm_name = frontmatter.name.strip().lstrip("/")
        if clean_fm_name and clean_fm_name not in aliases:
            aliases.append(clean_fm_name)

    loaded_from = "plugin" if source == "plugin" else ("skills" if is_skill else "commands_DEPRECATED")

    return CustomCommandDefinition(
        name=derived_name,
        description=description,
        body=body,
        frontmatter=frontmatter,
        file_path=file_path,
        base_dir=skill_base_dir,
        source=source,
        loaded_from=loaded_from,  # type: ignore[arg-type]
        plugin_name=plugin_name,
        plugin_root=plugin_root,
        plugin_data_dir=plugin_data_dir,
        aliases=aliases,
        has_user_specified_description=has_user_desc,
    )


def _discover_plugin_command_defs(
    project_dir: Path | None,
    seen_identities: set[str],
) -> list[CustomCommandDefinition]:
    """Scan enabled plugins' `commands/` directories for custom commands."""
    results: list[CustomCommandDefinition] = []
    try:
        from opscloud.config.paths import PLUGIN_DATA_DIR
        from opscloud.plugins.discovery import discover_plugins

        plugin_res = discover_plugins(project_root=project_dir)
        plugins = getattr(plugin_res, "plugins", ())
    except Exception:
        plugins = ()

    for plugin in plugins:
        p_name = getattr(plugin, "name", None) or getattr(getattr(plugin, "manifest", None), "name", "plugin")
        p_root = Path(getattr(plugin, "root", None) or getattr(plugin, "root_path", "."))
        p_data = PLUGIN_DATA_DIR / str(p_name)

        cmd_paths: list[Path] = []
        inv = getattr(plugin, "inventory", None)
        if inv is not None and getattr(inv, "commands", None):
            cmd_paths.extend(Path(p) for p in inv.commands)
        else:
            cmd_dir = p_root / "commands"
            if cmd_dir.exists():
                cmd_paths.append(cmd_dir)

        for cmd_path in cmd_paths:
            if not cmd_path.exists():
                continue
            if cmd_path.is_dir():
                for md_file in _scan_markdown_files_in_dir(cmd_path):
                    ident = _file_identity_key(md_file)
                    if ident in seen_identities:
                        continue
                    seen_identities.add(ident)
                    cmd_def = load_custom_command_file(
                        md_file,
                        cmd_path,
                        source="plugin",
                        namespace_prefix=str(p_name),
                        plugin_name=str(p_name),
                        plugin_root=p_root,
                        plugin_data_dir=p_data,
                        project_dir=project_dir,
                    )
                    if cmd_def is not None:
                        results.append(cmd_def)
            elif cmd_path.is_file() and cmd_path.suffix.lower() == ".md":
                ident = _file_identity_key(cmd_path)
                if ident in seen_identities:
                    continue
                seen_identities.add(ident)
                cmd_def = load_custom_command_file(
                    cmd_path,
                    cmd_path.parent,
                    source="plugin",
                    namespace_prefix=str(p_name),
                    plugin_name=str(p_name),
                    plugin_root=p_root,
                    plugin_data_dir=p_data,
                    project_dir=project_dir,
                )
                if cmd_def is not None:
                    results.append(cmd_def)
    return results


def discover_custom_commands_split(
    cwd: Path | None = None,
    *,
    project_root: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    extra_dirs: list[tuple[Path, CustomCommandSource, str | None]] | None = None,
    include_plugins: bool = True,
    use_cache: bool = True,
) -> tuple[list[CustomCommandDefinition], list[CustomCommandDefinition]]:
    """Discover unconditional and path-conditional custom commands.

    Returns `(unconditional_commands, conditional_commands)` where conditional
    commands have non-empty `paths:` frontmatter globs and are held until
    matching files are touched or the user explicitly invokes them.
    """
    resolved_cwd = (cwd or Path.cwd()).resolve()
    resolved_project = (
        project_root.resolve()
        if project_root is not None
        else find_project_root(resolved_cwd)
    )
    cache_key = f"{resolved_cwd}|{resolved_project}|{agent_name}|{include_plugins}|{extra_dirs}"
    if use_cache and cache_key in _DISCOVERED_CACHE:
        uncond, cond = _DISCOVERED_CACHE[cache_key]
        return list(uncond), list(cond)

    seen_identities: set[str] = set()
    all_loaded: list[CustomCommandDefinition] = []

    candidate_dirs: list[tuple[Path, CustomCommandSource, str | None]] = []

    # 1. Policy / Managed settings (`policySettings`)
    managed_env = os.environ.get("OPSCLOUD_MANAGED_COMMANDS_DIR") or os.environ.get(
        "CLAUDE_MANAGED_COMMANDS_DIR"
    )
    if managed_env:
        candidate_dirs.append((Path(managed_env), "policySettings", None))

    # 2. User settings (`~/.opscloud/commands` and `~/.claude/commands`)
    user_dir = get_user_commands_dir(agent_name)
    candidate_dirs.append((user_dir, "userSettings", None))
    claude_user_dir = get_user_claude_commands_dir()
    if claude_user_dir is not None:
        candidate_dirs.append((claude_user_dir, "userSettings", None))

    # 3. Project hierarchy settings (`.opscloud/commands` and `.claude/commands` from cwd to root)
    for proj_cmd_dir in get_project_hierarchy_command_dirs(resolved_cwd, resolved_project):
        candidate_dirs.append((proj_cmd_dir, "projectSettings", None))

    # 4. Extra explicitly supplied directories
    if extra_dirs:
        candidate_dirs.extend(extra_dirs)

    for cmd_dir, source, ns_prefix in candidate_dirs:
        if not cmd_dir.exists() or not cmd_dir.is_dir():
            continue
        for md_file in _scan_markdown_files_in_dir(cmd_dir):
            ident = _file_identity_key(md_file)
            if ident in seen_identities:
                continue
            seen_identities.add(ident)
            cmd_def = load_custom_command_file(
                md_file,
                cmd_dir,
                source=source,
                namespace_prefix=ns_prefix,
                project_dir=resolved_project or resolved_cwd,
            )
            if cmd_def is not None:
                all_loaded.append(cmd_def)

    # 5. Plugin commands
    if include_plugins:
        all_loaded.extend(_discover_plugin_command_defs(resolved_project or resolved_cwd, seen_identities))

    # Deduplicate by command name (later project-scoped overrides earlier user-scoped when names collide,
    # except policySettings which always wins)
    by_name: dict[str, CustomCommandDefinition] = {}
    for cmd_def in all_loaded:
        existing = by_name.get(cmd_def.name)
        if existing is not None and existing.source == "policySettings":
            continue
        by_name[cmd_def.name] = cmd_def

    unconditional: list[CustomCommandDefinition] = []
    conditional: list[CustomCommandDefinition] = []
    for cmd_def in by_name.values():
        if cmd_def.paths and cmd_def.name not in _ACTIVATED_CONDITIONAL_NAMES:
            conditional.append(cmd_def)
        else:
            unconditional.append(cmd_def)

    if use_cache:
        _DISCOVERED_CACHE[cache_key] = (list(unconditional), list(conditional))

    return unconditional, conditional


def discover_custom_commands(
    cwd: Path | None = None,
    *,
    project_root: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    extra_dirs: list[tuple[Path, CustomCommandSource, str | None]] | None = None,
    include_plugins: bool = True,
    include_conditional: bool = False,
    use_cache: bool = True,
) -> list[CustomCommandDefinition]:
    """Return active custom commands (plus conditional ones if `include_conditional=True`)."""
    unconditional, conditional = discover_custom_commands_split(
        cwd=cwd,
        project_root=project_root,
        agent_name=agent_name,
        extra_dirs=extra_dirs,
        include_plugins=include_plugins,
        use_cache=use_cache,
    )
    if include_conditional:
        return [*unconditional, *conditional]
    return unconditional


def _path_matches_glob(rel_path: str, pattern: str) -> bool:
    """Match a relative POSIX path against a frontmatter `paths:` glob."""
    norm_path = rel_path.replace("\\", "/").lstrip("./")
    norm_pat = pattern.replace("\\", "/").lstrip("./")
    if fnmatch.fnmatch(norm_path, norm_pat):
        return True
    if norm_path.startswith(f"{norm_pat.rstrip('/')}/"):
        return True
    if "**/" in norm_pat:
        tail = norm_pat.split("**/", 1)[1]
        if fnmatch.fnmatch(norm_path, tail) or fnmatch.fnmatch(Path(norm_path).name, tail):
            return True
    return False


def activate_conditional_commands_for_paths(
    file_paths: list[str | Path],
    *,
    cwd: Path | None = None,
    project_root: Path | None = None,
    extra_dirs: list[tuple[Path, CustomCommandSource, str | None]] | None = None,
) -> list[CustomCommandDefinition]:
    """Activate conditional `paths:` commands matching any of `file_paths`."""
    resolved_cwd = (cwd or Path.cwd()).resolve()
    resolved_root = (
        project_root.resolve()
        if project_root is not None
        else (find_project_root(resolved_cwd) or resolved_cwd)
    )
    _, conditional = discover_custom_commands_split(
        cwd=resolved_cwd,
        project_root=resolved_root,
        extra_dirs=extra_dirs,
        use_cache=False,
    )
    if not conditional:
        return []

    rel_candidates: list[str] = []
    for raw_p in file_paths:
        p = Path(raw_p)
        try:
            abs_p = p.resolve() if p.is_absolute() else (resolved_cwd / p).resolve()
            rel_candidates.append(str(abs_p.relative_to(resolved_root)).replace("\\", "/"))
        except (ValueError, OSError):
            rel_candidates.append(str(p).replace("\\", "/"))

    newly_activated: list[CustomCommandDefinition] = []
    for cmd_def in conditional:
        if cmd_def.name in _ACTIVATED_CONDITIONAL_NAMES:
            continue
        if any(
            _path_matches_glob(rel_p, pat)
            for rel_p in rel_candidates
            for pat in cmd_def.paths
        ):
            _ACTIVATED_CONDITIONAL_NAMES.add(cmd_def.name)
            newly_activated.append(cmd_def)

    if newly_activated:
        _DISCOVERED_CACHE.clear()
    return newly_activated


def clear_custom_command_cache(*, reset_conditional: bool = False) -> None:
    """Clear in-memory custom command discovery cache."""
    _DISCOVERED_CACHE.clear()
    if reset_conditional:
        _ACTIVATED_CONDITIONAL_NAMES.clear()


# ── Usage tracking ────────────────────────────────────────────────────────


def _load_usage_store(usage_path: Path | None = None) -> dict[str, dict[str, float]]:
    target = usage_path or COMMAND_USAGE_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def record_command_usage(command_name: str, *, usage_path: Path | None = None) -> None:
    """Increment usage count and update timestamp for `command_name`."""
    clean = command_name.lstrip("/")
    target = usage_path or COMMAND_USAGE_PATH
    try:
        if usage_path is None:
            ensure_state_dir()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        store = _load_usage_store(target)
        prev = store.get(clean, {})
        count = int(prev.get("usage_count", 0)) + 1
        store[clean] = {
            "usage_count": count,
            "last_used_at": time.time(),
        }
        target.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not persist command usage for %s: %s", clean, exc)


def get_command_usage_priority(command_name: str, *, usage_path: Path | None = None) -> float:
    """Compute 7-day half-life recency-weighted priority score.

    `score = usage_count * max(0.5 ** (days_since / 7), 0.1)`
    """
    clean = command_name.lstrip("/")
    store = _load_usage_store(usage_path)
    entry = store.get(clean)
    if not entry:
        return 0.0
    usage_count = float(entry.get("usage_count", 0))
    if usage_count <= 0:
        return 0.0
    last_used = float(entry.get("last_used_at", 0.0))
    days_since = max(0.0, (time.time() - last_used) / 86400.0)
    decay = max(0.5 ** (days_since / 7.0), 0.1)
    return usage_count * decay


# ── Live File Content-Hash Watcher / Hot-Reload ───────────────────────────


class CustomCommandWatcher:
    """Content-hash file watcher for custom command directories.

    Tracks SHA-256 digests of `.md` files across watched command directories so
    real content additions, edits, or deletions invalidate caches and hot-reload
    without spurious reloads on metadata-only `touch` events.
    """

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        project_root: Path | None = None,
        agent_name: str = DEFAULT_AGENT_NAME,
        extra_dirs: list[Path] | None = None,
    ) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.project_root = (
            project_root.resolve()
            if project_root is not None
            else find_project_root(self.cwd)
        )
        self.agent_name = agent_name
        self.extra_dirs = list(extra_dirs or [])
        self._file_hashes: dict[str, str] = {}
        self.snapshot()

    def watched_directories(self) -> list[Path]:
        """Return all candidate custom command directories."""
        dirs: list[Path] = [get_user_commands_dir(self.agent_name)]
        claude_user = get_user_claude_commands_dir()
        if claude_user is not None:
            dirs.append(claude_user)
        dirs.extend(get_project_hierarchy_command_dirs(self.cwd, self.project_root))
        dirs.extend(self.extra_dirs)
        return dirs

    def _compute_current_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for cmd_dir in self.watched_directories():
            if not cmd_dir.exists() or not cmd_dir.is_dir():
                continue
            for md_file in _scan_markdown_files_in_dir(cmd_dir):
                try:
                    digest = hashlib.sha256(md_file.read_bytes()).hexdigest()
                    hashes[str(md_file.resolve())] = digest
                except OSError:
                    continue
        return hashes

    def snapshot(self) -> dict[str, str]:
        """Capture baseline content hashes of all custom command files."""
        self._file_hashes = self._compute_current_hashes()
        return dict(self._file_hashes)

    def check_and_reload(self) -> bool:
        """Return True and invalidate caches if any command file was added, modified, or removed."""
        current = self._compute_current_hashes()
        if current != self._file_hashes:
            self._file_hashes = current
            clear_custom_command_cache()
            return True
        return False
