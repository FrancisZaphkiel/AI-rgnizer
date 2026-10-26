# Organizador

Organizador inteligente de archivos para Linux (probado en CachyOS). Aprende cómo organizas
tus carpetas, sugiere destinos para lo que llega a Descargas y detecta duplicados. Todo es local
y reversible: nada se borra, solo se envía a la papelera.

Estado: Fases 0–3 completas por CLI (núcleo, acciones seguras, duplicados y aprendizaje de
costumbres); Fase 4 en curso (vigilancia de Descargas). Última versión publicada `v0.1.0`. Ver [`PLAN.md`](PLAN.md) y [`CHANGELOG.md`](CHANGELOG.md).

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
uv run organizador dupes ~/Documentos          # duplicados exactos (solo muestra)
uv run organizador dupes ~/Documentos --trash  # pregunta por grupo y envía sobrantes a la papelera
uv run organizador similar ~/Imágenes          # imágenes casi idénticas (solo candidatos)
uv run organizador similar ~/Imágenes --trash  # confirmación obligatoria grupo a grupo (sin --yes)
uv run organizador names ~/Descargas ~/Documentos  # nombres parecidos: solo indicios, no borra nada
uv run organizador history
uv run organizador undo ID
```

### Aprendizaje de costumbres

```fish
uv run organizador train ~/Documentos ~/Música ~/Imágenes ~/Vídeos  # aprende de lo ya organizado
uv run organizador evaluate                     # precisión con archivos no vistos (top-1/top-3, ECE)
uv run organizador suggest ~/Descargas/*.pdf    # top-3 destinos y nombre según la carpeta; no mueve
uv run organizador mv -p ARCHIVO CARPETA        # mueve y aprende (--no-learn para no aprender)
uv run organizador teach ARCHIVO CARPETA        # corrige al modelo sin mover
uv run organizador review [--move]              # archivos posiblemente mal ubicados
uv run organizador patterns                     # cómo nombras los archivos de cada carpeta
uv run organizador model list | model revert N  # versiones del modelo
```

El destino se decide por prioridad: reglas de `config.toml` → reglas aprendidas → modelo (si
está seguro) → carpeta cuyo nombre aparece en el del archivo → destino por tipo (carpetas
XDG) → sin clasificar. Ejemplo de configuración:

```toml
indexed_dirs = ["~/Documentos", "~/Música", "~/Imágenes", "~/Vídeos"]

[learning]
exclude = ["~/Documentos/camerapy"]   # carpetas que no reflejan tus costumbres

[learning.rules]                      # mandan sobre todo lo aprendido
"ext:jpg" = "~/Imágenes"

[learning.type_destinations]          # destino inicial por tipo
"tipo:image" = "~/Imágenes"
```

### Vigilancia de Descargas

```fish
uv run organizador watch              # vigila watched_dirs (por defecto, Descargas) hasta Ctrl+C
uv run organizador watch --once       # revisa una vez y termina
uv run organizador pending            # sugerencias pendientes con su id
uv run organizador accept ID [--to CARPETA]   # mueve (se puede deshacer) y aprende
uv run organizador ignore ID...       # el archivo se queda donde está
```

Solo se consideran los archivos directamente dentro de la carpeta vigilada, y solo cuando la
descarga terminó: se ignoran `.part`, `.crdownload` y el marcador vacío de Firefox, y el
tamaño debe mantenerse estable unos segundos. Cada archivo recibe una sola sugerencia
aunque se reinicie la vigilancia. En modo automático (`--auto` o `mode = "auto"`) se mueve
sin preguntar solo lo que decide una regla tuya, una regla aprendida o el modelo con
probabilidad calibrada ≥ `auto_threshold`; el resto queda pendiente.

```toml
[watch]
mode = "suggest"         # o "auto"
auto_threshold = 0.9
stable_seconds = 3.0
reconcile_seconds = 300  # relectura completa por si inotify pierde eventos
```

Nada sale del equipo (hay un test que bloquea los sockets de red) y el modelo solo sugiere:
mover o renombrar siempre pasa por las acciones seguras y se puede deshacer.

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
| `~/.local/share/organizador/models/` | Versiones del modelo (`model-NNNN.joblib`, `current`) |
| `~/.local/state/organizador/logs/` | Registros |
