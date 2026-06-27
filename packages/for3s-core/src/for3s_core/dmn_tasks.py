"""For3s OS — DMN tasks HOUSEKEEPING (H9-b, 2026-06-25).

Las 5 tasks de mantenimiento del DMN (R5 §3). "Se mantiene solo": bajo riesgo,
auto-aplican, outcome medible directo. Se registran en el motor (dmn.py) al importar
este módulo. Separadas del motor para mantener dmn.py como orquestador puro.

Honestidad de ingeniería (Nota: "a detalle, mejores prácticas"):
  • REALES (tienen infra hoy): embedding_precompute · memory_consolidation ·
    cache_prewarming (con los contadores que añade H9-b).
  • STUBS HONESTOS (sin infra todavía — NO fingen trabajo, lo declaran en su outcome):
    routing_learning (no hay router multi-modelo: H7 enrutamiento bloqueado) ·
    eval_regression_detection (no hay golden set formal). Quedan listas para llenarse
    cuando exista la infra, sin tocar el motor.

Cada task: trigger_fn (¿vale correr?) → action_fn (trabajo) → DMNTaskResult (outcome).
TODAS defensivas: el motor ya captura excepciones, pero además degradan limpio.
"""

from __future__ import annotations

import logging

from for3s_core import dmn
from for3s_core.dmn import CLASE_HOUSEKEEPING, DMNTask, DMNTaskResult

logger = logging.getLogger("for3s.dmn.tasks")

# Cuántos items procesa una corrida (batch, no infinito — como el sueño consolida el día).
EMBED_BATCH = 200
CONSOLIDAR_UMBRAL = 20   # ≥20 episodios sin consolidar = vale correr el CLS (R5 §3.3)


# ───────────────────────── 1. embedding_precompute (LOW · $0) ─────────────────────────
async def trigger_embedding_precompute(pool, workspace: str) -> bool:
    """¿Hay turnos sin embedding? (cualquier pendiente vale — es $0 e idempotente)."""
    try:
        async with pool.acquire() as con:
            n = await con.fetchval(
                "SELECT count(*) FROM episodes_events "
                "WHERE embedding IS NULL AND deleted_at IS NULL")
        return (n or 0) > 0
    except Exception:  # noqa: BLE001
        return False


async def action_embedding_precompute(pool, workspace: str) -> DMNTaskResult:
    """Embebe los turnos pendientes (reusa memory.embeddear_turno). $0 (BGE-M3 local),
    idempotente, cero riesgo. Procesa en batch para no acaparar."""
    from for3s_core import memory
    creados = 0
    try:
        async with pool.acquire() as con:
            pend = await con.fetch(
                "SELECT session_id, seq, content FROM episodes_events "
                "WHERE embedding IS NULL AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT $1", EMBED_BATCH)
        for r in pend:
            ok = await memory.embeddear_turno(
                pool, r["session_id"], r["seq"], r["content"])
            if ok:
                creados += 1
    except Exception as e:  # noqa: BLE001
        return DMNTaskResult(outcome={"embeddings_created": creados},
                             motivo=f"parcial: {type(e).__name__}")
    return DMNTaskResult(outcome={"embeddings_created": creados}, costo_usd=0.0)


# ───────────────────────── 2. memory_consolidation (MEDIUM · ~$0.10) ─────────────────────────
async def trigger_memory_consolidation(pool, workspace: str) -> bool:
    """¿Hay ≥20 episodios sin consolidar al grafo? (batch eficiente, R5 §3.3)."""
    try:
        async with pool.acquire() as con:
            n = await con.fetchval(
                "SELECT count(*) FROM episodes_events "
                "WHERE consolidated_to_kg = false AND deleted_at IS NULL")
        return (n or 0) >= CONSOLIDAR_UMBRAL
    except Exception:  # noqa: BLE001
        return False


async def action_memory_consolidation(pool, workspace: str) -> DMNTaskResult:
    """REUSA el CLS de H6 (consolidator.consolidar) — NO reimplementa. Una sola lógica
    de consolidación, disparada tanto por el cron de H6 como por el idle del DMN.
    dry_run=False: escribe conceptos al grafo de verdad (con su anti-429 interno)."""
    from for3s_core import consolidator
    from for3s_core.tasks import SESSION_OWNER
    try:
        r = await consolidator.consolidar(pool, SESSION_OWNER, dry_run=False)
        return DMNTaskResult(
            outcome={"conceptos_escritos": r.conceptos_escritos,
                     "episodios_consolidados": r.episodios_marcados,
                     "clusters": r.clusters},
            costo_usd=0.10)
    except Exception as e:  # noqa: BLE001
        return DMNTaskResult(outcome={}, motivo=f"error CLS: {type(e).__name__}")


# ───────────────────────── 3. cache_prewarming (LOW · ~$0.15) ─────────────────────────
async def trigger_cache_prewarming(pool, workspace: str) -> bool:
    """¿Vale pre-calentar el cache? Necesita stats de hit/miss. H9-b añade contadores
    a cache.py; si aún no hay suficientes datos, el trigger dice NO (honesto).

    STUB HONESTO v1: hasta que los contadores acumulen señal (≥N misses recurrentes),
    no pre-calienta. No finge: devuelve False y la corrida queda registrada como
    'sin señal suficiente'."""
    try:
        from for3s_core import cache
        stats = getattr(cache, "stats_recientes", None)
        if stats is None:
            return False  # cache.py aún no expone stats → no corre (honesto)
        s = await stats(pool, workspace)
        return s.get("hit_rate", 1.0) < 0.5 and s.get("misses_recurrentes", 0) >= 3
    except Exception:  # noqa: BLE001
        return False


async def action_cache_prewarming(pool, workspace: str) -> DMNTaskResult:
    """Pre-calienta respuestas a patrones frecuentes que fallan. v1: declara que la
    medición de hit/miss es la pieza nueva; el pre-cómputo real de respuestas se
    activa cuando haya patrones identificados. Honesto: no inventa warming."""
    return DMNTaskResult(
        outcome={"patterns_warmed": 0, "estado": "pendiente_infra_stats"},
        motivo="cache_prewarming v1: requiere stats de hit/miss acumuladas")


# ───────────────────────── 4. routing_learning (STUB honesto) ─────────────────────────
async def trigger_routing_learning(pool, workspace: str) -> bool:
    """STUB HONESTO: For3s no tiene router multi-modelo activo (H7 enrutamiento
    BLOQUEADO por decisión). Sin rutas que aprender → nunca dispara. Se llenará
    cuando exista routing real. NO finge."""
    return False


async def action_routing_learning(pool, workspace: str) -> DMNTaskResult:
    """No-op honesto (no debería llamarse mientras el trigger sea False)."""
    return DMNTaskResult(outcome={"estado": "sin_router_multimodelo"},
                         motivo="routing_learning: H7 enrutamiento no activo")


# ───────────── 5. eval_regression_detection (v1 métrica simple) ─────────────
async def trigger_eval_regression(pool, workspace: str) -> bool:
    """¿Toca revisar si la calidad degradó? v1 sin golden set formal: corre 1×/día
    como guardián ligero (mira señales simples: tasa de errores recientes)."""
    try:
        async with pool.acquire() as con:
            ya_hoy = await con.fetchval(
                "SELECT count(*) FROM dmn_corridas WHERE task='eval_regression_detection' "
                "AND corrio=true AND creado_at >= date_trunc('day', now())")
        return (ya_hoy or 0) == 0  # solo una vez al día
    except Exception:  # noqa: BLE001
        return False


async def action_eval_regression(pool, workspace: str) -> DMNTaskResult:
    """GUARDIÁN ligero v1: sin golden set formal todavía, vigila una señal simple de
    salud — la proporción de turnos del assistant vacíos/error en las últimas 24h
    (proxy de degradación). Si sube de un umbral, lo marca para que el dueño lo vea.
    NO cambia nada (solo detecta). El golden set formal es deuda documentada."""
    try:
        async with pool.acquire() as con:
            total = await con.fetchval(
                "SELECT count(*) FROM episodes_events WHERE role='assistant' "
                "AND created_at >= now()-interval '24 hours' AND deleted_at IS NULL") or 0
            vacios = await con.fetchval(
                "SELECT count(*) FROM episodes_events WHERE role='assistant' "
                "AND created_at >= now()-interval '24 hours' AND deleted_at IS NULL "
                "AND (content IS NULL OR length(trim(content)) < 2)") or 0
        ratio = (vacios / total) if total else 0.0
        degradacion = ratio > 0.15  # >15% respuestas vacías = señal de alarma
        return DMNTaskResult(
            outcome={"respuestas_24h": total, "vacias": vacios,
                     "ratio_vacias": round(ratio, 3), "degradacion": degradacion},
            motivo="v1 métrica simple (golden set formal = deuda)")
    except Exception as e:  # noqa: BLE001
        return DMNTaskResult(outcome={}, motivo=f"error: {type(e).__name__}")


# ══════════════════════ GENERATIVAS (H9-c) ══════════════════════
# "Se mejora solo": ALTO riesgo. NUNCA auto-aplican — dejan propuestas en
# dmn_propuestas para que el dueño apruebe (/dmn propuestas), y solo corren si
# generativas_on está ON (default OFF). Provider LLM propio (corren en worker).
from for3s_core.dmn import CLASE_GENERATIVA  # noqa: E402

# Hipótesis usa el modelo más capaz (Opus) por ser razonamiento profundo (R5 §4.2).
HYP_MODEL = "claude-opus-4-8"
HYP_MAX_TOKENS = 900


def _provider_para(modelo: str):
    """Crea un provider LLM (OAuth-safe) para una task generativa. En worker, sin bot."""
    from for3s_core.config import load_settings
    from for3s_core.llm import ClaudeProvider
    s = load_settings()
    return ClaudeProvider(token=s.anthropic_token, oauth=s.is_oauth, model=modelo)


async def _guardar_propuesta(pool, *, task: str, tipo: str, titulo: str,
                             contenido: str, costo: float, workspace: str) -> int | None:
    """Deja una propuesta generativa para el dueño (estado=pendiente). Defensivo."""
    try:
        async with pool.acquire() as con:
            return await con.fetchval(
                "INSERT INTO dmn_propuestas (workspace, task, tipo, titulo, contenido, "
                " costo_usd) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
                workspace, task, tipo, titulo[:200], contenido, costo)
    except Exception:  # noqa: BLE001
        logger.warning("no pude guardar propuesta DMN de %s", task, exc_info=True)
        return None


# ── 6. pattern_detection (MEDIUM) — reusa el motor de skills de H12 ──
async def trigger_pattern_detection(pool, workspace: str) -> bool:
    """¿Hay suficiente material reciente para que valga buscar un patrón reutilizable?
    Reusa el umbral del motor de skills (si la auto-gen del governor está ON)."""
    try:
        from for3s_core.governor import SkillEcosystemGovernor
        if not await SkillEcosystemGovernor(pool).autogen_permitida():
            return False  # el governor de skills manda para pattern→skill
        async with pool.acquire() as con:
            n = await con.fetchval(
                "SELECT count(*) FROM episodes_events "
                "WHERE created_at >= now()-interval '24 hours' AND deleted_at IS NULL")
        return (n or 0) >= 10
    except Exception:  # noqa: BLE001
        return False


async def action_pattern_detection(pool, workspace: str) -> DMNTaskResult:
    """Detecta un patrón en el uso reciente → propone una SKILL. REUSA
    aprende.proponer_skill_auto (H12): destila → governor → nace en stale → gate del
    dueño. Cero duplicación: el motor de skills ya hace todo el camino seguro."""
    from for3s_core.aprende import proponer_skill_auto
    from for3s_core.tasks import SESSION_OWNER
    try:
        prov = _provider_para("claude-sonnet-4-6")
        res = await proponer_skill_auto(pool, prov, SESSION_OWNER, creada_por=None)
        return DMNTaskResult(
            outcome={"skill_propuesta": res.nombre or None, "ok": res.ok},
            costo_usd=0.05, motivo=res.mensaje)
    except Exception as e:  # noqa: BLE001
        return DMNTaskResult(outcome={}, motivo=f"error: {type(e).__name__}")


# ── 7. hypothesis_generation (HIGH · Opus) — propone hipótesis al dueño ──
_PROMPT_HYP = """\
Eres For3s OS reflexionando en segundo plano. Mirando lo que se ha trabajado, genera
1 HIPÓTESIS útil y accionable sobre el proyecto/código (ej. "este módulo tiende a
romper por X", "convendría revisar Y"). NO inventes: básate en lo que está abajo.
Responde SOLO JSON: {"vale": true|false, "titulo": "...", "hipotesis": "..."}.
Si no hay material para una hipótesis seria, {"vale": false}.

MATERIAL RECIENTE:
"""


async def trigger_hypothesis_generation(pool, workspace: str) -> bool:
    """Genera hipótesis como máximo 1×/día (Opus es caro). Solo si hay material."""
    try:
        async with pool.acquire() as con:
            ya = await con.fetchval(
                "SELECT count(*) FROM dmn_corridas WHERE task='hypothesis_generation' "
                "AND corrio=true AND creado_at >= date_trunc('day', now())")
            mat = await con.fetchval(
                "SELECT count(*) FROM episodes_events "
                "WHERE created_at >= now()-interval '48 hours' AND deleted_at IS NULL")
        return (ya or 0) == 0 and (mat or 0) >= 10
    except Exception:  # noqa: BLE001
        return False


async def action_hypothesis_generation(pool, workspace: str) -> DMNTaskResult:
    """Usa Opus para destilar 1 hipótesis del material reciente → la deja en
    dmn_propuestas (NO actúa). OAuth-safe (instrucción en user, system='')."""
    import asyncio
    import json

    from for3s_core import memory
    from for3s_core.tasks import SESSION_OWNER
    try:
        turnos = await memory.load_history(pool, SESSION_OWNER, last_n=20)
        if not turnos:
            return DMNTaskResult(outcome={"generadas": 0}, motivo="sin material")
        material = "\n".join(
            f"{'Usuario' if t.role == 'user' else 'For3s'}: {(t.content or '')[:200]}"
            for t in turnos)
        prov = _provider_para(HYP_MODEL)
        resp = await asyncio.to_thread(
            prov.complete, _PROMPT_HYP + material, system="", max_tokens=HYP_MAX_TOKENS)
        txt = (getattr(resp, "text", "") or "").strip()
        txt = txt.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            d = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            d = {}
        if not d.get("vale"):
            return DMNTaskResult(outcome={"generadas": 0}, costo_usd=0.50,
                                 motivo="el modelo no vio una hipótesis seria")
        pid = await _guardar_propuesta(
            pool, task="hypothesis_generation", tipo="hipotesis",
            titulo=d.get("titulo", "Hipótesis"), contenido=d.get("hipotesis", ""),
            costo=0.50, workspace=workspace)
        return DMNTaskResult(outcome={"generadas": 1, "propuesta_id": pid}, costo_usd=0.50)
    except Exception as e:  # noqa: BLE001
        return DMNTaskResult(outcome={}, motivo=f"error: {type(e).__name__}")


# ── 8. prompt_improvement (HIGH) — STUB honesto (cruza con AUTO-CONCIENCIA AC3) ──
async def trigger_prompt_improvement(pool, workspace: str) -> bool:
    """STUB HONESTO: auto-proponer cambios a la PROPIA personalidad/prompts del sistema
    es máximo cuidado y cruza con el pendiente AUTO-CONCIENCIA AC3 (debatir tipo Ronda).
    En v1 NO dispara. No finge."""
    return False


async def action_prompt_improvement(pool, workspace: str) -> DMNTaskResult:
    """No-op honesto. Cuando se construya (con AC3), propondrá mejoras de prompt a
    dmn_propuestas, NUNCA auto-editará FOR3S_ROLE."""
    return DMNTaskResult(outcome={"estado": "pendiente_AC3"},
                         motivo="prompt_improvement: requiere diseño AUTO-CONCIENCIA AC3")


# ── registro en el motor ──
# Pesadas (solo_noche=True): consolidation, eval, y TODAS las generativas (LLM/Opus).
# Ligeras de día: embedding, cache, routing.
def registrar_tasks() -> None:
    """Registra las 8 tasks (5 housekeeping + 3 generativas). Idempotente."""
    dmn.registrar(DMNTask("embedding_precompute", CLASE_HOUSEKEEPING,
                          trigger_embedding_precompute, action_embedding_precompute))
    dmn.registrar(DMNTask("cache_prewarming", CLASE_HOUSEKEEPING,
                          trigger_cache_prewarming, action_cache_prewarming))
    dmn.registrar(DMNTask("memory_consolidation", CLASE_HOUSEKEEPING,
                          trigger_memory_consolidation, action_memory_consolidation,
                          solo_noche=True))  # usa LLM → solo de noche
    dmn.registrar(DMNTask("routing_learning", CLASE_HOUSEKEEPING,
                          trigger_routing_learning, action_routing_learning))
    dmn.registrar(DMNTask("eval_regression_detection", CLASE_HOUSEKEEPING,
                          trigger_eval_regression, action_eval_regression,
                          solo_noche=True))
    # generativas — alto riesgo, solo de noche, requieren generativas_on
    dmn.registrar(DMNTask("pattern_detection", CLASE_GENERATIVA,
                          trigger_pattern_detection, action_pattern_detection,
                          solo_noche=True))
    dmn.registrar(DMNTask("hypothesis_generation", CLASE_GENERATIVA,
                          trigger_hypothesis_generation, action_hypothesis_generation,
                          solo_noche=True))
    dmn.registrar(DMNTask("prompt_improvement", CLASE_GENERATIVA,
                          trigger_prompt_improvement, action_prompt_improvement,
                          solo_noche=True))
    logger.info("[dmn.tasks] 8 tasks registradas (5 housekeeping + 3 generativas)")


registrar_tasks()
