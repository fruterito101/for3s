"""CLI de For3s OS (H1) — la terminal donde hablas con el agente.

H1.6 CLI loop con rich. DEMO de H1: escribes un mensaje y For3s responde
con Claude, mostrando tokens y costo estimado.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel

from for3s_core.agent import Agent
from for3s_core.config import load_settings
from for3s_core.llm import ClaudeProvider

console = Console()


def build_agent() -> tuple[Agent, str]:
    settings = load_settings()
    provider = ClaudeProvider(
        token=settings.anthropic_token,
        oauth=settings.is_oauth,
        model=settings.model,
    )
    return Agent(provider), f"{settings.model} · auth={settings.auth_mode}"


def run() -> int:
    try:
        agent, info = build_agent()
    except RuntimeError as exc:
        console.print(f"[red]Error de configuración:[/red] {exc}")
        return 1

    console.print(Panel.fit(f"[bold]For3s OS[/bold] — H1 HABLA\n{info}", border_style="cyan"))
    console.print("[dim]Escribe tu mensaje. 'salir' para terminar.[/dim]\n")

    while True:
        try:
            msg = console.input("[bold cyan]tú ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]hasta luego.[/dim]")
            return 0
        if msg.lower() in ("salir", "exit", "quit"):
            console.print("[dim]hasta luego.[/dim]")
            return 0
        if not msg:
            continue

        with console.status("[cyan]For3s pensando...[/cyan]"):
            resp = agent.ask(msg)

        console.print(Panel(resp.text, title="For3s", border_style="green"))
        console.print(
            f"[dim]tokens: {resp.input_tokens} in / {resp.output_tokens} out · "
            f"costo ref: ${resp.cost_usd:.5f} "
            f"(en suscripción OAuth no se cobra por token)[/dim]\n"
        )


if __name__ == "__main__":
    sys.exit(run())
