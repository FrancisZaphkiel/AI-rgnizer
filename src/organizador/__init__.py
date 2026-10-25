"""Organizador inteligente de archivos para Linux."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("organizador")
except PackageNotFoundError:  # pragma: no cover - ejecución sin instalar
    __version__ = "0.0.0"

APP_NAME = "organizador"

__all__ = ["APP_NAME", "__version__"]
