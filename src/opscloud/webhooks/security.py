"""Security verification, signature validation, SSRF protection, and alert storm deduplication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import hmac
import re
import threading
import time
from urllib.parse import urlparse

_SNS_HOST_PATTERN = re.compile(
    r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$",
    re.IGNORECASE,
)

_DEFAULT_TOKEN_HEADERS: tuple[str, ...] = (
    "x-webhook-token",
    "x-opscloud-token",
    "x-jenkins-token",
    "x-gitlab-token",
    "x-api-key",
)


def verify_hmac_sha256(
    raw_body: bytes,
    secret: str,
    signature_header: str | None,
    *,
    prefix: str = "sha256=",
) -> bool:
    """Verify an HMAC-SHA256 signature over the raw request bytes in constant time."""
    if not secret or not signature_header:
        return False

    sig = signature_header.strip()
    if prefix and sig.lower().startswith(prefix.lower()):
        sig = sig[len(prefix) :].strip()

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), sig.lower())


def verify_pagerduty_signature(
    raw_body: bytes,
    secret: str,
    signature_header: str | None,
) -> bool:
    """Verify PagerDuty v3 webhook signature (`X-PagerDuty-Signature: v1=<hex>,...`)."""
    if not secret or not signature_header:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest().lower()
    candidates = [part.strip() for part in signature_header.split(",") if part.strip()]
    for candidate in candidates:
        if candidate.startswith("v1="):
            digest = candidate[3:].strip().lower()
            if hmac.compare_digest(expected, digest):
                return True
    return False


def verify_bearer_or_header_token(
    headers: Mapping[str, str],
    expected_token: str,
    *,
    header_names: Sequence[str] = _DEFAULT_TOKEN_HEADERS,
) -> bool:
    """Verify a shared secret passed via Authorization Bearer header or custom token headers."""
    if not expected_token:
        return False

    lower_headers = {k.lower(): v for k, v in headers.items()}

    # 1. Check Authorization: Bearer <token> or Authorization: <token>
    auth_val = lower_headers.get("authorization", "").strip()
    if auth_val:
        if auth_val.lower().startswith("bearer "):
            token_candidate = auth_val[7:].strip()
        else:
            token_candidate = auth_val
        if hmac.compare_digest(expected_token, token_candidate):
            return True

    # 2. Check provider-specific token headers
    for name in header_names:
        candidate = lower_headers.get(name.lower(), "").strip()
        if candidate and hmac.compare_digest(expected_token, candidate):
            return True

    return False


def verify_request_signature(
    provider: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    secret: str | None,
) -> bool:
    """Unified signature/token verifier across webhook providers.

    If `secret` is None (not configured for this provider), returns True so
    operators can run in internal VPCs without mandatory HMAC setup, while
    enforcing strict verification whenever a secret is configured.
    """
    if not secret:
        return True

    lower_headers = {k.lower(): v for k, v in headers.items()}
    norm_provider = provider.lower().strip()

    if norm_provider == "pagerduty":
        pd_sig = lower_headers.get("x-pagerduty-signature")
        if pd_sig:
            return verify_pagerduty_signature(raw_body, secret, pd_sig)
        return verify_bearer_or_header_token(lower_headers, secret)

    if norm_provider == "github":
        gh_sig = lower_headers.get("x-hub-signature-256")
        if gh_sig:
            return verify_hmac_sha256(raw_body, secret, gh_sig, prefix="sha256=")
        return verify_bearer_or_header_token(lower_headers, secret)

    # Generic HMAC headers (`X-Signature-256`, `X-Hub-Signature-256`, `X-Webhook-Signature`)
    for hmac_header in ("x-signature-256", "x-hub-signature-256", "x-webhook-signature", "x-opscloud-signature"):
        sig_val = lower_headers.get(hmac_header)
        if sig_val:
            prefix = "sha256=" if sig_val.lower().startswith("sha256=") else ""
            if verify_hmac_sha256(raw_body, secret, sig_val, prefix=prefix):
                return True

    return verify_bearer_or_header_token(lower_headers, secret)


def is_valid_sns_subscribe_url(url: str) -> bool:
    """Validate that an AWS SNS SubscribeURL points to a genuine HTTPS AWS SNS endpoint (SSRF guard)."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return bool(_SNS_HOST_PATTERN.match(hostname))


class DeduplicationCache:
    """Thread-safe TTL cache to suppress duplicate webhook retries and alert storms."""

    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 4096) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str, *, now: float | None = None) -> bool:
        """Return True if `key` is a duplicate within TTL; False if new (and record it)."""
        if self.ttl_seconds <= 0:
            return False

        ts = time.monotonic() if now is None else now
        with self._lock:
            self._evict_expired(ts)
            exp = self._store.get(key)
            if exp is not None and exp > ts:
                return True
            if len(self._store) >= self.max_entries:
                # Evict oldest entry
                oldest_key = min(self._store, key=self._store.__getitem__)
                self._store.pop(oldest_key, None)
            self._store[key] = ts + self.ttl_seconds
            return False

    def clear(self) -> None:
        """Clear all recorded keys."""
        with self._lock:
            self._store.clear()

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, exp in self._store.items() if exp <= now]
        for k in expired:
            self._store.pop(k, None)
