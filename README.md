# Jarvis 🔵

Asistente de voz personal estilo J.A.R.V.I.S., corriendo **local en macOS a costo $0**.
Le decís *"jarvis"*, le hablás, y responde por voz con memoria real de quién sos y en qué estás trabajando — mientras un HUD tipo Iron Man muestra lo que pasa.

> 🎬 *Video demo: próximamente.*

## Qué hace

- **Manos libres**: wake word *"jarvis"* (openWakeWord, local) + VAD adaptativo — hablás y al callarte corta solo. Cero clicks.
- **Conversación con memoria**: sabe quién es su usuario, sus proyectos y decisiones pasadas, y **recuerda entre sesiones** (ver diseño de memoria abajo).
- **Manos**: abre y cierra apps, controla Spotify y el volumen, pone timers hablados, captura notas por voz y busca en su propia memoria — con un modelo de seguridad de allowlist.
- **HUD web**: arc reactor animado que reacciona a la voz, subtítulos sincronizados, telemetría en vivo (latencias, timers activos, qué herramienta está ejecutando).
- **Barge-in**: lo podés interrumpir a media frase con un click.

## Arquitectura

```
        ┌─ OÍDO ──────────────┐   ┌─ CEREBRO ──────────────┐   ┌─ VOZ ─────────────┐
 🎤 ──▶ │ openWakeWord (local) │──▶│ Claude vía `claude -p` │──▶│ edge-tts, síntesis │──▶ 🔊
        │ + VAD adaptativo     │   │ streaming + tool use   │   │ por oración en     │
        │ + Whisper MLX (local)│   │ con allowlist          │   │ paralelo al stream │
        └──────────────────────┘   └───────────┬────────────┘   └───────────────────┘
                                               │ lee / escribe
                                   ┌───────────▼────────────┐
                                   │ MEMORIA: vault Obsidian │
                                   │ (markdown local)        │
                                   └─────────────────────────┘
                       HUD (Flask + canvas) orquesta todo en el browser
```

**Decisión de fondo: la voz es interfaz, no arquitectura.** `jarvis_voz.py` y el HUD envuelven el mismo loop de `jarvis_cli.py` sin tocarlo — el cerebro no sabe si le hablaste o escribiste.

## El diseño de memoria (la parte interesante)

La memoria no es un vector store: es un **vault de Obsidian** (markdown plano) con instrucciones en capas:

1. **Capa de estrategia** — quién es el usuario, cómo hablarle, estructura del vault. Se carga siempre.
2. **Capa técnica por proyecto** — stack, decisiones y estado del proyecto activo. Se carga según contexto.
3. **Al cerrar sesión**, Jarvis escribe un resumen de lo aprendido de vuelta al vault.

El loop completo: `leer contexto del vault → llamar a Claude → responder → escribir lo aprendido`. La próxima sesión retoma sin re-explicar nada. Ventaja sobre embeddings: la memoria es **legible, editable y versionable por el humano** — el usuario ve y controla exactamente qué sabe su asistente.

## Manos y seguridad

Las acciones corren vía tool use de `claude -p` con `--allowedTools` de **prefijos exactos**: la seguridad es el menú de comandos permitidos, no el prompt. Lo que no está en la allowlist lo deniega el CLI antes de ejecutarse — pedirle "borrá tal archivo" no funciona ni con prompt injection, porque la herramienta simplemente no existe para él.

## Latencia a costo $0

Sin Realtime APIs pagas, la fluidez sale de ingeniería:

- **Streaming por oraciones**: el TTS sintetiza cada oración en paralelo mientras el resto de la respuesta sigue llegando — Jarvis empieza a hablar antes de terminar de pensar.
- **Pre-roll buffer**: ~1.2s de audio previo al wake word se anteponen a la captura — no se pierde lo dicho de corrido.
- **Recorte de silencios** en las fronteras de cada mp3 (los colchones de edge-tts sonaban como pausas robóticas).
- **Whisper 8-bit** (large-v3-turbo cuantizado): mitad de memoria que fp16, clave en una Mac de 8GB.
- **Anuncio antes de ejecutar**: "Dale, lo abro" suena mientras la herramienta corre — la espera nunca es muda.

## Correrlo

Requisitos: macOS Apple Silicon, Python 3.10+, [Claude Code](https://claude.com/claude-code) con sesión iniciada (suscripción — sin API key), internet para el TTS.

```bash
pip install -r requirements.txt
python3 jarvis_ui.py <ruta-a-tu-vault>   # HUD completo en localhost:7777
python3 jarvis_voz.py <ruta-a-tu-vault>  # solo voz, en terminal
python3 jarvis_cli.py <ruta-a-tu-vault>  # solo texto
```

La primera corrida descarga los modelos (Whisper, wake word) y macOS pide permiso de micrófono. El vault necesita un `CLAUDE.md` raíz con el contexto del usuario — la persona está adaptada a su usuario original; ajustá `PERSONA` en `jarvis_voz.py` y `RESPELL` para el tuyo.

## Estructura

| Archivo | Qué es |
|---|---|
| `jarvis_cli.py` | El cerebro: loop sobre `claude -p`, contexto del vault, streaming, memoria de sesión |
| `jarvis_voz.py` | Oído y voz: Whisper MLX local + edge-tts + persona |
| `jarvis_ui.py` + `ui.html` | HUD web: Flask + SSE + canvas; wake word, timers, barge-in |
| `manos.py` | Menú de acciones del sistema (apps, Spotify, volumen, capturas) |
| `timer.py` | Timers hablados |
| `jarvis.py` | Versión SDK (`anthropic`), lista para migrar cuando haya crédito API |

## Roadmap

- **Router de comandos**: interceptar comandos obvios antes del round-trip del LLM.
- **Proactividad**: briefing hablado del primer boot del día leyendo los pendientes del vault.
- **Visión**: snapshot de pantalla/webcam → Claude.
- **Multi-agent** (fase SDK): investigación en background sin bloquear la conversación.

---

*Proyecto personal de aprendizaje en público — construido con Claude Code como pair programmer.*
