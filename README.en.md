# 💓 HeartAgent — A Desktop AI Employee That Grows Its Own Skills

> **It watches your tasks, remembers your preferences, writes its own extensions, and runs your job hunt — while you sleep.**
> Zero third-party dependencies · Pure Python standard library · 7×24 unattended · Dual-model collaboration

[![CI](https://github.com/YOUR_USERNAME/HeartAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/HeartAgent/actions/workflows/ci.yml)

**Unlike a chatbot, it works proactively.** A heartbeat mechanism patrols every 30 seconds: consolidating memories when idle, asking *you* when memories conflict, promoting frequently-used code snippets into reusable extensions (with human approval), and mailing a daily report every morning. Failed tasks retry automatically and push notifications to your phone. The badges above are real runtime stats it computes about itself.

## Highlights

| | |
|---|---|
| 🤖 **Dual-model collaboration** | A main AI (reasoning & tool calls) + an assistant AI (memory keeper); native function calling with JSON-protocol fallback |
| 🔧 **Writes its own extensions** | Generates Python code → capability scan → *human approval gate* → versioned install → sandboxed subprocess execution with memory/timeout limits |
| 💓 **Heartbeat** | Idle learning, contradiction detection in memory, self-reflection, daily reports, prompt self-evolution, config hot-reload |
| 👀 **Glass-box transparency** | Left pane: results. Right pane: live stream of every thought, tool call, heartbeat and task |
| 📋 **Tasks** | Scheduled / loop / cron × prompt or code, retries, task chains, concurrency caps, push notifications, DAG view |
| 🎯 **Goals (OKR)** | Long-term goals auto-decomposed into tasks; heartbeat tracks progress |
| 🧠 **Memory** | SQLite + FTS5 + optional vector search; dedup & merge; contradiction resolution; entity-relation graph |
| 📚 **RAG** | Ingest files/folders/URLs (incl. PDF & DOCX best-effort) into a semantic knowledge base |
| 🛡️ **Safety rails** | Capability tokens (net/proc/fs), trust levels L0–L3, autonomy levels, cost wallet |
| 🧪 **Evals** | Agent "unit tests" with regression health scores — prompt/model changes show up as score changes |
| ⏪ **Replay & counterfactual** | Time-travel through past reasoning; re-run the same input with a different model and diff the paths |
| 📨 **Channels** | Email (IMAP/SMTP), Telegram bot, WeCom bot push — respond only to allowlisted senders |

## Quick start

```bash
python main.py          # open http://127.0.0.1:8765
python -m unittest discover tests -v
```

No API key yet? It runs in **mock mode** — the whole pipeline works, replies are placeholders.
Edit `config.json` (copy from `config.example.json`), save, and it hot-reloads. See [README.md](README.md) for the full Chinese documentation, architecture and security notes.
