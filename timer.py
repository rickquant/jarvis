#!/usr/bin/env python3
"""
Mano de Jarvis: poner un timer.

    python3 timer.py <minutos> ["etiqueta"]     # ej: python3 timer.py 20 "el arroz"

No duerme acá (bloquearía el turno): le pide al server del HUD (jarvis_ui)
que programe el aviso — cuando vence, el browser suena un bip y Jarvis lo
dice por voz. Existe como script para que la allowlist del cerebro sea un
prefijo exacto (`python3 timer.py`), no un `curl` genérico.
"""

import json
import sys
import urllib.request

if len(sys.argv) < 2:
    sys.exit("uso: python3 timer.py <minutos> [etiqueta]")

try:
    minutos = float(sys.argv[1])
except ValueError:
    sys.exit(f"minutos inválidos: {sys.argv[1]!r}")
etiqueta = sys.argv[2] if len(sys.argv) > 2 else ""

req = urllib.request.Request(
    "http://localhost:7777/api/timer",
    data=json.dumps({"minutos": minutos, "etiqueta": etiqueta}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        data = json.load(r)
except Exception as e:
    sys.exit(f"no pude poner el timer — ¿el HUD (jarvis_ui) está corriendo?: {e}")

if data.get("error"):
    sys.exit(f"el server rechazó el timer: {data['error']}")
print(f"timer puesto: {minutos:g} min" + (f" ({etiqueta})" if etiqueta else ""))
