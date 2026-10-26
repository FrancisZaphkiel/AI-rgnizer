## Benchmark Fase 2

Fecha: 2026-10-25

| Condición | Valor |
|---|---|
| CPU | Intel(R) Core(TM) i7-14700HX (28 hilos) |
| RAM | 15.3 GiB |
| Disco | SAMSUNG MZVL81T0HELB-00BH1 nvme (SSD) |
| Sistema de archivos | btrfs |
| Kernel | 7.3.0-4-cachyos |
| Python | 3.14.8 |
| Árbol | 50000 archivos en 556 carpetas, 1.62 GiB, 10% duplicados |
| Tamaños | 80 % de 1–16 KiB, 19,5 % de 16–128 KiB, 0,5 % de 1–4 MiB |
| Caché | de páginas caliente (el árbol se acaba de escribir o leer) |

| Medida | Resultado | Objetivo |
|---|---|---|
| Escaneo inicial | 2.3 s | < 120 s ✅ |
| Escaneo incremental sin cambios | 1.1 s | < 15 s ✅ |
| Duplicados exactos (1ª vez) | 5.5 s: 38848 archivos, 572 MB → 7030 archivos/s, 103 MB/s; 3156 grupos | informativo |
| Duplicados exactos (hashes en caché) | 2.2 s | informativo |
| BLAKE3 de un archivo (4.2 MB) | 940 MB/s | informativo |
| Nombres parecidos | 2.0 s | informativo |

Cómo reproducirlo: `uv run python benchmarks/bench_fase2.py` (genera el árbol en
`~/.cache/airgnizer-bench`, en el SSD y no en `/tmp`, que es tmpfs; lo borra al terminar
salvo con `--keep`).

### Antes y después de optimizar

Medido en el mismo equipo y árbol sintético.

| Medida | Antes | Después | Cambio principal |
|---|---|---|---|
| Escaneo inicial | 13,8 s | 2,3 s | SQLAlchemy Core con inserciones por lotes en lugar de objetos ORM |
| Escaneo incremental | 4,6 s | 1,0 s | no se reescriben los archivos sin cambios; los eliminados se detectan carpeta a carpeta |
| Duplicados exactos (1ª vez) | 66,9 s | 5,5 – 16,5 s | hashes guardados con una sentencia preparada (`executemany`) en lugar de un `UPDATE` ORM por archivo |
| Nombres parecidos | 55,6 s | 2,0 s | las series (nombres que solo difieren en números) no llegan a compararse |

### Árbol real (`~/Documentos`, 162 637 archivos)

| Medida | Antes | Después |
|---|---|---|
| Escaneo inicial | — | 33,8 s |
| Escaneo incremental sin cambios | ≈ 50 s | 12,5 s (≈ 3,8 s por cada 50 000 archivos) |
| `dupes` la primera vez (sin escaneo) | — | 51,9 s, 25 275 grupos |

### Notas

- **Duplicados exactos, variación de 5,5 a 16,5 s:** con un árbol recién escrito, `FIEMAP`
  (con `FIEMAP_FLAG_SYNC`, para medir el espacio real) fuerza la escritura pendiente de
  cada archivo de los grupos. Con un árbol ya asentado en disco, el caso normal, es el valor bajo.
- **MB/s frente a archivos/s:** con archivos de ~15 KiB de media el límite es la cantidad de
  archivos (abrir, `lstat`, escribir en el índice: ~0,15 ms por archivo), no el ancho de banda.
  BLAKE3 sobre un archivo grande da ~900 MB/s.
- **Árbol real más lento por archivo que el sintético:** tiene muchas más carpetas por archivo
  (una consulta al índice por carpeta).
- **Mejora futura posible:** hashear en varios hilos (BLAKE3 y la lectura liberan el GIL).
