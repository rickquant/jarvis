# Jarvis 🔵

*[English](README.md) · [Español](README.es.md)*

A J.A.R.V.I.S.-style personal voice assistant running **locally on macOS at $0 cost**.
Say *"jarvis"*, talk to it, and it answers out loud with real memory of who you are and what you're working on — while an Iron Man-style HUD shows what's happening.

> 🎬 *Demo video: coming soon.*

## What it does

- **Hands-free**: *"jarvis"* wake word (openWakeWord, local) + adaptive VAD — speak, and it auto-stops when you go quiet. Zero clicks.
- **Conversation with memory**: it knows its user, their projects and past decisions, and **remembers across sessions** (see the memory design below).
- **Hands**: opens and closes apps, controls Spotify and system volume, sets spoken timers, captures voice notes, and searches its own memory — behind an allowlist security model.
- **Web HUD**: an animated arc reactor that reacts to the voice, subtitles synced to speech, live telemetry (latencies, active timers, which tool is running).
- **Barge-in**: interrupt it mid-sentence with a click.

## Architecture

```
        ┌─ EARS ──────────────┐   ┌─ BRAIN ────────────────┐   ┌─ VOICE ───────────┐
 🎤 ──▶ │ openWakeWord (local) │──▶│ Claude via `claude -p` │──▶│ edge-tts, per-    │──▶ 🔊
        │ + adaptive VAD       │   │ streaming + tool use   │   │ sentence synthesis │
        │ + Whisper MLX (local)│   │ behind an allowlist    │   │ in parallel        │
        └──────────────────────┘   └───────────┬────────────┘   └───────────────────┘
                                               │ reads / writes
                                   ┌───────────▼────────────┐
                                   │ MEMORY: Obsidian vault  │
                                   │ (local markdown)        │
                                   └─────────────────────────┘
                     The HUD (Flask + canvas) orchestrates it all in the browser
```

**Core design decision: voice is an interface, not architecture.** `jarvis_voz.py` and the HUD wrap the same loop from `jarvis_cli.py` without touching it — the brain doesn't know whether you spoke or typed.

## The memory design (the interesting part)

Memory is not a vector store: it's an **Obsidian vault** (plain markdown) with layered instructions:

1. **Strategy layer** — who the user is, how to talk to them, vault structure. Always loaded.
2. **Per-project technical layer** — stack, decisions, and state of the active project. Loaded by context.
3. **On session close**, Jarvis writes a summary of what it learned back to the vault.

The full loop: `read vault context → call Claude → respond → write back what was learned`. The next session picks up without re-explaining anything. The advantage over embeddings: memory is **human-readable, editable, and versionable** — the user sees and controls exactly what their assistant knows.

## Hands and security

Actions run through `claude -p` tool use with `--allowedTools` restricted to **exact prefixes**: security is the menu of allowed commands, not the prompt. Anything not on the allowlist is denied by the CLI before it runs — asking it to "delete that file" doesn't work even via prompt injection, because the tool simply doesn't exist for it.

## Latency at $0

With no paid Realtime APIs, fluidity comes from engineering:

- **Per-sentence streaming**: TTS synthesizes each sentence in parallel while the rest of the response is still arriving — Jarvis starts talking before it finishes thinking.
- **Pre-roll buffer**: ~1.2s of audio before the wake word is prepended to the capture — nothing said in one breath gets lost.
- **Silence trimming** at each mp3's boundaries (edge-tts padding sounded like robotic pauses).
- **8-bit Whisper** (quantized large-v3-turbo): half the memory of fp16 — crucial on an 8GB Mac.
- **Announce before executing**: "On it." plays while the tool runs — the wait is never silent.

## Running it

Requirements: macOS on Apple Silicon, Python 3.10+, [Claude Code](https://claude.com/claude-code) logged in (subscription — no API key), internet for TTS.

```bash
pip install -r requirements.txt
python3 jarvis_ui.py <path-to-your-vault>   # full HUD at localhost:7777
python3 jarvis_voz.py <path-to-your-vault>  # voice only, in the terminal
python3 jarvis_cli.py <path-to-your-vault>  # text only
```

The first run downloads the models (Whisper, wake word) and macOS asks for microphone permission. The vault needs a root `CLAUDE.md` with the user's context — the persona is tuned to its original user; adjust `PERSONA` in `jarvis_voz.py` and `RESPELL` for yours. (The assistant speaks Spanish by default — the persona prompt is where you change that.)

## Structure

| File | What it is |
|---|---|
| `jarvis_cli.py` | The brain: loop over `claude -p`, vault context, streaming, session memory |
| `jarvis_voz.py` | Ears and voice: local Whisper MLX + edge-tts + persona |
| `jarvis_ui.py` + `ui.html` | Web HUD: Flask + SSE + canvas; wake word, timers, barge-in |
| `manos.py` | System actions menu (apps, Spotify, volume, captures) |
| `timer.py` | Spoken timers |
| `jarvis.py` | SDK version (`anthropic`), ready for when API credit lands |

## Roadmap

- **Command router**: intercept obvious commands before the LLM round-trip.
- **Proactivity**: spoken briefing on the first boot of the day, reading pending items from the vault.
- **Vision**: screen/webcam snapshot → Claude.
- **Multi-agent** (SDK phase): background research without blocking the conversation.

---

*A personal learning-in-public project — built with Claude Code as pair programmer.*
