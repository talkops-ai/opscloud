"""Approval modes for OpsCloud.

Re-exports canonical definitions and store/acknowledgement utilities from `opscloud.security.approval_mode`.
"""

from opscloud.security.approval_mode import (
    APPROVAL_MODE_NAMESPACE,
    AUTO_NOTICE_VERSION,
    ApprovalMode,
    ApprovalModePayload,
    approval_mode_key,
    approval_mode_payload,
    approval_mode_scope,
    approval_state_path,
    aread_approval_mode_from_store,
    awrite_approval_mode,
    coerce_approval_mode,
    get_approval_mode,
    has_auto_mode_notice,
    next_approval_mode,
    read_approval_mode_from_store,
    save_auto_mode_notice,
    set_approval_mode,
)

__all__ = [
    "APPROVAL_MODE_NAMESPACE",
    "AUTO_NOTICE_VERSION",
    "ApprovalMode",
    "ApprovalModePayload",
    "approval_mode_key",
    "approval_mode_payload",
    "approval_mode_scope",
    "approval_state_path",
    "aread_approval_mode_from_store",
    "awrite_approval_mode",
    "coerce_approval_mode",
    "get_approval_mode",
    "has_auto_mode_notice",
    "next_approval_mode",
    "read_approval_mode_from_store",
    "save_auto_mode_notice",
    "set_approval_mode",
]
