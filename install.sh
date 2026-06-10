#!/bin/sh
# For3s OS — installer base (C1, versión mínima).
# Patrón one-line heredado de Hermes; crece en hitos posteriores.
# Uso: curl -fsSL <url>/install.sh | sh
set -eu

echo "==> For3s OS installer (base)"

if ! command -v uv >/dev/null 2>&1; then
    echo "==> instalando uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "==> uv listo. El installer completo (deps, wizard, keys) llega en hitos posteriores."