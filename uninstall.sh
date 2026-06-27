#!/bin/sh
# For3s OS — desinstalación limpia (C8). Borra TODO de un golpe.
#   cd ~/for3s-os && ./uninstall.sh
set -eu

KEK_DIR="$HOME/.for3s"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"

printf '\033[1m%s\033[0m\n' "For3s OS — desinstalación"
printf '\033[33m%s\033[0m\n' "⚠️  Esto borra TODO de forma IRREVERSIBLE:"
echo "  • los contenedores (agente, worker, postgres, valkey)"
echo "  • TODOS tus datos: memoria, skills, conversaciones, perfil"
echo "  • la configuración y la KEK local (~/.for3s)"
echo
printf "¿Seguro? escribe 'borrar todo': "
read -r RESP
[ "$RESP" = "borrar todo" ] || { echo "Cancelado."; exit 1; }

echo "==> Bajando contenedores + volúmenes (datos)..."
$DOCKER compose down -v 2>/dev/null || true

# borrar la KEK + config del host (viven FUERA de Docker)
echo "==> Borrando configuración y KEK local ($KEK_DIR)..."
rm -rf "$KEK_DIR"
rm -f .env 2>/dev/null || true

# opcional: limpiar imágenes para liberar disco
printf "¿Borrar también las imágenes de Docker (libera ~10GB)? (s/N): "
read -r IMG
if [ "$IMG" = "s" ]; then
    $DOCKER rmi for3s-agent:local for3s-postgres:local 2>/dev/null || true
    echo "    imágenes borradas."
fi

printf '\033[32m%s\033[0m\n' "✅ For3s OS desinstalado. Tu máquina quedó como antes."
echo   "   (Docker sigue instalado; si no lo quieres: sudo apt-get remove docker-ce)"
