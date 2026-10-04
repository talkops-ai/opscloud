"""Jev System One Rubric & Criteria Compiler.

Compiles natural language acceptance criteria bullets into typed LangChain-native
decision primitives (Noul for binary assertions, Score for quality, and Choice for
environmental blocker detection).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from langchain_typesafe import Choice, Noul, NoulCriteria, Question, Score

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CompiledRubric:
    """Compiled suite of typed Jev questions for acceptance criteria evaluation."""

    objective: str
    criteria_map: dict[str, str]
    questions: dict[str, Question]

    @property
    def criterion_keys(self) -> list[str]:
        """Return sorted question IDs corresponding to individual criteria."""
        return list(self.criteria_map.keys())


class JevCriteriaCompiler:
    """Compiles natural language acceptance criteria into typed Jev evaluation questions."""

    DEFAULT_READINESS_SCORE = Score(
        instructions="Rate the operational readiness and robustness of the completed work.",
        criteria=[
            "Incomplete: Core requirements missing, execution halted, or contains fatal errors.",
            "Functional: Deployed or executed, but lacks full verification, security hardening, or error handling.",
            "Production Ready: Fully implemented, verified against criteria, secure, and operational.",
        ],
    )

    DEFAULT_BLOCKER_CHOICE = Choice(
        instructions="Classify whether execution is proceeding normally or blocked by external environmental limits.",
        criteria={
            "progressing": "Normal execution; progress is actively being made or work is completed.",
            "external_blocker": "Blocked by external cloud quotas, missing IAM permissions, or network failure.",
            "ambiguous_requirement": "Blocked because the objective cannot be satisfied as specified or constraints conflict.",
        },
    )

    @classmethod
    def parse_criteria_bullets(cls, criteria: str | Sequence[str]) -> list[str]:
        """Normalize criteria from a Markdown string or list of bullets."""
        if isinstance(criteria, str):
            lines = criteria.strip().splitlines()
        else:
            lines = list(criteria)

        bullets: list[str] = []
        for line in lines:
            cleaned = line.strip()
            if not cleaned or cleaned.startswith(("{", "}", "[", "]")):
                continue
            # Strip markdown bullet markers, numbers, or dashes
            stripped = re.sub(r"^[-*•\d.)\s]+", "", cleaned).strip()
            if stripped:
                bullets.append(stripped)

        return bullets

    @classmethod
    def compile(
        cls,
        objective: str,
        criteria: str | Sequence[str],
        *,
        readiness_score: Score | None = None,
        blocker_choice: Choice | None = None,
    ) -> CompiledRubric:
        """Compile an objective and criteria list into a CompiledRubric.

        Args:
            objective: The high-level goal objective string.
            criteria: Markdown bullet text or sequence of criteria strings.
            readiness_score: Optional custom readiness Score question.
            blocker_choice: Optional custom blocker Choice question.

        Returns:
            CompiledRubric containing typed questions ready for TypeSafeClassifier.
        """
        bullets = cls.parse_criteria_bullets(criteria)
        if not bullets and isinstance(criteria, str) and criteria.strip():
            bullets = [criteria.strip()]

        criteria_map: dict[str, str] = {
            f"criterion_{idx + 1}": text
            for idx, text in enumerate(bullets)
        }

        # Formulate strict semantic boolean Noul questions for each criterion
        def _build_noul_criteria(criterion_text: str) -> NoulCriteria:
            lower = criterion_text.lower()
            is_negative = any(
                phrase in lower
                for phrase in (
                    "without running",
                    "without executing",
                    "without invoking",
                    "does not run",
                    "does not execute",
                    "does not invoke",
                    "no command is run",
                    "no command is executed",
                    "avoids running",
                    "skips running",
                    "skips executing",
                    "is not executed",
                    "is not run",
                )
            )
            if is_negative:
                return NoulCriteria(
                    true=(
                        "Execution completed and the prohibited command, validation, or action was NOT "
                        "executed in the tool/command logs."
                    ),
                    false=(
                        "The prohibited command, validation, or action WAS executed in the tool/command logs."
                    ),
                )
            return NoulCriteria(
                true=(
                    "Direct verifiable evidence in command logs, modified files, or resource "
                    "queries confirms this requirement is completed."
                ),
                false=(
                    "Requirement is incomplete, unattempted, missing evidence, or command output "
                    "indicates failure/error."
                ),
            )

        questions: dict[str, Question] = {
            cid: Noul(
                instructions=(
                    f"Based on the execution evidence, has this requirement been fully satisfied? "
                    f"Requirement: {text}"
                ),
                criteria=_build_noul_criteria(text),
            )
            for cid, text in criteria_map.items()
        }

        questions["readiness"] = readiness_score or cls.DEFAULT_READINESS_SCORE
        questions["blocker_status"] = blocker_choice or cls.DEFAULT_BLOCKER_CHOICE

        logger.debug(
            "JevCriteriaCompiler: compiled %d criteria into %d Jev questions for objective %r",
            len(criteria_map),
            len(questions),
            objective[:60],
        )

        return CompiledRubric(
            objective=objective,
            criteria_map=criteria_map,
            questions=questions,
        )


__all__ = ["CompiledRubric", "JevCriteriaCompiler"]
