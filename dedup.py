"""
dedup.py — Семантический дедуп постов через локальные эмбеддинги.

Зачем: лексический дедуп (пересечение слов) пропускает перефраз — «то же самое,
другими словами». Эмбеддинги ловят смысловую близость: два поста про одно и то же
событие получают cosine ~0.9, а про разное — близко к 0.

Бэкенд подключаемый:
  - "embedding" — sentence-transformers (multilingual MiniLM, 384-dim) — если установлен.
  - "lexical"   — фолбэк (вызывающий код сам считает по словам), если модели нет.

Модель грузится ЛЕНИВО (при первом embed), чтобы старт бота оставался лёгким и
память росла только когда реально идёт генерация.

Вектора нормализованы → cosine = скалярное произведение.
Хранение в SQLite: float32 bytes (BLOB), 384*4 = 1536 байт на пост.
"""

import asyncio

from loguru import logger

_MODEL = None
_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_BACKEND: str | None = None  # "embedding" | "lexical"
_np = None


def _get_numpy():
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np


def _get_model():
    """Лениво загружает модель. При ошибке — переключает бэкенд на lexical."""
    global _MODEL, _BACKEND
    if _BACKEND == "lexical":
        return None
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(_MODEL_NAME)
        _BACKEND = "embedding"
        logger.info("dedup: модель эмбеддингов загружена (semantic backend)")
    except Exception as e:
        _BACKEND = "lexical"
        _MODEL = None
        logger.warning(f"dedup: эмбеддинги недоступны ({type(e).__name__}: {e}) — лексический фолбэк")
    return _MODEL


def backend() -> str:
    """Возвращает активный бэкенд ('embedding' или 'lexical')."""
    if _BACKEND is None:
        _get_model()
    return _BACKEND or "lexical"


def embed(text: str):
    """Синхронно считает нормализованный вектор. None если бэкенд лексический."""
    m = _get_model()
    if m is None:
        return None
    try:
        np = _get_numpy()
        vec = m.encode([text], normalize_embeddings=True)[0]
        return np.asarray(vec, dtype=np.float32)
    except Exception as e:
        logger.warning(f"dedup.embed ошибка: {e}")
        return None


async def aembed(text: str):
    """Асинхронная обёртка — считает эмбеддинг в executor, не блокируя event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed, text)


def to_blob(vec) -> bytes:
    """Вектор → bytes для хранения в SQLite."""
    np = _get_numpy()
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes):
    """bytes из SQLite → вектор."""
    np = _get_numpy()
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a, b) -> float:
    """Косинус для НОРМАЛИЗОВАННЫХ векторов = скалярное произведение."""
    np = _get_numpy()
    return float(np.dot(a, b))


def max_similarity(vec, others: list) -> float:
    """Максимальная близость vec к любому из others (список векторов)."""
    best = 0.0
    for o in others:
        try:
            s = cosine(vec, o)
        except Exception:
            continue
        if s > best:
            best = s
    return best
