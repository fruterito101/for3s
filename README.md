# For3s OS

Agente-cerebro multi-tenant, self-hosted. 11 nodos cerebrales + 3 pilares
(Seguridad E2E · Escalabilidad · Autonomía Generativa gobernada).

> Monorepo creado en C1 (Mapa de Construcción Incremental).
> `apps/` = ejecutables · `packages/` = módulos compartidos (los nodos).

## Dev

```bash
uv sync          # entorno reproducible (Python 3.12)
uv run ruff check . && uv run ty check && uv run pytest -q
```

— Brian López · For3s
