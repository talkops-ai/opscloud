"""Version information and lightweight constants for opscloud."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__: str = _pkg_version("talkops-opscloud")
except PackageNotFoundError:
    try:
        __version__ = _pkg_version("opscloud")
    except PackageNotFoundError:
        __version__ = "0.1.0-dev"

DOCS_URL = "https://github.com/talkops-ai/opscloud"
CHANGELOG_URL = "https://github.com/talkops-ai/opscloud/blob/main/CHANGELOG.md"
USER_AGENT = f"opscloud/{__version__}"
