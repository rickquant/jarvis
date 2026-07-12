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

# La tarjeta HOY/TODAY del HUD muestra estos datos CRUDOS (no pasan por el
# cerebro), así que los rótulos y fechas se componen en el idioma de la UI —
# con rótulos hardcodeados en español, el panel salía mixto en modo EN.
# El contenido del vault (Current-State, capturas) sigue en español: eso sí
# lo traduce el cerebro al narrar, según instruye PROMPT_BRIEFING["en"].
DIAS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
           "Saturday", "Sunday"]
MESES_EN = ["January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"]
TITULOS = {
    "es": {"fecha": "fecha y hora", "clima": f"clima en {CIUDAD}",
           "canvas": "entregas de la U (Canvas)",
           "recordatorios": "recordatorios para hoy",
           "capturas": "capturas recientes por voz",
           "inbox": "notas sin procesar en el Inbox",
           "proyectos": "proyectos: dónde quedamos"},
    "en": {"fecha": "date and time", "clima": f"weather in {CIUDAD}",
           "canvas": "university deadlines (Canvas)",
           "recordatorios": "reminders for today",
           "capturas": "recent voice captures",
           "inbox": "unprocessed Inbox notes",
           "proyectos": "projects: where we left off"},
}


def _fecha_larga(a: datetime, idioma: str) -> str:
    if idioma == "en":
        return (f"{DIAS_EN[a.weekday()]}, {MESES_EN[a.month - 1]} {a.day}, "
                f"{a.year}, {a:%H:%M}")
    return (f"{DIAS[a.weekday()]} {a.day} de {MESES[a.month - 1]} de "
            f"{a.year}, {a:%H:%M}")

# El prompt entero cambia con el idioma de la UI: la nota per-turn de
# idioma alcanza para turnos normales (cortos), pero acá el prompt + los
# datos son un bloque grande en español y la nota quedaba ahogada — en
# modo EN el briefing salía en español igual. Instrucción en inglés,
# arriba y explícita = el modelo obedece.
PROMPT_BRIEFING = {
    "es": (
        "Es el primer arranque del día y la intro de boot ya sonó — NO saludes "
        "de nuevo ni te presentes: arrancá directo con el briefing del día, "
        "como J.A.R.V.I.S. se lo daría a Tony. Breve (6-10 oraciones), al "
        "grano, con tu ingenio seco. Orden sugerido: clima en una frase → "
        "entregas o urgencias → pendientes que valgan la pena mencionar → "
        "próximo paso sugerido del proyecto activo. No listes todo: elegí lo "
        "que importa HOY y descartá el resto. Nada de markdown ni viñetas: "
        "esto se dice en voz alta.\n\n=== DATOS DEL DÍA ===\n{datos}"
    ),
    "en": (
        "Deliver this ENTIRE briefing in English — every single sentence. "
        "The data below is in Spanish because the vault is in Spanish: "
        "translate whatever you use, quote nothing verbatim. It's the first "
        "boot of the day and the boot intro already played — do NOT greet "
        "again or introduce yourself: go straight into the day's briefing, "
        "the way J.A.R.V.I.S. would give it to Tony. Brief (6-10 sentences), "
        "to the point, with your dry wit. Suggested order: weather in one "
        "sentence → deadlines or urgent items → pending notes worth "
        "mentioning → suggested next step on the active project. Don't list "
        "everything: pick what matters TODAY and drop the rest. No markdown, "
        "no bullets: this is spoken out loud.\n\n=== TODAY'S DATA ===\n{datos}"
    ),
}


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
    capturas (van aparte), las memorias de sesión de Jarvis (ruido) y el
    README de la carpeta (documenta la estructura, no es pendiente)."""
    carpeta = vault / "00-Inbox"
    if not carpeta.is_dir():
        return None
    notas = [f.stem for f in sorted(carpeta.glob("*.md"))
             if not f.name.startswith(("Capturas-Jarvis", "Jarvis-Sesion",
                                       "Recordatorios-Jarvis", "README"))]
    return ", ".join(notas[:12]) or None


def _recordatorios(vault: Path) -> str | None:
    """Recordatorios con fecha de hoy o vencida (los escribe manos.py
    recordar). El formato de línea es "- [ ] YYYY-MM-DD — texto": la fecha
    en posición fija permite comparar como texto. Los tachados ([x]) y los
    futuros no se cantan."""
    f = vault / "00-Inbox" / "Recordatorios-Jarvis.md"
    if not f.is_file():
        return None
    hoy = f"{datetime.now():%Y-%m-%d}"
    lineas = [ln[6:].strip() for ln in
              f.read_text(encoding="utf-8").splitlines()
              if ln.startswith("- [ ] ") and ln[6:16] <= hoy]
    return "\n".join(f"- {ln}" for ln in lineas[:8]) or None


def limpiar_markdown(texto: str) -> str:
    """Prosa del vault → texto plano para la tarjeta del HUD: el markdown
    crudo (callouts de Obsidian, negritas, wikilinks) se ve feo en el look
    de película. Solo presentación — el cerebro recibe el original."""
    t = re.sub(r"^>\s?\[!\w+\][ \t]*", "", texto, flags=re.M)  # > [!todo]
    t = re.sub(r"^>\s?", "", t, flags=re.M)                # > blockquote
    t = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", t)  # [[nota|alias]]
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)               # **negrita**
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", t)    # *cursiva*
    t = re.sub(r"`([^`]+)`", r"\1", t)                     # `código`
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.M)           # ## encabezados
    return t


def _proximos_pasos(vault: Path) -> str | None:
    """El bloque "Dónde quedamos / próximo paso" del Current-State de cada
    proyecto activo — el protocolo de memoria del vault lo mantiene al día."""
    piezas: list[str] = []
    for cs in sorted(vault.glob("01-Projects/*/Current-State.md")):
        cuerpo = cs.read_text(encoding="utf-8")
        m = re.search(r"^## Dónde quedamos.*?\n(.*?)(?=^## |\Z)",
                      cuerpo, re.M | re.S)
        if m and m.group(1).strip():
            bloque = m.group(1).strip()
            if len(bloque) > 700:  # cortar en palabra completa, no a la mitad
                bloque = bloque[:700].rsplit(None, 1)[0] + " …"
            piezas.append(f"[{cs.parent.name}]\n{bloque}")
    return "\n\n".join(piezas) or None


def _canvas(idioma: str = "es") -> str | None:
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
            return ("no upcoming deadlines — clear week" if idioma == "en"
                    else "sin entregas próximas — semana despejada")
        if idioma == "en":
            return "\n".join(
                f"- {nombre} ({materia}) — due {DIAS_EN[d.weekday()]}, "
                f"{MESES_EN[d.month - 1]} {d.day}, {d:%H:%M}"
                for dt, nombre, materia in proximas[:6]
                for d in [dt.astimezone()])
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


def secciones_briefing(vault: Path,
                       idioma: str = "es") -> list[tuple[str, str, bool]]:
    """Las secciones del día como (titulo, cuerpo, en_idioma_ui).

    `en_idioma_ui` marca si el CONTENIDO está en el idioma de la UI:
    - fecha/clima/canvas se generan acá en el idioma de la UI → True.
    - capturas/inbox/proyectos son prosa CRUDA del vault (español) → solo
      es True cuando la UI está en español. El cerebro las narra traducidas
      igual; el flag es para la tarjeta del HUD, que las muestra crudas y en
      modo EN salían en español (el bug que reportó Charles).
    """
    a = datetime.now()
    t = TITULOS.get(idioma, TITULOS["es"])
    vault_ui = idioma == "es"  # el vault está en español
    return [
        (t["fecha"], _fecha_larga(a, idioma), True),
        (t["clima"], _clima(idioma), True),
        (t["canvas"], _canvas(idioma), True),
        (t["recordatorios"], _recordatorios(vault), vault_ui),
        (t["capturas"], _capturas(vault), vault_ui),
        (t["inbox"], _inbox(vault), vault_ui),
        (t["proyectos"], _proximos_pasos(vault), vault_ui),
    ]


def datos_briefing(vault: Path, idioma: str = "es") -> tuple[str, str | None]:
    """Junta todo el contexto del día en un bloque para el cerebro.
    Devuelve (datos, clima) — el clima va aparte porque el HUD también
    lo muestra en la barra de telemetría. El cerebro recibe TODO (traduce
    lo que use); el filtro por idioma es solo para la tarjeta (ver
    secciones_briefing / jarvis_ui)."""
    secciones = secciones_briefing(vault, idioma)
    clima = next((c for tit, c, _ in secciones
                  if tit == TITULOS.get(idioma, TITULOS["es"])["clima"]), None)
    return ("\n\n".join(f"· {titulo}:\n{cuerpo}"
                        for titulo, cuerpo, _ in secciones if cuerpo), clima)


if __name__ == "__main__":
    # prueba directa: python3 briefing.py [ruta-al-vault]
    vault = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parents[3]
    print(PROMPT_BRIEFING["es"].format(datos=datos_briefing(vault)[0]))
