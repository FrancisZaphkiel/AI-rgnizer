# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones
según [SemVer](https://semver.org/lang/es/).

## [Sin publicar]

Fase 3: aprendizaje de costumbres, desde la línea de comandos. La versión `v0.2.0` se
publicará al cerrar la Fase 4 (vigilancia de Descargas), según el plan.

### Añadido

- **Vigilancia de Descargas** (`watch [--once] [--auto]`, `pending`, `accept`, `ignore`):
  watchdog sobre `watched_dirs` con reconciliación periódica; una descarga cuenta cuando
  no es temporal (`.part`, `.crdownload`, marcador vacío de Firefox) y su tamaño está
  estable. Cola de sugerencias en la base de datos (tabla `suggestions`, migración 0005)
  sin duplicados al reiniciar. Modo automático que solo mueve con reglas o probabilidad
  calibrada ≥ `watch.auto_threshold`; aceptar una sugerencia mueve y aprende.
- **Cualquier navegador o gestor de descargas**: temporales de la familia Firefox,
  Chromium, WebKitGTK, Opera, KGet, qBittorrent, µTorrent y aria2, y comprobación general
  de que ningún proceso tiene el archivo abierto para escritura (`/proc`).
- **Varias carpetas de descargas** (`watched list|add|remove`), también en discos externos:
  se vigilan al conectarse y se dejan al desconectarse, sin perder sugerencias; sondeo en
  sistemas de archivos de red.
- **Servicio** (`daemon`, `service install|uninstall|status`): unidad `systemd --user`
  (`Type=dbus`, `Restart=on-failure`) con activación D-Bus; un único hilo escritor y una
  sola instancia por usuario. **API D-Bus** versionada (`ListPending`, `Accept`, `Ignore`,
  `Undo`, `History`, `Rescan`; señales `SuggestionAdded` y `SuggestionResolved`).
- **Notificaciones de escritorio** con acciones; «Elegir…» usa el selector de carpetas del
  portal (nativo de KDE en Plasma), con `kdialog`/`zenity` como alternativa.
- Deshacer el movimiento de una descarga vuelve a dejar pendiente su sugerencia.

- **Características por archivo** (caché en el índice): palabras, fechas, versión y marcas
  de copia del nombre; extensión, MIME (libmagic), tamaño, EXIF, metadatos PDF y de audio,
  y dominio de origen (`user.xdg.origin.url`).
- **Modelo de destino** (`train`, `evaluate`, `suggest`): carpetas ya organizadas como
  ejemplos (sin proyectos de software, duplicados ni carpetas con pocos archivos);
  `SGDClassifier` sobre tokens y n-gramas del nombre, calibrado con *temperature scaling* y
  con abstención («sin clasificar»); reglas «token → carpeta» aprendidas; explicación con
  los tokens reales del archivo. En datos reales: top-1 96,7 %, top-3 98,7 %, ECE 0,017.
- **Prioridad de destinos**: reglas de `config.toml` → reglas aprendidas → modelo → nombre
  de carpeta existente → destino por tipo (carpetas XDG) → sin clasificar. Funciona sin
  modelo entrenado.
- **Aprendizaje continuo**: `mv` y `teach` registran la elección y ajustan el modelo; una
  carpeta nueva se aprende con un reentrenamiento automático tras `retrain_after` archivos.
  Versiones del modelo con `model list` / `model revert`.
- **Revisión** (`review [--move]`): archivos posiblemente mal ubicados, juzgados con
  validación cruzada; ignora estructuras exportadas y copias deliberadas.
- **Patrones de nombres** (`patterns`): plantillas por carpeta
  (`2024-05-10_informe_v2.pdf` → `{fecha:AAAA-MM-DD}_{texto}_v{n}`) y, en `suggest`, el
  nombre que tendría el archivo según el patrón de su destino.

### Corregido

- Mover a o desde exFAT, FAT y FUSE: ya no falla por `chmod` ni por falta de enlaces duros.
- Deshacer un movimiento hacia exFAT o FAT ya no lo rechaza por el redondeo del mtime.
- La vigilancia ignora aperturas y lecturas: antes, analizar un archivo (o que Baloo o un
  visor lo leyeran) lo volvía a encolar en bucle (~7 % de CPU en reposo; ahora 0,2 %).

### Seguridad

- Ningún dato sale del equipo: test que bloquea sockets `AF_INET`/`AF_INET6`.
- Los modelos (joblib/pickle) solo se cargan desde la carpeta de datos propia (0700) y se
  podan a través de `fsops`.

## [0.1.0] — 2026-10-25

Primer hito: núcleo, acciones seguras y duplicados, desde la línea de comandos (Fases 0–2).

### Añadido

- **Índice** (`organizador scan`): escaneo incremental de solo lectura en SQLite (WAL), con
  exclusiones, symlinks, montajes y registro de errores. Los nombres que empiezan por
  puntos suspensivos («... Baby One More Time.mp3») no se tratan como ocultos.
- **Acciones seguras** (`mv`, `rename`, `trash`, `undo`, `history`, `recover`): journal de
  estados escrito antes de tocar el disco, sin sobrescribir nunca (`RENAME_NOREPLACE`),
  recuperación tras cortes y papelera freedesktop propia, visible en Dolphin.
- **Duplicados exactos** (`organizador dupes`): tamaño → xxh3 parcial (solo filtro) →
  BLAKE3. Espacio recuperable real, sin contar hardlinks ni reflinks/instantáneas de Btrfs
  (`FIEMAP`). Propone qué copia conservar y explica por qué (carpeta organizada, fuera de
  Descargas, sin sufijo de copia, creada antes según `statx`); el usuario puede elegir otra.
- **Imágenes similares** (`organizador similar`): pHash con orientación EXIF, umbral
  calibrado (4 bits), grupos en estrella. Son candidatos: la papelera exige confirmar cada
  grupo y no existe `--yes`.
- **Nombres parecidos** (`organizador names`): solo indicios para revisión manual; descarta
  series numeradas e ID de yt-dlp. Nunca envía nada a la papelera.
- Benchmark reproducible (`benchmarks/bench_fase2.py`) con resultados en
  [`docs/benchmarks/fase2.md`](docs/benchmarks/fase2.md).

### Seguridad

- Solo `organizador.actions.fsops` puede borrar del sistema de archivos (ruff `TID251` y una
  prueba que lo audita).
- Antes de enviar duplicados a la papelera se verifica por hash la copia conservada y cada
  copia sobrante, bajo el bloqueo de la acción.

### Limitaciones conocidas

- Sin CI todavía: no hay remoto git configurado.
- En un subvolumen Btrfs anidado distinto de `~`, la papelera es `<subvolumen>/.Trash-1000`,
  que Dolphin puede no mostrar.
- En FAT/exFAT, la precisión de las fechas puede hacer que `undo` crea que el archivo cambió.
- El pHash confunde capturas de pantalla distintas de una misma aplicación (por eso los
  similares son solo candidatos).
