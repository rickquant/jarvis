#!/usr/bin/env python3
"""
Briefing proactivo de Jarvis: el "buenos días, señor" con sustancia.

La recolección de datos es código determinístico, no LLM — un solo
round-trip del cerebro convierte el bloque en el briefing hablado del
primer boot del día:

    clima (wttr.in, gratis sin key) + capturas y notas del Inbox +
    próximos pasos de los proyectos activos + entregas de Canvas
    (si el repo canvas-automation está a mano — cruza los 2 proyectos)

Todo es fail-silent: sin red no hay clima, sin Canvas no hay entregas,
pero el briefing sale igual con lo que haya.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manos import DIAS, MESES

CIUDAD = "Tegucigalpa"

PROMPT_BRIEFING = (
    "Es el primer arranque del día y la intro de boot ya sonó — NO saludes "
    "de nuevo ni te presentes: arrancá directo con el briefing del día, "
    "como J.A.R.V.I.S. se lo daría a Tony. Breve (6-10 oraciones), al "
    "grano, con tu ingenio seco. Orden sugerido: clima en una frase → "
    "entregas o urgencias → pendientes que valgan la pena mencionar → "
    "próximo paso sugerido del proyecto activo. No listes todo: elegí lo "
    "que importa HOY y descartá el resto. Nada de markdown ni viñetas: "
    "esto se dice en voz alta.\n\n=== DATOS DEL DÍA ===\n{datos}"
)


def _clima(idioma: str = "es") -> str | None:
    """wttr.in con format-string: una línea, sin key. Sin red → None."""
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(CIUDAD)
    url = f"https://wttr.in/{q}?format=%C,+%t&lang={idioma}&m"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            texto = r.read().decode("utf-8", "ignore").strip()
        return texto if texto and "Unknown" not in texto else None
    except Exception:
        return None


def _capturas(vault: Path) -> str | None:
    """Capturas de voz de las últimas 48h (las escribe manos.py nota)."""
    lineas: list[str] = []
    hoy = datetime.now()
    for d in (hoy - timedelta(days=1), hoy):
        f = vault / "00-Inbox" / f"Capturas-Jarvis-{d:%Y-%m-%d}.md"
        if f.exists():
            lineas += [ln for ln in f.read_text(encoding="utf-8").splitlines()
                       if ln.startswith("- **")]
    return "\n".join(lineas[-8:]) or None


def _inbox(vault: Path) -> str | None:
    """Notas sueltas del Inbox — pendientes de procesar. Se excluyen las
    capturas (van aparte) y las memorias de sesión de Jarvis (ruido)."""
    carpeta = vault / "00-Inbox"
    if not carpeta.is_dir():
        return None
    notas = [f.stem for f in sorted(carpeta.glob("*.md"))
             if not f.name.startswith(("Capturas-Jarvis", "Jarvis-Sesion"))]
    return ", ".join(notas[:12]) or None


def _proximos_pasos(vault: Path) -> str | None:
    """El bloque "Dónde quedamos / próximo paso" del Current-State de cada
    proyecto activo — el protocolo de memoria del vault lo mantiene al día."""
    piezas: list[str] = []
    for cs in sorted(vault.glob("01-Projects/*/Current-State.md")):
        cuerpo = cs.read_text(encoding="utf-8")
        m = re.search(r"^## Dónde quedamos.*?\n(.*?)(?=^## |\Z)",
                      cuerpo, re.M | re.S)
        if m and m.group(1).strip():
            piezas.append(f"[{cs.parent.name}]\n{m.group(1).strip()[:700]}")
    return "\n\n".join(piezas) or None


def _canvas() -> str | None:
    """Entregas próximas de la U vía el repo canvas-automation (la otra
    pieza del portafolio). Se busca el repo en $JARVIS_CANVAS o en rutas
    típicas, se carga su .env a mano y se usa su propia canvas_lib —
    misma lógica que canvas_notify.py, sin duplicar el cliente."""
    candidatos = ([Path(os.environ["JARVIS_CANVAS"]).expanduser()]
                  if os.environ.get("JARVIS_CANVAS") else [])
    candidatos += [Path.home() / d / "canvas-automation"
                   for d in ("Projects", "Desktop", "Documents", ".")]
    repo = next((c for c in candidatos if (c / "canvas_lib.py").is_file()),
                None)
    if repo is None:
        return None
    env = repo / ".env"
    if env.is_file():
        for linea in env.read_text(encoding="utf-8").splitlines():
            k, sep, v = linea.partition("=")
            if sep and not linea.lstrip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

    sys.path.insert(0, str(repo))
    try:
        from canvas_lib import canvas_get, cursos_trimestre_actual
        cursos, _ = cursos_trimestre_actual()
        ahora = datetime.now(timezone.utc)
        proximas = []
        for curso in cursos:
            for a in canvas_get(f"courses/{curso['id']}/assignments",
                                params={"bucket": "upcoming"}):
                due = a.get("due_at")
                if not due:
                    continue
                dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if dt >= ahora:
                    proximas.append((dt, a.get("name", "¿?"),
                                     curso.get("name", "")))
        proximas.sort(key=lambda x: x[0])
        if not proximas:
            return "sin entregas próximas — semana despejada"
        return "\n".join(
            f"- {nombre} ({materia}) — vence {DIAS[d.weekday()]} "
            f"{d.day} de {MESES[d.month - 1]}, {d:%H:%M}"
            for dt, nombre, materia in proximas[:6]
            for d in [dt.astimezone()])
    except BaseException:
        # SystemExit incluido: require_env corta con sys.exit si faltan
        # credenciales — acá eso significa "sin Canvas", no morirse
        return None
    finally:
        sys.path.remove(str(repo))


def datos_briefing(vault: Path, idioma: str = "es") -> str:
    """Junta todo el contexto del día en un bloque para el cerebro."""
    a = datetime.now()
    secciones = [("fecha y hora", f"{DIAS[a.weekday()]} {a.day} de "
                                  f"{MESES[a.month - 1]} de {a.year}, {a:%H:%M}"),
                 (f"clima en {CIUDAD}", _clima(idioma)),
                 ("entregas de la U (Canvas)", _canvas()),
                 ("capturas recientes por voz", _capturas(vault)),
                 ("notas sin procesar en el Inbox", _inbox(vault)),
                 ("proyectos: dónde quedamos", _proximos_pasos(vault))]
    return "\n\n".join(f"· {titulo}:\n{cuerpo}"
                       for titulo, cuerpo in secciones if cuerpo)


if __name__ == "__main__":
    # prueba directa: python3 briefing.py [ruta-al-vault]
    vault = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parents[3]
    print(PROMPT_BRIEFING.format(datos=datos_briefing(vault)))
