"""CLI de For3s OS — terminal con MEMORIA persistente (H2).

H2: la conversación se guarda en Postgres y se recupera al reiniciar.
Usa --session NOMBRE para retomar una sesión previa (default: "cli-default").
DEMO de H2: hablas, cierras, reabres con la misma sesión → For3s recuerda.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from for3s_core import db
from for3s_core.agent import Agent
from for3s_core.config import load_settings
from for3s_core.conversation import Conversation
from for3s_core.llm import ClaudeProvider

console = Console()


async def run(session_id: str) -> int:
    try:
        settings = load_settings()
    except RuntimeError as exc:
        console.print(f"[red]Error de configuración:[/red] {exc}")
        return 1
    if not settings.database_url:
        console.print("[red]Falta DATABASE_URL en .env (H2 necesita Postgres).[/red]")
        return 1

    pool = await db.connect(settings.database_url)
    await db.apply_schema(pool)

    provider = ClaudeProvider(
        token=settings.anthropic_token, oauth=settings.is_oauth, model=settings.model
    )
    convo = Conversation(pool, Agent(provider), session_id)

    info = f"{settings.model} · auth={settings.auth_mode} · sesión='{session_id}'"
    console.print(Panel.fit(f"[bold]For3s OS[/bold] — H2 RECUERDA\n{info}", border_style="cyan"))

    prior = await convo.history()
    if prior:
        console.print(f"[dim]🧠 memoria: {len(prior)} turnos recuperados de esta sesión.[/dim]")
    console.print("[dim]Escribe tu mensaje. 'salir' para terminar.[/dim]\n")

    try:
        while True:
            try:
                msg = console.input("[bold cyan]tú ›[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]hasta luego (la conversación quedó guardada).[/dim]")
                return 0
            if msg.lower() in ("salir", "exit", "quit"):
                console.print("[dim]hasta luego (la conversación quedó guardada).[/dim]")
                return 0
            if not msg:
                continue

            with console.status("[cyan]For3s pensando...[/cyan]"):
                resp = await convo.send(msg)

            console.print(Panel(resp.text, title="For3s", border_style="green"))
            console.print(
                f"[dim]tokens: {resp.input_tokens} in / {resp.output_tokens} out · "
                f"guardado en memoria + audit[/dim]\n"
            )
    finally:
        await pool.close()


def main() -> int:
    session_id = "cli-default"
    args = sys.argv[1:]
    if "--session" in args:
        i = args.index("--session")
        if i + 1 < len(args):
            session_id = args[i + 1]
    return asyncio.run(run(session_id))


if __name__ == "__main__":
    sys.exit(main())
