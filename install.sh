#!/bin/sh
# For3s OS — instalador de una línea (Fase Pre-Testers, C3+C5).
#   curl -fsSL https://install.for3s.dev | sh
# Linux limpio (Ubuntu/Debian) → deja For3s OS corriendo. Todo en contenedores.
set -eu

REPO_URL="${FOR3S_REPO:-https://github.com/for3s-os/for3s.git}"
DEST="${FOR3S_DIR:-$HOME/for3s-os}"
KEK_DIR="$HOME/.for3s"

c_red()  { printf '\033[31m%s\033[0m\n' "$1"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$1"; }
c_yel()  { printf '\033[33m%s\033[0m\n' "$1"; }
c_bold() { printf '\033[1m%s\033[0m\n' "$1"; }

# ─────────────────────────── 1. AVISO DE RIESGO (C3.B) ───────────────────────────
c_bold "=============================================="
c_bold "        For3s OS — instalador"
c_bold "=============================================="
echo
c_yel "⚠️  AVISO IMPORTANTE — léelo antes de continuar:"
echo "  • Este script instala software en tu máquina (Docker + contenedores)."
echo "  • For3s gestiona contenedores en tu equipo; lo hacemos con la mayor"
echo "    seguridad posible, pero corre BAJO TU RESPONSABILIDAD."
echo "  • Necesita permisos de administrador (sudo) para instalar Docker."
echo "  • Tus API keys se guardan CIFRADAS en tu máquina; nunca salen de aquí."
echo
printf "¿Aceptas y deseas continuar? escribe 'acepto': "
read -r RESP
[ "$RESP" = "acepto" ] || { c_red "Instalación cancelada."; exit 1; }

# ─────────────────────────── 2. DETECTAR DISTRO (C5.B: Ubuntu/Debian v1) ───────────────────────────
if [ ! -f /etc/os-release ]; then
    c_red "No pude detectar tu distro (falta /etc/os-release)."; exit 1
fi
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) : ;;  # soportado
    *) c_yel "⚠️  v1 soporta Ubuntu/Debian. Tu distro ('${ID:-?}') aún no está probada."
       printf "¿Intentar de todas formas? (s/N): "; read -r G; [ "$G" = "s" ] || exit 1 ;;
esac

# ─────────────────────────── 3. INSTALAR DOCKER si falta (C5.A) ───────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    c_grn "==> Docker no está. Instalándolo (requiere sudo)..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    c_yel "    (si Docker pide re-loguear por el grupo, este script seguirá con sudo)"
fi
# asegurar que el daemon corre
sudo systemctl enable --now docker 2>/dev/null || true
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# ─────────────────────────── 4. DESCARGAR For3s OS ───────────────────────────
if [ -d "$DEST/.git" ]; then
    c_grn "==> Actualizando For3s OS en $DEST..."
    git -C "$DEST" pull --ff-only
else
    c_grn "==> Descargando For3s OS en $DEST..."
    command -v git >/dev/null 2>&1 || sudo apt-get install -y git
    git clone --depth 1 "$REPO_URL" "$DEST"
fi
cd "$DEST"

# ─────────────────────────── 5. WIZARD de configuración (C3.C) ───────────────────────────
c_bold ""
c_bold "── Configura tu For3s OS ──"
printf "Nombre de tu For3s (ej. Nova): "; read -r NOMBRE
NOMBRE="${NOMBRE:-For3s}"
printf "Tu API key de Claude (obligatoria): "; read -r CLAUDE_KEY
[ -n "$CLAUDE_KEY" ] || { c_red "La key de Claude es obligatoria."; exit 1; }
printf "Tu token de bot de Telegram (obligatorio): "; read -r TG_TOKEN
[ -n "$TG_TOKEN" ] || { c_red "El token de Telegram es obligatorio."; exit 1; }
printf "PAT de GitHub (opcional — Enter para saltar): "; read -r GH_PAT

# ─────────────────────────── 6. KEK + .env (C3.A — la KEK la genera el sistema) ───────────────────────────
mkdir -p "$KEK_DIR"; chmod 700 "$KEK_DIR"
# crypto.load_or_create_master_key crea ~/.for3s/master.key solo si no existe (32 bytes, 600).
# Aquí solo garantizamos la carpeta; el agente la genera al primer arranque.
POSTGRES_PASSWORD="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
cat > "$DEST/.env" <<EOF
# For3s OS — generado por el instalador. NO lo subas a ningún lado.
FOR3S_AGENT_NAME=$NOMBRE
ANTHROPIC_TOKEN=$CLAUDE_KEY
TELEGRAM_BOT_TOKEN=$TG_TOKEN
GITHUB_PAT=$GH_PAT
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
FOR3S_MODEL=claude-sonnet-4-6
EOF
chmod 600 "$DEST/.env"

# ─────────────────────────── 7. LEVANTAR (orden lo maneja el compose: C4.C) ───────────────────────────
c_grn "==> Construyendo y levantando For3s OS (la 1ª vez tarda: imagen grande)..."
$DOCKER compose up -d --build

# ─────────────────────────── 8. LISTO ───────────────────────────
c_bold ""
c_grn "✅ Listo. Tu For3s '$NOMBRE' está corriendo."
echo   "   • Escríbele en Telegram a tu bot y manda: /start"
echo   "   • Ver estado:   cd $DEST && $DOCKER compose ps"
echo   "   • Ver logs:     cd $DEST && $DOCKER compose logs -f agent"
echo   "   • Desinstalar:  cd $DEST && ./uninstall.sh"
