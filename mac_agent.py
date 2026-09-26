#!/usr/bin/env python3
import json
import subprocess
import sys
import re
import datetime
from pathlib import Path

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"
LOG_FILE = Path.home() / ".mac_agent_log.jsonl"
MAX_STEPS = 15
MAX_TOOL_RESULT_CHARS = 2000   # truncate long tool output before it re-enters context

# Commands that require explicit user confirmation before running.
RISKY_PATTERNS = [
    r"\brm\b", r"\bsudo\b", r"\bdiskutil\b", r"\bmv\b.*(Trash|System)",
    r"\bdd\b", r"\bkill(all)?\b", r"\bshutdown\b", r"\breboot\b",
    r"\bformat\b", r"\bdelete\b", r"empty trash", r"\bchmod\b", r"\bchown\b",
    r"\bcurl\b.*-o\b", r"\bcurl\b.*\|\s*(sh|bash)", r"\bwget\b",
    r"\bnetworksetup\b", r"\bpmset\b",
]


def is_risky(kind: str, command: str) -> bool:
    """Check the raw command AND (for AppleScript) anything it shells out to."""
    if any(re.search(p, command, re.I) for p in RISKY_PATTERNS):
        return True
    if kind == "applescript" and "do shell script" in command:
        # A dangerous command can hide inside `do shell script "..."`.
        m = re.search(r'do shell script\s+"([^"]*)"', command)
        if m and any(re.search(p, m.group(1), re.I) for p in RISKY_PATTERNS):
            return True
    return False


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a command on macOS. Use kind='applescript' for AppleScript "
                "(app control, UI automation, System Events, dialogs), or "
                "kind='shell' for shell/bash commands (files, processes, CLI tools)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["applescript", "shell"],
                    },
                    "command": {
                        "type": "string",
                        "description": "The AppleScript or shell command to run.",
                    },
                },
                "required": ["kind", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Call this when the user's goal has been fully accomplished.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of what was done.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are a macOS automation agent. You accomplish the user's goal by \
calling run_command with AppleScript or shell commands, one step at a time. \
Look at the result of each command before deciding the next step. Prefer \
AppleScript for app/UI control (open apps, click menus, manage windows) and \
shell for file/process operations. Keep commands minimal and targeted. \
You will always call exactly one tool per turn — either run_command or \
task_complete — never plain text.

IMPORTANT — stopping condition:
- Treat the user's goal LITERALLY. Do not invent extra sub-goals or more \
specific interpretations than what was asked. "Open settings" means open \
the Settings/System Preferences app — nothing more. It does NOT mean \
navigate to a specific pane unless the user named one.
- As soon as the literal goal is satisfied (the app is open, the file is \
created, etc.), call task_complete immediately. Do not keep going to \
"improve" or "finish" the result further.
- If a command fails, do not blindly retry the same or a cosmetically \
different command. If you've already tried two different approaches to \
the same sub-step and both failed, stop trying that sub-step — either \
call task_complete describing what succeeded and what didn't, or try a \
genuinely different strategy (not a reworded version of the same one).
- Prefer stopping one step early over running one step too many."""


def get_system_context() -> str:
    """Front-load cheap, useful state so the model doesn't waste steps
    discovering it (running apps, cwd)."""
    try:
        apps = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process whose background only is false'],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        apps = "(unavailable)"
    cwd = str(Path.cwd())
    return f"Current context — running apps: {apps or '(unavailable)'}. Shell cwd: {cwd}."


def run_command(kind: str, command: str) -> str:
    try:
        if kind == "applescript":
            result = subprocess.run(
                ["osascript", "-e", command],
                capture_output=True, text=True, timeout=30,
            )
        else:
            result = subprocess.run(
                command, shell=True,
                capture_output=True, text=True, timeout=30,
            )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            text = f"ERROR (exit {result.returncode}): {err or out}"
        else:
            text = out if out else "(no output, success)"
    except subprocess.TimeoutExpired:
        text = "ERROR: command timed out after 30s"
    except Exception as e:
        text = f"ERROR: {e}"

    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f"\n...[truncated, {len(text)} chars total]"
    return text


def log(entry: dict):
    entry["ts"] = datetime.datetime.now().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def confirm(kind: str, command: str) -> bool:
    print(f"\n  ⚠️  RISKY COMMAND ({kind}):")
    print(f"     {command}")
    try:
        resp = input("  Run this? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return resp == "y"


def call_ollama(messages):
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def trim_history(messages, keep_last_n_tool_results=4):
    """Keep system + goal untouched, but collapse older tool results down
    to a one-line stub so context doesn't grow unbounded on long runs."""
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_last_n_tool_results:
        return messages
    stale = tool_indices[:-keep_last_n_tool_results]
    for i in stale:
        content = messages[i]["content"]
        if not content.startswith("[collapsed]"):
            messages[i]["content"] = "[collapsed] " + content[:120]
    return messages


def normalize_command(command: str) -> str:
    """Loose normalization so cosmetically-different retries of the same
    idea (extra whitespace, quote style) still count as the same attempt."""
    c = command.strip().lower()
    c = re.sub(r"\s+", " ", c)
    c = re.sub(r"[\"']", "", c)
    return c


def parse_tool_call(call):
    """Return (fn_name, args_dict) or raise ValueError/JSONDecodeError."""
    fn = call.get("function", {}).get("name")
    args = call.get("function", {}).get("arguments")
    if not fn:
        raise ValueError("tool call missing function name")
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        raise ValueError("tool call arguments not an object")
    return fn, args


def run_agent(goal: str):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": get_system_context()},
        {"role": "user", "content": goal},
    ]
    log({"event": "start", "goal": goal})

    fail_counts = {}   # normalized (kind, command) -> consecutive fail count
    success_count = 0  # commands that ran without error, for the nudge

    step = 1
    while step <= MAX_STEPS:
        messages = trim_history(messages)

        # After a couple of successful steps, remind the model to check if its goal is already satisfied
        if success_count >= 2 and step % 2 == 0:
            messages.append({
                "role": "user",
                "content": (
                    "Reminder: check whether the original goal is already "
                    "satisfied. If so, call task_complete now instead of "
                    "continuing."
                ),
            })

        try:
            data = call_ollama(messages)
        except requests.exceptions.RequestException as e:
            print(f"\n❌ Ollama request failed: {e}")
            log({"event": "ollama_error", "error": str(e)})
            return

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            # Model replied with plain text instead of a tool call
            content = msg.get("content", "")
            print(f"\n[model, no tool call] {content[:300]}")
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": "You must call run_command or task_complete — no plain text replies.",
            })
            step += 1
            continue

        messages.append(msg)
        made_progress = False

        for call in tool_calls:
            try:
                fn, args = parse_tool_call(call)
            except (ValueError, json.JSONDecodeError) as e:
                print(f"\n⚠️  Malformed tool call from model: {e}")
                messages.append({
                    "role": "tool",
                    "content": f"ERROR: your last tool call was malformed ({e}). "
                                f"Retry with valid JSON arguments matching the schema.",
                })
                log({"event": "malformed_tool_call", "error": str(e)})
                continue

            if fn == "task_complete":
                print(f"\n✅ Done: {args.get('summary', '')}")
                log({"event": "complete", "summary": args.get("summary", "")})
                return

            if fn == "run_command":
                kind = args.get("kind")
                command = args.get("command")
                if kind not in ("applescript", "shell") or not command:
                    messages.append({
                        "role": "tool",
                        "content": "ERROR: run_command needs kind='applescript'|'shell' and a non-empty command.",
                    })
                    continue

                print(f"\n[step {step}] {kind}: {command}")
                made_progress = True

                key = (kind, normalize_command(command))
                if fail_counts.get(key, 0) >= 1:
                    # This exact (or near-identical) command already failed
                    print("  ⏭  skipped (already failed once — same command)")
                    messages.append({
                        "role": "tool",
                        "content": (
                            "SKIPPED: this exact command already failed earlier. "
                            "Do not retry it again. Either try a genuinely "
                            "different approach, or if this sub-step isn't "
                            "essential to the literal goal, call task_complete "
                            "now with what has succeeded so far."
                        ),
                    })
                    log({"event": "repeat_skipped", "kind": kind, "command": command})
                    continue

                if is_risky(kind, command):
                    if not confirm(kind, command):
                        result = "USER DECLINED: command not run (flagged risky)."
                        print("  ✋ skipped")
                        messages.append({"role": "tool", "content": result})
                        log({"event": "declined", "kind": kind, "command": command})
                        continue

                result = run_command(kind, command)
                print(f"  → {result[:300]}")
                log({"event": "run", "kind": kind, "command": command, "result": result})
                messages.append({"role": "tool", "content": result})

                if result.startswith("ERROR"):
                    fail_counts[key] = fail_counts.get(key, 0) + 1
                else:
                    success_count += 1
            else:
                messages.append({
                    "role": "tool",
                    "content": f"ERROR: unknown function '{fn}'. Use run_command or task_complete.",
                })

        if made_progress:
            step += 1

    print("\n⚠️  Max steps reached without task_complete. Stopping.")
    log({"event": "max_steps_reached"})


def main():
    if len(sys.argv) > 1:
        goal = " ".join(sys.argv[1:])
    else:
        try:
            goal = input("What should the agent do? ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    if not goal:
        print("No goal given.")
        return

    try:
        requests.get("http://localhost:11434", timeout=3)
    except requests.exceptions.ConnectionError:
        print("❌ Can't reach Ollama at localhost:11434. Run `ollama serve` first.")
        sys.exit(1)

    try:
        run_agent(goal)
    except KeyboardInterrupt:
        print(f"\n\n🛑 Interrupted by user. Partial progress (if any) is in {LOG_FILE}.")
        log({"event": "interrupted"})


if __name__ == "__main__":
    main()
