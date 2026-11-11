"""Управление моделями эмбеддингов (D24): явные ручки, миграция при install."""

from __future__ import annotations

from ..config import Settings
from ..embed import (
    CATALOG,
    PLACEHOLDER,
    ModelState,
    delete_model_sidecars,
    install,
    installed_keys,
    is_installed,
    load,
    remove_weights,
    save,
)
from ..sources import ConfigError, default_registry, load_config
from ..store import SqliteStore
from .config import candidate_config


class ModelManager:
    """``persinder model ...``: установка, переключение и удаление моделей."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list(self) -> int:
        """Установленные модели, активные хендлы и каталог известных."""
        root = self.settings.models_dir
        models = load(self.settings.models_state_path)
        print(
            f"активные (профиль): embedding = {models.embedding}, "
            f"search = {models.search}"
        )
        print("установлено:")
        installed = installed_keys(root)
        for key in installed:
            mark = "  * активна" if key in (models.embedding, models.search) else ""
            print(f"  {key}{mark}")
        if not installed:
            print("  (нет — работает встроенная заглушка placeholder-v1)")
        print("каталог (доступно для install):")
        for key in sorted(CATALOG):
            print(f"  {key} — {CATALOG[key].title}")
        return 0

    def install(self, key: str) -> int:
        """Скачать модель, переключить embedding и поставить миграцию в очередь.

        Поиск при этом не трогается — он остаётся на старой модели, пока по новой
        не пересчитаны все существующие файлы (разбираются обычными ``embed``).
        """
        if key == PLACEHOLDER:
            print("заглушка placeholder-v1 встроена, устанавливать её не нужно.")
            return 1
        root = self.settings.models_dir
        root.mkdir(parents=True, exist_ok=True)
        if not is_installed(root, key):
            print(f"качаю {key} -> {root / key} ...")
            install(root, key)
            print("скачано.")
        else:
            print(f"{key} уже установлена.")

        models = load(self.settings.models_state_path)
        if models.embedding == key:
            print(f"embedding уже {key}.")
        else:
            save(
                ModelState(embedding=key, search=models.search),
                self.settings.models_state_path,
            )
            store = SqliteStore(self.settings.db_path)
            try:
                store.enqueue_all([key])
            finally:
                store.close()
            print(
                f"embedding переключён на {key} — пересчёт существующих файлов "
                "в очереди."
            )
        print(
            "поиск остаётся на старой модели. Когда очередь разобрана "
            "(persinder embed --background), переключи поиск: "
            "persinder model use ..."
        )
        return 0

    def use(self, key: str) -> int:
        """Переключить поиск (и embedding) на модель — она должна быть установлена.

        Именно здесь поиск переходит на новую модель: к этому моменту по ней уже
        должны быть пересчитаны существующие файлы (миграция стартовала при
        install).
        """
        root = self.settings.models_dir
        if key != PLACEHOLDER and not is_installed(root, key):
            print(
                f"модель не установлена: {key} (сначала persinder model install {key})."
            )
            return 1
        models = load(self.settings.models_state_path)
        if models.embedding == key and models.search == key:
            print(f"{key} уже активна (embedding и search).")
            return 0
        save(ModelState(embedding=key, search=key), self.settings.models_state_path)
        print(f"модель {key} активна: embedding = search = {key}.")
        return 0

    def remove(self, key: str) -> int:
        """Удалить модель вместе с её сайдкарами (не раньше, D24).

        Пока модель активна (embedding или search) — отказ: другие устройства
        могут ещё работать со старой моделью, решать, когда её убрать, должен
        юзер.
        """
        if key == PLACEHOLDER:
            print("встроенную заглушку placeholder-v1 удалить нельзя.")
            return 1
        models = load(self.settings.models_state_path)
        if key in (models.embedding, models.search):
            print(f"модель {key} активна — сначала переключи: persinder model use ...")
            return 1
        # Модель может быть активной не только в models.json, но и для папки в
        # конфиге: убирать её тогда нельзя, иначе конфиг станет невалидным.
        config_path = candidate_config(self.settings)
        if config_path is not None:
            try:
                areas, _watch = load_config(
                    config_path, known_formats=default_registry().known_names()
                )
            except ConfigError:
                areas = ()
            if any(area.embedding_model == key for area in areas):
                print(
                    f"модель {key} выбрана для папки в {config_path} — "
                    "сначала убери embedding_model из конфига."
                )
                return 1
        root = self.settings.models_dir
        if not is_installed(root, key):
            print(f"модель не установлена: {key}.")
            return 1
        store = SqliteStore(self.settings.db_path)
        try:
            sources = store.all_sources()
            store.drop_queue_model(key)
            store.drop_embeddings_model(key)
        finally:
            store.close()
        ann_path = self.settings.ann_dir / f"{key}.usearch"
        if ann_path.exists():
            ann_path.unlink()
        delete_model_sidecars(self.settings.virtual_dir, key, sources)
        remove_weights(root, key)
        print(f"удалена {key}: веса, сайдкары, векторы и ANN-индекс.")
        return 0


def run_model_command(settings: Settings, command: str | None, key: str | None) -> int:
    """Разобрать подкоманду ``model`` и выполнить её."""
    manager = ModelManager(settings)
    if command == "list":
        return manager.list()
    if command == "install":
        assert key is not None
        return manager.install(key)
    if command == "use":
        assert key is not None
        return manager.use(key)
    if command == "remove":
        assert key is not None
        return manager.remove(key)
    return 1


def run_model_list(settings: Settings) -> int:
    """Установленные модели, активные хендлы и каталог известных."""
    return ModelManager(settings).list()


def run_model_install(settings: Settings, key: str) -> int:
    """Скачать модель, переключить embedding и поставить миграцию в очередь."""
    return ModelManager(settings).install(key)


def run_model_use(settings: Settings, key: str) -> int:
    """Переключить поиск (и embedding) на модель."""
    return ModelManager(settings).use(key)


def run_model_remove(settings: Settings, key: str) -> int:
    """Удалить модель вместе с её сайдкарами."""
    return ModelManager(settings).remove(key)
