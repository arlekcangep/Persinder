"""``persinder serve``: HTTP + watcher + эмбеддинги + фоновые сверки.

Оркестрация вынесена в класс :class:`ServeRunner`: старт (каталог → сверка
журнала → очередь → восстановление), фоновые потоки и остановка — отдельные
методы, а не одна длинная функция.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ..api import IndexLog, serve_fastapi
from ..api.index_log import KIND_SYSTEM
from ..config import Settings
from ..embed import (
    PLACEHOLDER,
    ModelState,
    file_sidecar_path,
    is_installed,
    load,
    virtual_sidecar_path,
)
from ..ingest import IngestService
from ..search import SearchService
from ..sources import (
    JOURNAL_RETENTION_DAYS,
    AreaConfig,
    AreaScanner,
    IndexingPipeline,
    Journal,
    PipelineStats,
    ScanResult,
    WatchConfig,
    Watcher,
    default_registry,
    load_config,
    reconcile,
    sync_catalog,
)
from ..sources.reindex import (
    format_reindex,
    reindex_files,
    share_from_percent,
)
from ..store import SqliteStore
from ..textstore import FsTextStore
from .config import known_models
from .embedding import EmbedRunner, embed_loop
from .report import append_embed_error, format_recovery, print_stats
from .service import build_service, models_for_source


class ServeRunner:
    """Единая точка запуска всех фоновых активностей сервиса.

    При ``reindex=True`` (по умолчанию в ``run.sh``):

    * очередь эмбеддингов очищается — задачи прошлого прогона не нужны;
    * журнал **не сбрасывается**: в фоне все файлы прогоняются через обычный
      конвейер, а решение «текст не менялся» принимает уже существующая
      проверка ``IngestService`` по ``documents.content_hash``. Сверка идёт
      в пределах 5% CPU (``PERSINDER_REINDEX_CPU_LIMIT``) и не мешает ни
      сервису, ни обычному скану.
    """

    def __init__(
        self,
        config_path: Path,
        settings: Settings,
        *,
        reindex: bool = False,
    ) -> None:
        self.config_path = config_path
        self.settings = settings
        self.reindex = reindex

        self.registry = default_registry()
        self.areas: tuple[AreaConfig, ...] = ()
        self.watch: WatchConfig | None = None
        self.ingest: IngestService | None = None
        self.search: SearchService | None = None
        self.store: SqliteStore | None = None
        self.log = IndexLog()
        self.journal: Journal | None = None
        self.pipeline: IndexingPipeline | None = None
        self.sweep_pipeline: IndexingPipeline | None = None
        self.scanner: AreaScanner | None = None
        self.watcher: Watcher | None = None
        self.embed: EmbedRunner | None = None
        self.embed_label = ""
        self.embed_thread: threading.Thread | None = None
        self.sweep_thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.kick = threading.Event()
        self.pipeline_lock = threading.Lock()

    # --- запуск ---

    def run(self) -> int:
        """Собрать сервис, запустить потоки и держать HTTP до Ctrl+C."""
        settings = self.settings
        settings.ensure()
        print(f"Home: {settings.home}", flush=True)

        self.areas, self.watch = load_config(
            self.config_path,
            known_formats=self.registry.known_names(),
            known_models=known_models(settings),
        )
        ingest, search, store = build_service(
            settings,
            areas=self.areas,
            semantic=self.watch.semantic,
            min_score=self.watch.min_score,
        )
        self.ingest, self.search, self.store = ingest, search, store

        if self.watch.semantic:
            self.embed = EmbedRunner(settings, self.log, registry=self.registry)
            self.embed_label = self.embed.label
        else:
            self.embed_label = (
                "Semantic: выключено конфигом (semantic: false) — "
                "эмбеддинги не считаются"
            )

        self._warn_missing_model()

        self.journal = Journal(settings.home / "journal.jsonl")
        models = load(settings.models_state_path)
        self.pipeline = IndexingPipeline(
            ingest,
            self.registry,
            journal=self.journal,
            semantic=self.watch.semantic,
            log=self.log,
        )
        # Сверка reindex молчит про неизменённые файлы: иначе каждый старт
        # писал бы по три строки журнала на каждый файл библиотеки.
        self.sweep_pipeline = IndexingPipeline(
            ingest,
            self.registry,
            journal=self.journal,
            semantic=self.watch.semantic,
            log=self.log,
            quiet_unchanged=True,
        )

        share = share_from_percent(os.environ.get("PERSINDER_REINDEX_CPU_LIMIT"))
        if self.reindex:
            self._clear_queue(share)

        # 1. Файлы — источник правды, а не журнал (D25): полные каталоги всех
        #    областей сравниваются с журналом **по именам** (хэши на этом этапе
        #    не считаются — это долго). Новые файлы индексируются, исчезнувшие
        #    убираются из индекса — в первую очередь, до очереди и сверки базы.
        self._sync_catalog()

        # 2. Сверка журнала с реальностью: файл, который журнал считает
        #    устроенным, а база/текст — нет (пропущенный fb2, потерянный текст,
        #    документ из чужого профиля), иначе не будет прочитан никогда.
        self._reconcile()

        self._compact_journal()
        self._enqueue_missing(models)
        self._recover()

        scanner = self.scanner
        self.watcher = Watcher(
            self.areas,
            self.watch,
            self.on_events,
            journal=self.journal,
            scanner=scanner,
        )
        if self.embed is not None:
            self.embed_thread = threading.Thread(
                target=embed_loop,
                args=(
                    self.embed.worker,
                    self.stop,
                    self.kick,
                    5.0,
                    self.embed.log_path,
                ),
                daemon=True,
                name="embed",
            )
        if self.reindex:
            self.sweep_thread = threading.Thread(
                target=self._reindex_worker,
                args=(share,),
                daemon=True,
                name="reindex",
            )

        self._print_startup()
        self.watcher.start()
        if self.embed_thread is not None:
            self.embed_thread.start()
        if self.sweep_thread is not None:
            self.sweep_thread.start()
        print(f"Persinder слушает http://{settings.host}:{settings.port}", flush=True)

        try:
            serve_fastapi(
                self.ingest,
                self.search,
                settings.host,
                settings.port,
                self.log,
                self._health_stats,
                store=self.store,
                textstore=FsTextStore(settings.text_dir),
                registry=self.registry,
            )
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    # --- стартовые шаги ---

    def _warn_missing_model(self) -> None:
        settings = self.settings
        models_state = load(settings.models_state_path)
        assert self.watch is not None
        if (
            self.watch.semantic
            and models_state.embedding != PLACEHOLDER
            and not is_installed(settings.models_dir, models_state.embedding)
        ):
            print(
                f"ВНИМАНИЕ: активная модель {models_state.embedding} не установлена "
                f"в {settings.models_dir} — сайдкары не будут считаться.\n"
                f"Установи её: persinder model install {models_state.embedding}",
                flush=True,
            )

    def _clear_queue(self, share: float) -> None:
        """``--reindex``: задачи прошлого прогона не нужны, начинаем с чистого листа."""
        assert self.store is not None
        cleared = self.store.clear_queue()
        print(
            "Полная переиндексация: очередь эмбеддингов очищена "
            f"({cleared} задач снято); файлы сверяются с базой в фоне "
            f"(не более {share:.0%} CPU)",
            flush=True,
        )

    def _sync_catalog(self) -> None:
        """Полные каталоги областей против журнала — по именам."""
        assert self.journal is not None and self.watch is not None
        self.scanner = AreaScanner(self.watch)
        catalog = sync_catalog(self.areas, self.scanner, self.journal)
        print(catalog.line(), flush=True)
        for result in catalog.results:
            self.on_events(result)

    def _reconcile(self) -> None:
        """Журнал против базы: потерянный текст, пропущенный формат, чужой профиль."""
        assert self.journal is not None and self.store is not None
        checked = reconcile(
            self.areas,
            self.registry,
            self.journal,
            self.store,
            FsTextStore(self.settings.text_dir),
        )
        for rel, reason in checked.retried:
            self.log.warning(
                f"сверка: {rel} — {reason}; файл будет прочитан заново",
                kind=KIND_SYSTEM,
            )
        for reason, files in checked.unsupported.items():
            shown = ", ".join(files[:3])
            tail = f" и ещё {len(files) - 3}" if len(files) > 3 else ""
            self.log.warning(
                f"не индексируется ({len(files)}): {reason} — {shown}{tail}",
                kind=KIND_SYSTEM,
            )
        if checked.empty:
            self.log.info(
                f"сверка: журнал и база согласованы ({checked.checked} файлов)",
                kind=KIND_SYSTEM,
            )

    def _compact_journal(self) -> None:
        assert self.journal is not None
        dropped = self.journal.compact()
        if dropped:
            print(
                f"Журнал: обрезано старых записей: {dropped} "
                f"(старше {JOURNAL_RETENTION_DAYS} дней)",
                flush=True,
            )

    def _enqueue_missing(self, models: ModelState) -> None:
        """Досчитать источники без сайдкара (рестарт, миграция модели)."""
        assert self.store is not None and self.watch is not None
        settings = self.settings

        def has_sidecar(model: str, doc_id: str, source_path: str) -> bool:
            if source_path:
                return file_sidecar_path(source_path, model).exists()
            return virtual_sidecar_path(settings.virtual_dir, doc_id, model).exists()

        semantic = self.watch.semantic
        texts = FsTextStore(settings.text_dir)
        added = self.store.enqueue_missing(
            list(models.queue_models()) if semantic else [],
            has_sidecar,
            models_for=lambda _area, source_path: models_for_source(
                self.areas, source_path, models, semantic=semantic
            ),
            # Без сырого текста эмбеддинг не посчитать: иначе каждый старт
            # ставит тысячи задач документу, чей текст удалён, и воркер тут же
            # снимает их как сиротские («очередь при старте» пугает, прогресса
            # нет).
            has_text=texts.exists,
        )
        if added:
            print(
                f"Очередь эмбеддингов: добавлено {added} задач (досчёт без сайдкара)",
                flush=True,
            )

    def _recover(self) -> None:
        assert self.ingest is not None
        recovery = self.ingest.recover()
        if recovery.removed:
            print(format_recovery(recovery), flush=True)

    def _print_startup(self) -> None:
        assert self.watch is not None and self.store is not None
        startup = self.store.counts()
        print(
            f"Областей: {len(self.areas)}, "
            f"интервал скана: {self.watch.poll_interval} с",
            flush=True,
        )
        print(self.embed_label, flush=True)
        print(
            f"Очередь эмбеддингов при старте: "
            f"{startup['pending_embeddings']} задач "
            f"у {startup['pending_documents']} документов",
            flush=True,
        )
        print(
            f"Ошибки эмбеддингов пишутся в {self.settings.home / 'embed_errors.log'}",
            flush=True,
        )
        # Файл лога создаётся при каждом запуске — его наличие само по себе
        # говорит, что сервис дошёл до фонового цикла. Если файла нет, запуск
        # упал раньше (конфиг, импорт, OOM) — тогда смотри вывод run.sh.
        append_embed_error(
            self.settings.home / "embed_errors.log",
            "запуск: " + self.embed_label,
        )

    # --- события ---

    def process_events(self, result: ScanResult) -> PipelineStats:
        """Прогнать события через конвейер (один поток за раз)."""
        assert self.pipeline is not None
        with self.pipeline_lock:
            return self.pipeline.process(result)

    def on_events(self, result: ScanResult) -> None:
        stats = self.process_events(result)
        if (
            stats.indexed
            or stats.deleted
            or stats.duplicates
            or stats.rejected
            or stats.skipped
            or stats.errors
        ):
            print_stats(stats)
        # Новые документы и восстановленные тексты (дубликаты по хэшу) ставят
        # задачи в очередь — будим эмбеддинги сразу, не ждём тика.
        if stats.indexed or stats.duplicates:
            self.kick.set()

    def _reindex_worker(self, share: float) -> None:
        """Фоновая переиндексация: файлы через конвейер и его проверку хэша."""
        assert (
            self.scanner is not None
            and self.sweep_pipeline is not None
            and self.journal is not None
        )
        try:
            stats = reindex_files(
                self.areas,
                self.scanner,
                self.journal,
                self.sweep_pipeline,
                share=share,
                stop=self.stop,
            )
        except Exception as exc:  # noqa: BLE001 — фон не должен ронять сервис
            print(f"Переиндексация: прервана ошибкой — {exc}", flush=True)
            self.log.warning(f"переиндексация: ошибка — {exc}", kind=KIND_SYSTEM)
            return
        print(format_reindex(stats), flush=True)
        self.log.info(format_reindex(stats), kind=KIND_SYSTEM)
        for message in stats.messages[:10]:
            self.log.warning(f"переиндексация: {message}", kind=KIND_SYSTEM)
        if stats.changed:
            self.kick.set()  # изменённые файлы в очереди — будим эмбеддинги

    # --- наблюдаемость и остановка ---

    def _health_stats(self) -> dict[str, object]:
        """Живые счётчики для ``/health`` — по ним видно, движется ли очередь.

        Смысл — наблюдение за эмбеддингами без консоли: если
        ``pending_embeddings`` не убывает между опросами, значит воркер стоит,
        и это видно в интерфейсе.
        """
        assert self.store is not None
        counts = self.store.counts()
        state = load(self.settings.models_state_path)
        if self.embed_thread is None:
            embedding = "выключено"
        else:
            embedding = "работает" if self.embed_thread.is_alive() else "остановлен"
        return {
            "documents": counts["documents"],
            "vectors": counts["vectors"],
            "model": state.search,
            "pending_embeddings": counts["pending_embeddings"],
            "embedding": embedding,
        }

    def _shutdown(self) -> None:
        print("Останавливаюсь — завершаю watcher и эмбеддинги...", flush=True)
        self.stop.set()
        self.kick.set()
        if self.watcher is not None:
            self.watcher.stop()
        if self.sweep_thread is not None:
            # Переиндексация спит между файлами и проверяет stop перед каждым:
            # обычно выходит за доли секунды. Не дождались — процесс всё равно
            # уходит (поток daemon), а ждать минутами из-за одного большого
            # файла незачем.
            self.sweep_thread.join(5.0)
        if self.embed_thread is not None:
            self.embed_thread.join(10.0)
            if not self.embed_thread.is_alive() and self.embed is not None:
                self.embed.store.close()
        if self.store is not None:
            self.store.close()
        print("Persinder остановлен.", flush=True)


def run_serve(config_path: Path, settings: Settings, reindex: bool = False) -> int:
    """Полный сервис: HTTP + watcher + эмбеддинги + восстановление после сбоев."""
    return ServeRunner(config_path, settings, reindex=reindex).run()
