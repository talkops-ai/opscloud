"""Subagent branch memory storage for isolated learning persistence."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from opscloud.config.settings import settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class BranchMemoryEntry:
    run_id: str
    subagent_name: str
    content: str
    path: Path
    created_at: float


class BranchMemoryStore:
    """Manages branch memory files under .opscloud/memories/{subagent_name}-{run_id}.md"""

    def __init__(
        self,
        subagent_name: str,
        *,
        run_id: str | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.subagent_name = subagent_name
        self.run_id = run_id or uuid.uuid4().hex[:8]
        root = project_root or settings.project_root or Path.cwd()
        self.memories_dir = root / ".opscloud" / "memories"
        self.branch_file = self.memories_dir / f"{self.subagent_name}-{self.run_id}.md"

    def write_observation(self, content: str) -> Path:
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry_text = f"\n\n### [{timestamp}] Observation ({self.subagent_name})\n{content.strip()}"

        if not self.branch_file.exists():
            header = f"# Branch Memory: {self.subagent_name} (Run ID: {self.run_id})\nCreated at: {timestamp}"
            self.branch_file.write_text(header + entry_text, encoding="utf-8")
        else:
            with self.branch_file.open("a", encoding="utf-8") as f:
                f.write(entry_text)

        return self.branch_file

    def get_content(self) -> str:
        if self.branch_file.is_file():
            try:
                return self.branch_file.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""
