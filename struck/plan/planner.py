"""The pipeline's plan source: ``Planner`` (protocol) and ``FixedPlanner``
(a plan file the human wrote or saved from the Chat dock). LLM proposals come
from the Chat dock (``struck.chat.assistant``, stock structured output).

The planner only PROPOSES (the human-in-the-loop law). It has no tools, it is never asked to
approve, and nothing after the plan node consults it.
"""

from __future__ import annotations

from typing import Protocol


from .models import PlanProposal


class PlannerError(RuntimeError):
    pass


class Planner(Protocol):
    def propose(self, request: str) -> PlanProposal: ...


class FixedPlanner:
    """A fixed plan document instead of a live LLM (proof runs, tests). It is
    validated by the same strict model as LLM output: a bad shape raises."""

    def __init__(self, plan_json: str) -> None:
        self._plan_json = plan_json

    def propose(self, request: str) -> PlanProposal:
        return PlanProposal.model_validate_json(self._plan_json)
