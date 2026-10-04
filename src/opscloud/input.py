"""Input handling utilities including @file mention parsing and path resolution."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import re
from pathlib import Path

logger = get_logger(__name__)

PATH_CHAR_CLASS = r"A-Za-z0-9._~/\\:-"
FILE_MENTION_PATTERN = re.compile(r"@(?P<path>(?:\\.|[" + PATH_CHAR_CLASS + r"])+)")
EMAIL_PREFIX_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]$")


def parse_file_mentions(text: str) -> tuple[str, list[Path]]:
    """Extract `@file` mentions from input text and return resolved file paths.

    Args:
        text: Input text potentially containing `@file` mentions.

    Returns:
        Tuple of (original text unchanged, list of resolved file paths that exist).
    """
    matches = FILE_MENTION_PATTERN.finditer(text)
    files: list[Path] = []

    for match in matches:
        text_before = text[: match.start()]
        if text_before and EMAIL_PREFIX_PATTERN.search(text_before):
            continue

        raw_path = match.group("path")
        clean_path = raw_path.replace("\\ ", " ")

        try:
            path = Path(clean_path).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path

            resolved = path.resolve()
            if resolved.exists() and resolved.is_file():
                if resolved not in files:
                    files.append(resolved)
        except (OSError, RuntimeError) as e:
            logger.debug("Path resolution failed for %s: %s", raw_path, e)

    return text, files
