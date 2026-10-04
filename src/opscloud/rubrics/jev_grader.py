"""Jev-powered System One rubric grader and hybrid evaluator.

Performs ultra-fast (<200ms) parallel criteria verification using TypeSafe AI Jev,
eliminating autoregressive LLM judge variance and cost, while providing transparent
Tier 2 generative escalation and environmental blocker detection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import inspect
import time
from typing import Any, cast

from deepagents.middleware.rubric import (
    CriterionEval,
    CriterionFail,
    CriterionPass,
    GraderResponse,
)
from langchain_core.runnables import RunnableConfig
from langchain_typesafe import (
    ClassifierRequest,
    ClassifierResponse,
    State,
    TypeSafeClassifier,
)

from opscloud.config.settings import get_settings, resolve_env_var
from opscloud.rubrics.evidence_extractor import extract_grading_evidence
from opscloud.rubrics.jev_compiler import CompiledRubric
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# ── Declarative Dispatch & Threshold Rules ─────────────────────────────────

_CRITERION_PASS_THRESHOLD = 0.50
_MIN_READINESS_SCORE = 1.0


def build_criteria_evaluations(
    criteria_map: Mapping[str, str],
    response: ClassifierResponse,
    pass_threshold: float = _CRITERION_PASS_THRESHOLD,
) -> list[CriterionEval]:
    """Map Jev Noul probabilities into deepagents CriterionPass / CriterionFail list."""
    evals: list[CriterionEval] = []
    for cid, text in criteria_map.items():
        noul_answer = response.nouls.get(cid)
        prob = noul_answer.noul if noul_answer is not None else 0.0
        if prob >= pass_threshold:
            evals.append(CriterionPass(name=text, passed=True))
        else:
            evals.append(
                CriterionFail(
                    name=text,
                    passed=False,
                    gap=f"Criterion unsatisfied (confidence {prob:.2f} < {pass_threshold:.2f}): {text}",
                )
            )
    return evals


BlockerHandler = Callable[[ClassifierResponse, list[CriterionEval]], GraderResponse]

def _make_blocker_response(
    blocker_type: str,
    explanation: str,
    evals: list[CriterionEval],
) -> GraderResponse:
    resp = GraderResponse(
        result="needs_revision",
        explanation=explanation,
        criteria=evals,
    )
    object.__setattr__(resp, "blocker_type", blocker_type)
    return resp


BLOCKER_DISPATCH: dict[str, BlockerHandler] = {
    "external_blocker": lambda resp, evals: _make_blocker_response(
        "external_blocker",
        (
            "Environmental blocker detected by Jev (e.g. AWS service quota, "
            "IAM permissions, or network partition). Proposing dynamic rubric amendment."
        ),
        evals,
    ),
    "ambiguous_requirement": lambda resp, evals: _make_blocker_response(
        "ambiguous_requirement",
        (
            "Criteria cannot be verified because the requirements conflict with "
            "environmental constraints or are ambiguous. Operator clarification required."
        ),
        evals,
    ),
    "progressing": lambda resp, evals: (
        GraderResponse(
            result="satisfied",
            explanation=(
                f"All {len(evals)} criteria successfully verified via Jev System One "
                f"(Operational readiness: {getattr(resp.scores.get('readiness'), 'score', 2.0):.2f})."
            ),
            criteria=evals,
        )
        if all(c.get("passed", False) for c in evals)
        and getattr(resp.scores.get("readiness"), "score", 2.0) >= _MIN_READINESS_SCORE
        else GraderResponse(
            result="needs_revision",
            explanation=(
                f"{sum(1 for c in evals if not c.get('passed', False))} of {len(evals)} criteria "
                "not yet satisfied. Revision required."
            ),
            criteria=evals,
        )
    ),
}


# ── Jev Hybrid Rubric Grader ───────────────────────────────────────────────


class JevHybridRubricGrader:
    """Two-tier rubric evaluator: Jev System One fast-pass + Frontier LLM diagnostic fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        """Initialize Jev rubric grader with API key resolution."""
        if api_key is not None:
            self._api_key = api_key if api_key else None
            self._explicit_key = True
        else:
            settings_key = getattr(get_settings(), "typesafe_api_key", None)
            resolved_key = settings_key or resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",))
            self._api_key = resolved_key
            self._explicit_key = False

        self.timeout_seconds = timeout_seconds
        self._classifier: TypeSafeClassifier | None = None

    def _get_classifier(self) -> TypeSafeClassifier:
        """Lazily initialize TypeSafeClassifier instance."""
        if self._classifier is None:
            if self._api_key:
                self._classifier = TypeSafeClassifier(api_key=self._api_key)
            else:
                self._classifier = TypeSafeClassifier()
        return self._classifier

    def is_available(self) -> bool:
        """Check whether TypeSafe API credentials are configured."""
        if self._explicit_key:
            return bool(self._api_key)
        if self._api_key:
            return True
        settings_key = getattr(get_settings(), "typesafe_api_key", None)
        if settings_key:
            return True
        return bool(resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",)))

    async def agrade(
        self,
        compiled_rubric: CompiledRubric,
        evidence: dict[str, Any] | Sequence[Any] | str,
        *,
        fallback_llm_fn: Callable[[list[str]], Awaitable[GraderResponse] | GraderResponse] | None = None,
    ) -> GraderResponse:
        """Evaluate criteria against evidence asynchronously using Jev System One.

        Escalates to `fallback_llm_fn` only when a criterion fails and detailed remediation
        is requested.
        """
        classifier = self._get_classifier()
        start_time = time.monotonic()

        # Build clean state payload for Jev
        if isinstance(evidence, dict) and "commands_executed" in evidence:
            structured_evidence = evidence
        else:
            structured_evidence = extract_grading_evidence(
                evidence if isinstance(evidence, (Mapping, Sequence)) and not isinstance(evidence, (str, bytes)) else [evidence]
            )

        state_payload: State = cast(
            State,
            {
                "objective": compiled_rubric.objective,
                "evidence": structured_evidence,
            },
        )

        request: ClassifierRequest = {
            "state": state_payload,
            "questions": compiled_rubric.questions,
        }

        run_config: RunnableConfig = {
            "run_name": "jev_rubric_grader",
            "tags": ["opscloud:smart", "opscloud:jev", "opscloud:rubric"],
            "metadata": {
                "objective": compiled_rubric.objective[:100],
                "criteria_count": len(compiled_rubric.criteria_map),
            },
        }

        try:
            async with asyncio.timeout(self.timeout_seconds):
                response: ClassifierResponse = await classifier.ainvoke(
                    request,
                    config=run_config,
                )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Jev rubric evaluation failed or timed out (%d ms): %s", latency_ms, exc)
            raise

        latency_ms = int((time.monotonic() - start_time) * 1000)
        criteria_evals = build_criteria_evaluations(compiled_rubric.criteria_map, response)

        blocker = response.choices.get("blocker_status")
        blocker_key = blocker.choice if blocker else "progressing"

        handler = BLOCKER_DISPATCH.get(blocker_key, BLOCKER_DISPATCH["progressing"])
        verdict = handler(response, criteria_evals)

        readiness = getattr(response.scores.get("readiness"), "score", 0.0)
        logger.info(
            "Jev rubric fast-pass completed in %d ms: result=%s, readiness=%.2f, blocker=%s, passed=%d/%d",
            latency_ms,
            verdict.result,
            readiness,
            blocker_key,
            sum(1 for c in criteria_evals if c.get("passed", False)),
            len(criteria_evals),
        )

        # ── Tier 2 Diagnostic Escalation ────────────────────────────────────
        # When criteria fail under normal execution, escalate to generative LLM
        # to author targeted remediation feedback for failing requirements.
        failed_items = [c.get("name", "") for c in criteria_evals if not c.get("passed", False)]
        if not failed_items and readiness < _MIN_READINESS_SCORE:
            failed_items = [f"Operational readiness score ({readiness:.2f}) below threshold ({_MIN_READINESS_SCORE:.2f})"]

        if verdict.result == "needs_revision" and failed_items and fallback_llm_fn and blocker_key == "progressing":
            logger.info(
                "Jev fast-pass detected %d failing criteria; invoking Tier 2 LLM diagnostic for remediation advice.",
                len(failed_items),
            )
            try:
                llm_res = fallback_llm_fn(failed_items)
                if inspect.isawaitable(llm_res):
                    llm_verdict = await llm_res
                else:
                    llm_verdict = llm_res
                if isinstance(llm_verdict, GraderResponse):
                    if not llm_verdict.criteria:
                        object.__setattr__(llm_verdict, "criteria", criteria_evals)
                    return llm_verdict
            except Exception as llm_exc:
                logger.warning("Tier 2 LLM diagnostic fallback failed: %s; returning Jev verdict", llm_exc)

        return verdict

    def grade(
        self,
        compiled_rubric: CompiledRubric,
        evidence: dict[str, Any] | Sequence[Any] | str,
        *,
        fallback_llm_fn: Callable[[list[str]], GraderResponse] | None = None,
    ) -> GraderResponse:
        """Synchronous version of grade."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(
                    asyncio.run,
                    self.agrade(compiled_rubric, evidence, fallback_llm_fn=fallback_llm_fn),  # type: ignore[arg-type]
                ).result()
        return asyncio.run(self.agrade(compiled_rubric, evidence, fallback_llm_fn=fallback_llm_fn))  # type: ignore[arg-type]


__all__ = [
    "BLOCKER_DISPATCH",
    "JevHybridRubricGrader",
    "build_criteria_evaluations",
]
