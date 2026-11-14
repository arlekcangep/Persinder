"""Разбор командной строки и диспетчеризация к классам-исполнителям."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from ..api import IndexLog, serve_fastapi
from ..config import Settings
from ..sources import default_registry, load_config
from ..textstore import FsTextStore
from .ann import run_ann_rebuild
from .config import candidate_config, config_candidates
from .embedding import run_embed
from .models import run_model_command
from .report import format_recovery
from .search import run_search
from .serve import run_serve
from .service import build_service
from .watch import run_watch


class Cli:
    """``python -m persinder``: парсер аргументов и выбор команды."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self.argv = argv
        self.default_home = Settings.default().home

    # --- парсер ---

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="persinder", description="Персональный индексатор Persinder"
        )
        sub = parser.add_subparsers(dest="command")
        home_help = f"каталог приложения (по умолчанию {self.default_home})"

        serve_p = sub.add_parser("serve", help="HTTP-сервис + watcher + эмбеддинги")
        serve_p.add_argument(
            "--config",
            type=Path,
            default=None,
            help="YAML-конфиг областей (по умолчанию <home>/persinder.yaml|yml)",
        )
        serve_p.add_argument("--home", type=Path, default=None, help=home_help)
        serve_p.add_argument("--host", default=None)
        serve_p.add_argument("--port", type=int, default=None)
        serve_p.add_argument(
            "--reindex",
            action="store_true",
            help="полная переиндексация: очистить очередь эмбеддингов и в фоне "
            "(не более 5%% CPU) прогнать все файлы через конвейер — неизменённые "
            "по хэшу не переиндексируются (по умолчанию включено в run.sh)",
        )

        watch_p = sub.add_parser("watch", help="следить за областями и индексировать")
        watch_p.add_argument("--config", type=Path, required=True, help="YAML-конфиг")
        watch_p.add_argument("--home", type=Path, default=None, help=home_help)

        embed_p = sub.add_parser(
            "embed", help="эмбеддинги: очередь → векторы → сайдкары"
        )
        embed_p.add_argument("--home", type=Path, default=None, help=home_help)
        embed_p.add_argument("--limit", type=int, default=100, help="задач за проход")
        embed_p.add_argument(
            "--all",
            action="store_true",
            help="поставить пересчёт всех источников под моделью, затем проход",
        )
        embed_p.add_argument("--model", default=None, help="модель для --all")
        embed_p.add_argument(
            "--background",
            action="store_true",
            help="разбирать очередь до пустой (миграция)",
        )

        model_p = sub.add_parser("model", help="управление моделями эмбеддингов (D24)")
        model_common = argparse.ArgumentParser(add_help=False)
        model_common.add_argument("--home", type=Path, default=None, help=home_help)
        model_sub = model_p.add_subparsers(dest="model_command")

        model_sub.add_parser(
            "list", parents=[model_common], help="установленные + активные + каталог"
        )
        install_p = model_sub.add_parser(
            "install", parents=[model_common], help="скачать модель и начать миграцию"
        )
        install_p.add_argument("key")
        use_p = model_sub.add_parser(
            "use", parents=[model_common], help="переключить поиск/embedding на модель"
        )
        use_p.add_argument("key")
        remove_p = model_sub.add_parser(
            "remove", parents=[model_common], help="удалить модель и её сайдкары"
        )
        remove_p.add_argument("key")

        search_p = sub.add_parser(
            "search", help="поиск из консоли (морфология + семантика)"
        )
        search_p.add_argument("--home", type=Path, default=None, help=home_help)
        search_p.add_argument(
            "--config",
            type=Path,
            default=None,
            help="YAML-конфиг областей для локального поиска (включает --local; "
            "по умолчанию ищется в cwd и <home>)",
        )
        search_p.add_argument(
            "--host", default=None, help="хост сервера (по умолчанию 127.0.0.1)"
        )
        search_p.add_argument(
            "--port", type=int, default=None, help="порт сервера (по умолчанию 8756)"
        )
        search_p.add_argument(
            "--local",
            action="store_true",
            help="не обращаться к серверу, искать в БД профиля",
        )
        search_p.add_argument(
            "--min-score",
            type=float,
            default=None,
            help="порог косинуса семантического слоя (например 0.85); "
            "абсолютное значение зависит от модели",
        )
        search_p.add_argument(
            "--morph-limit",
            type=int,
            default=None,
            help="лимит морфологического слоя (0 — выключить; по умолчанию --limit)",
        )
        search_p.add_argument(
            "--semantic-limit",
            type=int,
            default=None,
            help="лимит семантического слоя (0 — выключить; по умолчанию --limit)",
        )
        search_p.add_argument("--limit", type=int, default=20)
        search_p.add_argument("text")

        ann_p = sub.add_parser(
            "ann", help="ANN-индекс (USearch): пересборка из таблицы"
        )
        ann_p.add_argument("--home", type=Path, default=None, help=home_help)
        ann_p.add_argument(
            "--model", default=None, help="только эта модель (по умолчанию все)"
        )
        ann_p.add_argument(
            "subcommand",
            nargs="?",
            default="rebuild",
            choices=["rebuild"],
            help="операция (сейчас только rebuild)",
        )

        # Совместимость: без подкоманды — serve с флагами. ``dest`` отдельный:
        # значение, переданное до подкоманды, не должно затираться её дефолтом.
        parser.add_argument(
            "--home", dest="home_top", type=Path, default=None, help=argparse.SUPPRESS
        )
        parser.add_argument(
            "--host", dest="host_top", default=None, help=argparse.SUPPRESS
        )
        parser.add_argument(
            "--port", dest="port_top", type=int, default=None, help=argparse.SUPPRESS
        )
        return parser

    def main(self) -> int:
        parser = self.build_parser()
        args = parser.parse_args(self.argv)
        # ``--home`` понимается и до подкоманды, и после: у подкоманд свой флаг.
        # Без этого ``persinder --home X search`` молча искал бы в ~/.persinder.
        args.home = getattr(args, "home", None) or args.home_top or self.default_home
        if getattr(args, "host", None) is None:
            args.host = getattr(args, "host_top", None)
        if getattr(args, "port", None) is None:
            args.port = getattr(args, "port_top", None)

        if args.command == "watch":
            return run_watch(args.config, args.home)
        if args.command == "embed":
            return run_embed(
                args.home, args.limit, args.all, args.model, args.background
            )
        if args.command == "search":
            return run_search(
                args.home,
                args.text,
                args.limit,
                args.config,
                host=args.host,
                port=args.port,
                local=args.local,
                min_score=args.min_score,
                morph_limit=args.morph_limit,
                semantic_limit=args.semantic_limit,
            )
        if args.command == "ann":
            return run_ann_rebuild(args.home, args.model)
        if args.command == "model":
            return self._model_main(parser, args)
        if args.command == "serve" or args.command is None:
            return self._serve_main(args)
        parser.print_help()
        return 1

    # --- команды ---

    @staticmethod
    def _model_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
        settings = Settings.default()
        if getattr(args, "home", None):
            settings = replace(settings, home=args.home)
        if args.model_command is None:
            parser.parse_args([args.command, "--help"])
            return 1
        return run_model_command(
            settings, args.model_command, getattr(args, "key", None)
        )

    def _serve_main(self, args: argparse.Namespace) -> int:
        settings = Settings.default()
        if getattr(args, "home", None):
            settings = replace(settings, home=args.home)
        if getattr(args, "host", None):
            settings = replace(settings, host=args.host)
        if getattr(args, "port", None):
            settings = replace(settings, port=args.port)
        # Home и подкаталоги существуют всегда — даже если конфига нет (HTTP-only).
        settings = settings.ensure()

        config_path: Path | None = getattr(args, "config", None)
        if config_path is None:
            found = candidate_config(settings)
            if found is not None:
                config_path = found
                print(f"Конфиг не задан — использую {config_path}", flush=True)
        if config_path is not None:
            return run_serve(config_path, settings, getattr(args, "reindex", False))

        # Конфига нет ни явного, ни в папке запуска/home — деградация до
        # HTTP-only: watcher/эмбеддинги не знают областей.
        print(
            "ВНИМАНИЕ: конфиг не найден — работает только HTTP (приём + поиск), "
            "без watcher и эмбеддингов.\n"
            f"Искал: persinder.{{yaml,yml}} в текущей папке и в {settings.home}\n"
            "Полный сервис: persinder serve --config persinder.yaml",
            flush=True,
        )
        ingest, search, store, recovery = build_service(settings)
        if recovery.removed:
            print(format_recovery(recovery))
        print(f"Persinder слушает http://{settings.host}:{settings.port}", flush=True)
        try:
            serve_fastapi(
                ingest,
                search,
                settings.host,
                settings.port,
                IndexLog(),
                store=store,
                textstore=FsTextStore(settings.text_dir),
                registry=default_registry(),
            )
        except KeyboardInterrupt:
            pass
        finally:
            print("Останавливаюсь (Ctrl+C)...", flush=True)
            store.close()
            print("Persinder остановлен.", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: ``main(["serve", "--config", ...])``."""
    return Cli(argv).main()


__all__ = ["Cli", "main", "config_candidates", "load_config", "default_registry"]
