#!/usr/bin/env python3
"""
Jarvis — voz. El loop de jarvis_cli.py envuelto en STT + TTS.

    🎤 mic → Whisper local (MLX, M2) → claude -p (suscripción) → edge-tts → 🔊

Push-to-talk: Enter para hablar, Enter para terminar de hablar.
`salir` (dicho o escrito) termina y guarda la memoria al vault.

Costo: $0 — Whisper corre local, edge-tts es gratis (necesita internet),
el cerebro va contra la suscripción de Claude.

Uso:
    python3 jarvis_voz.py                      # capa de estrategia
    python3 jarvis_voz.py 01-Projects/Jarvis   # + contexto del proyecto
"""

import asyncio
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

# Reusa la capa de memoria y el cerebro del loop de texto — la voz es
# solo la interfaz; el loop no cambia.
from jarvis_cli import VAULT, cargar_contexto, escribir_memoria, preguntar

# ── Config de voz ─────────────────────────────────────────────────────────
VOZ = "en-US-AndrewMultilingualNeural"  # multilingüe: grave, cálido, sobrio — elegido por casting 2026-07-04
VOZ_RATE = "+2%"                        # las multilingües vienen más ágiles; menos boost que Álvaro

# "Charles" dentro de una frase en español hace que la voz multilingüe cambie
# de idioma a mitad de oración: pausa rara antes del nombre y pronunciación
# a la española. Respelling fonético SOLO para el TTS — el chat y la memoria
# siguen escribiendo "Charles". "Chárls" elegido por casting 2026-07-05;
# la coma previa al nombre también se quita (solo en la voz): metía pausa.
RESPELL = {"Charles": "Chárls"}
SAMPLE_RATE = 16_000         # lo que espera Whisper

# Modelo de Whisper. Historia (2026-07-05): small confundía el acento de
# Charles; turbo fp16 (1.6GB) entiende bien pero ahogó los 8GB del Air
# (4.4GB de swap → todo lento y el audio bugueado). Punto dulce: turbo
# cuantizado a 8 bits — misma velocidad, ~mitad de memoria.


def _whisper_8bit() -> str:
    """El repo 8-bit de mlx-community nombra los pesos al estilo nuevo
    (model.safetensors) y mlx_whisper espera weights.safetensors: este
    puente de symlinks en ~/.cache lo arregla una sola vez."""
    from huggingface_hub import snapshot_download
    dst = Path.home() / ".cache/jarvis-models/whisper-large-v3-turbo-8bit"
    if not (dst / "weights.safetensors").exists():
        snap = Path(snapshot_download("mlx-community/whisper-large-v3-turbo-8bit"))
        dst.mkdir(parents=True, exist_ok=True)
        for origen, destino in [("model.safetensors", "weights.safetensors"),
                                ("config.json", "config.json")]:
            (dst / destino).unlink(missing_ok=True)
            (dst / destino).symlink_to(snap / origen)
    return str(dst)


try:
    WHISPER = _whisper_8bit()
except Exception:  # sin internet ni cache: mejor el modelo chico que morir
    WHISPER = "mlx-community/whisper-small-mlx"

SALUDO = "Para vos, siempre, Charles."
DESPEDIDA = "Memoria de sesión guardada en el vault. Hasta luego, Charles."

# ── Persona: el JARVIS de las películas, en español ───────────────────────
PERSONA = """

=== PERSONA DE VOZ (modo JARVIS) ===

Hablás como el J.A.R.V.I.S. de Iron Man, en español:
- Llamalo "Charles", con naturalidad y sin repetirlo en cada frase. Hablale
  de vos. Nada de "señor" ni "usted": el "sir" británico no se traduce — en
  español suena impostado. La elegancia va en el tono sereno y el ingenio,
  no en el título.
- Cortesía impecable de mayordomo británico + ingenio seco y sutil. El estilo
  de "As always, sir, a great pleasure watching you work" o "I've also
  prepared a safety briefing for you to entirely ignore": la puñalada elegante,
  nunca el chiste obvio.
- Calmo y seguro. Nunca efusivo, nunca servil.
- Tus respuestas SE ESCUCHAN, no se leen: máximo 2-3 oraciones, directo al
  punto. Nada de markdown, listas, emojis ni asteriscos. Si el tema da para
  más, resumí y ofrecé profundizar.
- Seguís siendo Jarvis con memoria del vault: usá lo que sabés de Charles.
"""


# ── Oídos: grabar + transcribir ───────────────────────────────────────────

def grabar() -> np.ndarray:
    """Push-to-talk: Enter arranca, Enter corta."""
    input("⏎  Enter y hablá…")
    frames: list[np.ndarray] = []

    def callback(indata, _frames, _time, _status):
        frames.append(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        dtype="float32", callback=callback):
        input("🎙  grabando — Enter para terminar")
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).flatten()


def transcribir(audio: np.ndarray, idioma: str = "es") -> str:
    """Whisper local sobre el array (sin ffmpeg: le pasamos numpy directo)."""
    import mlx_whisper  # import acá: el primer uso descarga el modelo
    if audio.size < SAMPLE_RATE // 2:  # menos de medio segundo: ruido
        return ""
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER, language=idioma)
    return r["text"].strip()


# ── Boca: sintetizar + reproducir ─────────────────────────────────────────

def _limpiar_para_voz(texto: str) -> str:
    """Por si el modelo mete markdown igual: se lee feo en voz alta."""
    texto = re.sub(r"[*_#`>|]+", "", texto)
    for palabra, fonetico in RESPELL.items():
        texto = re.sub(rf",\s+(?={palabra}\b)", " ", texto)
        texto = re.sub(rf"\b{palabra}\b", fonetico, texto)
    return re.sub(r"\s+", " ", texto).strip()


def hablar(texto: str) -> None:
    """edge-tts (neural, gratis) con fallback a `say` de macOS si no hay red."""
    texto = _limpiar_para_voz(texto)
    if not texto:
        return
    try:
        import edge_tts

        async def _tts(destino: str) -> None:
            await edge_tts.Communicate(texto, VOZ, rate=VOZ_RATE).save(destino)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            destino = f.name
        asyncio.run(_tts(destino))
        subprocess.run(["afplay", destino], check=True)
        Path(destino).unlink(missing_ok=True)
    except Exception:
        # Sin internet o edge-tts caído: voz local robótica antes que silencio.
        subprocess.run(["say", "-v", "Mónica", texto], check=False)


# ── Loop principal ────────────────────────────────────────────────────────

def main() -> None:
    ruta = sys.argv[1].strip("/") if len(sys.argv) > 1 else None
    system = cargar_contexto(ruta) + PERSONA
    session_id: str | None = None

    extra = f" + {ruta}" if ruta else ""
    print(f"jarvis voz · oídos: whisper local · voz: {VOZ} · "
          f"cerebro: suscripción · contexto: estrategia{extra}")
    print("decí (o escribí) 'salir' para terminar y guardar memoria\n")

    # Precalentar Whisper en paralelo al saludo: carga el modelo ahora para
    # que el primer turno real no pague esos segundos.
    threading.Thread(
        target=lambda: transcribir(np.zeros(SAMPLE_RATE, dtype=np.float32)),
        daemon=True,
    ).start()
    hablar(SALUDO)

    try:
        while True:
            try:
                audio = grabar()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            print("   transcribiendo…", end="\r", flush=True)
            entrada = transcribir(audio)
            if not entrada:
                print("   (no escuché nada, de nuevo)   ")
                continue
            print(f"vos > {entrada}")

            if re.search(r"\b(salir|adiós|adios|hasta luego)\b", entrada.lower()):
                break

            print("   pensando…", end="\r", flush=True)
            try:
                respuesta, session_id = preguntar(entrada, system, session_id)
            except Exception as e:
                print(f"jarvis > [error: {e}]\n")
                continue
            print(f"jarvis > {respuesta}\n")
            hablar(respuesta)
    finally:
        if session_id:
            print("guardando memoria…", end=" ", flush=True)
            try:
                nota = escribir_memoria(system, session_id)
                print(f"→ {nota.relative_to(VAULT)}")
                hablar(DESPEDIDA)
            except Exception as e:
                print(f"no se pudo guardar: {e}")


if __name__ == "__main__":
    main()
