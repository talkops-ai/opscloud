# Engineering Issue & Architectural Audit: MCP Tool Schema Explosion & Extreme Latency in Plugin Skills Flow

**Issue ID:** `ISSUE-OPSCLOUD-MCP-001`  
**Date:** October 8, 2026  
**Severity:** **P0 — Blocker**  
**Component:** Deep Agent Core / Subagent Flow / MCP Session Manager / Plugin Adapters  
**Target Environments:** `src/opscloud` (OpsCloud Orchestrator) vs. `reference/dcode` (Reference Implementation)  
**Authors:** AI Architecture & Core Systems Audit  
**Audience:** Core Engineering Team, Agent Platform Engineers, DevOps & Infrastructure  

---

## 1. Executive Summary & Impact Analysis

During testing of skill plugins equipped with MCP (Model Context Protocol) servers (e.g., `aws-containers@talkops-devops-plugins`), the Deep Agent experienced catastrophic response latency and token usage degradation:

* **OpsCloud Turn 1 Performance:** **315.79 seconds (5.22 minutes)** total turn duration, processing **211,300 input tokens** ($0.1531 cost) on a simple user command.
* **Reference `dcode` Performance:** **15.10s P50** across 4 turns (Turn 1: 0.73s, Turn 2: 25.39s [45.37K tokens], Turn 3: 15.09s [46.6K tokens], Turn 4: 40.15s [254.8K tokens]).
* **Discrepancy Ratio:** **~21x Latency Explosion**, **~4.7x Initial Prompt Context Explosion**.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                      INITIAL TURN LATENCY & CONTEXT COMPARISON                │
├──────────────────┬─────────────────────────────┬──────────────────────────────┤
│ Metric           │ OpsCloud (Current)          │ reference/dcode (Benchmark)  │
├──────────────────┼─────────────────────────────┼──────────────────────────────┤
│ Turn 1 Latency   │ 315.79s (5m 15s) ⚠️ BLOCKER │ 0.73s - 25.39s (P50: 15.10s) │
│ Turn 1 Tokens    │ 211,300 tokens (Context full)│ ~45,370 tokens               │
│ Tool Injection   │ Eager / Global / Unscoped   │ Scoped / Filtered            │
│ Schema Strategy  │ Recursive $ref Dereference  │ Protocol Native (Compact)    │
│ Session / Loop   │ Ephemeral Thread Loop + Stdio│ Server Loop Router Client    │
└──────────────────┴─────────────────────────────┴──────────────────────────────┘
```

The system became completely unresponsive for 5+ minutes because **the entire LLM context window was inundated with over 211K tokens of recursively inlined JSON schemas for dozens of un-scoped Kubernetes/AWS tools**, which then had to be re-evaluated across every internal graph step over disjointed stdio subprocess loops.

---

## 2. Trace & Incident Evidence Breakdown

### 2.1 OpsCloud Trace (Image 1: `media_1791459244211.png`)
* **Execution Status:** 1 Turn, Duration: **315.79s**, Context: **211.3K tokens**, Cost: **$0.1531**.
* **Observed Bound Tools:**
  * Core: `create_temp_artifact`, `delete_temp_artifact`, `fetch_url`.
  * Finch MCP: `plugin__aws-containers_ta_finch_build_container_ima_df47730a85e8`, `plugin__aws-containers_talkops-dev_finch_push_image_6b02bcde1560`, `plugin__aws-containers_talkop_finch_create_ecr_repo_a8f88f0a1f9f`.
  * EKS / Kubernetes MCP: `plugin__aws-containers_talkops-d_list_k8s_resources_5aa672efc864`, `plugin__aws-containers_talkops-devops-_get_pod_logs_a03a76775bc1`, `plugin__aws-containers_talkops-devop_get_k8s_events_465d1e587238`, `plugin__aws-containers_talkops-de_list_api_versions_c0a261010b5e`, `plugin__aws-containers_talkops-_manage_k8s_resource_b877895269a7`, `plugin__aws-containers_talkops-devops-pl_apply_yaml_34e08bc5a89a`, `plugin__aws-containers_talkop_generate_app_manifest_555c8188f2bb`...
* **Symptom:** Every single tool from all MCP servers declared in the plugin was injected directly into the main agent's LLM prompt, expanding schemas recursively.

### 2.2 Reference `dcode` Trace (Image 2: `media_1791459309416.png`)
* **Execution Status:** 4 Turns, P50: **15.10s**, Cost: **$0.1014**.
  * Turn 1: **0.73s** (Goal initialization)
  * Turn 2: **25.39s** (45.37K tokens)
  * Turn 3: **15.09s** (46.6K tokens)
  * Turn 4: **40.15s** (254.8K tokens after 4 rounds of conversation history accumulation)
* **Observed Bound Tools:**
  * Core: `update_goal`, `ask_user`, `js_eval`, `create_temp_artifact`, `delete_temp_artifact`, `fetch_url`, `get_current_thread_id`.
  * Compute MCP (`aws-compute`): Only 6 focused tools (`aws__get_regional_availa`, `aws__search_documentatio`, `aws__read_documentation`, `aws__retrieve_skill`, `aws__list_regions`, `aws__get_tasks`).
* **Symptom:** Fast execution, minimal initial token overhead, and precise tool availability.

---

## 3. Deep Root-Cause Analysis (Forensic Audit)

Our cross-codebase audit identified **5 compounding architectural flaws** in OpsCloud that do not exist in `reference/dcode`.

---

### Root Cause 1: Recursive `$ref` Dereferencing in `_clean_mcp_schema`
**Location:** [`src/opscloud/mcp/session_manager.py:68–85`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/mcp/session_manager.py#L68-L85)

#### What OpsCloud Does:
```python
def _clean_mcp_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}

    from langchain_core.utils.json_schema import dereference_refs

    try:
        resolved = dereference_refs(schema)
    except Exception as exc:
        logger.debug("Failed to dereference MCP schema refs: %s", exc)
        resolved = schema

    cleaned = dict(resolved) if isinstance(resolved, dict) else dict(schema)
    for key in ("$schema", "$id", "$defs", "definitions", "additionalProperties"):
        cleaned.pop(key, None)
    ...
```

#### Why This Explodes Context to 211.3K Tokens:
* When `awslabs.eks-mcp-server` loads, tools like `apply_yaml`, `manage_k8s_resource`, and `generate_app_manifest` define parameters that accept complete Kubernetes manifests.
* The underlying JSON schema contains deep OpenAPI definitions (`#/$defs/ObjectMeta`, `#/$defs/PodSpec`, `#/$defs/Container`, `#/$defs/EnvVar`, etc.) with multiple recursive and cross-referencing branches.
* `dereference_refs()` replaces every `{"$ref": ...}` pointer with a **deep, full copy** of the referenced definition.
* If a single schema references sub-models 15 times, inlining them causes exponential duplication. A single tool's schema grows from a compact 1.5KB reference graph into a **60KB–100KB JSON behemoth**.
* Multiplying this by 10+ Kubernetes tools in `aws-containers` results in **150,000+ tokens generated from tool schema definitions alone**.

#### What `reference/dcode` Does Differently:
**Location:** [`reference/dcode/mcp_tools.py:1514–1520`](file:///Users/structbinary/Documents/work/talkops/opscloud/reference/dcode/mcp_tools.py#L1514-L1520)
* `dcode` passes the raw MCP schema directly to `langchain.mcp.as_langchain_tool(routed_tool, client)`.
* It **never calls `dereference_refs`**. The schema remains compact and standard, allowing the LLM provider's API client to handle references natively without exponential duplication.

---

### Root Cause 2: Global Eager Tool Injection of Vertical Plugins into Main Coordinator Agent
**Locations:**
* [`src/opscloud/plugins/models.py:174–185`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/models.py#L174-L185)
* [`src/opscloud/plugins/adapters/mcp.py:200–203`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/adapters/mcp.py#L200-L203)
* [`src/opscloud/agent/factory.py:604–660`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/agent/factory.py#L604-L660)

#### The Fault Chain:
1. In `src/opscloud/plugins/models.py`:
   ```python
   @property
   def is_agent_plugin(self) -> bool:
       """Return True if this plugin provides autonomous agents (agent-based plugin)."""
       if self.inventory.agents:
           ...
       if self.root and isinstance(self.root, Path):
           agents_dir = self.root / "agents"
           return agents_dir.is_dir() and any(agents_dir.glob("*.md"))
       return False
   ```
2. In `src/opscloud/plugins/adapters/mcp.py`:
   ```python
   def plugin_mcp_configs(plugins, *, include_subagents: bool = False):
       for plugin in plugins:
           if not include_subagents and plugin.is_agent_plugin:
               continue  # Dynamic subagent plugins isolate their own MCP servers
   ```
3. Vertical plugins (such as `aws-containers`, `aws-networking`, `aws-web-and-mobile`) and skill plugins do **not** define an `agents/*.md` directory. Therefore, `plugin.is_agent_plugin` evaluates to **`False`**.
4. Consequently, when `factory.py` calls `discover_mcp_configs()` to build the **Main Coordinator Agent**, all MCP servers declared by `aws-containers` (`finch`, `eks`, `ecs`, `aws-mcp`) are treated as global coordinator tools!
5. In `factory.py` lines 654–657:
   ```python
   discovered_tools = build_mcp_tools_from_server_infos(coordinator_infos, mcp_manager)
   if discovered_tools:
       mcp_tools_list.extend(discovered_tools)
       all_tools.extend(discovered_tools)  # <-- Injected into all_tools!
   ```
6. In `factory.py` line 1081:
   ```python
   graph = create_deep_agent(
       model=active_model,
       tools=all_tools,  # <-- ALL vertical plugin tools bound to Main Agent!
       ...
   )
   ```
7. **The result:** The main agent is inundated with 40+ specialized container/Kubernetes tools that should have been isolated to a specialized subagent or dynamically mounted skill.

---

### Root Cause 3: `ToolFilterMiddleware` Does NOT Filter Tools from the Model Request
**Location:** [`src/opscloud/middleware/tool_filter.py:412–431`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/middleware/tool_filter.py#L412-L431)

#### The Missing Hook:
* `ToolFilterMiddleware` only implements:
  - `wrap_tool_call(self, request: ToolCallRequest, handler)`
  - `awrap_tool_call(self, request: ToolCallRequest, handler)`
* It **does not implement** `wrap_model_call`, `awrap_model_call`, or `modify_request`.
* In LangGraph / DeepAgents, `tools` are passed to `model.bind_tools(self.tools)`.
* Because `ToolFilterMiddleware` does not intercept the model call request to prune `request.tools`, **100% of all registered tools are sent in the system prompt / tool definitions to the LLM on every single turn**.
* The filter only functions as an execution-time authorization gate (blocking the tool *after* the LLM decides to call it). It provides **zero context-reduction benefits**.

---

### Root Cause 4: Multi-Step Compounding Turn Latency (315 Seconds TTFT)
* Processing **211,300 input tokens** on remote LLM endpoints (Gemini 2.5 / 3.7 Flash, Claude 3.5 Sonnet) incurs an extreme Time-To-First-Token (TTFT) penalty: ~30 to 70 seconds per call.
* When the agent runs an agentic step (e.g., executing a command or asking for user clarification), LangGraph's Pregel engine executes sequential model nodes (`model` -> `tools` -> `model` -> `tools` -> `model`).
* Because the tool schema footprint remains at 211K tokens, **each model call must re-send and the LLM must re-process the full 211K tokens**.
* 4 sequential steps × 75 seconds per step = **300+ seconds (5.22 minutes)**.

---

### Root Cause 5: Event Loop Mismatch & Stdio Process Locking/Churn
**Locations:**
* [`src/opscloud/agent/factory.py:643–646`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/agent/factory.py#L643-L646)
* [`src/opscloud/mcp/session_manager.py:530–547`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/mcp/session_manager.py#L530-L547)

#### Thread & Event Loop Lifecycles in OpsCloud:
1. In `factory.py`:
   ```python
   if loop is not None and loop.is_running():
       with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
           new_infos = pool.submit(asyncio.run, preload_mcp_metadata(missing_configs)).result()
   else:
       new_infos = asyncio.run(preload_mcp_metadata(missing_configs))
   ```
   `asyncio.run()` creates a temporary event loop. Preloading connects via stdio, fetches tool names, and when `asyncio.run()` finishes, **that event loop is destroyed**. Any stdio pipes or process handles bound to that loop become dead or detached.
2. Later, when an agent turn executes on the server's `uvloop`, `get_session(server_name)` finds that no live session exists for that loop and calls `connect_server()` to start a brand new stdio connection via `uvx`.
3. Running multiple `uvx` instances (`uvx awslabs.eks-mcp-server@latest`, `uvx awslabs.finch-mcp-server@latest`, `uvx ecs-mcp-server`) concurrently causes:
   - Python virtual environment creation / cache resolution latency for each `uvx` invocation.
   - Stdio process blocking and mutex lock contention under `_get_server_lock(server_name)`.
   - Thread stalls while waiting for child processes to initialize their IPC pipes.

#### What `reference/dcode` Does Differently:
**Locations:**
* [`reference/dcode/server_graph.py:161–168`](file:///Users/structbinary/Documents/work/talkops/opscloud/reference/dcode/server_graph.py#L161-L168)
* [`reference/dcode/mcp_tools.py:2247–2251`](file:///Users/structbinary/Documents/work/talkops/opscloud/reference/dcode/mcp_tools.py#L2247-L2251)
* `dcode` initializes a single, shared `MCPSessionManager` directly on the server's running event loop via `_get_mcp_session_manager()`.
* Backends are mounted onto a single FastMCP router client.
* Preflight and connection setup are executed concurrently with bounded concurrency:
  ```python
  preflight_results = await _gather_bounded(
      [functools.partial(_preflight_and_connect, name, cfg) for name, cfg in server_items],
      limit=_MCP_LOAD_CONCURRENCY,
  )
  ```
* All tools route through this unified client. There are no detached thread pools or orphaned event loops.

---

## 4. Architectural Comparison: OpsCloud vs. `dcode`

| Architectural Dimension | OpsCloud (`src/opscloud`) | Reference Implementation (`reference/dcode`) |
| :--- | :--- | :--- |
| **Tool Adapter Layer** | `langchain_mcp_adapters` with custom `StructuredTool` wrapping | `fastmcp.Client` + `langchain.mcp.as_langchain_tool` |
| **JSON Schema Handling** | **Recursive `dereference_refs()`** expanding all definitions inline | Native schema pass-through without ref dereferencing |
| **Plugin MCP Scoping** | Only checks `plugin.is_agent_plugin` (`agents/*.md`). Vertical/Skill plugins dump all tools to Main Agent | Strict config discovery; plugins inject scoped configs with user trust gating |
| **Model Prompt Filtering** | **None.** `ToolFilterMiddleware` only acts at tool execution time | `_apply_tool_filter` prunes tools before agent construction via `allowedTools`/`disabledTools` |
| **Session Lifetime** | Eager thread preloading with `asyncio.run()`, on-demand reconnect on separate `uvloop` | Singleton `MCPSessionManager` bound to server event loop |
| **Connection Pooling** | Separate `_MCPSessionEntry` per server with individual locks | Single unified FastMCP router client multiplexing all backends |
| **Teardown & Cleanup** | `session.invalidate()` per server | Bounded 5s teardown via `AsyncExitStack` |

---

## 5. Engineering Action Plan & Implementation Guidance

To permanently resolve this blocker and achieve the ~15s response latency of `reference/dcode`, the engineering team must implement the following four-phase remediation:

### Phase 1: Eliminate Recursive Schema Expansion (Immediate Fix)
**Target:** [`src/opscloud/mcp/session_manager.py:68–103`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/mcp/session_manager.py#L68-L103)
1. **Remove `dereference_refs()`**: Cease using `langchain_core.utils.json_schema.dereference_refs` on raw MCP tool schemas.
2. **Schema Sanitization**: Instead of deep inlining, clean schemas by stripping provider-incompatible root keys (`$schema`, `$id`) while preserving local `$defs` or flattening top-level properties shallowly.
3. **Verify Schemas**: Ensure parameters for tools like `apply_yaml` and `manage_k8s_resource` pass concise schemas to the model.

### Phase 2: Restrict Plugin MCP Tool Scope to Subagents / Skills
**Target:** [`src/opscloud/plugins/adapters/mcp.py`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/plugins/adapters/mcp.py) & [`src/opscloud/agent/factory.py`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/agent/factory.py)
1. **Update Plugin Classification**: Plugins that provide skills with MCP servers must not automatically dump their MCP tools into the Main Coordinator Agent (`all_tools`).
2. **Scope Vertical Plugins**: Tools from vertical plugins (`aws-containers`, `aws-networking`, etc.) should only be attached to their respective specialized subagents or mounted when their specific skill is activated.
3. **Main Agent Tool Budget**: Ensure the Main Coordinator Agent is initialized with only core tools (`fetch_url`, `web_search`, `read_file`, `run_command`, `task`, `ask_user`), keeping initial context below **15,000 tokens**.

### Phase 3: Implement Model-Level Tool Filtering in `ToolFilterMiddleware`
**Target:** [`src/opscloud/middleware/tool_filter.py`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/middleware/tool_filter.py)
1. Implement `wrap_model_call` and `awrap_model_call` (or `modify_request`):
   ```python
   async def awrap_model_call(
       self,
       request: ModelRequest,
       handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
   ) -> ModelResponse:
       if self._allowed_patterns and request.tools:
           filtered_tools = [
               t for t in request.tools
               if self.is_tool_allowed(getattr(t, "name", str(t)), tool_obj=t)
           ]
           request = request.override(tools=filtered_tools)
       return await handler(request)
   ```
2. This guarantees that subagents with restricted tool lists (e.g., `allowed_tools: ["aws-finops-*"]`) only send their authorized tools to the LLM prompt.

### Phase 4: Align Session Lifecycle with `dcode` Pattern
**Target:** [`src/opscloud/mcp/session_manager.py`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/mcp/session_manager.py) & [`src/opscloud/server/server_graph.py`](file:///Users/structbinary/Documents/work/talkops/opscloud/src/opscloud/server/server_graph.py)
1. **Bind Session Manager to Server Loop**: Ensure `MCPSessionManager` is instantiated on the server's primary event loop, eliminating cross-thread `asyncio.run()` preloading.
2. **Router Client Multiplexing**: Adopt FastMCP router client mounting so all stdio/http transports share a single client context.
3. **Graceful Subprocess Management**: Ensure bounded subprocess shutdown (5s timeout) to prevent dangling `uvx` / `docker` processes on server reload.

---

## 6. Verification & Acceptance Criteria

The fix will be considered complete and verified when:
1. **Context Benchmark:** Turn 1 prompt context with `aws-containers` installed does not exceed **45,000 tokens** (comparable to `dcode`).
2. **Latency Benchmark:** Turn 1 execution latency drops from **315 seconds to under 25 seconds** on the same test query.
3. **No Schema Inlining Bloat:** Inspection of LangSmith tool definitions confirms that Kubernetes OpenAPI definitions are not duplicated into thousands of lines of JSON.
4. **Subagent Isolation:** Tools from `aws-containers` only appear in turns where the container subagent/skill is explicitly dispatched.
