"""Interfaz de línea de comandos provisional."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from organizador import __version__
from organizador.actions import ActionError, ActionService
from organizador.core.config import Settings, load_settings
from organizador.core.paths import AppPaths
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


def _cmd_scan(ctx: AppContext, args: argparse.Namespace) -> int:
    def progress(stats: ScanStats) -> None:
        print(f"\r{stats.files_seen} archivos…", end="", file=sys.stderr, flush=True)

    scanner = Scanner(ctx.db, ctx.settings.scan)
    roots: list[Path] = args.roots or ctx.settings.indexed_dirs
    if not roots:
        print("Indica una carpeta o configura indexed_dirs en config.toml", file=sys.stderr)
        return 2
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
