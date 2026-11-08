"""Сайдкары эмбеддингов: чтение и атомарная запись (§7).

Один сайдкар на один исходный источник и **одну модель** — рядом с источником
в его ``.persinder/<модель>/`` папке (папка ``.persinder/`` игнорируется
индексацией и уезжает в Syncthing). У источника могут одновременно жить
сайдкары нескольких моделей (миграция, D24). Для данных, которых на диске нет
(web, вставленный текст), сайдкары живут в отдельной виртуальной папке:
``virtual_dir/<модель>/<doc_id>.pdr``.

Архив — просто часть пути (в т.ч. вложенные архивы), поэтому внутри
``.persinder`` заводится папка на архив, а не плоское имя:

  реальный файл  a/note.md          → a/.persinder/<модель>/note.md.pdr
  часть архива   a/arc.zip/inner.md → a/.persinder/<модель>/arc.zip/inner.md.pdr
  вложенность    a/o.zip/i.zip/x.md → a/.persinder/<модель>/o.zip/i.zip/x.md.pdr

Формат — компактный бинарный: текст не хранится (он у источника), только
отпечаток модели, параметры чанкинга и векторы чанков (f16).
"""

from __future__ import annotations

import contextlib
import os
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ChunkParams, EmbedFingerprint

_MAGIC = b"PDR1"
_FORMAT_VERSION = 1
_MAX_MODEL_LEN = 255
_MAX_CHUNKS = 1_000_000
_MAX_DIM = 10_000

_QUANT_CODES = {"f16": 2, "f32": 4}
_QUANT_NAMES = {2: "f16", 4: "f32"}
# magic, формат, len(имя), dim, quant, size, overlap, count
_HEADER = struct.Struct(">4sBBHHIII")


@dataclass(frozen=True)
class SidecarChunk:
    """Чанк из сайдкара: диапазон в тексте и вектор."""

    char_start: int
    char_end: int
    vector: tuple[float, ...]


@dataclass(frozen=True)
class SidecarData:
    """Разобранный сайдкар."""

    fingerprint: EmbedFingerprint
    params: ChunkParams
    chunks: list[SidecarChunk]


def file_sidecar_path(source_path: str | Path, model: str) -> Path:
    """Путь сайдкара источника под моделью (см. ``sidecar_path``).

    ``<каталог>/.persinder/<модель>/<файл>.pdr`` — по модели своя подпапка,
    чтобы у файла одновременно жили эмбеды нескольких моделей (D24).
    """
    return sidecar_path(source_path, model)


def sidecar_path(source_path: str | Path, model: str) -> Path:
    """Путь сайдкара источника: ``.persinder/`` ближайшей реальной папки.

    Архив — часть пути, поэтому вложенность архива (в т.ч. вложенные архивы)
    сохраняется как каталоги в ``.persinder/<модель>/``:

      реальный файл  a/note.md            → a/.persinder/<model>/note.md.pdr
      часть архива   a/arc.zip/inner.md   → a/.persinder/<model>/arc.zip/inner.md.pdr
      вложенность    a/o.zip/i.zip/x.md   → a/.persinder/<model>/o.zip/i.zip/x.md.pdr

    Т.е. внутри ``.persinder`` заводится папка на архив (по имени архива как
    части пути). Ближайший реальный префикс (до первой несуществующей части
    пути) — сам файл или внешний контейнер; ``.persinder/`` кладётся в его
    настоящую папку.
    """
    src = Path(source_path)
    # Поднимаемся к первому компоненту, реально существующему на диске:
    # для обычного файла это он сам, для части архива — внешний контейнер.
    probe = src
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    real_dir = probe if probe.is_dir() else probe.parent
    rel = src.relative_to(real_dir)
    return real_dir / ".persinder" / model / f"{rel}.pdr"


def virtual_sidecar_path(virtual_dir: str | Path, doc_id: str, model: str) -> Path:
    """Путь сайдкара виртуального элемента (нет файла на диске) под моделью."""
    return Path(virtual_dir) / model / f"{doc_id}.pdr"


def delete_file_sidecars(source_path: str | Path) -> None:
    """Удалить все сайдкары источника (по всем моделям): источник ушёл — они
    неактуальны. Учитывает части контейнера по правилу ``sidecar_path``.
    """
    real_dir = _real_dir(Path(source_path))
    if real_dir is None:
        return
    pen = real_dir / ".persinder"
    if not pen.is_dir():
        return
    rel = Path(source_path).relative_to(real_dir)
    for model_dir in pen.iterdir():
        if model_dir.is_dir():
            with contextlib.suppress(FileNotFoundError):
                path = model_dir / f"{rel}.pdr"
                path.unlink()
                _prune_empty_dirs(path)


def _real_dir(src: Path) -> Path | None:
    """Ближайшая реальная папка для ``.persinder/`` источника (см. ``sidecar_path``)."""
    probe = src
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        return None
    return probe if probe.is_dir() else probe.parent


def _prune_empty_dirs(path: Path) -> None:
    """Убрать опустевшие каталоги вверх до ``.persinder/<модель>/``."""
    model_dir = path.parent
    while model_dir != model_dir.parent and model_dir.name != ".persinder":
        try:
            model_dir.rmdir()
        except OSError:
            return
        model_dir = model_dir.parent


def delete_virtual_sidecars(virtual_dir: str | Path, doc_id: str) -> None:
    """Удалить все сайдкары виртуального элемента (по всем моделям)."""
    root = Path(virtual_dir)
    if not root.is_dir():
        return
    name = f"{doc_id}.pdr"
    for model_dir in root.iterdir():
        if model_dir.is_dir():
            with contextlib.suppress(FileNotFoundError):
                (model_dir / name).unlink()


def delete_model_sidecars(
    virtual_dir: str | Path, model: str, sources: Sequence[Mapping[str, object]]
) -> None:
    """Удалить все сайдкары модели (``model remove``): веса и эмбеды уходят вместе.

    ``sources`` — строки ``document_sources`` (см. ``MetaStore.all_sources``).
    Учитывает и части контейнера (архив как часть пути) — берётся
    ``sidecar_path`` каждого источника.
    """
    virtual_root = Path(virtual_dir) / model
    with contextlib.suppress(FileNotFoundError):
        for path in virtual_root.iterdir():
            if path.is_file():
                path.unlink()
    for row in sources:
        raw = row.get("source_path")
        if not raw:
            continue
        source_path = str(raw)
        path = sidecar_path(source_path, model)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        _prune_empty_dirs(path)


def pack_f16_vectors(vectors: list[list[float]], dim: int) -> bytes:
    """Упаковать векторы в f16-блоб (формат сайдкара и таблицы embeddings)."""
    fmt = struct.Struct(f">{dim}e")
    out = bytearray()
    for vector in vectors:
        if len(vector) != dim:
            raise ValueError(f"размерность вектора {len(vector)} != {dim}")
        out += fmt.pack(*vector)
    return bytes(out)


def unpack_f16_vectors(data: bytes, dim: int) -> list[tuple[float, ...]]:
    """Разобрать f16-блоб обратно в список векторов."""
    fmt = struct.Struct(f">{dim}e")
    size = fmt.size
    return [fmt.unpack(data[i : i + size]) for i in range(0, len(data), size)]


def write_sidecar(
    path: Path,
    fingerprint: EmbedFingerprint,
    params: ChunkParams,
    chunks: list[tuple[int, int, list[float]]],
) -> None:
    """Записать сайдкар атомарно (временный файл → ``rename``).

    Каждый элемент ``chunks`` — ``(char_start, char_end, vector)``.
    """
    quant_code = _QUANT_CODES[fingerprint.quant]
    name = fingerprint.model.encode("utf-8")
    if len(name) > _MAX_MODEL_LEN:
        raise ValueError(f"имя модели слишком длинное: {fingerprint.model!r}")
    if len(chunks) > _MAX_CHUNKS or fingerprint.dim > _MAX_DIM:
        raise ValueError("сайдкар слишком большой")

    buf = bytearray()
    buf += _HEADER.pack(
        _MAGIC,
        _FORMAT_VERSION,
        len(name),
        fingerprint.dim,
        quant_code,
        params.size,
        params.overlap,
        len(chunks),
    )
    buf += name
    vector_fmt = struct.Struct(f">{fingerprint.dim}e")
    for start, end, vector in chunks:
        if len(vector) != fingerprint.dim:
            raise ValueError(f"размерность вектора {len(vector)} != {fingerprint.dim}")
        buf += struct.pack(">II", start, end)
        buf += vector_fmt.pack(*vector)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(bytes(buf))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_sidecar(path: Path) -> SidecarData | None:
    """Прочитать сайдкар; битый/несуществующий файл → None."""
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    try:
        header = _HEADER.unpack(data[: _HEADER.size])
        magic, version, name_len, dim, quant_code, size, overlap, count = header
        if magic != _MAGIC or version != _FORMAT_VERSION:
            return None
        if name_len > _MAX_MODEL_LEN or dim <= 0 or dim > _MAX_DIM:
            return None
        if count > _MAX_CHUNKS:
            return None
        offset = _HEADER.size
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        quant = _QUANT_NAMES.get(quant_code)
        if quant is None:
            return None
        vector_fmt = struct.Struct(f">{dim}e")
        vector_bytes = vector_fmt.size
        need = offset + count * (8 + vector_bytes)
        if len(data) < need:
            return None
        chunks: list[SidecarChunk] = []
        for _ in range(count):
            start, end = struct.unpack(">II", data[offset : offset + 8])
            offset += 8
            vector = vector_fmt.unpack(data[offset : offset + vector_bytes])
            offset += vector_bytes
            chunks.append(SidecarChunk(start, end, vector))
    except (struct.error, UnicodeDecodeError, IndexError, ValueError):
        return None
    return SidecarData(
        fingerprint=EmbedFingerprint(model=name, dim=dim, quant=quant),
        params=ChunkParams(size=size, overlap=overlap),
        chunks=chunks,
    )
