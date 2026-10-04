"""Rubrics and evaluation package for opscloud."""

from opscloud.rubrics.evaluator import (
    _RUBRIC_GRADER_SYSTEM_PROMPT,
    _create_rubric_grader_tools,
    _rubric_grader_system_prompt,
    create_rubric_grader_agent,
    evaluate_rubric,
)
from opscloud.rubrics.evidence_extractor import extract_grading_evidence
from opscloud.rubrics.generator import (
    GOAL_AMENDMENT_SYSTEM_PROMPT,
    GOAL_RUBRIC_SYSTEM_PROMPT,
    generate_rubric,
    _goal_amendment_human_prompt,
    _goal_rubric_human_prompt,
)
from opscloud.rubrics.jev_compiler import CompiledRubric, JevCriteriaCompiler
from opscloud.rubrics.jev_grader import (
    BLOCKER_DISPATCH,
    JevHybridRubricGrader,
    build_criteria_evaluations,
)

__all__ = [
    "BLOCKER_DISPATCH",
    "CompiledRubric",
    "GOAL_AMENDMENT_SYSTEM_PROMPT",
    "GOAL_RUBRIC_SYSTEM_PROMPT",
    "JevCriteriaCompiler",
    "JevHybridRubricGrader",
    "_RUBRIC_GRADER_SYSTEM_PROMPT",
    "_create_rubric_grader_tools",
    "_goal_amendment_human_prompt",
    "_goal_rubric_human_prompt",
    "_rubric_grader_system_prompt",
    "build_criteria_evaluations",
    "create_rubric_grader_agent",
    "evaluate_rubric",
    "extract_grading_evidence",
    "generate_rubric",
]
