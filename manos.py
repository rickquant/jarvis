#!/usr/bin/env python3
"""
Manos de Jarvis: acciones del Mac detrás de UN prefijo allowlisteado.

    python3 manos.py cerrar "Spotify"            # quit ordenado de una app
    python3 manos.py musica play|pausa|siguiente|anterior   # controla Spotify
    python3 manos.py volumen <0-100>             # volumen del sistema
    python3 manos.py nota "texto a capturar"     # captura rápida → 00-Inbox
    python3 manos.py hora                        # fecha y hora actual

El menú fijo ES la seguridad: la allowlist del cerebro permite el prefijo
`python3 manos.py` y este script solo sabe hacer esto — nada de osascript
libre ni comandos arbitrarios. Para agregar una mano, se agrega acá y se
documenta en la sección MANOS de jarvis_cli.py.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parents[3]

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        sys.exit(f"osascript falló: {r.stderr.strip()}")
    return r.stdout.strip()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mano, args = sys.argv[1].lower(), sys.argv[2:]

    if mano == "cerrar" and args:
        app = args[0].replace('"', "")  # sin comillas: van dentro del script
        osa(f'tell application "{app}" to quit')
        print(f"{app} cerrada")

    elif mano in ("musica", "música") and args:
        orden = {"play": "play", "pausa": "pause", "pause": "pause",
                 "siguiente": "next track", "anterior": "previous track",
                 }.get(args[0].lower())
        if not orden:
            sys.exit("música: play | pausa | siguiente | anterior")
        osa(f'tell application "Spotify" to {orden}')
        print(f"música: {args[0].lower()}")

    elif mano == "volumen" and args:
        try:
            v = max(0, min(100, int(float(args[0]))))
        except ValueError:
            sys.exit(f"volumen inválido: {args[0]!r}")
        osa(f"set volume output volume {v}")
        print(f"volumen al {v}%")

    elif mano == "nota" and args:
        texto = " ".join(args).strip()
        ahora = datetime.now()
        nota = VAULT / "00-Inbox" / f"Capturas-Jarvis-{ahora:%Y-%m-%d}.md"
        encabezado = ("" if nota.exists() else
                      "---\ntags: [inbox, jarvis, captura]\n"
                      f"fecha: {ahora:%Y-%m-%d}\n---\n\n"
                      f"# Capturas de Jarvis — {ahora:%Y-%m-%d}\n\n")
        with nota.open("a", encoding="utf-8") as f:
            f.write(f"{encabezado}- **{ahora:%H:%M}** — {texto}\n")
        print(f"anotado en 00-Inbox/{nota.name}")

    elif mano == "hora":
        a = datetime.now()
        print(f"{DIAS[a.weekday()]} {a.day} de {MESES[a.month - 1]} "
              f"de {a.year}, {a:%H:%M}")

    else:
        sys.exit(f"mano desconocida: {mano}\n\n{__doc__}")


if __name__ == "__main__":
    main()
