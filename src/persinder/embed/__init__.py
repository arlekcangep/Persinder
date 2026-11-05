"""Модуль 2: эмбеддинги — очередь → векторы → сайдкары."""

from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .service import EmbedWorker, PlaceholderEmbedder
from .sidecar import (
    SidecarChunk,
    SidecarData,
    file_sidecar_path,
    read_sidecar,
    virtual_sidecar_path,
    write_sidecar,
)

__all__ = [
    "EmbedWorker",
    "PlaceholderEmbedder",
    "EmbedFingerprint",
    "ChunkParams",
    "EmbedStats",
    "SidecarChunk",
    "SidecarData",
    "file_sidecar_path",
    "virtual_sidecar_path",
    "write_sidecar",
    "read_sidecar",
]
