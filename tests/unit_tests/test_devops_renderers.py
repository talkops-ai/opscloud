"""Unit tests for DevOps terminal output renderers (Terraform, Kubectl, Helm, Ansible)."""

from __future__ import annotations

from opscloud.ui.theme import DARK_COLORS, LIGHT_COLORS
from opscloud.ui.widgets.devops_renderers import (
    AnsiblePlaybookRenderer,
    HelmDiffRenderer,
    KubectlRenderer,
    TerraformPlanRenderer,
    detect_devops_renderer,
    get_devops_renderer_for_command,
)
from opscloud.ui.widgets.messages import ToolCallMessage


def test_kubectl_renderer_header_and_statuses():
    """Verify KubectlRenderer highlights headers as dim bold and statuses with semantic styles."""
    renderer = KubectlRenderer()

    # Table header line
    header = "NAME                               READY   STATUS             RESTARTS   AGE"
    rendered_header = renderer.render_line(header, colors=DARK_COLORS)
    assert rendered_header.plain == header
    assert any("bold" in str(span.style) for span in rendered_header.spans)

    # Pod with Running status (green / success)
    pod_running = "coredns-78fcdf6894-abcde          1/1     Running            0          12d"
    rendered_running = renderer.render_line(pod_running, colors=DARK_COLORS)
    assert rendered_running.plain == pod_running
    assert any(DARK_COLORS.success in str(span.style) for span in rendered_running.spans)

    # Pod with Pending status (amber / warning)
    pod_pending = "frontend-deployment-55b6c-xyz12   0/1     Pending            0          2m"
    rendered_pending = renderer.render_line(pod_pending, colors=DARK_COLORS)
    assert any(DARK_COLORS.warning in str(span.style) for span in rendered_pending.spans)

    # Pod with CrashLoopBackOff status (red / error)
    pod_crash = "api-service-99bfa-12345           0/1     CrashLoopBackOff   4          15m"
    rendered_crash = renderer.render_line(pod_crash, colors=DARK_COLORS)
    assert any(DARK_COLORS.error in str(span.style) for span in rendered_crash.spans)

    # Full output text render
    full_output = f"{header}\n{pod_running}\n{pod_crash}"
    full_content = renderer.render(full_output, colors=DARK_COLORS)
    assert len(full_content.plain.splitlines()) == 3


def test_terraform_plan_renderer_diff_and_summary():
    """Verify TerraformPlanRenderer highlights summary chips and diff lines."""
    renderer = TerraformPlanRenderer()

    # Plan summary line
    summary_line = "Plan: 3 to add, 1 to change, 0 to destroy."
    rendered_summary = renderer.render_line(summary_line, colors=DARK_COLORS)
    assert "+3 to add" in rendered_summary.plain
    assert "~1 to change" in rendered_summary.plain
    assert "-0 to destroy" in rendered_summary.plain
    assert any(DARK_COLORS.plan_add in str(span.style) for span in rendered_summary.spans)
    assert any(DARK_COLORS.plan_change in str(span.style) for span in rendered_summary.spans)
    assert any(DARK_COLORS.error in str(span.style) for span in rendered_summary.spans)

    # Diff lines
    add_line = "  + resource \"aws_instance\" \"web\" {"
    rendered_add = renderer.render_line(add_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.plan_add in str(span.style) for span in rendered_add.spans)

    mod_line = "  ~ instance_type = \"t3.micro\" => \"t3.large\""
    rendered_mod = renderer.render_line(mod_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.plan_change in str(span.style) for span in rendered_mod.spans)

    del_line = "  - cidr_blocks = [\"0.0.0.0/0\"]"
    rendered_del = renderer.render_line(del_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.plan_destroy in str(span.style) for span in rendered_del.spans)

    # JSON plan rendering
    plan_json = {
        "resource_changes": [
            {"address": "aws_s3_bucket.data", "change": {"actions": ["create"]}},
            {"address": "aws_iam_role.app", "change": {"actions": ["delete"]}},
            {"address": "aws_security_group.web", "change": {"actions": ["update"]}},
        ]
    }
    rendered_json = renderer.render_json(plan_json, colors=DARK_COLORS)
    assert "+1 to add" in rendered_json.plain
    assert "~1 to change" in rendered_json.plain
    assert "-1 to destroy" in rendered_json.plain


def test_helm_diff_renderer():
    """Verify HelmDiffRenderer highlights value changes and hunk headers."""
    renderer = HelmDiffRenderer()

    add_line = "+ image: nginx:1.25.0"
    rendered_add = renderer.render_line(add_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.success in str(span.style) for span in rendered_add.spans)

    del_line = "- image: nginx:1.24.0"
    rendered_del = renderer.render_line(del_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.error in str(span.style) for span in rendered_del.spans)

    hunk_line = "@@ -10,3 +10,3 @@"
    rendered_hunk = renderer.render_line(hunk_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.secondary in str(span.style) for span in rendered_hunk.spans)


def test_ansible_playbook_renderer():
    """Verify AnsiblePlaybookRenderer highlights plays, tasks, ok, changed, and failed."""
    renderer = AnsiblePlaybookRenderer()

    task_line = "TASK [Ensure nginx is installed] *************************************"
    rendered_task = renderer.render_line(task_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.primary in str(span.style) for span in rendered_task.spans)

    ok_line = "ok: [webserver-01]"
    rendered_ok = renderer.render_line(ok_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.success in str(span.style) for span in rendered_ok.spans)

    changed_line = "changed: [webserver-02]"
    rendered_changed = renderer.render_line(changed_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.warning in str(span.style) for span in rendered_changed.spans)

    failed_line = "failed: [webserver-03] => {\"msg\": \"Package not found\"}"
    rendered_failed = renderer.render_line(failed_line, colors=DARK_COLORS)
    assert any(DARK_COLORS.error in str(span.style) for span in rendered_failed.spans)


def test_command_routing_and_detection():
    """Verify detection of DevOps tools by command line and output inspection."""
    # Kubectl variations
    assert isinstance(get_devops_renderer_for_command("kubectl get pods -A"), KubectlRenderer)
    assert isinstance(get_devops_renderer_for_command("k get pods"), KubectlRenderer)
    assert isinstance(get_devops_renderer_for_command("sudo kubectl describe svc"), KubectlRenderer)

    # Terraform plan variations
    assert isinstance(get_devops_renderer_for_command("terraform plan"), TerraformPlanRenderer)
    assert isinstance(get_devops_renderer_for_command("tf plan -out=tfplan"), TerraformPlanRenderer)

    # Helm diff variations
    assert isinstance(get_devops_renderer_for_command("helm diff upgrade my-release ."), HelmDiffRenderer)

    # Ansible variations
    assert isinstance(get_devops_renderer_for_command("ansible-playbook -i hosts site.yml"), AnsiblePlaybookRenderer)

    # Unrelated commands
    assert get_devops_renderer_for_command("ls -la") is None
    assert get_devops_renderer_for_command("git status") is None

    # Auto-detection from output when command is generic
    sample_k8s = "NAME               READY   STATUS    RESTARTS   AGE\nnginx-pod          1/1     Running   0          5m"
    assert isinstance(detect_devops_renderer(sample_k8s, command="bash"), KubectlRenderer)

    sample_tf = "Plan: 2 to add, 0 to change, 0 to destroy."
    assert isinstance(detect_devops_renderer(sample_tf, command=""), TerraformPlanRenderer)


def test_tool_call_message_shell_output_integration():
    """Verify ToolCallMessage._format_shell_output uses DevOps renderers when matched."""
    msg = ToolCallMessage(
        tool_name="execute",
        args={"command": "kubectl get pods"},
    )

    k8s_output = (
        "$ kubectl get pods\n"
        "NAME             READY   STATUS    RESTARTS   AGE\n"
        "auth-service     1/1     Running   0          10m\n"
        "db-proxy         0/1     Error     2          1m"
    )

    formatted = msg._format_shell_output(k8s_output, is_preview=False)
    assert formatted.content is not None
    lines = formatted.content.plain.splitlines()
    assert len(lines) == 4
    # Line 0 is dimmed command echo
    # Line 1 is styled header
    # Line 2 and 3 contain status styles
    assert any(DARK_COLORS.success in str(s.style) or "success" in str(s.style) for s in formatted.content.spans)
    assert any(DARK_COLORS.error in str(s.style) or "error" in str(s.style) for s in formatted.content.spans)
