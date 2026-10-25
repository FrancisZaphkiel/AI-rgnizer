"""Interfaz de línea de comandos provisional."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from organizador import __version__
from organizador.actions import ActionError, ActionService
from organizador.core.config import Settings, load_settings
from organizador.core.paths import AppPaths
from organizador.duplicates import DuplicateFinder, DuplicateGroup, KeepPolicy, trash_redundant
from organizador.scanner import Scanner, ScanStats
from organizador.storage import Database

log = logging.getLogger("organizador")


@dataclass(slots=True)
class AppContext:
    paths: AppPaths
    settings: Settings
    db: Database

    @classmethod
    def open(cls) -> AppContext:
        paths = AppPaths.from_env()
        paths.ensure()
        db = Database(paths.database_file)
        db.migrate()
        return cls(paths, load_settings(paths), db)

    def actions(self) -> ActionService:
        service = ActionService(
            self.db, self.settings.actions, self.paths.state_dir / "actions.lock", origin="cli"
        )
        for outcome in service.recover():
            print(f"recuperada {outcome.action_id[:8]}: {outcome.state} — {outcome.detail}")
        return service


def _cmd_paths(_ctx: AppContext, _args: argparse.Namespace) -> int:
    paths = AppPaths.from_env()
    for name in ("config_dir", "data_dir", "state_dir", "cache_dir"):
        print(f"{name:<11} {getattr(paths, name)}")
    return 0


def _resolve_roots(ctx: AppContext, args: argparse.Namespace) -> list[Path] | None:
    """Carpetas indicadas (o ``indexed_dirs``); ``None`` si falta alguna, tras avisar de todas."""
    roots: list[Path] = [r.expanduser() for r in args.roots] or ctx.settings.indexed_dirs
    if not roots:
        print("Indica una carpeta o configura indexed_dirs en config.toml", file=sys.stderr)
        return None
    missing = [r for r in roots if not r.is_dir()]
    for root in missing:
        print(f"error: no es una carpeta: {root}", file=sys.stderr)
    if missing:
        print("Si la ruta tiene espacios, ponla entre comillas.", file=sys.stderr)
        return None
    return roots


def _cmd_scan(ctx: AppContext, args: argparse.Namespace) -> int:
    def progress(stats: ScanStats) -> None:
        print(f"\r{stats.files_seen} archivos…", end="", file=sys.stderr, flush=True)

    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    scanner = Scanner(ctx.db, ctx.settings.scan)
    for root in roots:
        try:
            stats = scanner.scan(root, progress=progress)
        except KeyboardInterrupt:
            print("\nescaneo cancelado", file=sys.stderr)
            return 130
        print(
            f"\r{stats.root}: {stats.files_seen} archivos "
            f"({stats.files_new} nuevos, {stats.files_changed} modificados, "
            f"{stats.files_removed} eliminados del índice), {stats.errors} errores"
        )
    return 0


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("inalcanzable")  # pragma: no cover


def _print_group(index: int, group: DuplicateGroup) -> None:
    print(
        f"\nGrupo {index} · {len(group.files)} archivos de {human_size(group.size)} · "
        f"recuperable {human_size(group.recoverable_bytes)}"
    )
    hardlinks = set(group.hardlinks_of_keep)
    for n, f in enumerate(group.files, start=1):
        if f is group.keep:
            label, note = "conservar", f"  ({group.keep_reason})"
        elif f in hardlinks:
            label, note = "enlace", "  (hardlink de la copia conservada: no ocupa espacio)"
        else:
            label, note = "sobra", ""
        print(f"  [{n}] {label:<9} {f.path}{note}")


def _ask_group(group: DuplicateGroup) -> DuplicateGroup | None:
    """Confirmación por grupo: s = enviar sobrantes, n = saltar, número = conservar otra."""
    while True:
        try:
            answer = input("  ¿Enviar las copias sobrantes a la papelera? [s/N/nº a conservar] ")
        except EOFError:
            return None
        answer = answer.strip().lower()
        if answer in {"s", "si", "sí", "y"}:
            return group
        if answer in {"", "n", "no"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(group.files):
            group = group.with_keep(group.files[int(answer) - 1])
            print(f"  conservar: {group.keep.path}")
            continue
        print("  Respuesta no válida.")


def _trash_groups(
    service: ActionService, groups: Sequence[DuplicateGroup], *, assume_yes: bool
) -> int:
    failures = trashed = 0
    for index, proposed in enumerate(groups, start=1):
        group: DuplicateGroup | None = proposed
        if not assume_yes:
            _print_group(index, proposed)
            group = _ask_group(proposed)
        if group is None:
            continue
        try:
            result = trash_redundant(group, service)
        except ActionError as exc:
            print(f"  grupo {index} omitido: {exc}", file=sys.stderr)
            failures += 1
            continue
        for action in result.trashed:
            print(f"  {action.action_id[:8]}  {action.source} → papelera")
        for dup, error in result.failed:
            print(f"  error con {dup.path}: {error}", file=sys.stderr)
        failures += len(result.failed)
        trashed += len(result.trashed)
    if trashed:
        print(f"{trashed} archivos enviados a la papelera. Para restaurar uno: organizador undo ID")
    return 1 if failures else 0


def _cmd_dupes(ctx: AppContext, args: argparse.Namespace) -> int:
    roots = _resolve_roots(ctx, args)
    if roots is None:
        return 2
    if not args.no_scan and _cmd_scan(ctx, argparse.Namespace(roots=roots)) != 0:
        return 1

    settings = ctx.settings
    duplicates = settings.duplicates
    if args.min_size is not None:
        duplicates = replace(duplicates, min_size=args.min_size)
    policy = KeepPolicy(settings.indexed_dirs, settings.watched_dirs)
    try:
        report = DuplicateFinder(ctx.db, duplicates, policy).find(roots)
    except KeyboardInterrupt:
        print("\nbúsqueda cancelada", file=sys.stderr)
        return 130

    interactive = args.trash and not args.yes
    if not interactive:  # en modo interactivo cada grupo se muestra al preguntar
        for index, group in enumerate(report.groups, start=1):
            _print_group(index, group)
    stats = report.stats
    print(
        f"\n{len(report.groups)} grupos de duplicados exactos; "
        f"recuperable {human_size(report.recoverable_bytes)}. "
        f"Leídos {human_size(stats.bytes_hashed)} ({stats.full_hashed} hashes completos)."
    )
    if stats.stale:
        print(f"{stats.stale} archivos cambiaron desde el escaneo y se omitieron.")
    if not args.trash:
        return 0

    return _trash_groups(ctx.actions(), report.groups, assume_yes=args.yes)


def _cmd_mv(ctx: AppContext, args: argparse.Namespace) -> int:
    result = ctx.actions().move(args.source, args.dest_dir)
    print(f"{result.action_id[:8]}  {result.source} → {result.destination}")
    return 0


def _cmd_rename(ctx: AppContext, args: argparse.Namespace) -> int:
    result = ctx.actions().rename(args.source, args.new_name)
    print(f"{result.action_id[:8]}  {result.source} → {result.destination}")
    return 0


def _cmd_trash(ctx: AppContext, args: argparse.Namespace) -> int:
    service = ctx.actions()
    for source in args.sources:
        result = service.trash(source)
        print(f"{result.action_id[:8]}  {result.source} → papelera")
    return 0


def _cmd_undo(ctx: AppContext, args: argparse.Namespace) -> int:
    result = ctx.actions().undo(args.action_id)
    print(f"{result.action_id[:8]}  restaurado {result.destination}")
    return 0


def _cmd_history(ctx: AppContext, args: argparse.Namespace) -> int:
    for entry in ctx.actions().history(args.limit):
        when = entry.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
        target = entry.destination or "-"
        print(
            f"{entry.id[:8]}  {when}  {entry.kind:<6} {entry.state:<11} {entry.source} → {target}"
        )
    return 0


def _cmd_recover(ctx: AppContext, _args: argparse.Namespace) -> int:
    ctx.actions()  # la recuperación se ejecuta al crear el servicio
    return 0


Command = Callable[[AppContext, argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="organizador", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="registro detallado")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Command, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    add("paths", _cmd_paths, "muestra las rutas XDG de la aplicación")

    p = add("scan", _cmd_scan, "indexa carpetas (solo lectura)")
    p.add_argument("roots", nargs="*", type=Path)

    p = add(
        "dupes", _cmd_dupes, "busca duplicados exactos y, opcionalmente, los envía a la papelera"
    )
    p.add_argument("roots", nargs="*", type=Path)
    p.add_argument("--no-scan", action="store_true", help="usa el índice sin reescanear")
    p.add_argument("--min-size", type=int, metavar="BYTES", help="ignora archivos más pequeños")
    p.add_argument(
        "--trash", action="store_true", help="ofrece enviar las copias sobrantes a la papelera"
    )
    p.add_argument("--yes", action="store_true", help="con --trash, no pide confirmación")

    p = add("mv", _cmd_mv, "mueve un archivo a una carpeta (sin sobrescribir)")
    p.add_argument("source", type=Path)
    p.add_argument("dest_dir", type=Path)

    p = add("rename", _cmd_rename, "renombra un archivo")
    p.add_argument("source", type=Path)
    p.add_argument("new_name")

    p = add("trash", _cmd_trash, "envía archivos a la papelera del sistema")
    p.add_argument("sources", nargs="+", type=Path)

    p = add("undo", _cmd_undo, "deshace una acción (id o prefijo)")
    p.add_argument("action_id")

    p = add("history", _cmd_history, "historial de acciones")
    p.add_argument("-n", "--limit", type=int, default=30)

    add("recover", _cmd_recover, "completa o revierte acciones interrumpidas")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx = AppContext.open()
    try:
        result: int = args.func(ctx, args)
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        ctx.db.dispose()
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
