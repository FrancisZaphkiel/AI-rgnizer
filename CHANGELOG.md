# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones
según [SemVer](https://semver.org/lang/es/).

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
