#!/usr/bin/env bash
# Install OpsCloud — AI DevOps & Cloud Operations Agent.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- [options]
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- VERSION
#
# Install an exact version:
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | OPSCLOUD_VERSION="0.1.0" bash
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- 0.1.0
#
# Install directly from the GitHub repository:
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | OPSCLOUD_SOURCE="git" bash
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- --git
#
# Install in editable mode from a local checkout:
#   ./scripts/install.sh --local
#
# Uninstall:
#   ./scripts/install.sh --uninstall
#   # Or manually:
#   uv tool uninstall opscloud
#   rm -rf ~/.opscloud
#
# Options:
#   --help, -h          Show this help message and exit
#   --version, -v       Print installer version and exit
#   --yes, -y           Accept all prompts (assume yes, non-interactive)
#   --verbose           Show detailed command outputs and debug logs
#   --source <src>      Install source: "pypi", "git", or "local" (default: auto)
#   --git               Force installation from GitHub git repository
#   --local, --editable Force editable installation from local repository
#   --python <version>  Python version to provision via uv (default: 3.12)
#   --extras <extras>   Comma-separated pip extras (e.g. "dev", "sandboxes")
#   --uninstall         Uninstall opscloud and clean up binaries
#
# Environment variables:
#   OPSCLOUD_PACKAGE_NAME     — Package name on PyPI (default: "talkops-opscloud")
#   OPSCLOUD_REPO_URL         — GitHub repo URL (default: "https://github.com/talkops-ai/opscloud")
#   OPSCLOUD_SOURCE           — "pypi", "git", or "local" (default: auto-detected)
#   OPSCLOUD_VERSION          — Exact version to install (e.g. "0.1.0")
#   OPSCLOUD_EXTRAS           — Comma-separated pip extras (e.g. "dev", "sandboxes")
#   OPSCLOUD_PRERELEASE       — uv pre-release strategy: allow, disallow, if-necessary (default: allow)
#   OPSCLOUD_PYTHON           — Python version to use (default: 3.12)
#   OPSCLOUD_YES              — Set to 1 to accept prompts without asking (assume "yes")
#   OPSCLOUD_RIPGREP_INSTALLER— How to provision ripgrep: "managed" (default), "system", or "skip"
#   OPSCLOUD_OFFLINE          — Set to 1 to skip external downloads
#   OPSCLOUD_SKIP_XCODE_CHECK — Set to 1 to bypass macOS Xcode Command Line Tools check
#   OPSCLOUD_SKIP_DEVOPS_CHECK— Set to 1 to skip checking external DevOps tools (aws, kubectl, etc.)
#   OPSCLOUD_NO_MODIFY_PATH   — Set to 1 to skip modifying shell profile PATH
#   OPSCLOUD_VERBOSE          — Set to 1 to enable verbose output
#   UV_BIN                    — Path to uv binary (auto-detected if unset)

set -euo pipefail

# ── Package & Repository Constants ───────────────────────────

PACKAGE_NAME="${OPSCLOUD_PACKAGE_NAME:-talkops-opscloud}"
PRIMARY_BIN="opscloud"
REPO_URL="${OPSCLOUD_REPO_URL:-https://github.com/talkops-ai/opscloud}"
DEFAULT_PYTHON="3.12"
RIPGREP_VERSION="14.1.1"

# ── Colors & Formatting ──────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  RED=$'\033[0;31m'
  GREEN=$'\033[0;32m'
  YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m'
  MAGENTA=$'\033[0;35m'
  CYAN=$'\033[0;36m'
  NC=$'\033[0m'
else
  BOLD=""
  DIM=""
  RED=""
  GREEN=""
  YELLOW=""
  BLUE=""
  MAGENTA=""
  CYAN=""
  NC=""
fi

log_info()    { printf "%s\n" "$*"; }
log_step()    { printf "\n${BLUE}==>${NC} ${BOLD}%s${NC}\n" "$*"; }
log_success() { printf "${GREEN}✔${NC} %s\n" "$*"; }
log_warn()    { printf "${YELLOW}Warning:${NC} %s\n" "$*" >&2; }
log_error()   { printf "${RED}Error:${NC} %s\n" "$*" >&2; }
log_debug()   { if [ "${VERBOSE:-0}" = "1" ]; then printf "${DIM}[DEBUG] %s${NC}\n" "$*"; fi; }

# ── Help & Version Information ───────────────────────────────

resolve_latest_pypi_version() {
  local latest=""
  if command -v curl >/dev/null 2>&1; then
    local pypi_json
    pypi_json="$(curl -sS --max-time 4 "https://pypi.org/pypi/${PACKAGE_NAME}/json" 2>/dev/null || true)"
    if [ -n "$pypi_json" ]; then
      if command -v python3 >/dev/null 2>&1; then
        latest="$(printf '%s' "$pypi_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('info',{}).get('version',''))" 2>/dev/null || true)"
      elif command -v python >/dev/null 2>&1; then
        latest="$(printf '%s' "$pypi_json" | python -c "import sys,json; print(json.load(sys.stdin).get('info',{}).get('version',''))" 2>/dev/null || true)"
      fi
      if [ -z "$latest" ]; then
        latest="$(printf '%s' "$pypi_json" | grep -o '"version":"[^"]*"' | head -n 1 | cut -d'"' -f4 || true)"
      fi
    fi
  fi
  if [ -z "$latest" ]; then
    latest="0.1.0"
  fi
  printf '%s' "$latest"
}

print_help() {
  cat <<EOF
${BOLD}OpsCloud Installer${NC} — AI DevOps & Cloud Operations Agent

${BOLD}Usage:${NC}
  curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash
  curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- [options]
  curl -LsSf https://raw.githubusercontent.com/talkops-ai/opscloud/main/scripts/install.sh | bash -s -- VERSION

${BOLD}Options:${NC}
  --help, -h          Show this help message and exit
  --version, -v       Print installer version and exit
  --yes, -y           Accept all prompts (assume yes, non-interactive)
  --verbose           Enable verbose output and trace logs
  --source <src>      Install source: "pypi", "git", or "local" (default: auto)
  --git               Force installation from GitHub repository
  --local, --editable Force editable installation from local directory
  --python <ver>      Python version to provision via uv (default: ${DEFAULT_PYTHON})
  --extras <extras>   Comma-separated pip extras (e.g. "dev", "sandboxes")
  --uninstall         Uninstall opscloud and remove binaries

${BOLD}Environment Variables:${NC}
  OPSCLOUD_VERSION          Exact version to install (e.g. "0.1.0")
  OPSCLOUD_SOURCE           "pypi", "git", or "local" (default: auto)
  OPSCLOUD_EXTRAS           Comma-separated pip extras
  OPSCLOUD_PRERELEASE       uv pre-release strategy (default: allow)
  OPSCLOUD_PYTHON           Python version to use (default: ${DEFAULT_PYTHON})
  OPSCLOUD_YES              Set to 1 to accept prompts without asking
  OPSCLOUD_RIPGREP_INSTALLER "managed" (default), "system", or "skip"
  OPSCLOUD_OFFLINE          Set to 1 to skip external downloads
  OPSCLOUD_SKIP_XCODE_CHECK Set to 1 to bypass macOS Xcode check
  OPSCLOUD_SKIP_DEVOPS_CHECK Set to 1 to bypass external DevOps tool checks
  OPSCLOUD_NO_MODIFY_PATH   Set to 1 to skip shell PATH modifications
  OPSCLOUD_VERBOSE          Set to 1 for verbose debug logs
  UV_BIN                    Path to custom uv binary

${BOLD}Repository & Docs:${NC}
  ${REPO_URL}
EOF
}

# ── Argument Parsing ─────────────────────────────────────────

POSITIONAL_VERSION=""
REQUESTED_SOURCE="${OPSCLOUD_SOURCE:-}"
REQUESTED_EXTRAS="${OPSCLOUD_EXTRAS:-}"
REQUESTED_PYTHON="${OPSCLOUD_PYTHON:-$DEFAULT_PYTHON}"
REQUESTED_PRERELEASE="${OPSCLOUD_PRERELEASE:-allow}"
ACTION="install"
ASSUME_YES="${OPSCLOUD_YES:-0}"
VERBOSE="${OPSCLOUD_VERBOSE:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      print_help
      exit 0
      ;;
    --version|-v)
      printf "opscloud-installer %s\n" "$(resolve_latest_pypi_version)"
      exit 0
      ;;
    --yes|-y)
      ASSUME_YES=1
      ;;
    --verbose)
      VERBOSE=1
      ;;
    --source)
      shift
      if [ $# -eq 0 ]; then log_error "--source requires an argument"; exit 1; fi
      REQUESTED_SOURCE="$1"
      ;;
    --git)
      REQUESTED_SOURCE="git"
      ;;
    --local|--editable)
      REQUESTED_SOURCE="local"
      ;;
    --python)
      shift
      if [ $# -eq 0 ]; then log_error "--python requires an argument"; exit 1; fi
      REQUESTED_PYTHON="$1"
      ;;
    --extras)
      shift
      if [ $# -eq 0 ]; then log_error "--extras requires an argument"; exit 1; fi
      REQUESTED_EXTRAS="$1"
      ;;
    --uninstall)
      ACTION="uninstall"
      ;;
    -*)
      log_error "Unknown option: $1"
      print_help >&2
      exit 1
      ;;
    *)
      if [ -z "$POSITIONAL_VERSION" ]; then
        POSITIONAL_VERSION="$1"
      else
        log_error "Unexpected extra argument: $1"
        print_help >&2
        exit 1
      fi
      ;;
  esac
  shift
done

VERSION="${OPSCLOUD_VERSION:-$POSITIONAL_VERSION}"

# ── Temp File & Directory Tracking ───────────────────────────

TEMP_FILES=()
TEMP_DIRS=()

register_temp() { TEMP_FILES+=("$1"); }
register_temp_dir() { TEMP_DIRS+=("$1"); }

cleanup_temp_files() {
  for f in "${TEMP_FILES[@]:-}"; do
    [ -n "$f" ] && [ -f "$f" ] && rm -f "$f" 2>/dev/null || true
  done
}

cleanup_temp_dirs() {
  for d in "${TEMP_DIRS[@]:-}"; do
    [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d" 2>/dev/null || true
  done
}

# ── Lock Management ──────────────────────────────────────────

LOCK_DIR=""

release_install_lock() {
  if [ -n "$LOCK_DIR" ] && [ -d "$LOCK_DIR" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    LOCK_DIR=""
  fi
}

cleanup_on_exit() {
  cleanup_temp_files
  cleanup_temp_dirs
  release_install_lock
}

trap cleanup_on_exit EXIT
trap 'cleanup_on_exit; exit 130' INT TERM HUP

# ── OS & Platform Detection ──────────────────────────────────

OS="unknown"
ARCH="unknown"
IS_WSL=0

detect_platform() {
  local raw_os
  raw_os="$(uname -s 2>/dev/null || echo "unknown")"

  case "$raw_os" in
    Darwin*)
      OS="macos"
      ;;
    Linux*)
      OS="linux"
      if [ -f "/proc/version" ] && grep -qiE "(microsoft|wsl)" "/proc/version" 2>/dev/null; then
        IS_WSL=1
      fi
      ;;
    CYGWIN*|MINGW*|MSYS*)
      OS="windows"
      ;;
    *)
      OS="unknown"
      ;;
  esac

  local raw_arch
  raw_arch="$(uname -m 2>/dev/null || echo "unknown")"

  case "$raw_arch" in
    x86_64|amd64)
      ARCH="x86_64"
      ;;
    aarch64|arm64)
      ARCH="aarch64"
      ;;
    armv7l|armv6l)
      ARCH="arm"
      ;;
    *)
      ARCH="$raw_arch"
      ;;
  esac
}

detect_platform

# Determine safe lock directory across platforms
TMP_ROOT="${TMPDIR:-${TMP:-${TEMP:-/tmp}}}"
USER_ID="$(id -u 2>/dev/null || echo 0)"
LOCK_DIR="${TMP_ROOT}/opscloud-install-${USER_ID}.lock"

acquire_install_lock() {
  local attempts=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 10 ]; then
      log_warn "Existing install lock detected at $LOCK_DIR. Breaking stale lock..."
      rm -rf "$LOCK_DIR" 2>/dev/null || true
      mkdir "$LOCK_DIR" 2>/dev/null || true
      break
    fi
    sleep 1
  done
}

acquire_install_lock

# ── Platform Checks & Guidance ───────────────────────────────

if [ "$OS" = "windows" ]; then
  log_warn "Detected Windows environment (${raw_os:-bash})."
  log_info "For the best experience with Docker, Terraform, and Kubernetes CLIs,"
  log_info "running OpsCloud inside WSL (Windows Subsystem for Linux) is recommended."
  log_info "Proceeding with Git Bash / MSYS compatible installation..."
elif [ "$IS_WSL" = "1" ]; then
  log_debug "Running inside Windows Subsystem for Linux (WSL)."
fi

if [ "$OS" = "macos" ] && [ "${OPSCLOUD_SKIP_XCODE_CHECK:-0}" != "1" ]; then
  if ! xcode-select -p >/dev/null 2>&1; then
    log_warn "macOS Command Line Tools not found."
    log_info "If compilation is required, run: xcode-select --install"
    log_info "To skip this check in the future, set: OPSCLOUD_SKIP_XCODE_CHECK=1"
  fi
fi

# ── Download Helpers ─────────────────────────────────────────

download_to_stdout() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$url"
  else
    log_error "Neither 'curl' nor 'wget' was found on your system."
    log_info "Please install curl or wget using your operating system's package manager."
    exit 1
  fi
}

download_to_file() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf -o "$dest" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" "$url"
  else
    log_error "Neither 'curl' nor 'wget' was found on your system."
    exit 1
  fi
}

# ── Prompt Helper ────────────────────────────────────────────

prompt_yn() {
  local prompt_text="$1"
  local default_ans="${2:-y}"

  if [ "$ASSUME_YES" = "1" ] || [ "$ASSUME_YES" = "true" ]; then
    return 0
  fi

  if [ ! -t 0 ]; then
    # Unattended / non-interactive session
    return 0
  fi

  local choice
  if [ "$default_ans" = "y" ]; then
    printf "%s [Y/n]: " "$prompt_text"
  else
    printf "%s [y/N]: " "$prompt_text"
  fi

  read -r choice || return 1
  choice="$(printf '%s' "$choice" | tr '[:upper:]' '[:lower:]')"

  if [ -z "$choice" ]; then
    choice="$default_ans"
  fi

  case "$choice" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# ── UV Bootstrap & Binary Resolution ─────────────────────────

UV_BIN="${UV_BIN:-}"

resolve_uv_bin() {
  if [ -n "$UV_BIN" ] && [ -x "$UV_BIN" ]; then
    return 0
  fi

  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
    return 0
  fi

  local candidate_paths=(
    "${HOME}/.local/bin/uv"
    "${HOME}/.cargo/bin/uv"
    "/usr/local/bin/uv"
    "/opt/homebrew/bin/uv"
  )

  # Add Windows-specific candidate paths
  if [ "$OS" = "windows" ]; then
    if [ -n "${USERPROFILE:-}" ]; then
      candidate_paths+=(
        "${USERPROFILE}/.cargo/bin/uv.exe"
        "${USERPROFILE}/.local/bin/uv.exe"
        "${USERPROFILE}/AppData/Local/Programs/uv/uv.exe"
      )
    fi
    if [ -n "${LOCALAPPDATA:-}" ]; then
      candidate_paths+=("${LOCALAPPDATA}/Programs/uv/uv.exe")
    fi
  fi

  for p in "${candidate_paths[@]}"; do
    if [ -x "$p" ]; then
      UV_BIN="$p"
      return 0
    fi
  done

  return 1
}

install_uv() {
  log_step "Installing uv (high-performance Python package & environment manager)..."
  local installer_script
  installer_script="$(mktemp "${TMP_ROOT}/uv-install-XXXXXX.sh" 2>/dev/null || mktemp /tmp/uv-install-XXXXXX.sh)"
  register_temp "$installer_script"

  if download_to_stdout "https://astral.sh/uv/install.sh" > "$installer_script"; then
    sh "$installer_script" >/dev/null 2>&1 || sh "$installer_script"
    if resolve_uv_bin; then
      log_success "uv installed successfully at ${UV_BIN}"
      return 0
    fi
  fi

  log_error "Failed to install uv automatically."
  log_info "Please install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run this script."
  exit 1
}

if ! resolve_uv_bin; then
  install_uv
fi

log_debug "Using uv binary: $UV_BIN"

# ── Resolve Tool Binary Directory ────────────────────────────

TOOL_BIN_DIR=""

resolve_tool_bin_dir() {
  if [ -n "${UV_TOOL_BIN_DIR:-}" ]; then
    TOOL_BIN_DIR="${UV_TOOL_BIN_DIR}"
  else
    # Ask uv directly for the configured tool bin directory
    local uv_dir=""
    uv_dir="$("$UV_BIN" tool dir --bin 2>/dev/null || true)"
    if [ -n "$uv_dir" ] && [ -d "$(dirname "$uv_dir")" ]; then
      TOOL_BIN_DIR="$uv_dir"
    elif [ -d "${HOME}/.local/bin" ]; then
      TOOL_BIN_DIR="${HOME}/.local/bin"
    elif [ -d "${HOME}/.cargo/bin" ]; then
      TOOL_BIN_DIR="${HOME}/.cargo/bin"
    else
      TOOL_BIN_DIR="${HOME}/.local/bin"
    fi
  fi

  mkdir -p "$TOOL_BIN_DIR" 2>/dev/null || true
  log_debug "Tool binary directory resolved to: $TOOL_BIN_DIR"
}

resolve_tool_bin_dir

# ── Lifecycle: Uninstall Action ──────────────────────────────

if [ "$ACTION" = "uninstall" ]; then
  log_step "Uninstalling OpsCloud..."
  
  if "$UV_BIN" tool uninstall "$PACKAGE_NAME" >/dev/null 2>&1; then
    log_success "Removed $PACKAGE_NAME uv tool environment."
  else
    log_info "No active uv tool installation found for $PACKAGE_NAME."
  fi

  # Remove installed binaries
  for b in "${TOOL_BIN_DIR}/${PRIMARY_BIN}" "${TOOL_BIN_DIR}/${PRIMARY_BIN}.exe"; do
    if [ -e "$b" ] || [ -L "$b" ]; then
      rm -f "$b" 2>/dev/null || true
      log_info "Removed $b"
    fi
  done

  printf "\n"
  if prompt_yn "Do you also want to remove OpsCloud configuration and state (~/.opscloud)?" "n"; then
    rm -rf "${HOME}/.opscloud" 2>/dev/null || true
    log_success "Removed ~/.opscloud data directory."
  else
    log_info "Preserved user configuration in ~/.opscloud."
  fi

  printf "\n${GREEN}✔${NC} ${BOLD}OpsCloud has been uninstalled.${NC}\n\n"
  exit 0
fi

# ── Check Existing Installation ──────────────────────────────

CURRENT_VERSION=""
for bin_candidate in "$PRIMARY_BIN" "${PRIMARY_BIN}.exe"; do
  if command -v "$bin_candidate" >/dev/null 2>&1; then
    CURRENT_VERSION="$("$bin_candidate" --version 2>/dev/null | awk '{print $NF}' || true)"
    if [ -n "$CURRENT_VERSION" ]; then
      break
    fi
  fi
done

if [ -n "$CURRENT_VERSION" ]; then
  log_info "Existing OpsCloud ${CURRENT_VERSION} detected."
fi

# ── Determine Source Strategy (Local / Git / PyPI) ───────────

RESOLVED_SOURCE=""

if [ "$REQUESTED_SOURCE" = "local" ]; then
  RESOLVED_SOURCE="local"
elif [ "$REQUESTED_SOURCE" = "git" ]; then
  RESOLVED_SOURCE="git"
elif [ "$REQUESTED_SOURCE" = "pypi" ]; then
  RESOLVED_SOURCE="pypi"
else
  # Auto-detection
  if [ -f "pyproject.toml" ] && [ -d "src/opscloud" ] && [ -z "$VERSION" ]; then
    log_info "Detected local OpsCloud repository in current directory."
    RESOLVED_SOURCE="local"
  else
    RESOLVED_SOURCE="pypi"
  fi
fi

# ── Ripgrep Provisioning (Fast Search Acceleration) ──────────

RIPGREP_INSTALLER="${OPSCLOUD_RIPGREP_INSTALLER:-managed}"

provision_ripgrep() {
  if [ "$RIPGREP_INSTALLER" = "skip" ] || [ "${OPSCLOUD_OFFLINE:-0}" = "1" ]; then
    return 0
  fi

  # If ripgrep is already installed and functional, we're done
  if command -v rg >/dev/null 2>&1; then
    log_debug "ripgrep is already installed at $(command -v rg)"
    return 0
  fi

  if [ -x "${TOOL_BIN_DIR}/rg" ] || [ -x "${TOOL_BIN_DIR}/rg.exe" ]; then
    log_debug "ripgrep binary already present in ${TOOL_BIN_DIR}"
    return 0
  fi

  if [ "$RIPGREP_INSTALLER" = "system" ]; then
    log_info "ripgrep not found. To install via system package manager:"
    if [ "$OS" = "macos" ]; then
      log_info "  brew install ripgrep"
    elif [ "$OS" = "linux" ]; then
      log_info "  sudo apt-get install -y ripgrep  # or dnf/pacman"
    fi
    return 0
  fi

  # Managed installation: download official standalone release
  local asset=""
  case "${OS}-${ARCH}" in
    macos-aarch64)
      asset="ripgrep-${RIPGREP_VERSION}-aarch64-apple-darwin.tar.gz"
      ;;
    macos-x86_64)
      asset="ripgrep-${RIPGREP_VERSION}-x86_64-apple-darwin.tar.gz"
      ;;
    linux-x86_64)
      asset="ripgrep-${RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz"
      ;;
    linux-aarch64)
      asset="ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-musl.tar.gz"
      ;;
    linux-arm)
      asset="ripgrep-${RIPGREP_VERSION}-arm-unknown-linux-gnueabihf.tar.gz"
      ;;
    windows-x86_64)
      asset="ripgrep-${RIPGREP_VERSION}-x86_64-pc-windows-msvc.zip"
      ;;
    *)
      log_debug "No pre-built ripgrep asset matched for ${OS}-${ARCH}."
      return 0
      ;;
  esac

  log_step "Provisioning ripgrep ${RIPGREP_VERSION} (fast code & file search)..."
  local rg_url="https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/${asset}"
  local rg_tmp_dir
  rg_tmp_dir="$(mktemp -d "${TMP_ROOT}/rg-download-XXXXXX" 2>/dev/null || mktemp -d /tmp/rg-download-XXXXXX)"
  register_temp_dir "$rg_tmp_dir"

  local archive_path="${rg_tmp_dir}/${asset}"
  if download_to_file "$rg_url" "$archive_path" 2>/dev/null; then
    if [[ "$asset" == *.tar.gz ]]; then
      tar -xzf "$archive_path" -C "$rg_tmp_dir" 2>/dev/null || true
      local found_rg
      found_rg="$(find "$rg_tmp_dir" -type f -name "rg" -perm -111 2>/dev/null | head -n 1)"
      if [ -n "$found_rg" ] && [ -x "$found_rg" ]; then
        cp -f "$found_rg" "${TOOL_BIN_DIR}/rg"
        chmod +x "${TOOL_BIN_DIR}/rg"
        log_success "ripgrep provisioned in ${TOOL_BIN_DIR}/rg"
        return 0
      fi
    elif [[ "$asset" == *.zip ]] && command -v unzip >/dev/null 2>&1; then
      unzip -q "$archive_path" -d "$rg_tmp_dir" 2>/dev/null || true
      local found_rg_exe
      found_rg_exe="$(find "$rg_tmp_dir" -type f -name "rg.exe" 2>/dev/null | head -n 1)"
      if [ -n "$found_rg_exe" ]; then
        cp -f "$found_rg_exe" "${TOOL_BIN_DIR}/rg.exe"
        log_success "ripgrep provisioned in ${TOOL_BIN_DIR}/rg.exe"
        return 0
      fi
    fi
  fi

  log_debug "Could not automatically download ripgrep. OpsCloud will use built-in Python search."
}

provision_ripgrep || true

# ── Cloud & DevOps Pre-flight Checks ─────────────────────────

check_devops_prerequisites() {
  if [ "${OPSCLOUD_SKIP_DEVOPS_CHECK:-0}" = "1" ]; then
    return 0
  fi

  local devops_tools=("aws" "kubectl" "terraform" "helm" "docker" "git")
  local missing=()
  local found=()

  for tool in "${devops_tools[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
      found+=("$tool")
    else
      missing+=("$tool")
    fi
  done

  # Git is particularly important for repo bounds, skills, and plugins
  if ! command -v git >/dev/null 2>&1; then
    log_warn "git is not installed. OpsCloud relies on git for repository bounds and plugin management."
    if [ "$OS" = "macos" ]; then
      log_info "  To install git: brew install git (or xcode-select --install)"
    elif [ "$OS" = "linux" ]; then
      log_info "  To install git: sudo apt-get install -y git  # or dnf/pacman"
    fi
  fi
}

check_devops_prerequisites

# ── Package Installation via UV Tool ─────────────────────────

install_opscloud_tool() {
  local target_source="$1"
  local install_args=(
    "$UV_BIN" "tool" "install"
    "--python" "$REQUESTED_PYTHON"
    "--force"
    "--upgrade"
    "--refresh"
  )

  if [ -n "$REQUESTED_PRERELEASE" ]; then
    install_args+=("--prerelease=${REQUESTED_PRERELEASE}")
  fi

  local display_target=""

  if [ "$target_source" = "local" ]; then
    display_target="OpsCloud (local editable: .)"
    install_args+=("--editable" ".")
    if [ -n "$REQUESTED_EXTRAS" ]; then
      install_args+=("--extra" "$REQUESTED_EXTRAS")
    fi
  elif [ "$target_source" = "git" ]; then
    local git_spec="${REPO_URL}.git"
    if [ -n "$VERSION" ]; then
      git_spec="${REPO_URL}.git@${VERSION}"
    fi
    display_target="OpsCloud from GitHub (${git_spec})"
    local full_spec="${PACKAGE_NAME} @ git+${git_spec}"
    if [ -n "$REQUESTED_EXTRAS" ]; then
      full_spec="${PACKAGE_NAME}[${REQUESTED_EXTRAS}] @ git+${git_spec}"
    fi
    install_args+=("$full_spec")
  else
    # PyPI installation
    local spec="${PACKAGE_NAME}"
    if [ -n "$REQUESTED_EXTRAS" ]; then
      spec="${spec}[${REQUESTED_EXTRAS}]"
    fi
    if [ -n "$VERSION" ]; then
      spec="${spec}==${VERSION}"
    fi
    display_target="${spec} from PyPI"
    install_args+=("$spec")
  fi

  log_step "Installing ${display_target} via uv tool..."

  if [ "$VERBOSE" = "1" ]; then
    "${install_args[@]}"
  else
    # Capture output for clean error reporting
    local install_log
    install_log="$(mktemp "${TMP_ROOT}/opscloud-install-output-XXXXXX.log" 2>/dev/null || mktemp /tmp/opscloud-install-output-XXXXXX.log)"
    register_temp "$install_log"

    if ! "${install_args[@]}" > "$install_log" 2>&1; then
      # If PyPI failed and source wasn't explicitly pinned to pypi, try git fallback
      if [ "$target_source" = "pypi" ] && [ -z "${OPSCLOUD_SOURCE:-}" ] && [ "${OPSCLOUD_OFFLINE:-0}" != "1" ]; then
        log_warn "PyPI resolution for '${PACKAGE_NAME}' did not succeed."
        log_info "Falling back to installing directly from GitHub repository (${REPO_URL})..."
        install_opscloud_tool "git"
        return $?
      else
        log_error "Installation command failed:"
        cat "$install_log" >&2
        return 1
      fi
    fi
  fi

  log_success "OpsCloud package installed successfully."
  return 0
}

install_opscloud_tool "$RESOLVED_SOURCE"

# ── Ensure Executable Permissions ────────────────────────────

ensure_executable_permissions() {
  local primary_bin_path="${TOOL_BIN_DIR}/${PRIMARY_BIN}"

  # Windows .exe fallback checks
  if [ ! -f "$primary_bin_path" ] && [ -f "${primary_bin_path}.exe" ]; then
    primary_bin_path="${primary_bin_path}.exe"
  fi

  if [ -f "$primary_bin_path" ]; then
    chmod +x "$primary_bin_path" 2>/dev/null || true
  fi
}

ensure_executable_permissions

# ── Configure Shell PATH ─────────────────────────────────────

setup_shell_path() {
  if [ "${OPSCLOUD_NO_MODIFY_PATH:-0}" = "1" ]; then
    return 0
  fi

  # Check if TOOL_BIN_DIR is already in current PATH
  case ":${PATH}:" in
    *:"${TOOL_BIN_DIR}":*)
      return 0
      ;;
  esac

  local user_shell
  user_shell="$(basename "${SHELL:-bash}")"
  local profile_file=""

  case "$user_shell" in
    zsh)
      profile_file="${ZDOTDIR:-$HOME}/.zshrc"
      ;;
    bash)
      if [ "$OS" = "macos" ]; then
        profile_file="${HOME}/.bash_profile"
        [ ! -f "$profile_file" ] && profile_file="${HOME}/.profile"
      else
        profile_file="${HOME}/.bashrc"
        [ ! -f "$profile_file" ] && profile_file="${HOME}/.profile"
      fi
      ;;
    fish)
      local fish_conf_dir="${HOME}/.config/fish/conf.d"
      mkdir -p "$fish_conf_dir" 2>/dev/null || true
      local fish_conf="${fish_conf_dir}/opscloud.env.fish"
      if [ ! -f "$fish_conf" ]; then
        echo "fish_add_path -m ${TOOL_BIN_DIR}" > "$fish_conf"
        log_success "Added ${TOOL_BIN_DIR} to fish path (${fish_conf})"
      fi
      return 0
      ;;
    *)
      profile_file="${HOME}/.profile"
      ;;
  esac

  if [ -n "$profile_file" ] && [ -w "$(dirname "$profile_file")" ]; then
    local path_line="export PATH=\"${TOOL_BIN_DIR}:\$PATH\""
    if [ -f "$profile_file" ]; then
      if ! grep -Fq "${TOOL_BIN_DIR}" "$profile_file" 2>/dev/null; then
        printf "\n# Added by OpsCloud installer\n%s\n" "$path_line" >> "$profile_file"
        log_success "Added ${TOOL_BIN_DIR} to PATH in ${profile_file}"
      fi
    else
      printf "# Added by OpsCloud installer\n%s\n" "$path_line" > "$profile_file"
      log_success "Created ${profile_file} with ${TOOL_BIN_DIR} in PATH"
    fi
  fi
}

setup_shell_path

# ── Verification & Smoke Test ────────────────────────────────

INSTALLED_PRIMARY=""

for dir in "$TOOL_BIN_DIR" $(echo "$PATH" | tr ':' ' '); do
  for cand in "${PRIMARY_BIN}" "${PRIMARY_BIN}.exe"; do
    if [ -x "${dir}/${cand}" ] && [ -z "$INSTALLED_PRIMARY" ]; then
      INSTALLED_PRIMARY="${dir}/${cand}"
      break 2
    fi
  done
done

FINAL_VERSION=""
if [ -n "$INSTALLED_PRIMARY" ]; then
  FINAL_VERSION="$("$INSTALLED_PRIMARY" --version 2>/dev/null | awk '{print $NF}' || echo "0.1.0")"
fi

# ── Welcome Banner & Quick Start ─────────────────────────────

printf "\n"
printf "${CYAN}  ╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${CYAN}  ║${NC}   ${BOLD}TALKOPS / OPSCLOUD${NC}                                    ${CYAN}║${NC}\n"
printf "${CYAN}  ║${NC}   ${DIM}Autonomous AI Agent for DevOps & Cloud Operations${NC}     ${CYAN}║${NC}\n"
printf "${CYAN}  ╚══════════════════════════════════════════════════════════╝${NC}\n"
printf "\n"

if [ -n "$FINAL_VERSION" ]; then
  printf "${GREEN}✔${NC} ${BOLD}OpsCloud %s installed successfully!${NC}\n\n" "$FINAL_VERSION"
else
  printf "${GREEN}✔${NC} ${BOLD}OpsCloud installed successfully!${NC}\n\n"
fi

case ":${PATH}:" in
  *:"${TOOL_BIN_DIR}":*)
    printf "Launch interactive session:\n"
    printf "  ${BOLD}opscloud${NC}\n"
    ;;
  *)
    printf "${YELLOW}Note:${NC} ${TOOL_BIN_DIR} is not yet active in your current shell session.\n"
    printf "To use OpsCloud immediately in this terminal:\n"
    printf "  ${BOLD}export PATH=\"%s:\$PATH\"${NC}\n" "$TOOL_BIN_DIR"
    printf "Then start:\n"
    printf "  ${BOLD}opscloud${NC}\n"
    ;;
esac

printf "\n${BOLD}Quick Commands:${NC}\n"
printf "  ${CYAN}opscloud doctor${NC}         Verify AWS STS credentials & DevOps toolchain\n"
printf "  ${CYAN}opscloud /auth${NC}          Configure LLM API keys (Anthropic, Bedrock, OpenAI...)\n"
printf "  ${CYAN}opscloud -p \"<prompt>\"${NC}  Run non-interactive / headless operational task\n"
printf "  ${CYAN}opscloud --help${NC}         Show full CLI documentation and subcommands\n\n"
