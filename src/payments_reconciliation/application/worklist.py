"""The worklist: the breaks nothing in the code can resolve safely."""

from __future__ import annotations

from payments_reconciliation.application.ports import StoredBreak, UnitOfWorkFactory


class GetWorklist:
    """Breaks waiting for a person, oldest first."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def execute(self) -> list[StoredBreak]:
        """Every break with status NEEDS_REVIEW."""
        with self._uow_factory() as uow:
            return uow.breaks.worklist()
