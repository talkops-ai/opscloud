"""CLI AST Safety Evaluator — deterministic safety classification for shell commands.

Parses composite bash commands, subshells, pipelines, and process substitutions using
`bashlex` AST traversal to detect dangerous verbs and flags in sub-millisecond time.
"""

from __future__ import annotations

import re
from typing import Any

import bashlex
import bashlex.ast

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# Wrapper utilities that execute other commands
WRAPPER_UTILITIES: frozenset[str] = frozenset(
    {
        "xargs",
        "sudo",
        "doas",
        "time",
        "nohup",
        "env",
        "parallel",
        "chroot",
        "exec",
        "sh",
        "bash",
        "zsh",
    }
)

# Pure read-only utilities whose execution only inspects local data/state
READONLY_UTILITIES: frozenset[str] = frozenset(
    {
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "ls",
        "find",
        "echo",
        "pwd",
        "awk",
        "sed",
        "jq",
        "yq",
        "wc",
        "diff",
        "stat",
        "file",
        "uname",
        "which",
        "whereis",
        "whoami",
        "printenv",
        "date",
        "uptime",
        "df",
        "du",
        "free",
        "ps",
        "top",
        "tree",
        "sort",
        "uniq",
        "tr",
        "cut",
        "less",
        "more",
        "curl",
    }
)

# Destructive operations that delete or disrupt live cloud state, disk, or databases
DANGEROUS_VERBS: frozenset[str] = frozenset(
    {
        "delete",
        "terminate",
        "terminate-instances",
        "delete-db-instance",
        "delete-db-cluster",
        "delete-cluster",
        "delete-nodegroup",
        "rb",
        "remove",
        "rm",
        "drain",
        "evict",
        "taint",
        "drop",
        "uninstall",
        "zap",
        "destroy",
        "prune",
        "truncate",
        "kill",
        "purge",
        "wipe",
        "format",
        "cordon",
        "uncordon",
        "shutdown",
        "reboot",
    }
)

# Destructive force-override CLI flags
DANGEROUS_FLAGS: frozenset[str] = frozenset(
    {
        "--force",
        "--purge",
        "--grace-period=0",
        "-rf",
        "-fr",
        "--all",
        "--no-preserve-root",
        "--cascade=foreground",
        "--cascade=orphan",
        "--hard",
        "-D",
        "--recursive",
    }
)

# Standard AWS/DevOps/Git read-only verbs
READONLY_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "describe",
        "status",
        "logs",
        "log",
        "version",
        "show",
        "view",
        "explain",
        "top",
        "diff",
        "cluster-info",
        "auth",
        "can-i",
        "api-resources",
        "api-versions",
        "branch",
        "remote",
        "config",
        "plan",
        "validate",
        "fmt",
        "get-caller-identity",
    }
)


# Standard mutating verbs across cloud providers and CLI tools
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "create",
        "delete",
        "remove",
        "destroy",
        "terminate",
        "kill",
        "purge",
        "drop",
        "uninstall",
        "update",
        "set",
        "start",
        "stop",
        "restart",
        "apply",
        "attach",
        "detach",
        "install",
        "run",
        "execute",
        "deploy",
        "scale",
        "patch",
        "edit",
        "import",
        "push",
        "commit",
        "merge",
        "rebase",
    }
)


class SecurityASTVisitor(bashlex.ast.nodevisitor):
    """Traverses bashlex AST nodes inspecting words, commands, and substitutions."""

    def __init__(self) -> None:
        self.found_verbs: list[str] = []
        self.found_flags: list[str] = []
        self.found_commands: list[str] = []
        self.is_destructive: bool = False
        self.is_readonly: bool = True

    def visitcommand(self, n: Any, parts: list[Any]) -> None:
        words: list[str] = []
        for part in parts:
            if getattr(part, "kind", None) == "word":
                w = getattr(part, "word", "")
                if w:
                    words.append(w)

        if not words:
            return

        cmd_utility = words[0].lower()
        self.found_commands.append(cmd_utility)

        # 1. Pure inspection utilities (cat, grep, ls, jq, etc.)
        if cmd_utility in READONLY_UTILITIES:
            if cmd_utility == "rm":
                self.is_destructive = True
                self.is_readonly = False
            return

        # 2. Wrapper utilities (xargs, sudo, time, etc.) - inspect all words
        if cmd_utility in WRAPPER_UTILITIES:
            self.is_readonly = False
            for word in words[1:]:
                w_lower = word.lower()
                if word.startswith("-"):
                    self.found_flags.append(word)
                    if word in DANGEROUS_FLAGS or w_lower in DANGEROUS_FLAGS:
                        self.is_destructive = True
                else:
                    self.found_verbs.append(w_lower)
                    if w_lower in DANGEROUS_VERBS:
                        self.is_destructive = True
            return

        # 3. AWS CLI special handling (aws <service> <verb>)
        if cmd_utility == "aws":
            flags: list[str] = []
            non_flag_args: list[str] = []
            for word in words[1:]:
                w_lower = word.lower()
                if word.startswith("-"):
                    flags.append(word)
                    if word in DANGEROUS_FLAGS or w_lower in DANGEROUS_FLAGS:
                        self.is_destructive = True
                else:
                    non_flag_args.append(w_lower)
            self.found_flags.extend(flags)
            if len(non_flag_args) >= 2:
                # e.g. aws ec2 terminate-instances
                service = non_flag_args[0]
                verb = non_flag_args[1]
                self.found_verbs.append(verb)
                if (
                    verb in DANGEROUS_VERBS
                    or verb.startswith(("terminate-", "delete-", "stop-", "disassociate-", "deregister-"))
                    or (service == "s3" and verb in ("rb",))
                ):
                    self.is_destructive = True
                    self.is_readonly = False
                elif (
                    verb in READONLY_VERBS
                    or verb.startswith(("describe-", "list-", "get-"))
                    or (service == "s3" and verb in ("ls",))
                    or (service == "sts" and verb in ("get-caller-identity",))
                ):
                    pass
                else:
                    self.is_readonly = False
            return

        # 4. Azure CLI special handling (az <service> [subgroup] <action>)
        if cmd_utility == "az":
            flags = []
            non_flag_args = []
            skip_next = False
            for word in words[1:]:
                if skip_next:
                    skip_next = False
                    continue
                w_lower = word.lower()
                if word.startswith("-"):
                    flags.append(word)
                    if word in DANGEROUS_FLAGS or w_lower in DANGEROUS_FLAGS:
                        self.is_destructive = True
                    if word in ("-o", "--output", "-g", "--resource-group", "-n", "--name", "-s", "--subscription", "--query", "-f", "--file"):
                        skip_next = True
                else:
                    non_flag_args.append(w_lower)
            self.found_flags.extend(flags)
            if non_flag_args:
                for verb in non_flag_args:
                    self.found_verbs.append(verb)
                    if verb in DANGEROUS_VERBS or verb.startswith(("delete", "purge", "destroy", "terminate")):
                        self.is_destructive = True
                        self.is_readonly = False
                        return
                if any(v in DANGEROUS_VERBS for v in non_flag_args):
                    self.is_destructive = True
                    self.is_readonly = False
                elif any(v in MUTATING_VERBS for v in non_flag_args):
                    self.is_readonly = False
                elif any(v in READONLY_VERBS or v.startswith(("show", "list", "get")) for v in non_flag_args):
                    pass
                else:
                    self.is_readonly = False
            return

        # 5. Google Cloud CLI special handling (gcloud <group> [subgroup] <action>)
        if cmd_utility == "gcloud":
            flags = []
            non_flag_args = []
            skip_next = False
            for word in words[1:]:
                if skip_next:
                    skip_next = False
                    continue
                w_lower = word.lower()
                if word.startswith("-"):
                    flags.append(word)
                    if word in DANGEROUS_FLAGS or w_lower in DANGEROUS_FLAGS:
                        self.is_destructive = True
                    if word in ("--format", "--filter", "--project", "--zone", "--region"):
                        skip_next = True
                else:
                    non_flag_args.append(w_lower)
            self.found_flags.extend(flags)
            if non_flag_args:
                for verb in non_flag_args:
                    self.found_verbs.append(verb)
                    if verb in DANGEROUS_VERBS or verb.startswith(("delete", "purge", "destroy", "terminate")):
                        self.is_destructive = True
                        self.is_readonly = False
                        return
                if any(v in DANGEROUS_VERBS for v in non_flag_args):
                    self.is_destructive = True
                    self.is_readonly = False
                elif any(v in MUTATING_VERBS for v in non_flag_args):
                    self.is_readonly = False
                elif any(v in READONLY_VERBS or v.startswith(("list", "describe", "get", "info", "version")) for v in non_flag_args):
                    pass
                else:
                    self.is_readonly = False
            return

        # 6. Command utilities with sub-commands (terraform, tofu, kubectl, helm, git, docker, etc.)
        flags = []
        non_flag_args = []
        for word in words[1:]:
            w_lower = word.lower()
            if word.startswith("-"):
                flags.append(word)
                if (
                    word in DANGEROUS_FLAGS
                    or w_lower in DANGEROUS_FLAGS
                    or (cmd_utility == "rm" and ("f" in word or "r" in word))
                ):
                    self.is_destructive = True
                    self.is_readonly = False
            else:
                non_flag_args.append(w_lower)

        self.found_flags.extend(flags)
        if not non_flag_args:
            if cmd_utility == "rm":
                self.is_destructive = True
                self.is_readonly = False
            return

        for verb in non_flag_args:
            self.found_verbs.append(verb)
            if verb in DANGEROUS_VERBS:
                self.is_destructive = True
                self.is_readonly = False
                return

        primary_verb = non_flag_args[0]
        if primary_verb in READONLY_VERBS:
            pass
        else:
            self.is_readonly = False

    def visitprocesssubstitution(self, n: Any, command: Any) -> None:
        self.visit(command)

    def visitcommandsubstitution(self, n: Any, command: Any) -> None:
        self.visit(command)


def evaluate_cli_safety(command_string: str) -> dict[str, Any]:
    """Parse a composite shell command and determine its operational risk tier.

    Returns:
        dict containing:
            - tier: 1 (Read-Only), 2 (Low-Impact), 3 (Mutating), or 4 (Destructive)
            - is_destructive: bool
            - is_readonly: bool
            - safe: bool
            - reason: str
    """
    cmd_str = (command_string or "").strip()
    if not cmd_str:
        return {
            "tier": 1,
            "is_destructive": False,
            "is_readonly": True,
            "safe": True,
            "reason": "Empty command string.",
        }

    try:
        trees = bashlex.parse(cmd_str)
    except Exception as exc:
        # Fallback to regex-based classification on syntax failure
        logger.debug("AST Parsing failed for command %r, falling back to heuristics: %s", cmd_str[:120], exc)
        from opscloud.security.shell_safety import classify_command

        cls_res = classify_command(cmd_str)
        if cls_res == "dangerous":
            return {
                "tier": 4,
                "is_destructive": True,
                "is_readonly": False,
                "safe": False,
                "is_safe": False,
                "reason": "Dangerous command pattern detected via heuristics.",
            }
        if cls_res == "safe":
            return {
                "tier": 1,
                "is_destructive": False,
                "is_readonly": True,
                "safe": True,
                "is_safe": True,
                "reason": "Read-only command identified via heuristics.",
            }
        return {
            "tier": 3,
            "is_destructive": False,
            "is_readonly": False,
            "safe": False,
            "is_safe": False,
            "reason": "Unrecognized or mutating command syntax.",
        }

    visitor = SecurityASTVisitor()
    for tree in trees:
        visitor.visit(tree)

    if visitor.is_destructive:
        matched_verbs = set(visitor.found_verbs) & DANGEROUS_VERBS
        matched_flags = set(visitor.found_flags) & DANGEROUS_FLAGS
        reasons: list[str] = []
        if matched_verbs:
            reasons.append(f"destructive verbs: {sorted(matched_verbs)}")
        if matched_flags:
            reasons.append(f"destructive flags: {sorted(matched_flags)}")
        if not reasons:
            reasons.append("destructive command pattern")
        reason_str = f"Detected {', '.join(reasons)}."
        res = {
            "tier": 4,
            "is_destructive": True,
            "is_readonly": False,
            "safe": False,
            "is_safe": False,
            "reason": reason_str,
        }
    elif visitor.is_readonly:
        res = {
            "tier": 1,
            "is_destructive": False,
            "is_readonly": True,
            "safe": True,
            "is_safe": True,
            "reason": "Command contains only read-only inspection utilities and verbs.",
        }
    else:
        res = {
            "tier": 3,
            "is_destructive": False,
            "is_readonly": False,
            "safe": False,
            "is_safe": False,
            "reason": f"Operational mutation command: verbs {visitor.found_verbs[:3]}.",
        }

    return res


from dataclasses import dataclass


@dataclass(frozen=True)
class AstEvaluationResult:
    is_safe: bool
    is_destructive: bool
    is_readonly: bool
    tier: int
    reason: str


class CliAstEvaluator:
    """Evaluates shell commands against allow lists and AST safety criteria."""

    def evaluate(self, command: str, allow_list: list[str] | None = None) -> AstEvaluationResult:
        if allow_list:
            from opscloud.security.shell_safety import is_shell_command_allowed

            if not is_shell_command_allowed(command, allow_list):
                return AstEvaluationResult(
                    is_safe=False,
                    is_destructive=False,
                    is_readonly=False,
                    tier=3,
                    reason="Command not in allow list",
                )
        res = evaluate_cli_safety(command)
        return AstEvaluationResult(
            is_safe=res.get("safe", False),
            is_destructive=res.get("is_destructive", False),
            is_readonly=res.get("is_readonly", False),
            tier=res.get("tier", 3),
            reason=res.get("reason", ""),
        )


evaluate_command_safety = evaluate_cli_safety

__all__ = [
    "AstEvaluationResult",
    "CliAstEvaluator",
    "DANGEROUS_FLAGS",
    "DANGEROUS_VERBS",
    "READONLY_UTILITIES",
    "READONLY_VERBS",
    "SecurityASTVisitor",
    "WRAPPER_UTILITIES",
    "evaluate_cli_safety",
    "evaluate_command_safety",
]

