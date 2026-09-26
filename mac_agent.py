#!/usr/bin/env python3
"""
mac_agent.py — a lightweight local computer-use agent for macOS.

Uses a local LLM (via Ollama) with tool-calling to generate AppleScript /
shell commands, executes them via osascript/subprocess, and loops until
the goal is complete.

Requirements:
    brew install ollama
    ollama pull qwen2.5:7b-instruct
    pip install requests

Usage:
    python3 mac_agent.py "close all Finder windows and open Notes"
    python3 mac_agent.py              # interactive prompt
"""

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

# Commands that require explicit user confirmation before running.
RISKY_PATTERNS = [
    r"\brm\b", r"\bsudo\b", r"\bdiskutil\b", r"\bmv\b.*(Trash|System)",
    r"\bdd\b", r"\bkill(all)?\b", r"\bshutdown\b", r"\breboot\b",
    r"\bformat\b", r"\bdelete\b", r"empty trash", r"\bchmod\b", r"\bchown\b",
]

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
When the goal is fully done, call task_complete with a short summary. \
Do not call task_complete until you've verified the result where possible."""


def is_risky(kind: str, command: str) -> bool:
    if kind == "applescript":
        # AppleScript "do shell script" can embed shell risk too
        if "do shell script" in command:
            return any(re.search(p, command, re.I) for p in RISKY_PATTERNS)
        return False
    return any(re.search(p, command, re.I) for p in RISKY_PATTERNS)


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
            return f"ERROR (exit {result.returncode}): {err or out}"
        return out if out else "(no output, success)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 30s"
    except Exception as e:
        return f"ERROR: {e}"


def log(entry: dict):
    entry["ts"] = datetime.datetime.now().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def confirm(kind: str, command: str) -> bool:
    print(f"\n  ⚠️  RISKY COMMAND ({kind}):")
    print(f"     {command}")
    resp = input("  Run this? [y/N]: ").strip().lower()
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


def run_agent(goal: str):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    log({"event": "start", "goal": goal})

    for step in range(1, MAX_STEPS + 1):
        data = call_ollama(messages)
        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            # Model replied with plain text instead of a tool call
            content = msg.get("content", "")
            print(f"\n[model] {content}")
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": "Please use run_command or task_complete to proceed.",
            })
            continue

        messages.append(msg)

        for call in tool_calls:
            fn = call["function"]["name"]
            args = call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)

            if fn == "task_complete":
                print(f"\n✅ Done: {args.get('summary', '')}")
                log({"event": "complete", "summary": args.get("summary", "")})
                return

            if fn == "run_command":
                kind = args["kind"]
                command = args["command"]
                print(f"\n[step {step}] {kind}: {command}")

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

    print("\n⚠️  Max steps reached without task_complete. Stopping.")
    log({"event": "max_steps_reached"})


def main():
    if len(sys.argv) > 1:
        goal = " ".join(sys.argv[1:])
    else:
        goal = input("What should the agent do? ").strip()

    if not goal:
        print("No goal given.")
        return

    # Quick check that Ollama is reachable
    try:
        requests.get("http://localhost:11434", timeout=3)
    except requests.exceptions.ConnectionError:
        print("❌ Can't reach Ollama at localhost:11434. Run `ollama serve` first.")
        sys.exit(1)

    run_agent(goal)


if __name__ == "__main__":
    main()
