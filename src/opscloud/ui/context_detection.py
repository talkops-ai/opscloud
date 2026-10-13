"""Production-context detection shared by the status bar and infra panel.

One definition of "production": a name counts when ``prod``, ``production``
or ``prd`` appears as a whole token, delimited by the start/end of the
string or one of ``- _ . / : @`` or a space. That lights ``client-prod``,
``acme_production`` and ``prd-eu`` but not ``product-dev``, ``reproduce``
or ``preprod-test``.

Git branches are deliberately not consulted: ``main`` is not production,
and a branch like ``prod-fix`` would raise a false alarm.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

ProductionSource = Literal["aws", "k8s", "tf"]

_PROD_RE = re.compile(r"(?:^|[-_./:@ ])(prod|production|prd)(?:$|[-_./:@ ])", re.IGNORECASE)

_CURRENT_CONTEXT_RE = re.compile(r"^current-context:\s*(.*?)\s*$", re.MULTILINE)


def is_production_name(value: str | None) -> bool:
    """Whether *value* names a production environment.

    Args:
        value: A profile, context or workspace name; ``None`` / empty is never prod.
    """
    return bool(value) and _PROD_RE.search(value) is not None


def detect_production(
    *, aws_profile: str = "", k8s_context: str = "", tf_workspace: str = ""
) -> ProductionSource | None:
    """Return which source flagged production (``'aws'``, ``'k8s'``, ``'tf'``) or ``None``.

    Sources are checked in that order, so the AWS profile wins when several match.
    """
    if is_production_name(aws_profile):
        return "aws"
    if is_production_name(k8s_context):
        return "k8s"
    if is_production_name(tf_workspace):
        return "tf"
    return None


def _kubeconfig_path() -> Path:
    """First file in ``$KUBECONFIG``, else ``~/.kube/config``."""
    env = os.environ.get("KUBECONFIG", "")
    first = next((p for p in env.split(os.pathsep) if p), "")
    return Path(first).expanduser() if first else Path.home() / ".kube" / "config"


def current_k8s_context() -> str:
    """Read ``current-context`` from the kubeconfig without spawning ``kubectl``.

    Honours ``$KUBECONFIG_CONTEXT`` first (as the infra panel always has).
    Returns ``""`` when there is no kubeconfig or no current context.
    """
    override = os.environ.get("KUBECONFIG_CONTEXT", "")
    if override:
        return override
    try:
        text = _kubeconfig_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _CURRENT_CONTEXT_RE.search(text)
    if not match:
        return ""
    return match.group(1).strip("\"'")


__all__ = [
    "ProductionSource",
    "current_k8s_context",
    "detect_production",
    "is_production_name",
]
