# Engineering Issue & Forensic Audit: Plugin Uninstall Failure, Silent No-Op, and Cross-Workspace Scope Desynchronization

**Issue ID:** `ISSUE-OPSCLOUD-PLUGIN-002`  
**Date:** October 8, 2026  
**Severity:** **P1 — High (Core TUI / Plugin Lifecycle Defect)**  
**Component:** Plugin Management (`src/opscloud/plugins/store.py`, `src/opscloud/plugins/discovery.py`, `src/opscloud/ui/widgets/plugin_manager.py`)  
**Target Environments:** OpsCloud TUI (`/plugins`), OpsCloud CLI (`opscloud plugins uninstall`)  
**Reference Codebase:** `reference/dcode`  
**Author:** Platform Engineering & Core Architecture Audit  
**Audience:** Core Platform Team, Agent Runtime Engineers, QA & Testing Infrastructure  

---

## 1. Incident Overview & Problem Statement

Users attempting to uninstall installed plugins via the interactive TUI modal (`/plugins`) experience an **irrecoverable silent failure**:

1. In the TUI modal under `> Installed <`, a user selects an installed plugin (e.g. `aws-agents-for-devsecops`, `AWS Data Engineer`, `AWS Containers`, or `Terraform (HashiCorp)`).
2. The user selects **"Uninstall"** from the action menu.
3. The UI briefly shows `Uninstalling <plugin>...` and then permanently displays the success banner:
   ```text
   Plugin uninstalled.
   ```
4. **However, the plugin is NOT uninstalled:** It remains in the `> Installed <` list.
5. Repeating the uninstall action displays `Plugin uninstalled.` every single time, but the plugin is never deleted from disk, never removed from `~/.opscloud/.state/installed_plugins.json`, and its cached directories in `~/.opscloud/plugins/cache/` are never purged.

### 1.1 Visual Evidence (User TUI Capture)
From the reported TUI screenshot:
```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                   Plugins                                    │
│       Discover             > Installed <          Marketplaces       Errors  │
│ ──────────────────────────────────────────────────────────────────────────── │
│  Plugin uninstalled.                                                         │
│                                                                              │
│  Search plugins...                                                           │
│ ┌──────────────────────────────────────────────────────────────────────────┐ │
│ │aws-agents-for-devsecops · Plugin · agent-toolkit-for-aws                 │ │
│ │ Investigate incidents, review code and execute UAT for release...        │ │
│ │                                                                          │ │
│ │AWS Data Engineer · Plugin · talkops-devops-plugins                       │ │
│ │ Data-lake and analytics engineer: S3 Tables/Iceberg, Glue...             │ │
│ │                                                                          │ │
│ │AWS Containers · Plugin · talkops-devops-plugins · pending /reload        │ │
│ │ Container skills for the main agent: EKS, ECS, Fargate...                │ │
│ │                                                                          │ │
│ │Terraform (HashiCorp) · Plugin · talkops-devops-plugins · pending /reload │ │
│ └──────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

Despite the status reading `Plugin uninstalled.`, the four plugins remain active and listed.

---

## 2. Root Cause Analysis (Forensic Breakdown)

Our forensic investigation revealed **5 compounding architectural flaws** spanning the TUI state model, the plugin store's multi-scope record matching, and silent error suppression.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   THE FAULT CHAIN REPRODUCTION                               │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Plugin was installed in Repo A (e.g. ~/work/talkops/terraform-example-modules) with       │
│    scope="local" → recorded in ~/.opscloud/.state/installed_plugins.json with:               │
│    { scope: "local", projectPath: "/.../terraform-example-modules" }                         │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 2. OpsCloud runs in Repo B (e.g. ~/work/talkops/opscloud):                                   │
│    load_installed_plugins() reads ~/.opscloud/.state/installed_plugins.json GLOBALLY.        │
│    No filtering by current project root occurs → Plugin appears under "> Installed <".       │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 3. UI builds _PluginRow:                                                                     │
│    Extracts scope="local", BUT DISCARDS project_path.                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 4. User clicks "Uninstall":                                                                  │
│    TUI calls uninstall_plugin(pid, scope="local", project_root=Path("/.../opscloud"))        │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 5. store.py:remove_installed_plugin executes:                                                │
│    e.scope == "local" and e.project_path == project_path                                     │
│    → "/.../terraform-example-modules" == "/.../opscloud" → FALSE!                            │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 6. removed remains None → Record is NOT deleted → Cache is NOT purged.                       │
│    uninstall_plugin() exits with return None (SILENT NO-OP).                                 │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 7. TUI catches no exception → sets self._status = "Plugin uninstalled."                      │
│    _refresh_state() reloads installed_plugins.json → Plugin STILL THERE!                     │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### Root Cause 1: Path Mismatch in Scoped Record Removal (`store.py:remove_installed_plugin`)
**Location:** [`src/opscloud/plugins/store.py:753–787`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/store.py#L753-L787)

#### The Code:
```python
def remove_installed_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
) -> InstalledPluginEntry | None:
    all_entries = load_installed_plugin_entries(strict=True)
    existing = all_entries.get(plugin_id)
    if not existing:
        return None

    if scope is None:
        removed = existing[0] if existing else None
        del all_entries[plugin_id]
    else:
        project_path = (
            str(project_root)
            if (scope != "user" and project_root is not None)
            else None
        )
        removed = None
        remaining = []
        for e in existing:
            if e.scope == scope and (scope == "user" or e.project_path == project_path):
                removed = e
            else:
                remaining.append(e)
        if remaining:
            all_entries[plugin_id] = remaining
        else:
            all_entries.pop(plugin_id, None)

    _write_installed_plugins_raw(all_entries)
    return removed
```

#### What Happens at Runtime:
1. An entry in `~/.opscloud/.state/installed_plugins.json` has:
   ```json
   {
     "scope": "local",
     "projectPath": "/Users/structbinary/Documents/work/talkops/terraform-example-modules"
   }
   ```
2. The user is currently running `opscloud` from `/Users/structbinary/Documents/work/talkops/opscloud`.
3. When `remove_installed_plugin` is called with `scope="local"` and `project_root=Path("/Users/structbinary/Documents/work/talkops/opscloud")`:
   - `project_path` evaluates to `"/Users/structbinary/Documents/work/talkops/opscloud"`.
   - The loop checks `e.project_path == project_path`.
   - `"/Users/.../terraform-example-modules" == "/Users/.../opscloud"` evaluates to **`False`**.
   - `removed` remains `None`.
   - `e` is kept in `remaining`.
   - `all_entries[plugin_id]` is re-written with `[e]` completely intact!
   - The function returns `None`.

#### Live Reproduction:
```bash
$ uv run python -c "
from pathlib import Path
from opscloud.plugins.discovery import uninstall_plugin
from opscloud.plugins.store import load_installed_plugins

pid = 'aws-agents-for-devsecops@agent-toolkit-for-aws'
print('Before:', pid in load_installed_plugins())
uninstall_plugin(pid, scope='local', project_root=Path('/Users/structbinary/Documents/work/talkops/opscloud'))
print('After:', pid in load_installed_plugins())
"
Before: True
After: True
```
**Result:** 100% reproducible no-op.

---

### Root Cause 2: Dropping `project_path` in `_PluginRow` and Passing Wrong `project_root`
**Locations:**
* [`src/opscloud/ui/widgets/plugin_manager.py:79–95`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/ui/widgets/plugin_manager.py#L79-L95)
* [`src/opscloud/ui/widgets/plugin_manager.py:465–486`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/ui/widgets/plugin_manager.py#L465-L486)
* [`src/opscloud/ui/widgets/plugin_manager.py:1142–1161`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/ui/widgets/plugin_manager.py#L1142-L1161)

#### The Faulty Logic:
1. `_PluginRow` is defined as:
   ```python
   class _PluginRow:
       plugin_id: str
       description: str
       enabled: bool
       version: str | None
       author: str | None
       display_name: str = ""
       ...
       scope: InstallScope | None = None
       # NOTE: project_path is NOT stored!
   ```
2. In `_load_manager_state()`:
   ```python
   entry_list = all_entries.get(plugin_id, [])
   scope: InstallScope | None = (
       entry_list[0].scope
       if entry_list
       else ("project" if is_project_marketplace else None)
   )
   row = _PluginRow(
       ...,
       scope=scope if is_installed else None,
   )
   ```
   The record's actual `entry_list[0].project_path` is completely lost.
3. In `on_option_list_option_selected()`:
   ```python
   await asyncio.to_thread(
       uninstall_plugin,
       self._selected_plugin.plugin_id,
       scope=self._selected_plugin.scope,  # "local"
       project_root=self._project_root,    # current project root, NOT the plugin's project_path!
   )
   ```
   The TUI supplies its own workspace path (`self._project_root`) instead of the workspace where the plugin was actually installed.

---

### Root Cause 3: Void Function Contract & Silent Swallowing of Non-Matches
**Locations:**
* [`src/opscloud/plugins/store.py:857–906`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/store.py#L857-L906)
* [`src/opscloud/plugins/discovery.py:239–254`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/discovery.py#L239-L254)

#### The Code:
```python
def uninstall_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
) -> None:
    ...
    removed = remove_installed_plugin(
        plugin_id, scope=scope, project_root=effective_root
    )
    ...
    if removed is not None:
        # cache cleanup
        ...
```
1. `uninstall_plugin()` returns `None` regardless of whether `remove_installed_plugin()` removed a record or returned `None`.
2. It **never raises an exception** (such as `PluginNotFoundError` or `PluginScopeMismatchError`) when `removed is None`.
3. The calling UI code assumes that any call that does not raise an exception was successful:
   ```python
   try:
       await asyncio.to_thread(uninstall_plugin, ...)
       self._mode = "list"
       self._selected_plugin = None
       self._status = "Plugin uninstalled."  # <-- Falsely reports success!
       await self._refresh_state()
   except Exception as exc:
       self._status = None
       self._error = str(exc)
   ```

---

### Root Cause 4: Global `load_installed_plugins()` Leaking Cross-Workspace State
**Location:** [`src/opscloud/plugins/store.py:647–655`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/store.py#L647-L655)

#### The Code:
```python
def load_installed_plugins(*, strict: bool = False) -> dict[str, InstalledPluginEntry]:
    """Load installed plugin records (first entry per plugin, backward-compat)."""
    all_entries = load_installed_plugin_entries(strict=strict)
    return {
        plugin_id: entries[0]
        for plugin_id, entries in all_entries.items()
        if entries
    }
```
1. `load_installed_plugins()` takes **no `project_root` parameter**.
2. It returns all entries from `~/.opscloud/.state/installed_plugins.json` unconditionally.
3. Therefore, if a user installed a plugin with `--scope local` in Repo A (`terraform-example-modules`), when they open OpsCloud in Repo B (`opscloud`), the plugin appears in `> Installed <` as if it were installed in Repo B!
4. But when the user tries to uninstall it from Repo B, it cannot be deleted because it belongs to Repo A.

---

### Root Cause 5: Partial Scope Desynchronization
**Location:** [`src/opscloud/plugins/discovery.py:246–253`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/discovery.py#L246-L253)

```python
    canonical_id = _resolve_installed_plugin_id(plugin_id)
    effective_scope = scope or ("project" if project_root else "user")
    set_plugin_enabled_for_scope(
        canonical_id, False, scope=effective_scope, project_root=project_root
    )
    uninstall_plugin_record(
        canonical_id, scope=scope, project_root=project_root
    )
```
When `uninstall_plugin()` was executed in the user's session:
1. `set_plugin_enabled_for_scope` wrote `"aws-agents-for-devsecops@agent-toolkit-for-aws": false` into Repo B's `settings.local.json` (`/Users/.../opscloud/.opscloud/settings.local.json`).
2. But the install record remained in `installed_plugins.json`.
3. Meanwhile, `~/.opscloud/settings.json` still contained enabled flags for other plugins.
4. The system is left in an inconsistent hybrid state: the plugin is marked disabled in Repo B's local settings, but remains registered as installed in global state.

---

## 3. Why Unit Tests Failed to Catch This (Gap Analysis)

The team asked: **"how come the test cases doesn't catch them?"**

Here is the detailed test gap breakdown from our audit of `tests/`:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   TEST GAP MATRIX                                      │
├──────────────────────────┬─────────────────────────────┬───────────────────────────────┤
│ Failure Point            │ What Existing Tests Do      │ What Is Missing               │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ Scope Symmetrical Test   │ Tests add + remove in the   │ Never tests different         │
│                          │ exact same tmp_path fixture │ project_root or foreign repo  │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ Local Scope Coverage     │ Only tests "project" and    │ "local" scope uninstall is    │
│                          │ "user" scopes               │ completely untested           │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ UI Modal Integration     │ test_plugin_manager_search  │ Zero tests for action routing │
│                          │ mocks _load_manager_state   │ or "action:uninstall"         │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ Negative Assertions      │ Tests assert no exception   │ No test checks that non-match │
│                          │ thrown                      │ raises error or returns False │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ Cross-Project Isolation  │ Tests use isolated tmp_path │ No test checks cross-project  │
│                          │ without global state drift  │ installed_plugins leakage     │
└──────────────────────────┴─────────────────────────────┴───────────────────────────────┘
```

### Gap 1: Symmetrical Single-Scope Fixtures in `test_plugins.py`
In [`tests/unit_tests/test_plugins.py:442–465`](file:///Users/structbinary/Documents/work/talkops/opscloud/tests/unit_tests/test_plugins.py#L442-L465):
```python
add_installed_plugin(
    "test@mp",
    install_path=str(cache_dir),
    version="1.0.0",
    scope="project",
    project_root=project_root,
)
...
uninstall_plugin("test@mp", scope="project", project_root=project_root)
```
* The test passes because `project_root` in line 447 and line 459 is the **exact same Python variable**.
* No test ever attempted to uninstall a plugin where `project_root != entry.project_path`.
* No test ever called `uninstall_plugin` with `scope="local"`.

### Gap 2: Textual TUI Tests Completely Mocked Out `_load_manager_state`
In [`tests/unit_tests/test_plugin_manager_search.py:104`](file:///Users/structbinary/Documents/work/talkops/opscloud/tests/unit_tests/test_plugin_manager_search.py#L104):
```python
with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
    ...
```
* The only TUI test file for `PluginManagerScreen` completely mocks out `_load_manager_state`.
* There are **zero tests** that simulate pressing Enter on `action:uninstall`.
* There are **zero tests** verifying that `self._status = "Plugin uninstalled."` accurately reflects whether the record was actually deleted from `installed_plugins.json`.

### Gap 3: Void Contract Masked Failure
Because `uninstall_plugin()` has a return type of `None` and does not raise on failure:
* Any test that called `uninstall_plugin()` would pass as long as it didn't throw an unhandled crash.
* No test asserted `assert pid not in load_installed_plugins()` after an uninstall attempt with mismatched paths.

---

## 4. Architectural Comparison with Reference `reference/dcode`

In `reference/dcode`, the plugin storage architecture is dramatically simpler and immune to this defect:

| Dimension | `reference/dcode` | `src/opscloud` (Current) |
| :--- | :--- | :--- |
| **Storage Model** | Flat single-entry dictionary per plugin ID | Multi-scope array per plugin ID (`list[InstalledPluginEntry]`) |
| **Uninstall Method** | `plugins.pop(plugin_id, None)` directly deletes entry | Complex loop checking `scope == e.scope` and `project_path == e.project_path` |
| **Path Dependency** | Path-agnostic global removal | Hard-coupled to matching exact string `project_path` |
| **TUI Scope Passing** | Passes only `plugin_id` (`uninstall_plugin(row.plugin_id)`) | Passes `scope` and `project_root` without matching metadata |
| **Status Feedback** | Single source of truth | Optimistic UI feedback disconnected from store return value |

**Reference Implementation in `dcode`:**
[`reference/dcode/plugins/store.py:479–481`](file:///Users/structbinary/Documents/work/talkops/opscloud/reference/dcode/plugins/store.py#L479-L481):
```python
def remove_installed_plugin(plugin_id: str) -> InstalledPluginEntry | None:
    plugins = dict(load_installed_plugins(strict=True))
    removed = plugins.pop(plugin_id, None)
    _write_installed_plugins(plugins)
    return removed
```
[`reference/dcode/tui/modals/plugin_manager/__init__.py:1200`](file:///Users/structbinary/Documents/work/talkops/opscloud/reference/dcode/tui/modals/plugin_manager/__init__.py#L1200):
```python
await asyncio.to_thread(uninstall_plugin, row.plugin_id)
```

In `dcode`, when a user uninstalls a plugin, it uninstalls the plugin. It does not check whether the user's terminal current working directory matches the path where the plugin was originally added.

---

## 5. Step-by-Step Remediation Plan

To permanently fix this issue and prevent regressions, the engineering team must implement the following changes:

---

### Step 1: Update `remove_installed_plugin` in `src/opscloud/plugins/store.py`
Make `remove_installed_plugin` resilient:
1. If an exact match for `(scope, project_path)` exists, remove it.
2. **Fallback:** If `scope` is provided (or if there is only one entry for `plugin_id`), and no exact `project_path` match is found (e.g. cross-project invocation or orphaned entry), remove that entry instead of failing silently.
3. If `scope is None`, remove all entries for that `plugin_id`.
4. Ensure the function returns the removed entry (or raises `PluginNotFoundError` if nothing was removed).

```python
def remove_installed_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
    project_path: str | None = None,
) -> InstalledPluginEntry | None:
    all_entries = load_installed_plugin_entries(strict=True)
    existing = all_entries.get(plugin_id)
    if not existing:
        return None

    target_project_path = (
        project_path
        if project_path is not None
        else (str(project_root) if (scope != "user" and project_root is not None) else None)
    )

    removed = None
    if scope is None:
        removed = existing[0] if existing else None
        del all_entries[plugin_id]
    else:
        remaining = []
        for e in existing:
            # Exact match on scope and project_path
            if removed is None and e.scope == scope and (scope == "user" or target_project_path is None or e.project_path == target_project_path):
                removed = e
            else:
                remaining.append(e)

        # Fallback: if no match found but existing entries have this scope or single entry exists
        if removed is None and existing:
            removed = existing.pop(0)
            remaining = existing

        if remaining:
            all_entries[plugin_id] = remaining
        else:
            all_entries.pop(plugin_id, None)

    _write_installed_plugins_raw(all_entries)
    return removed
```

---

### Step 2: Add Boolean/Result Return to `uninstall_plugin` and Raise on Failure
In `src/opscloud/plugins/store.py` and `src/opscloud/plugins/discovery.py`:
```python
def uninstall_plugin(
    plugin_id: str,
    *,
    scope: InstallScope | None = None,
    project_root: Path | None = None,
    project_path: str | None = None,
) -> bool:
    ...
    removed = remove_installed_plugin(
        plugin_id, scope=scope, project_root=effective_root, project_path=project_path
    )
    if removed is None:
        raise PluginNotFoundError(f"Plugin {plugin_id!r} is not installed (scope={scope}, root={project_root})")
    ...
    return True
```

---

### Step 3: Preserve `project_path` in `_PluginRow` in `plugin_manager.py`
In `src/opscloud/ui/widgets/plugin_manager.py`:
1. Add `project_path: str | None = None` to `_PluginRow`.
2. In `_load_manager_state()`, populate `project_path`:
   ```python
   entry_list = all_entries.get(plugin_id, [])
   primary_entry = entry_list[0] if entry_list else None
   scope = primary_entry.scope if primary_entry else ("project" if is_project_marketplace else None)
   project_path = primary_entry.project_path if primary_entry else None
   ...
   row = _PluginRow(
       ...,
       scope=scope if is_installed else None,
       project_path=project_path if is_installed else None,
   )
   ```
3. In `on_option_list_option_selected()`:
   ```python
   if option_id == "action:uninstall":
       if self._selected_plugin:
           target_root = (
               Path(self._selected_plugin.project_path)
               if self._selected_plugin.project_path
               else self._project_root
           )
           await asyncio.to_thread(
               uninstall_plugin,
               self._selected_plugin.plugin_id,
               scope=self._selected_plugin.scope,
               project_root=target_root,
           )
   ```

---

### Step 4: Add Comprehensive Test Suites
Create a dedicated test file `tests/unit_tests/test_plugin_uninstall_lifecycle.py` covering:
1. **Cross-Project Uninstall:** Install in `dir_a` with `scope="local"`, call uninstall from `dir_b` with `scope="local"` → verifies fallback uninstalls successfully.
2. **TUI Action Handler Test:** Instantiate `PluginManagerScreen`, select an installed plugin, trigger `action:uninstall`, and verify `installed_plugins.json` is modified and cache directory is removed.
3. **Negative Test:** Attempt to uninstall a non-existent plugin → verifies `PluginNotFoundError` is raised instead of silent no-op.
4. **Cache Directory Purge:** Verify `shutil.rmtree` is called when no remaining scopes reference the cache path.

---

## 6. Action Items & Owners

| Task | Priority | Component | Verification Criteria |
| :--- | :--- | :--- | :--- |
| **1. Resilient `remove_installed_plugin`** | **P0** | `src/opscloud/plugins/store.py` | Cross-project and local scope removals succeed without silent no-op |
| **2. Explicit Error on Missing Record** | **P0** | `src/opscloud/plugins/discovery.py` | `uninstall_plugin` raises `PluginNotFoundError` on failure |
| **3. Preserve `project_path` in `_PluginRow`** | **P1** | `src/opscloud/ui/widgets/plugin_manager.py` | TUI uninstalls local/project plugins with their correct original root |
| **4. End-to-End TUI Modal Tests** | **P1** | `tests/unit_tests/` | Automated tests verify `/plugins` modal uninstall action |
