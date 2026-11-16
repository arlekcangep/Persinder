"""Архивы (zip/7z/tar/gz/rar) — контейнер, класс ``archive``.

Каждая запись — отдельная часть; экстрактор для записи выбирает резолвер
(``_AreaEntryResolver``) **по содержимому записи** (магия/снифф первых
байтов), расширение — лишь хинт. Записи-архивы раскрываются **рекурсивно** с
лимитом глубины ``ARCHIVE_MAX_DEPTH`` (``folder/a.zip/b.zip/c.pdf``): у части
явно сохраняется ``container_path`` и цепочка ``container_entries``, а не только
человекочитаемое имя. ``tar.gz``/``tgz`` — один tar-уровень, одиночный ``.gz``
— один «файл» без суффикса.

Память: архив **не** читается в RAM целиком. Записи берутся по одной
(``ZipFile.open``/``TarFile.extractfile``/``RarFile.open``/поток gzip; ``7z``
распаковывается в temp-каталог) и сразу пишутся во временный файл; экстрактор
работает с путём. Пик памяти не зависит от размера архива. Единственный лимит
— размер файла архива на диске ``ARCHIVE_MAX_BYTES`` (2 ГБ): больший архив
**пропускается целиком** (``Extracted.skipped``) с причиной для журнала.
Битая запись не валит архив (часть с ``error``), битый архив — одна часть с
ошибкой. ``7z`` требует extra ``7z`` (py7zr), ``rar`` — extra ``rar``
(rarfile + внешний распаковщик).
"""

from __future__ import annotations

import dataclasses
import gzip
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from ..classify import ARCHIVE, SNIFF_BYTES
from ._base import BaseExtractor, Extracted
from ._common import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_DEPTH,
    ARCHIVE_MAX_TEMP_BYTES,
    archive_suffix,
)

if TYPE_CHECKING:
    from ..interfaces import EntryResolver, Extractor

#: Запись контейнера в потоковом виде: имя и открытый поток (закрывается
#: читателем после ``yield``).
EntryStream = tuple[str, BinaryIO]

#: Размер буфера копирования «поток записи → temp-файл». Память при обходе
#: архива не зависит ни от размера записи, ни от их числа.
_COPY_BUFFER = 1024 * 1024


class _TempLimitError(RuntimeError):
    """Обход архива упёрся в лимит выгрузки в temp (архив-бомба/гигант)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"превышен лимит temp {_gb(limit)} — обход архива прерван")


class _TempBudget:
    """Сколько байт уже выгружено в temp при обходе одного архива."""

    __slots__ = ("limit", "used")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise _TempLimitError(self.limit)


def _gb(size: int) -> str:
    """Размер для сообщений: 2 ГБ, 20 ГБ (дробные — с одним знаком)."""
    value = size / (1024 * 1024 * 1024)
    return f"{value:.0f} ГБ" if value >= 1 else f"{value:.1f} ГБ"


def _copy_into(src: BinaryIO, dest: BinaryIO, budget: _TempBudget) -> None:
    """Скопировать поток записи в temp-файл, считая байты по бюджету."""
    while True:
        chunk = src.read(_COPY_BUFFER)
        if not chunk:
            return
        budget.add(len(chunk))
        dest.write(chunk)


def _live_limit(name: str, default: int) -> int:
    """Текущий лимит из публичного модуля ``extract`` (тесты его патчат).

    Архив читает лимиты в рантайме из ``persinder.sources.extract``, а не
    связывает при импорте: иначе ``mock.patch.object(extract, ...)`` в тестах
    не влиял бы на поведение.
    """
    mod: Any = sys.modules.get("persinder.sources.extract")
    if mod is None or not hasattr(mod, name):
        return default
    return getattr(mod, name)


def _gz_stem(name: str) -> str:
    """Имя записи одиночного gz: ``notes.txt.gz`` → ``notes.txt``."""
    lower = name.lower()
    return name[: -len(".gz")] if lower.endswith(".gz") else "data"


# --- потоковые читатели форматов: путь → записи по одной ---


def _iter_zip(path: Path) -> Iterator[EntryStream]:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as handle:
                yield info.filename, cast(BinaryIO, handle)


def _iter_tar(path: Path) -> Iterator[EntryStream]:
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            with handle:
                yield member.name, cast(BinaryIO, handle)


def _iter_rar(path: Path) -> Iterator[EntryStream]:
    try:
        import rarfile  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:  # extra 'rar' не установлен
        raise RuntimeError(
            "формат rar требует extra 'rar': pip install 'persinder[archiverar]'"
        ) from exc
    with rarfile.RarFile(path) as archive:
        infos = cast(Any, archive.infolist())
        for info in infos:
            if info.is_dir():
                continue
            opened = cast(Any, archive.open(info))  # pyright: ignore[reportUnknownMemberType]
            try:
                yield info.filename, cast(BinaryIO, opened)
            finally:
                opened.close()  # pyright: ignore[reportUnknownMemberType]


def _iter_7z(path: Path) -> Iterator[EntryStream]:
    """7z не умеет потоково: распаковываем в temp (диск) и читаем по записи.

    ``py7zr`` требует каталог назначения, поэтому весь архив материализуется
    на диск (не в RAM); по записям идём уже по файлам и удаляем каталог в
    конце.
    """
    try:
        import py7zr  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as exc:  # extra '7z' не установлен
        raise RuntimeError(
            "формат 7z требует extra '7z': pip install 'persinder[archive7z]'"
        ) from exc
    with tempfile.TemporaryDirectory() as tmp:
        with py7zr.SevenZipFile(path, "r") as archive:
            names = list(archive.getnames())
            archive.extractall(path=tmp)
        for name in names:
            file = Path(tmp) / name
            if not file.is_file():
                continue
            with file.open("rb") as handle:
                yield name, handle


def _iter_gz(path: Path) -> Iterator[EntryStream]:
    with gzip.open(path, "rb") as handle:
        yield _gz_stem(path.name), cast(BinaryIO, handle)


#: Формат → потоковый читатель. ``None`` (нет в таблице) — формат не читаем.
_ENTRY_READERS: dict[str, Callable[[Path], Iterator[EntryStream]]] = {
    ".zip": _iter_zip,
    ".tar": _iter_tar,
    ".tar.gz": _iter_tar,
    ".tgz": _iter_tar,
    ".tar.bz2": _iter_tar,
    ".tbz2": _iter_tar,
    ".tar.xz": _iter_tar,
    ".txz": _iter_tar,
    ".7z": _iter_7z,
    ".rar": _iter_rar,
    ".gz": _iter_gz,
}


class ArchiveExtractor(BaseExtractor):
    """Архивы (zip/7z/tar/gz/rar) — контейнер, класс ``archive``."""

    name = "archive"
    format_class = ARCHIVE
    subtypes = frozenset({"zip", "7z", "tar", "gzip", "rar"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".zip", ".7z", ".tar", ".gz", ".tgz", ".rar"})

    # --- адресное чтение части (T4): только нужная запись ---

    def read_entry_chain(
        self, container_path: str | Path, entries: Sequence[str]
    ) -> tuple[str, bytes] | None:
        """Пройти цепочку записей контейнера и вернуть ``(имя_листа, байты)``.

        Каждый уровень пишется в temp-файл и следующий читается уже из него,
        поэтому в памяти не оказывается весь архив. ``None`` — цепочка пуста
        или какой-то записи нет.
        """
        if not entries:
            return None
        with self._chain_temp(Path(container_path), entries) as leaf:
            if leaf is None:
                return None
            return entries[-1], leaf.read_bytes()

    def extract_part(
        self,
        container_path: str,
        entries: Sequence[str],
        extractor: Extractor,
    ) -> Extracted | None:
        """Извлечь часть контейнера по цепочке записей и прогнать листовой экстрактор.

        Адресное чтение (T4): архив целиком не раскрывается — из него
        достаётся только нужная запись (в temp-файл). ``None`` — записи нет
        или прочитать нельзя.
        """
        if not entries:
            return None
        with self._chain_temp(Path(container_path), entries) as leaf:
            if leaf is None:
                return None
            return self._part_from_path(extractor, entries[-1], leaf)

    # --- раскрытие архива при индексации ---

    def extract_all(self, path: Path, resolver: EntryResolver) -> list[Extracted]:
        container_path = str(path)
        suffix = archive_suffix(path.name)
        if _ENTRY_READERS.get(suffix) is None:
            return [
                Extracted(
                    text="",
                    error=f"archive: формат {suffix} не читается",
                    container_path=container_path,
                )
            ]
        try:
            size = path.stat().st_size
        except OSError as exc:
            return [
                Extracted(
                    text="", error=f"archive: {exc}", container_path=container_path
                )
            ]
        limit = _live_limit("ARCHIVE_MAX_BYTES", ARCHIVE_MAX_BYTES)
        if size > limit:
            # Ничего не пропускаем по размеру записи, но слишком большой архив
            # целиком пропускаем: это разовая защита от гигантских файлов.
            return [
                Extracted(
                    text="",
                    skipped=True,
                    error=f"архив больше {_gb(limit)} — пропущен",
                    container_path=container_path,
                )
            ]
        try:
            parts: list[Extracted] = []
            budget = _TempBudget(
                _live_limit("ARCHIVE_MAX_TEMP_BYTES", ARCHIVE_MAX_TEMP_BYTES)
            )
            try:
                parts.extend(
                    self._expand_entries(
                        path,
                        suffix,
                        resolver,
                        container_path=container_path,
                        budget=budget,
                    )
                )
            except _TempLimitError as exc:
                # Уже собранные части сохраняем, хвост архива не обходим:
                # причина видна в журнале как пропуск.
                parts.append(
                    Extracted(
                        text="",
                        skipped=True,
                        error=str(exc),
                        container_path=container_path,
                    )
                )
        except RuntimeError as exc:  # нет extra для 7z/rar
            return [Extracted(text="", error=str(exc), container_path=container_path)]
        except Exception as exc:  # noqa: BLE001 — битый/нечитаемый архив
            return [
                Extracted(
                    text="", error=f"archive: {exc}", container_path=container_path
                )
            ]
        return parts

    def _expand_entries(
        self,
        path: Path,
        suffix: str,
        resolver: EntryResolver,
        *,
        container_path: str,
        budget: _TempBudget,
        prefix: tuple[str, ...] = (),
        depth: int = 0,
    ) -> Iterator[Extracted]:
        """Записи архива → части; вложенные архивы раскрываются рекурсивно.

        Каждая запись сразу пишется в temp-файл (память не зависит от её
        размера), классифицируется по первым ``SNIFF_BYTES`` байтам и
        отдаётся экстрактору путём. ``prefix`` — цепочка записей от контейнера
        до текущего архива, ``depth`` — сколько архивов уже раскрыто. Часть
        получает ``container_path`` + ``container_entries = prefix + (name,)`` —
        однозначный обратный путь, не зависящий от разбора ``source_path``.
        """
        reader = _ENTRY_READERS.get(suffix)
        if reader is None:
            return
        for name, handle in reader(path):
            chain = (*prefix, name)
            entry_name = "/".join(chain)
            with tempfile.TemporaryDirectory() as tmp:
                entry_path = _entry_file(tmp, name)
                with entry_path.open("wb") as out:
                    _copy_into(handle, out, budget)
                nested_suffix = archive_suffix(name)
                if _ENTRY_READERS.get(nested_suffix) is not None:
                    depth_limit = _live_limit("ARCHIVE_MAX_DEPTH", ARCHIVE_MAX_DEPTH)
                    if depth >= depth_limit:
                        yield Extracted(
                            text="",
                            name=entry_name,
                            error=(
                                "archive: превышена глубина вложенности"
                                f" ({depth_limit})"
                            ),
                            container_path=container_path,
                            container_entries=chain,
                        )
                        continue
                    try:
                        nested = list(
                            self._expand_entries(
                                entry_path,
                                nested_suffix,
                                resolver,
                                container_path=container_path,
                                prefix=chain,
                                depth=depth + 1,
                                budget=budget,
                            )
                        )
                    except _TempLimitError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — битый вложенный архив
                        yield Extracted(
                            text="",
                            name=entry_name,
                            error=f"archive: {exc}",
                            container_path=container_path,
                            container_entries=chain,
                        )
                        continue
                    yield from nested
                    continue
                extractors = resolver.extractors_for(name, _head(entry_path))
                if not extractors:
                    continue  # формат не поддержан/класс не в конфиге области
                multi = len(extractors) > 1
                for extractor in extractors:
                    part = self._part_from_path(extractor, entry_name, entry_path)
                    if multi:
                        # Несколько экстракторов на одну запись: имена частей
                        # различаем по экстрактору, чтобы источники не конфликтовали.
                        part = dataclasses.replace(
                            part, name=f"{entry_name}/{extractor.name}"
                        )
                    yield dataclasses.replace(
                        part, container_path=container_path, container_entries=chain
                    )

    # --- помощники ---

    @contextmanager
    def _chain_temp(
        self, container_path: Path, entries: Sequence[str]
    ) -> Generator[Path | None, None, None]:
        """Пройти цепочку записей, каждый уровень — temp-файл.

        Отдаёт путь к файлу-листу (внутри живого временного каталога) или
        ``None``, если какой-то записи нет.
        """
        with tempfile.TemporaryDirectory() as tmp:
            budget = _TempBudget(
                _live_limit("ARCHIVE_MAX_TEMP_BYTES", ARCHIVE_MAX_TEMP_BYTES)
            )
            current = container_path
            suffix = archive_suffix(container_path.name)
            for index, name in enumerate(entries):
                out = _entry_file(tmp, name, prefix=f"{index}-")
                if not self._spool_entry(current, suffix, name, out, budget):
                    yield None
                    return
                current, suffix = out, archive_suffix(name)
            yield current

    def _spool_entry(
        self,
        source: Path,
        suffix: str,
        entry_name: str,
        out: Path,
        budget: _TempBudget,
    ) -> bool:
        """Скопировать одну запись архива в ``out`` (потоком, без RAM).

        ``source`` — файл-контейнер на диске. ``False`` — записи нет или
        формат не читается.
        """
        reader = _ENTRY_READERS.get(suffix)
        if reader is None:
            return False
        for name, handle in reader(source):
            if name != entry_name:
                continue
            with out.open("wb") as dest:
                _copy_into(handle, dest, budget)
            return True
        return False

    @staticmethod
    def _part_from_path(extractor: Extractor, name: str, path: Path) -> Extracted:
        """Запись архива (temp-файл) → Extracted с именем части."""
        try:
            result = extractor.extract(path)
        except Exception as exc:  # noqa: BLE001 — битая запись не валит архив
            return Extracted(
                text="",
                name=name,
                error=f"archive entry: {exc}",
                extractor=extractor.name,
            )
        return Extracted(
            text=result.text,
            tags=result.tags,
            pages=result.pages,
            error=result.error,
            name=name,
            extractor=extractor.name,
        )


def _entry_file(tmp: str, name: str, prefix: str = "") -> Path:
    """Temp-файл для записи: имя сохраняет суффикс (и stem у ``.gz``).

    Базовое имя записи используется как есть (в т.ч. ``notes.txt.gz`` — от него
    зависит имя единственной записи gz); пустое/слишком длинное имя заменяется
    на ``entry`` + суффикс.
    """
    base = Path(name).name
    if not base or base in (".", "..") or len(base) > 150:
        base = "entry" + archive_suffix(name)
    return Path(tmp) / f"{prefix}{base}"


def _head(path: Path) -> bytes:
    """Первые ``SNIFF_BYTES`` байт записи — для классификации по содержимому."""
    with path.open("rb") as handle:
        return handle.read(SNIFF_BYTES)
