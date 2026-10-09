"""Stop hook for Claude Code: after a stretch of work, remind Claude to leave Khala memory notes.

Claude Code runs this every time Claude finishes a turn. It reads only what the transcript gained since the last
run, and counts work: each tool call 1, each message from the person 3. A call to any server's memory_note or
memory_write resets the count, since Claude has just saved what it learned. When the count reaches the threshold
(the plugin's checkpoint_every option, 15 by default, 0 for never), the hook hands Claude a short reminder and the
turn continues; Claude decides whether anything is worth keeping. The notes land in the Khala inbox and go through
review like any other.

Standard library only, so it runs without Khala installed. It never fails a turn: any problem means no reminder.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_EVERY = 15
MESSAGE_WEIGHT = 3
STALE_DAYS = 30
SAVED = re.compile(r"mcp__.+__memory_(note|write)")

REMINDER = (
    "Khala memory checkpoint. Before you finish, look back over the work since the last checkpoint. If it produced "
    "something worth keeping across sessions (a decision and why it was made, a fact about this project or "
    "environment that took effort to find, a preference the person stated, a fix for a problem that may come back), "
    "leave it with the Khala memory_note tool: one note per topic, in the scope that fits (memory_scopes lists "
    "them), with enough context and pointers (paths, commands, links) to act on later. Never record secrets, "
    "credentials or the conversation itself. If nothing is worth keeping, or the Khala tools are not available in "
    "this session, finish without mentioning this."
)


def threshold(environ):
    try:
        return max(0, int(float(environ.get("CLAUDE_PLUGIN_OPTION_CHECKPOINT_EVERY", DEFAULT_EVERY))))
    except ValueError:
        return DEFAULT_EVERY


def state_dir(environ):
    base = environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".cache", "khala-claude-code")
    return Path(base) / "sessions"


def work_in(line):
    """(work, saved) for one transcript line: work done, and whether it saved memory."""
    try:
        entry = json.loads(line)
    except ValueError:
        return 0, False
    if not isinstance(entry, dict) or entry.get("isSidechain"):
        return 0, False
    content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
    if entry.get("type") == "assistant" and isinstance(content, list):
        calls = [b.get("name", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        return len(calls), any(SAVED.fullmatch(name) for name in calls)
    if entry.get("type") == "user" and not entry.get("isMeta"):
        if isinstance(content, str) and content.strip():
            return MESSAGE_WEIGHT, False
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content) \
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return MESSAGE_WEIGHT, False
    return 0, False


def advance(transcript, state):
    """Read the transcript from the saved offset, up to its last complete line, and update the count."""
    with open(transcript, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if state["offset"] > size:              # rewritten or replaced: start over
            state.update(offset=0, work=0)
        f.seek(state["offset"])
        chunk = f.read(size - state["offset"])
    end = chunk.rfind(b"\n") + 1                # a line still being written is read next time
    for line in chunk[:end].splitlines():
        work, saved = work_in(line.decode("utf-8", "replace"))
        state["work"] = 0 if saved else state["work"] + work
    state["offset"] += end
    return state


def tidy(folder, now):
    for old in folder.glob("*.json"):
        try:
            if now - old.stat().st_mtime > STALE_DAYS * 86400:
                old.unlink()
        except OSError:
            pass


def run(hook, environ, now=None):
    """The hook's JSON output for one Stop event, or None."""
    now = time.time() if now is None else now
    every = threshold(environ)
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(hook.get("session_id") or ""))
    transcript = hook.get("transcript_path")
    if hook.get("hook_event_name") not in (None, "Stop") or not session or not transcript:
        return None
    folder = state_dir(environ)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (session + ".json")
    try:
        state = json.loads(path.read_text())
        state = {"offset": int(state["offset"]), "work": int(state["work"])}
    except (OSError, ValueError, KeyError, TypeError):
        state = {"offset": 0, "work": 0}
        tidy(folder, now)                       # once per new session
    state = advance(transcript, state)
    remind = bool(every) and state["work"] >= every and not hook.get("stop_hook_active")
    if remind:
        state["work"] = 0
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)
    if not remind:
        return None
    return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": REMINDER}}


def main():
    try:
        out = run(json.load(sys.stdin), os.environ)
    except Exception:                           # a reminder is never worth breaking a session for
        return 0
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
