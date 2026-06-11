"""Tests de INTEGRACIÓN de H2 — contra Postgres real.

Requieren DATABASE_URL (servidor o el Postgres de servicio del CI). Si no
está, se saltan (skip). Validan lo que los tests de lógica pura no cubren:
la capa de BD real, el audit chain end-to-end, y la inmutabilidad (trigger).
"""

from __future__ import annotations

import os
import uuid

import pytest
from for3s_core import audit, db, memory

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requiere DATABASE_URL (Postgres)"
)


@pytest.fixture
async def pool():
    p = await db.connect(os.environ["DATABASE_URL"])
    await db.apply_migrations(p)
    yield p
    await p.close()


async def test_memoria_persiste_y_se_recupera(pool) -> None:
    sid = f"test-{uuid.uuid4().hex[:8]}"
    await memory.ensure_session(pool, sid)
    await memory.record_turn(pool, sid, role="user", content="hola")
    await memory.record_turn(pool, sid, role="assistant", content="qué tal")
    hist = await memory.load_history(pool, sid)
    assert [t.role for t in hist] == ["user", "assistant"]
    assert hist[0].content == "hola"


async def test_episodes_seq_incrementa(pool) -> None:
    sid = f"test-{uuid.uuid4().hex[:8]}"
    await memory.ensure_session(pool, sid)
    s1 = await memory.record_turn(pool, sid, role="user", content="a")
    s2 = await memory.record_turn(pool, sid, role="assistant", content="b")
    assert s2 == s1 + 1


async def test_audit_chain_integra(pool) -> None:
    await audit.append(pool, actor="user", action="test_in", detail={"x": 1})
    await audit.append(pool, actor="for3s", action="test_out", detail={"y": 2})
    ok, count = await audit.verify_chain(pool)
    assert ok is True
    assert count >= 2


async def test_audit_inmutable_no_delete(pool) -> None:
    await audit.append(pool, actor="user", action="immutable_test", detail={})
    with pytest.raises(asyncpg_error()):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_events WHERE id = (SELECT MIN(id) FROM audit_events)"
            )


def asyncpg_error():
    import asyncpg

    return asyncpg.PostgresError
