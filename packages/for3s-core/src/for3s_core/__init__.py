"""for3s-core — núcleo compartido de For3s OS (nace en C1)."""

__version__ = "0.0.1"


def heartbeat() -> str:
    """Señal de vida mínima del monorepo (la usa el smoke test de C1)."""
    return "for3s-os alive"
