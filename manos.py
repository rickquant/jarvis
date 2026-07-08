#!/usr/bin/env python3
"""
Manos de Jarvis: acciones del Mac detrás de UN prefijo allowlisteado.

    python3 manos.py cerrar "Spotify"            # quit ordenado de una app
    python3 manos.py musica play|pausa|siguiente|anterior   # controla Spotify
    python3 manos.py cancion "nombre y artista"  # busca la canción y la pone en Spotify
    python3 manos.py volumen <0-100>             # volumen del sistema
    python3 manos.py nota "texto a capturar"     # captura rápida → 00-Inbox
    python3 manos.py hora                        # fecha y hora actual
    python3 manos.py tab ["https://…"]           # tab nuevo en Safari (URL opcional)
    python3 manos.py url "https://…"             # abre un URL en el browser default

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


def _buscar_track(consulta: str) -> str | None:
    """Track ID de Spotify SIN API key: primer resultado de DuckDuckGo lite
    restringido a open.spotify.com/track. Scraping frágil por diseño — si un
    día deja de funcionar, el caller cae al fallback (abrir la búsqueda
    dentro de Spotify), nunca a un error."""
    import re
    from urllib.parse import quote_plus
    from urllib.request import Request, urlopen

    url = ("https://lite.duckduckgo.com/lite/?q="
           + quote_plus(f"site:open.spotify.com/track {consulta}"))
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
    try:
        html = urlopen(Request(url, headers={"User-Agent": ua}),
                       timeout=8).read().decode("utf-8", "replace")
    except Exception:
        return None
    # los links de DDG vienen como redirect con el URL real percent-encoded
    m = re.search(r"open\.spotify\.com(?:%2F|/)track(?:%2F|/)([A-Za-z0-9]{22})",
                  html)
    return m.group(1) if m else None


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

    elif mano in ("cancion", "canción") and args:
        consulta = " ".join(args).replace('"', "").strip()
        tid = _buscar_track(consulta)
        if tid:
            osa(f'tell application "Spotify" to play track "spotify:track:{tid}"')
            print(f"sonando en Spotify: {consulta}")
        else:
            # sin match: al menos dejarlo a un click — abre la búsqueda en la app
            subprocess.run(["open", f"spotify:search:{consulta}"], check=False)
            print(f"no encontré el track exacto de {consulta!r}; "
                  "dejé abierta la búsqueda en Spotify")

    elif mano == "tab":
        url = (args[0] if args else "").replace('"', "").strip()
        if url and not url.startswith(("http://", "https://")):
            sys.exit(f"tab: URL inválida (solo http/https): {url!r}")
        props = f' with properties {{URL:"{url}"}}' if url else ""
        osa(f'''tell application "Safari"
    activate
    if (count of windows) = 0 then
        make new document{props}
    else
        tell window 1 to set current tab to (make new tab{props})
    end if
end tell''')
        print("tab nuevo en Safari" + (f" → {url}" if url else ""))

    elif mano == "url" and args:
        url = args[0].replace('"', "").strip()
        if not url.startswith(("http://", "https://")):
            sys.exit(f"url inválida (solo http/https): {url!r}")
        subprocess.run(["open", url], check=False)
        print(f"abierto en el browser: {url}")

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
