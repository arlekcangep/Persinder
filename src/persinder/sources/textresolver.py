"""Текст документа: кэш → оригинал (шаг 2, вариант C; D26).

Единственное место, где решается, **откуда** взять текст документа. Два
потребителя:

* ``/context`` (``api/fastapi_server.py``) — режет текст по символьным
  смещениям (``resolve``);
* воркер эмбеддингов (``embed/service.py``) — берёт текст целиком, чтобы
  посчитать векторы, и сверяет его хэш с документом (``document_text``).

Порядок источников:

1. ``TextStore`` (кэш ``pending_texts``) — если текст ещё там, берём оттуда:
   быстро, без перечитывания.
2. Иначе — перечитываем оригинал: ``MetaStore.document_source(doc_id)`` даёт
   ``source_path``, ``extractor`` и, для части архива, ``container_path`` +
   ``container_entries``. Часть контейнера читается **адресно** по цепочке
   записей (T4) — весь архив в память не поднимается; обычный файл читается
   экстрактором по пути.
3. Ничего нет — возвращаем причину (``source_missing``/``error``) с
   пояснением, которое UI или журнал покажет пользователю.

Смещения ``start``/``end`` — **символы документа**, ``offset`` подматывает оба.
Резолвер сам режет текст и возвращает только нужный кусок: наверх (в эндпоинт)
весь документ не тащится.

Виртуальные документы (``source_path = ''``): их текст живёт только в
``TextStore``; если его там уже нет — перечитывать нечего, честный
``source_missing`` (потоковый доступ к виртуальному тексту — отдельная задача).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..interfaces import MetaStore, TextStore

if TYPE_CHECKING:
    from ..store.models import DocumentSource
    from .extract import ExtractorRegistry
    from .interfaces import ContainerExtractor, Extractor


@dataclass(frozen=True)
class ContextText:
    """Срез текста документа для одного фрагмента ответа."""

    text: str
    #: Общая длина документа в символах (для клиента: границы ответа).
    total_chars: int


@dataclass(frozen=True)
class DocumentText:
    """Текст документа целиком **или** причина, почему его нет.

    ``text is None`` — текста нет, ``code`` — машинная причина (см. константы
    ниже), ``detail`` — пояснение для журнала. ``content_hash`` — хэш текста из
    ``documents`` (воркер сверяет с ним перечитанный оригинал); ``from_cache`` —
    текст взят из кэша, а не перечитан, поэтому сверка хэша не нужна.
    """

    text: str | None
    code: str | None = None
    detail: str = ""
    content_hash: str = ""
    from_cache: bool = False


#: Статус-ошибки, которые видны снаружи (в ответе API).
SOURCE_MISSING = "source_missing"
SOURCE_ERROR = "error"

# Машинные коды причин (не текст для UI).
_NO_DOCUMENT = "no_document"
_VIRTUAL = "virtual"
_NO_REGISTRY = "no_registry"
_UNKNOWN_EXTRACTOR = "unknown_extractor"
_UNKNOWN_CONTAINER = "unknown_container"
_ENTRY_MISSING = "entry_missing"
_MISSING_FILE = "missing_file"
_READ_ERROR = "read_error"
_EMPTY_TEXT = "empty_text"

#: «Источника нет» — ``source_missing``; «перечитывать нечем» — ``error`` без
#: подробностей (это наша недонастройка, а не проблема файла).
_SOURCE_MISSING_CODES = frozenset({_NO_DOCUMENT, _VIRTUAL})
_NO_DETAIL_CODES = frozenset({_NO_REGISTRY, _UNKNOWN_EXTRACTOR})


class TextResolver:
    """Текст документа по ``doc_id``: textstore → перечитывание оригинала."""

    def __init__(
        self,
        store: MetaStore,
        textstore: TextStore,
        registry: ExtractorRegistry | None = None,
    ) -> None:
        self._store = store
        self._textstore = textstore
        self._registry = registry

    # --- публичное API ---

    def document_text(self, doc_id: str) -> DocumentText:
        """Полный текст документа: из кэша, иначе перечитанный оригинал."""
        cached = self._from_cache(doc_id)
        if cached is not None:
            return cached
        return self._read_original(doc_id)

    def resolve(
        self,
        doc_id: str,
        start: int,
        end: int,
        offset: int = 0,
    ) -> tuple[ContextText | None, str | None]:
        """Вернуть срез ``[start + offset, end + offset]`` символов.

        ``(ContextText, None)`` — успех; ``(None, status)`` — ошибка
        (``source_missing`` или ``error``).
        """
        if start < 0 or end < 0 or start > end:
            return None, "invalid range"
        document = self.document_text(doc_id)
        if document.text is None:
            return None, self._status(document)
        text = document.text
        lo = max(0, min(start + offset, len(text)))
        hi = max(0, min(end + offset, len(text)))
        if hi < lo:
            hi = lo
        return ContextText(text=text[lo:hi], total_chars=len(text)), None

    # --- кэш и оригинал ---

    def _from_cache(self, doc_id: str) -> DocumentText | None:
        """Текст из ``TextStore``; ``None`` — кэш пуст (или текст пропал)."""
        if not self._textstore.exists(doc_id):
            return None
        try:
            text = self._textstore.read(doc_id)
        except OSError:
            # Текст пропал между exists и read — идём перечитывать оригинал.
            return None
        return DocumentText(text=text, from_cache=True)

    def _read_original(self, doc_id: str) -> DocumentText:
        """Перечитать оригинал по источнику (файл или часть контейнера)."""
        source = self._store.document_source(doc_id)
        if source is None:
            return DocumentText(None, _NO_DOCUMENT, "документа нет в базе")
        content_hash = source["content_hash"]
        if not source["source_path"]:
            return DocumentText(
                None,
                _VIRTUAL,
                "виртуальный документ: оригинала на диске нет",
                content_hash,
            )
        if self._registry is None:
            return DocumentText(
                None,
                _NO_REGISTRY,
                "оригинал перечитать нечем: нет реестра экстракторов",
                content_hash,
            )
        extractor_name = source["extractor"]
        extractor = self._registry.extractor_for(extractor_name)
        if extractor is None:
            return DocumentText(
                None,
                _UNKNOWN_EXTRACTOR,
                f"экстрактор {extractor_name!r} не найден",
                content_hash,
            )
        if source["container_path"] and source["container_entries"]:
            original = self._read_container_part(source, extractor, self._registry)
        else:
            original = self._read_file(source["source_path"], extractor)
        return DocumentText(
            original.text,
            original.code,
            original.detail,
            content_hash,
        )

    def _read_container_part(
        self,
        source: DocumentSource,
        extractor: Extractor,
        registry: ExtractorRegistry,
    ) -> DocumentText:
        """Часть контейнера: адресное чтение одной записи по цепочке (T4)."""
        container_path = source["container_path"]
        container: ContainerExtractor | None = registry.container_for(container_path)
        if container is None:
            return DocumentText(
                None, _UNKNOWN_CONTAINER, f"нет контейнера для {container_path}"
            )
        try:
            part = container.extract_part(
                container_path, source["container_entries"], extractor
            )
        except (OSError, ValueError, NotImplementedError, RuntimeError) as exc:
            return DocumentText(None, _READ_ERROR, f"ошибка чтения оригинала: {exc}")
        if part is None:
            return DocumentText(
                None, _ENTRY_MISSING, f"запись не найдена в {container_path}"
            )
        if part.error or not part.text:
            return DocumentText(None, _EMPTY_TEXT, part.error or "пустой текст")
        return DocumentText(part.text)

    def _read_file(self, source_path: str, extractor: Extractor) -> DocumentText:
        """Обычный файл: экстрактор по пути источника."""
        path = Path(source_path)
        if not path.is_file():
            return DocumentText(
                None, _MISSING_FILE, f"оригинал недоступен: {source_path}"
            )
        try:
            extracted = extractor.extract(path)
        except (OSError, ValueError, NotImplementedError) as exc:
            return DocumentText(None, _READ_ERROR, f"ошибка чтения оригинала: {exc}")
        if extracted.error or not extracted.text:
            return DocumentText(None, _EMPTY_TEXT, extracted.error or "пустой текст")
        return DocumentText(extracted.text)

    @staticmethod
    def _status(document: DocumentText) -> str:
        """Машинная причина → статус для ответа API."""
        if document.code in _SOURCE_MISSING_CODES:
            return SOURCE_MISSING
        if document.code in _NO_DETAIL_CODES:
            return SOURCE_ERROR
        return f"{SOURCE_ERROR}: {document.detail}"
