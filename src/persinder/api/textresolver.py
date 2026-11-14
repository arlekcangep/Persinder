"""Резолвер текста документа для ``/context`` (шаг 2, вариант C).

Отдаёт срез текста по символьным смещениям (вариант б: раз у нас есть текст
целиком — пусть клиент оперирует символами, а не байтами). Источник текста —
по порядку:

1. ``TextStore`` (кэш ``pending_texts``) — если текст ещё там, берём оттуда:
   быстро, без перечитывания.
2. Иначе — перечитываем оригинал: ``MetaStore.document_source(doc_id)`` даёт
   ``source_path`` + ``extractor`` (имя типа), экстрактор по имени из
   ``ExtractorRegistry`` распаковывает файл в текст.
3. Ничего нет — возвращаем ошибку (``source_missing``/``error``), которую
   web UI или консоль покажет пользователю.

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
    from ..sources.extract import ExtractorRegistry


@dataclass(frozen=True)
class ContextText:
    """Срез текста документа для одного фрагмента ответа."""

    text: str
    #: Общая длина документа в символах (для клиента: границы ответа).
    total_chars: int


#: Статус-ошибки: текст получить не удалось.
SOURCE_MISSING = "source_missing"
SOURCE_ERROR = "error"


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
        text, error = self._full_text(doc_id)
        if text is None:
            return None, error
        lo = max(0, min(start + offset, len(text)))
        hi = max(0, min(end + offset, len(text)))
        if hi < lo:
            hi = lo
        return ContextText(text=text[lo:hi], total_chars=len(text)), None

    def _full_text(self, doc_id: str) -> tuple[str | None, str | None]:
        """Полный текст документа (str) или ``(None, status)``."""
        if self._textstore is not None and self._textstore.exists(doc_id):
            try:
                return self._textstore.read(doc_id), None
            except OSError:
                # Текст пропал между exists и read — идём перечитывать оригинал.
                pass

        source = self._store.document_source(doc_id)
        if source is None:
            return None, SOURCE_MISSING
        if not source["source_path"]:
            # Виртуальный документ (web/вставка): текст был только в textstore.
            return None, SOURCE_MISSING
        if self._registry is None or not source["extractor"]:
            # Перечитывать нечем: экстрактор не назван или реестр не передан.
            return None, SOURCE_ERROR
        extractor = self._registry.extractor_for(source["extractor"])
        if extractor is None:
            return None, SOURCE_ERROR
        try:
            extracted = extractor.extract(Path(source["source_path"]))
        except (OSError, ValueError, NotImplementedError) as exc:
            return None, f"{SOURCE_ERROR}: {exc}"
        if extracted.error or not extracted.text:
            return None, f"{SOURCE_ERROR}: {extracted.error or 'пустой текст'}"
        return extracted.text, None
