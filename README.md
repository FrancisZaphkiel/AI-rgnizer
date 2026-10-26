# Organizador

Organizador inteligente de archivos para Linux (probado en CachyOS). Aprende cómo organizas
tus carpetas, sugiere destinos para lo que llega a Descargas y detecta duplicados. Todo es local
y reversible: nada se borra, solo se envía a la papelera.

Estado: Fases 0–4 completas (núcleo, acciones seguras, duplicados, aprendizaje de costumbres y
vigilancia de Descargas como servicio con API D-Bus); última versión publicada `v0.2.0`. Ver [`PLAN.md`](PLAN.md) y [`CHANGELOG.md`](CHANGELOG.md).

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
uv run organizador watched            # carpetas vigiladas, su sistema de archivos y si están conectadas
uv run organizador watched add /run/media/$USER/Disco/Descargas
uv run organizador watched remove CARPETA
```

Se pueden vigilar varias carpetas a la vez, también en discos externos con cualquier sistema
de archivos (ext4, Btrfs, NTFS, exFAT, FAT…). Un disco desconectado no da error: se empieza
a vigilar al conectarlo, se relee entero y sus sugerencias pendientes se conservan. En
sistemas de red (NFS, SMB, sshfs) se sondea cada 5 s, porque inotify no ve los cambios
hechos desde otro equipo. Mover entre discos copia, verifica con hash y solo después borra
el original, y funciona en sistemas sin permisos POSIX ni enlaces duros.

Solo se consideran los archivos directamente dentro de la carpeta vigilada, y solo cuando la
descarga terminó, con cualquier navegador o gestor de descargas: se ignoran los temporales
(`.part` y el marcador vacío de Firefox y derivados; `.crdownload` de Chrome, Chromium,
Brave, Edge, Vivaldi y Opera; `.wkdownload`, `.opdownload`, `.kget`, `.!qB`, `.aria2`…), el
tamaño debe mantenerse estable unos segundos y ningún programa debe tenerlo abierto para
escritura (así se cubren wget, curl o cualquiera que escriba directamente en el nombre final). Cada archivo recibe una sola sugerencia
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

### Servicio y API D-Bus

```fish
uv run organizador service install    # unidad systemd --user (Restart=on-failure) y la arranca
uv run organizador service status
uv run organizador service uninstall
uv run organizador daemon             # el mismo servicio en primer plano (--no-notify)
```

El servicio vigila las carpetas, muestra notificaciones con botones («Mover a X», «Elegir…»,
«Ignorar»; «Deshacer» tras un movimiento automático) y atiende la API D-Bus que usará la
interfaz gráfica: bus de sesión, nombre `io.github.organizador.Organizador`, interfaz
versionada `io.github.organizador.Organizador1`. El protocolo está documentado en
[`service/protocol.py`](src/organizador/service/protocol.py). Prueba rápida:

```fish
busctl --user call io.github.organizador.Organizador /io/github/organizador/Organizador \
    io.github.organizador.Organizador1 ListPending
```

Todas las escrituras del servicio pasan por un único hilo, y solo puede haber un servicio
por usuario.

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
