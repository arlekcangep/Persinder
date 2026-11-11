"""Эмбеддинги из консоли: разовый проход и фоновый цикл сервиса."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from ..api import IndexLog
from ..config import Settings
from ..embed import EmbedStats, EmbedWorker, load
from ..store import SqliteStore
from .report import append_embed_error, format_embed, say
from .service import build_embed_worker


class EmbedRunner:
    """Воркер эмбеддингов профиля: проход по очереди и фоновый цикл."""

    def __init__(self, settings: Settings, log: IndexLog | None = None) -> None:
        self.settings = settings
        self.worker, self.store, self.label = build_embed_worker(settings, log)
        self.log_path = settings.home / "embed_errors.log"

    def close(self) -> None:
        self.store.close()

    def run_once(self, limit: int = 100) -> EmbedStats:
        """Один проход по очереди."""
        return self.worker.run_once(limit)

    def drain(self, limit: int = 100) -> EmbedStats:
        """Разобрать очередь до пустой (миграция, ``embed --background``)."""
        total = EmbedStats()
        while True:
            stats = self.worker.run_once(limit)
            total.docs += stats.docs
            total.chunks += stats.chunks
            total.sidecars += stats.sidecars
            total.skipped += stats.skipped
            total.errors += stats.errors
            total.messages.extend(stats.messages)
            if stats.docs == 0 and stats.skipped == 0 and stats.errors == 0:
                break
            time.sleep(0.2)
        return total

    def loop(
        self,
        stop: threading.Event,
        kick: threading.Event,
        interval: float = 5.0,
    ) -> None:
        """Фоновый разбор очереди, пока сервис жив (см. :func:`embed_loop`)."""
        embed_loop(self.worker, stop, kick, interval, self.log_path)


def embed_loop(
    worker: EmbedWorker,
    stop: threading.Event,
    kick: threading.Event,
    interval: float = 5.0,
    log_path: Path | None = None,
) -> None:
    """Фоновый разбор очереди эмбеддингов, пока сервис жив.

    Один тик — порция задач (``run_once``); пока очередь не опустела, тики идут
    без паузы, на пустой очереди — пауза ``interval``. ``kick`` будит цикл сразу
    после индексации (иначе новые документы ждали бы тика). Ошибка одного
    прохода не валит сервис: задачи остаются в очереди и уходят в следующий тик.
    Ошибки дублируются в ``log_path`` (``<home>/embed_errors.log``).

    Цикл защищён целиком, а не только вокруг ``run_once``: раньше исключение из
    печати отчёта (``UnicodeEncodeError`` на Windows-консоли в чужой кодировке)
    или из записи лога убивало поток молча — сервис отвечал по HTTP, а очередь
    не двигалась. Теперь поток живёт, а причина попадает в лог.
    """
    last_pending: int | None = None
    idle_passes = 0
    while not stop.is_set():
        try:
            stats = worker.run_once(100)
            did = bool(stats.docs or stats.skipped or stats.errors)
            if did:
                say(format_embed(stats))
                for message in stats.messages[:10]:
                    say(f"Embed: {message}")
                    append_embed_error(log_path, message)
            # Молчание на пустой очереди читается как «воркер встал»: говорим
            # об опустевшей очереди один раз — когда она только что была
            # непустой или только что закончили работу.
            if stats.pending == 0 and (did or last_pending):
                say("Embed: очередь пуста")
            # Очередь есть, а проход ничего не сделал — это не норма: сказать
            # об этом явно, а не молчать (иначе «очередь не движется» видно
            # только сторонним скриптом).
            if stats.pending > 0 and not did:
                idle_passes += 1
                if idle_passes % 12 == 0:  # раз в минуту при interval=5 с
                    say(
                        f"Embed: в очереди {stats.pending} задач, но проход "
                        "ничего не обработал — смотри embed_errors.log"
                    )
            else:
                idle_passes = 0
            last_pending = stats.pending
            if stats.docs == 0:
                kick.wait(interval)
                kick.clear()
        except Exception as exc:  # noqa: BLE001 — поток не должен умирать молча
            message = f"ошибка прохода: {exc.__class__.__name__}: {exc}"
            say(f"Embed: {message}")
            append_embed_error(log_path, message)
            kick.wait(interval)
            kick.clear()


def run_embed(
    home: Path | None, limit: int, all_: bool, model: str | None, background: bool
) -> int:
    """Эмбеддинги: очередь → векторы → сайдкары (§7)."""
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    settings.ensure()
    if all_:
        key = model or load(settings.models_state_path).embedding
        store = SqliteStore(settings.db_path)
        try:
            store.enqueue_all([key])
        finally:
            store.close()
        print(f"поставлен пересчёт всех источников под {key}")
    runner = EmbedRunner(settings, IndexLog())
    try:
        stats = runner.drain(limit) if background else runner.run_once(limit)
    finally:
        runner.close()
    print(runner.label)
    print(format_embed(stats))
    return 0
