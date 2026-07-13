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
     el STT entendió al instante, y el turno arranca después.

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

from briefing import (PROMPT_BRIEFING, TITULOS, _clima, datos_briefing,
                      limpiar_markdown, secciones_briefing)
from jarvis_cli import (VAULT, cargar_contexto, escribir_memoria,
                        preguntar_stream)
from jarvis_voz import (PERSONA, RESPELL, SAMPLE_RATE, VOZ, VOZ_RATE, YAP,
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
    "clima": {"es": None, "en": None},  # en AMBOS idiomas (refresco cada 45 min):
                            # el HUD muestra el del toggle actual, no el del fetch
    "paneles": [],          # tarjetas situacionales del HUD {id,tipo,lineas,ts,ttl}
    "paneles_seq": 0,
    "lock": threading.Lock(),
}

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


# ── Paneles situacionales del HUD ─────────────────────────────────────────
#
# Regla de las películas (ver Investigacion-HUD-Paneles-Situacionales en el
# vault): la información aparece cuando se la necesita y se va sola. Los
# datos son deterministas — ya pasan por el server (clima, tool_use del
# stream, briefing): cero tokens extra, el LLM ni se entera. Un panel por
# tipo (el nuevo reemplaza al viejo, como en el cine); viajan por el poll
# de /api/estado y el browser los materializa con scanlines.

def panel(tipo: str, lineas: list[str], ttl: int = 30) -> None:
    lineas = [l.strip()[:64] for l in lineas if l and l.strip()][:6]
    if not lineas:
        return
    with S["lock"]:
        S["paneles_seq"] += 1
        S["paneles"] = [p for p in S["paneles"] if p["tipo"] != tipo]
        S["paneles"].append({"id": S["paneles_seq"], "tipo": tipo,
                             "lineas": lineas, "ts": time.time(), "ttl": ttl})


def _err_ocupado(idioma: str) -> str:
    # los errores visibles en la UI se componen en el idioma del toggle
    # (mismo patrón que el aviso de timer y la despedida, sesión 33)
    return ("busy — wait for the current turn" if idioma == "en"
            else "ocupado, esperá el turno actual")


def _paneles_vivos() -> list[dict]:
    ahora = time.time()
    with S["lock"]:
        S["paneles"] = [p for p in S["paneles"] if ahora - p["ts"] < p["ttl"]]
        return [{"id": p["id"], "tipo": p["tipo"], "lineas": p["lineas"]}
                for p in S["paneles"]]


_CLIMA_PREGUNTA = re.compile(
    r"\b(clima|tiempo|temperatura|lluvia|llover|lloviendo|calor|fr[íi]o|"
    r"weather|temperature|rain|raining|forecast)\b", re.I)


def _clima_loop() -> None:
    """El clima del HUD ya no depende del briefing: se refresca cada 45 min
    desde el arranque, EN AMBOS IDIOMAS (el toggle es/en puede cambiar en
    cualquier momento y "parcialmente nublado" en modo EN desentona).
    Fail-silent: sin red, se queda con el último."""
    while True:
        for idi in ("es", "en"):
            c = _clima(idi)
            if c:
                S["clima"][idi] = c
        time.sleep(45 * 60)


def _panel_web(nombre: str, texto: str) -> None:
    """Resultado de WebSearch/WebFetch → panel con títulos o dominio."""
    if nombre == "WebSearch":
        titulos = re.findall(r'"title"\s*:\s*"([^"]{4,70})"', texto)[:4]
        if titulos:
            panel("web", titulos)
            return
    # WebFetch (o WebSearch sin links parseables): dominio + primera línea
    dominio = re.search(r"https?://([^/\s\"]+)", texto)
    primera = next((l for l in texto.splitlines() if l.strip()), "")
    panel("web", [dominio.group(1) if dominio else "", primera])


def _panel_vault(nombre: str, input_: dict) -> None:
    """Qué está leyendo/buscando en el vault → panel al instante."""
    if nombre == "Read" and input_.get("file_path"):
        panel("vault", [Path(input_["file_path"]).name])
    elif nombre in ("Grep", "Glob") and input_.get("pattern"):
        panel("vault", [f"» {input_['pattern']}"])


# ── Oído único: UN stream de mic para todo el server ─────────────────────
#
# Historia (2026-07-12): antes cada click del mic abría y cerraba su propio
# InputStream, y cada toggle de manos libres levantaba otro más (thread +
# modelo ONNX recargado). En macOS eso es ruleta rusa: PortAudio serializa
# contra CoreAudio con un lock interno y un stop()/open() eventualmente se
# cuelga; los watchdogs de entonces "abandonaban" la llamada colgada — el
# thread quedaba vivo dentro de CoreAudio con el lock tomado y TODA apertura
# posterior del proceso se colgaba para siempre: el mic moría tras unos
# requests y manos libres no volvía hasta reiniciar el server (reproducido
# en vivo: mic/start tardaba exactamente el watchdog y devolvía "no
# respondió", con el server recién usado esa misma mañana).
#
# Diseño nuevo: un solo thread (_audio_daemon) es DUEÑO del único stream.
#   - Se abre a demanda (primer uso) y NO se cierra nunca (close() es justo
#     donde CoreAudio se cuelga; PortAudio limpia al salir). Pero sí se
#     PAUSA: si nadie necesita el mic por PAUSA_OCIOSO segundos, abort()
#     suelta el hardware — el indicador naranja de macOS se apaga (reporte
#     de Charles 2026-07-13: el puntito quedaba fijo tras el primer uso) —
#     y el MISMO stream se reanuda con start() al próximo uso. abort/start
#     sobre un stream vivo es el patrón normal de cualquier app de voz;
#     el veneno era el close()+open() de streams nuevos, y encima
#     concurrente — esto queda serializado en el único daemon.
#   - Push-to-talk ya no abre nada: mic/start|stop solo marcan "grabando"
#     y el daemon acumula los frames (tope 60s — deque circular).
#   - Manos libres corre sobre el mismo feed; el modelo de wake word se
#     carga UNA vez, no en cada toggle.
#   - Si el stream muere (CoreAudio abort, device desconectado), el daemon
#     lo reabre solo con backoff — serializado, sin threads abandonados.
#
# Anti-eco (igual que siempre): la detección se pausa mientras el browser
# reproduce la voz de Jarvis, mientras hay un turno en curso, o si ningún
# browser está mirando (poll viejo).

CHUNK = 1280           # 80 ms @ 16 kHz — lo que espera openwakeword
UMBRAL_WAKE = 0.35     # score para despertar — bajado de 0.5 (2026-07-05) para
                       # que "jarvis" a secas también dispare; si empieza a
                       # despertar por error, subirlo de a 0.05
SILENCIO_FIN = 1.3     # segundos callado = terminaste de hablar — 1.0 cortaba
                       # a Charles a media frase en pausas naturales
MAX_UTTERANCE = 15.0   # tope de captura por turno
ESPERA_HABLA = 6.0     # despertó pero nunca habló → volver a dormir
PAUSA_OCIOSO = 20.0    # sin manos libres ni push-to-talk por tanto tiempo →
                       # abort() del stream (indicador naranja fuera). Gracia
                       # holgada: entre clicks de una conversación normal el
                       # mic ni se pausa — cero churn de start/abort


_audio = {
    "vivo": False,      # stream abierto y leyendo
    "error": None,      # último error de audio — mic/start lo muestra
    "grabando": False,  # push-to-talk: el daemon acumula frames
    # tope 60s: si el mic queda "grabando" minutos (click perdido, tab
    # duplicado), la memoria no crece y se transcribe solo la cola
    "frames": deque(maxlen=int(60 * SAMPLE_RATE / CHUNK)),
}


def _oido_activo() -> bool:
    # el 1.5 tolera el heartbeat de "sigo hablando" (400ms, o ~1s si el tab
    # está en background y el browser lo estrangula).
    # 65s de poll: manos libres se usa justo cuando el HUD NO es el tab
    # visible, y Chrome estrangula los timers de un tab en background hasta
    # 1/min — con 5s el oído se dormía en pleno uso. Sin ningún browser,
    # igual se apaga al minuto de cerrar el último tab.
    return (not S["ocupado"]
            and not _audio["grabando"]                      # sin push-to-talk en curso
            and time.time() - S["hablando_browser"] > 1.5   # jarvis no está sonando
            and time.time() - S["ultimo_poll"] < 65)        # hay un browser vivo


def _cargar_oww():
    """El modelo de wake word, UNA vez por proceso (antes se recargaba en
    cada toggle). Si falla, manos libres se apaga y el toggle reintenta."""
    try:
        from openwakeword.model import Model as OWW
        from openwakeword.utils import download_models
        download_models(["hey_jarvis"])  # no-op si ya están
        return OWW(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    except Exception as e:
        S["manos_error"] = f"openwakeword no cargó: {e}"
        S["manos_libres"] = False
        return None


def _audio_daemon() -> None:
    """Dueño único del mic. Corre desde el boot; duerme hasta el primer uso."""
    stream = None
    pausado = False             # stream abierto pero abort()eado (mic suelto)
    ocioso_desde = None         # cuándo dejó de necesitarse el mic
    oww = None
    espera = 2.0                # backoff de reapertura
    aviso_open = False          # el error de apertura se canta una vez por racha
    piso = deque(maxlen=60)     # RMS del ruido de fondo del cuarto (rolling)
    preroll = deque(maxlen=15)  # ~1.2s de audio previo al wake: sin esto, las
                                # palabras dichas mientras el detector confirma
                                # "jarvis" se pierden y Whisper solo ve la cola

    def umbral_voz() -> float:
        base = sorted(piso)[len(piso) // 2] if piso else 0.0
        return max(base * 3.0, 0.006)

    def soltar(e) -> None:
        # el stream murió (CoreAudio abort, device desconectado, start/abort
        # que falló): soltarlo y dejar que el loop lo reabra con backoff —
        # el server nunca más queda sordo hasta reiniciar. abort() primero:
        # teardown liviano, sin drenar buffers
        nonlocal stream, pausado
        _audio.update(vivo=False, error=f"el mic murió: {e}")
        for cerrar in (stream.abort, stream.close):
            try:
                cerrar()
            except Exception:
                pass
        stream = None
        pausado = False
        S["nivel"] = 0.0
        if S["fase"] in ("escuchando", "transcribiendo"):
            S["fase"] = "listo"
        if S["manos_libres"]:
            S["manos_error"] = f"el oído murió: {e} — reabriendo el mic"

    while True:
        necesitado = S["manos_libres"] or _audio["grabando"]

        if stream is None:
            if not necesitado:
                time.sleep(0.1)  # nadie necesita el mic todavía
                continue
            try:
                st = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="float32", blocksize=CHUNK)
                st.start()
                stream = st
                pausado, ocioso_desde = False, None
                _audio.update(vivo=True, error=None)
                espera, aviso_open = 2.0, False
            except Exception as e:
                _audio.update(vivo=False, error=str(e))
                if S["manos_libres"] and not aviso_open:
                    S["manos_error"] = f"no pude abrir el mic: {e}"
                    aviso_open = True
                time.sleep(espera)
                espera = min(espera * 2, 30)
                continue

        if pausado:
            if not necesitado:
                time.sleep(0.1)  # mic suelto, indicador apagado
                continue
            try:
                stream.start()   # reanudar el MISMO stream: <100ms
                pausado, ocioso_desde = False, None
                _audio.update(vivo=True, error=None)
            except Exception as e:
                soltar(e)        # si reanudar falla, reabrir de cero
                continue

        if not necesitado:
            # nadie usa el mic: tras la gracia, soltar el hardware para que
            # el indicador naranja de macOS se apague. abort(), NO close()
            if ocioso_desde is None:
                ocioso_desde = time.time()
            elif time.time() - ocioso_desde >= PAUSA_OCIOSO:
                try:
                    stream.abort()
                    pausado = True
                    _audio["vivo"] = False
                    S["nivel"] = 0.0
                except Exception as e:
                    soltar(e)
                continue
        else:
            ocioso_desde = None

        # el modelo se carga la primera vez que manos libres lo necesita
        if S["manos_libres"] and oww is None:
            oww = _cargar_oww()  # si falla: apaga manos_libres y avisa

        try:
            datos, _ = stream.read(CHUNK)
            x = datos[:, 0]

            if _audio["grabando"]:            # push-to-talk manda
                with S["lock"]:
                    _audio["frames"].append(x.copy())
                S["nivel"] = float(np.sqrt((x ** 2).mean()))
                continue

            if not (S["manos_libres"] and oww is not None and _oido_activo()):
                if S["nivel"]:
                    S["nivel"] = 0.0          # que no quede nivel fantasma
                continue  # seguir leyendo (buffer fresco) pero sin detectar

            # ── detección "hey jarvis" ──
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
            while S["manos_libres"] and not _audio["grabando"]:
                datos, _ = stream.read(CHUNK)
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

            if _audio["grabando"]:  # click a media captura: el click gana
                S["fase"] = "escuchando"
                continue
            if not hablo:
                S["fase"] = "listo"
                continue
            S["fase"] = "transcribiendo"
            try:
                texto = transcribir(np.concatenate(frames).flatten(),
                                    S["idioma"])
            except Exception as e:
                # STT caído no es razón para tirar el stream: avisar y seguir
                S["manos_error"] = f"no pude transcribir: {e}"
                S["fase"] = "listo"
                continue
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
            soltar(e)


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    # El HUD muestra el oído REAL: la verdad se decide acá al servir la
    # página, no hardcodeada en el HTML (si yap falta, cae a Whisper y el
    # boot lo dice — importa: el repo es público y otros lo van a clonar).
    html = (AQUI / "ui.html").read_text()
    return html.replace("{{OIDOS}}", "apple" if YAP.exists() else "whisper")


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
                    "timers": timers, "clima": S["clima"].get(S["idioma"]),
                    "paneles": _paneles_vivos()})


@app.post("/api/manos_libres")
def manos_libres():
    # solo mueve el flag: el daemon del oído (dueño del stream) reacciona.
    # Antes cada toggle levantaba thread + stream + modelo ONNX nuevos —
    # el churn que terminaba trabando CoreAudio.
    S["manos_libres"] = bool((request.json or {}).get("on"))
    if S["manos_libres"]:
        S["manos_error"] = None
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
    """Ya no abre ningún stream: marca "grabando" y el daemon del oído
    acumula los frames del stream único (así se acabó el open/close por
    click que trababa CoreAudio). Idempotente: si ya estaba grabando (tab
    viejo, click perdido), reinicia la captura en vez de fallar — el click
    del usuario siempre gana."""
    if _audio["grabando"]:
        with S["lock"]:
            _audio["frames"].clear()
        S["fase"] = "escuchando"
        return jsonify({"ok": True, "reiniciado": True})
    with S["lock"]:
        _audio["frames"].clear()
    _audio["error"] = None     # intento fresco: que no mande un error viejo
    _audio["grabando"] = True  # esto despierta el open del daemon si hace falta
    t0 = time.time()
    while time.time() - t0 < 5:  # el open sano tarda <1s; margen de 5s
        if _audio["vivo"]:
            S["fase"] = "escuchando"
            return jsonify({"ok": True})
        if _audio["error"]:
            break
        time.sleep(0.05)
    _audio["grabando"] = False
    return jsonify({"error": _audio["error"] or (
        "el mic no respondió — corré Jarvis desde tu terminal "
        "(permiso de micrófono de macOS)")}), 500


@app.post("/api/mic/stop")
def mic_stop():
    """Corta la grabación y devuelve SOLO la transcripción; el turno lo
    dispara el browser contra /api/stream con este texto. Sin stop()/close()
    de ningún stream: solo se baja el flag y se recogen los frames."""
    if not _audio["grabando"]:
        # los errores visibles en la UI se componen en el idioma del toggle
        # (mismo patrón que el aviso de timer y la despedida, sesión 33)
        return jsonify({"error": "wasn't recording" if S["idioma"] == "en"
                        else "no estaba grabando"}), 409
    _audio["grabando"] = False
    # ojo: nivel NO se toca acá — el único escritor es el daemon (si lo
    # pisáramos, su chunk en vuelo lo revive); él lo baja a 0 en ≤80ms
    with S["lock"]:  # el daemon pudo estar a media append
        frames = list(_audio["frames"])
        _audio["frames"].clear()

    S["fase"] = "transcribiendo"
    try:
        # el deque ya limita a 60s: si el mic quedó "grabando" minutos
        # (click perdido, tab duplicado), se transcribe solo la cola
        audio = (np.concatenate(frames).flatten()
                 if frames else np.zeros(0, dtype=np.float32))
        texto = transcribir(audio, S["idioma"])
    finally:
        S["fase"] = "listo"
    if not texto:
        return jsonify({"error": "didn't hear anything" if S["idioma"] == "en"
                        else "no escuché nada"})
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
        secciones = secciones_briefing(VAULT, idioma)
        datos = "\n\n".join(f"· {t}:\n{c}" for t, c, _ in secciones if c)
        titulo_clima = TITULOS.get(idioma, TITULOS["es"])["clima"]
        clima = next((c for t, c, _ in secciones if t == titulo_clima), None)
        if clima:
            S["clima"][idioma] = clima  # de paso, al HUD (barra de telemetría)
            panel("clima", [clima], ttl=90)
        # tarjeta de agenda: las secciones del briefing SIN el clima (tiene
        # tarjeta propia) y SIN la prosa que no esté en el idioma de la UI —
        # el vault es español, así que en modo EN capturas/proyectos salían
        # en español dentro de la tarjeta (el bug que reportó Charles). El
        # cerebro igual las narra traducidas; a la tarjeta solo va lo que ya
        # está en el idioma correcto (fecha, entregas de Canvas).
        # limpiar_markdown: la prosa del vault trae callouts/negritas/
        # wikilinks que en la tarjeta se veían crudos (feo para el look de
        # película); solo presentación — `datos` (el cerebro) va original
        lineas_tarjeta = [ln for t, c, en_idioma_ui in secciones
                          if c and en_idioma_ui and t != titulo_clima
                          for ln in (f"· {t}:",
                                     *limpiar_markdown(c).splitlines())
                          if ln.strip()]
        panel("agenda", lineas_tarjeta, ttl=90)
        entrada = PROMPT_BRIEFING.get(idioma, PROMPT_BRIEFING["es"]) \
            .format(datos=datos)
    elif entrada and _CLIMA_PREGUNTA.search(entrada) and S["clima"].get(idioma):
        # preguntó por el clima: la tarjeta se materializa mientras responde
        panel("clima", [S["clima"][idioma]])
    if not entrada:
        return jsonify({"error": "vacío"}), 400
    if S["ocupado"]:
        return jsonify({"error": _err_ocupado(idioma)}), 409

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
                    "persona, same dry wit. Say \"sir\" at most ONCE in this "
                    "reply, never twice. Don't mention this note.]")
    else:
        entrada += ("\n\n[Modo de idioma de la UI: ESPAÑOL — respondé SOLO "
                    "en español este turno, te hablen en el idioma que te "
                    "hablen. Misma persona. Decí «señor» como máximo UNA vez "
                    "en esta respuesta, nunca dos. No menciones esta nota.]")

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
                    # la frase de anuncio ("Dale, la pongo.") quedaba PRESA
                    # en `pendiente` hasta después de la herramienta: el
                    # corte de oraciones espera texto posterior que no llega
                    # — lo siguiente es el tool_use — y el "on it" sonaba
                    # con la acción ya hecha, pegado al "listo" (reporte de
                    # Charles 2026-07-12). Al arrancar una mano se suelta lo
                    # acumulado: se dice MIENTRAS la herramienta corre.
                    if pendiente.strip():
                        yield _sse({"tipo": "oracion", "texto": pendiente})
                        pendiente = ""
                        primera = False
                    yield _sse({"tipo": "mano", "texto": ev[1]})
                elif ev[0] == "mano_uso":
                    _panel_vault(ev[1], ev[2])   # qué nota lee / qué busca
                elif ev[0] == "mano_result":
                    if ev[1] in ("WebSearch", "WebFetch"):
                        _panel_web(ev[1], ev[2])  # títulos / dominio hallados
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
            texto = (f"Your {etiqueta} timer is done, sir." if etiqueta
                     else f"Your {minutos:g} minute timer is done, sir.")
        else:
            texto = (f"Terminó el timer de {etiqueta}, señor." if etiqueta
                     else f"Terminó tu timer de {minutos:g} minutos, señor.")
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
        return jsonify({"error": _err_ocupado(S["idioma"])}), 409
    S["ocupado"] = True
    S["fase"] = "pensando"
    try:
        nota = escribir_memoria(S["system"], S["session_id"])
        S["session_id"] = None
        despedida = ("Session memory saved to the vault, sir."
                     if S["idioma"] == "en" else
                     "Memoria de sesión guardada en el vault, señor.")
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

    # Precalentar el STT: con yap pre-verifica binario y asset; sin yap
    # carga el modelo de Whisper para que el primer turno no pague eso
    threading.Thread(
        target=lambda: transcribir(np.zeros(SAMPLE_RATE, dtype=np.float32)),
        daemon=True,
    ).start()
    # oído único: el daemon dueño del mic arranca ya (duerme hasta el 1er uso)
    threading.Thread(target=_audio_daemon, daemon=True).start()
    # clima siempre fresco en el HUD (antes solo lo traía el briefing del día)
    threading.Thread(target=_clima_loop, daemon=True).start()

    url = "http://localhost:7777"
    extra = f" + {ruta}" if ruta else ""
    print(f"jarvis HUD · {url} · contexto: estrategia{extra}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=7777, debug=False, threaded=True)


if __name__ == "__main__":
    main()
