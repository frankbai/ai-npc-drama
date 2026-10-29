# Snow Trace (雪痕) · An AI NPC Interactive Drama Framework

![Snow Trace](content/assets/cg/kv.webp)

[中文](README.md)

**An interactive drama where the characters remember you.** NPCs have their own backstories, secrets and plot, yet every conversation you have with them is yours alone. They remember what you said, pass what they heard on to each other, and in the scene that matters, they quote your own words back to you.

This repository contains:

1. **A framework** for LLM-driven NPCs that have memory and secrets, follow a script, and still chat freely. Zero dependencies (Python standard library only). Works with any OpenAI Chat Completions–compatible model.
2. **A complete sample story, *Snow Trace*** (in Chinese): a frontier inn, a snowed-in pass, a ten-year-old murder case. 6 chapters, 3 free-talk NPCs, 3 endings. A playthrough takes about an hour.

## The sample story (no spoilers)

You run the only inn in a frontier town and have done so for ten years. Nobody knows who you really are. On the night of the first snow, a young swordsman kicks the door open and points his sword at the counter: *"Which one of you is Yi Dao Xue?"* That night an avalanche seals the mountain pass. The next morning, someone is found dead in your backyard.

Each chapter opens with a scripted scene. After that you have one lamp's worth of time: 12 lines of free conversation shared among three characters. You can also run the inn, investigate, and decide whether to show what your hands can really do. In the last chapter, you name the killer in front of everyone.

## Framework capabilities

| Capability | What it does |
|---|---|
| Layered secrets | Secrets unlock by chapter. Locked parts never enter the prompt, so the model can't leak them. The killer doesn't know he's the killer in free chat. The "truth" is only provided for the final reveal line. |
| Memory consolidation | After each conversation the NPC writes a diary entry, updates their impression of you, and records facts, promises and what they've told you. |
| Verbatim quotes | Your own words are stored by topic. A quote is kept only if it actually appears in the transcript. Later, NPCs can quote you exactly. |
| NPC-to-NPC relays | NPCs pass on what you told them, tagged with source and reliability. |
| Inner voice | An optional "what I really think" line per turn. It can be always shown, shown once trust is high enough, or never shown ("unreadable"). |
| Life simulation | At the start of each chapter, every NPC's life is simulated from script anchors, taking your recent interactions into account. |
| Directed scenes | Key scenes are defined by required beats and key lines, then acted out with the model over 2–5 turns. A scene can have variants depending on conditions such as trust. |
| Personal lines | Slots in scripted scenes that the NPC fills from its memory of *you*. |
| Turn budget | Free-talk turns per chapter, shareable across NPCs. NPCs wind the conversation down naturally as the budget runs out. |
| Reply options | Generated from the player persona, which also unlocks by chapter, so options never spoil things. |
| Events, variables, clues, accusation, endings | Data-driven, with a small condition language. |
| Spoiler checker | Assembles every NPC's prompt for every chapter and checks for forbidden text. |

## Run it in 30 seconds

```bash
git clone <this repo> ai-npc-drama && cd ai-npc-drama
python3 run.py            # Python 3.10+, no dependencies
```

Or with Docker: `docker build -t ai-npc-drama . && docker run -p 8800:8800 -v "$PWD/local-data:/data" ai-npc-drama`. On macOS you can also double-click `start.command`.

Open <http://127.0.0.1:8800>. It starts in **mock mode**: no model calls and placeholder dialogue, useful only for checking the flow. To use a real model, click "模型" (Model) in the top-right corner, or start with:

```bash
OPENAI_API_KEY=sk-... NPC_BASE_URL=https://api.openai.com/v1 NPC_MODEL=<model> python3 run.py
```

## Write your own story

The framework knows nothing about *Snow Trace*. Everything lives in `content/`. Copy it, edit it, check it with `python3 tools/check_content.py my-story`, then run with `CONTENT_DIR=my-story python3 run.py`. See `docs/内容包写作指南.md` (content pack guide, in Chinese).

## Layout

```
npc/       generic runtime: content loader, prompts, dialogue, memory, storage, model gateway
host/      demo host: story director (story.py), HTTP server (server.py), web UI (web/)
content/   the sample story's content pack; art goes in content/assets/
tools/     content checker + spoiler checker
tests/     test suite (mock mode, offline)
docs/      architecture, content guide, config, deployment, art prompts, story design notes (spoilers)
```

## License

MIT. The sample story is original. It borrows some common mystery and wuxia devices (the investigator is the killer, a frontier inn, a snowed-in closed circle) but no characters, names or plots from any existing work.
