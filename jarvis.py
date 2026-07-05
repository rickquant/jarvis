#!/usr/bin/env python3
"""
Jarvis — Fase 2, MVP del loop de texto.

El loop (ver [[_Jarvis]]):
    leer contexto del vault  →  Claude API  →  responder  →  escribir memoria al vault

Uso:
    python3 jarvis.py                      # carga solo la capa de estrategia (nivel naive)
    python3 jarvis.py 01-Projects/Jarvis   # + notas de esa carpeta (nivel ruteado)

Comandos dentro del chat:
    /salir   termina la sesión y escribe la memoria al vault (00-Inbox/)
    /costo   muestra tokens y costo acumulado de la sesión

Requiere ANTHROPIC_API_KEY en el entorno.
"""

import sys
from datetime import datetime
from pathlib import Path

import anthropic

# ── Config ────────────────────────────────────────────────────────────────
# El cerebro es intercambiable: cambiá MODEL y listo. El contexto es lo que
# hace útil a Jarvis, no el modelo.
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

# Precios USD por millón de tokens (para el tracking de costo de sesión)
PRECIOS = {
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00},
    "claude-fable-5": {"in": 10.00, "out": 50.00},
}

# El vault es la raíz del repo de memoria. Este archivo vive en
# 01-Projects/Jarvis/code/, así que la raíz está 3 niveles arriba.
VAULT = Path(__file__).resolve().parents[3]
INBOX = VAULT / "00-Inbox"

PROMPT_RESUMEN = """\
La sesión terminó. Escribí una nota de memoria para el vault, en markdown, con:

1. **Qué se habló** — los temas, en 2-4 bullets.
2. **Decisiones o conclusiones** — si las hubo.
3. **Pendientes / próximo paso** — si quedó algo abierto.

Sé concreto y breve. Es una nota para retomar contexto en la próxima sesión,
no una transcripción. No agregues encabezado de título (ya lo pone el sistema).
"""


# ── Capa de memoria: lectura ──────────────────────────────────────────────

def cargar_contexto(ruta_proyecto: str | None) -> list[dict]:
    """Arma el system prompt desde el vault.

    Nivel naive: siempre carga el CLAUDE.md raíz (capa de estrategia).
    Nivel ruteado: si se pasa una carpeta, suma sus notas .md (capa técnica).
    Devuelve bloques de system con cache_control en el último — el contexto
    es estable durante la sesión, así que se cachea entero.
    """
    estrategia = (VAULT / "CLAUDE.md").read_text(encoding="utf-8")
    bloques = [{
        "type": "text",
        "text": (
            "Sos Jarvis, el asistente personal de Charles. Tu memoria es su "
            "vault de Obsidian; el contexto de abajo viene de ahí.\n\n"
            "=== CAPA DE ESTRATEGIA (CLAUDE.md raíz del vault) ===\n\n"
            + estrategia
        ),
    }]

    if ruta_proyecto:
        carpeta = VAULT / ruta_proyecto
        if not carpeta.is_dir():
            sys.exit(f"error: no existe la carpeta '{ruta_proyecto}' en el vault")
        notas = sorted(carpeta.glob("*.md"))
        if not notas:
            sys.exit(f"error: '{ruta_proyecto}' no tiene notas .md")
        cuerpo = "\n\n".join(
            f"--- {n.relative_to(VAULT)} ---\n{n.read_text(encoding='utf-8')}"
            for n in notas
        )
        bloques.append({
            "type": "text",
            "text": f"=== CONTEXTO DEL PROYECTO ({ruta_proyecto}) ===\n\n{cuerpo}",
        })

    # Breakpoint de caché en el último bloque: cachea todo el prefijo estable.
    bloques[-1]["cache_control"] = {"type": "ephemeral"}
    return bloques


# ── Capa de memoria: escritura ────────────────────────────────────────────

def escribir_memoria(client: anthropic.Anthropic, system: list[dict],
                     historial: list[dict]) -> Path | None:
    """Cierra el loop: pide un resumen de la sesión y lo escribe a 00-Inbox/.

    Va al Inbox (zona de captura) y no directo a Current-State: la memoria
    automática se revisa antes de promoverla — control humano sobre qué
    entra al vault en limpio.
    """
    if not historial:
        return None

    resumen = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,  # mismo prefijo → aprovecha el caché de la sesión
        messages=historial + [{"role": "user", "content": PROMPT_RESUMEN}],
    )
    texto = "".join(b.text for b in resumen.content if b.type == "text")

    ahora = datetime.now()
    nota = INBOX / f"Jarvis-Sesion-{ahora:%Y-%m-%d-%H%M}.md"
    nota.write_text(
        "---\n"
        f"tags: [jarvis, sesion]\n"
        f"fecha: {ahora:%Y-%m-%d}\n"
        f"modelo: {MODEL}\n"
        "---\n\n"
        f"# Sesión con Jarvis — {ahora:%Y-%m-%d %H:%M}\n\n"
        f"{texto.strip()}\n",
        encoding="utf-8",
    )
    return nota


# ── Loop principal ────────────────────────────────────────────────────────

def main() -> None:
    ruta = sys.argv[1].strip("/") if len(sys.argv) > 1 else None
    system = cargar_contexto(ruta)
    client = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY del entorno

    tokens = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}

    def costo() -> float:
        p = PRECIOS[MODEL]
        # cache write ≈ 1.25x input, cache read ≈ 0.1x input
        return (
            tokens["in"] * p["in"]
            + tokens["cache_write"] * p["in"] * 1.25
            + tokens["cache_read"] * p["in"] * 0.10
            + tokens["out"] * p["out"]
        ) / 1_000_000

    extra = f" + {ruta}" if ruta else ""
    print(f"jarvis listo · modelo: {MODEL} · contexto: capa de estrategia{extra}")
    print("(/salir para terminar y guardar memoria, /costo para ver el gasto)\n")

    historial: list[dict] = []
    try:
        while True:
            try:
                entrada = input("vos > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not entrada:
                continue
            if entrada.lower() in {"/salir", "salir", "exit", "quit"}:
                break
            if entrada.lower() == "/costo":
                print(f"  tokens in/out: {tokens['in']}/{tokens['out']} · "
                      f"cache r/w: {tokens['cache_read']}/{tokens['cache_write']} · "
                      f"~${costo():.4f} USD\n")
                continue

            historial.append({"role": "user", "content": entrada})
            print("\njarvis > ", end="", flush=True)
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=system,
                messages=historial,
            ) as stream:
                for texto in stream.text_stream:
                    print(texto, end="", flush=True)
                final = stream.get_final_message()
            print("\n")

            # El content completo (incl. bloques de thinking) vuelve al
            # historial — la API lo exige para continuar el turno.
            historial.append({"role": "assistant", "content": final.content})

            u = final.usage
            tokens["in"] += u.input_tokens
            tokens["out"] += u.output_tokens
            tokens["cache_read"] += u.cache_read_input_tokens or 0
            tokens["cache_write"] += u.cache_creation_input_tokens or 0
    finally:
        if historial:
            print("guardando memoria...", end=" ", flush=True)
            try:
                nota = escribir_memoria(client, system, historial)
                print(f"→ {nota.relative_to(VAULT)}" if nota else "(sesión vacía)")
            except anthropic.APIError as e:
                print(f"no se pudo guardar: {e.__class__.__name__}: {e}")
            print(f"sesión: ~${costo():.4f} USD "
                  f"({tokens['in'] + tokens['cache_read'] + tokens['cache_write']} in / "
                  f"{tokens['out']} out)")


if __name__ == "__main__":
    try:
        main()
    except anthropic.AuthenticationError:
        sys.exit(
            "error: API key inválida o ausente.\n"
            "Generá una en https://console.anthropic.com y corré:\n"
            "  export ANTHROPIC_API_KEY='sk-ant-...'"
        )
    except anthropic.APIConnectionError:
        sys.exit("error: sin conexión con la API. Revisá tu internet.")
