"""Архивы (zip/7z/tar/gz/rar) — контейнер, класс ``archive``.

Каждая запись — отдельная часть; вложенные архивы раскрываются рекурсивно
до ``ARCHIVE_MAX_DEPTH`` с общим бюджетом анти-бомб.
"""

from __future__ import annotations

import dataclasses
import gzip
import io
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..classify import ARCHIVE
from ._base import BaseExtractor, Extracted
from ._common import (
    ARCHIVE_MAX_DEPTH,
    ARCHIVE_MAX_ENTRIES,
    ARCHIVE_MAX_ENTRY_BYTES,
    ARCHIVE_MAX_TOTAL_BYTES,
    archive_suffix,
)

if TYPE_CHECKING:
    from ..interfaces import EntryResolver, Extractor


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


class _ArchiveBudget:
    """Общий на весь файл бюджет распаковки (счётчики на рекурсию)."""

    __slots__ = ("entries", "total")

    def __init__(self) -> None:
        self.entries = 0
        self.total = 0


# Составные суффиксы проверяются до обычных: tar.gz/tgz — один tar-контейнер.


class ArchiveExtractor(BaseExtractor):
    """Архивы (zip/7z/tar/gz/rar) — контейнер, класс ``archive``.

    Каждая запись — отдельная часть; экстрактор для записи выбирает резолвер
    (``_AreaEntryResolver``) **по содержимому записи** (магия/снифф байтов),
    расширение — лишь хинт. Записи-архивы раскрываются **рекурсивно** с лимитом
    глубины ``ARCHIVE_MAX_DEPTH``
    (``folder/a.zip/b.zip/c.pdf``): у части
    явно сохраняется ``container_path`` и цепочка ``container_entries``, а не только
    человекочитаемое имя. ``tar.gz``/``tgz`` — один tar-уровень, одиночный
    ``.gz`` — один «файл» без суффикса. Пределы анти-бомб — на весь файл:
    число записей, размер записи, суммарный объём, глубина. Битая запись не
    валит архив (часть с ``error``), битый архив — одна часть с ошибкой.
    ``7z`` требует extra ``7z`` (py7zr), ``rar`` — extra ``rar`` (rarfile +
    внешний распаковщик).
    """

    name = "archive"
    format_class = ARCHIVE
    subtypes = frozenset({"zip", "7z", "tar", "gzip", "rar"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".zip", ".7z", ".tar", ".gz", ".tgz", ".rar"})

    # --- читатели форматов: путь → [(имя записи, байты)] ---

    @staticmethod
    def _read_zip(path: Path) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                out.append(
                    (
                        info.filename.replace("\\", "/"),
                        archive.read(info),
                    )
                )
        return out

    @staticmethod
    def _read_7z(path: Path) -> list[tuple[str, bytes]]:
        try:
            import py7zr  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra '7z' не установлен
            raise RuntimeError(
                "формат 7z требует extra '7z': pip install 'persinder[archive7z]'"
            ) from exc
        out: list[tuple[str, bytes]] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            py7zr.SevenZipFile(path, "r") as archive,
        ):
            archive.extractall(path=tmp)
            root = Path(tmp)
            for name in archive.getnames():
                file = root / name
                if file.is_file():
                    out.append((name.replace("\\", "/"), file.read_bytes()))
        return out

    def _read_7z_entry(self, path: Path, entry_name: str) -> bytes | None:
        """Одна запись из 7z по имени (через ``extract(targets=[...])``).

        Извлекает в temp-папку только выбранную запись и читает её байты;
        ``None`` — записи нет. ``ImportError`` прокидывается как ``RuntimeError``
        (формат требует extra '7z').
        """
        try:
            import py7zr  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra '7z' не установлен
            raise RuntimeError(
                "формат 7z требует extra '7z': pip install 'persinder[archive7z]'"
            ) from exc
        with (
            tempfile.TemporaryDirectory() as tmp,
            py7zr.SevenZipFile(path, "r") as archive,
        ):
            try:
                info = archive.getinfo(entry_name)
            except KeyError:
                return None
            if info.is_directory:
                return None
            archive.extract(path=tmp, targets=[entry_name])
            file = Path(tmp) / entry_name
            if not file.is_file():
                return None
            return file.read_bytes()

    @staticmethod
    def _read_tar(path: Path) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                out.append((member.name.replace("\\", "/"), handle.read()))
        return out

    @staticmethod
    def _read_gz(path: Path) -> list[tuple[str, bytes]]:
        data = gzip.decompress(path.read_bytes())
        # имя без последнего суффикса: notes.txt.gz → notes.txt
        stem = (
            path.name[: -len(".gz")] if path.name.lower().endswith(".gz") else path.name
        )
        return [(stem, data)]

    def _read_gz_entry(self, path: Path, entry_name: str) -> bytes | None:
        """Единственная запись gz (имя — stem без ``.gz``).

        gz — один поток, «адресность» сводится к сверке имени: если запрос не
        совпадает со stem, вернуть ``None``.
        """
        stem = (
            path.name[: -len(".gz")] if path.name.lower().endswith(".gz") else path.name
        )
        if entry_name != stem:
            return None
        return gzip.decompress(path.read_bytes())

    def _read_rar(self, path: Path) -> list[tuple[str, bytes]]:
        try:
            import rarfile  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra 'rar' не установлен
            raise RuntimeError(
                "формат rar требует extra 'rar': pip install 'persinder[archiverar]'"
            ) from exc
        out: list[tuple[str, bytes]] = []
        archive: Any = rarfile.RarFile(path)  # pyright: ignore[reportUnknownMemberType]
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = str(info.filename).replace("\\", "/")
                out.append((name, bytes(archive.read(info))))
        return out

    def _read_rar_entry(self, path: Path, entry_name: str) -> bytes | None:
        """Одна запись из rar по имени (``RarFile.read`` читает поштучно).

        ``None`` — записи нет. ``ImportError`` прокидывается как ``RuntimeError``
        (формат требует extra 'rar').
        """
        try:
            import rarfile  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
        except ImportError as exc:  # extra 'rar' не установлен
            raise RuntimeError(
                "формат rar требует extra 'rar': pip install 'persinder[archiverar]'"
            ) from exc
        archive: Any = rarfile.RarFile(path)  # pyright: ignore[reportUnknownMemberType]
        with archive:
            try:
                info = archive.getinfo(entry_name)
            except KeyError:
                return None
            if info.is_dir():
                return None
            return bytes(archive.read(entry_name))

    def _read_entry_bytes(
        self, source: Path | bytes, suffix: str, entry_name: str
    ) -> bytes | None:
        """Байты одной записи из архива (по пути или уже прочитанным байтам).

        ``source`` — файл-контейнер на диске или его байты (для вложенных
        архивов: запись верхнего архива — это архив целиком). ``suffix`` —
        формат контейнера (``.zip``/``.tar.gz``/``.7z``…), ``entry_name`` —
        имя записи внутри. ``None`` — записи нет или это директория.
        """
        if suffix == ".zip":
            handle: Any = io.BytesIO(source) if isinstance(source, bytes) else source
            with zipfile.ZipFile(handle) as zf:
                if entry_name not in zf.namelist():
                    return None
                return zf.read(entry_name)
        if suffix in (
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
        ):
            tarball: Any = io.BytesIO(source) if isinstance(source, bytes) else source
            with tarfile.open(tarball, "r:*") as tf:
                try:
                    member = tf.getmember(entry_name)
                except KeyError:
                    return None
                if not member.isfile():
                    return None
                f = tf.extractfile(member)
                return None if f is None else f.read()
        if suffix == ".7z":
            if isinstance(source, bytes):
                with tempfile.TemporaryDirectory() as tmp:
                    inner = Path(tmp) / "archive.7z"
                    inner.write_bytes(source)
                    return self._read_7z_entry(inner, entry_name)
            return self._read_7z_entry(Path(source), entry_name)
        if suffix == ".rar":
            if isinstance(source, bytes):
                with tempfile.TemporaryDirectory() as tmp:
                    inner = Path(tmp) / "archive.rar"
                    inner.write_bytes(source)
                    return self._read_rar_entry(inner, entry_name)
            return self._read_rar_entry(Path(source), entry_name)
        if suffix == ".gz":
            if isinstance(source, bytes):
                return gzip.decompress(source)
            return self._read_gz_entry(Path(source), entry_name)
        return None

    def _reader(self, suffix: str) -> Callable[[Path], list[tuple[str, bytes]]] | None:
        readers: dict[str, Callable[[Path], list[tuple[str, bytes]]]] = {
            ".zip": self._read_zip,
            ".tar": self._read_tar,
            ".tar.gz": self._read_tar,
            ".tar.bz2": self._read_tar,
            ".tar.xz": self._read_tar,
            ".tgz": self._read_tar,
            ".tbz2": self._read_tar,
            ".txz": self._read_tar,
            ".gz": self._read_gz,
            ".7z": self._read_7z,
            ".rar": self._read_rar,
        }
        return readers.get(suffix)

    # --- главный вход контейнера ---

    def extract_all(self, path: Path, resolver: EntryResolver) -> list[Extracted]:
        container_path = str(path)
        try:
            suffix = archive_suffix(path.name)
            reader = self._reader(suffix)
            if reader is None:
                return [
                    Extracted(
                        text="",
                        error=f"archive: формат {suffix} не читается",
                        container_path=container_path,
                    )
                ]
            entries = reader(path)
        except RuntimeError as exc:
            return [Extracted(text="", error=str(exc), container_path=container_path)]
        except Exception as exc:  # noqa: BLE001 — битый/нечитаемый архив
            return [
                Extracted(
                    text="", error=f"archive: {exc}", container_path=container_path
                )
            ]
        return self._expand_entries(
            entries, resolver, container_path=container_path, budget=_ArchiveBudget()
        )

    def _expand_entries(
        self,
        entries: list[tuple[str, bytes]],
        resolver: EntryResolver,
        *,
        container_path: str,
        prefix: tuple[str, ...] = (),
        depth: int = 0,
        budget: _ArchiveBudget,
    ) -> list[Extracted]:
        """Записи архива → части; вложенные архивы раскрываются рекурсивно.

        ``prefix`` — цепочка записей от контейнера до текущего архива,
        ``depth`` — сколько архивов уже раскрыто. Часть получает
        ``container_path`` + ``container_entries = prefix + (name,)`` — однозначный
        обратный путь, не зависящий от разбора ``source_path``. Бюджет
        (записи/байты) общий на весь файл, поэтому вложенность не обходит
        анти-бомб-лимиты.
        """
        parts: list[Extracted] = []
        for name, data in entries:
            budget.entries += 1
            if budget.entries > _live_limit("ARCHIVE_MAX_ENTRIES", ARCHIVE_MAX_ENTRIES):
                break
            if budget.total + len(data) > _live_limit(
                "ARCHIVE_MAX_TOTAL_BYTES", ARCHIVE_MAX_TOTAL_BYTES
            ):
                break
            if len(data) > _live_limit(
                "ARCHIVE_MAX_ENTRY_BYTES", ARCHIVE_MAX_ENTRY_BYTES
            ):
                continue
            chain = (*prefix, name)
            entry_name = "/".join(chain)
            if self._reader(archive_suffix(name)) is not None:
                budget.total += len(data)
                depth_limit = _live_limit("ARCHIVE_MAX_DEPTH", ARCHIVE_MAX_DEPTH)
                if depth >= depth_limit:
                    parts.append(
                        Extracted(
                            text="",
                            name=entry_name,
                            error=(
                                "archive: превышена глубина вложенности"
                                f" ({depth_limit})"
                            ),
                            container_path=container_path,
                            container_entries=chain,
                        )
                    )
                    continue
                try:
                    nested = self._read_nested(name, data)
                except Exception as exc:  # noqa: BLE001 — битый вложенный архив
                    parts.append(
                        Extracted(
                            text="",
                            name=entry_name,
                            error=f"archive: {exc}",
                            container_path=container_path,
                            container_entries=chain,
                        )
                    )
                    continue
                parts.extend(
                    self._expand_entries(
                        nested,
                        resolver,
                        container_path=container_path,
                        prefix=chain,
                        depth=depth + 1,
                        budget=budget,
                    )
                )
                continue
            budget.total += len(data)
            extractors = resolver.extractors_for(name, data)
            if not extractors:
                continue  # формат не поддержан/класс не в конфиге области
            multi = len(extractors) > 1
            for extractor in extractors:
                part = self._part_from_bytes(extractor, entry_name, data)
                if multi:
                    # Несколько экстракторов на одну запись: имена частей
                    # различаем по экстрактору, чтобы источники не конфликтовали.
                    part = dataclasses.replace(
                        part, name=f"{entry_name}/{extractor.name}"
                    )
                parts.append(
                    dataclasses.replace(
                        part, container_path=container_path, container_entries=chain
                    )
                )
        return parts

    def _read_nested(self, name: str, data: bytes) -> list[tuple[str, bytes]]:
        """Прочитать вложенный архив из байтов записи (через временный файл)."""
        with tempfile.TemporaryDirectory() as tmp:
            inner_path = Path(tmp) / Path(name).name
            inner_path.write_bytes(data)
            reader = self._reader(archive_suffix(name))
            assert reader is not None
            return reader(inner_path)

    @staticmethod
    def _part_from_bytes(extractor: Extractor, name: str, data: bytes) -> Extracted:
        """Запись архива → временный файл → Extracted с именем части."""
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / ("entry" + archive_suffix(name))
            entry.write_bytes(data)
            try:
                result = extractor.extract(entry)
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

    def extract_entry(self, path: Path, entry_name: str) -> Extracted | None:
        """Извлечь одну запись из архива по имени — без чтения всего архива в память.

        Используется при записи текста в кэш (T4), чтобы не материализовать
        весь архив. Поддерживает zip, tar, 7z, rar, gz.

        Возвращает ``Extracted`` с текстом записи или ``None``, если запись
        не найдена, является директорией или формат не поддерживает адресное
        чтение.
        """
        suffix = archive_suffix(path.name)
        if self._reader(suffix) is None:
            return None
        try:
            data = self._read_entry_bytes(path, suffix, entry_name)
        except Exception:  # noqa: BLE001 — битый архив не валит индексацию
            return None
        if data is None:
            return None
        return Extracted(text=data.decode("utf-8", errors="replace"), name=entry_name)

    def read_entry_chain(
        self, container_path: str | Path, entries: Sequence[str]
    ) -> tuple[str, bytes] | None:
        """Пройти цепочку записей контейнера и вернуть ``(имя_листа, байты)``.

        ``entries`` — цепочка от файла-контейнера ``container_path`` к нужному
        файлу; вложенные архивы проходятся по байтам (без временных файлов,
        кроме 7z/rar). ``None`` — цепочка пуста или какой-то записи нет.
        """
        if not entries:
            return None
        current: Path | bytes = Path(container_path)
        suffix = archive_suffix(Path(container_path).name)
        for index, name in enumerate(entries):
            data = self._read_entry_bytes(current, suffix, name)
            if data is None:
                return None
            if index == len(entries) - 1:
                return name, data
            current = data
            suffix = archive_suffix(name)
        return None

    def extract_part(
        self,
        container_path: str,
        entries: Sequence[str],
        extractor: Extractor,
    ) -> Extracted | None:
        """Извлечь часть контейнера по цепочке записей и прогнать листовой экстрактор.

        Адресное чтение (T4): весь архив в память не материализуется — читается
        только нужная запись. ``None`` — записи нет или прочитать нельзя.
        """
        found = self.read_entry_chain(container_path, entries)
        if found is None:
            return None
        name, data = found
        return self._part_from_bytes(extractor, name, data)
