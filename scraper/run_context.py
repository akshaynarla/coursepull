"""Runtime resources shared by all nodes of one run. Not checkpointed.

LangGraph injects this into every node as the run context; the graph state only
holds data, this holds the things that do work or enforce limits.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import BUDGET_SOFT_RATIO, MAX_PAGES_PER_RUN, RUN_TOKEN_BUDGET
from .llm import LLMRouter

log = logging.getLogger(__name__)


class Budget:
    """Token budget across all agents. Soft limit: wind down. Hard limit: stop."""

    def __init__(self, limit: int = RUN_TOKEN_BUDGET, soft_ratio: float = BUDGET_SOFT_RATIO,
                 spent: dict[str, int] | None = None) -> None:
        self.limit = limit
        self.soft_limit = int(limit * soft_ratio)
        self._spent: dict[str, int] = dict(spent or {})
        self._lock = threading.Lock()

    def charge(self, role: str, tokens: int) -> None:
        with self._lock:
            self._spent[role] = self._spent.get(role, 0) + tokens

    @property
    def used(self) -> int:
        with self._lock:
            return sum(self._spent.values())

    def by_role(self) -> dict[str, int]:
        with self._lock:
            return dict(self._spent)

    def soft_exceeded(self) -> bool:
        return self.used >= self.soft_limit

    def hard_exceeded(self) -> bool:
        return self.used >= self.limit


@dataclass
class RunContext:
    output_dir: Path
    llm: LLMRouter
    budget: Budget = field(default_factory=Budget)
    cancel: threading.Event = field(default_factory=threading.Event)
    max_pages: int = MAX_PAGES_PER_RUN
    pages: object = None  # PageStore, created lazily (import cycle)

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if self.pages is None:
            from .agents.toolbox import PageStore

            self.pages = PageStore(self.max_pages)

    def stop_reason(self) -> str | None:
        if self.cancel.is_set():
            return "cancelled"
        if self.budget.hard_exceeded():
            return "budget"
        return None


def emit(event: dict) -> None:
    """Send an event to whoever is streaming the graph (the UI, the CLI, tests).

    Only works on a node's own thread; elsewhere the event is logged instead.
    """
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - outside a graph run, e.g. in unit tests
        log.debug("event (no stream): %s", event)
