# mac-agent

A lightweight local computer-use agent for macOS. No API keys, no cloud, runs entirely on your machine using [Ollama](https://ollama.com) + Qwen2.5-7B-Instruct
Dependencies are managed with [uv](https://docs.astral.sh/uv/)

It talks to your Mac through AppleScript (`osascript`) for app/UI control and shell commands for file/process operations, in a tool-calling loop:
the model proposes a command, the script runs it, the result goes back to the model, repeat until done.

## Setup

```bash
git clone https://github.com/<your-username>/mac-agent.git
cd mac-agent

# one-time setup
brew install uv ollama
ollama pull qwen2.5:7b-instruct
uv sync

# in one terminal, keep the Ollama server running
ollama serve

# in another terminal, run the agent
uv run mac_agent.py "open Notes and create a new note titled Groceries"
```

## Usage

```bash
# one-shot
uv run mac_agent.py "close all Finder windows and open Safari"

# interactive — prompts you for a goal
uv run mac_agent.py
```

While it runs you'll see each command it executes and the result:

[step 1] applescript: tell application "Safari" to activate
  → (no output, success)

✅ Done: Opened Safari.

Press Ctrl+C at any point to abort a run

## Safety

Commands matching risky patterns (`rm`, `sudo`, `diskutil`, `kill`, `chmod`, `chown`, emptying Trash, etc.) pause for a `y/N` confirmation before running. Everything else runs automatically.

Every command and its result is logged to `~/.mac_agent_log.jsonl` for auditing — one JSON line per event (start, run, declined, complete).

## macOS permissions

The first time it controls an app via AppleScript, macOS will prompt for **Automation** permission (System Settings → Privacy & Security → Automation) for your terminal app to control the target app (Finder, Notes, System Events, etc.). Approve each prompt as it appears

For UI scripting (clicking menus/buttons via System Events), you may also need to grant your terminal **Accessibility** access under Privacy & Security → Accessibility

## Notes on the model choice

Qwen2.5-7B-Instruct was picked for 16GB RAM Macs as the best balance of tool-calling reliability and resource use. If you hit memory pressure, `qwen2.5:3b-instruct` is a lighter fallback (worse at multi-step reasoning). If you have 32GB+, `qwen2.5:14b-instruct` will be more reliable at longer, more ambiguous tasks

## Limitations

- No screen vision — it can't see the screen, only what AppleScript/shell
  commands report back as text. Tasks needing visual judgment (e.g.
  "click the blue button") aren't reliably doable this way; stick to
  tasks expressible as app commands, menu items, or file operations
- 15-step cap per run to avoid runaway loops (edit `MAX_STEPS` in the
  script to change)
