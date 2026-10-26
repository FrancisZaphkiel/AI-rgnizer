"""Benchmark de cierre de la Fase 2 (ver PLAN.md).

Mide por separado, sobre un árbol sintético de 50 000 archivos:

1. escaneo inicial de metadatos (objetivo < 2 min);
2. escaneo incremental sin cambios (objetivo < 15 s);
3. búsqueda de duplicados exactos (hashing, en MB/s);
4. nombres parecidos.

Uso (genera ~1,5 GiB; por defecto en ~/.cache, que suele estar en el SSD, no en tmpfs):

    uv run python benchmarks/bench_fase2.py [--dir RUTA] [--files 50000] [--keep]

El árbol se reutiliza entre ejecuciones si ya existe con la misma semilla y tamaño.
Escribe los resultados en Markdown por la salida estándar.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from airgnizer.core.config import DuplicateSettings, ScanSettings
from airgnizer.duplicates import DuplicateFinder, KeepPolicy
from airgnizer.duplicates.hashing import full_hash
from airgnizer.duplicates.names import SimilarNameFinder
from airgnizer.scanner import Scanner
from airgnizer.storage import Database

SEED = 20261025
FILES_PER_DIR = 100
DUPLICATE_RATIO = 0.10
# (probabilidad, tamaño mínimo, tamaño máximo): mayoría de archivos pequeños, cola larga.
SIZE_BUCKETS = ((0.80, 1024, 16 * 1024), (0.195, 16 * 1024, 128 * 1024), (0.005, 1 << 20, 4 << 20))


def _size(rng: random.Random) -> int:
    r = rng.random()
    for p, lo, hi in SIZE_BUCKETS:
        if r < p:
            return rng.randint(lo, hi)
        r -= p
    return SIZE_BUCKETS[-1][2]


def generate(root: Path, n_files: int) -> tuple[int, int]:
    """Crea el árbol (determinista). Devuelve (archivos, bytes)."""
    marker = root / f".bench-{SEED}-{n_files}"
    if marker.exists():
        total = int(marker.read_text())
        return n_files, total
    if root.exists():
        shutil.rmtree(root)  # noqa: TID251 - árbol sintético propio del benchmark
    rng = random.Random(SEED)
    originals: list[Path] = []
    total = 0
    for i in range(n_files):
        d = root / f"d{i // 10_000:02d}" / f"s{i // 1000 % 10}" / f"c{i // FILES_PER_DIR % 10}"
        if i % FILES_PER_DIR == 0:
            d.mkdir(parents=True, exist_ok=True)
        path = d / f"archivo_{i:06d}.bin"
        if originals and rng.random() < DUPLICATE_RATIO:
            data = rng.choice(originals).read_bytes()
        else:
            data = rng.randbytes(_size(rng))
            if len(originals) < 5000:
                originals.append(path)
        path.write_bytes(data)
        total += len(data)
    marker.write_text(str(total))
    return n_files, total


def timed[T](func: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    result = func()
    return result, time.perf_counter() - start


def _cmd(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "desconocido"


def conditions(root: Path) -> list[tuple[str, str]]:
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "desconocido",
    )
    mem_kib = int(Path("/proc/meminfo").read_text().split()[1])
    source = _cmd("findmnt", "-no", "SOURCE", "--target", str(root)).split("[")[0]
    parent = _cmd("lsblk", "-no", "PKNAME", source).splitlines()
    device = f"/dev/{parent[0]}" if parent and parent[0] else source
    model = _cmd("lsblk", "-ndo", "MODEL,TRAN", device)
    rotational = _cmd("lsblk", "-ndo", "ROTA", device) == "1"
    disk = f"{' '.join(model.split())} ({'HDD' if rotational else 'SSD'})"
    return [
        ("CPU", f"{cpu} ({os.cpu_count()} hilos)"),
        ("RAM", f"{mem_kib / 1024**2:.1f} GiB"),
        ("Disco", disk),
        ("Sistema de archivos", _cmd("stat", "-f", "-c", "%T", str(root))),
        ("Kernel", platform.release()),
        ("Python", platform.python_version()),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=Path.home() / ".cache/airgnizer-bench")
    parser.add_argument("--files", type=int, default=50_000)
    parser.add_argument("--keep", action="store_true", help="no borrar el árbol al terminar")
    args = parser.parse_args()

    tree = args.dir / "arbol"
    print(f"Generando {args.files} archivos en {tree}…", file=sys.stderr)
    n_files, total_bytes = generate(tree, args.files)

    with tempfile.TemporaryDirectory(prefix="airgnizer-bench-db-") as tmp:
        db = Database(Path(tmp) / "bench.db")
        db.migrate()
        scanner = Scanner(db, ScanSettings(), mounts=())
        print("Escaneo inicial…", file=sys.stderr)
        initial, t_initial = timed(lambda: scanner.scan(tree))
        print("Escaneo incremental…", file=sys.stderr)
        incremental, t_incremental = timed(lambda: scanner.scan(tree))

        print("Duplicados exactos…", file=sys.stderr)
        finder = DuplicateFinder(db, DuplicateSettings(), KeepPolicy([], []))
        report, t_dupes = timed(lambda: finder.find([tree]))
        _, t_dupes_cached = timed(lambda: finder.find([tree]))

        print("Nombres parecidos…", file=sys.stderr)
        _, t_names = timed(lambda: SimilarNameFinder(db).find([tree]))
        db.dispose()

    big = max(tree.rglob("*.bin"), key=lambda p: p.stat().st_size)
    _, t_hash = timed(lambda: full_hash(big))
    hash_mbps = big.stat().st_size / t_hash / 1e6

    stats = report.stats
    dupes_mbps = stats.bytes_hashed / t_dupes / 1e6 if t_dupes else 0.0
    print("## Benchmark Fase 2\n")
    print(f"Fecha: {time.strftime('%Y-%m-%d')}\n")
    print("| Condición | Valor |\n|---|---|")
    for key, value in conditions(tree):
        print(f"| {key} | {value} |")
    print(
        f"| Árbol | {n_files} archivos en {initial.dirs_seen} carpetas, "
        f"{total_bytes / 1024**3:.2f} GiB, {DUPLICATE_RATIO:.0%} duplicados |"
    )
    print(
        "| Tamaños | 80 % de 1–16 KiB, 19,5 % de 16–128 KiB, 0,5 % de 1–4 MiB |\n"
        "| Caché | de páginas caliente (el árbol se acaba de escribir o leer) |\n"
    )
    print("| Medida | Resultado | Objetivo |\n|---|---|---|")
    ok = "✅" if t_initial < 120 else "❌"
    print(f"| Escaneo inicial | {t_initial:.1f} s | < 120 s {ok} |")
    ok = "✅" if t_incremental < 15 else "❌"
    print(f"| Escaneo incremental sin cambios | {t_incremental:.1f} s | < 15 s {ok} |")
    files_per_s = (stats.partial_hashed + stats.full_hashed) / t_dupes if t_dupes else 0.0
    print(
        f"| Duplicados exactos (1ª vez) | {t_dupes:.1f} s: {stats.full_hashed} archivos, "
        f"{stats.bytes_hashed / 1e6:.0f} MB → {files_per_s:.0f} archivos/s, {dupes_mbps:.0f} MB/s; "
        f"{len(report.groups)} grupos | informativo |"
    )
    print(f"| Duplicados exactos (hashes en caché) | {t_dupes_cached:.1f} s | informativo |")
    big_mb = big.stat().st_size / 1e6
    print(f"| BLAKE3 de un archivo ({big_mb:.1f} MB) | {hash_mbps:.0f} MB/s | informativo |")
    print(f"| Nombres parecidos | {t_names:.1f} s | informativo |")
    assert incremental.files_changed == 0 and incremental.files_new == 0

    if not args.keep:
        shutil.rmtree(args.dir)  # noqa: TID251 - árbol sintético propio del benchmark
    return 0


if __name__ == "__main__":
    sys.exit(main())
