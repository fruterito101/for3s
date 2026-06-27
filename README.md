# For3s OS

**Un agente-cerebro self-hosted con memoria persistente.** Tool use nativo,
trabajo en equipo multi-agente y despliegue en un comando.

For3s OS no es un chatbot sin estado: es un segundo cerebro que recuerda, se
organiza solo, aprende recetas reutilizables y sabe cuándo no sabe.

## Qué hace

- 🧠 **Memoria real** — recuerda por significado (no solo los últimos mensajes) y
  organiza su conocimiento en un grafo que consolida solo, de noche.
- 🤝 **Equipo multi-agente** — para tareas complejas coordina varios especialistas
  en paralelo y sintetiza un informe único.
- 🎓 **Aprende skills** — destila recetas reutilizables de lo que trabajas y las
  aplica después, gobernadas por un freno de seguridad.
- 🌙 **Trabaja solo cuando estás inactivo** — se mantiene y se mejora en segundo plano.
- 🧭 **Sabe cuándo no sabe** — mide su confianza y, si duda, lo dice en vez de inventar.
- 🔌 **Integraciones** — lee y escribe en GitHub, lee la web, procesa imágenes/PDF/Word/Excel.
- 🔒 **Seguro por diseño** — secretos cifrados, auditoría inmutable, todo self-hosted.

## Instalación

> Requisitos: Linux (Ubuntu/Debian) + tus propias API keys (Claude y Telegram).

```bash
curl -fsSL https://install.for3s.dev | sh
```

El instalador deja tu máquina lista de cero: instala lo necesario, te pide el nombre
de tu For3s y tus keys, y levanta todo en contenedores. Al terminar, le escribes por
Telegram.

## Y luego, ¿qué?

1. Abre Telegram y escríbele a tu bot: `/start`.
2. Conversa, pídele analizar un repo, deja que aprenda una skill con `/aprende`.
3. Explora los comandos con el menú `/`.

## Para testers

¿Vienes a probar? Lee la guía: [TESTING.md](TESTING.md) — qué probar y cómo reportar.

## Desinstalar

```bash
cd ~/for3s-os && ./uninstall.sh
```

Borra los contenedores, los datos y la configuración local. Deja la máquina como antes.

## Licencia

For3s OS se distribuye bajo **GNU AGPL-3.0** (ver [LICENSE](LICENSE) y [NOTICE](NOTICE)).

- Puedes usar, modificar y redistribuir el software.
- Si lo ofreces **como servicio en red**, debes publicar tus modificaciones (AGPL).
- Para uso comercial **sin** las obligaciones de la AGPL, hay licencia comercial
  disponible — contacto: brayan002150@gmail.com

© 2026 Brian Jovany López Pérez — autor y único titular de los derechos.
