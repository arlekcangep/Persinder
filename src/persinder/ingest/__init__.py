"""Сервис приёма: морфоиндекс + сайдкар + очередь эмбеддингов."""

from .models import IngestRequest, IngestResult
from .service import IngestService, chunk_text

__all__ = ["IngestService", "chunk_text", "IngestRequest", "IngestResult"]
