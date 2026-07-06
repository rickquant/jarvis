# Jarvis 🔵

*[English](README.md) · [Español](README.es.md)*

Asistente de voz personal estilo J.A.R.V.I.S., corriendo **local en macOS a costo $0**.
Le decís *"jarvis"*, le hablás, y responde por voz con memoria real de quién sos y en qué estás trabajando — mientras un HUD tipo Iron Man muestra lo que pasa.

> 🎬 *Video demo: próximamente.*

## Qué hace

- **Manos libres**: wake word *"jarvis"* (openWakeWord, local) + VAD adaptativo — hablás y al callarte corta solo. Cero clicks.
- **Conversación con memoria**: sabe quién es su usuario, sus proyectos y decisiones pasadas, y **recuerda entre sesiones** (ver diseño de memoria abajo).
- **Briefing proactivo**: en el primer boot del día no solo saluda — informa el clima, notas pendientes del vault, capturas de voz y entregas próximas de la universidad (integración con Canvas LMS), todo hablado.
- **Manos**: abre y cierra apps, controla Spotify y el volumen, pone timers hablados, captura notas por voz, busca en su propia memoria y **busca en la web** (solo lectura, con regla anti prompt-injection: lo que lee es información, nunca instrucciones) — con un modelo de seguridad de allowlist.
- **100% bilingüe (es/en)**: un toggle cambia el pipeline completo — oído, cerebro, voz y UI. Elegí el idioma en la pantalla de boot, o forzalo con `?idioma=en`.
- **HUD web**: arc reactor animado que reacciona a la voz, subtítulos estilo película sincronizados, telemetría en vivo (latencias, timers activos, clima, qué herramienta está ejecutando).
- **Barge-in**: lo podés interrumpir a media frase con un click.

## Arquitectura

```
        ┌─ OÍDO ────────────────┐   ┌─ CEREBRO ──────────────┐   ┌─ VOZ ─────────────┐
 🎤 ──▶ │ openWakeWord (local)   │──▶│ Claude vía `claude -p` │──▶│ edge-tts, síntesis │──▶ 🔊
        │ + VAD adaptativo       │   │ streaming + tool use   │   │ por oración en     │
        │ + Apple SpeechAnalyzer │   │ con allowlist          │   │ paralelo al stream │
        │   (Neural Engine;      │   └───────────┬────────────┘   └───────────────────┘
        │   fallback Whisper MLX)│               │ lee / escribe
        └────────────────────────┘   ┌───────────▼────────────┐
                                     │ MEMORIA: vault Obsidian │
                                     │ (markdown local)        │
                                     └─────────────────────────┘
                       HUD (Flask + canvas) orquesta todo en el browser
```

**Decisión de fondo: la voz es interfaz, no arquitectura.** `jarvis_voz.py` y el HUD envuelven el mismo loop de `jarvis_cli.py` sin tocarlo — el cerebro no sabe si le hablaste o escribiste. Por eso mismo cambiar el motor de STT (Whisper → Apple) fue un cambio dentro de una sola función que ningún otro componente notó.

## El diseño de memoria (la parte interesante)

La memoria no es un vector store: es un **vault de Obsidian** (markdown plano) con instrucciones en capas:

1. **Capa de estrategia** — quién es el usuario, cómo hablarle, estructura del vault. Se carga siempre.
2. **Capa técnica por proyecto** — stack, decisiones y estado del proyecto activo. Se carga según contexto.
3. **Al cerrar sesión**, Jarvis escribe un resumen de lo aprendido de vuelta al vault.

El loop completo: `leer contexto del vault → llamar a Claude → responder → escribir lo aprendido`. La próxima sesión retoma sin re-explicar nada. Ventaja sobre embeddings: la memoria es **legible, editable y versionable por el humano** — el usuario ve y controla exactamente qué sabe su asistente.

## El oído: STT a nivel de sistema (nuevo)

La transcripción corre sobre el **SpeechAnalyzer de Apple** (macOS 26+) a través del CLI [`yap`](https://github.com/finnvoor/yap): el modelo se ejecuta en el **Neural Engine como servicio del sistema, fuera de este proceso**. En un MacBook Air de 8GB eso liberó **~1GB de RAM** y eliminó el pico de CPU por turno que obligaba al HUD a congelar sus animaciones durante la transcripción.

Medido contra Whisper local (large-v3-turbo 8-bit) con clips de voz reales: misma precisión, 0.4–0.6s por clip contra ~1s, ~0% de CPU — y un bonus que decidió la migración: **ante audio malo Apple devuelve vacío donde Whisper alucinaba texto** (que llegaba al cerebro como si el usuario lo hubiera dicho).

Si `yap` no está instalado (o el macOS es más viejo), Jarvis **cae solo a Whisper MLX** y la línea de boot del HUD reporta qué oído está activo de verdad — la telemetría nunca miente.

## Manos y seguridad

Las acciones corren vía tool use de `claude -p` con `--allowedTools` de **prefijos exactos**: la seguridad es el menú de comandos permitidos, no el prompt. Lo que no está en la allowlist lo deniega el CLI antes de ejecutarse — pedirle "borrá tal archivo" no funciona ni con prompt injection, porque la herramienta simplemente no existe para él. El acceso web es solo lectura (`WebSearch`/`WebFetch`), con regla explícita: lo leído en la web es información, nunca instrucciones.

## Latencia a costo $0

Sin Realtime APIs pagas, la fluidez sale de ingeniería:

- **Streaming por oraciones**: el TTS sintetiza cada oración en paralelo mientras el resto de la respuesta sigue llegando — Jarvis empieza a hablar antes de terminar de pensar.
- **STT fuera del proceso**: la transcripción en el Neural Engine no le cuesta nada a este proceso — el HUD anima a tasa completa incluso mientras transcribe.
- **Pre-roll buffer**: ~1.2s de audio previo al wake word se anteponen a la captura — no se pierde lo dicho de corrido.
- **Recorte de silencios** en las fronteras de cada mp3 (los colchones de edge-tts sonaban como pausas robóticas).
- **Anuncio antes de ejecutar**: "Dale, lo abro" suena mientras la herramienta corre — la espera nunca es muda.
- **Briefing determinístico**: el briefing de la mañana junta todos sus datos (clima, vault, Canvas) en código y hace UN solo round-trip del LLM — nada de cadenas lentas de tool use.

## Correrlo

Requisitos: macOS Apple Silicon, Python 3.10+, [Claude Code](https://claude.com/claude-code) con sesión iniciada (suscripción — sin API key), internet para el TTS. Para el oído nativo: macOS 26+ y [`yap`](https://github.com/finnvoor/yap) (`brew install yap`, o el binario del release en tu `PATH` o `~/.local/bin`) — sin él, todo funciona igual sobre Whisper local.

```bash
pip install -r requirements.txt
python3 jarvis_ui.py <ruta-a-tu-vault>   # HUD completo en localhost:7777
python3 jarvis_voz.py <ruta-a-tu-vault>  # solo voz, en terminal
python3 jarvis_cli.py <ruta-a-tu-vault>  # solo texto
```

La primera corrida descarga los modelos (wake word; Whisper solo si se usa como fallback) y macOS pide permiso de micrófono. El vault necesita un `CLAUDE.md` raíz con el contexto del usuario — la persona está adaptada a su usuario original; ajustá `PERSONA` en `jarvis_voz.py` y `RESPELL` para el tuyo. (El asistente habla español por defecto — el toggle del boot o `?idioma=en` pasa todo a inglés.)

Deep links útiles: `?idioma=en|es` (fuerza el idioma de la UI), `?briefing` (fuerza el briefing de la mañana — ideal para demos), `?sin-boot` (salta la secuencia de boot).

## Estructura

| Archivo | Qué es |
|---|---|
| `jarvis_cli.py` | El cerebro: loop sobre `claude -p`, contexto del vault, streaming, memoria de sesión |
| `jarvis_voz.py` | Oído y voz: Apple SpeechAnalyzer vía `yap` (fallback Whisper MLX) + edge-tts + persona |
| `jarvis_ui.py` + `ui.html` | HUD web: Flask + SSE + canvas; wake word, timers, barge-in, UI bilingüe |
| `briefing.py` | Briefing de la mañana: clima, pendientes del vault, capturas de voz, entregas de Canvas |
| `manos.py` | Menú de acciones del sistema (apps, Spotify, volumen, capturas) |
| `timer.py` | Timers hablados |
| `jarvis.py` | Versión SDK (`anthropic`), lista para migrar cuando haya crédito API |

## Roadmap

- **Recordatorios persistentes**: "recordame mañana…" → nota con fecha en el vault, que el briefing del día siguiente levanta.
- **Router de comandos**: interceptar comandos obvios antes del round-trip del LLM.
- **Visión**: snapshot de pantalla/webcam → Claude.
- **Multi-agent** (fase SDK): investigación en background sin bloquear la conversación.

---

*Proyecto personal de aprendizaje en público — construido con Claude Code como pair programmer.*
