# AI-rgnizer

**AI-rgnizer** es un organizador inteligente de archivos para Linux (probado en CachyOS con
KDE Plasma). Aprende cómo organizas tus carpetas, sugiere destinos para lo que llega a
Descargas, propone nombres según tus patrones y detecta duplicados. Todo es local y
reversible: nada se borra, solo se envía a la papelera, y cada acción se puede deshacer.

Estado: Fases 0–5 completas y empaquetado como AppImage. Ver [`PLAN.md`](PLAN.md) y
[`CHANGELOG.md`](CHANGELOG.md).

## Instalación (AppImage)

Descarga `AI-rgnizer-<versión>-x86_64.AppImage` (de las *releases* del repositorio o
construyéndola, ver [Construir la AppImage](#construir-la-appimage)), guárdala en una ruta
fija y ábrela:

```fish
mkdir -p ~/Aplicaciones
mv AI-rgnizer-*-x86_64.AppImage ~/Aplicaciones/
chmod +x ~/Aplicaciones/AI-rgnizer-*-x86_64.AppImage
~/Aplicaciones/AI-rgnizer-*-x86_64.AppImage           # abre la ventana
```

- La primera vez se añade sola al **menú de aplicaciones** (con su icono). Si mueves el
  archivo, ábrelo una vez desde la ruta nueva y el menú se actualiza.
- **Ajustes → «Vigilar las descargas al iniciar sesión»** instala el servicio de systemd,
  que ejecuta la propia AppImage. Por eso conviene una ruta fija: si la mueves, ábrela una
  vez desde la ruta nueva y el servicio (y el menú) se actualizan solos.
- La CLI es la misma AppImage con argumentos (`./AI-rgnizer-…AppImage scan ~/Documentos`).
  En los ejemplos de más abajo, cambia `uv run airgnizer` por la ruta de la AppImage.
- Lleva su propio Python y Qt; del sistema solo usa glibc, `libmagic` (paquete `file`) y
  el escritorio. En KDE sigue el modo claro u oscuro y usa el selector de archivos nativo.
- Para quitarla: desactiva el servicio y el inicio en Ajustes, ejecuta
  `./AI-rgnizer-…AppImage desktop uninstall` y borra el archivo. Tus datos quedan en las
  rutas de [Datos](#datos).

### Si usabas «organizador» (≤ 0.3.0)

El proyecto se llamaba **organizador**. La primera vez que abras AI-rgnizer:

1. detiene y retira el servicio antiguo (`organizador.service`), su activación D-Bus y su
   entrada de inicio;
2. **traslada sin copiar** tus datos: `~/.config/organizador` → `~/.config/airgnizer`,
   `~/.local/share/organizador/organizador.db` → `~/.local/share/airgnizer/airgnizer.db`, y
   lo mismo en `~/.local/state` y `~/.cache`;
3. vuelve a activar con el nombre nuevo lo que tuvieras activado (servicio, bandeja).

Nunca sobrescribe datos que ya existan con el nombre nuevo, y no traslada nada mientras el
servicio antiguo siga en marcha.

## Instalación para desarrollo (CachyOS / Arch)

```fish
sudo pacman -S --needed python uv git
uv sync                      # crea .venv con dependencias y herramientas de desarrollo
uv run pre-commit install    # hooks de ruff y mypy
```

## Uso

```fish
uv run airgnizer paths             # rutas XDG de la aplicación
uv run airgnizer scan ~/Documentos # indexa una carpeta (no modifica archivos)
uv run airgnizer mv ARCHIVO DESTINO
uv run airgnizer trash ARCHIVO     # envía a la papelera del sistema
uv run airgnizer dupes ~/Documentos          # duplicados exactos (solo muestra)
uv run airgnizer dupes ~/Documentos --trash  # pregunta por grupo y envía sobrantes a la papelera
uv run airgnizer similar ~/Imágenes          # imágenes casi idénticas (solo candidatos)
uv run airgnizer similar ~/Imágenes --trash  # confirmación obligatoria grupo a grupo (sin --yes)
uv run airgnizer names ~/Descargas ~/Documentos  # nombres parecidos: solo indicios, no borra nada
uv run airgnizer history
uv run airgnizer undo ID
```

### Aprendizaje de costumbres

```fish
uv run airgnizer train ~/Documentos ~/Música ~/Imágenes ~/Vídeos  # aprende de lo ya organizado
uv run airgnizer evaluate                     # precisión con archivos no vistos (top-1/top-3, ECE)
uv run airgnizer suggest ~/Descargas/*.pdf    # top-3 destinos y nombre según la carpeta; no mueve
uv run airgnizer mv -p ARCHIVO CARPETA        # mueve y aprende (--no-learn para no aprender)
uv run airgnizer teach ARCHIVO CARPETA        # corrige al modelo sin mover
uv run airgnizer review [--move]              # archivos posiblemente mal ubicados
uv run airgnizer patterns                     # cómo nombras los archivos de cada carpeta
uv run airgnizer model list | model revert N  # versiones del modelo
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
uv run airgnizer watch              # vigila watched_dirs (por defecto, Descargas) hasta Ctrl+C
uv run airgnizer watch --once       # revisa una vez y termina
uv run airgnizer pending            # sugerencias pendientes con su id
uv run airgnizer accept ID [--to CARPETA]   # mueve (se puede deshacer) y aprende
uv run airgnizer ignore ID...       # el archivo se queda donde está
uv run airgnizer watched            # carpetas vigiladas, su sistema de archivos y si están conectadas
uv run airgnizer watched add /run/media/$USER/Disco/Descargas
uv run airgnizer watched remove CARPETA
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
uv run airgnizer service install    # unidad systemd --user (Restart=on-failure) y la arranca
uv run airgnizer service status
uv run airgnizer service uninstall
uv run airgnizer daemon             # el mismo servicio en primer plano (--no-notify)
```

El servicio vigila las carpetas, muestra notificaciones con botones («Mover a X», «Elegir…»,
«Ignorar»; «Deshacer» tras un movimiento automático) y atiende la API D-Bus que usará la
interfaz gráfica: bus de sesión, nombre `io.github.airgnizer.AIrgnizer`, interfaz
versionada `io.github.airgnizer.AIrgnizer1`. El protocolo está documentado en
[`service/protocol.py`](src/airgnizer/service/protocol.py). Prueba rápida:

```fish
busctl --user call io.github.airgnizer.AIrgnizer /io/github/airgnizer/AIrgnizer \
    io.github.airgnizer.AIrgnizer1 ListPending
```

Todas las escrituras del servicio pasan por un único hilo, y solo puede haber un servicio
por usuario. Mientras está en marcha es el **único escritor**: los comandos que modifican
archivos (`mv`, `rename`, `trash`, `undo`, `accept`, `ignore`, `teach`, `review --move`,
`dupes --trash`, `similar --trash`) se lo piden por D-Bus, y `watch` se niega a arrancar.
Solo se delega en un servicio que use la misma base de datos; `--local` lo evita.
`scan` y `train` siguen siendo locales: escriben el índice y el modelo, nunca tus archivos,
y el servicio recarga el modelo al detectar una versión nueva.

Nada sale del equipo (hay un test que bloquea los sockets de red) y el modelo solo sugiere:
mover o renombrar siempre pasa por las acciones seguras y se puede deshacer.

### Interfaz gráfica

```fish
uv sync --extra gui               # PySide6-Essentials
uv run airgnizer gui              # ventana principal + icono de bandeja
uv run airgnizer gui --hidden     # solo la bandeja (lo usa el arranque con la sesión)
uv run airgnizer desktop install  # entrada en el menú de aplicaciones (la AppImage lo hace sola)
```

Las carpetas de **aprendizaje** (Ajustes → «Carpetas organizadas») y las de **búsqueda de
duplicados** (sección Duplicados → «Buscar duplicados en») son listas distintas; si la de
duplicados está vacía se usan las organizadas. Cada «Añadir…» ofrece también las unidades
montadas (`/run/media`, `/media`, `/mnt` y de red).

En **Descargas**, **Abrir** (o doble clic) abre el archivo con su aplicación predeterminada
(visor de imágenes, reproductor, editor…) para comprobar la sugerencia antes de aceptarla.
Si el servicio está en marcha la GUI le pide las operaciones por D-Bus y se actualiza con
sus señales; si no, trabaja en local. Enviar a la papelera pide siempre confirmación y todo
se puede deshacer desde **Historial**. **Ajustes → Borrar datos aprendidos** elimina la
base de datos y los modelos (no tus archivos ni la configuración).

## Desarrollo

```fish
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov
```

Flujo de ramas: Git Flow (`feature/*` desde `develop`). Commits con Conventional Commits.

### Construir la AppImage

```fish
packaging/appimage/build.sh            # → dist/AI-rgnizer-<versión>-x86_64.AppImage
packaging/appimage/build.sh --appdir   # solo build/appimage/AI-rgnizer.AppDir (probar con su AppRun)
```

Usa el Python portable de uv (python-build-standalone), instala exactamente las versiones
de `uv.lock`, quita de Qt y de Python lo que la aplicación no usa y empaqueta con
[appimagetool](https://github.com/AppImage/appimagetool) (lo descarga la primera vez en
`build/tools/`). Al publicar una etiqueta `v*`, el flujo de GitHub Actions
`.github/workflows/appimage.yml` la construye y la adjunta a la *release*.

### Reglas de seguridad del código

- Solo `airgnizer.actions.fsops` puede borrar del sistema de archivos (`os.unlink` y afines
  están prohibidos en el resto mediante ruff `TID251`, y un test lo verifica).
- Toda operación sobre archivos pasa por `ActionService` y queda en el journal **antes** de
  ejecutarse.
- Nunca se sobrescribe un archivo: los renombrados usan `renameat2(RENAME_NOREPLACE)`.

## Datos

| Ruta | Contenido |
|---|---|
| `~/.config/airgnizer/config.toml` | Configuración |
| `~/.local/share/airgnizer/airgnizer.db` | Índice, journal y aprendizaje (SQLite) |
| `~/.local/share/airgnizer/models/` | Versiones del modelo (`model-NNNN.joblib`, `current`) |
| `~/.local/state/airgnizer/logs/` | Registros |
