"""Errores de la capa de acciones (mensajes pensados para mostrarse al usuario)."""

from __future__ import annotations

from pathlib import Path


class ActionError(Exception):
    """Base: la operación no se realizó y no se modificó nada."""


class SourceMissingError(ActionError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"El archivo ya no existe: {path}")
        self.path = path


class SourceChangedError(ActionError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"El archivo cambió desde la operación original: {path}")
        self.path = path


class DestinationOccupiedError(ActionError):
    def __init__(self, path: Path, suggestion: Path | None = None) -> None:
        hint = f" (nombre libre sugerido: {suggestion.name})" if suggestion else ""
        super().__init__(f"Ya existe un archivo en {path}{hint}")
        self.path = path
        self.suggestion = suggestion


class InvalidTargetError(ActionError):
    pass


class ConcurrentOperationError(ActionError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"Hay otra operación en curso sobre {path}")
        self.path = path


class NotUndoableError(ActionError):
    pass
