# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — CLS Consolidation (Nodo 10), motor de consolidación de memoria.

CLS = Complementary Learning Systems: cómo el cerebro, durante el sueño, pasa lo
episódico (Hipocampo) a lo semántico permanente (Neocorteza). For3s lo replica:
de noche agrupa episodios parecidos y extrae conceptos al Knowledge Graph.

Se construye en piezas AISLADAS (H6 Sub-pasos 4-7):
  · Sub-paso 4 (ESTE): CLUSTERING — agrupar episodios por significado. SIN LLM,
    SIN escribir, SIN marcar nada. Solo forma los grupos.
  · Sub-paso 5: extraer concepto de cada cluster con sonnet-4-7.
  · Sub-paso 6: escribir el concepto al grafo (reusa kg.py).
  · Sub-paso 7: orquestador end-to-end + dry-run.

Este módulo NO borra nada y NO se conecta al cron todavía (Sub-paso 10).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger("for3s.cls")

# Parámetros LOCKED (R2 §2.6)
MIN_CLUSTER_SIZE = 3  # un cluster necesita ≥3 episodios parecidos
THRESHOLD_PENDIENTES = 10  # si hay <10 pendientes, no vale la pena consolidar

# Modelo LLM para extraer conceptos. Configurable por env (FOR3S_CLS_MODEL).
# Default sonnet-4-6: el mismo verificado que corre el bot (sonnet-4-7 daba 404).
CLS_MODEL = os.environ.get("FOR3S_CLS_MODEL", "claude-sonnet-4-6").strip()

# Privacidad (Pilar 1): cuántos ejemplos y cuántos chars por ejemplo se mandan al
# LLM. NUNCA se envían los episodios crudos completos — solo un summary acotado.
_EJEMPLOS_MAX = 3
_CHARS_POR_EJEMPLO = 200


@dataclass(frozen=True)
class Cluster:
    """Un grupo de episodios parecidos por significado, candidato a un concepto."""

    seqs: list[int]  # los seq de los episodios del grupo
    contents: list[str]  # sus textos (para construir el summary en Sub-paso 5)
    tam: int  # cuántos episodios


@dataclass(frozen=True)
class ResultadoClustering:
    """Salida del clustering: los grupos formados + diagnóstico."""

    clusters: list[Cluster]
    total_pendientes: int  # cuántos episodios pendientes se evaluaron
    n_ruido: int  # cuántos quedaron SIN cluster (etiqueta -1, normal)
    salto: bool  # True si se saltó por <THRESHOLD_PENDIENTES


async def _cargar_pendientes(
    pool: asyncpg.Pool,
    session_id: str,
    limite: int,
) -> list[asyncpg.Record]:
    """Lee episodios PENDIENTES de consolidar (no consolidados, vivos, con embedding)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT seq, content, embedding FROM episodes_events "
            "WHERE session_id = $1 AND consolidated_to_kg = false "
            "AND deleted_at IS NULL AND embedding IS NOT NULL "
            "ORDER BY seq ASC LIMIT $2",
            session_id,
            limite,
        )


def _parse_vector(emb) -> list[float]:
    """pgvector llega como str '[0.1,0.2,...]' o como lista. Normaliza a list[float]."""
    if isinstance(emb, str):
        return [float(x) for x in emb.strip("[]").split(",") if x]
    return [float(x) for x in emb]


async def clusterizar_pendientes(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    max_per_run: int = 500,
) -> ResultadoClustering:
    """Agrupa por SIGNIFICADO los episodios pendientes de consolidar (Sub-paso 4).

    Usa HDBSCAN sobre los embeddings BGE-M3 (ya normalizados). Como están
    normalizados, la distancia euclídea es monótona con la coseno → usamos
    'euclidean' (HDBSCAN no trae 'cosine' nativo en su algoritmo rápido).

    NO escribe nada, NO llama LLM. Solo forma los grupos. Los episodios que no se
    parecen a ningún grupo quedan como RUIDO (etiqueta -1) y NO se consolidan —
    eso es correcto y honesto, no un fallo.

    Devuelve ResultadoClustering. Si hay <THRESHOLD_PENDIENTES, salta (salto=True).
    """
    import numpy as np
    from hdbscan import HDBSCAN

    rows = await _cargar_pendientes(pool, session_id, max_per_run)
    total = len(rows)

    if total < THRESHOLD_PENDIENTES:
        logger.info("[cls] %d pendientes < umbral %d → salto", total, THRESHOLD_PENDIENTES)
        return ResultadoClustering([], total, 0, salto=True)

    vectores = np.array([_parse_vector(r["embedding"]) for r in rows], dtype=np.float32)

    modelo = HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, metric="euclidean")
    labels = modelo.fit_predict(vectores)

    # agrupar por label (ignorando -1 = ruido)
    grupos: dict[int, list[int]] = {}
    for idx, lab in enumerate(labels):
        lab = int(lab)
        if lab == -1:
            continue
        grupos.setdefault(lab, []).append(idx)

    clusters = [
        Cluster(
            seqs=[int(rows[i]["seq"]) for i in idxs],
            contents=[rows[i]["content"] for i in idxs],
            tam=len(idxs),
        )
        for idxs in grupos.values()
    ]
    n_ruido = int((labels == -1).sum())

    logger.info(
        "[cls] %d pendientes → %d clusters, %d en ruido",
        total,
        len(clusters),
        n_ruido,
    )
    return ResultadoClustering(clusters, total, n_ruido, salto=False)


# ===========================================================================
# Sub-paso 5 — EXTRACCIÓN DE CONCEPTO (LLM focaliza, con privacidad y fallback)
# ===========================================================================


@dataclass(frozen=True)
class Concepto:
    """El concepto destilado de un cluster — lo que el Sub-paso 6 escribe al grafo."""

    label: str  # nombre corto del concepto (ej. "Análisis de repos GitHub")
    descripcion: str  # 1-2 frases que resumen el patrón
    tipo: str  # categoría libre (ej. "tema", "actividad", "saludo")
    seqs: list[int]  # episodios fuente (para las aristas DERIVED_FROM)
    via_llm: bool  # True si lo extrajo el LLM; False si fue fallback heurístico


# Palabras vacías mínimas para el fallback heurístico (es-en, sin dependencias).
_STOP = {
    "the",
    "a",
    "an",
    "de",
    "la",
    "el",
    "los",
    "las",
    "y",
    "o",
    "que",
    "en",
    "un",
    "una",
    "es",
    "con",
    "por",
    "para",
    "se",
    "su",
    "lo",
    "al",
    "del",
    "me",
    "te",
    "mi",
    "tu",
    "este",
    "esta",
    "esto",
    "como",
    "más",
    "pero",
    "ya",
    "si",
    "no",
    "hola",
    "ok",
    "of",
    "to",
    "and",
    "in",
    "is",
    "it",
    "you",
    "i",
}


def _construir_summary(cluster: Cluster) -> str:
    """Construye el SUMMARY que se manda al LLM. PRIVACIDAD (Pilar 1): NO manda los
    episodios crudos completos — solo el tamaño + hasta 3 ejemplos TRUNCADOS."""
    lineas = [f"Grupo de {cluster.tam} mensajes parecidos entre sí. Ejemplos:"]
    for txt in cluster.contents[:_EJEMPLOS_MAX]:
        recorte = (txt or "").strip().replace("\n", " ")[:_CHARS_POR_EJEMPLO]
        lineas.append(f"- {recorte}")
    return "\n".join(lineas)


def _fallback_heuristico(cluster: Cluster) -> Concepto:
    """Concepto SIN LLM: label = las palabras más frecuentes del cluster. Pobre
    pero funcional — se usa si el LLM cae o devuelve algo inválido."""
    palabras: Counter = Counter()
    for txt in cluster.contents:
        for w in re.findall(r"[a-záéíóúñ0-9]{3,}", (txt or "").lower()):
            if w not in _STOP:
                palabras[w] += 1
    top = [w for w, _ in palabras.most_common(3)]
    label = " / ".join(top) if top else "grupo sin etiqueta"
    return Concepto(
        label=label[:80],
        descripcion=f"Patrón recurrente ({cluster.tam} episodios): {label}.",
        tipo="heuristico",
        seqs=cluster.seqs,
        via_llm=False,
    )


# ⚠️ OAUTH-SAFE: el OAuth de suscripción RECHAZA system prompts personalizados con
# un falso "429 rate_limit_error" (mensaje "Error", sin retry-after). Verificado
# 2026-06-20: mismo patrón que agent.py — las instrucciones van en el USER message,
# NO en `system` (que queda vacío / solo identidad Claude Code). En modo API key sí
# se podría usar system, pero por seguridad usamos el mismo patrón en ambos.
_INSTRUCCION_CONCEPTO = (
    "Eres un consolidador de memoria. Abajo hay un grupo de mensajes parecidos de "
    "una conversación. Extrae EL CONCEPTO común y responde SOLO con JSON estricto, "
    "sin texto extra:\n"
    '{"label": "<nombre corto 2-5 palabras>", '
    '"descripcion": "<1 frase>", "tipo": "<una palabra: tema|actividad|saludo|otro>"}\n\n'
)


def _parse_concepto_json(texto: str, cluster: Cluster) -> Concepto | None:
    """Extrae el JSON del concepto de la respuesta del LLM. None si no es válido."""
    try:
        m = re.search(r"\{.*\}", texto, re.DOTALL)
        if not m:
            return None
        d = json.loads(m.group(0))
        label = str(d.get("label", "")).strip()
        if not label:
            return None
        return Concepto(
            label=label[:80],
            descripcion=str(d.get("descripcion", "")).strip()[:300],
            tipo=str(d.get("tipo", "otro")).strip()[:30] or "otro",
            seqs=cluster.seqs,
            via_llm=True,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def extraer_concepto(cluster: Cluster, provider=None) -> Concepto:
    """Destila el concepto de un cluster usando el LLM (sonnet-4-6 por defecto),
    con SUMMARY acotado (privacidad) y FALLBACK heurístico si el LLM falla.

    provider: un ClaudeProvider ya construido. Si es None, lo crea desde settings
    con CLS_MODEL. complete() es síncrono → se corre en un thread (no bloquea).
    """
    try:
        if provider is None:
            from for3s_core.config import load_settings
            from for3s_core.llm import ClaudeProvider

            s = load_settings()
            provider = ClaudeProvider(
                token=s.anthropic_token,
                oauth=s.is_oauth,
                model=CLS_MODEL,
            )
        summary = _construir_summary(cluster)
        # OAUTH-SAFE: instrucción + summary van juntos en el USER message; system="".
        prompt = _INSTRUCCION_CONCEPTO + summary
        resp = await asyncio.to_thread(
            provider.complete,
            prompt,
            system="",
            max_tokens=150,
        )
        concepto = _parse_concepto_json(resp.text, cluster)
        if concepto is not None:
            logger.info("[cls] concepto (LLM): %r (%d ep)", concepto.label, cluster.tam)
            return concepto
        logger.warning("[cls] LLM devolvió JSON inválido → fallback heurístico")
    except Exception as e:  # noqa: BLE001 — el LLM nunca rompe CLS
        logger.warning("[cls] LLM falló (%s) → fallback heurístico", type(e).__name__)
    return _fallback_heuristico(cluster)


# ===========================================================================
# Sub-paso 6 — ESCRIBIR el concepto al Knowledge Graph (reusa kg.py)
# ===========================================================================


async def escribir_concepto(pool, concepto: Concepto, session_id: str = "") -> bool:
    """Escribe un Concepto en el grafo (nodo + aristas DERIVED_FROM a sus episodios
    fuente), reusando kg.registrar_concepto. Idempotente (MERGE). Defensivo.

    BUG-19 (2026-07-01): pasa session_id → los nodos Episodio se identifican por
    (seq, session_id), sin colisionar entre sesiones (los seq se solapan entre
    usuarios). Imprescindible con el CLS multi-sesión (BUG-18).

    NO marca consolidated_to_kg (eso lo hace el orquestador del Sub-paso 7 cuando
    TODO el pipeline del cluster completó). Aquí solo escribe al grafo.
    """
    from for3s_core import kg

    return await kg.registrar_concepto(
        pool,
        concepto.label,
        concepto.descripcion,
        concepto.tipo,
        concepto.seqs,
        session_id=session_id,
    )


# ===========================================================================
# Sub-paso 7 — ORQUESTADOR (une clustering→concepto→grafo→flag, con anti-429)
# ===========================================================================

# Pausa entre clusters para NO mandar llamadas LLM en ráfaga (anti-429). CLS corre
# de noche y pocas veces → unos segundos por cluster es invisible y muy seguro.
PAUSA_ENTRE_CLUSTERS_SEG = 3.0
MAX_CONCEPTOS_POR_RUN = 20  # tope duro de clusters procesados por corrida


@dataclass(frozen=True)
class ResultadoCLS:
    """Resumen de una corrida del orquestador CLS."""

    dry_run: bool
    total_pendientes: int
    salto: bool  # True si <umbral pendientes
    clusters: int  # clusters formados
    conceptos_escritos: int  # conceptos que llegaron al grafo (0 en dry-run)
    episodios_marcados: int  # episodios consolidated_to_kg=true (0 en dry-run)
    via_llm: int  # cuántos conceptos los hizo el LLM (vs fallback)
    detalle: list[dict]  # por cluster: {label, tipo, n_ep, via_llm, escrito}


async def consolidar(
    pool,
    session_id: str,
    *,
    dry_run: bool = True,
    pausa_seg: float = PAUSA_ENTRE_CLUSTERS_SEG,
    max_conceptos: int = MAX_CONCEPTOS_POR_RUN,
    provider=None,
) -> ResultadoCLS:
    """Orquesta el ciclo CLS completo (el "sleep cycle"): clustering → por cada
    cluster extrae concepto (LLM espaciado) → lo escribe al grafo → marca los
    episodios como consolidados. Registra un meta-evento en el audit chain.

    dry_run=True (default): NO escribe al grafo NI marca flags — solo muestra qué
        haría. Para revisar antes de que sea real.
    dry_run=False: escribe y marca de verdad.

    ANTI-429: reusa UN provider (un bucket) + pausa entre clusters + el fallback
    heurístico de extraer_concepto cubre cualquier 429 puntual (no rompe la corrida).

    ⚠️ ORDEN SEGURO: el flag consolidated_to_kg=true se marca SOLO si el concepto
    se escribió al grafo con éxito. Nunca se marca un episodio cuya lección no llegó
    al grafo (si no, la Microglía podría borrarlo sin respaldo).
    """
    from for3s_core import audit, memory

    res_clu = await clusterizar_pendientes(pool, session_id)
    if res_clu.salto:
        logger.info("[cls] corrida saltada (<umbral pendientes)")
        return ResultadoCLS(dry_run, res_clu.total_pendientes, True, 0, 0, 0, 0, [])

    # provider único para toda la corrida (un solo bucket de rate-limit)
    if provider is None and not dry_run:
        from for3s_core.config import load_settings
        from for3s_core.llm import ClaudeProvider

        s = load_settings()
        provider = ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=CLS_MODEL)
    # en dry-run igual extraemos conceptos (para mostrar), reusando provider si se pasó

    detalle: list[dict] = []
    conceptos_escritos = 0
    episodios_marcados = 0
    via_llm_n = 0

    clusters = res_clu.clusters[:max_conceptos]
    for i, cluster in enumerate(clusters):
        if i > 0 and pausa_seg > 0:
            await asyncio.sleep(pausa_seg)  # anti-ráfaga

        concepto = await extraer_concepto(cluster, provider=provider)
        if concepto.via_llm:
            via_llm_n += 1

        escrito = False
        marcados = 0
        if not dry_run:
            escrito = await escribir_concepto(pool, concepto, session_id=session_id)
            if escrito:
                conceptos_escritos += 1
                # SOLO marcar si el concepto llegó al grafo (orden seguro)
                marcados = await memory.marcar_consolidados(pool, session_id, concepto.seqs)
                episodios_marcados += marcados

        detalle.append(
            {
                "label": concepto.label,
                "tipo": concepto.tipo,
                "n_ep": cluster.tam,
                "via_llm": concepto.via_llm,
                "escrito": escrito if not dry_run else None,
            }
        )
        logger.info(
            "[cls] cluster %d/%d: %r (%d ep, llm=%s, escrito=%s)",
            i + 1,
            len(clusters),
            concepto.label,
            cluster.tam,
            concepto.via_llm,
            escrito if not dry_run else "DRY",
        )

    # meta-evento en audit (trazabilidad) — también en dry-run, marcado como tal
    try:
        await audit.append(
            pool,
            actor="cls_orchestrator",
            action="cls_consolidation_dryrun" if dry_run else "cls_consolidation",
            detail={
                "session_id": session_id,
                "dry_run": dry_run,
                "total_pendientes": res_clu.total_pendientes,
                "clusters": len(clusters),
                "conceptos_escritos": conceptos_escritos,
                "episodios_marcados": episodios_marcados,
                "via_llm": via_llm_n,
            },
        )
    except Exception as e:  # noqa: BLE001 — el audit no debe tumbar la corrida
        logger.warning("[cls] audit append falló (no crítico): %s", type(e).__name__)

    return ResultadoCLS(
        dry_run,
        res_clu.total_pendientes,
        False,
        len(clusters),
        conceptos_escritos,
        episodios_marcados,
        via_llm_n,
        detalle,
    )
