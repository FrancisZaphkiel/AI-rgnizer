"""Acciones seguras sobre archivos: mover, renombrar, papelera, deshacer y recuperación."""

from organizador.actions.errors import ActionError
from organizador.actions.service import ActionResult, ActionService, RecoveryOutcome

__all__ = ["ActionError", "ActionResult", "ActionService", "RecoveryOutcome"]
