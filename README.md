# Organizador

Organizador inteligente de archivos para Linux (probado en CachyOS). Aprende cómo organizas
tus carpetas, sugiere destinos para lo que llega a Descargas y detecta duplicados. Todo es local
y reversible: nada se borra, solo se envía a la papelera.

Estado: en desarrollo (Fase 1). Ver [`PLAN.md`](PLAN.md).

## Instalación para desarrollo (CachyOS / Arch)

```fish
sudo pacman -S --needed python uv git
uv sync                      # crea .venv con dependencias y herramientas de desarrollo
uv run pre-commit install    # hooks de ruff y mypy
```

## Uso

```fish
uv run organizador paths             # rutas XDG de la aplicación
uv run organizador scan ~/Documentos # indexa una carpeta (no modifica archivos)
uv run organizador mv ARCHIVO DESTINO
uv run organizador trash ARCHIVO     # envía a la papelera del sistema
uv run organizador history
uv run organizador undo ID
```

## Desarrollo

```fish
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov
```

Flujo de ramas: Git Flow (`feature/*` desde `develop`). Commits con Conventional Commits.

### Reglas de seguridad del código

- Solo `organizador.actions.fsops` puede borrar del sistema de archivos (`os.unlink` y afines
  están prohibidos en el resto mediante ruff `TID251`, y un test lo verifica).
- Toda operación sobre archivos pasa por `ActionService` y queda en el journal **antes** de
  ejecutarse.
- Nunca se sobrescribe un archivo: los renombrados usan `renameat2(RENAME_NOREPLACE)`.

## Datos

| Ruta | Contenido |
|---|---|
| `~/.config/organizador/config.toml` | Configuración |
| `~/.local/share/organizador/organizador.db` | Índice, journal y aprendizaje (SQLite) |
| `~/.local/state/organizador/logs/` | Registros |
