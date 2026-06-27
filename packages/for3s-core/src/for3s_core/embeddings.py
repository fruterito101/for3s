"""Motor de embeddings de For3s OS (H5, 2026-06-19) — Nodo 2 Hipocampo.

Convierte texto (turnos de memoria, queries) en vectores de 1024 dimensiones que
capturan el SIGNIFICADO. Es la materia prima de la búsqueda semántica: vectores
parecidos = significados parecidos.

Modelo: BAAI/bge-m3 (multilingüe — español + código, lo que For3s usa; 1024 dim;
8192 tokens de contexto; corre LOCAL en CPU, sin API externa = privacidad + costo
cero). Reemplazó a Stella, que daba bugs de código custom en CPU y era solo-inglés
(ver Mente OS/Doc/H5_Infra_Memoria_AGE_pgvector.md §"Decisión Stella→BGE-M3").

CLAVE DE RENDIMIENTO: el modelo se carga UNA vez (lazy singleton) y se reusa. La
primera carga tarda ~160s (modelo a RAM, ~2.6GB); recargarlo por cada turno sería
inviable. Por eso `_modelo` es global y `_get_modelo()` lo carga solo la 1ª vez.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("for3s.embeddings")

MODELO_NOMBRE = "BAAI/bge-m3"
DIM = 1024  # dimensión del vector (debe coincidir con la columna vector(1024) en BD)

# Singleton perezoso del modelo + lock para que dos hilos no lo carguen a la vez.
_modelo = None
_lock = threading.Lock()


def _get_modelo():
    """Carga BGE-M3 la 1ª vez y lo reusa. Thread-safe. Bloqueante (~160s la 1ª vez)."""
    global _modelo
    if _modelo is None:
        with _lock:
            if _modelo is None:  # doble check dentro del lock
                logger.info("cargando modelo de embeddings %s (1ª vez, ~160s)...", MODELO_NOMBRE)
                from sentence_transformers import SentenceTransformer

                _modelo = SentenceTransformer(MODELO_NOMBRE, device="cpu")
                logger.info("modelo de embeddings cargado")
    return _modelo


def embed(texto: str) -> list[float]:
    """Embedding de UN texto → lista de 1024 floats. SÍNCRONO (CPU intensivo).
    Normaliza el vector (para usar distancia coseno en pgvector)."""
    return embed_lote([texto])[0]


def embed_lote(textos: list[str]) -> list[list[float]]:
    """Embeddings de varios textos a la vez (más eficiente que uno por uno).
    Devuelve lista de vectores (cada uno 1024 floats, normalizados)."""
    modelo = _get_modelo()
    # normalize_embeddings=True → vectores unitarios → el producto punto ES la
    # similitud coseno (lo que usa el índice HNSW vector_cosine_ops).
    arr = modelo.encode(textos, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in arr]


def a_pgvector(vector: list[float]) -> str:
    """Formatea un vector como literal de pgvector: '[0.1,0.2,...]'.
    pgvector acepta ese string en INSERT/UPDATE de una columna vector."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
