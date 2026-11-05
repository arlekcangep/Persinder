"""Сайдкары эмбеддингов: чтение и атомарная запись (§7).

Один сайдкар на один исходный файл — ``.persinder/<файл>.pdr`` рядом с ним
(папка ``.persinder/`` игнорируется индексацией и уезжает в Syncthing).
Для данных, которых на диске нет (web, вставленный текст), сайдкары живут в
отдельной виртуальной папке: ``virtual_dir/<doc_id>.pdr``.

Формат — компактный бинарный: текст не хранится (он у источника), только
отпечаток модели, параметры чанкинга и векторы чанков (f16).
"""

from __future__ import annotations

import os
import struct
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


def file_sidecar_path(source_path: str | Path) -> Path:
    """Путь сайдкара для реального файла: ``<каталог>/.persinder/<файл>.pdr``."""
    src = Path(source_path)
    return src.parent / ".persinder" / f"{src.name}.pdr"


def virtual_sidecar_path(virtual_dir: str | Path, doc_id: str) -> Path:
    """Путь сайдкара виртуального элемента (нет файла на диске)."""
    return Path(virtual_dir) / f"{doc_id}.pdr"


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
