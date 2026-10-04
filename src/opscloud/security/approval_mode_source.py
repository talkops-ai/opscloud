"""Unified approval-mode resolution and security policy evaluation for OpsCloud."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opscloud.security.approval_mode import (
    ApprovalMode,
    approval_mode_key,
    aread_approval_mode_from_store,
    coerce_approval_mode,
    read_approval_mode_from_store,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ApprovalPolicyResolver",
    "_DecidedMode",
    "_LiveLookup",
    "_approval_mode_source",
    "_aresolve_approval_mode",
    "_resolve_approval_mode",
]


@dataclass(frozen=True)
class _DecidedMode:
    """Context-only approval decision — no store lookup required."""

    mode: ApprovalMode


@dataclass(frozen=True)
class _LiveLookup:
    """A trusted Store key whose record must be read, failing closed to Manual."""

    key: str


class ApprovalPolicyResolver:
    """Evaluates execution context to determine active approval constraints."""

    @staticmethod
    def validate_store_key(raw_key: str, thread_id: object) -> str | None:
        """Validate that *raw_key* matches the canonical key for *thread_id*."""
        if not raw_key:
            return None
        if not isinstance(thread_id, str) or not thread_id:
            return raw_key
        expected = approval_mode_key(thread_id)
        if raw_key == expected:
            return raw_key
        logger.warning(
            "Approval-mode Store key %r does not match expected key for thread %r",
            raw_key,
            thread_id,
        )
        return None

    @classmethod
    def _extract_explicit_mode(cls, context: object) -> ApprovalMode | None:
        """Extract explicit approval mode specified in context without defaulting."""
        if isinstance(context, Mapping):
            if context.get("smart") is True:
                return ApprovalMode.SMART
            raw = context.get("approval_mode")
            if raw:
                return coerce_approval_mode(raw)
            if context.get("auto_approve") is True:
                return ApprovalMode.AUTO
            cfg = context.get("configurable")
            if isinstance(cfg, Mapping):
                if cfg.get("smart") is True:
                    return ApprovalMode.SMART
                raw_cfg = cfg.get("approval_mode")
                if raw_cfg:
                    return coerce_approval_mode(raw_cfg)
                if cfg.get("auto_approve") is True:
                    return ApprovalMode.AUTO
        elif context is not None:
            if getattr(context, "smart", None) is True:
                return ApprovalMode.SMART
            raw = getattr(context, "approval_mode", None)
            if raw:
                return coerce_approval_mode(raw)
            if getattr(context, "auto_approve", None) is True:
                return ApprovalMode.AUTO
        return None

    @classmethod
    def resolve_source(cls, context: object) -> _DecidedMode | _LiveLookup:
        """Extract approval mode from invocation context (dataclass, dict, or CLI args)."""
        raw_key: object = None
        thread_id: object = None
        raw_mode: object = None
        legacy_auto: object = None
        has_typed_mode = False

        if isinstance(context, (Mapping, dict)):
            raw_key = context.get("approval_mode_key")
            thread_id = context.get("thread_id") or context.get("context_id") or context.get("session_id")
            raw_mode = context.get("approval_mode")
            legacy_auto = context.get("auto_approve")
            if context.get("smart") is True:
                return _DecidedMode(ApprovalMode.SMART)

            cfg = context.get("configurable")
            if isinstance(cfg, (Mapping, dict)):
                raw_key = raw_key or cfg.get("approval_mode_key")
                thread_id = thread_id or cfg.get("thread_id") or cfg.get("context_id")
                raw_mode = raw_mode or cfg.get("approval_mode")
                if legacy_auto is None:
                    legacy_auto = cfg.get("auto_approve")
                if cfg.get("smart") is True:
                    return _DecidedMode(ApprovalMode.SMART)

            meta = context.get("metadata")
            if isinstance(meta, (Mapping, dict)):
                raw_key = raw_key or meta.get("approval_mode_key")
                thread_id = thread_id or meta.get("thread_id") or meta.get("context_id")
                raw_mode = raw_mode or meta.get("approval_mode")
                if legacy_auto is None:
                    legacy_auto = meta.get("auto_approve")

            ctx_dict = context.get("context")
            if isinstance(ctx_dict, (Mapping, dict)):
                raw_key = raw_key or ctx_dict.get("approval_mode_key")
                thread_id = thread_id or ctx_dict.get("thread_id") or ctx_dict.get("context_id")
                raw_mode = raw_mode or ctx_dict.get("approval_mode")
                if legacy_auto is None:
                    legacy_auto = ctx_dict.get("auto_approve")

            has_typed_mode = raw_mode is not None
        elif context is not None:
            raw_key = getattr(context, "approval_mode_key", None)
            thread_id = (
                getattr(context, "thread_id", None)
                or getattr(context, "context_id", None)
                or getattr(context, "session_id", None)
            )
            raw_mode = getattr(context, "approval_mode", None)
            legacy_auto = getattr(context, "auto_approve", None)
            if getattr(context, "smart", None) is True:
                return _DecidedMode(ApprovalMode.SMART)
            if legacy_auto is True and not raw_mode:
                return _DecidedMode(ApprovalMode.AUTO)
            has_typed_mode = isinstance(raw_mode, (str, ApprovalMode))
        else:
            return _DecidedMode(ApprovalMode.MANUAL)

        # 1. Store key present -> live store lookup
        if raw_key is not None:
            if not isinstance(raw_key, str) or not raw_key:
                logger.warning("Approval-mode Store key is malformed")
                return _DecidedMode(ApprovalMode.MANUAL)
            key = cls.validate_store_key(raw_key, thread_id)
            if key is None:
                return _DecidedMode(ApprovalMode.MANUAL)
            return _LiveLookup(key)

        # 2. Explicit typed mode present
        if has_typed_mode and raw_mode:
            requested = coerce_approval_mode(raw_mode)
            if isinstance(thread_id, str) and thread_id:
                return _LiveLookup(approval_mode_key(thread_id))
            return _DecidedMode(requested)

        # 3. Thread ID available -> live store lookup
        if isinstance(thread_id, str) and thread_id:
            return _LiveLookup(approval_mode_key(thread_id))

        # 4. Legacy auto_approve flag fallback
        if legacy_auto is True:
            return _DecidedMode(ApprovalMode.AUTO)
        return _DecidedMode(ApprovalMode.MANUAL)

    @classmethod
    def resolve_sync(cls, context: object = None, store: object = None) -> ApprovalMode:
        """Resolve approval mode synchronously using local store or context."""
        source = cls.resolve_source(context)
        if isinstance(source, _DecidedMode):
            return source.mode
        mode = read_approval_mode_from_store(store, source.key)
        if mode is not None:
            return mode
        context_mode = cls._extract_explicit_mode(context)
        if context_mode is not None:
            return context_mode
        return ApprovalMode.MANUAL

    @classmethod
    async def resolve_async(cls, context: object = None, store: object = None) -> ApprovalMode:
        """Resolve approval mode asynchronously using server store or context."""
        source = cls.resolve_source(context)
        if isinstance(source, _DecidedMode):
            return source.mode
        mode = await aread_approval_mode_from_store(store, source.key)
        if mode is not None:
            return mode
        context_mode = cls._extract_explicit_mode(context)
        if context_mode is not None:
            return context_mode
        return ApprovalMode.MANUAL

    @classmethod
    def resolve_mode_from_context(cls, context: Any = None) -> ApprovalMode:
        """Backwards compatibility method."""
        return cls.resolve_sync(context)


def _validated_live_approval_key(raw_key: str, thread_id: object) -> str | None:
    return ApprovalPolicyResolver.validate_store_key(raw_key, thread_id)


def _approval_mode_source(context: object) -> _DecidedMode | _LiveLookup:
    return ApprovalPolicyResolver.resolve_source(context)


def _resolve_approval_mode(context: Any = None, store: Any = None) -> ApprovalMode:
    """Synchronous resolver for active approval mode."""
    return ApprovalPolicyResolver.resolve_sync(context, store)


async def _aresolve_approval_mode(context: Any = None, store: Any = None) -> ApprovalMode:
    """Asynchronous resolver for active approval mode."""
    return await ApprovalPolicyResolver.resolve_async(context, store)
