"""Eval test runner verifying trajectory and output evaluators against benchmark datasets."""

from pathlib import Path
import pytest
import yaml

from tests.evals.evaluators.trajectory_evaluator import evaluate_tool_sequence
from tests.evals.evaluators.output_evaluator import evaluate_output_patterns


def test_eval_dataset_and_evaluators():
    dataset_file = Path(__file__).parent.parent / "datasets" / "aws_cloud_ops.yaml"
    assert dataset_file.exists()

    with open(dataset_file, "r") as f:
        data = yaml.safe_load(f)

    tasks = data.get("tasks", [])
    assert len(tasks) >= 3

    # Test trajectory matching on simulated tool execution
    task_iam = tasks[0]
    simulated_calls = [{"name": "execute", "args": {"command": "aws iam list-roles"}}]
    traj_res = evaluate_tool_sequence(simulated_calls, task_iam["expected_tools"])
    assert traj_res.matched is True
    assert traj_res.score == 1.0

    # Test output pattern matching
    simulated_output = "Audit Complete: Found 2 roles with AdministratorAccess attached and 4 users with MFA disabled."
    out_res = evaluate_output_patterns(simulated_output, task_iam["expected_output_patterns"])
    assert out_res.matched is True
    assert out_res.score == 1.0
