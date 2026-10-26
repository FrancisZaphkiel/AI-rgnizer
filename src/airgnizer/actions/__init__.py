"""Acciones seguras sobre archivos: mover, renombrar, papelera, deshacer y recuperación."""

from airgnizer.actions.errors import ActionError
from airgnizer.actions.service import ActionResult, ActionService, RecoveryOutcome

__all__ = ["ActionError", "ActionResult", "ActionService", "RecoveryOutcome"]
