"""Модуль 2: эмбеддинги — очередь → векторы → сайдкары + модель-менеджмент (D24)."""

from .models import ChunkParams, EmbedFingerprint, EmbedStats
from .onnx import OnnxEmbedder
from .registry import (
    CATALOG,
    PLACEHOLDER,
    ModelSpec,
    build_embedder,
    install,
    installed_keys,
    is_installed,
    models_root,
    remove_weights,
    validate_key,
)
from .service import EmbedWorker, PlaceholderEmbedder
from .sidecar import (
    SidecarChunk,
    SidecarData,
    delete_file_sidecars,
    delete_model_sidecars,
    delete_virtual_sidecars,
    file_sidecar_path,
    pack_f16_vectors,
    read_sidecar,
    unpack_f16_vectors,
    virtual_sidecar_path,
    write_sidecar,
)
from .state import ModelState, load, save

__all__ = [
    "EmbedWorker",
    "PlaceholderEmbedder",
    "OnnxEmbedder",
    "EmbedFingerprint",
    "ChunkParams",
    "EmbedStats",
    "SidecarChunk",
    "SidecarData",
    "file_sidecar_path",
    "virtual_sidecar_path",
    "write_sidecar",
    "read_sidecar",
    "pack_f16_vectors",
    "unpack_f16_vectors",
    "delete_file_sidecars",
    "delete_virtual_sidecars",
    "delete_model_sidecars",
    "CATALOG",
    "PLACEHOLDER",
    "ModelSpec",
    "models_root",
    "validate_key",
    "installed_keys",
    "is_installed",
    "install",
    "remove_weights",
    "build_embedder",
    "ModelState",
    "load",
    "save",
]
