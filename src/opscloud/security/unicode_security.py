"""Unicode security helpers for deceptive text and URL checks in OpsCloud."""

from __future__ import annotations

import ipaddress
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_DANGEROUS_CODEPOINTS: frozenset[int] = frozenset(
    {
        # BiDi directional formatting controls (embeddings, overrides, pop)
        *range(0x202A, 0x202F),
        # BiDi isolate controls (isolates, pop isolate)
        *range(0x2066, 0x206A),
        # Zero-width and invisible formatting controls
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x2060,  # WORD JOINER
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        # Other commonly abused invisible controls
        0x00AD,  # SOFT HYPHEN
        0x034F,  # COMBINING GRAPHEME JOINER
        0x115F,  # HANGUL CHOSEONG FILLER
        0x1160,  # HANGUL JUNGSEONG FILLER
    }
)

_DANGEROUS_CHARACTERS: frozenset[str] = frozenset(
    chr(codepoint) for codepoint in _DANGEROUS_CODEPOINTS
)

CONFUSABLES: dict[str, str] = {
    # Cyrillic
    "\u0430": "a",
    "\u0435": "e",
    "\u043e": "o",
    "\u0440": "p",
    "\u0441": "c",
    "\u0443": "y",
    "\u0445": "x",
    "\u043d": "h",
    "\u0456": "i",
    "\u0458": "j",
    "\u043a": "k",
    "\u0455": "s",
    # Greek
    "\u03b1": "a",
    "\u03b5": "e",
    "\u03bf": "o",
    "\u03c1": "p",
    "\u03c7": "x",
    "\u03ba": "k",
    "\u03bd": "v",
    "\u03c4": "t",
    # Armenian
    "\u0570": "h",
    "\u0578": "n",
    "\u057d": "u",
    # Fullwidth Latin
    "\uff41": "a",
    "\uff45": "e",
    "\uff4f": "o",
}

URL_ARG_KEYS: frozenset[str] = frozenset(
    {"url", "uri", "href", "link", "base_url", "endpoint"}
)

_URL_SAFE_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class UnicodeIssue:
    position: int
    character: str
    codepoint: str
    name: str

    def __post_init__(self) -> None:
        if len(self.character) != 1:
            msg = f"character must be a single code point, got length {len(self.character)}"
            raise ValueError(msg)
        expected = f"U+{ord(self.character):04X}"
        if self.codepoint != expected:
            msg = f"codepoint {self.codepoint!r} does not match character (expected {expected})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UrlSafetyResult:
    safe: bool
    decoded_domain: str | None
    warnings: tuple[str, ...]
    issues: tuple[UnicodeIssue, ...]


@dataclass(frozen=True)
class UnicodeSanitizeResult:
    """Result of sanitizing input text."""

    sanitized_text: str
    removed_characters_count: int
    has_bidi_controls: bool
    has_invisible_chars: bool


def detect_dangerous_unicode(text: str) -> list[UnicodeIssue]:
    issues: list[UnicodeIssue] = []
    for position, character in enumerate(text):
        if character not in _DANGEROUS_CHARACTERS:
            continue
        issues.append(
            UnicodeIssue(
                position=position,
                character=character,
                codepoint=_format_codepoint(character),
                name=_unicode_name(character),
            )
        )
    return issues


def strip_dangerous_unicode(text: str) -> str:
    return "".join(ch for ch in text if ch not in _DANGEROUS_CHARACTERS)


def sanitize_control_chars(
    text: str,
    *,
    keep_newlines: bool = False,
    collapse_whitespace: bool = True,
    max_length: int | None = None,
) -> str:
    allowed = {" ", "\n"} if keep_newlines else {" "}
    cleaned = "".join(
        ch if ch in allowed or not unicodedata.category(ch).startswith("C") else " "
        for ch in strip_dangerous_unicode(text)
    )
    if collapse_whitespace:
        if keep_newlines:
            cleaned = "\n".join(" ".join(line.split()) for line in cleaned.split("\n"))
        else:
            cleaned = " ".join(cleaned.split())
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + "…"
    return cleaned


def render_with_unicode_markers(text: str) -> str:
    rendered_parts: list[str] = []
    for character in text:
        if character not in _DANGEROUS_CHARACTERS:
            rendered_parts.append(character)
            continue
        rendered_parts.append(
            f"<{_format_codepoint(character)} {_unicode_name(character)}>"
        )
    return "".join(rendered_parts)


def summarize_issues(issues: list[UnicodeIssue], *, max_items: int = 3) -> str:
    unique_entries: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        entry = f"{issue.codepoint} {issue.name}"
        if entry in seen:
            continue
        seen.add(entry)
        unique_entries.append(entry)

    if len(unique_entries) <= max_items:
        return ", ".join(unique_entries)

    displayed = ", ".join(unique_entries[:max_items])
    remainder = len(unique_entries) - max_items
    suffix = "entry" if remainder == 1 else "entries"
    return f"{displayed}, +{remainder} more {suffix}"


def format_warning_detail(warnings: tuple[str, ...], *, max_shown: int = 2) -> str:
    shown = warnings[:max_shown]
    detail = "; ".join(shown)
    remaining = len(warnings) - max_shown
    if remaining > 0:
        detail += f"; +{remaining} more"
    return detail


def sanitize_unicode(text: str) -> UnicodeSanitizeResult:
    """Sanitize potentially deceptive unicode characters from command or prompt."""
    if not text:
        return UnicodeSanitizeResult(text, 0, False, False)

    removed = 0
    has_bidi = False
    has_invisible = False
    cleaned_chars: list[str] = []

    for char in text:
        cp = ord(char)
        if cp in _DANGEROUS_CODEPOINTS:
            removed += 1
            if (0x202A <= cp <= 0x202E) or (0x2066 <= cp <= 0x2069):
                has_bidi = True
            else:
                has_invisible = True
        else:
            cleaned_chars.append(char)

    sanitized = unicodedata.normalize("NFC", "".join(cleaned_chars))
    return UnicodeSanitizeResult(sanitized, removed, has_bidi, has_invisible)


def normalize_confusables(text: str) -> str:
    """Replace visually confusable homoglyphs with their ASCII equivalents."""
    return "".join(CONFUSABLES.get(c, c) for c in text)


def check_url_safety(url: str) -> UrlSafetyResult:
    warnings: list[str] = []
    suspicious = False

    issues = detect_dangerous_unicode(url)
    if issues:
        suspicious = True
        warnings.append(
            f"URL contains hidden Unicode characters ({summarize_issues(issues)})"
        )

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return UrlSafetyResult(
            safe=not suspicious,
            decoded_domain=None,
            warnings=tuple(warnings),
            issues=tuple(issues),
        )

    decoded_hostname, failed_punycode = _decode_hostname(hostname)
    decoded_domain = decoded_hostname if decoded_hostname != hostname else None
    if decoded_domain:
        warnings.append(f"Punycode domain decodes to '{decoded_domain}'")
    if failed_punycode:
        suspicious = True
        labels = ", ".join(failed_punycode)
        warnings.append(f"Punycode label(s) could not be decoded: {labels}")

    if _is_local_or_ip_hostname(decoded_hostname):
        return UrlSafetyResult(
            safe=not suspicious,
            decoded_domain=decoded_domain,
            warnings=tuple(warnings),
            issues=tuple(issues),
        )

    for label in _split_hostname_labels(decoded_hostname):
        scripts = _scripts_in_label(label)
        if len(scripts) > 1:
            suspicious = True
            script_names = ", ".join(sorted(scripts))
            warnings.append(f"Domain label '{label}' mixes scripts ({script_names})")

        if _label_has_suspicious_confusable_mix(label):
            suspicious = True
            warnings.append(
                f"Domain label '{label}' contains confusable Unicode characters"
            )

    return UrlSafetyResult(
        safe=not suspicious,
        decoded_domain=decoded_domain,
        warnings=tuple(warnings),
        issues=tuple(issues),
    )


def _decode_hostname(hostname: str) -> tuple[str, list[str]]:
    decoded_labels: list[str] = []
    failed_labels: list[str] = []
    for label in _split_hostname_labels(hostname):
        if label.startswith("xn--"):
            try:
                decoded_labels.append(label.encode("ascii").decode("idna"))
            except UnicodeError:
                decoded_labels.append(label)
                failed_labels.append(label)
            continue
        decoded_labels.append(label)
    return ".".join(decoded_labels), failed_labels


def _split_hostname_labels(hostname: str) -> list[str]:
    return [label for label in hostname.split(".") if label]


def _is_local_or_ip_hostname(hostname: str) -> bool:
    host = hostname.strip().rstrip(".")
    if not host:
        return False

    if host.lower() in _URL_SAFE_LOCAL_HOSTS:
        return True

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _scripts_in_label(label: str) -> set[str]:
    scripts: set[str] = set()
    for character in label:
        script = _char_script(character)
        if script in {"Common", "Inherited"}:
            continue
        scripts.add(script)
    return scripts


def _label_has_suspicious_confusable_mix(label: str) -> bool:
    if not any(character in CONFUSABLES for character in label):
        return False

    scripts = _scripts_in_label(label)
    return len(scripts) > 1


def _char_script(character: str) -> str:
    name = unicodedata.name(character, "")
    category = unicodedata.category(character)

    if "FULLWIDTH LATIN" in name:
        return "Fullwidth"
    if "LATIN" in name:
        return "Latin"
    if "CYRILLIC" in name:
        return "Cyrillic"
    if "GREEK" in name:
        return "Greek"
    if "ARMENIAN" in name:
        return "Armenian"
    if any(
        token in name
        for token in (
            "CJK",
            "HIRAGANA",
            "KATAKANA",
            "HANGUL",
            "BOPOMOFO",
            "IDEOGRAPHIC",
        )
    ):
        return "EastAsian"

    if category.startswith("M"):
        return "Inherited"
    if category[0] in {"N", "P", "S", "Z", "C"}:
        return "Common"

    return "Other"


def _format_codepoint(character: str) -> str:
    return f"U+{ord(character):04X}"


def _unicode_name(character: str) -> str:
    return unicodedata.name(character, "UNKNOWN CHARACTER")


def iter_string_values(
    data: dict[str, Any],
    *,
    prefix: str = "",
) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for key, value in data.items():
        key_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, str):
            values.append((key_path, value))
            continue
        if isinstance(value, dict):
            values.extend(iter_string_values(value, prefix=key_path))
            continue
        if isinstance(value, list):
            values.extend(_iter_string_values_from_list(value, prefix=key_path))
    return values


def _iter_string_values_from_list(
    values: list[Any],
    *,
    prefix: str,
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        key_path = f"{prefix}[{index}]"
        if isinstance(value, str):
            entries.append((key_path, value))
            continue
        if isinstance(value, dict):
            entries.extend(iter_string_values(value, prefix=key_path))
            continue
        if isinstance(value, list):
            entries.extend(_iter_string_values_from_list(value, prefix=key_path))
    return entries


def looks_like_url_key(arg_path: str) -> bool:
    key = arg_path.rsplit(".", maxsplit=1)[-1]
    key = key.split("[", maxsplit=1)[0].lower()
    return key in URL_ARG_KEYS


__all__ = [
    "CONFUSABLES",
    "URL_ARG_KEYS",
    "UnicodeIssue",
    "UnicodeSanitizeResult",
    "UrlSafetyResult",
    "check_url_safety",
    "detect_dangerous_unicode",
    "format_warning_detail",
    "iter_string_values",
    "looks_like_url_key",
    "normalize_confusables",
    "render_with_unicode_markers",
    "sanitize_control_chars",
    "sanitize_unicode",
    "strip_dangerous_unicode",
    "summarize_issues",
]
