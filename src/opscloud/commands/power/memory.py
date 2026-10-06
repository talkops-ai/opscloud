"""``/memory`` (``/remember``) — Conversation learning extraction and memory management.

``/remember`` is an alias for ``/skill:remember`` which extracts learnings
from conversation history into persistent memory (AGENTS.md) or skills.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import TYPE_CHECKING

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

if TYPE_CHECKING:
    from opscloud.memory.store import MemoryStore

logger = get_logger(__name__)


class MemoryHandler(BaseCommandHandler):
    """Manage conversation learnings and persisted memories.

    Subcommands & Syntax:
      ``/memory``                      — extract learnings via /skill:remember
      ``/remember``                    — extract learnings via /skill:remember
      ``/remember <instruction>``      — extract learnings with explicit focus
      ``/memory show``                 — list active AGENTS.md memories & stored entries
      ``/memory search <query>``       — search active memories & stored entries
      ``/memory get <key>``            — show a specific stored memory
      ``/memory save <key> <content>`` — save a key-value memory directly
      ``/memory delete <key>``         — delete a stored memory
      ``/memory clear``                — clear stored project memories
    """

    @property
    def name(self) -> str:
        return "/memory"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/remember",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from opscloud.memory.store import MemoryStore

        args = ctx.args.strip()

        # Project root detection
        project_root = _detect_project_root()
        store = MemoryStore(project_root=project_root)

        if not args:
            return await self._handle_skill_remember(ctx, "")

        sub, _, remainder = args.partition(" ")
        sub = sub.lower()
        remainder = remainder.strip()

        if sub in {"show", "list"}:
            return self._show_memories(store)

        if sub in {"search", "find"}:
            return self._search_memories(store, remainder)

        if sub == "get":
            if not remainder:
                return CommandResult(success=False, message="Usage: /memory get <key>")
            return self._get_memory(store, remainder)

        if sub == "save":
            key, _, content = remainder.partition(" ")
            if not key or not content:
                return CommandResult(success=False, message="Usage: /memory save <key> <content>")
            path = store.save(key.strip(), content.strip())
            return CommandResult(
                success=True,
                message=f"Memory saved: `{key.strip()}` → `{path}`",
            )

        if sub in {"delete", "rm", "remove"}:
            if not remainder:
                return CommandResult(success=False, message="Usage: /memory delete <key>")
            if store.delete(remainder):
                return CommandResult(success=True, message=f"Deleted memory: `{remainder}`")
            return CommandResult(success=False, message=f"Memory `{remainder}` not found.")

        if sub == "clear":
            entries = store.list_all()
            count = 0
            for entry in entries:
                if store.delete(entry.key):
                    count += 1
            return CommandResult(
                success=True,
                message=f"Cleared {count} memory entries." if count else "No memories to clear.",
            )

        # For any non-subcommand args (e.g. /remember prefer terraform over tofu), invoke skill:remember
        return await self._handle_skill_remember(ctx, args)

    async def _handle_skill_remember(
        self,
        ctx: CommandContext,
        args: str,
    ) -> CommandResult:
        """Handle ``/remember [args]`` — rewrite to ``/skill:remember [args]``."""
        app = ctx.app
        # Guard: if called bare with no messages in conversation, output hint
        if not args:
            has_messages = True
            if app is not None and hasattr(app, "_has_conversation_messages"):
                has_messages = await app._has_conversation_messages()

            if not has_messages:
                if app is not None and hasattr(app, "_mount_message"):
                    from opscloud.ui.widgets.messages import UserMessage
                    await app._mount_message(UserMessage(ctx.raw_command))
                return CommandResult(
                    success=True,
                    message="Nothing to remember yet. Start a conversation first,"
                    " then use /remember to capture learnings.",
                )

        rewritten = f"/skill:remember {args}" if args else "/skill:remember"

        if app is not None and hasattr(app, "_handle_skill_command"):
            try:
                await app._handle_skill_command(rewritten)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception as exc:
                logger.warning("Failed to invoke %s: %s", rewritten, exc)

        # Fallback to direct SkillInvokeHandler if app is not attached
        from opscloud.commands.power.skill_invoke import SkillInvokeHandler

        skill_ctx = CommandContext(
            app=ctx.app,
            session=ctx.session,
            agent=ctx.agent,
            settings=ctx.settings,
            raw_command=rewritten,
            args=f"remember {args}".strip(),
            thread_id=ctx.thread_id,
            model_spec=ctx.model_spec,
        )
        return await SkillInvokeHandler().execute(skill_ctx)

    def _show_memories(self, store: "MemoryStore") -> CommandResult:
        """List currently active memories loaded from AGENTS.md files and MemoryStore."""
        from opscloud.memory.registry import MemoryRegistry

        lines = ["**Active Memories & Knowledge:**", ""]
        agents_found = 0

        # Read active AGENTS.md paths
        try:
            paths = MemoryRegistry.get_instance().get_memory_paths_for_scope("auto")
            for path in paths:
                if path.is_file():
                    content = path.read_text(encoding="utf-8").strip()
                    if content:
                        agents_found += 1
                        lines.append(f"**{path}**")
                        preview = "\n".join(f"   {line}" for line in content.splitlines()[:10])
                        lines.append(preview)
                        if len(content.splitlines()) > 10:
                            lines.append("   *... [truncated]*")
                        lines.append("")
        except Exception as exc:
            logger.debug("Failed to read AGENTS.md files: %s", exc)

        # Key-Value MemoryStore entries
        entries = store.list_all()
        if entries:
            lines.append("**Persisted Key-Value Memories:**")
            for entry in entries:
                preview = entry.content.strip().splitlines()[0][:80] if entry.content.strip() else ""
                tag = f" ({entry.source})" if entry.source else ""
                lines.append(f"  • **{entry.key}**{tag} — {preview}")
            lines.append("")
            lines.append(f"_{len(entries)} key-value memories_")
        elif not agents_found:
            return CommandResult(
                success=True,
                message="No memories stored yet.\n"
                "Use `/remember` to distill conversation history, or `/memory save <key> <content>` to save directly.",
            )

        return CommandResult(success=True, message="\n".join(lines).strip())

    def _search_memories(self, store: "MemoryStore", query: str) -> CommandResult:
        """Search across active AGENTS.md files and MemoryStore entries."""
        if not query:
            return CommandResult(success=False, message="Usage: /memory search <query>")

        q_lower = query.lower()
        results: list[str] = []

        # Search AGENTS.md files
        from opscloud.memory.registry import MemoryRegistry

        try:
            paths = MemoryRegistry.get_instance().get_memory_paths_for_scope("auto")
            for path in paths:
                if path.is_file():
                    content = path.read_text(encoding="utf-8")
                    matching_lines = [
                        line.strip()
                        for line in content.splitlines()
                        if q_lower in line.lower()
                    ]
                    if matching_lines:
                        results.append(f"**{path}**:")
                        for line in matching_lines[:5]:
                            results.append(f"   • {line}")
                        if len(matching_lines) > 5:
                            results.append(f"   *... and {len(matching_lines) - 5} more matches*")
        except Exception:
            pass

        # Search MemoryStore entries
        for entry in store.list_all():
            if q_lower in entry.key.lower() or q_lower in entry.content.lower():
                preview = entry.content.strip().splitlines()[0][:80] if entry.content.strip() else ""
                results.append(f"• **{entry.key}** ({entry.source}) — {preview}")

        if not results:
            return CommandResult(
                success=True,
                message=f"No memories found matching query `{query}`.",
            )

        output = ["**Memory Search Results:**", ""] + results
        return CommandResult(success=True, message="\n".join(output))

    def _get_memory(self, store: "MemoryStore", key: str) -> CommandResult:
        entry = store.get(key)
        if entry is None:
            return CommandResult(success=False, message=f"Memory `{key}` not found.")
        return CommandResult(
            success=True,
            message=f"**Memory: {entry.key}** ({entry.source})\n\n{entry.content}",
        )

    def _usage(self) -> CommandResult:
        return CommandResult(
            success=True,
            message=(
                "**Usage:**\n"
                "  `/memory`                        — extract learnings from conversation\n"
                "  `/memory show`                   — list active memories & AGENTS.md\n"
                "  `/memory search <query>`         — search persisted memories\n"
                "  `/memory get <key>`              — view a specific memory\n"
                "  `/memory save <key> <text>`      — save a memory directly\n"
                "  `/memory delete <key>`           — delete a memory\n"
                "  `/memory clear`                  — delete all stored memories\n"
                "  `/remember`                      — extract learnings from conversation\n"
                "  `/remember <instruction>`        — extract learnings with explicit focus"
            ),
        )


def _detect_project_root() -> Path | None:
    """Walk up from cwd to find a project root (has .git or .opscloud)."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists() or (parent / ".opscloud").exists():
            return parent
    return None
