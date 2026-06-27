# Probar For3s OS — guía para testers

Gracias por probar For3s OS 🙏. Tu objetivo es ayudarnos a ver **lo que nosotros no
vemos**: lo que falla, lo confuso, lo que no funcionó como esperabas. Esta guía te dice
qué probar y cómo reportarlo.

> Requisitos: una máquina **Linux (Ubuntu/Debian)**, tu **API key de Claude** y un
> **bot de Telegram** propio (créalo con @BotFather y copia su token).

---

## 1. Instalar

```bash
curl -fsSL https://install.for3s.dev | sh
```

El instalador te pedirá: aceptar el aviso, un nombre para tu For3s, tu key de Claude y
tu token de Telegram. Luego levanta todo solo (la primera vez tarda — la imagen es grande).

✅ **Qué deberías ver:** al final, "Listo. Tu For3s '…' está corriendo."

---

## 2. Qué probar (checklist)

Marca lo que funcionó y anota lo que no:

- [ ] **Arranque** — ¿el instalador terminó sin errores? ¿en cuánto tiempo?
- [ ] **Primer contacto** — en Telegram, `/start` → ¿responde?
- [ ] **Conversar** — háblale normal → ¿responde con sentido?
- [ ] **Memoria** — cuéntale algo de ti; más tarde pregúntaselo → ¿lo recuerda?
- [ ] **Memoria tras reinicio** — `docker compose restart agent`, vuelve a preguntar →
      ¿sigue recordando? (no debe olvidar al reiniciar)
- [ ] **Skills** — `/aprende` tras una conversación útil → `/skills` → ¿la guardó?
- [ ] **"Sabe cuándo no sabe"** — pregúntale algo que no pueda saber → ¿lo admite o inventa?
- [ ] **Comandos** — abre el menú `/` → ¿los comandos funcionan?
- [ ] **(Opcional) GitHub** — si pusiste PAT, pídele analizar un repo público.
- [ ] **Desinstalar** — `cd ~/for3s-os && ./uninstall.sh` → ¿borra todo limpio?

---

## 3. Qué reportar

Por cada problema, cuéntanos (entre más detalle, mejor):

- **Qué hacías** (el paso o comando exacto)
- **Qué esperabas** que pasara
- **Qué pasó** en realidad (copia el mensaje de error / pega captura)
- **Tu entorno** (distro y versión: `cat /etc/os-release`)
- Logs si aplica: `cd ~/for3s-os && docker compose logs --tail=50 agent`

También nos sirve lo **subjetivo**: ¿qué te confundió? ¿qué esperabas y no estaba?
¿qué te gustó? Eso es justo lo que no podemos ver desde adentro.

---

## 4. Dónde reportar

Abre un **Issue** en este repo → usa la plantilla de bug. Un reporte = un problema
(más fácil de seguir). Si algo es urgente o sensible, indícalo en el título.

¡Gracias por ayudarnos a hacer For3s OS mejor! 🚀
