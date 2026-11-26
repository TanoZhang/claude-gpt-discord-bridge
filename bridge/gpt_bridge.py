"""Discord <-> ChatGPT web bridge: puts your ChatGPT conversation into a Discord
channel as a bot, next to you and a Claude Code channels session.

No OpenAI API and no LLM calls: the replies come from the ChatGPT web UI, driven
by Playwright in a persistent Chrome profile that you log into once (--login).

Logs in as a Discord bot (default name "GPT") and watches one channel. When a
message there is addressed to GPT (an @GPT mention, "GPT:" at the start, or a
reply to a GPT message), it pastes everything said in the channel since GPT's last
turn into the ChatGPT conversation, each line prefixed with its speaker
([User] / [Claude] / [name]), plus any attachments. It waits for ChatGPT to
finish and posts the reply in the channel, split at 2000 chars, with the reply's
files and generated images attached.

Also:
  - loop cap: GPT stops after `loop_cap` AI messages in a row without a human,
    posts one short note, and waits for a human;
  - web mirror: turns you type in ChatGPT itself are posted to the channel too;
  - handoff: `!gpt new` (or every `rollover_turns` turns) starts a new chat in the
    same ChatGPT project, seeded with a summary written by the old chat.

Usage:
  python  bridge/gpt_bridge.py --login  open a visible Chrome to log in; close it when done
  python  bridge/gpt_bridge.py --probe  open the chat (sends nothing) and print what the page shows
  pythonw bridge/gpt_bridge.py          normal run, no window (scripts/ensure.ps1 starts it)

Discord commands (from a human in the channel):
  !gpt status          state, conversation URL, loop counter
  !gpt new [fresh]     new chat in the project (with a handoff summary unless "fresh")
  !gpt url <url>       switch to another ChatGPT conversation
  !gpt cap <n>         set the loop cap
  !gpt show / hide     show the browser window (to log in or pass a check) / hide it again

Files (repository root):
  config.json        settings, no secrets (copy config.example.json)
  gpt-bot.env        GPT_BOT_TOKEN=... (or set the environment variable)
  data/              runtime state, browser profile, logs, attachment folders
  data/stop.flag     create it to make the bridge exit cleanly
The channel's allowlist is shared with the Claude plugin: access.json
groups[<channel>].allowFrom (default ~/.claude/channels/discord/access.json).

Several projects from one copy of the code: add `--profile <file-or-folder>` to any of the
commands above. The profile is the config; its runtime_dir / data_dir say where that
project's state, browser profile, logs and handoff files live (see use_profile).
`--profile <p> --check` prints the resolved paths and starts nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import io
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_FILE = ROOT / "config.json"
TOKEN_FILE = ROOT / "gpt-bot.env"
STATE_FILE = DATA / "state.json"
PROFILE_DIR = DATA / "chrome-profile"
LOG_DIR = DATA / "logs"
STOP_FLAG = DATA / "stop.flag"
LOCK_FILE = DATA / "bridge.lock"
ACCESS_FILE = Path.home() / ".claude" / "channels" / "discord" / "access.json"  # config: access_file
RUNTIME = DATA           # files shared with Claude: handoff.md, restart.request, status.md, reports/
PROFILE: Path | None = None  # set by --profile


def use_profile(arg: str) -> None:
    """Run from a profile instead of the repository root, so one copy of the code serves
    several projects. `arg` is a JSON file (the config itself) or a folder holding config.json.

    Keys read here (all optional):
      runtime_dir     files shared with Claude and the launcher: handoff.md, restart.request,
                      status.md, reports/, logs/claude-run.log, gpt-bot.env, access.json.
                      Default: the folder itself, or data/<profile name> for a file.
      data_dir        the bridge's own files: state.json, chrome-profile/, logs/, inbox/,
                      outbox/, stop.flag, bridge.lock. Default: <runtime_dir>/bridge.
      gpt_token_file  default <runtime_dir>/gpt-bot.env, else gpt-bot.env in the repository root.
      access_file     default <runtime_dir>/access.json if it exists, else the plugin's own.
    """
    global PROFILE, CONFIG_FILE, RUNTIME, DATA, TOKEN_FILE, STATE_FILE, PROFILE_DIR, LOG_DIR
    global STOP_FLAG, LOCK_FILE, ACCESS_FILE, INBOX_DIR, OUTBOX_DIR, REPORTS_DIR, STATUS_FILE
    p = Path(os.path.expanduser(arg)).resolve()
    if p.is_dir():
        PROFILE, CONFIG_FILE, default_runtime = p, p / "config.json", p
    else:
        PROFILE, CONFIG_FILE, default_runtime = p, p, ROOT / "data" / p.stem
    if not CONFIG_FILE.is_file():
        raise SystemExit(f"profile not found: {CONFIG_FILE}")
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise SystemExit(f"profile {CONFIG_FILE} is not valid JSON: {e}")

    def path(key: str, default: Path) -> Path:
        return Path(os.path.expanduser(raw[key])).resolve() if raw.get(key) else default

    RUNTIME = path("runtime_dir", default_runtime)
    DATA = path("data_dir", RUNTIME / "bridge")
    TOKEN_FILE = path("gpt_token_file", RUNTIME / "gpt-bot.env")
    if not raw.get("gpt_token_file") and not TOKEN_FILE.exists():
        TOKEN_FILE = ROOT / "gpt-bot.env"
    if (RUNTIME / "access.json").exists():
        ACCESS_FILE = RUNTIME / "access.json"
    STATE_FILE = DATA / "state.json"
    PROFILE_DIR = DATA / "chrome-profile"
    LOG_DIR = DATA / "logs"
    STOP_FLAG = DATA / "stop.flag"
    LOCK_FILE = DATA / "bridge.lock"
    INBOX_DIR = DATA / "inbox"
    OUTBOX_DIR = DATA / "outbox"
    REPORTS_DIR = RUNTIME / "reports"
    STATUS_FILE = RUNTIME / "status.md"

# Every message the bridge itself writes. Override any of them in config.json "strings".
DEFAULT_STRINGS = {
    "user_label": "User",
    "claude_label": "Claude",
    "attachment": "[attachment: {name}]",
    "omitted": "[... earlier messages omitted]",
    "cap_hit": "(AI messages in a row: {n}. GPT is pausing until a human speaks.)",
    "status": "GPT bridge: browser {browser} ({mode}), {activity}; AI streak {streak}/{cap}; turns in this chat {turns}{limit}; chat <{url}>",
    "up": "up", "down": "down", "replying": "replying", "idle": "idle",
    "cap_set": "Loop cap set to {n}.",
    "url_set": "Switched conversation.",
    "needs_human_cmd": "{problem}. Send `!gpt show` to open the browser window, then `!gpt hide` when done.",
    "new_failed": "Could not start a new chat: {err}",
    "shown": "Browser window is open.",
    "hidden": "Browser is hidden again.",
    "shown_problem": "{problem}. Fix it in the window, then send `!gpt hide`.",
    "browser_failed": "Browser failed to start: {err}",
    "help": "Commands: `!gpt status` / `!gpt new` (with a handoff summary) / `!gpt new fresh` / `!gpt url <link>` / `!gpt cap <n>` / `!gpt show` / `!gpt hide`",
    "rollover_start": "Starting a new chat in the project ({reason}{handoff})...",
    "with_handoff": ", with a handoff summary",
    "reason_manual": "asked for",
    "reason_auto": "this chat reached {n} turns",
    "handoff_header": "[Handoff summary from the previous chat]",
    "halt_already": "⏸️ Already paused. Send `!resume` to continue.",
    "halt_claude": "The user paused the group: finish the step you are on, say where you stopped, start nothing new, do not message GPT. Running background tasks may finish; post their results after [Resume].",
    "halt_done": "⏸️ Paused: GPT stays quiet and Claude stops after its current step. Send `!resume` to continue.",
    "resume_not_halted": "▶️ Nothing is paused.",
    "resume_claude": "The user resumed the group: continue where you stopped, and say in one line what you continue with.",
    "resume_done": "▶️ Resumed.",
    "claude_limit": "⏸️ Claude hit its usage limit ({text}). Back around {when}; I will tell it to continue then. GPT keeps working meanwhile.",
    "limit_over_claude": "Your usage limit should have reset: continue the work that was cut off. Check `git worktree list` and the latest channel messages first, and report when done.",
    "ctx_claude": "Your context is about {k}k tokens. When no background task is running: write a handoff to `{handoff}` (what is done, what is in progress and where, next steps, questions for the user), then create the empty file `{restart}`. The bridge starts a fresh session that reads the handoff first.",
    "restart_start": "♻️ Claude asked for a fresh session (saves tokens); restarting...",
    "restart_resume": "♻️ Claude is switching its model or effort ({setting}); restarting in the same conversation...",
    "handoff_claude": "You are a fresh session replacing the previous Claude. Read `{handoff}` first, continue from it, and say in one line what you took over.",
    "status_halted": "Group: ⏸️ paused by the user (`!resume` to continue)",
    "status_running": "Group: ▶️ running",
    "status_claude_limit": "; Claude: ⏸️ usage limit, back around {when}",
    "status_claude_ctx": "; Claude context about {k}k tokens",
    "recent_header": "[Your most recent replies in the previous chat (excerpts); carry on from here]",
    "rollover_done": "New chat: <{url}>",
    "mirror_header": "**(asked directly in ChatGPT)**",
    "followed_chat": "You moved to another chat in the ChatGPT project; the bridge follows you there and posts it here: <{url}>",
    "attach_failed": "[these attachments could not be passed on: {names}]",
    "needs_human_ask": "{problem}; the message is kept. Send `!gpt show` to open the browser window and `!gpt hide` when done: it is then sent again.",
    "retry_gave_up": "(GPT did not get these messages after {n} tries: {reason}. They stay queued and go out with the next @GPT.)",
    "reason_too_long": "ChatGPT says the chat is too long",
    "ask_failed": "GPT bridge error ({err}); no reply for this one. Restarting the browser, please @GPT again in a moment.",
    "too_big": "(These files are over Discord's {limit} MB upload limit and were not sent; download them in ChatGPT: {files})",
    "empty": "(GPT returned nothing.)",
    "timeout": "(GPT timed out; above is what it had written so far.)",
    "problem_check": "ChatGPT is showing a human check",
    "problem_login": "ChatGPT needs you to log in",
    "problem_no_composer": "ChatGPT did not load (no input box found)",
    "page_retrying": "{problem}, probably rate limited; the message is kept and I keep trying on my own. Nothing to do.",
    "report_head": "\U0001f4e5 [report] <@{claude}> A report from another session: **{title}** ({n} chars)\nFull text: `{path}`\n\n",
    "status_head": "\U0001f4cc **Status** (updated by Claude {stamp})\n\n",
    "status_cut": "\n... (too long, the rest is cut)",
    "claude_asleep_limit": "💤 Claude's usage limit has not reset yet; it cannot be woken before about {when}.",
    "claude_waking": "💤 Claude is not running; waking it (about a minute)...",
    "claude_wake_failed": "Claude could not be started. Start its session by hand.",
    "wake_claude": "You are a freshly started session: the user mentioned you in Discord and that woke you. Read `{handoff}` first, check `git worktree list`, then answer the user's message in {where} (you did not receive it): \"{text}\"",
    # Coding-agent members (Codex, Gemini): {name} is the member's name.
    "agent_loop_cap": "(AI messages in a row: {n}. {name} is not starting; waiting for a human.)",
    "agent_failed": "({name} did not finish this time: {err}. The messages are kept; the next @{name} sends them again.)",
    "agent_limit": "{name} usage or rate limit reached: {err}",
    "agent_empty": "({name} returned nothing.)",
    "agent_stalled": "{name} produced nothing for {min} min and was stopped",
    "agent_timeout": "{name} ran longer than {min} min and was stopped",
    "agent_continue": "[The previous run was cut off: {err}] Continue the same task from where it stopped; do not start over. Check what is already done first.",
    "agent_too_big": "(These files are over Discord's upload limit and were not sent: {files})",
    "agent_recent_header": "[Your most recent replies in the previous session]",
    "agent_context_header": "[Group chat messages since your last reply]",
    "agent_files_header": "[Attachments sent with the messages, saved locally]",
    "agent_target": "[This reply will be posted in the {where}]",
    "agent_main": "main channel",
    "agent_log": "log channel",
    "agent_stopped": "{name}'s current turn was stopped.",
    "agent_not_running": "{name} is not running right now.",
    "agent_new": "The next @{name} starts a new {name} session.",
    "agent_status": "{name}: {state}; session {session}, {turns} turns so far; {pending} messages waiting for it; posting as {poster}.",
    "agent_running": "running", "agent_idle": "idle", "agent_new_session": "(new)",
    "agent_poster_bot": "the {name} bot", "agent_poster_gpt": "the GPT bot ({name} bot not logged in)",
    "agent_usage": " Last turn: model {model} ({tier}), {last} tokens; {total} tokens since the bridge started.",
    "agent_model_line": " Model {model}, effort {effort}.",
    "agent_default_value": "default",
    "agent_session_help": "[New sessions] Every turn re-reads your whole session, so a long one wastes quota. When your session has grown long (many turns, big files or logs read) or a big piece of work is finished and reported, write [[new-session]] in your reply: your next turn starts a fresh session that gets the intro and your last replies. Put anything the next session must know (what is done, what is open, where things are) in that reply. Do not do it in the middle of a task.",
    "agent_session_next": "({name} starts a fresh session from its next turn.)",
    "agent_model_words": "model|effort|reasoning",  # a request matching this regex gets the switch help
    "agent_switch_help": "[Your model and effort] You run on model {model}, effort {effort}. Pick the model and effort that fit the task: lighter for simple work, stronger only when the work needs it. To switch, write [[model:NAME]] and/or [[effort:LEVEL]] in your reply (NAME from {models}; LEVEL one of: {efforts}; \"default\" returns to the configured one); the bridge applies it from your next turn and says so in the channel. Only when {user} asks for it: {premium}. Do not change any config file yourself.",
    "agent_switched": "({name} uses {what} {value} from its next turn.)",
    "agent_choice_reset": "({name}'s CLI refused {choice}: dropped it; {name} now runs on model {model}, effort {effort}, and the turn goes on.)",
    "agent_choice_bare": "({name}'s CLI refused the configured {choice}: this turn runs on the CLI's own default model.)",
    "agent_switch_bad": "({name}: {what} \"{value}\" is not one of: {choices}. Nothing changed.)",
    "agent_switch_refused": "({name} wanted {value}, which only the owner can ask for. Nothing changed.)",
    "fresh_words": "fresh",  # `!gpt new <word>`: space-separated words that mean "no handoff summary"
    "tag_pause": "[Pause]", "tag_resume": "[Resume]", "tag_limit_over": "[Limit over]",
    "tag_context": "[Context]", "tag_handoff": "[Handoff]",
    "tag_switched": "[Model]",
    "tag_progress": "[Progress check]",
    "wake_progress": "You are a freshly started session: you were not running while the status board lists work in progress, so the bridge started you. Read `{handoff}` first and check `git worktree list`. Then: {text}",
    "wake_progress_resumed": "You were stopped (idle or crashed) while the status board lists work in progress, and have just been resumed in the same session. {text}",
    "progress_claude": "Nobody has moved for {min} min while the status board lists work in progress. Check where each item stands (background jobs, Codex or Gemini tasks); carry on with the next step that needs no decision from the user, and nudge Codex or Gemini in the log if their part is due. Post only what changed (results for the user in the main channel, details in the log); if nothing changed and nothing is due, post nothing.",
    "switched_claude": "You were restarted in the same conversation, now on {setting}. Say so in one line and continue where you stopped.",
    "claude_login_expired": "⚠️ Claude's login has expired, so it cannot answer. Run `claude` in a terminal, enter `/login`, then @Claude again. GPT and Codex are not affected.",
    "claude_not_logged_in": "⚠️ Claude started but is not logged in, so it cannot answer. Run `claude` in a terminal, enter `/login`, then @Claude again. GPT and Codex are not affected.",
    "claude_stuck_startup": "⚠️ Claude was started but never came up: it is waiting at a startup question in its hidden window, so it receives nothing. Its screen shows:\n```\n{screen}\n```\nAnswer it on the computer (or settle what it asks), then restart this project's bridge. GPT and Codex are not affected.",
    "claude_stuck_unknown": "⚠️ Claude was started but never came up (its Discord plugin is not running), so it receives nothing. Its screen could not be read; restart this project's bridge on the computer. GPT and Codex are not affected.",
    "missed_claude": "While you were starting, these messages mentioned you. You may not have received them; answer the ones you have not answered:\n{text}",
    "wake_claude_resumed": "You were stopped while idle and have just been resumed in the same session. Answer the user's message in {where} (you did not receive it): \"{text}\"",
    "pin_failed": "(I could not pin this status message: the GPT bot lacks the Pin Messages permission. Pin it once by hand.)",
}
STR = dict(DEFAULT_STRINGS)


def S(key: str, **kw) -> str:
    return STR[key].format(**kw)


DEFAULT_INTRO = (
    "[Group chat] From now on I will relay messages from a Discord channel to you. "
    "Members: the user, Claude (another AI, working in the user's repository), and you (GPT). "
    "Each message starts with its speaker, like [User] or [Claude]. Your reply is posted to the channel as is. "
    "To get Claude to respond, write @Claude in your reply; leave it out otherwise. "
    "Images and files posted in the channel are uploaded to you, and images and files you produce are posted back. "
    "Keep replies short."
)

# pythonw has no console: give print() and libraries somewhere to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = logging.getLogger("bridge")

DISCORD_LIMIT = 1990  # Discord rejects > 2000; leave room for a closing code fence.


# ---------------------------------------------------------------- config / state

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as e:  # corrupt file: keep running on defaults
        log.warning("could not read %s: %s", path.name, e)
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = load_json(CONFIG_FILE, {})
    if cfg.get("strings_file"):  # shared wording for several profiles; "strings" still wins
        shared = Path(os.path.expanduser(cfg["strings_file"]))
        if not shared.is_absolute():
            shared = CONFIG_FILE.parent / shared
        STR.update(load_json(shared, {}))
    cfg.setdefault("loop_cap", 4)
    cfg.setdefault("reply_timeout_s", 900)
    cfg.setdefault("first_token_timeout_s", 120)
    cfg.setdefault("quiet_period_s", 3)
    cfg.setdefault("context_max_chars", 12000)
    cfg.setdefault("reload_if_idle_s", 120)
    cfg.setdefault("browser_mode", "headless")
    cfg.setdefault("browser_channel", "chrome")
    cfg.setdefault("mirror_interval_s", 60)   # 0 = do not mirror turns typed in ChatGPT itself
    cfg.setdefault("rollover_turns", 40)      # 0 = never start a new chat automatically
    cfg.setdefault("archive_dir", "")         # "" = no local copy of the Discord channels
    cfg.setdefault("browser_idle_close_s", 180)  # close the headless browser after this long unused; 0 = keep it open
    cfg.setdefault("handoff_prompt", "[Handoff] This chat is getting long, so I am moving to a new chat in the same project. Write a handoff summary (under 1500 words): goals, decisions made, tasks in progress, to-dos, who does what. Output only the summary.")
    cfg.setdefault("intro", DEFAULT_INTRO)
    STR.update(cfg.get("strings", {}))
    sel = cfg.setdefault("selectors", {})
    for k, v in DEFAULT_SELECTORS.items():
        sel.setdefault(k, v)
    return cfg


# ChatGPT web UI as of 2026-11 (fix these in config.json when the page changes).
DEFAULT_SELECTORS = {
    "composer": "[data-chatgpt-composer] [contenteditable='true'][role='textbox']",
    "send": "[data-chatgpt-composer] button[type='submit']",
    "stop": "[data-chatgpt-composer] button[aria-label^='Stop'], [data-chatgpt-composer] button[data-testid*='stop'], button[data-testid='stop-button']",
    "turn": "[data-turn-key]",
    "turn_state": "[data-talvt-turn-state]",
    "turn_done": "complete",
    "user_unit": "[data-user-message-bubble]",
    "assistant_content": "[data-markdown-copy='contents']",
    "copy_button": "button[aria-label='Copy']",
    "file_input": "[data-chatgpt-composer] input[type='file'][aria-label='Attach files']",
    "attach_busy": "[data-composer-attachments] [aria-busy='true'], [data-composer-attachments] [role='progressbar']",
    "user_part": "[data-content-search-unit-key$=':user'], [data-chatgpt-search-unit-key$=':user']",
    "download_button": "button[aria-label='Download file'], button[aria-label='Download this image'], button[aria-label='Download']",
}

INBOX_DIR = DATA / "inbox"    # Discord attachments on their way to ChatGPT
OUTBOX_DIR = DATA / "outbox"  # ChatGPT files and images on their way to Discord
REPORTS_DIR = DATA / "reports"  # other Claude Code sessions drop finished reports here
STATUS_FILE = DATA / "status.md"  # Claude keeps this current; shown pinned in the channel
REPORT_EXCERPT = 800
MAX_UPLOAD_FILES = 10
MAX_DISCORD_FILE = 10 * 1024 * 1024  # bot upload limit in a server without boosts


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "").strip()) or "file"
    return name[-120:]


def prune_dir(path: Path, max_age_s: float = 2 * 86400) -> None:
    """Delete files older than max_age_s (inbox/outbox only)."""
    now = time.time()
    for p in path.glob("**/*"):
        try:
            if p.is_file() and now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except Exception:
            pass


def read_token() -> str:
    """GPT_BOT_TOKEN from the environment, else from gpt-bot.env in the repository root."""
    if os.environ.get("GPT_BOT_TOKEN"):
        return os.environ["GPT_BOT_TOKEN"].strip()
    try:
        for line in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*GPT_BOT_TOKEN\s*=\s*(\S+)\s*$", line)
            if m:
                return m.group(1)
    except FileNotFoundError:
        pass
    raise SystemExit(f"GPT_BOT_TOKEN missing: set it in the environment or in {TOKEN_FILE}")


def setup_logging(console: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "bridge.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("discord").setLevel(logging.WARNING)


def acquire_lock():
    """Single instance (the browser profile allows one user): hold a lock on
    data/bridge.lock for the life of the process. Returns None if another bridge holds it."""
    DATA.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.write(fd, str(os.getpid()).encode())
    return fd


# ---------------------------------------------------------------- text helpers

def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split for Discord, preferring paragraph/line breaks, keeping code fences balanced."""
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        para = text.rfind("\n\n", 0, limit)
        line = text.rfind("\n", 0, limit)
        space = text.rfind(" ", 0, limit)
        if para > limit // 2:
            cut = para
        elif line > limit // 2:
            cut = line
        elif space > limit // 4:
            cut = space
        else:
            cut = limit
        piece, rest = text[:cut], text[cut:].lstrip("\n")
        open_lang = None
        for ln in piece.splitlines():
            m = re.match(r"^\s*```(\S*)", ln)
            if m:
                open_lang = None if open_lang is not None else m.group(1)
        if open_lang is not None:
            piece += "\n```"
            rest = f"```{open_lang}\n" + rest
        chunks.append(piece)
        text = rest
    if text:
        chunks.append(text)
    return chunks


TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _width(s: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _render_table(rows: list[list[str]], max_width: int) -> list[str]:
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [max(_width(r[k]) for r in rows) for k in range(ncol)]
    if sum(widths) + 2 * (ncol - 1) <= max_width:
        fmt = lambda r: "  ".join(c + " " * (widths[k] - _width(c)) for k, c in enumerate(r)).rstrip()
        return ["```", fmt(rows[0]), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows[1:]] + ["```"]
    head, out = rows[0], []
    for r in rows[1:]:
        rest = "; ".join(f"{head[k]}: {r[k]}" for k in range(1, ncol) if r[k])
        out.append(f"- **{r[0]}**" + (f" — {rest}" if rest else ""))
    return out


def _clean_cell(c: str) -> str:
    return re.sub(r"\*\*|`", "", c).strip()


def _tab_cols(line: str) -> int:
    return len(line.rstrip("\r").split("\t")) if "\t" in line else 0


def fence_tables(text: str, max_width: int = 60) -> str:
    """Discord does not render markdown tables. A narrow table becomes an aligned code
    block; a wide one becomes a list (one bullet per row), which reads better on a phone.
    Handles markdown pipe tables and the tab-separated tables that ChatGPT's Copy button
    sometimes produces."""
    lines = text.split("\n")
    out, i, in_code = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
        if not in_code and line.strip().startswith("|") and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            rows, j = [], i
            while j < len(lines) and lines[j].strip().startswith("|"):
                if not TABLE_SEP_RE.match(lines[j]):
                    raw = lines[j].strip().strip("|").replace("\\|", "\x00")
                    rows.append([_clean_cell(c).replace("\x00", "|") for c in raw.split("|")])
                j += 1
            out += _render_table(rows, max_width)
            i = j
            continue
        n = 0 if in_code else _tab_cols(line)
        if n >= 2 and i + 1 < len(lines) and _tab_cols(lines[i + 1]) == n:
            rows, j = [], i
            while j < len(lines) and _tab_cols(lines[j]) == n:
                rows.append([_clean_cell(c) for c in lines[j].rstrip("\r").split("\t")])
                j += 1
            out += _render_table(rows, max_width)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


ADDRESS_RE = re.compile(r"(?:^\s*gpt\s*[,\uff0c:\uff1a])|(?:@gpt\b)", re.I)


# ---------------------------------------------------------------- ChatGPT driver

DOM_TO_MARKDOWN = r"""
(root) => {
  const SKIP = new Set(['BUTTON', 'SVG', 'STYLE', 'SCRIPT', 'NOSCRIPT', 'TEXTAREA', 'INPUT']);
  const inline = (n, d) => Array.from(n.childNodes).map(c => conv(c, d)).join('');
  function list(n, depth) {
    let s = '', i = parseInt(n.getAttribute('start') || '1', 10);
    for (const li of n.children) {
      if (li.tagName !== 'LI') continue;
      const prefix = n.tagName === 'OL' ? (i++) + '. ' : '- ';
      let body = '', nested = '';
      for (const c of li.childNodes) {
        if (c.nodeType === 1 && (c.tagName === 'UL' || c.tagName === 'OL')) nested += list(c, depth + 1);
        else body += conv(c, depth);
      }
      s += '  '.repeat(depth) + prefix + body.trim().replace(/\n{2,}/g, '\n') + '\n' + nested;
    }
    return depth === 0 ? s + '\n' : s;
  }
  function conv(n, depth) {
    if (n.nodeType === 3) return n.nodeValue;
    if (n.nodeType !== 1) return '';
    let t = n.tagName.toUpperCase();
    if (SKIP.has(t)) return '';
    if (n.getAttribute('data-d-component') === 'table') t = 'TABLE';
    if (n.classList.contains('sr-only')) return '';
    if (n.classList.contains('katex')) {
      const tex = n.querySelector('annotation[encoding="application/x-tex"]');
      if (tex) return n.closest('.katex-display') ? '\n$$' + tex.textContent + '$$\n' : '$' + tex.textContent + '$';
    }
    switch (t) {
      case 'BR': return '\n';
      case 'HR': return '\n---\n\n';
      case 'P': return inline(n, depth).trim() + '\n\n';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': {
        const k = Math.min(3, parseInt(t[1], 10));
        return '#'.repeat(k) + ' ' + inline(n, depth).trim() + '\n\n';
      }
      case 'STRONG': case 'B': { const s = inline(n, depth); return s.trim() ? '**' + s + '**' : s; }
      case 'EM': case 'I': { const s = inline(n, depth); return s.trim() ? '*' + s + '*' : s; }
      case 'DEL': case 'S': return '~~' + inline(n, depth) + '~~';
      case 'CODE': {
        const s = n.textContent;
        if (!s.includes('\n') || n.closest('pre')) return '`' + s + '`';
        const m = (n.className || '').match(/language-([\w+#-]+)/);
        return '\n```' + (m ? m[1] : '') + '\n' + s.replace(/\n+$/, '') + '\n```\n\n';
      }
      case 'PRE': {
        const code = n.querySelector('code');
        let lang = '';
        if (code) { const m = (code.className || '').match(/language-([\w+#-]+)/); if (m) lang = m[1]; }
        const body = (code ? code.textContent : n.innerText).replace(/\n+$/, '');
        return '\n```' + lang + '\n' + body + '\n```\n\n';
      }
      case 'A': {
        const s = inline(n, depth).trim(), href = n.getAttribute('href') || '';
        if (!href || href.startsWith('#') || !s) return s;
        return s === href ? href : '[' + s + '](' + href + ')';
      }
      case 'UL': case 'OL': return list(n, depth);
      case 'BLOCKQUOTE':
        return inline(n, depth).trim().split('\n').map(l => '> ' + l).join('\n') + '\n\n';
      case 'TABLE': {
        const rows = Array.from(n.querySelectorAll('tr')).map(tr =>
          '| ' + Array.from(tr.children).map(td => td.innerText.trim().replace(/\|/g, '\\|').replace(/\n/g, ' ')).join(' | ') + ' |');
        if (rows.length > 1) {
          const cols = n.querySelector('tr').children.length;
          rows.splice(1, 0, '|' + ' --- |'.repeat(cols));
        }
        return '\n```\n' + rows.join('\n') + '\n```\n\n';
      }
      default: return inline(n, depth);
    }
  }
  return conv(root, 0).replace(/\n{3,}/g, '\n\n').trim();
}
"""

# The last conversation turn (one user message + the assistant reply to it).
LAST_TURN_JS = r"""
(sel) => {
  const conv = __DOM_TO_MARKDOWN__;
  const turns = document.querySelectorAll(sel.turn);
  const last = turns[turns.length - 1];
  if (!last) return { key: null, count: 0, state: null, userText: '', text: '' };
  const stAttr = sel.turn_state.replace(/[\[\]]/g, '').split('=')[0];
  const st = last.matches(sel.turn_state) ? last : last.querySelector(sel.turn_state);
  const user = last.querySelector(sel.user_unit);
  const contents = Array.from(last.querySelectorAll(sel.assistant_content));
  // Reply images and files: anywhere in the turn except the user's own message.
  const reply = e => !e.closest(sel.user_part);
  const bigImg = i => reply(i) && i.alt !== 'User attachment' && !i.closest('a') && i.naturalWidth >= 100 && i.naturalHeight >= 100 && i.getBoundingClientRect().width >= 96;
  const imgs = Array.from(last.querySelectorAll('img')).filter(bigImg).length;
  let fallback = '';
  if (!contents.some(c => c.innerText.trim())) {
    // Unknown reply shape: its visible text, without the user's message, buttons and labels.
    const clone = last.cloneNode(true);
    clone.querySelectorAll(sel.user_part + ', button, h4, svg, [aria-hidden="true"], .sr-only').forEach(e => e.remove());
    fallback = (clone.innerText || clone.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
  }
  const downloads = Array.from(last.querySelectorAll(sel.download_button)).filter(reply).length;
  // The turn-level Copy button appears once the reply is finished.
  const copy = Array.from(last.querySelectorAll(sel.copy_button))
    .filter(b => !b.closest(sel.assistant_content) && !b.closest(sel.user_unit)).length;
  return {
    key: last.getAttribute('data-turn-key') || String(turns.length),
    count: turns.length,
    state: st ? st.getAttribute(stAttr) : null,
    userText: user ? user.innerText : '',
    text: contents.map(conv).filter(s => s.trim()).join('\n\n'),
    imgs, downloads, fallback, copy,
  };
}
""".replace("__DOM_TO_MARKDOWN__", DOM_TO_MARKDOWN.strip())

# Every turn after the one with key sel.seen: [{key, state, userText, text}], oldest first.
# found=false when that key is not on the page (another chat, or turns not rendered).
TURNS_AFTER_JS = r"""
(sel) => {
  const conv = __DOM_TO_MARKDOWN__;
  const turns = Array.from(document.querySelectorAll(sel.turn));
  const stAttr = sel.turn_state.replace(/[\[\]]/g, '').split('=')[0];
  const keyOf = (t, i) => t.getAttribute('data-turn-key') || String(i + 1);
  const at = sel.seen ? turns.findIndex((t, i) => keyOf(t, i) === sel.seen) : -1;  // "" = from the start
  if (sel.seen && at < 0) return { found: false, turns: [] };
  return { found: true, turns: turns.slice(at + 1).map((t, j) => {
    const st = t.matches(sel.turn_state) ? t : t.querySelector(sel.turn_state);
    const user = t.querySelector(sel.user_unit);
    let text = Array.from(t.querySelectorAll(sel.assistant_content)).map(conv).filter(s => s.trim()).join('\n\n');
    if (!text) {
      // Unknown reply shape: its visible text, without the user's message, buttons and labels.
      const clone = t.cloneNode(true);
      clone.querySelectorAll(sel.user_part + ', button, h4, svg, [aria-hidden="true"], .sr-only').forEach(e => e.remove());
      text = (clone.innerText || clone.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
    }
    return {
      key: keyOf(t, at + 1 + j),
      state: st ? st.getAttribute(stAttr) : null,
      userText: user ? user.innerText : '',
      text,
    };
  }) };
}
""".replace("__DOM_TO_MARKDOWN__", DOM_TO_MARKDOWN.strip())

# Keep whatever the page copies inside the page (window.__bridgeCopied) instead of the
# system clipboard, so the reply's own Copy button gives us its markdown source.
COPY_HOOK_JS = r"""
(() => {
  const store = t => { window.__bridgeCopied = String(t); };
  try {
    const c = navigator.clipboard;
    if (c) {
      c.writeText = async t => { store(t); };
      c.write = async items => {
        try {
          for (const it of items) {
            if (it.types.includes('text/plain')) { store(await (await it.getType('text/plain')).text()); break; }
          }
        } catch (e) {}
      };
    }
  } catch (e) {}
  window.addEventListener('copy', e => {
    try { const t = e.clipboardData && e.clipboardData.getData('text/plain'); if (t) store(t); } catch (x) {}
  });
})();
"""

# Click the turn-level Copy button of the last turn (not a code block's or a writing block's own).
CLICK_COPY_JS = r"""
(sel) => {
  const turns = document.querySelectorAll(sel.turn);
  const last = turns[turns.length - 1];
  if (!last) return false;
  const btns = Array.from(last.querySelectorAll(sel.copy_button))
    .filter(b => !b.closest(sel.assistant_content) && !b.closest(sel.user_unit));
  const b = btns[btns.length - 1];
  if (!b) return false;
  window.__bridgeCopied = null;
  b.click();
  return true;
}
"""

# Images in the reply part of the last turn (not the user's own attachments, not icons).
REPLY_IMAGES_JS = r"""
(sel) => {
  const turns = document.querySelectorAll(sel.turn);
  const last = turns[turns.length - 1];
  if (!last) return [];
  const seen = new Set(), out = [];
  for (const img of last.querySelectorAll('img')) {
    const src = img.currentSrc || img.src;
    if (!src || seen.has(src) || img.closest(sel.user_part) || img.closest('a') || img.alt === 'User attachment') continue;
    if (img.naturalWidth < 100 || img.naturalHeight < 100 || img.getBoundingClientRect().width < 96) continue;
    seen.add(src);
    out.push({ src, alt: img.alt || '' });
  }
  return out;
}
"""

# Tag the reply's own download buttons in the last turn as data-bridge-dl=0..n-1; return n.
TAG_DOWNLOADS_JS = r"""
(sel) => {
  document.querySelectorAll('[data-bridge-dl]').forEach(b => b.removeAttribute('data-bridge-dl'));
  const turns = document.querySelectorAll(sel.turn);
  const last = turns[turns.length - 1];
  if (!last) return 0;
  const btns = Array.from(last.querySelectorAll(sel.download_button)).filter(b => !b.closest(sel.user_part));
  btns.forEach((b, i) => b.setAttribute('data-bridge-dl', String(i)));
  return btns.length;
}
"""

# Read a blob:/data: image from inside the page as [mime, base64].
FETCH_B64_JS = r"""
async (src) => {
  const r = await fetch(src);
  const b = await r.blob();
  const u8 = new Uint8Array(await b.arrayBuffer());
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return [b.type, btoa(s)];
}
"""

PASTE_JS = r"""
([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  el.focus();
  if (el.tagName === 'TEXTAREA') {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, text);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }
  const dt = new DataTransfer();
  dt.setData('text/plain', text);
  el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  return true;
}
"""


MIRRORED_MAX = 2000  # web turns remembered as already seen (posted or not)
WEB_BLANK_LOOKS = 4    # looks a finished last turn may show no reply before it is given up
WEB_PARTIAL_LOOKS = 3  # looks a last turn may lack its Copy button before it is posted as it is


def pick_web_turns(turns: list[dict], seen: set[str], waiting: set[str] = frozenset()) -> tuple[list[dict], list[str], bool]:
    """Which turns of a chat page are news. turns: every turn on the page, oldest first; seen: keys
    of turns the bridge has seen before (in any chat); waiting: keys of turns found new on an
    earlier look but not ready then. Returns (new, late, first):
    new   - the turns after the newest seen turn, plus waiting ones wherever they are;
    late  - keys of unseen turns before the newest seen turn (old turns the page had not shown
            yet): history, to be recorded and never posted;
    first - True when no turn on the page was seen before: a chat looked at for the first time,
            where everything is history."""
    at = max((i for i, t in enumerate(turns) if t["key"] in seen), default=-1)
    if at < 0:
        return [], [], bool(turns)
    new = [t for i, t in enumerate(turns) if t["key"] not in seen and (i > at or t["key"] in waiting)]
    late = [t["key"] for i, t in enumerate(turns) if i < at and t["key"] not in seen and t["key"] not in waiting]
    return new, late, False
PAGE_FAILS_NOTE = 3  # page loads failed in a row before the channel is told the bridge is retrying
PAGE_FAILS_ASK = 8   # ... before the user is asked to look after all (a login page we did not recognize?)


def conv_id(url: str) -> str:
    m = re.search(r"/c/([0-9a-f-]{8,})", url or "")
    return m.group(1) if m else (url or "").split("?")[0]


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


LIMIT_RE = re.compile(r"hit your (session |5-hour |weekly )?limit|usage limit|limit reached|out of (extra )?usage", re.I)
LOGIN_RE = re.compile(r"login expired|please run /login|not logged in|invalid api key|oauth token", re.I)
RESET_RE = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I)


def parse_reset(text: str) -> float | None:
    """'... resets 2:50am (America/Los_Angeles)' -> next such moment, as a unix time."""
    import datetime as dt

    m = RESET_RE.search(text or "")
    if not m:
        return None
    hour, minute, ampm, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(zone) if zone else None
    except Exception:
        tz = None
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    at = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += dt.timedelta(days=1)
    return at.timestamp()


class ReplyFailed(Exception):
    """ChatGPT did not answer our message: it showed an error, or the message vanished."""

    def __init__(self, reason: str, too_long: bool = False):
        super().__init__(reason)
        self.too_long = too_long


ERROR_RE = re.compile(r"something went wrong|unknown error|error (generating|occurred)|network error|try again|retry|\u51fa\u9519|\u9519\u8bef|\u91cd\u8bd5|\u8bf7\u7a0d\u540e", re.I)
TOO_LONG_RE = re.compile(r"too long|too large|maximum (context|length)|context (window|length)|start (a )?new chat|\u5bf9\u8bdd\u592a\u957f|\u8fc7\u957f|\u65b0\u5bf9\u8bdd", re.I)


class NeedsHuman(Exception):
    """The ChatGPT page needs the user: login, or a check only a person may click."""


class PageNotLoaded(Exception):
    """The ChatGPT page did not come up (no input box), and it shows no login or verification
    page either: a rate limit or a half-loaded page. Nothing for the user to do; try again later."""


def page_problem(url: str, title: str, body: str) -> str | None:
    t, b = (title or "").lower(), (body or "")[:3000].lower()
    if "just a moment" in t or "challenges.cloudflare.com" in b or "verify you are human" in b:
        return S("problem_check")
    if "/auth/" in url or "auth.openai.com" in url or "log in" in t:
        return S("problem_login")
    return None


class ChatGPT:
    def __init__(self, cfg: dict, state: dict):
        self.cfg = cfg
        self.state = state
        self.sel = cfg["selectors"]
        self.pw = None
        self.ctx = None
        self.page = None
        self.mode = cfg["browser_mode"]
        self.last_used = 0.0
        self.last_key: str | None = None  # last turn the bridge itself produced
        self.too_large = False  # ChatGPT answered our last send with 413 conversation_too_large
        self.crashed = False  # the page's renderer died: page calls would hang, so start a new browser
        self.throttled_until = 0.0  # ChatGPT said 429: no page loads before this time
        self.last_429_at = 0.0  # time.monotonic() of the last 429
        self.loaded_at = 0.0  # time.monotonic() of the last full load of the chat page

    @property
    def chat_url(self) -> str:
        return self.state.get("chat_url") or self.cfg["chat_url"]

    async def start(self, mode: str | None = None) -> None:
        from playwright.async_api import async_playwright

        await self.close()
        self.mode = mode or self.mode
        if self.pw is None:
            self.pw = await async_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]
        kw = dict(
            user_data_dir=str(PROFILE_DIR),
            channel=self.cfg["browser_channel"] or None,
            headless=self.mode == "headless",
            args=args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
        )
        if self.mode == "headless" and self.state.get("user_agent"):
            kw["user_agent"] = self.state["user_agent"]
        self.ctx = await self.pw.chromium.launch_persistent_context(**kw)
        await self.ctx.add_init_script(script=COPY_HOOK_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.crashed = False
        self.page.on("response", self._on_response)
        self.page.on("crash", self._on_crash)
        if self.mode == "headless":
            ua = await self.page.evaluate("navigator.userAgent")
            if "HeadlessChrome" in ua:
                self.state["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
                save_json(STATE_FILE, self.state)
                log.info("headless user agent fixed; relaunching")
                await self.start("headless")
                return
        log.info("browser started (%s)", self.mode)
        await self.open_chat()

    def _on_response(self, resp) -> None:
        # The chat is full: ChatGPT shows only "Unknown error / Start new chat".
        if resp.status == 413 and "/conversation" in resp.url and resp.request.method == "POST":
            self.too_large = True
            log.warning("ChatGPT rejected the message: conversation too large (413)")
        # Constant reloads can get the account throttled (HTTP 429). Remember how long to keep away;
        # retrying sooner only extends it.
        if resp.status == 429 and "chatgpt.com" in resp.url:
            try:
                wait = int(resp.headers.get("retry-after", "") or 300)
            except ValueError:
                wait = 300
            until = time.time() + min(max(wait, 60), 1800)
            self.last_429_at = time.monotonic()
            log.info("429 on %s %s (retry-after=%s)", resp.request.method, urlsplit(resp.url).path[:120],
                     resp.headers.get("retry-after", "-"))
            if until > self.throttled_until:
                self.throttled_until = until
                log.warning("ChatGPT answered 429 (rate limited); keeping away for %d s", until - time.time())

    def _on_crash(self, page) -> None:
        # The tab's renderer died: every later page call would hang and hold the page lock
        # for hours. Mark it; the next use starts a new browser.
        self.crashed = True
        log.warning("the ChatGPT page crashed; the browser will be restarted")

    async def close(self) -> None:
        if self.ctx is not None:
            try:
                await asyncio.wait_for(self.ctx.close(), 30)  # a crashed browser may not answer
            except Exception:
                pass
        self.ctx = None
        self.page = None

    async def shutdown(self) -> None:
        await self.close()
        if self.pw is not None:
            try:
                await self.pw.stop()
            except Exception:
                pass
            self.pw = None

    def alive(self) -> bool:
        return self.page is not None and not self.page.is_closed() and not self.crashed

    async def open_chat(self, url: str | None = None) -> None:
        url = url or self.chat_url
        log.info("loading %s", urlsplit(url).path[:80])
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await self.wait_composer()
        self.last_used = self.loaded_at = time.monotonic()

    async def wait_composer(self, timeout_s: float = 45) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if await self.page.locator(self.sel["composer"]).first.is_visible():
                return
            problem = page_problem(self.page.url, await self.page.title(), await self._body())
            if problem and time.monotonic() > end - timeout_s + 8:
                raise NeedsHuman(problem)
            await asyncio.sleep(1)
        problem = page_problem(self.page.url, await self.page.title(), await self._body())
        if problem:
            raise NeedsHuman(problem)
        raise PageNotLoaded(S("problem_no_composer"))

    async def _body(self) -> str:
        try:
            return await self.page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''")
        except Exception:
            return ""

    async def ensure_ready(self) -> None:
        if not self.alive():
            await self.start()
            return
        if time.monotonic() - self.last_used > self.cfg["reload_if_idle_s"] or conv_id(self.page.url) != conv_id(self.chat_url):
            await self.open_chat()  # pick up anything typed in this chat elsewhere

    async def last_turn(self) -> dict:
        return await self.page.evaluate(LAST_TURN_JS, self.sel)

    async def dump_turn(self, reason: str) -> None:
        """Save the last turn's HTML to the log folder, to fix the reader later."""
        try:
            html = await self.page.evaluate(
                "(sel) => { const t = document.querySelectorAll(sel); return t.length ? t[t.length - 1].outerHTML : document.body.outerHTML }",
                self.sel["turn"])
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / f"turn-{reason}-{time.strftime('%Y%m%d-%H%M%S')}.html").write_text(html[:3_000_000], encoding="utf-8")
        except Exception as e:
            log.warning("dump_turn failed: %s", e)

    async def copied_reply(self) -> str | None:
        """The last reply's markdown, through its own Copy button; None if that fails."""
        try:
            if not await self.page.evaluate(CLICK_COPY_JS, self.sel):
                return None
            for _ in range(10):
                text = await self.page.evaluate("window.__bridgeCopied")
                if text and text.strip():
                    return text.strip()
                await asyncio.sleep(0.3)
        except Exception as e:
            log.warning("copy button failed: %s", e)
        return None

    async def _wait_uploads(self, timeout_s: float = 180) -> None:
        end = time.monotonic() + timeout_s
        idle = 0
        await asyncio.sleep(1.5)
        while time.monotonic() < end:
            busy = await self.page.locator(self.sel["attach_busy"]).count()
            idle = idle + 1 if busy == 0 else 0
            if idle >= 2:
                return
            await asyncio.sleep(1)
        raise RuntimeError("file upload to ChatGPT did not finish")

    async def _send(self, text: str, files: list[Path] | None = None) -> str:
        self.too_large = False
        before = await self.last_turn()
        box = self.page.locator(self.sel["composer"]).first
        await box.click()
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Delete")
        if files:
            await self.page.locator(self.sel["file_input"]).first.set_input_files([str(f) for f in files])
            await self._wait_uploads()
            await box.click()
        await self.page.evaluate(PASTE_JS, [self.sel["composer"], text])
        await asyncio.sleep(0.5)
        if not (await box.inner_text()).strip():
            await self.page.keyboard.insert_text(text)
            await asyncio.sleep(0.5)
        send = self.page.locator(self.sel["send"]).first
        try:
            await send.wait_for(state="visible", timeout=10_000)
            for _ in range(20):
                if await send.is_enabled():
                    break
                await asyncio.sleep(0.5)
            await send.click()
        except Exception:
            await box.press("Enter")
        # The new user turn must appear, otherwise nothing was sent.
        # A new turn holding our text must appear, otherwise nothing was sent.
        snippet = squash(text)[:20]
        end = time.monotonic() + 30
        while time.monotonic() < end:
            now = await self.last_turn()
            if now["key"] != before["key"] and snippet in squash(now["userText"]):
                return snippet
            await asyncio.sleep(0.5)
        raise RuntimeError("message did not appear in the chat after sending")

    async def _wait_reply(self, snippet: str | None = None) -> tuple[str, bool]:
        """Return (text, finished). finished=False means timeout with partial text.
        Raises ReplyFailed when ChatGPT shows an error or our message disappears."""
        start = time.monotonic()
        stop_sel = self.sel["stop"]
        prev, stable, empty_done, gone = None, 0, 0, 0
        while True:
            elapsed = time.monotonic() - start
            if self.too_large:
                await self.dump_turn("too-large")
                raise ReplyFailed("conversation_too_large", too_long=True)
            turn = await self.last_turn()
            if snippet:
                gone = gone + 1 if snippet not in squash(turn["userText"]) else 0
                if gone >= 3:  # ChatGPT took the message back (failed send, server error)
                    raise ReplyFailed("ChatGPT dropped the message")
            text = turn["text"]
            generating = await self.page.locator(stop_sel).first.is_visible()
            done_state = turn["state"] == self.sel["turn_done"]
            if turn["state"] is not None and not done_state:
                generating = True
            # A reply can be text, images, files, or any mix of them.
            seen = (text, turn["imgs"], turn["downloads"])
            has_output = bool(text or turn["imgs"] or turn["downloads"])
            if has_output and not generating:
                stable = stable + 1 if seen == prev else 0
                if stable >= 2:
                    copied = await self.copied_reply() if text else None
                    return copied or text, True
            prev = seen
            empty_done = empty_done + 1 if (done_state and not generating and not has_output) else 0
            if empty_done >= 5 and turn.get("fallback"):
                fb = turn["fallback"]
                if len(fb) < 500 and (ERROR_RE.search(fb) or TOO_LONG_RE.search(fb)):
                    await self.dump_turn("error")
                    raise ReplyFailed(fb[:300], too_long=bool(TOO_LONG_RE.search(fb)))
                if len(fb) >= 40:  # shorter is a status line such as "Thinking"
                    await self.dump_turn("unparsed")
                    log.warning("reply shape not parsed; sending its visible text (%d chars)", len(fb))
                    return fb, True
            if not has_output and not generating and elapsed > self.cfg["first_token_timeout_s"]:
                if done_state:
                    await self.dump_turn("empty")
                    raise ReplyFailed("ChatGPT finished without a reply")
                problem = page_problem(self.page.url, await self.page.title(), await self._body())
                if problem:
                    raise NeedsHuman(problem)
                raise RuntimeError("ChatGPT gave no reply")
            if elapsed > self.cfg["reply_timeout_s"]:
                return text, False
            await asyncio.sleep(1.0)

    async def ask(self, text: str, files: list[Path] | None = None, collect: bool = True) -> tuple[str, bool, list[Path]]:
        """Send text (+ files); return (reply text, finished, files/images from the reply)."""
        await self.ensure_ready()
        snippet = await self._send(text, files)
        reply, finished = await self._wait_reply(snippet)
        outputs = await self.outputs_of_last_turn() if collect else []
        self.last_key = (await self.last_turn())["key"]
        self.last_used = time.monotonic()
        return reply, finished, outputs

    async def outputs_of_last_turn(self) -> list[Path]:
        try:
            return await self.collect_outputs(OUTBOX_DIR / time.strftime("%Y%m%d-%H%M%S"))
        except Exception:
            log.exception("collecting reply files failed")
            return []

    async def page_turns(self) -> list[dict]:
        """Every turn on the open page, oldest first: [{key, state, userText, text}]."""
        return (await self.page.evaluate(TURNS_AFTER_JS, {**self.sel, "seen": ""}))["turns"]

    async def check_for_web_turns(self, seen: set[str] | frozenset[str], fresh_s: float = 0,
                                  wait_busy_s: float = 0) -> dict | None:
        """Reload the chat and look for turns typed in ChatGPT itself. seen: keys of every turn the
        bridge has seen before. Returns {key, items, seen}: items are the finished new turns,
        [{key, user, text, outputs}], oldest first; seen lists further keys to record as seen without
        posting (history, turns the bridge sent, turns without a reply); key is the newest of all
        these. None when there is nothing to post or record.
        A turn is news only when it comes after a turn seen before. A chat with no turn seen before
        is being looked at for the first time: all of it is history, nothing is posted. A turn still
        being answered, or without its reply yet, is left for the next look.
        A page loaded less than fresh_s ago is not loaded again. With wait_busy_s, a turn that is
        still being answered is waited for (the caller is about to send, and would skip it)."""
        if self.crashed or not self.alive():
            await self.start()  # the browser is closed while idle; a check opens it again
            fresh_s = max(fresh_s, 30)  # start() has just loaded the chat: do not load it twice
        self.last_used = time.monotonic()
        if not (fresh_s and time.monotonic() - getattr(self, "loaded_at", 0.0) < fresh_s
                and conv_id(self.page.url) == conv_id(self.chat_url)):
            await self.open_chat()
        turn = await self.last_turn()
        for _ in range(15):  # turns render a moment after the composer appears
            if turn["key"] and turn["state"] is not None:
                break
            await asyncio.sleep(1)
            turn = await self.last_turn()
        # Turns keep appearing for a while after the first ones (image turns come late): a page read
        # too early ends at an older turn. Wait until the count holds.
        count = -1
        for _ in range(10):
            if turn["count"] == count:
                break
            count = turn["count"]
            await asyncio.sleep(1.5)
            turn = await self.last_turn()
        waits: dict[str, int] = self.__dict__.setdefault("web_wait", {})  # key -> looks it was not ready
        seen = set(seen)
        if not turn["key"]:
            return None
        done = self.sel["turn_done"]
        last_busy = turn["state"] not in (None, done)
        if not last_busy and self.page and await self.page.locator(self.sel["stop"]).first.is_visible():
            last_busy = True
        end = time.monotonic() + wait_busy_s
        while last_busy and time.monotonic() < end:
            await asyncio.sleep(2)
            turn = await self.last_turn()
            last_busy = (turn["state"] not in (None, done)
                         or await self.page.locator(self.sel["stop"]).first.is_visible())
        chat = conv_id(self.chat_url) or "?"
        turns = await self.page_turns()
        new, late, first = pick_web_turns(turns, seen, set(waits))
        if first:
            await asyncio.sleep(4)  # late turns of a chat never seen must not look new on the next look
            turns = await self.page_turns()
            keys = [t["key"] for t in turns]
            if last_busy and keys:
                keys.pop()  # being answered right now: that one is news once it is finished
            log.info("web mirror: chat %s has no turn seen before; %d turns marked as already seen, none posted",
                     chat, len(keys))
            return {"key": keys[-1], "items": [], "seen": keys} if keys else None
        if late:
            log.info("web mirror: %d older turns of chat %s appeared late; marked as seen, not posted", len(late), chat)
        record, items, key = list(late), [], None
        last_key = turns[-1]["key"] if turns else None
        for t in new:
            k, is_last = t["key"], t["key"] == last_key
            if (is_last and last_busy) or (not is_last and t["state"] not in (None, done)):
                break
            if re.match(r"^\[[^\]]{1,32}\]", (t["userText"] or "").strip()):
                waits.pop(k, None)
                record.append(k)  # a turn the bridge itself sent
                key = k
                continue
            text, outputs, whole = t["text"], [], True
            if is_last:
                text = turn["text"] or turn.get("fallback", "") or text
                whole = bool(turn.get("copy", 1))
                if text:
                    text = (await self.copied_reply()) or text
                outputs = await self.outputs_of_last_turn()
            n = waits.get(k, 0) + 1
            if not (text or "").strip() and not outputs:
                if is_last and n < WEB_BLANK_LOOKS:  # the reply may not be on the page yet
                    waits[k] = n
                    log.info("web mirror: turn %s in chat %s shows no reply yet (look %d of %d); waiting",
                             k, chat, n, WEB_BLANK_LOOKS)
                    break
                log.info("web mirror: turn %s in chat %s has no reply text or files; marked as seen, not posted", k, chat)
                waits.pop(k, None)
                record.append(k)
                key = k
                continue
            if not whole and n < WEB_PARTIAL_LOOKS:  # no Copy button: the reply may be unfinished
                waits[k] = n
                log.info("web mirror: turn %s in chat %s may be unfinished (look %d of %d); waiting",
                         k, chat, n, WEB_PARTIAL_LOOKS)
                break
            waits.pop(k, None)
            items.append({"key": k, "user": t["userText"], "text": text or "", "outputs": outputs})
            key = k
        for k in list(waits):  # forget waiting turns that left the page
            if all(t["key"] != k for t in turns):
                del waits[k]
        return {"key": key, "items": items, "seen": record} if items or record else None

    async def newest_project_chat(self) -> str | None:
        """URL of the project's most recently used chat, read from the sidebar of the open page
        (the sidebar lists chats newest first). None when the sidebar shows no project chat."""
        m = re.search(r"(g-p-[0-9a-f]+)", self.cfg.get("project_url", ""))
        if not m or not self.page:
            return None
        href = await self.page.evaluate(
            "(p) => { const a = document.querySelector('nav a[href*=\"' + p + '\"][href*=\"/c/\"]'); return a ? a.getAttribute('href') : null; }",
            m.group(1))
        return ("https://chatgpt.com" + href.split("?")[0]) if href and href.startswith("/") else None

    async def collect_outputs(self, dest: Path) -> list[Path]:
        """Save the last reply's files (its Download buttons) and images into dest."""
        import base64
        import hashlib
        import mimetypes

        out: list[Path] = []
        hashes: set[str] = set()

        def keep(path: Path) -> None:
            h = hashlib.sha1(path.read_bytes()).hexdigest()
            if h in hashes:
                path.unlink(missing_ok=True)
                return
            hashes.add(h)
            out.append(path)

        dest.mkdir(parents=True, exist_ok=True)
        count = await self.page.evaluate(TAG_DOWNLOADS_JS, self.sel)
        for i in range(min(count, MAX_UPLOAD_FILES)):
            try:
                async with self.page.expect_download(timeout=60_000) as info:
                    await self.page.locator(f'[data-bridge-dl="{i}"]').evaluate("b => b.click()")
                dl = await info.value
                path = dest / safe_name(dl.suggested_filename)
                await dl.save_as(path)
                keep(path)
            except Exception as e:
                log.warning("download button %d failed: %s", i, e)

        images = await self.page.evaluate(REPLY_IMAGES_JS, self.sel)
        for n, im in enumerate(images[:MAX_UPLOAD_FILES]):
            try:
                src = im["src"]
                if src.startswith(("blob:", "data:")):
                    mime, b64 = await self.page.evaluate(FETCH_B64_JS, src)
                    body = base64.b64decode(b64)
                else:
                    resp = await self.ctx.request.get(src)
                    if not resp.ok:
                        raise RuntimeError(f"HTTP {resp.status}")
                    body, mime = await resp.body(), resp.headers.get("content-type", "")
                ext = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".png"
                path = dest / f"gpt-image-{n + 1}{ext}"
                path.write_bytes(body)
                keep(path)
            except Exception as e:
                log.warning("reply image %d failed: %s", n, e)
        if out:
            log.info("reply files: %s", ", ".join(p.name for p in out))
        return out

    async def new_chat(self, first_message: str) -> str:
        """Start a conversation in the project; return its URL."""
        if not self.alive():
            await self.start()
        await self.open_chat(self.cfg["project_url"])
        await self._send(first_message)
        end = time.monotonic() + 30
        while time.monotonic() < end and "/c/" not in self.page.url:
            await asyncio.sleep(0.5)
        if "/c/" not in self.page.url:
            raise RuntimeError("new conversation URL did not appear")
        await self._wait_reply()
        url = self.page.url.split("?")[0]
        self.state["chat_url"] = url
        save_json(STATE_FILE, self.state)
        self.last_key = (await self.last_turn())["key"]
        self.last_used = time.monotonic()
        return url


# ---------------------------------------------------------------- Discord side

def make_bridge(cfg: dict, state: dict):
    import discord

    class Bridge(discord.Client):
        def __init__(self):
            intents = discord.Intents.default()
            intents.message_content = True
            super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False))
            self.cfg = cfg
            self.state = state
            self.channel_id = int(cfg["channel_id"])
            self.log_channel_id = int(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
            self.log_channel = None
            self.reply_target = None  # channel the current batch was addressed from
            self.claude_id = int(cfg["claude_bot_id"])
            self.owner_id = int(cfg["owner_id"])
            self.gpt = ChatGPT(cfg, state)
            self.buffer: list[list] = []  # [speaker, text, ts]
            self.trigger = asyncio.Event()
            self.last_msg_at = 0.0
            self.streak = 0
            self.capped = False
            self.last_claude_at = 0.0
            self.busy = False
            self.stopping = False
            self.channel = None
            self.page_lock = asyncio.Lock()
            self.failures = 0  # failed tries of the current batch
            self.page_fails = 0  # times in a row the ChatGPT page did not load for a batch
            self.caught_up = False
            self.halted = self.state.get("halted")  # {"since": ...} while the user has paused the group
            self.gpt_pending = False  # GPT was addressed while halted
            self.status_mtime = None
            self.follow_candidate = None  # a newer chat of the project, seen once
            self.waking = False  # a wake-up of Claude is in progress
            self.web_fail = ("", 0)  # (turn key, failed posts) of a web turn that could not be posted
            self.seen_ids: set[int] = set()  # Discord messages already in the buffer (live or from catch-up)
            self.wake_missed: list = []  # messages that mentioned Claude while it was starting
            self.web_active_until = 0.0  # until then the web mirror looks every mirror_fast_s
            self.notes = collections.deque(maxlen=50)  # the bridge's own notices: status lines, not conversation
            self.report_bodies = collections.deque(maxlen=20)  # session reports the bridge posted itself
            self.archive_dir = Path(os.path.expanduser(cfg["archive_dir"])) if cfg.get("archive_dir") else None
            self.archive_lock = asyncio.Lock()
            # Claude watcher
            w = self.cfg.get("claude_watch", {})
            self.cw_wake = bool(w.get("ensure_script"))  # waking a stopped Claude needs an explicit ensure_script
            c = self.cfg.get("claude", {})
            if c.get("workdir"):
                w.setdefault("project_dir", str(Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", c["workdir"])))
            w.setdefault("session_name", c.get("name", "Group Claude"))
            self.cw_wake = self.cw_wake or bool(w.get("wake_on_mention"))
            self.cw_wake_report = bool(w.get("wake_on_report"))  # a posted session report starts a stopped Claude
            w.setdefault("work_dir", str(RUNTIME))
            w.setdefault("lock_file", str(RUNTIME / "ensure.lock"))
            self.cw_dir = Path(os.path.expanduser(w["project_dir"])) if w.get("project_dir") else None
            self.cw_name = w.get("session_name", "")
            self.cw_ctx_limit = int(w.get("ctx_limit", 150000))
            self.cw_work = Path(os.path.expanduser(w["work_dir"]))
            # How to start this project's Claude session: the configured script, else scripts/ensure.ps1
            # (with this profile). Single-quoted for PowerShell.
            if w.get("ensure_script"):
                self.cw_ensure = "& '" + os.path.expanduser(w["ensure_script"]).replace("'", "''") + "'"
            else:
                self.cw_ensure = "& '" + str(ROOT / "scripts" / "ensure.ps1").replace("'", "''") + "'"
                if PROFILE is not None:
                    self.cw_ensure += " -Profile '" + str(PROFILE).replace("'", "''") + "'"
            self.cw_idle_s = float(w.get("idle_stop_min", 0)) * 60  # 0 = never stop an idle Claude
            self.cw_idle_checked = time.time()
            # 0 = off: no automatic progress checks while the board lists work in progress
            self.cw_progress_s = float(w.get("progress_check_min", 0)) * 60
            self.cw_progress_checked = self.cw_progress_sent = time.time()
            self.cw_boot_check_at = time.time() + 120  # when to look whether a started Claude came up; 0 = settled
            self.cw_stuck = False  # the running Claude is waiting at a startup question
            self.cw_stuck_noted = 0.0
            self.cw_last_session = self.cw_work / "claude-last-session.txt"  # written at an idle stop; the launcher resumes it
            self.cw_lock = os.path.expanduser(w.get("lock_file", ""))
            self.cw_file: Path | None = None
            self.cw_offset = 0
            self.cw_known: dict[str, bool] = {}
            self.cw_ctx = 0
            self.cw_nudged_at = 0.0
            self.cw_paused_until: float | None = None
            self.cw_limit_text = ""
            self.cw_login_noted = 0.0
            # Coding-agent CLIs as group members (optional, config "codex" and "gemini")
            self.members = []
            helpers = {"split_message": split_message, "fence_tables": fence_tables,
                       "outbox": OUTBOX_DIR, "root": ROOT if PROFILE is None else RUNTIME, "S": S}
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            for key, module, cls in (("codex", "codex_agent", "CodexAgent"), ("gemini", "gemini_agent", "GeminiAgent")):
                if cfg.get(key, {}).get("enabled"):
                    agent_cls = getattr(__import__(module), cls)
                    self.members.append(agent_cls(self, discord, cfg, state, lambda: save_json(STATE_FILE, self.state),
                                                  log, helpers))

        def member_of(self, msg):
            """The member whose bot wrote this message, or None."""
            return next((m for m in self.members if m.is_mine(msg)), None)

        # -- helpers
        @property
        def cap(self) -> int:
            return int(self.state.get("loop_cap", self.cfg["loop_cap"]))

        def humans(self) -> set[int]:
            access = load_json(Path(os.path.expanduser(self.cfg.get("access_file") or str(ACCESS_FILE))), {})
            group = access.get("groups", {}).get(str(self.channel_id), {})
            ids = {int(x) for x in group.get("allowFrom", []) if str(x).isdigit()}
            ids.discard(self.user.id if self.user else 0)
            ids.discard(self.claude_id)
            for m in self.members:
                ids.discard(m.bot_id)
            return ids or {self.owner_id}

        def in_channel(self, msg) -> bool:
            ch = msg.channel
            ids = {self.channel_id, self.log_channel_id} - {None}
            return ch.id in ids or getattr(ch, "parent_id", None) in ids

        def uncapped(self, channel) -> bool:
            """`log_channel_uncapped`: AI messages in the log channel do not count toward the loop cap."""
            return bool(self.cfg.get("log_channel_uncapped")) and channel is not None \
                and self.log_channel_id is not None \
                and self.log_channel_id in (getattr(channel, "id", None), getattr(channel, "parent_id", None))

        def is_log(self, msg) -> bool:
            ch = msg.channel
            return self.log_channel_id is not None and self.log_channel_id in (ch.id, getattr(ch, "parent_id", None))

        def tag(self, speaker: str, msg) -> str:
            """Speaker label for GPT; lines from the log channel say so."""
            return f"{speaker} · #{getattr(msg.channel, 'name', 'log')}" if self.is_log(msg) else speaker

        def target_for(self, msg) -> None:
            """GPT answers where it was asked; the main channel wins if both asked."""
            if self.reply_target is None or self.reply_target.id != self.channel_id:
                self.reply_target = msg.channel

        def speaker(self, msg) -> str:
            if msg.author.id == self.owner_id:
                return S("user_label")
            if msg.author.id == self.claude_id:
                return "Claude"
            return msg.author.display_name

        def clean(self, msg) -> str:
            names = {self.owner_id: S("user_label"), self.claude_id: S("claude_label")}
            if self.user:
                names[self.user.id] = "GPT"
            for u in msg.mentions:
                names.setdefault(u.id, u.display_name)
            text = re.sub(r"<@!?(\d+)>", lambda m: "@" + names.get(int(m.group(1)), "someone"), msg.content)
            text = re.sub(r"<@&(\d+)>", lambda m: "@" + (msg.guild.get_role(int(m.group(1))).name if msg.guild and msg.guild.get_role(int(m.group(1))) else "role"), text)
            text = re.sub(r"<#(\d+)>", "#channel", text)
            for a in msg.attachments:
                text += "\n" + S("attachment", name=a.filename)
            return text.strip()

        def mentions_claude(self, msg) -> bool:
            """@Claude as a user mention, as its role (Discord's picker offers the bot's
            role next to the bot itself, and the two look the same), or as plain text."""
            if any(u.id == self.claude_id for u in msg.mentions):
                return True
            for r in msg.role_mentions:
                if (getattr(getattr(r, "tags", None), "bot_id", None) == self.claude_id
                        or re.fullmatch(r"(bot)?claude", r.name.strip(), re.I)):
                    return True
            known = {int(x) for x in self.cfg.get("claude_role_ids", [])}
            if known.intersection(getattr(msg, "raw_role_mentions", [])):
                return True  # role cache can be empty without the Guilds intent
            return bool(re.search(r"@(bot)?claude\b", msg.content or "", re.I))

        async def addressed_to_gpt(self, msg) -> bool:
            me = self.user
            if me and any(u.id == me.id for u in msg.mentions):
                return True
            for r in msg.role_mentions:
                tags = getattr(r, "tags", None)
                if (tags and getattr(tags, "bot_id", None) == me.id) or r.name.lower() == "gpt":
                    return True
            if ADDRESS_RE.search(msg.content or ""):
                return True
            ref = msg.reference
            if ref and ref.message_id:
                target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
                if target is None:
                    try:
                        target = await msg.channel.fetch_message(ref.message_id)
                    except Exception:
                        target = None
                if target is not None and me and target.author.id == me.id:
                    return True
            return False

        def remember(self, speaker: str, text: str, attachments=(), msg_id: int = 0) -> bool:
            """Add a channel message to GPT's buffer; False if it is already there."""
            if msg_id:
                if msg_id in self.seen_ids:
                    return False
                if len(self.seen_ids) > 5000:
                    self.seen_ids.clear()
                self.seen_ids.add(msg_id)
            now = time.monotonic()
            atts = list(attachments)
            if self.buffer and self.buffer[-1][0] == speaker and now - self.buffer[-1][2] < 10:
                self.buffer[-1][1] += "\n" + text  # a long message split into chunks
                self.buffer[-1][2] = now
                self.buffer[-1][3].extend(atts)
                self.buffer[-1][4] = max(self.buffer[-1][4], msg_id)
            else:
                self.buffer.append([speaker, text, now, atts, msg_id])
            self.last_msg_at = now
            return True

        def take_buffer(self) -> tuple[str, list, int]:
            """Everything said since GPT's last turn, the attachments in it (newest kept), and the
            id of the newest Discord message in it."""
            entries = list(self.buffer)
            atts = [a for entry in entries for a in entry[3]]
            newest = max((entry[4] for entry in entries), default=0)
            self.buffer.clear()
            limit = self.cfg["context_max_chars"]
            out, total, cut = [], 0, False
            for s, t, _, _, _ in reversed(entries):
                line = t if s is None else f"[{s}] {t}"
                if s is None:  # a batch that already failed once holds the request itself: never cut it
                    out.append(line)
                    total += len(line)
                    continue
                if cut or (total + len(line) > limit and out):
                    if not cut:
                        out.append(S("omitted"))
                    cut = True
                    continue
                out.append(line)
                total += len(line)
            return "\n\n".join(reversed(out)), atts[-MAX_UPLOAD_FILES:], newest

        async def download(self, atts: list) -> tuple[list[Path], list[str]]:
            """Save Discord attachments for upload; return (paths, names that could not be fetched)."""
            INBOX_DIR.mkdir(parents=True, exist_ok=True)
            paths, failed = [], []
            for a in atts:
                path = INBOX_DIR / f"{a.id}-{safe_name(a.filename)}"
                try:
                    await a.save(path)
                    paths.append(path)
                except Exception as e:
                    log.warning("attachment %s failed: %s", a.filename, e)
                    failed.append(a.filename)
            return paths, failed

        async def note(self, text: str, log_channel: bool = False) -> None:
            try:
                ch = (self.log_channel or self.channel) if log_channel else self.channel
                if ch:
                    self.notes.append(text.strip())  # before sending: the gateway event can beat the HTTP reply
                    await ch.send(text, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            except Exception as e:
                log.warning("note failed: %s", e)

        def request_gpt(self) -> None:
            if self.halted:
                self.gpt_pending = True  # answered after !resume
                return
            if self.streak >= self.cap:
                if not self.capped:
                    self.capped = True
                    log.info("loop cap hit (streak=%d cap=%d)", self.streak, self.cap)
                    asyncio.create_task(self.note(S("cap_hit", n=self.streak)))
                return
            self.trigger.set()

        # -- events
        async def setup_hook(self) -> None:
            prune_dir(INBOX_DIR)
            prune_dir(OUTBOX_DIR)
            self.loop.create_task(self.worker())
            self.loop.create_task(self.stop_watch())
            self.loop.create_task(self.mirror_watch())
            self.loop.create_task(self.claude_watch())
            self.loop.create_task(self.reports_watch())
            self.loop.create_task(self.status_watch())
            self.loop.create_task(self.browser_idle_watch())
            for m in self.members:
                if m.enabled:
                    self.loop.create_task(m.start())

        async def on_ready(self) -> None:
            log.info("discord connected as %s (%s)", self.user, self.user.id)
            try:
                self.channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
                if self.log_channel_id:
                    self.log_channel = self.get_channel(self.log_channel_id) or await self.fetch_channel(self.log_channel_id)
            except Exception as e:
                log.error("cannot open channel %s: %s", self.channel_id, e)
            if not self.gpt.alive():
                try:
                    await self.gpt.start()
                except NeedsHuman as e:
                    log.warning("browser needs the user: %s", e)
                except PageNotLoaded as e:
                    log.warning("browser started, page not loaded: %s", e)
                except Exception:
                    log.exception("browser start failed")
            if self.archive_dir is not None:
                self.loop.create_task(self.archive_backfill())
            if not self.caught_up:
                self.caught_up = True
                await self.catch_up()

        # -- Archive: a local copy of the channels, <archive_dir>/<channel>/<YYYY-MM-DD>.md
        # (local time) with attachments in <channel>/files/. Append-only; edits are not tracked.
        async def archive(self, msg) -> None:
            async with self.archive_lock:
                try:
                    await self._archive_one(msg)
                except Exception:
                    log.exception("archive failed for message %s", msg.id)

        async def archive_backfill(self) -> None:
            """Everything since the last archived message of each channel (all of it the first time)."""
            async with self.archive_lock:
                for ch in (self.channel, self.log_channel):
                    if ch is None:
                        continue
                    n = 0
                    for attempt in range(5):  # Discord has short outages (503); a gap here would stay a gap
                        last = int(self.state.get("archive_last", {}).get(str(ch.id), 0))
                        try:
                            async for m in ch.history(limit=None, after=discord.Object(id=last) if last else None, oldest_first=True):
                                await self._archive_one(m)
                                n += 1
                            break
                        except Exception as e:
                            log.warning("archive backfill in #%s failed (try %d): %s", getattr(ch, "name", ch.id), attempt + 1, e)
                            await asyncio.sleep(15 * (attempt + 1))
                    if n:
                        log.info("archive backfill: %d messages from #%s", n, getattr(ch, "name", ch.id))

        async def _archive_one(self, msg) -> None:
            ch = msg.channel
            marks = self.state.setdefault("archive_last", {})
            if msg.id <= int(marks.get(str(ch.id), 0)):
                return  # already archived
            when = msg.created_at.astimezone()
            folder = self.archive_dir / safe_name(getattr(ch, "name", None) or str(ch.id))
            folder.mkdir(parents=True, exist_ok=True)
            day = folder / f"{when:%Y-%m-%d}.md"
            lines = []
            if not day.exists():
                lines.append(f"# #{getattr(ch, 'name', ch.id)} {when:%Y-%m-%d}\n")
            known = {self.claude_id, self.user.id} | {m.bot_id for m in self.members}
            who = msg.author.display_name + (" (bot)" if msg.author.bot and msg.author.id not in known else "")
            lines.append(f"---\n\U0001f552 **{when:%H:%M:%S} {who}**\n")  # not a heading: messages have their own
            body = msg.clean_content.strip()
            if not body and msg.is_system():
                body = f"*({msg.system_content})*" if msg.system_content else ""
            if not body and msg.embeds:
                e = msg.embeds[0]
                body = "\n".join(x for x in (e.title, e.description) if x)
            if body:
                lines.append(body)
            for att in msg.attachments:
                name = f"{msg.id}-{safe_name(att.filename)}"
                if att.size <= 50 * 1024 * 1024:
                    try:
                        (folder / "files").mkdir(exist_ok=True)
                        await att.save(folder / "files" / name)
                        lines.append(f"\U0001f4ce [{att.filename}](files/{name})")
                        continue
                    except Exception as e:
                        log.warning("archive: could not save %s: %s", att.filename, e)
                lines.append(f"\U0001f4ce {att.filename} ({att.size} bytes, not saved)")
            with day.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n\n")
            marks[str(ch.id)] = str(msg.id)
            save_json(STATE_FILE, self.state)

        async def catch_up(self) -> None:
            """After a restart, re-queue everything said since the last batch that reached GPT
            (older state files only know GPT's last reply), and feed Codex what it missed."""
            last_id = self.state.get("delivered_msg_id") or self.state.get("last_reply_msg_id")
            if not last_id or self.channel is None:
                return
            msgs = None
            for attempt in range(3):  # Discord has short outages (503) right after connecting
                try:
                    msgs = []
                    for ch in (self.channel, self.log_channel):
                        if ch is not None:
                            # the newest 300: if more piled up, the latest are the ones still waiting for an answer
                            msgs += [m async for m in ch.history(limit=300, after=discord.Object(id=int(last_id)), oldest_first=False)]
                    break
                except Exception as e:
                    msgs = None
                    log.warning("catch-up: reading the channels failed (try %d): %s", attempt + 1, e)
                    await asyncio.sleep(10 * (attempt + 1))
            if msgs is None:
                return
            try:
                n, pending, for_claude = 0, False, None
                msgs.sort(key=lambda m: m.id)
                seen = {m.key: int(self.state.get(m.key, {}).get("seen_msg_id") or 0) for m in self.members}
                commands = ("!gpt", "!halt", "!pause", "!resume", "!continue") + tuple("!" + m.key for m in self.members)
                for msg in msgs:
                    # A member gets what was said while the bridge was down, like a live message.
                    for m in self.members:
                        if m.enabled and seen[m.key] and msg.id > seen[m.key] and msg.author.id != self.user.id:
                            try:
                                await self.member_observe(m, msg)
                            except Exception:
                                log.exception("%s catch-up failed", m.key)
                    member = self.member_of(msg)
                    if msg.author.id == self.user.id or (msg.author.bot and msg.author.id != self.claude_id and not member):
                        continue
                    if not msg.author.bot and msg.author.id not in self.humans():
                        continue
                    if (msg.content or "").strip().lower().startswith(commands):
                        continue
                    who = member.name if member else "Claude" if msg.author.bot else self.speaker(msg)
                    if not self.remember(self.tag(who, msg), self.clean(msg), msg.attachments, msg.id):
                        continue  # arrived live while the history was being read
                    n += 1
                    if await self.addressed_to_gpt(msg):
                        pending = True
                        self.target_for(msg)
                    if (not msg.author.bot or member) and self.mentions_claude(msg):
                        for_claude = msg
                self.buffer.sort(key=lambda entry: entry[4])  # live messages may have been added before the old ones
                log.info("catch-up: %d messages since the last batch; addressed to GPT: %s", n, pending)
                if pending:
                    self.streak, self.capped = 0, False
                    self.request_gpt()  # not while the group is paused
                # Someone called Claude while the bridge was down and Claude is not running: wake it.
                # Only for a recent message; an old one was most likely answered before Claude stopped.
                if (for_claude is not None and self.cw_wake
                        and (discord.utils.utcnow() - for_claude.created_at).total_seconds() < 600):
                    self.loop.create_task(self.wake_claude(for_claude))
            except Exception:
                log.exception("catch-up failed")

        async def on_message(self, msg) -> None:
            if self.archive_dir is not None and self.in_channel(msg):
                self.loop.create_task(self.archive(msg))
            if (self.cw_wake and self.in_channel(msg)
                    and ((not msg.author.bot and msg.author.id in self.humans())
                         or self.member_of(msg) is not None)
                    and self.mentions_claude(msg)):
                self.loop.create_task(self.wake_claude(msg))
            if self.user is not None and self.in_channel(msg):
                for m in self.members:
                    if m.enabled:
                        try:
                            await self.member_observe(m, msg)
                        except Exception:
                            log.exception("%s observe failed", m.key)
            try:
                await self.handle(msg)
            except Exception:
                log.exception("on_message failed")

        async def member_observe(self, member, msg) -> None:
            """Feed a member the channel like GPT gets it; the owner, Claude, GPT and the other
            members may call it."""
            content = (msg.content or "").strip()
            if msg.author.id == self.user.id:
                if content.startswith("\u2699") or content in self.notes:
                    return  # bridge control messages and bridge notices
                if content.startswith(member.prefix):
                    return  # its own fallback posts
                other = next((m for m in self.members if content.startswith(m.prefix)), None)
                if content in self.report_bodies:  # a background session's report: context, never a call
                    await member.observe(msg, self.tag("Report", msg), self.clean(msg), may_trigger=False)
                    return
                speaker = other.name if other else "GPT"
            elif msg.author.id == self.claude_id:
                speaker = S("claude_label")
            elif msg.author.bot:
                other = self.member_of(msg)
                if other is None or other is member:
                    return
                speaker = other.name
            elif msg.author.id in self.humans():
                if content.startswith("!"):
                    return
                speaker = self.speaker(msg)
            else:
                return
            await member.observe(msg, self.tag(speaker, msg), self.clean(msg), may_trigger=True)

        async def handle(self, msg) -> None:
            if not self.in_channel(msg) or self.user is None or msg.author.id == self.user.id:
                return
            if msg.author.bot:
                member = self.member_of(msg)
                if msg.author.id != self.claude_id and member is None:
                    return
                who = member.name if member else "Claude"
                now = time.monotonic()
                if now - self.last_claude_at > 5 and not self.uncapped(msg.channel):  # chunks of one reply count once
                    self.streak += 1
                self.last_claude_at = now
                self.remember(self.tag(who, msg), self.clean(msg), msg.attachments, msg.id)
                if await self.addressed_to_gpt(msg):
                    log.info("%s -> gpt (streak=%d)%s", who.lower(), self.streak, " [log]" if self.is_log(msg) else "")
                    self.target_for(msg)
                    self.request_gpt()
                return
            if msg.author.id not in self.humans():
                return
            self.streak, self.capped = 0, False
            content = (msg.content or "").strip()
            low = content.lower()
            if low.startswith(("!halt", "!pause")):
                await self.halt()
                return
            if low.startswith(("!resume", "!continue")):
                await self.resume()
                return
            if content.lower().startswith("!gpt"):
                await self.command(content[4:].strip())
                return
            for m in self.members:
                if (low.split() or [""])[0] == "!" + m.key:
                    await self.note(await m.command(content[len(m.key) + 1:].strip()))
                    return
            self.remember(self.tag(self.speaker(msg), msg), self.clean(msg), msg.attachments, msg.id)
            if await self.addressed_to_gpt(msg):
                log.info("human -> gpt")
                self.target_for(msg)
                self.request_gpt()

        async def command(self, arg: str) -> None:
            parts = arg.split()
            cmd = parts[0].lower() if parts else "status"
            if cmd == "status":
                limit = int(self.cfg.get("rollover_turns", 0))
                await self.note(
                    S("status", browser=S("up") if self.gpt.alive() else S("down"), mode=self.gpt.mode,
                      activity=S("replying") if self.busy else S("idle"), streak=self.streak, cap=self.cap,
                      turns=self.state.get("turns_in_chat", 0), limit=f"/{limit}" if limit else "", url=self.gpt.chat_url)
                    + "\n" + (S("status_halted") if self.halted else S("status_running"))
                    + (S("status_claude_limit", when=time.strftime("%H:%M", time.localtime(self.cw_paused_until))) if self.cw_paused_until else "")
                    + (S("status_claude_ctx", k=self.cw_ctx // 1000) if self.cw_ctx else ""))
            elif cmd == "cap" and len(parts) > 1 and parts[1].isdigit():
                self.state["loop_cap"] = max(1, int(parts[1]))
                save_json(STATE_FILE, self.state)
                await self.note(S("cap_set", n=self.cap))
            elif cmd == "url" and len(parts) > 1 and parts[1].startswith("https://chatgpt.com/"):
                self.switch_chat(parts[1].strip("<>"))
                await self.note(S("url_set"))
            elif cmd == "new":
                fresh = len(parts) > 1 and parts[1].lower() in S("fresh_words").lower().split()
                try:
                    async with self.page_lock, self.channel.typing():
                        await self.rollover(handoff=not fresh, reason=S("reason_manual"))
                except NeedsHuman as e:
                    await self.note(S("needs_human_cmd", problem=e))
                except Exception as e:
                    log.exception("new chat failed")
                    await self.note(S("new_failed", err=e))
            elif cmd in ("show", "hide"):
                try:
                    async with self.page_lock:
                        await self.gpt.start("headed" if cmd == "show" else self.cfg["browser_mode"])
                    if cmd == "hide" and self.buffer:
                        self.trigger.set()
                    await self.note(S("shown") if cmd == "show" else S("hidden"))
                except NeedsHuman as e:
                    await self.note(S("shown_problem", problem=e) if cmd == "show" else str(e))
                except Exception as e:
                    await self.note(S("browser_failed", err=e))
            else:
                await self.note(S("help"))

        async def rollover(self, handoff: bool, reason: str) -> None:
            """New chat in the same project, seeded with the intro and (optionally) a handoff
            summary written by the old chat. Call with page_lock held."""
            await self.note(S("rollover_start", reason=reason, handoff=S("with_handoff") if handoff else ""), log_channel=True)
            summary = ""
            if handoff:
                try:
                    summary, _, _ = await self.gpt.ask(self.cfg["handoff_prompt"], collect=False)
                except ReplyFailed as e:
                    log.warning("handoff summary failed: %s", e)
            if not summary.strip() and self.state.get("recent_replies"):
                summary = S("recent_header") + "\n\n" + "\n\n---\n\n".join(self.state["recent_replies"])
            first = self.cfg["intro"]
            if self.cfg.get("intro_update"):  # a new chat needs the latest update too
                first += "\n\n" + self.cfg["intro_update"]
            if summary.strip():
                first += "\n\n" + S("handoff_header") + "\n" + summary.strip()
            url = await self.gpt.new_chat(first)
            self.state.update(intro_sent=True, intro_version=int(self.cfg.get("intro_version", 1)),
                              turns_in_chat=0, last_turn_key=self.gpt.last_key)
            save_json(STATE_FILE, self.state)
            self.mark_seen([self.gpt.last_key])
            await self.note(S("rollover_done", url=url), log_channel=True)

        async def browser_idle_watch(self) -> None:
            """Close the browser after browser_idle_close_s without use. A request or a web check
            opens it again, so an idle bridge loads nothing from ChatGPT."""
            idle_s = int(self.cfg.get("browser_idle_close_s", 180))
            if idle_s <= 0:
                return
            while not self.is_closed():
                await asyncio.sleep(30)
                if (not self.gpt.alive() or self.busy or self.page_lock.locked() or self.trigger.is_set()
                        or time.monotonic() - self.gpt.last_used < idle_s):
                    continue
                async with self.page_lock:
                    await self.gpt.close()
                log.info("browser closed after %d s idle; the next request or web check reopens it", idle_s)

        async def mirror_watch(self) -> None:
            """Post turns the user typed in ChatGPT itself (not through Discord) to the channel."""
            await self.wait_until_ready()
            interval = int(self.cfg.get("mirror_interval_s", 0))
            if interval <= 0:
                return
            fails = 0  # page-load failures in a row: back off so a throttled account is not hammered
            fast = int(self.cfg.get("mirror_fast_s", 0))  # the interval while the user is active in ChatGPT; 0 = no fast mode
            while not self.is_closed():
                step = fast if fast > 0 and not fails and time.time() < self.web_active_until else interval
                await asyncio.sleep(min(step * (2 ** min(fails, 4)), 3600))
                if self.busy or self.page_lock.locked() or self.trigger.is_set() or self.channel is None:
                    continue
                if time.time() < self.gpt.throttled_until:
                    continue  # rate limited: a check would only reload the page again
                look_at = time.monotonic()
                try:
                    async def look():
                        async with self.page_lock:
                            got = await self.gpt.check_for_web_turns(self.mirrored())
                            return got, await self.gpt.newest_project_chat()
                    try:
                        # never hold the page for long: a hung page call must not block messages to GPT
                        found, newest = await asyncio.wait_for(look(), 300)
                        # A look that drew a 429 counts as a failed one: the next look comes later.
                        fails = fails + 1 if self.gpt.last_429_at >= look_at else 0
                    except asyncio.TimeoutError:
                        log.warning("mirror check hung for 5 min; the browser will be restarted")
                        self.gpt.crashed = True
                        continue
                    if found:
                        await self.post_web_turns(found, "periodic")
                    # The user went on in another chat of the project: follow them there. Seen as the
                    # newest chat on two looks in a row, so one odd sidebar render does not move us.
                    if newest and conv_id(newest) != conv_id(self.gpt.chat_url):
                        if self.follow_candidate == conv_id(newest):
                            self.follow_candidate = None
                            await self.follow_chat(newest)
                        else:
                            self.follow_candidate = conv_id(newest)
                    else:
                        self.follow_candidate = None
                except (NeedsHuman, PageNotLoaded) as e:
                    fails += 1
                    log.warning("mirror check %s: %s (next look in %d s)",
                                "needs the user" if isinstance(e, NeedsHuman) else "found no page", e,
                                min(interval * (2 ** min(fails, 4)), 3600))
                except Exception:
                    if not self.stopping:  # a look cut short by the bridge stopping is not an error
                        log.exception("mirror check failed")

        def mirrored(self) -> set[str]:
            """Keys of the turns the bridge has seen before, in any chat: posted, sent by the bridge,
            or history found on a first look."""
            keys = set(self.state.get("mirrored_keys", []))
            keys.add(self.state.get("last_turn_key"))
            keys.update((self.state.get("chat_seen") or {}).values())
            return {k for k in keys if k}

        def mark_seen(self, keys) -> None:
            """Record turn keys as seen, so that they are never posted as news."""
            have = self.state.get("mirrored_keys", [])
            known = set(have)
            add = [k for k in dict.fromkeys(keys) if k and k not in known]
            if add:
                self.state["mirrored_keys"] = (have + add)[-MIRRORED_MAX:]
                save_json(STATE_FILE, self.state)

        async def post_web_turns(self, found: dict, look: str = "") -> None:
            """Post the turns the user typed in ChatGPT itself to the channel and remember what was seen."""
            chat = conv_id(self.gpt.chat_url) or "?"
            self.mark_seen(found.get("seen", []))
            posted = 0
            for item in found["items"]:
                if item["key"] in self.mirrored():
                    continue  # never post a turn twice, whatever the page showed this time
                if not (item["text"] or "").strip() and not item["outputs"]:
                    log.info("web mirror: turn %s in chat %s has nothing to post; marked as seen", item["key"], chat)
                    self.mark_seen([item["key"]])
                    continue  # never post an empty reply
                user = (item["user"] or "").strip()
                quote = "\n".join("> " + line for line in user[:800].splitlines() if line.strip())
                log.info("mirroring a turn typed in ChatGPT (%d chars, %d files; turn %s, chat %s, %s look)",
                         len(item["text"]), len(item["outputs"]), item["key"], chat, look or "unnamed")
                try:
                    await self.post_reply(f"{S('mirror_header')}\n{quote}\n\n{item['text']}", True, item["outputs"])
                except Exception:
                    # A turn is recorded only after it is posted, so the next look tries it again;
                    # a turn that fails three times is given up, or it would block the ones after it.
                    key, n = self.web_fail
                    self.web_fail = (item["key"], n + 1 if key == item["key"] else 1)
                    if self.web_fail[1] < 3:
                        raise
                    log.exception("a web turn could not be posted three times; skipping it")
                posted += 1
                self.state["turns_in_chat"] = int(self.state.get("turns_in_chat", 0)) + 1
                self.streak, self.capped = 0, False  # a human started this turn
                self.state["last_turn_key"] = item["key"]
                self.mark_seen([item["key"]])
                save_json(STATE_FILE, self.state)
            if found.get("key"):
                self.state["last_turn_key"] = found["key"]
                self.mark_seen([found["key"]])
                save_json(STATE_FILE, self.state)
            if posted:  # the user is working in ChatGPT right now: look more often for a while
                self.web_active_until = time.time() + float(self.cfg.get("mirror_fast_for_s", 600))

        async def follow_chat(self, url: str) -> None:
            """Make url the bridge's chat: channel messages go there, and its turns get mirrored."""
            self.switch_chat(url)
            log.info("following the user to chat %s", url)
            self.web_active_until = time.time() + float(self.cfg.get("mirror_fast_for_s", 600))
            await self.note(S("followed_chat", url=url), log_channel=True)

        def switch_chat(self, url: str) -> None:
            """Point the bridge at another chat, remembering how far the old one was mirrored."""
            if conv_id(url) == conv_id(self.gpt.chat_url):
                return
            seen = self.state.setdefault("chat_seen", {})
            seen[conv_id(self.gpt.chat_url)] = self.state.get("last_turn_key")
            while len(seen) > 30:
                seen.pop(next(iter(seen)))
            self.state["chat_url"] = url
            self.state["last_turn_key"] = seen.get(conv_id(url)) or ""  # "" = never looked at: its turns are history
            self.state["intro_sent"] = False  # GPT in that chat has not heard how the group works
            self.state["turns_in_chat"] = 0
            save_json(STATE_FILE, self.state)

        def requeue(self, batch: str, atts, newest: int = 0) -> None:
            """Put a batch that did not reach GPT back at the front of the buffer."""
            self.buffer.insert(0, [None, batch, time.monotonic(), list(atts), newest])

        def page_retry_delay(self, fails: int) -> float:
            """Seconds before a batch is sent again after the page did not load: past the rate
            limit window when there is one, and longer with every failure in a row."""
            throttled = max(self.gpt.throttled_until - time.time(), 0)
            return throttled + min(60 * 2 ** min(fails - 1, 4), 900)

        async def retry_later(self, delay: float) -> None:
            await asyncio.sleep(delay)
            if self.buffer:
                if self.halted:
                    self.gpt_pending = True  # sent after !resume
                else:
                    self.trigger.set()

        async def control(self, tag: str, text: str) -> None:
            """A bridge control message to Claude: posted by the GPT bot, starts with the gear emoji."""
            try:
                await (self.log_channel or self.channel).send(f"\u2699\ufe0f {tag} <@{self.claude_id}> {text}",
                                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=self.claude_id)]),
                                        suppress_embeds=True)
            except Exception as e:
                log.warning("control message failed: %s", e)

        async def halt(self) -> None:
            if self.halted:
                await self.note(S("halt_already"))
                return
            self.halted = {"since": time.time()}
            self.state["halted"] = self.halted
            save_json(STATE_FILE, self.state)
            log.info("halted by the user")
            await self.control(S("tag_pause"), S("halt_claude"))
            await self.note(S("halt_done"))

        async def resume(self) -> None:
            if not self.halted:
                await self.note(S("resume_not_halted"))
                return
            self.halted = None
            self.state.pop("halted", None)
            save_json(STATE_FILE, self.state)
            log.info("resumed by the user")
            await self.control(S("tag_resume"), S("resume_claude"))
            await self.note(S("resume_done"))
            for m in self.members:
                m.resume()
            if self.gpt_pending:
                self.gpt_pending = False
                self.streak, self.capped = 0, False
                self.trigger.set()

        # -- Claude watcher: reads the Claude session's transcript (no tokens spent)
        def _cw_find(self) -> Path | None:
            if not self.cw_dir or not self.cw_dir.is_dir():
                return None
            # the title may carry a prefix (claude.name_format, e.g. a start date) before the session name
            marker = re.compile(r'"customTitle":"[^"]*' + re.escape(json.dumps(self.cw_name, ensure_ascii=False)[1:-1]) + '"')
            for f in sorted(self.cw_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:12]:
                key = str(f)
                if key not in self.cw_known:
                    try:
                        self.cw_known[key] = bool(marker.search(f.read_text(encoding="utf-8", errors="replace")))
                    except Exception:
                        self.cw_known[key] = False
                if self.cw_known[key]:
                    return f
            return None

        def _cw_read(self) -> list[dict]:
            f = self._cw_find()
            if f is None:
                return []
            if f != self.cw_file:  # a new session: start at its end, the past is not news
                self.cw_file, self.cw_offset, self.cw_ctx = f, f.stat().st_size, 0
                log.info("watching Claude transcript %s", f.name)
                return []
            size = f.stat().st_size
            if size <= self.cw_offset:
                return []
            with open(f, "rb") as fh:
                fh.seek(self.cw_offset)
                chunk = fh.read(size - self.cw_offset)
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                return []
            self.cw_offset += last_nl + 1
            out = []
            for line in chunk[:last_nl].splitlines():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
            return out

        async def claude_watch(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await asyncio.sleep(30)
                try:
                    await self.check_claude()
                except Exception:
                    log.exception("claude watch failed")

        async def check_claude(self) -> None:
            if self.channel is None:
                return
            req = self.cw_work / "restart.request"
            if req.exists():
                try:  # "resume" in the file: same conversation (after a model or effort switch)
                    resume = req.read_text(encoding="utf-8-sig").strip().lower() == "resume"
                except OSError:
                    resume = False
                req.unlink(missing_ok=True)
                await self.restart_claude(resume=resume)
                return
            for e in self._cw_read():
                if e.get("type") != "assistant":
                    continue
                msg = e.get("message") or {}
                usage = msg.get("usage") or {}
                ctx = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if ctx:
                    self.cw_ctx = ctx
                text = " ".join(b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict))
                if e.get("isApiErrorMessage") and LIMIT_RE.search(text) and self.cw_paused_until is None:
                    self.cw_paused_until = parse_reset(text) or time.time() + 3600
                    self.cw_limit_text = text.strip()[:200]
                    when = time.strftime("%H:%M", time.localtime(self.cw_paused_until))
                    log.warning("Claude hit its usage limit: %s", self.cw_limit_text)
                    await self.note(S("claude_limit", text=self.cw_limit_text, when=when))
                if ((e.get("isApiErrorMessage") or msg.get("model") == "<synthetic>") and LOGIN_RE.search(text)
                        and time.time() - self.cw_login_noted > 3600):  # once an hour, not per message
                    self.cw_login_noted = time.time()
                    log.warning("Claude cannot answer: %s", text.strip()[:200])
                    await self.note(S("claude_login_expired"))
            now = time.time()
            if self.cw_boot_check_at and now >= self.cw_boot_check_at and not self.waking:
                await self.check_claude_startup()
            if self.cw_idle_s and now - self.cw_idle_checked >= 300:
                self.cw_idle_checked = now
                await self.stop_idle_claude()
            if self.cw_progress_s and now - self.cw_progress_checked >= 60:
                self.cw_progress_checked = now
                await self.progress_check(now)
            if self.cw_paused_until and now >= self.cw_paused_until + 60:
                self.cw_paused_until = None
                await self.control(S("tag_limit_over"), S("limit_over_claude"))
            if (self.cw_ctx > self.cw_ctx_limit and now - self.cw_nudged_at > 7200
                    and not self.halted and not self.cw_paused_until):
                self.cw_nudged_at = now
                handoff, restart = self.cw_work / "handoff.md", self.cw_work / "restart.request"
                await self.control(S("tag_context"), S("ctx_claude", k=self.cw_ctx // 1000, handoff=handoff, restart=restart))

        def in_progress(self) -> list[str]:
            """The items under the status board's in-progress heading (claude_watch.progress_heading)."""
            if not STATUS_FILE.exists():
                return []
            w = self.cfg.get("claude_watch", {})
            heading = w.get("progress_heading", "In progress").strip().lower()
            empty = {x.lower() for x in w.get("progress_empty", ["none", "nothing", "-"])}  # an item that means "nothing"
            items, inside = [], False
            for line in STATUS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                bare = line.strip().strip("*#: ").strip().lower()
                if line.strip().startswith(("**", "#")):
                    inside = bare == heading
                    continue
                item = line.strip().lstrip("-*• ").strip()
                if inside and item and item.lower() not in empty:
                    items.append(item)
            return items

        async def progress_check(self, now: float) -> None:
            """While the status board lists work in progress and Claude has been quiet for
            claude_watch.progress_check_min, ask it to check on that work and carry on, so the
            user does not have to. Nothing is sent while the board shows nothing in progress.
            A Claude that is not running (crashed, PC restarted) is started for it, so pending work
            never waits for someone to call Claude."""
            if (self.halted or self.waking or self.cw_paused_until or now - self.cw_progress_sent < self.cw_progress_s
                    or (self.cw_work / "restart.request").exists()):
                return
            f = self._cw_find()
            if f is not None and now - f.stat().st_mtime < self.cw_progress_s:
                return  # Claude was active recently
            items = self.in_progress()
            if not items:
                return
            if not await self.claude_running():
                if not self.cw_wake:  # waking is configured (ensure_script or wake_on_mention)
                    return
                self.cw_progress_sent = now
                log.info("progress check: Claude is not running with %d items in progress; waking it", len(items))
                if not await self.wake_claude(None, S("progress_claude", min=int(self.cw_progress_s // 60))):
                    self.cw_progress_sent = now + 3 * self.cw_progress_s  # it did not come up: try again later, not every round
                return
            if f is None:
                return  # running, but no transcript yet: it has only just started
            self.cw_progress_sent = now
            log.info("progress check: Claude quiet for %d min with %d items in progress",
                     (now - f.stat().st_mtime) // 60, len(items))
            await self.control(S("tag_progress"), S("progress_claude", min=int(self.cw_progress_s // 60)))

        async def claude_running(self) -> bool:
            name = (self.cw_name or "").replace("'", "''")  # only this project's session counts
            ps = ("@(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                  "Where-Object { $_.CommandLine -match '--channels' -and $_.CommandLine -like '*" + name + "*' }).Count")
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-Command", ps, stdout=asyncio.subprocess.PIPE,
                creationflags=0x08000000)  # CREATE_NO_WINDOW
            out, _ = await proc.communicate()
            try:
                return int(out.decode().strip() or 0) > 0
            except ValueError:
                return True  # unsure: do not start a second one

        async def check_claude_startup(self) -> None:
            """A started Claude whose Discord plugin never loaded is waiting at a startup question in
            its hidden console (a trust or API-key prompt, often new after a CLI update) and receives
            nothing. Its sign: the process is two minutes old and has no child process. Say so in
            the channel, with what its screen shows."""
            name = (self.cw_name or "").replace("'", "''")
            ps = ("$all = Get-CimInstance Win32_Process; "
                  "$c = @($all | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match '--channels' "
                  "-and $_.CommandLine -like '*" + name + "*' } | Sort-Object CreationDate); "
                  "if (-not $c) { 'none'; exit }; $p = $c[-1]; "
                  "'{0} {1} {2}' -f $p.ProcessId, [int]((Get-Date) - $p.CreationDate).TotalSeconds, "
                  "@($all | Where-Object { $_.ParentProcessId -eq $p.ProcessId }).Count")
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-Command", ps, stdout=asyncio.subprocess.PIPE,
                creationflags=0x08000000)
            out, _ = await proc.communicate()
            fields = out.decode(errors="replace").split()
            if len(fields) != 3 or not all(x.isdigit() for x in fields):  # not running, or unsure
                self.cw_boot_check_at, self.cw_stuck = 0, False
                return
            pid, age, kids = fields[0], int(fields[1]), int(fields[2])
            if kids:  # it came up
                self.cw_boot_check_at, self.cw_stuck = 0, False
                return
            if age < 110:
                return  # still starting: look again at the next round
            self.cw_boot_check_at, self.cw_stuck, self.cw_stuck_noted = 0, True, time.time()
            screen = await self.read_console(pid)
            log.warning("Claude (pid %s) started %d s ago but never came up; its screen: %s", pid, age,
                        screen.replace("\n", " | ")[:400] or "(unreadable)")
            await self.note(S("claude_stuck_startup", screen=screen) if screen else S("claude_stuck_unknown"))

        async def read_console(self, pid: str) -> str:
            """The last lines on the hidden console of process `pid`, or "" when it cannot be read."""
            out_file = self.cw_work / "logs" / "claude-console.txt"
            try:
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.unlink(missing_ok=True)
                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "read-console.ps1"), "-TargetPid", pid, "-OutFile", str(out_file),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                await asyncio.wait_for(proc.wait(), 40)
                text = out_file.read_text(encoding="utf-8", errors="replace")
            except (OSError, asyncio.TimeoutError):
                return ""
            lines = [ln.strip() for ln in text.splitlines()]
            lines = [ln for ln in lines if ln and not re.fullmatch(r"[─━═\-_\s]+", ln)]  # no rules, no blanks
            return "\n".join(lines[-12:]).replace("```", "'''")[-900:]

        async def stop_idle_claude(self) -> None:
            """Stop this project's Claude session after claude_watch.idle_stop_min minutes without
            activity (saves memory, like the idle browser). The session id is kept, so the next
            @Claude resumes the same conversation. A shell or script still running under Claude
            counts as work in progress, and so does an item under the status board's in-progress
            heading: the progress checks keep such work moving, a stopped Claude would not."""
            if self.waking or self.halted or (self.cw_work / "restart.request").exists():
                return
            if self.cw_progress_s and self.in_progress():
                return
            name = (self.cw_name or "").replace("'", "''")
            ps = ("$all = Get-CimInstance Win32_Process; "
                  "$c = @($all | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match '--channels' "
                  "-and $_.CommandLine -like '*" + name + "*' }); "
                  "if (-not $c) { 'none'; exit }; "
                  "$started = ($c | ForEach-Object { $_.CreationDate } | Sort-Object | Select-Object -First 1); "
                  "$ids = @($c | ForEach-Object { $_.ProcessId }); $busy = 0; "
                  "for ($d = 0; $d -lt 4 -and $ids; $d++) { "
                  "$kids = @($all | Where-Object { $ids -contains $_.ParentProcessId }); "
                  "if ($kids | Where-Object { $_.Name -match '^(bash|sh|cmd|powershell|pwsh|python|pythonw|py)\\.exe$' "
                  "-and $_.CommandLine -notmatch 'mcp|plugin' }) { $busy = 1; break }; "
                  "$ids = @($kids | ForEach-Object { $_.ProcessId }) }; "
                  "'{0} {1} {2}' -f ([DateTimeOffset]$started).ToUnixTimeSeconds(), $busy, "
                  "(($c | ForEach-Object { $_.ProcessId }) -join ',')")
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-Command", ps, stdout=asyncio.subprocess.PIPE,
                creationflags=0x08000000)
            out, _ = await proc.communicate()
            fields = out.decode(errors="replace").split()
            if len(fields) != 3 or not fields[0].isdigit():
                return  # not running, or unsure
            started, busy, pids = float(fields[0]), fields[1] == "1", [x for x in fields[2].split(",") if x.isdigit()]
            last = started
            f = self._cw_find()
            if f is not None and f.stat().st_mtime > started:  # a fresh session has no transcript until its first message
                last = f.stat().st_mtime
            else:
                f = None
            if busy or time.time() - last < self.cw_idle_s:
                return
            if f is not None:
                self.cw_last_session.write_text(f.stem, encoding="utf-8")
            for pid in pids:
                kill = await asyncio.create_subprocess_exec(
                    "taskkill.exe", "/PID", pid, "/T", "/F", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                await kill.wait()
            log.info("Claude was idle for %d min; stopped it (session %s kept for resume)",
                     (time.time() - last) // 60, f.stem if f is not None else "none")

        async def wake_claude(self, msg, text: str | None = None) -> bool:
            """Someone @mentioned Claude but its session is not running (PC restarted, crash):
            start it with the ensure script, then hand it the handoff and the message it missed.
            msg None: woken by the progress check for the work on the status board; `text` says what
            to do. Only active when config claude_watch.ensure_script is set. True when it came up."""
            if self.waking:  # already starting: hand this message over too, once Claude is up
                if msg is not None and msg.id not in [m.id for m, _ in self.wake_missed]:
                    self.wake_missed.append((msg, text))
                return False
            if not self.cw_ensure:
                return False
            if await self.claude_running():
                # still waiting at its startup question: tell the person who called it, at most every 5 minutes
                if self.cw_stuck and msg is not None and not msg.author.bot and time.time() - self.cw_stuck_noted > 300:
                    self.cw_boot_check_at = time.time()
                return False
            if self.cw_paused_until and time.time() < self.cw_paused_until:
                when = time.strftime("%H:%M", time.localtime(self.cw_paused_until))
                if msg is not None:
                    await self.note(S("claude_asleep_limit", when=when))
                return False
            self.waking = True
            try:
                log.info("Claude is not running; waking it for %s", f"message {msg.id}" if msg is not None else "the work in progress")
                if msg is not None and not msg.author.bot:  # only a person who called Claude is told; Discord's status dot shows the rest
                    await self.note(S("claude_waking"))
                ps = ""
                if self.cw_lock:
                    ps += f"Remove-Item '{self.cw_lock}' -ErrorAction SilentlyContinue; "
                ps += self.cw_ensure
                resumed = False  # stopped while idle: the launcher continues that session
                try:
                    last = self.cw_last_session.read_text(encoding="utf-8").strip()
                    resumed = bool(last and self.cw_dir and (self.cw_dir / f"{last}.jsonl").exists())
                except OSError:
                    pass
                proc = await asyncio.create_subprocess_exec("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                                            "-Command", ps, creationflags=0x08000000)
                await proc.wait()
                self.cw_boot_check_at = time.time() + 120
                await asyncio.sleep(50)  # the new session needs a moment to connect to Discord
                if not await self.claude_running():
                    await self.note(S("claude_wake_failed"))
                    return False
                try:  # a logged-out CLI stays open at its prompt and never answers
                    run_log = self.cw_work / "logs" / "claude-run.log"
                    tail = run_log.read_bytes()[-20000:].decode("utf-8", "replace") if run_log.exists() else ""
                    tail = tail.rsplit("transcript start", 1)[-1]  # this launch only; the log is appended to
                except OSError:
                    tail = ""
                if re.search(r"not logged in|run /login", tail, re.I):
                    log.warning("Claude started but is not logged in")
                    await self.note(S("claude_not_logged_in"))
                    return False
                self.cw_file, self.cw_ctx, self.cw_nudged_at = None, 0, time.time()
                handoff = self.cw_work / "handoff.md"
                if msg is None:
                    await self.control(S("tag_progress"), S("wake_progress_resumed" if resumed else "wake_progress",
                                                            handoff=handoff, text=text or ""))
                else:
                    where = f"#{getattr(self.log_channel, 'name', 'log')}" if self.is_log(msg) else f"#{getattr(self.channel, 'name', 'channel')}"
                    await self.control(S("tag_handoff"), S("wake_claude_resumed" if resumed else "wake_claude",
                                                           handoff=handoff, where=where, text=(text or self.clean(msg))[:600]))
                await self.flush_missed()
                return True
            except Exception:
                log.exception("waking Claude failed")
                return False
            finally:
                self.waking = False
                self.wake_missed = []

        async def flush_missed(self) -> None:
            """Messages that mentioned Claude while it was starting did not reach it: pass them on."""
            missed, self.wake_missed = self.wake_missed, []
            if missed:
                lines = [f"[{self.speaker(m)}] {(t or self.clean(m))[:300]}" for m, t in missed[-5:]]
                await self.control(S("tag_handoff"), S("missed_claude", text="\n".join(lines)))

        async def restart_claude(self, resume: bool = False) -> None:
            """Claude restart on request: stop the old session and start a fresh one that reads the
            handoff, or (resume) the same conversation again, e.g. on another model or effort."""
            self.waking = True  # a mention meanwhile must not start a second session; it is handed over after
            try:
                await self._restart_claude(resume)
                await self.flush_missed()
            finally:
                self.waking = False
                self.wake_missed = []

        def claude_setting(self) -> str:
            """Model and effort Claude starts with: <runtime>/claude-model.json, else the profile."""
            c = self.cfg.get("claude", {})
            over = load_json(self.cw_work / "claude-model.json", {})
            return f"model {over.get('model') or c.get('model') or 'sonnet'}, effort {over.get('effort') or c.get('effort') or 'default'}"

        async def _restart_claude(self, resume: bool = False) -> None:
            handoff = self.cw_work / "handoff.md"
            session = self.cw_file.stem if resume and self.cw_file else None
            await self.note(S("restart_resume", setting=self.claude_setting()) if session else S("restart_start"), log_channel=True)
            name = self.cw_name.replace("'", "''")
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match '--channels' "
                  f"-and $_.CommandLine -match [regex]::Escape('{name}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}; "
                  "Start-Sleep -Seconds 3; ")
            if PROFILE is not None:  # the old session's launcher console too: ensure.ps1 counts it as a running Claude
                prof = str(PROFILE).replace("'", "''")
                ps += ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(powershell|pwsh)[.]exe$' "
                       "-and $_.CommandLine -match 'launch-claude[.]ps1' "
                       f"-and $_.CommandLine.IndexOf('{prof}', [StringComparison]::OrdinalIgnoreCase) -ge 0 }} | "
                       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; ")
            if self.cw_lock:
                ps += f"Remove-Item '{self.cw_lock}' -ErrorAction SilentlyContinue; "
            if session:  # the launcher resumes this conversation once
                self.cw_last_session.write_text(session, encoding="utf-8")
            else:
                self.cw_last_session.unlink(missing_ok=True)  # a fresh session, not a resumed one
            ps += self.cw_ensure
            proc = await asyncio.create_subprocess_exec("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps)
            await proc.wait()
            self.cw_file, self.cw_ctx, self.cw_nudged_at = None, 0, time.time()
            self.cw_boot_check_at = time.time() + 120
            await asyncio.sleep(45)  # the new session needs a moment to connect to Discord
            if session:
                await self.control(S("tag_switched"), S("switched_claude", setting=self.claude_setting()))
                log.info("Claude restarted in conversation %s (%s)", session, self.claude_setting())
            else:
                await self.control(S("tag_handoff"), S("handoff_claude", handoff=handoff))
                log.info("Claude restarted as a fresh session")

        async def worker(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await self.trigger.wait()
                while time.monotonic() - self.last_msg_at < self.cfg["quiet_period_s"]:
                    await asyncio.sleep(0.5)  # wait for the rest of a multi-chunk message
                self.trigger.clear()
                if self.halted:  # paused by the user: keep the buffer, send after !resume
                    self.gpt_pending = bool(self.buffer)
                    continue
                batch, atts, newest = self.take_buffer()
                if not batch:
                    continue
                prompt = batch
                target, self.reply_target = self.reply_target or self.channel, None
                self.busy = True
                wait = self.gpt.throttled_until - time.time()
                if wait > 0:  # rate limited: hold the batch instead of reloading into more 429s
                    log.info("ChatGPT is rate limiting; sending in %d s", wait)
                    await asyncio.sleep(wait)
                try:
                    if self.channel is None:
                        self.channel = await self.fetch_channel(self.channel_id)
                    async with self.page_lock, self.channel.typing():
                        if int(self.cfg.get("mirror_interval_s", 0)) > 0:
                            # Turns typed in ChatGPT since the last look come first: after this batch the
                            # bridge's position moves past them, and they would never reach the channel.
                            try:
                                busy_s = min(float(self.cfg["reply_timeout_s"]), 600)
                                found = await asyncio.wait_for(
                                    self.gpt.check_for_web_turns(self.mirrored(), fresh_s=10, wait_busy_s=busy_s),
                                    busy_s + 180)
                                if found:
                                    await self.post_web_turns(found, "before sending")
                            except (NeedsHuman, PageNotLoaded):
                                raise
                            except Exception:
                                log.exception("looking for web turns before sending failed")
                        limit = int(self.cfg.get("rollover_turns", 0))
                        if limit and int(self.state.get("turns_in_chat", 0)) >= limit:
                            await self.rollover(handoff=True, reason=S("reason_auto", n=limit))
                        version = int(self.cfg.get("intro_version", 1))
                        if not self.state.get("intro_sent"):
                            prompt = self.cfg["intro"] + "\n\n" + prompt
                        elif int(self.state.get("intro_version", 1)) < version and self.cfg.get("intro_update"):
                            prompt = self.cfg["intro_update"] + "\n\n" + prompt  # tell an old chat what changed
                        files, failed = await self.download(atts)
                        if failed:
                            prompt += "\n\n" + S("attach_failed", names=", ".join(failed))
                        try:  # _wait_reply gives up after reply_timeout_s; this catches a hung page call
                            text, finished, outputs = await asyncio.wait_for(
                                self.gpt.ask(prompt, files), self.cfg["reply_timeout_s"] + 300)
                        except asyncio.TimeoutError:
                            self.gpt.crashed = True  # the next try starts a new browser
                            raise RuntimeError("ChatGPT page stopped responding")
                    self.failures = self.page_fails = 0
                    self.state.update(intro_sent=True, intro_version=version, last_turn_key=self.gpt.last_key,
                                      turns_in_chat=int(self.state.get("turns_in_chat", 0)) + 1)
                    if newest:  # a restart re-reads the channel from here
                        self.state["delivered_msg_id"] = str(max(newest, int(self.state.get("delivered_msg_id") or 0)))
                    save_json(STATE_FILE, self.state)
                    self.mark_seen([self.gpt.last_key])  # the bridge's own turn is never news
                    # GPT has answered: from here a failure must not send the batch to GPT again.
                    for attempt in range(3):
                        try:
                            await self.post_reply(text, finished, outputs, target)
                            break
                        except Exception:
                            log.exception("posting GPT's reply failed (try %d)", attempt + 1)
                            await asyncio.sleep(5 * (attempt + 1))
                except NeedsHuman as e:
                    log.warning("needs human: %s", e)
                    self.requeue(batch, atts, newest)
                    self.reply_target = self.reply_target or target
                    await self.note(S("needs_human_ask", problem=e))
                except PageNotLoaded as e:
                    # Rate limited or half loaded: keep the batch and send it again without the user.
                    self.requeue(batch, atts, newest)
                    self.reply_target = self.reply_target or target
                    self.page_fails += 1
                    delay = self.page_retry_delay(self.page_fails)
                    log.warning("page did not load (try %d, 429 %s); sending again in %d s", self.page_fails,
                                "seen" if self.gpt.throttled_until > time.time() else "not seen", delay)
                    if self.page_fails == PAGE_FAILS_NOTE:  # say it once, so a long wait is not silent
                        await self.note(S("page_retrying", problem=e))
                    elif self.page_fails == PAGE_FAILS_ASK:
                        await self.note(S("needs_human_ask", problem=e))
                    asyncio.create_task(self.retry_later(delay))
                except Exception as e:
                    # Never drop the batch: put it back, then retry (or roll over to a new chat).
                    self.requeue(batch, atts, newest)
                    self.reply_target = self.reply_target or target
                    self.failures += 1
                    tries = self.failures
                    if isinstance(e, ReplyFailed):
                        log.warning("reply failed (try %d): %s", tries, e)
                    else:
                        log.exception("ask failed (try %d)", tries)
                    try:
                        if isinstance(e, ReplyFailed) and e.too_long and tries <= 2:
                            async with self.page_lock:
                                await self.rollover(handoff=False, reason=S("reason_too_long"))
                            self.trigger.set()
                        elif tries < 3:
                            try:
                                async with self.page_lock:
                                    if isinstance(e, ReplyFailed) and self.gpt.alive():
                                        await self.gpt.open_chat()  # reload: shows the chat as the server has it
                                    else:
                                        await self.gpt.start()
                            except PageNotLoaded as e2:  # the retry loads it again
                                log.warning("reload for the retry failed: %s", e2)
                            asyncio.create_task(self.retry_later(30 * tries))
                        else:
                            self.failures = 0
                            await self.note(S("retry_gave_up", n=tries, reason=e))
                    except Exception:
                        log.exception("recovery failed")
                finally:
                    self.busy = False

        def file_limit(self) -> int:
            """Upload limit in bytes: config max_file_mb, else what the server allows."""
            mb = self.cfg.get("max_file_mb")
            if mb:
                return int(float(mb) * 1048576)
            guild = getattr(self.channel, "guild", None)
            return guild.filesize_limit if guild else MAX_DISCORD_FILE

        def too_big_text(self, paths) -> str:
            return S("too_big", limit=f"{self.file_limit() / 1048576:.0f}", files=", ".join(
                f"{p.name} {p.stat().st_size / 1048576:.1f} MB" for p in paths))

        async def post_reply(self, text: str, finished: bool, outputs: list[Path] = (), channel=None) -> None:
            channel = channel or self.channel
            text = re.sub(r"@(?:bot)?claude\b", f"<@{self.claude_id}>", text, flags=re.I)
            for m in self.members:
                if m.enabled:
                    text = m.mention_re.sub(f"<@{m.bot_id}>", text)
            text = fence_tables(text)
            text = text.replace("\u2699\ufe0f", "").replace("\u2699", "")  # only the bridge writes control messages
            text = re.sub(r"@(everyone|here)\b", "@​\\1", text)
            limit = self.file_limit()
            files = [p for p in outputs if p.stat().st_size <= limit]
            too_big = [p for p in outputs if p.stat().st_size > limit]
            if too_big:
                text += "\n\n" + self.too_big_text(too_big)
            if not text.strip() and not files:
                await self.note(S("empty"))
                return
            if not finished:
                text += "\n\n" + S("timeout")
            parts = split_message(text) or [""]
            if len(parts) > 1:  # numbered, so a reader who got only some parts knows to fetch the rest
                parts = [f"{p}\n({i + 1}/{len(parts)})" for i, p in enumerate(parts)]
            first_msg = None
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if last and files:
                    for j in range(0, len(files), 10):  # Discord takes at most 10 files per message
                        chunk = files[j:j + 10]
                        head = part if j == 0 else None
                        try:
                            sent = await channel.send(head, files=[discord.File(str(p), filename=p.name) for p in chunk],
                                                           suppress_embeds=True)
                            first_msg = first_msg or sent
                        except discord.HTTPException as e:
                            if e.status != 413:
                                raise
                            log.warning("upload rejected as too large: %s", [p.name for p in chunk])
                            sent = await channel.send(((head + "\n\n") if head else "") + self.too_big_text(chunk),
                                                      suppress_embeds=True)
                            first_msg = first_msg or sent
                else:
                    sent = await channel.send(part, suppress_embeds=True)
                    first_msg = first_msg or sent
            if first_msg is not None:
                self.state["last_reply_msg_id"] = str(first_msg.id)
                self.state["recent_replies"] = (self.state.get("recent_replies", []) + [text[:1500]])[-3:]
                save_json(STATE_FILE, self.state)
                if self.cw_wake and f"<@{self.claude_id}>" in text:
                    self.loop.create_task(self.wake_claude(first_msg, text))  # GPT called a Claude that may be stopped
            if not self.uncapped(channel):
                self.streak += 1
            log.info("gpt posted %d chars, %d files (streak=%d)", len(text), len(files), self.streak)

        # -- Session reports: another Claude Code session writes data/reports/<name>.md when done
        async def reports_watch(self) -> None:
            await self.wait_until_ready()
            (REPORTS_DIR / "sent").mkdir(parents=True, exist_ok=True)
            while not self.is_closed():
                await asyncio.sleep(15)
                if self.halted or (self.log_channel or self.channel) is None:
                    continue  # held while the user has paused the group
                try:
                    await self.post_reports()
                except Exception:
                    log.exception("reports watch failed")

        async def post_reports(self) -> None:
            ch = self.log_channel or self.channel
            now = time.time()
            for stale in REPORTS_DIR.glob("*.md.sending"):  # the bridge stopped mid-send: send it again
                if now - stale.stat().st_mtime > 60:
                    stale.replace(stale.with_name(stale.name[:-len(".sending")]))
            for f in sorted(REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime):
                try:
                    await self.post_report(f, ch, now)
                except Exception:  # one bad report must not hold up the ones behind it
                    log.exception("posting report %s failed", f.name)

        async def post_report(self, f: Path, ch, now: float) -> None:
            if True:
                if now - f.stat().st_mtime < 10:
                    return  # still being written
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                dest = REPORTS_DIR / "sent" / f"{time.strftime('%Y%m%d-%H%M%S')}-{f.name}"
                if not text:
                    f.replace(dest)
                    return
                first = text.splitlines()[0].lstrip("# ").strip()
                title = first[:80] if text.startswith("#") else f.stem
                excerpt = text if len(text) <= REPORT_EXCERPT else text[:REPORT_EXCERPT].rsplit("\n", 1)[0] + "\n..."
                if excerpt.count("```") % 2:
                    excerpt += "\n```"
                excerpt = fence_tables(excerpt).replace("⚙️", "").replace("⚙", "")
                body = S("report_head", claude=self.claude_id, title=title, n=len(text), path=dest) + excerpt
                if len(body) > 1990:
                    body = body[:1985] + "\n..."
                # read into memory: an open handle would block the move below on Windows
                data = f.read_bytes() if f.stat().st_size <= self.file_limit() else None
                # Out of the queue before sending, so a failed move afterwards cannot post it every 15 s.
                sending = f.with_name(f.name + ".sending")
                f.replace(sending)
                ping = discord.AllowedMentions(users=[discord.Object(id=self.claude_id)])
                self.report_bodies.append(body.strip())  # before sending: the post comes back through on_message
                try:
                    try:
                        files = [discord.File(io.BytesIO(data), filename=f.name)] if data is not None else []
                        sent = await ch.send(body, files=files, suppress_embeds=True, allowed_mentions=ping)
                    except discord.HTTPException as e:
                        if e.status != 413 or data is None:
                            raise
                        sent = await ch.send(body, suppress_embeds=True, allowed_mentions=ping)  # too large: the text names its path
                except Exception:
                    sending.replace(f)  # back in the queue for the next round
                    raise
                sending.replace(dest)
                log.info("posted session report %s (%d chars)", f.name, len(text))
                if self.cw_wake and self.cw_wake_report:  # off by default: a background report waits in the channel
                    self.loop.create_task(self.wake_claude(sent, body))

        # -- Status board: Claude rewrites data/status.md, the bridge keeps one pinned message in step
        async def status_watch(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await asyncio.sleep(20)
                try:
                    await self.update_status()
                except Exception:
                    log.exception("status board failed")

        async def update_status(self) -> None:
            if self.channel is None or not STATUS_FILE.exists():
                return
            mtime = STATUS_FILE.stat().st_mtime
            if mtime == self.status_mtime or time.time() - mtime < 3:
                return
            body = STATUS_FILE.read_text(encoding="utf-8", errors="replace").strip()
            body = fence_tables(body).replace("⚙️", "").replace("⚙", "")
            content = S("status_head", stamp=time.strftime("%m-%d %H:%M", time.localtime(mtime))) + body
            if len(content) > 1990:
                content = content[:1950].rsplit("\n", 1)[0] + S("status_cut")
            if not body or content == self.state.get("status_text"):
                self.status_mtime = mtime
                return
            quiet = discord.AllowedMentions.none()
            msg, mid = None, self.state.get("status_msg_id")
            if mid:
                try:
                    msg = await self.channel.fetch_message(int(mid))
                    await msg.edit(content=content, suppress=True, allowed_mentions=quiet)
                except discord.NotFound:
                    msg = None  # deleted: post a new one
            if msg is None:
                self.notes.append(content.strip())
                msg = await self.channel.send(content, suppress_embeds=True, allowed_mentions=quiet)
                self.state["status_msg_id"] = str(msg.id)
                try:
                    await msg.pin(reason="status board")
                except discord.HTTPException as e:
                    log.warning("could not pin the status board: %s", e)
                    await self.note(S("pin_failed"))
            self.status_mtime = mtime  # only now: a failed update is tried again
            self.state["status_text"] = content
            save_json(STATE_FILE, self.state)
            log.info("status board updated (%d chars)", len(content))

        async def stop_watch(self) -> None:
            while not self.is_closed():
                if STOP_FLAG.exists():
                    try:
                        STOP_FLAG.unlink()
                    except Exception:
                        pass
                    log.info("stop.flag found, exiting")
                    self.stopping = True
                    await self.gpt.shutdown()
                    await self.close()
                    return
                await asyncio.sleep(3)

        async def close(self) -> None:
            for m in self.members:
                await m.close()
            try:
                await self.gpt.shutdown()
            except Exception:
                pass
            await super().close()

    return Bridge()


# ---------------------------------------------------------------- CLI modes

def run_login(cfg: dict, state: dict) -> None:
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel=cfg["browser_channel"] or None, headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
            ignore_default_args=["--enable-automation"], no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(state.get("chat_url") or cfg["chat_url"], wait_until="domcontentloaded")
        print("Log in to ChatGPT in the window, open your chat once, then close the window.", flush=True)
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    print("login window closed", flush=True)


async def run_probe(cfg: dict, state: dict) -> None:
    gpt = ChatGPT(cfg, state)
    try:
        await gpt.start()
        await asyncio.sleep(5)
        turn = await gpt.last_turn()
        copied = await gpt.copied_reply()
        print(json.dumps({
            "url": gpt.page.url,
            "title": await gpt.page.title(),
            "composer": await gpt.page.locator(cfg["selectors"]["composer"]).first.is_visible(),
            "turns": turn["count"],
            "last_turn_state": turn["state"],
            "last_user_head": turn["userText"][:120],
            "last_reply_dom_head": turn["text"][:300],
            "last_reply_copy_head": (copied or "")[:300],
            "copy_len": len(copied or ""),
            "dom_len": len(turn["text"]),
        }, ensure_ascii=False, indent=2), flush=True)
    except (NeedsHuman, PageNotLoaded) as e:
        print("NEEDS HUMAN:" if isinstance(e, NeedsHuman) else "PAGE NOT LOADED:", e, "| url:", gpt.page.url if gpt.page else None, flush=True)
    finally:
        await gpt.shutdown()


def run_check() -> None:
    """Dry check of a profile: what would be used and what is missing. Starts no browser and
    no Discord connection; it only tries the single-instance lock to see if a bridge is running."""
    cfg = load_config()
    members = {k: cfg.get(k, {}) for k in ("codex", "gemini")}
    w = cfg.get("claude_watch", {})
    rows = [
        ("config", CONFIG_FILE, CONFIG_FILE.is_file()),
        ("runtime_dir", RUNTIME, RUNTIME.is_dir()),
        ("data_dir", DATA, DATA.is_dir()),
        ("state", STATE_FILE, STATE_FILE.is_file()),
        ("chrome profile", PROFILE_DIR, PROFILE_DIR.is_dir()),
        ("gpt token file", TOKEN_FILE, TOKEN_FILE.is_file()),
        ("access file", Path(os.path.expanduser(cfg.get("access_file") or str(ACCESS_FILE))), None),
        ("handoff", RUNTIME / "handoff.md", None),
        ("status", STATUS_FILE, None),
        ("reports", REPORTS_DIR, None),
    ]
    for key, m in members.items():
        if m.get("enabled"):
            token = Path(os.path.expanduser(m["token_file"])) if m.get("token_file") else (ROOT if PROFILE is None else RUNTIME) / f"{key}-bot.env"
            rows += [(f"{key} token file", token, token.is_file()),
                     (f"{key} workdir", Path(os.path.expanduser(m.get("workdir") or ".")), None)]
    ok = True
    for name, p, must in rows:
        exists = p.exists()
        if must is not None and not exists and name not in ("state", "data_dir", "chrome profile"):
            ok = False  # state, data folder and browser profile are created on first use
        print(f"{name:17} {'ok     ' if exists else 'MISSING'} {p}")
    for key in ("channel_id", "owner_id", "claude_bot_id"):
        if not str(cfg.get(key, "")).isdigit():
            ok = False
            print(f"config key {key} is missing or not a number")
    unknown = sorted(set(STR) - set(DEFAULT_STRINGS))
    if unknown:
        print("unknown strings (ignored):", ", ".join(unknown))
    for key, text in STR.items():
        try:
            text.format(**{n: "x" for n in re.findall(r"{(\w+)}", text)})
            want = set(re.findall(r"{(\w+)}", DEFAULT_STRINGS.get(key, "")))
            extra = set(re.findall(r"{(\w+)}", text)) - want
            if extra:
                ok = False
                print(f"string {key} uses unknown placeholders: {', '.join(sorted(extra))}")
        except Exception as e:
            ok = False
            print(f"string {key} does not format: {e}")
    print(f"channel {cfg.get('channel_id')} log {cfg.get('log_channel_id') or '-'}; session \"{w.get('session_name') or cfg.get('claude', {}).get('name', '')}\"; "
          + "".join(f"{k} {'on' if m.get('enabled') else 'off'}; " for k, m in members.items()) +
          f"idle stop {w.get('idle_stop_min', 0)} min; "
          f"wake on mention {bool(w.get('ensure_script') or w.get('wake_on_mention'))}; archive {cfg.get('archive_dir') or '-'}")
    lock = acquire_lock() if DATA.is_dir() else "free"
    print("bridge lock:", "free" if lock else "HELD (a bridge is running on this profile)")
    if lock and lock != "free":
        os.close(lock)
    print("CHECK OK" if ok else "CHECK FAILED")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--profile", help="a profile JSON file, or a folder holding config.json (default: config.json in the repository root)")
    ap.add_argument("--check", action="store_true", help="print the resolved paths and settings, start nothing")
    a = ap.parse_args()
    if a.profile:
        use_profile(a.profile)
    if a.check:
        run_check()
        return
    setup_logging(console=a.login or a.probe)
    cfg, state = load_config(), load_json(STATE_FILE, {})

    lock = acquire_lock()
    if lock is None:
        log.info("another bridge instance holds the lock; exiting")
        print("bridge is already running (it holds the browser profile). Stop it first: create stop.flag", flush=True)
        return
    if a.login:
        run_login(cfg, state)
        return
    if a.probe:
        asyncio.run(run_probe(cfg, state))
        return

    import discord

    token = read_token()
    backoff = 5
    while True:
        bridge = make_bridge(cfg, state)
        try:
            bridge.run(token, log_handler=None)
        except discord.LoginFailure:
            log.error("Discord login failed: check GPT_BOT_TOKEN")
            return
        except Exception:
            log.exception("bridge crashed")
        if bridge.stopping:
            return
        log.info("restarting in %ds", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
