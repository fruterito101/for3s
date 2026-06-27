"""For3s OS — cálculo de relevancia y decay de memoria (H6 "SE CUIDA", Sub-paso 3).

La Microglía (olvido) necesita saber qué episodios son POCO relevantes para poder
olvidarlos. Este módulo define ese criterio: una `relevance` en [0,1] que decae con
el desuso y se refuerza con el uso, inspirada en cómo la memoria humana retiene lo
que se recuerda seguido y desvanece lo que no se toca.

⚠️ FÓRMULA v1 PROVISIONAL Y CONSERVADORA (2026-06-20).
   Es deliberadamente TÍMIDA: vida media de 90 días → casi nada baja rápido. Es más
   seguro olvidar de menos que de más. Está parametrizada para endurecerse luego.
   🔖 PENDIENTE (el dueño): definir la fórmula de relevancia AFINADA más adelante. Esta
      v1 es un placeholder funcional para que la Microglía tenga un criterio seguro.

Dos piezas (ver memory.py para la integración):
  · refrescar last_accessed cuando un recuerdo se recupera (en caliente, background)
  · recalcular relevance por lote (en frío, dentro del job nocturno — Sub-paso 10)

Este módulo NO borra nada y NO se conecta al cron todavía (eso es el Sub-paso 10).
"""

from __future__ import annotations

import asyncpg

# --- Parámetros de la fórmula (ajustables) ---------------------------------
VIDA_MEDIA_DIAS = 90.0   # a los 90 días sin usar, el decay temporal llega a 0.5
REFUERZO_POR_USO = 0.1   # +10% de relevancia por cada recuperación (tope abajo)
REFUERZO_USO_TOPE = 5    # como máximo cuenta 5 usos (→ refuerzo máx +50%)
PISO_RELEVANCIA = 0.15   # piso de seguridad v1: no bajar de aquí salvo muy viejos
DIAS_MUY_VIEJO = 180.0   # umbral para permitir caer por debajo del piso


def calcular_relevance(
    dias_sin_usar: float,
    veces_recuperado: int = 0,
) -> float:
    """Devuelve la relevancia en [0,1] de un episodio.

    dias_sin_usar: días desde la última vez que se usó (last_accessed), o desde
        que se creó (created_at) si nunca se recuperó.
    veces_recuperado: cuántas veces se ha recuperado como recuerdo. v1: si no se
        cuenta aún, pasar 0 → refuerzo neutro (factor 1.0).

    Conservadora a propósito (ver módulo). Recorta a [0,1] con piso de seguridad.
    """
    dias = max(0.0, float(dias_sin_usar))

    # decay temporal: 0.5 ^ (dias / vida_media) → 1.0 hoy, 0.5 a los 90d, 0.25 a 180d
    decay_temporal = 0.5 ** (dias / VIDA_MEDIA_DIAS)

    # refuerzo por uso: lo muy usado resiste el olvido (hasta +50%)
    usos = max(0, min(int(veces_recuperado), REFUERZO_USO_TOPE))
    refuerzo = 1.0 + REFUERZO_POR_USO * usos

    rel = decay_temporal * refuerzo

    # piso de seguridad v1: salvo episodios MUY viejos (>180d) y sin tocar, no caer
    # por debajo del piso (extra cautela conservadora).
    if dias < DIAS_MUY_VIEJO:
        rel = max(rel, PISO_RELEVANCIA)

    return max(0.0, min(1.0, rel))


# --- Recálculo por lote (en frío) ------------------------------------------
# `días_sin_usar` en SQL: EXTRACT(EPOCH FROM (now() - COALESCE(last_accessed,
# created_at))) / 86400. El UPDATE espeja calcular_relevance(): decay (vida media 90d
# + piso) × refuerzo_uso. relevance v2 (2026-06-22): el refuerzo YA NO es neutro
# — usa veces_recuperado REAL (1 + 0.1×min(usos,5), tope +50%). Lo recuperado resiste.
_SQL_RECALCULAR = """
WITH calc AS (
    SELECT id,
        EXTRACT(EPOCH FROM (now() - COALESCE(last_accessed, created_at))) / 86400.0
            AS dias,
        (1.0 + $5 * LEAST(veces_recuperado, $6)) AS refuerzo
    FROM episodes_events
    WHERE session_id = $1 AND deleted_at IS NULL
)
UPDATE episodes_events e
SET relevance = GREATEST(0.0, LEAST(1.0,
    calc.refuerzo * (
        CASE WHEN calc.dias < $2
             THEN GREATEST($3, power(0.5, calc.dias / $4))
             ELSE power(0.5, calc.dias / $4)
        END)))
FROM calc
WHERE e.id = calc.id
"""


async def recalcular_relevance_lote(
    pool: asyncpg.Pool, session_id: str,
) -> int:
    """Recalcula `relevance` de todos los episodios VIVOS de la sesión (en frío).

    Pensado para el job nocturno (Sub-paso 10). NO borra nada, solo actualiza la
    columna relevance. Usa veces_recuperado real (refuerzo por uso). Devuelve cuántas
    filas tocó. Defensiva en el caller.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            _SQL_RECALCULAR, session_id, DIAS_MUY_VIEJO, PISO_RELEVANCIA, VIDA_MEDIA_DIAS,
            REFUERZO_POR_USO, REFUERZO_USO_TOPE,
        )
    # result tipo "UPDATE N" → extraer N
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
