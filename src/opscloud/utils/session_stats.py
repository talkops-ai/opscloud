"""Lightweight session statistics and token formatting utilities for OpsCloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SpinnerStatus = (
    Literal[
        "Thinking",
        "Offloading",
        "Loading thread",
        "Drafting acceptance criteria",
    ]
    | None
)


@dataclass
class ModelStats:
    """Token stats for a single model within a session."""

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    model_name: str = ""


ModelStatsKey = tuple[str, str]


@dataclass
class SessionStats:
    """Stats accumulated over a single agent turn (or full session)."""

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    wall_time_seconds: float = 0.0
    per_model: dict[ModelStatsKey, ModelStats] = field(default_factory=dict)

    def record_request(
        self,
        model_name: str,
        input_toks: int,
        output_toks: int,
        provider: str = "",
        *,
        cost_usd: float | None = None,
    ) -> None:
        """Accumulate usage for one completed LLM request."""
        self.request_count += 1
        self.input_tokens += input_toks
        self.output_tokens += output_toks
        if cost_usd is not None:
            self.total_cost_usd += cost_usd
        if model_name:
            key = (provider, model_name)
            entry = self.per_model.setdefault(
                key,
                ModelStats(provider=provider, model_name=model_name),
            )
            entry.request_count += 1
            entry.input_tokens += input_toks
            entry.output_tokens += output_toks
            if cost_usd is not None:
                entry.cost_usd += cost_usd

    def merge(self, other: SessionStats) -> None:
        """Merge another SessionStats into this one."""
        self.request_count += other.request_count
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_cost_usd += other.total_cost_usd
        self.wall_time_seconds += other.wall_time_seconds
        for key, ms in other.per_model.items():
            entry = self.per_model.setdefault(
                key,
                ModelStats(provider=ms.provider, model_name=ms.model_name),
            )
            entry.request_count += ms.request_count
            entry.input_tokens += ms.input_tokens
            entry.output_tokens += ms.output_tokens
            entry.cost_usd += ms.cost_usd


def format_token_count(count: int) -> str:
    """Format a token count into a human-readable short string."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


def format_cost(cost_usd: float) -> str:
    """Format an estimated USD cost for compact display."""
    if cost_usd <= 0:
        return "$0.00"
    if cost_usd < 0.01:
        return "<$0.01"
    return f"${cost_usd:.2f}"


__all__ = [
    "ModelStats",
    "ModelStatsKey",
    "SessionStats",
    "SpinnerStatus",
    "format_cost",
    "format_token_count",
]
