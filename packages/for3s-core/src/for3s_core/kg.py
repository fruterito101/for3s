"""Knowledge Graph de For3s OS (H5 sub-paso 7, 2026-06-19) — Nodo 1 KG.

Capa LIMPIA sobre Apache AGE para registrar y navegar el conocimiento que For3s
acumula: qué repos/owners/issues/PRs ha tocado y cómo se relacionan. Permite
preguntas multi-hop ("¿qué issues tiene lo que el owner X mantiene?").

Esquema simple (poblado desde datos que YA tenemos en gh_resources, SIN LLM):
    (Owner {nombre})  -[:DUENO_DE]->  (Repo {nombre})
    (Repo)            -[:TIENE]->     (Issue {numero, titulo})
    (Repo)            -[:TIENE]->     (PullRequest {numero, titulo})

El auto-llenado inteligente (consolidación de episodios→conceptos vía LLM) es H6
(CLS). Esto es el poblado SIMPLE de entidades obvias para que el grafo sirva en H5.

REGLAS DE AGE respetadas (ver Doc/H5_Infra_Memoria_AGE_pgvector.md):
  • Se usa vía las funciones SQL cypher_write / cypher_read_json (NO cypher() directo).
  • RETURN de propiedad SIEMPRE con alias. NADA de palabras reservadas.
  • NUNCA SET search_path en la conexión (las funciones lo hacen).
  • MERGE (no CREATE) → idempotente: re-registrar no duplica.
  • Sanitización: los valores de texto se escapan (comillas simples) y se valida
    que NO contengan la secuencia $cy$ (el dollar-quote de las funciones wrapper).

DEFENSIVO: cualquier error se traga (el grafo es secundario; no debe tumbar el
guardado normal del turno). Devuelve éxito/fallo silencioso.
"""

from __future__ import annotations

import json
import logging

import asyncpg

logger = logging.getLogger("for3s.kg")

GRAFO = "for3s_kg"


def _esc(valor: str) -> str:
    """Escapa un string para meterlo en una query Cypher: comillas simples
    duplicadas + quita la secuencia $cy$ (que rompería el dollar-quote del wrapper)."""
    s = (valor or "").replace("$cy$", "").replace("'", "''")
    return s[:500]  # tope defensivo de longitud


async def _write(pool: asyncpg.Pool, cypher: str) -> bool:
    """Ejecuta un Cypher de escritura vía la función wrapper. Defensivo."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT cypher_write($1, $2)", GRAFO, cypher)
        return True
    except Exception as exc:  # noqa: BLE001 — grafo secundario, no rompe el turno
        logger.warning("kg write falló (no crítico): %s", exc)
        return False


async def _read(pool: asyncpg.Pool, cypher: str) -> list:
    """Ejecuta un Cypher de lectura vía la función wrapper (→ json). Defensivo."""
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval("SELECT cypher_read_json($1, $2)", GRAFO, cypher)
        return json.loads(raw) if raw else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("kg read falló (no crítico): %s", exc)
        return []


async def registrar_repo(pool: asyncpg.Pool, owner: str, repo: str) -> bool:
    """Registra un repo y su owner en el grafo (idempotente vía MERGE).
    Crea: (Owner)-[:DUENO_DE]->(Repo). Re-registrar no duplica."""
    if not owner or not repo:
        return False
    o, r = _esc(owner), _esc(repo)
    cypher = (
        f"MERGE (ow:Owner {{nombre:'{o}'}}) "
        f"MERGE (rp:Repo {{nombre:'{o}/{r}'}}) "
        f"MERGE (ow)-[:DUENO_DE]->(rp)"
    )
    return await _write(pool, cypher)


async def registrar_recurso(
    pool: asyncpg.Pool,
    owner: str,
    repo: str,
    kind: str,
    numero: int | None = None,
    titulo: str = "",
) -> bool:
    """Registra un issue/PR de un repo en el grafo (idempotente).
    kind: 'issue' | 'pr'. Crea (Repo)-[:TIENE]->(Issue/PullRequest)."""
    if not owner or not repo or kind not in ("issue", "pr"):
        return False
    await registrar_repo(pool, owner, repo)  # asegura el repo primero
    o, r = _esc(owner), _esc(repo)
    label = "Issue" if kind == "issue" else "PullRequest"
    t = _esc(titulo)
    if numero is None:
        return False
    # MERGE por (repo, numero) → identidad única del recurso dentro del repo
    cypher = (
        f"MATCH (rp:Repo {{nombre:'{o}/{r}'}}) "
        f"MERGE (x:{label} {{repo:'{o}/{r}', numero:{int(numero)}}}) "
        f"SET x.titulo = '{t}' "
        f"MERGE (rp)-[:TIENE]->(x)"
    )
    return await _write(pool, cypher)


async def repos_de_owner(pool: asyncpg.Pool, owner: str) -> list[str]:
    """Navega: qué repos mantiene un owner (1 hop). Devuelve el nodo Repo (1 columna,
    regla AGE: RETURN multi-columna rompe la función wrapper → devolvemos 1 valor)."""
    o = _esc(owner)
    filas = await _read(
        pool,
        f"MATCH (:Owner {{nombre:'{o}'}})-[:DUENO_DE]->(rp:Repo) RETURN rp.nombre AS r",
    )
    # cada fila es el valor escalar del RETURN (string del nombre)
    return [f for f in filas if isinstance(f, str)]


async def recursos_de_repo(pool: asyncpg.Pool, owner: str, repo: str) -> list[dict]:
    """Navega: issues/PRs de un repo (1 hop). RETURN un MAPA (1 columna) — AGE no
    soporta RETURN multi-columna vía la función wrapper json."""
    o, r = _esc(owner), _esc(repo)
    filas = await _read(
        pool,
        f"MATCH (:Repo {{nombre:'{o}/{r}'}})-[:TIENE]->(x) "
        f"RETURN {{tipo: labels(x)[0], numero: x.numero, titulo: x.titulo}} AS r",
    )
    return [f for f in filas if isinstance(f, dict)]


async def stats(pool: asyncpg.Pool) -> dict:
    """Conteo de nodos por tipo. count(*) NO puede ir dentro de un mapa en el RETURN
    (AGE exige que lo no-agregado esté en GROUP BY); usamos WITH para agregar primero
    y luego construir el mapa de 1 columna."""
    filas = await _read(
        pool,
        "MATCH (n) WITH labels(n)[0] AS tipo, count(*) AS c RETURN {tipo: tipo, n: c} AS r",
    )
    return {f.get("tipo"): f.get("n") for f in filas if isinstance(f, dict)}


# ===========================================================================
# H6 CLS — conceptos consolidados (escritos por consolidator.py, Sub-paso 6)
# ===========================================================================


async def registrar_concepto(
    pool: asyncpg.Pool,
    label: str,
    descripcion: str,
    tipo: str,
    seqs: list[int],
) -> bool:
    """Escribe un CONCEPTO consolidado en el grafo (idempotente vía MERGE).

    Crea: (Concepto {label, descripcion, tipo}) y, por cada episodio fuente,
    (Concepto)-[:DERIVED_FROM]->(Episodio {seq}). Re-registrar el mismo concepto
    NO duplica (MERGE por label). Respeta las 4 reglas de AGE (ver cabecera).

    Lo usa CLS (consolidator.py) tras extraer el concepto de un cluster. Defensivo.
    """
    if not label:
        return False
    lab, desc, tp = _esc(label), _esc(descripcion), _esc(tipo)
    # 1) MERGE del nodo concepto (identidad = label) + actualizar props
    cypher_nodo = (
        f"MERGE (c:Concepto {{label:'{lab}'}}) SET c.descripcion = '{desc}', c.tipo = '{tp}'"
    )
    if not await _write(pool, cypher_nodo):
        return False
    # 2) una arista DERIVED_FROM por cada episodio fuente (MERGE → idempotente)
    ok = True
    for seq in seqs:
        try:
            n = int(seq)
        except (ValueError, TypeError):
            continue
        cypher_arista = (
            f"MERGE (c:Concepto {{label:'{lab}'}}) "
            f"MERGE (e:Episodio {{seq:{n}}}) "
            f"MERGE (c)-[:DERIVED_FROM]->(e)"
        )
        ok = await _write(pool, cypher_arista) and ok
    return ok


async def episodios_de_concepto(pool: asyncpg.Pool, label: str) -> list[int]:
    """Navega: de qué episodios se derivó un concepto (1 hop). RETURN un MAPA (1
    columna) — AGE no castea un integer escalar a json (regla extra: envolver el
    entero en un mapa, como recursos_de_repo)."""
    lab = _esc(label)
    filas = await _read(
        pool,
        f"MATCH (:Concepto {{label:'{lab}'}})-[:DERIVED_FROM]->(e:Episodio) "
        f"RETURN {{seq: e.seq}} AS r",
    )
    return [int(f["seq"]) for f in filas if isinstance(f, dict) and "seq" in f]


async def conceptos(pool: asyncpg.Pool) -> list[dict]:
    """Lista los conceptos consolidados (label + tipo). RETURN un MAPA (1 columna)."""
    filas = await _read(
        pool,
        "MATCH (c:Concepto) RETURN {label: c.label, tipo: c.tipo, desc: c.descripcion} AS r",
    )
    return [f for f in filas if isinstance(f, dict)]
