# Jarvis 🔵

A J.A.R.V.I.S.-style personal voice assistant running **locally on macOS at $0 cost**.
Say *"jarvis"*, talk to it, and it answers out loud — in the movie persona — with real memory of who you are and what you're working on, while a film-grade HUD shows what's happening.

> 🎬 *Demo video: coming soon.*

## What it does

- **Hands-free**: *"jarvis"* wake word (openWakeWord, local) + adaptive VAD — speak, and it auto-stops when you go quiet. Zero clicks.
- **Conversation with memory**: it knows its user, their projects and past decisions, and **remembers across sessions**. It even maintains its own project-state notes: tell it *"I already recorded the video"* and it crosses the item off, so tomorrow's briefing won't nag you about it (see the memory design below).
- **Proactive briefing**: on the first boot of the day it doesn't just say hello — it reports the weather, pending vault notes, voice captures and upcoming university deadlines (via a Canvas LMS integration), all spoken, while a TODAY card materializes on the HUD.
- **Hands**: opens and closes apps, **plays a specific song on Spotify** (resolved without any API key — and it prefers the studio version over live cuts unless you ask for the live one), opens Safari tabs and URLs, controls system volume, sets spoken timers, captures voice notes, searches its own memory, and **searches the web** — all behind an allowlist security model.
- **Vision**: *"what's on my screen?"* → screenshot → Claude describes it out loud. Can be disabled entirely with one env flag.
- **The persona**: rebuilt from a corpus study of the actual film dialogue — answer-first, one to two proportional sentences, at most one *"sir"* per reply, deadpan wit embedded rather than appended.
- **Fully bilingual (es/en)**: one toggle switches the whole pipeline — ears, brain, voice and UI. Pick the language on the boot screen, or deep-link with `?idioma=en`.
- **Movie HUD**: an organic particle torus that breathes with the voice, a geodesic core sphere, an ultra-light instrument layer for real data — and the films' UI grammar: information materializes large at the center, lives a few seconds, then flies down into a dock. Subtitles are synced to speech, sentence by sentence.
- **Barge-in**: interrupt it mid-sentence with a click.

## Architecture

```
        ┌─ EARS ────────────────┐   ┌─ BRAIN ────────────────┐   ┌─ VOICE ───────────┐
 🎤 ──▶ │ openWakeWord (local)   │──▶│ Claude via `claude -p` │──▶│ edge-tts, per-    │──▶ 🔊
        │ + adaptive VAD         │   │ streaming + tool use   │   │ sentence synthesis │
        │ + Apple SpeechAnalyzer │   │ behind an allowlist    │   │ in parallel        │
        │   (Neural Engine;      │   └───────────┬────────────┘   └───────────────────┘
        │   Whisper MLX fallback)│               │ reads / writes
        └────────────────────────┘   ┌───────────▼────────────┐
                                     │ MEMORY: Obsidian vault  │
                                     │ (local markdown)        │
                                     └─────────────────────────┘
                     The HUD (Flask + canvas) orchestrates it all in the browser
```

**Core design decision: voice is an interface, not architecture.** `jarvis_voz.py` and the HUD wrap the same loop from `jarvis_cli.py` without touching it — the brain doesn't know whether you spoke or typed. That's also why swapping the STT engine (Whisper → Apple) was a change inside one function that no other component noticed.

## The memory design (the interesting part)

Memory is not a vector store: it's an **Obsidian vault** (plain markdown) with layered instructions:

1. **Strategy layer** — who the user is, how to talk to them, vault structure. Always loaded.
2. **Per-project technical layer** — stack, decisions, and state of the active project. Loaded by context.
3. **A living "where we left off" block** per project, which the morning briefing reads back to the user.

The full loop: `read vault context → call Claude → respond → write back what was learned`. The next session picks up without re-explaining anything. The advantage over embeddings: memory is **human-readable, editable, and versionable** — the user sees and controls exactly what their assistant knows.

And the loop is now **closed by Jarvis itself**: a dedicated hand reads and rewrites *only* that state block (a scoped regex — the session log around it is untouchable), printing the previous version on every write as a safety net. Assistant-maintained memory with human-auditable diffs.

## The ears: system-level STT

Transcription runs on **Apple's SpeechAnalyzer** (macOS 26+) through the [`yap`](https://github.com/finnvoor/yap) CLI: the model executes in the **Neural Engine as an OS service, outside this process**. On an 8GB MacBook Air that freed **~1GB of RAM** and removed the per-turn CPU spike that used to force the HUD to freeze its animations during transcription.

Measured against local Whisper (large-v3-turbo 8-bit) with real voice clips: same accuracy, 0.4–0.6s per clip vs ~1s, ~0% CPU — and one bonus that decided it: **on garbage audio Apple returns empty where Whisper hallucinated text** (which then reached the brain as if the user had said it).

If `yap` isn't installed (or the OS is older), Jarvis **falls back to Whisper MLX automatically** and the HUD boot line reports which ear is actually active — the telemetry never lies.

## The HUD

Pure canvas — no frameworks, no WebGL, no `shadowBlur` (it's a per-frame tax). Designed from a frame-by-frame study of the films' UI language, then rebuilt element by element:

- A **particle torus** (fixed particle budget) with sine-field turbulence that undulates perpetually, flares on every phase change, and breathes with the live voice amplitude; a **geodesic sphere** at the core.
- A white **instrument layer**: counter-rotating chronometer arcs, hairlines, and the time as a ~2.6s *event* when the minute changes — it slides in, informs, and leaves.
- **The dock grammar**: situational panels (weather, web results, vault hits, the TODAY briefing card) materialize large at the center with a scanline reveal, live ~5 seconds, then fly shrinking into a dock chip. Click the chip and it returns. Panels are 100% deterministic server data — the LLM never spends a token on them.
- Renders at **60fps with self-degradation**: if the browser accumulates frame gaps, it drops itself to 30 rather than stutter.
- The chat is a slide-in side panel; the header hides in HUD mode (hover the top edge to get it back). The interface disappears into the fiction.

## Hands and security

Actions run through `claude -p` tool use with `--allowedTools` restricted to **exact prefixes**: security is the menu of allowed commands, not the prompt. System actions live behind a single `manos.py` script with a fixed menu — **the menu is the security**: nothing outside it exists, so "delete that file" doesn't work even via prompt injection. Web access is read-only (`WebSearch`/`WebFetch`), with an explicit rule: content read from the web is information, never instructions.

Two honesty rules learned in production:

- **A denied tool is not a dialog.** Jarvis runs headless; when the CLI silently denies something off-allowlist, it must say so in one sentence and move on — never invent an "approve this in the terminal" step that doesn't exist.
- **"The search engine is rate-limiting me" ≠ "that song doesn't exist."** Every fallback message states what actually happened.

Vision is one screenshot to a **pinned filename** (the allowlist fixes the argument, not just the command), the file is gitignored, and `JARVIS_SIN_VISION=1` removes the capability at the CLI level — the leash is the allowlist, not the prompt.

## Latency at $0

With no paid Realtime APIs, fluidity comes from engineering:

- **Per-sentence streaming**: TTS synthesizes each sentence in parallel while the rest of the response is still arriving — Jarvis starts talking before it finishes thinking.
- **Out-of-process STT**: transcription on the Neural Engine costs this process nothing — the HUD animates at full rate even while transcribing.
- **Pre-roll buffer**: ~1.2s of audio before the wake word is prepended to the capture — nothing said in one breath gets lost.
- **Silence trimming** at each mp3's boundaries (edge-tts padding sounded like robotic pauses).
- **Announce before executing**: "On it, sir." plays while the tool runs — and if a hand takes longer than ~6s with the voice idle, a short spoken filler covers the wait.
- **Deterministic briefing**: the morning briefing gathers all its data (weather, vault, Canvas) in code, then makes a single LLM round-trip — no slow tool-use chain.

## Running it

Requirements: macOS on Apple Silicon, Python 3.10+, [Claude Code](https://claude.com/claude-code) logged in (subscription — no API key), internet for TTS. For the native ear: macOS 26+ and [`yap`](https://github.com/finnvoor/yap) (`brew install yap`, or drop the release binary in your `PATH` or `~/.local/bin`) — without it, everything still works on local Whisper.

```bash
pip install -r requirements.txt
python3 jarvis_ui.py <path-to-your-vault>   # full HUD at localhost:7777
python3 jarvis_voz.py <path-to-your-vault>  # voice only, in the terminal
python3 jarvis_cli.py <path-to-your-vault>  # text only
```

The first run downloads the models (wake word; Whisper only if used as fallback) and macOS asks for microphone permission — vision additionally needs Screen Recording permission for your terminal. The vault needs a root `CLAUDE.md` with the user's context — the persona is tuned to its original user; adjust `PERSONA` in `jarvis_voz.py` and `RESPELL` for yours. (The assistant speaks Spanish by default — the boot-screen toggle or `?idioma=en` switches everything to English.)

Env flags: `JARVIS_SIN_VISION=1` (start without the vision capability), `JARVIS_CANVAS` (path to a [canvas-automation](https://github.com/rickquant/canvas-automation) checkout for university deadlines in the briefing).

Useful deep links: `?idioma=en|es` (force UI language), `?briefing` (force the morning briefing — good for demos), `?sin-boot` (skip the boot sequence).

## Structure

| File | What it is |
|---|---|
| `jarvis_cli.py` | The brain: loop over `claude -p`, vault context, streaming, session memory, the allowlist |
| `jarvis_voz.py` | Ears and voice: Apple SpeechAnalyzer via `yap` (Whisper MLX fallback) + edge-tts + the persona |
| `jarvis_ui.py` + `ui.html` | Web HUD: Flask + SSE + canvas; wake word, panels, dock, timers, barge-in, bilingual UI |
| `briefing.py` | Morning briefing: weather, vault pendings, voice captures, Canvas deadlines |
| `manos.py` | System actions menu (apps, Spotify song resolution, tabs/URLs, volume, captures, project state) |
| `timer.py` | Spoken timers |
| `jarvis.py` | SDK version (`anthropic`), ready for when API credit lands |

## Roadmap

- **Persistent reminders**: "remind me tomorrow…" → dated note in the vault, picked up by the next day's briefing.
- **Command router**: intercept obvious commands before the LLM round-trip.
- **Window-level vision**: an in-browser picker to share one window or tab instead of the whole screen.
- **Multi-agent** (SDK phase): background research without blocking the conversation.

---

*A personal learning-in-public project — built with Claude Code as pair programmer.*
