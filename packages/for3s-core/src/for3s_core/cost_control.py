# For3s OS — Copyright (c) 2026 Brian Jovany López Pérez. Licencia AGPL-3.0 (ver LICENSE).
"""For3s OS — Cost Control multi-agente (H8 S8, R5 B3 §5.3.4). LAS 7 CAPAS COMPLETAS.

El FRENO del equipo multi-agente: 5 agentes = 5× consumo → sin tope = runaway de
costo/cuota. Regla de oro H8: el freno ANTES de soltar el motor.

el dueño (2026-06-23): construir LAS 7 CAPAS completas para tener la base lista para
ESCALAR. Las que aplican hoy (single-user, OAuth suscripción = tarifa plana) quedan
ACTIVAS (protegen rate-limit/cuota/abuso); las que dependen de pago-por-token o
multi-tenant (budget en $, mensual, por-workspace) quedan CONSTRUIDAS pero con su
switch en modo "no-cap" → al escalar (API key/clientes) solo se activan, no se
reconstruyen.

Las 7 capas (LOCKED):
  1. Pre-flight check      — ¿hay presupuesto antes de lanzar? si no, REJECT.
  2. Per-specialist budget — tope de tokens por specialist (ya en SpecialistDefinition).
  3. Real-time monitoring  — mide el gasto durante; warning 80% / emergency 95%.
  4. Circuit breaker       — corta la corrida si excede (vía broadcast del bus, S3).
  5. Partial results rescue— al cortar, rescata los specialists que SÍ terminaron.
  6. Client visibility     — reporta el costo (tokens/$) al usuario.
  7. Workspace isolation   — presupuesto namespaced por workspace (multi-tenant).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("for3s.cost")

# --- Ratios y topes (del diseño LOCKED §5.3.4) -----------------------------
SAFETY_BUFFER_RATIO = 1.3  # margen al estimar el costo de una corrida
WARNING_THRESHOLD_RATIO = 0.80  # 80% del cap → warning (LOCKED: 0.80)
EMERGENCY_ABORT_RATIO = 0.95  # 95% del cap → abort (LOCKED: 0.95)

# Tope por CORRIDA (lo que SÍ aplica hoy): máximo de llamadas LLM por análisis del
# equipo. Protege el rate-limit/abuso aunque la suscripción sea plana. 5 specialists
# + 1 synthesizer = 6 normal; el tope da margen y corta runaways.
MAX_LLAMADAS_POR_CORRIDA = 8

# Budget en TOKENS por corrida (capa 1/3): tope duro de tokens de toda la corrida.
MAX_TOKENS_POR_CORRIDA = 15_000

# --- Budget en $ / mensual / por-workspace (capa 7): PREPARADO pero INACTIVO ---
# Hoy con OAuth de suscripción (tarifa plana) no hay costo por token → sin cap en $.
# Al migrar a API key de pago / clientes, poner CAP_USD_MENSUAL > 0 por workspace.
CAP_USD_MENSUAL_DEFAULT = 0.0  # 0 = sin tope en $ (no aplica a suscripción)


@dataclass
class PresupuestoCorrida:
    """Estado de costo de UNA corrida del equipo (capa 1/2/3)."""

    workspace_id: str = "default"  # capa 7: namespaced (single-user = default)
    max_llamadas: int = MAX_LLAMADAS_POR_CORRIDA
    max_tokens: int = MAX_TOKENS_POR_CORRIDA
    cap_usd_mensual: float = CAP_USD_MENSUAL_DEFAULT
    # acumuladores (capa 3 real-time monitoring)
    llamadas: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    abortada: bool = False
    motivo_abort: str = ""

    # --- capa 1: pre-flight check ---
    def puede_lanzar(self, n_specialists: int) -> tuple[bool, str]:
        """¿Hay presupuesto para lanzar n specialists? (capa 1). Considera el safety
        buffer. Devuelve (ok, razón)."""
        proyectado = (self.llamadas + n_specialists) * SAFETY_BUFFER_RATIO
        if proyectado > self.max_llamadas:
            return False, f"excedería el tope de {self.max_llamadas} llamadas/corrida"
        return True, "ok"

    # --- capa 3: registrar gasto de un specialist + monitoring ---
    def registrar(self, tokens_in: int, tokens_out: int) -> str:
        """Suma el gasto de una llamada y devuelve el estado: 'ok'|'warning'|'emergency'
        (capa 3 real-time monitoring con los ratios LOCKED)."""
        self.llamadas += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        ratio = self.total_tokens / self.max_tokens if self.max_tokens else 0
        if ratio >= EMERGENCY_ABORT_RATIO:
            return "emergency"
        if ratio >= WARNING_THRESHOLD_RATIO:
            logger.warning(
                "[cost] corrida %s al %.0f%% del budget de tokens", self.workspace_id, ratio * 100
            )
            return "warning"
        return "ok"

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    # --- capa 6: client visibility (reporte legible) ---
    def reporte(self) -> str:
        """Resumen de costo para mostrar al usuario (capa 6 client visibility)."""
        return (
            f"💰 costo: {self.llamadas} llamadas · {self.total_tokens} tokens "
            f"(in {self.tokens_in} / out {self.tokens_out})"
            + (f" · ⛔ abortada: {self.motivo_abort}" if self.abortada else "")
        )


# --- capa 7: workspace isolation (preparado, single-user usa default) ---
# Acumulado mensual por workspace — para cuando haya multi-tenant + budget en $.
# Hoy es un dict en memoria (single-user); al escalar irá a BD namespaced.
_gasto_mensual: dict[str, float] = {}


def gasto_mensual(workspace_id: str = "default") -> float:
    """Gasto acumulado del mes para un workspace (capa 7). Hoy ~0 (suscripción plana)."""
    return _gasto_mensual.get(workspace_id, 0.0)


def hay_budget_mensual(
    workspace_id: str = "default",
    cap_usd: float = CAP_USD_MENSUAL_DEFAULT,
) -> bool:
    """¿Queda budget mensual en $? (capa 7). Si cap_usd=0 (suscripción) → siempre True
    (no hay tope en dinero). Al activar pago, pasar cap_usd>0."""
    if cap_usd <= 0:
        return True  # sin tope en $ (OAuth suscripción)
    return gasto_mensual(workspace_id) < cap_usd
