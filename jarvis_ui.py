#!/usr/bin/env python3
"""
Jarvis — UI. Servidor local que sirve el HUD (ui.html) sobre el mismo loop.

    browser (HUD) → Flask → [loop de jarvis_cli] + [voz de jarvis_voz]

v2 — tres upgrades sobre la v1, misma arquitectura:
  1. Streaming: el cerebro va vía preguntar_stream() y el texto llega al
     browser token a token (SSE) — se acabó la espera muda de 3-8s.
  2. El TTS ya no suena en el servidor (afplay): el mp3 de edge-tts viaja
     al browser, que lo reproduce con Web Audio — así el reactor pulsa con
     la onda real de la voz y un click interrumpe a Jarvis (barge-in).
  3. La transcripción del mic se devuelve aparte del turno: ves lo que
     Whisper entendió al instante, y el turno arranca después.

Corre local, costo $0.

Uso:
    python3 jarvis_ui.py                      # capa de estrategia
    python3 jarvis_ui.py 01-Projects/Jarvis   # + contexto del proyecto
    → abrí http://localhost:7777
"""

import asyncio
import json
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
from flask import Flask, Response, jsonify, request, send_file

from briefing import PROMPT_BRIEFING, datos_briefing
from jarvis_cli import (VAULT, cargar_contexto, escribir_memoria,
                        preguntar_stream)
from jarvis_voz import (PERSONA, RESPELL, SAMPLE_RATE, VOZ, VOZ_RATE,
                        _limpiar_para_voz, transcribir)

app = Flask(__name__)
AQUI = Path(__file__).resolve().parent

# Estado global de la sesión (la UI es single-user: es tu Mac)
S = {
    "fase": "listo",        # listo | escuchando | transcribiendo | pensando
    "system": "",           # ("hablando" lo maneja el browser: el audio vive allá)
    "session_id": None,
    "ocupado": False,
    "nivel": 0.0,           # RMS del mic en vivo — anima el reactor mientras hablás
    "manos_libres": False,  # oído siempre encendido: "hey jarvis" + VAD
    "manos_error": None,
    "pendiente": None,      # turno capturado por manos libres; el browser lo reclama
    "hablando_browser": 0,  # timestamp del último "estoy hablando" del browser
    "ultimo_poll": 0,       # último /api/estado — si nadie mira, el oído se pausa
    "avisos": [],           # timers vencidos etc. — el browser los recoge y los dice
    "timers": [],           # timers activos {fin, etiqueta} — el HUD los muestra en vivo
    "idioma": "es",         # idioma de la UI — el oído transcribe en este idioma
    "clima": None,          # lo trae el briefing; el HUD lo muestra en telemetría
    "lock": threading.Lock(),
}
_rec = {"stream": None, "frames": []}

# Corte de oraciones para TTS incremental: apenas hay una oración completa
# en el stream se manda a sintetizar, sin esperar el resto de la respuesta.
_ORACION = re.compile(r"(.+?[.!?…])(?:\s+|$)", re.S)

# Primer bocado: se corta en la primera cláusula (coma, etc.) para que la voz
# arranque antes — pero NUNCA justo antes del nombre: si "Charles" cae al
# inicio del pedazo siguiente, la coma queda en el pedazo anterior, el
# respelling de _limpiar_para_voz no la ve y el gap entre audios suena como
# la pausa robótica que vinimos a matar.
_PRIMER_BOCADO = re.compile(
    rf"(.{{15,}}?[,;:.!?…])\s+(?!(?:{'|'.join(RESPELL)})\b)", re.S)


def _sse(datos: dict) -> str:
    return f"data: {json.dumps(datos, ensure_ascii=False)}\n\n"


# ── Manos libres: "hey jarvis" (openWakeWord) + fin de habla (VAD) ────────
#
# Loop en thread propio:  mic continuo → wake word → capturar hasta silencio
# → Whisper → S["pendiente"]  (el browser lo reclama vía /api/pendiente y
# dispara el turno normal por /api/stream — nada más cambia).
#
# Anti-eco: se pausa mientras el browser reproduce la voz de Jarvis, mientras
# hay un turno en curso, o si ningún browser está mirando (poll viejo).

CHUNK = 1280           # 80 ms @ 16 kHz — lo que espera openwakeword
UMBRAL_WAKE = 0.35     # score para despertar — bajado de 0.5 (2026-07-05) para
                       # que "jarvis" a secas también dispare; si empieza a
                       # despertar por error, subirlo de a 0.05
SILENCIO_FIN = 1.3     # segundos callado = terminaste de hablar — 1.0 cortaba
                       # a Charles a media frase en pausas naturales
MAX_UTTERANCE = 15.0   # tope de captura por turno
ESPERA_HABLA = 6.0     # despertó pero nunca habló → volver a dormir


def _oido_activo() -> bool:
    # el 1.5 tolera el heartbeat de "sigo hablando" (400ms, o ~1s si el tab
    # está en background y el browser lo estrangula)
    return (not S["ocupado"]
            and _rec["stream"] is None                      # sin push-to-talk en curso
            and time.time() - S["hablando_browser"] > 1.5   # jarvis no está sonando
            and time.time() - S["ultimo_poll"] < 5)         # hay un browser mirando


def _manos_libres_loop() -> None:
    try:
        from openwakeword.model import Model as OWW
        from openwakeword.utils import download_models
        download_models(["hey_jarvis"])  # no-op si ya están
        oww = OWW(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    except Exception as e:
        S["manos_error"] = f"openwakeword no cargó: {e}"
        S["manos_libres"] = False
        return

    piso = deque(maxlen=60)     # RMS del ruido de fondo del cuarto (rolling)
    preroll = deque(maxlen=15)  # ~1.2s de audio previo al wake: sin esto, las
                                # palabras dichas mientras el detector confirma
                                # "jarvis" se pierden y Whisper solo ve la cola

    def umbral_voz() -> float:
        base = sorted(piso)[len(piso) // 2] if piso else 0.0
        return max(base * 3.0, 0.006)

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32", blocksize=CHUNK) as st:
            while S["manos_libres"]:
                datos, _ = st.read(CHUNK)
                x = datos[:, 0]
                if not _oido_activo():
                    continue  # seguir leyendo (buffer fresco) pero sin detectar
                preroll.append(x.copy())
                piso.append(float(np.sqrt((x ** 2).mean())))
                score = max(oww.predict((x * 32767).astype(np.int16)).values())
                if score > 0.2:
                    # visible en la terminal: con esto se tunea UMBRAL_WAKE
                    # (si tus "hey jarvis" marcan 0.3-0.4, bajá el umbral)
                    print(f"  oído · score {score:.2f} (umbral {UMBRAL_WAKE})",
                          flush=True)
                if score < UMBRAL_WAKE:
                    continue

                # ── despertó: capturar hasta que dejes de hablar ──
                # arranca con el pre-roll: incluye el "jarvis" y lo que hayas
                # dicho de corrido mientras el detector confirmaba
                oww.reset()
                S["fase"] = "escuchando"
                frames = list(preroll)
                preroll.clear()
                hablo, silencio, t0 = False, 0.0, time.time()
                while S["manos_libres"]:
                    datos, _ = st.read(CHUNK)
                    x = datos[:, 0]
                    frames.append(x.copy())
                    rms = float(np.sqrt((x ** 2).mean()))
                    S["nivel"] = rms
                    if rms > umbral_voz():
                        hablo, silencio = True, 0.0
                    elif hablo:
                        silencio += CHUNK / SAMPLE_RATE
                        if silencio >= SILENCIO_FIN:
                            break
                    if time.time() - t0 > (MAX_UTTERANCE if hablo else ESPERA_HABLA):
                        break
                S["nivel"] = 0.0

                if not hablo:
                    S["fase"] = "listo"
                    continue
                S["fase"] = "transcribiendo"
                texto = transcribir(np.concatenate(frames).flatten(),
                                    S["idioma"])
                # el pre-roll mete el wake word (y a veces ruido previo) al
                # inicio — quitar hasta el "jarvis" inclusive, si está al frente
                texto = re.sub(r"^.{0,40}?\bjarvis\b[\s,.:;!?]*", "", texto,
                               count=1, flags=re.I | re.S).strip() or texto
                if texto:
                    S["pendiente"] = texto
                    S["fase"] = "pensando"  # el browser reclama y dispara el turno
                else:
                    S["fase"] = "listo"
    except Exception as e:
        S["manos_error"] = f"el oído murió: {e}"
        S["manos_libres"] = False
        if S["fase"] in ("escuchando", "transcribiendo"):
            S["fase"] = "listo"


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_file(AQUI / "ui.html")


@app.get("/api/estado")
def estado():
    S["ultimo_poll"] = time.time()
    if request.args.get("hablando"):
        # heartbeat anti-eco: mientras la voz de jarvis suena, cada poll
        # renueva la pausa del oído (ver /api/hablando)
        S["hablando_browser"] = time.time()
    if request.args.get("idioma") in ("es", "en"):
        # el poll sincroniza el idioma al server: así el oído transcribe en
        # el idioma correcto aun antes del primer turno tras el toggle
        S["idioma"] = request.args["idioma"]
    err, S["manos_error"] = S["manos_error"], None  # se informa una sola vez
    aviso = None
    with S["lock"]:
        if S["avisos"]:
            aviso = S["avisos"].pop(0)
        timers = [{"resta": max(0, int(t["fin"] - time.time())),
                   "etiqueta": t["etiqueta"]} for t in S["timers"]]
    return jsonify({"fase": S["fase"], "nivel": round(S["nivel"], 4),
                    "manos": S["manos_libres"], "manos_error": err,
                    "pendiente": S["pendiente"] is not None, "aviso": aviso,
                    "timers": timers, "clima": S["clima"]})


@app.post("/api/manos_libres")
def manos_libres():
    encender = bool((request.json or {}).get("on"))
    if encender and not S["manos_libres"]:
        S["manos_libres"] = True
        S["manos_error"] = None
        threading.Thread(target=_manos_libres_loop, daemon=True).start()
    elif not encender:
        S["manos_libres"] = False  # el loop se apaga solo al ver el flag
    return jsonify({"on": S["manos_libres"]})


@app.post("/api/hablando")
def hablando():
    # el browser avisa cuando la voz de jarvis empieza o termina: el oído se
    # pausa (anti-eco). Es solo el aviso instantáneo — la pausa la SOSTIENE
    # el heartbeat del poll de estado (?hablando=1). Antes el "on" dejaba el
    # oído sordo "hasta nuevo aviso" (+1h): si el "off" se perdía (tab
    # cerrado a media frase, server reiniciado), manos libres moría en
    # silencio. Ahora lo peor que pasa es ~1.5s extra de pausa.
    S["hablando_browser"] = time.time()
    return jsonify({"ok": True})


@app.post("/api/pendiente")
def pendiente():
    # reclamo atómico: con dos tabs abiertos, solo uno se lleva el turno
    with S["lock"]:
        texto, S["pendiente"] = S["pendiente"], None
    return jsonify({"texto": texto})


@app.post("/api/mic/start")
def mic_start():
    """Idempotente: si ya estaba grabando (tab viejo, click perdido), reinicia
    la captura en vez de fallar — el click del usuario siempre gana. Si no,
    el browser y el servidor quedan desincronizados y los clicks mueren."""
    if _rec["stream"] is not None:
        _rec["frames"] = []
        S["fase"] = "escuchando"
        return jsonify({"ok": True, "reiniciado": True})
    _rec["frames"] = []

    def callback(indata, _frames, _time, _status):
        _rec["frames"].append(indata.copy())
        S["nivel"] = float(np.sqrt((indata ** 2).mean()))

    # Abrir el mic también puede colgarse (PortAudio/CoreAudio, p.ej. si el
    # proceso no tiene permiso de mic): watchdog de 5s y error visible antes
    # que una UI muda.
    res = {}

    def _abrir():
        try:
            st = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="float32", callback=callback)
            st.start()
            res["stream"] = st
        except Exception as e:
            res["error"] = str(e)

    th = threading.Thread(target=_abrir, daemon=True)
    th.start()
    th.join(timeout=5)
    if "stream" not in res:
        return jsonify({"error": res.get(
            "error",
            "el mic no respondió — corré Jarvis desde tu terminal "
            "(permiso de micrófono de macOS)")}), 500

    _rec["stream"] = res["stream"]
    S["fase"] = "escuchando"
    return jsonify({"ok": True})


@app.post("/api/mic/stop")
def mic_stop():
    """Corta la grabación y devuelve SOLO la transcripción; el turno lo
    dispara el browser contra /api/stream con este texto."""
    stream = _rec["stream"]
    if stream is None:
        return jsonify({"error": "no estaba grabando"}), 409
    _rec["stream"] = None
    S["nivel"] = 0.0

    # stop() de PortAudio se puede colgar (visto en macOS). Si eso pasa y el
    # endpoint no responde, el browser queda "ocupado" para siempre y los
    # clicks mueren. Watchdog: 3s y seguimos con los frames que ya tenemos.
    def _cerrar():
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
    th = threading.Thread(target=_cerrar, daemon=True)
    th.start()
    th.join(timeout=3)

    S["fase"] = "transcribiendo"
    try:
        audio = (np.concatenate(_rec["frames"]).flatten()
                 if _rec["frames"] else np.zeros(0, dtype=np.float32))
        _rec["frames"] = []
        # tope de 60s: si el mic quedó abierto minutos (click perdido, tab
        # duplicado), transcribirlo entero colgaría el turno
        audio = audio[-60 * SAMPLE_RATE:]
        texto = transcribir(audio, S["idioma"])
    finally:
        S["fase"] = "listo"
    if not texto:
        return jsonify({"error": "no escuché nada"})
    return jsonify({"texto": texto})


@app.post("/api/stream")
def stream():
    """Un turno del loop, en vivo. SSE con eventos:
        {tipo: "delta",   texto}  — fragmento de respuesta
        {tipo: "oracion", texto}  — oración completa lista para TTS
        {tipo: "fin",     texto}  — respuesta completa
        {tipo: "error",   texto}
    """
    d = request.json or {}
    entrada = d.get("texto", "").strip()
    idioma = d.get("idioma", "es")
    S["idioma"] = idioma  # el oído también transcribe en este idioma
    if d.get("briefing"):
        # briefing proactivo del primer boot del día: la recolección de
        # datos es determinística (briefing.py) y el cerebro solo narra —
        # un único round-trip. Mientras se junta, la intro sigue sonando.
        datos, clima = datos_briefing(VAULT, idioma)
        if clima:
            S["clima"] = clima  # de paso, al HUD (barra de telemetría)
        entrada = PROMPT_BRIEFING.get(idioma, PROMPT_BRIEFING["es"]) \
            .format(datos=datos)
    if not entrada:
        return jsonify({"error": "vacío"}), 400
    if S["ocupado"]:
        return jsonify({"error": "ocupado, esperá el turno actual"}), 409

    # el toggle de idioma de la UI manda sobre el cerebro: en cada modo
    # responde SIEMPRE en ese idioma, le hablen como le hablen (los
    # subtítulos son su respuesta — sin esto el demo quedaba mixto).
    # La instrucción va anexada al TURNO, no al system: probado que
    # `claude -p --resume` ignora --append-system-prompt al retomar una
    # sesión, así que por el system solo entraba en el turno 1.
    sistema = S["system"]
    if idioma == "en":
        entrada += ("\n\n[UI language mode: ENGLISH — reply ONLY in English "
                    "this turn, no matter the language spoken to you. Same "
                    "persona, same dry wit. Don't mention this note.]")
    else:
        entrada += ("\n\n[Modo de idioma de la UI: ESPAÑOL — respondé SOLO "
                    "en español este turno, te hablen en el idioma que te "
                    "hablen. Misma persona. No menciones esta nota.]")

    def gen():
        S["ocupado"] = True
        S["fase"] = "pensando"
        pendiente = ""   # texto acumulado aún sin cortar en oraciones
        primera = True   # el primer bocado se corta temprano (ver abajo)
        try:
            for ev in preguntar_stream(entrada, sistema, S["session_id"]):
                if ev[0] == "delta":
                    pendiente += ev[1]
                    yield _sse({"tipo": "delta", "texto": ev[1]})
                    # primer bocado: cortar en la primera cláusula (coma,
                    # punto…) en vez de esperar la oración completa — menos
                    # texto que sintetizar = la voz arranca ~1s antes
                    if primera and (m := _PRIMER_BOCADO.match(pendiente)):
                        yield _sse({"tipo": "oracion", "texto": m.group(1)})
                        pendiente = pendiente[m.end():]
                        primera = False
                    while (m := _ORACION.match(pendiente)) and \
                            len(pendiente) > len(m.group(1)):
                        yield _sse({"tipo": "oracion", "texto": m.group(1)})
                        pendiente = pendiente[m.end():]
                        primera = False
                elif ev[0] == "mano":
                    yield _sse({"tipo": "mano", "texto": ev[1]})
                else:  # ("fin", respuesta, session_id)
                    _, respuesta, S["session_id"] = ev
                    if pendiente.strip():
                        yield _sse({"tipo": "oracion", "texto": pendiente})
                    yield _sse({"tipo": "fin", "texto": respuesta})
        except Exception as e:
            yield _sse({"tipo": "error", "texto": str(e)})
        finally:
            S["fase"] = "listo"
            S["ocupado"] = False

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.post("/api/tts")
def tts():
    """Sintetiza una oración con edge-tts y devuelve el mp3 al browser."""
    texto = _limpiar_para_voz((request.json or {}).get("texto", ""))
    if not texto:
        return ("", 204)
    try:
        import edge_tts

        async def _gen() -> bytes:
            buf = bytearray()
            com = edge_tts.Communicate(texto, VOZ, rate=VOZ_RATE)
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        return Response(asyncio.run(_gen()), mimetype="audio/mpeg")
    except Exception as e:
        # Sin internet o edge-tts caído: la UI sigue muda pero viva.
        return jsonify({"error": str(e)}), 503


@app.post("/api/timer")
def timer():
    """Mano de Jarvis (vía timer.py): programa un aviso hablado. Al vencer,
    el texto entra a S["avisos"] y el poll de estado se lo lleva al browser,
    que lo dice por voz."""
    d = request.json or {}
    try:
        minutos = float(d.get("minutos", 0))
    except (TypeError, ValueError):
        minutos = 0
    if not 0 < minutos <= 24 * 60:
        return jsonify({"error": "minutos fuera de rango (0 a 1440)"}), 400
    etiqueta = str(d.get("etiqueta") or "").strip()
    registro = {"fin": time.time() + minutos * 60, "etiqueta": etiqueta}

    def avisar():
        # el texto se compone AL VENCER, en el idioma que la UI tenga en ese
        # momento — un timer puesto en español puede vencer en modo inglés
        if S["idioma"] == "en":
            texto = (f"Charles, your {etiqueta} timer is done." if etiqueta
                     else f"Charles, your {minutos:g} minute timer is done.")
        else:
            texto = (f"Charles, terminó el timer de {etiqueta}." if etiqueta
                     else f"Charles, terminó tu timer de {minutos:g} minutos.")
        with S["lock"]:
            S["avisos"].append(texto)
            if registro in S["timers"]:
                S["timers"].remove(registro)

    with S["lock"]:
        S["timers"].append(registro)
    threading.Timer(minutos * 60, avisar).start()
    return jsonify({"ok": True, "minutos": minutos})


@app.post("/api/salir")
def salir():
    if not S["session_id"]:
        return jsonify({"nota": None})
    if S["ocupado"]:
        return jsonify({"error": "ocupado, esperá el turno actual"}), 409
    S["ocupado"] = True
    S["fase"] = "pensando"
    try:
        nota = escribir_memoria(S["system"], S["session_id"])
        S["session_id"] = None
        despedida = ("Session memory saved to the vault, Charles."
                     if S["idioma"] == "en" else
                     "Memoria de sesión guardada en el vault, Charles.")
        return jsonify({"nota": str(nota.relative_to(VAULT)),
                        "despedida": despedida})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        S["fase"] = "listo"
        S["ocupado"] = False


# ── Arranque ──────────────────────────────────────────────────────────────

def main() -> None:
    ruta = sys.argv[1].strip("/") if len(sys.argv) > 1 else None
    S["system"] = cargar_contexto(ruta) + PERSONA

    # Precalentar Whisper para que el primer turno de mic no pague la carga
    threading.Thread(
        target=lambda: transcribir(np.zeros(SAMPLE_RATE, dtype=np.float32)),
        daemon=True,
    ).start()

    url = "http://localhost:7777"
    extra = f" + {ruta}" if ruta else ""
    print(f"jarvis HUD · {url} · contexto: estrategia{extra}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=7777, debug=False, threaded=True)


if __name__ == "__main__":
    main()
