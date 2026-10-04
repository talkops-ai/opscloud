"""Multi-Cloud (AWS, Azure, GCP), Terraform, and DevOps shell command safety classification and allow-list verification."""

from __future__ import annotations

import re
import shlex

# Disallowed dangerous shell patterns in non-interactive/auto-approve mode (injection prevention)
DANGEROUS_SHELL_PATTERNS: tuple[str, ...] = (
    "$(",  # Command substitution
    "`",  # Backtick command substitution
    "$'",  # ANSI-C quoting
    "\n",  # Newline
    "\r",  # Carriage return
    "\t",  # Tab
    "<(",  # Process substitution (input)
    ">(",  # Process substitution (output)
    "<<<",  # Here-string
    "<<",  # Here-doc
    ">>",  # Append redirect
    ">",  # Output redirect
    "<",  # Input redirect
    "${",  # Variable expansion with braces
)

# Commands that are ALWAYS safe (read-only, zero mutating side effects across clouds)
CLOUD_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        # AWS CLI read-only inspection
        "aws ec2 describe-instances",
        "aws ec2 describe-vpcs",
        "aws ec2 describe-subnets",
        "aws ec2 describe-security-groups",
        "aws ec2 describe-volumes",
        "aws ec2 describe-snapshots",
        "aws ec2 describe-key-pairs",
        "aws ec2 describe-route-tables",
        "aws s3 ls",
        "aws rds describe-db-instances",
        "aws rds describe-db-clusters",
        "aws eks describe-cluster",
        "aws eks list-clusters",
        "aws eks list-nodegroups",
        "aws iam get-user",
        "aws iam get-role",
        "aws iam list-users",
        "aws iam list-roles",
        "aws iam list-policies",
        "aws sts get-caller-identity",
        "aws cloudwatch get-metric-data",
        "aws cloudwatch get-metric-statistics",
        "aws cloudwatch describe-alarms",
        "aws logs describe-log-groups",
        "aws logs describe-log-streams",
        "aws logs filter-log-events",
        "aws lambda list-functions",
        "aws lambda get-function",
        # Azure CLI read-only inspection
        "az account show",
        "az account list",
        "az group list",
        "az group show",
        "az vm list",
        "az vm show",
        "az aks list",
        "az aks show",
        "az storage account list",
        "az storage account show",
        "az keyvault list",
        "az keyvault show",
        "az monitor metrics list",
        "az version",
        # Google Cloud (GCP) read-only inspection
        "gcloud auth list",
        "gcloud config list",
        "gcloud projects list",
        "gcloud compute instances list",
        "gcloud container clusters list",
        "gcloud container clusters describe",
        "gcloud storage ls",
        "gcloud iam service-accounts list",
        "gcloud version",
        # Terraform / OpenTofu read-only
        "terraform plan",
        "terraform show",
        "terraform fmt -check",
        "terraform validate",
        "terraform version",
        "tofu plan",
        "tofu show",
        "tofu fmt -check",
        "tofu validate",
        "tofu version",
        # Kubernetes read-only
        "kubectl get",
        "kubectl describe",
        "kubectl explain",
        "kubectl top",
        "kubectl logs",
        "kubectl cluster-info",
        "kubectl config view",
        "kubectl config get-contexts",
        "kubectl config current-context",
        "kubectl version",
        "kubectl auth can-i",
        "kubectl diff",
        # Helm read-only
        "helm list",
        "helm status",
        "helm get",
        "helm show",
        "helm search",
        "helm version",
        # Docker read-only
        "docker ps",
        "docker images",
        "docker version",
        "docker info",
        # Git & Unix read-only exploration
        "git status",
        "git log",
        "git diff",
        "git branch",
        "git show",
        "cat",
        "ls",
        "find",
        "grep",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "diff",
        "jq",
        "yq",
        "echo",
        "pwd",
        "which",
    }
)

AWS_SAFE_COMMANDS = CLOUD_SAFE_COMMANDS
DEVOPS_SAFE_COMMANDS = list(CLOUD_SAFE_COMMANDS)

# Patterns that are ALWAYS dangerous (require human approval or strict checks)
CLOUD_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS deletions & stops
    re.compile(r"aws\s+ec2\s+terminate-instances\b", re.IGNORECASE),
    re.compile(r"aws\s+ec2\s+stop-instances\b", re.IGNORECASE),
    re.compile(r"aws\s+s3\s+rb\b", re.IGNORECASE),
    re.compile(r"aws\s+s3\s+rm\s+.*--recursive\b", re.IGNORECASE),
    re.compile(r"aws\s+rds\s+delete-db-instance\b", re.IGNORECASE),
    re.compile(r"aws\s+rds\s+delete-db-cluster\b", re.IGNORECASE),
    re.compile(r"aws\s+eks\s+delete-cluster\b", re.IGNORECASE),
    re.compile(r"aws\s+eks\s+delete-nodegroup\b", re.IGNORECASE),
    re.compile(r"aws\s+iam\s+delete-\w+\b", re.IGNORECASE),
    # Azure deletions
    re.compile(r"az\s+vm\s+delete\b", re.IGNORECASE),
    re.compile(r"az\s+group\s+delete\b", re.IGNORECASE),
    re.compile(r"az\s+aks\s+delete\b", re.IGNORECASE),
    re.compile(r"az\s+keyvault\s+purge\b", re.IGNORECASE),
    re.compile(r"az\s+storage\s+(account|blob)\s+delete\b", re.IGNORECASE),
    # GCP deletions
    re.compile(r"gcloud\s+compute\s+instances\s+delete\b", re.IGNORECASE),
    re.compile(r"gcloud\s+container\s+clusters\s+delete\b", re.IGNORECASE),
    re.compile(r"gcloud\s+projects\s+delete\b", re.IGNORECASE),
    re.compile(r"gcloud\s+iam\s+service-accounts\s+delete\b", re.IGNORECASE),
    re.compile(r"gcloud\s+storage\s+rm\s+.*--recursive\b", re.IGNORECASE),
    # IaC mutations
    re.compile(r"terraform\s+destroy\b", re.IGNORECASE),
    re.compile(r"terraform\s+apply\b", re.IGNORECASE),
    re.compile(r"tofu\s+destroy\b", re.IGNORECASE),
    re.compile(r"tofu\s+apply\b", re.IGNORECASE),
    re.compile(r"pulumi\s+destroy\b", re.IGNORECASE),
    # Kubernetes / Helm
    re.compile(r"kubectl\s+delete\s+.*--all", re.IGNORECASE),
    re.compile(r"kubectl\s+delete\s+namespace\b", re.IGNORECASE),
    re.compile(r"kubectl\s+delete\s+all\b", re.IGNORECASE),
    re.compile(r"helm\s+uninstall\b", re.IGNORECASE),
    # System destructions
    re.compile(r"rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/", re.IGNORECASE),
    re.compile(r"mkfs", re.IGNORECASE),
    re.compile(r"dd\s+if=", re.IGNORECASE),
    # Credential files & metadata endpoints
    re.compile(
        r"(\.aws/credentials|\.aws/config|\.azure/|\.config/gcloud/|gcloud/credentials\.db|id_rsa|id_ed25519|169\.254\.169\.254|metadata\.google\.internal)",
        re.IGNORECASE,
    ),
)

AWS_DANGEROUS_PATTERNS = CLOUD_DANGEROUS_PATTERNS

DEVOPS_DESTRUCTIVE_COMMANDS: list[str] = [
    "aws ec2 terminate-instances",
    "aws s3 rb",
    "aws rds delete-db-instance",
    "aws eks delete-cluster",
    "az vm delete",
    "az group delete",
    "az aks delete",
    "gcloud compute instances delete",
    "gcloud container clusters delete",
    "terraform destroy",
    "tofu destroy",
    "pulumi destroy",
    "helm uninstall",
    "kubectl delete",
    "kubectl drain",
]


def classify_command(command: str) -> str:
    """Classify a shell command as 'safe', 'dangerous', or 'ambiguous'.

    - 'safe': Read-only queries with no state change
    - 'dangerous': High-risk operations (terminations, deletions, bulk destroy)
    - 'ambiguous': Standard mutations that need checking against active approval mode
    """
    cmd = command.strip()
    if not cmd:
        return "safe"

    cmd_lower = cmd.lower()

    # 1. Check dangerous patterns first
    for pattern in CLOUD_DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return "dangerous"

    # 2. Check always-safe commands
    for safe in CLOUD_SAFE_COMMANDS:
        if cmd_lower.startswith(safe):
            return "safe"

    # 3. Fast heuristics for read-only prefix verbs across cloud CLIs
    tokens = cmd_lower.split()
    if not tokens:
        return "safe"

    utility = tokens[0]

    # AWS
    if utility in ("aws", "aws.") and len(tokens) >= 3:
        if tokens[2].startswith(("describe-", "list-", "get-")):
            return "safe"

    # Azure (az <service> [subgroup] <action>)
    if utility == "az" and len(tokens) >= 3:
        if tokens[-1] in ("list", "show") or tokens[1] in ("account", "version"):
            return "safe"

    # GCP (gcloud <group> [subgroup] <action>)
    if utility == "gcloud" and len(tokens) >= 3:
        if tokens[-1] in ("list", "describe"):
            return "safe"

    # Kubectl
    if utility == "kubectl" and len(tokens) >= 2:
        if tokens[1] in ("get", "describe", "logs", "top", "explain", "cluster-info"):
            return "safe"

    return "ambiguous"


def contains_dangerous_patterns(command: str) -> bool:
    """Return True if command contains shell injections, substitutions, redirections, or dangerous operations."""
    if any(pattern in command for pattern in DANGEROUS_SHELL_PATTERNS):
        return True

    if any(pattern.search(command) for pattern in CLOUD_DANGEROUS_PATTERNS):
        return True

    if re.search(r"\$[A-Za-z_]", command):
        return True

    return bool(re.search(r"(?<![&])&(?![&])", command))


def is_shell_command_allowed(command: str, allow_list: list[str] | None) -> bool:
    """Validate prefix match on whitespace-separated command tokens against an allow list."""
    if not allow_list or not command or not command.strip():
        return False

    if contains_dangerous_patterns(command):
        return False

    allow_entries = [entry.strip() for entry in allow_list if entry.strip()]
    if not allow_entries:
        return False

    segments = re.split(r"&&|\|\||[|;]", command)
    found_command = False

    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
            if not tokens:
                continue
            found_command = True

            matched = False
            for entry in allow_entries:
                entry_tokens = shlex.split(entry)
                if not entry_tokens:
                    continue
                if len(tokens) >= len(entry_tokens) and tokens[: len(entry_tokens)] == entry_tokens:
                    matched = True
                    break

            if not matched:
                return False
        except ValueError:
            return False

    return found_command


def is_safe_command(command: str) -> bool:
    """Return True if command is classified as definitely safe."""
    return classify_command(command) == "safe"


__all__ = [
    "AWS_DANGEROUS_PATTERNS",
    "AWS_SAFE_COMMANDS",
    "CLOUD_DANGEROUS_PATTERNS",
    "CLOUD_SAFE_COMMANDS",
    "DANGEROUS_SHELL_PATTERNS",
    "DEVOPS_DESTRUCTIVE_COMMANDS",
    "DEVOPS_SAFE_COMMANDS",
    "classify_command",
    "contains_dangerous_patterns",
    "is_safe_command",
    "is_shell_command_allowed",
]
