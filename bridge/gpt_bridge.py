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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path

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
    "halt_claude": "The user paused the group: finish the step you are on, say where you stopped, start nothing new, do not message GPT. Running background tasks may finish; post their results after [继续].",
    "halt_done": "⏸️ Paused: GPT stays quiet and Claude stops after its current step. Send `!resume` to continue.",
    "resume_not_halted": "▶️ Nothing is paused.",
    "resume_claude": "The user resumed the group: continue where you stopped, and say in one line what you continue with.",
    "resume_done": "▶️ Resumed.",
    "claude_limit": "⏸️ Claude hit its usage limit ({text}). Back around {when}; I will tell it to continue then. GPT keeps working meanwhile.",
    "limit_over_claude": "Your usage limit should have reset: continue the work that was cut off. Check `git worktree list` and the latest channel messages first, and report when done.",
    "ctx_claude": "Your context is about {k}k tokens. When no background task is running: write a handoff to `{handoff}` (what is done, what is in progress and where, next steps, questions for the user), then create the empty file `{restart}`. The bridge starts a fresh session that reads the handoff first.",
    "restart_start": "♻️ Claude asked for a fresh session (saves tokens); restarting...",
    "handoff_claude": "You are a fresh session replacing the previous Claude. Read `{handoff}` first, continue from it, and say in one line what you took over.",
    "status_halted": "Group: ⏸️ paused by the user (`!resume` to continue)",
    "status_running": "Group: ▶️ running",
    "status_claude_limit": "; Claude: ⏸️ usage limit, back around {when}",
    "status_claude_ctx": "; Claude context about {k}k tokens",
    "recent_header": "[Your most recent replies in the previous chat (excerpts); carry on from here]",
    "rollover_done": "New chat: <{url}>",
    "mirror_header": "**(asked directly in ChatGPT)**",
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


def fence_tables(text: str, max_width: int = 60) -> str:
    """Discord does not render markdown tables. A narrow table becomes an aligned code
    block; a wide one becomes a list (one bullet per row), which reads better on a phone."""
    lines = text.split("\n")
    out, i, in_code = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
        is_table = (not in_code and line.strip().startswith("|") and i + 1 < len(lines)
                    and TABLE_SEP_RE.match(lines[i + 1]))
        if not is_table:
            out.append(line)
            i += 1
            continue
        rows, j = [], i
        while j < len(lines) and lines[j].strip().startswith("|"):
            if not TABLE_SEP_RE.match(lines[j]):
                raw = lines[j].strip().strip("|").replace("\\|", "\x00")
                rows.append([re.sub(r"\*\*|`", "", c).strip().replace("\x00", "|") for c in raw.split("|")])
            j += 1
        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]
        widths = [max(_width(r[k]) for r in rows) for k in range(ncol)]
        if sum(widths) + 2 * (ncol - 1) <= max_width:
            fmt = lambda r: "  ".join(c + " " * (widths[k] - _width(c)) for k, c in enumerate(r)).rstrip()
            out += ["```", fmt(rows[0]), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows[1:]] + ["```"]
        else:
            head = rows[0]
            for r in rows[1:]:
                rest = "; ".join(f"{head[k]}: {r[k]}" for k in range(1, ncol) if r[k])
                out.append(f"- **{r[0]}**" + (f" — {rest}" if rest else ""))
        i = j
    return "\n".join(out)


ADDRESS_RE = re.compile(r"(?:^\s*gpt\s*[,，:：])|(?:@gpt\b)", re.I)


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
  return {
    key: last.getAttribute('data-turn-key') || String(turns.length),
    count: turns.length,
    state: st ? st.getAttribute(stAttr) : null,
    userText: user ? user.innerText : '',
    text: contents.map(conv).filter(s => s.trim()).join('\n\n'),
    imgs, downloads, fallback,
  };
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


def conv_id(url: str) -> str:
    m = re.search(r"/c/([0-9a-f-]{8,})", url or "")
    return m.group(1) if m else (url or "").split("?")[0]


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


LIMIT_RE = re.compile(r"hit your (session |5-hour |weekly )?limit|usage limit|limit reached|out of (extra )?usage", re.I)
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


ERROR_RE = re.compile(r"something went wrong|unknown error|error (generating|occurred)|network error|try again|retry|出错|错误|重试|请稍后", re.I)
TOO_LONG_RE = re.compile(r"too long|too large|maximum (context|length)|context (window|length)|start (a )?new chat|对话太长|过长|新对话", re.I)


class NeedsHuman(Exception):
    """The ChatGPT page needs the user: login, or a check only a person may click."""


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
        self.page.on("response", self._on_response)
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

    async def close(self) -> None:
        if self.ctx is not None:
            try:
                await self.ctx.close()
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
        return self.page is not None and not self.page.is_closed()

    async def open_chat(self, url: str | None = None) -> None:
        url = url or self.chat_url
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await self.wait_composer()
        self.last_used = time.monotonic()

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
        raise NeedsHuman(problem or S("problem_no_composer"))

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

    async def check_for_web_turn(self, seen_key: str | None) -> dict | None:
        """Reload the chat; if its last turn is new (typed in ChatGPT itself) and finished,
        return {key, user, text, outputs}. Returns None when there is nothing new."""
        await self.open_chat()
        turn = await self.last_turn()
        for _ in range(15):  # turns render a moment after the composer appears
            if turn["key"] and turn["state"] is not None:
                break
            await asyncio.sleep(1)
            turn = await self.last_turn()
        await asyncio.sleep(1)
        turn = await self.last_turn()
        if not turn["key"] or turn["key"] == seen_key or turn["state"] not in (None, self.sel["turn_done"]):
            return None
        if self.page and await self.page.locator(self.sel["stop"]).first.is_visible():
            return None
        if seen_key is None:  # first look after start: remember, do not replay history
            return {"key": turn["key"], "skip": True}
        if re.match(r"^\[[^\]]{1,32}\]", (turn["userText"] or "").strip()):
            return {"key": turn["key"], "skip": True}  # a turn the bridge itself sent
        text = turn["text"] or turn.get("fallback", "")
        if text:
            text = (await self.copied_reply()) or text
        return {"key": turn["key"], "user": turn["userText"], "text": text, "outputs": await self.outputs_of_last_turn()}

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
            self.caught_up = False
            self.halted = self.state.get("halted")  # {"since": ...} while the user has paused the group
            self.gpt_pending = False  # GPT was addressed while halted
            # Claude watcher
            w = self.cfg.get("claude_watch", {})
            c = self.cfg.get("claude", {})
            if c.get("workdir"):
                w.setdefault("project_dir", str(Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", c["workdir"])))
            w.setdefault("session_name", c.get("name", "Group Claude"))
            w.setdefault("work_dir", str(DATA))
            w.setdefault("ensure_script", str(ROOT / "scripts" / "ensure.ps1"))
            w.setdefault("lock_file", str(DATA / "ensure.lock"))
            self.cw_dir = Path(os.path.expanduser(w["project_dir"])) if w.get("project_dir") else None
            self.cw_name = w.get("session_name", "")
            self.cw_ctx_limit = int(w.get("ctx_limit", 150000))
            self.cw_work = Path(os.path.expanduser(w["work_dir"]))
            self.cw_ensure = os.path.expanduser(w.get("ensure_script", ""))
            self.cw_lock = os.path.expanduser(w.get("lock_file", ""))
            self.cw_file: Path | None = None
            self.cw_offset = 0
            self.cw_known: dict[str, bool] = {}
            self.cw_ctx = 0
            self.cw_nudged_at = 0.0
            self.cw_paused_until: float | None = None
            self.cw_limit_text = ""

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
            return ids or {self.owner_id}

        def in_channel(self, msg) -> bool:
            ch = msg.channel
            parent = getattr(ch, "parent_id", None)
            return ch.id == self.channel_id or parent == self.channel_id

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

        def remember(self, speaker: str, text: str, attachments=()) -> None:
            now = time.monotonic()
            atts = list(attachments)
            if self.buffer and self.buffer[-1][0] == speaker and now - self.buffer[-1][2] < 10:
                self.buffer[-1][1] += "\n" + text  # a long message split into chunks
                self.buffer[-1][2] = now
                self.buffer[-1][3].extend(atts)
            else:
                self.buffer.append([speaker, text, now, atts])
            self.last_msg_at = now

        def take_buffer(self) -> tuple[str, list]:
            """Everything said since GPT's last turn, and the attachments in it (newest kept)."""
            lines = [t if s is None else f"[{s}] {t}" for s, t, _, _ in self.buffer]
            atts = [a for entry in self.buffer for a in entry[3]]
            self.buffer.clear()
            limit = self.cfg["context_max_chars"]
            out, total = [], 0
            for line in reversed(lines):
                if total + len(line) > limit and out:
                    out.append(S("omitted"))
                    break
                out.append(line)
                total += len(line)
            return "\n\n".join(reversed(out)), atts[-MAX_UPLOAD_FILES:]

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

        async def note(self, text: str) -> None:
            try:
                if self.channel:
                    await self.channel.send(text, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
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

        async def on_ready(self) -> None:
            log.info("discord connected as %s (%s)", self.user, self.user.id)
            try:
                self.channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            except Exception as e:
                log.error("cannot open channel %s: %s", self.channel_id, e)
            if not self.gpt.alive():
                try:
                    await self.gpt.start()
                except NeedsHuman as e:
                    log.warning("browser needs the user: %s", e)
                except Exception:
                    log.exception("browser start failed")
            if not self.caught_up:
                self.caught_up = True
                await self.catch_up()

        async def catch_up(self) -> None:
            """After a restart, re-queue everything said since GPT's last real reply."""
            last_id = self.state.get("last_reply_msg_id")
            if not last_id or self.channel is None:
                return
            try:
                n, pending = 0, False
                async for msg in self.channel.history(limit=100, after=discord.Object(id=int(last_id)), oldest_first=True):
                    if msg.author.id == self.user.id or (msg.author.bot and msg.author.id != self.claude_id):
                        continue
                    if not msg.author.bot and msg.author.id not in self.humans():
                        continue
                    if (msg.content or "").strip().lower().startswith("!gpt"):
                        continue
                    self.remember("Claude" if msg.author.bot else self.speaker(msg), self.clean(msg), msg.attachments)
                    n += 1
                    pending = pending or await self.addressed_to_gpt(msg)
                log.info("catch-up: %d messages since the last reply; addressed to GPT: %s", n, pending)
                if pending:
                    self.streak, self.capped = 0, False
                    self.trigger.set()
            except Exception:
                log.exception("catch-up failed")

        async def on_message(self, msg) -> None:
            try:
                await self.handle(msg)
            except Exception:
                log.exception("on_message failed")

        async def handle(self, msg) -> None:
            if not self.in_channel(msg) or self.user is None or msg.author.id == self.user.id:
                return
            if msg.author.bot:
                if msg.author.id != self.claude_id:
                    return
                now = time.monotonic()
                if now - self.last_claude_at > 5:  # chunks of one Claude reply count once
                    self.streak += 1
                self.last_claude_at = now
                self.remember("Claude", self.clean(msg), msg.attachments)
                if await self.addressed_to_gpt(msg):
                    log.info("claude -> gpt (streak=%d)", self.streak)
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
            self.remember(self.speaker(msg), self.clean(msg), msg.attachments)
            if await self.addressed_to_gpt(msg):
                log.info("human -> gpt")
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
                self.state["chat_url"] = parts[1].strip("<>")
                save_json(STATE_FILE, self.state)
                await self.note(S("url_set"))
            elif cmd == "new":
                fresh = len(parts) > 1 and parts[1].lower() == "fresh"
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
            await self.note(S("rollover_start", reason=reason, handoff=S("with_handoff") if handoff else ""))
            summary = ""
            if handoff:
                try:
                    summary, _, _ = await self.gpt.ask(self.cfg["handoff_prompt"], collect=False)
                except ReplyFailed as e:
                    log.warning("handoff summary failed: %s", e)
            if not summary.strip() and self.state.get("recent_replies"):
                summary = S("recent_header") + "\n\n" + "\n\n---\n\n".join(self.state["recent_replies"])
            first = self.cfg["intro"]
            if summary.strip():
                first += "\n\n" + S("handoff_header") + "\n" + summary.strip()
            url = await self.gpt.new_chat(first)
            self.state.update(intro_sent=True, intro_version=int(self.cfg.get("intro_version", 1)),
                              turns_in_chat=0, last_turn_key=self.gpt.last_key)
            save_json(STATE_FILE, self.state)
            await self.note(S("rollover_done", url=url))

        async def mirror_watch(self) -> None:
            """Post turns the user typed in ChatGPT itself (not through Discord) to the channel."""
            await self.wait_until_ready()
            interval = int(self.cfg.get("mirror_interval_s", 0))
            if interval <= 0:
                return
            while not self.is_closed():
                await asyncio.sleep(interval)
                if self.busy or self.page_lock.locked() or self.trigger.is_set() or not self.gpt.alive() or self.channel is None:
                    continue
                try:
                    async with self.page_lock:
                        found = await self.gpt.check_for_web_turn(self.state.get("last_turn_key"))
                    if not found:
                        continue
                    self.state["last_turn_key"] = found["key"]
                    save_json(STATE_FILE, self.state)
                    user = (found.get("user") or "").strip()
                    if found.get("skip") or re.match(r"^\[[^\]]{1,32}\]", user):
                        continue  # first look, or a turn the bridge itself sent
                    self.state["turns_in_chat"] = int(self.state.get("turns_in_chat", 0)) + 1
                    self.streak, self.capped = 0, False  # a human started this turn
                    quote = "\n".join("> " + line for line in user[:800].splitlines() if line.strip())
                    log.info("mirroring a turn typed in ChatGPT (%d chars)", len(found["text"]))
                    await self.post_reply(f"{S('mirror_header')}\n{quote}\n\n{found['text']}", True, found["outputs"])
                except NeedsHuman as e:
                    log.warning("mirror check needs the user: %s", e)
                except Exception:
                    log.exception("mirror check failed")

        def requeue(self, batch: str, atts) -> None:
            """Put a batch that did not reach GPT back at the front of the buffer."""
            self.buffer.insert(0, [None, batch, time.monotonic(), list(atts)])

        async def retry_later(self, delay: float) -> None:
            await asyncio.sleep(delay)
            if self.buffer:
                self.trigger.set()

        async def control(self, tag: str, text: str) -> None:
            """A bridge control message to Claude: posted by the GPT bot, starts with the gear emoji."""
            try:
                await self.channel.send(f"\u2699\ufe0f {tag} <@{self.claude_id}> {text}",
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
            await self.control("[暂停]", S("halt_claude"))
            await self.note(S("halt_done"))

        async def resume(self) -> None:
            if not self.halted:
                await self.note(S("resume_not_halted"))
                return
            self.halted = None
            self.state.pop("halted", None)
            save_json(STATE_FILE, self.state)
            log.info("resumed by the user")
            await self.control("[继续]", S("resume_claude"))
            await self.note(S("resume_done"))
            if self.gpt_pending:
                self.gpt_pending = False
                self.streak, self.capped = 0, False
                self.trigger.set()

        # -- Claude watcher: reads the Claude session's transcript (no tokens spent)
        def _cw_find(self) -> Path | None:
            if not self.cw_dir or not self.cw_dir.is_dir():
                return None
            marker = f'"customTitle":"{self.cw_name}"'
            for f in sorted(self.cw_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:12]:
                key = str(f)
                if key not in self.cw_known:
                    try:
                        self.cw_known[key] = marker in f.read_text(encoding="utf-8", errors="replace")
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
                req.unlink(missing_ok=True)
                await self.restart_claude()
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
            now = time.time()
            if self.cw_paused_until and now >= self.cw_paused_until + 60:
                self.cw_paused_until = None
                await self.control("[额度恢复]", S("limit_over_claude"))
            if (self.cw_ctx > self.cw_ctx_limit and now - self.cw_nudged_at > 7200
                    and not self.halted and not self.cw_paused_until):
                self.cw_nudged_at = now
                handoff, restart = self.cw_work / "handoff.md", self.cw_work / "restart.request"
                await self.control("[上下文]", S("ctx_claude", k=self.cw_ctx // 1000, handoff=handoff, restart=restart))

        async def restart_claude(self) -> None:
            """Fresh Claude session on request: stop the old one, start a new one, hand over."""
            handoff = self.cw_work / "handoff.md"
            await self.note(S("restart_start"))
            name = self.cw_name.replace("'", "''")
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' -and $_.CommandLine -match '--channels' "
                  f"-and $_.CommandLine -match [regex]::Escape('{name}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}; "
                  "Start-Sleep -Seconds 3; ")
            if self.cw_lock:
                ps += f"Remove-Item '{self.cw_lock}' -ErrorAction SilentlyContinue; "
            if self.cw_ensure:
                ps += f"& '{self.cw_ensure}'"
            proc = await asyncio.create_subprocess_exec("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps)
            await proc.wait()
            self.cw_file, self.cw_ctx, self.cw_nudged_at = None, 0, time.time()
            await asyncio.sleep(45)  # the new session needs a moment to connect to Discord
            await self.control("[交接]", S("handoff_claude", handoff=handoff))
            log.info("Claude restarted as a fresh session")

        async def worker(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await self.trigger.wait()
                while time.monotonic() - self.last_msg_at < self.cfg["quiet_period_s"]:
                    await asyncio.sleep(0.5)  # wait for the rest of a multi-chunk message
                self.trigger.clear()
                batch, atts = self.take_buffer()
                if not batch:
                    continue
                prompt = batch
                self.busy = True
                try:
                    if self.channel is None:
                        self.channel = await self.fetch_channel(self.channel_id)
                    async with self.page_lock, self.channel.typing():
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
                        text, finished, outputs = await self.gpt.ask(prompt, files)
                    self.failures = 0
                    self.state.update(intro_sent=True, intro_version=version, last_turn_key=self.gpt.last_key,
                                      turns_in_chat=int(self.state.get("turns_in_chat", 0)) + 1)
                    save_json(STATE_FILE, self.state)
                    await self.post_reply(text, finished, outputs)
                except NeedsHuman as e:
                    log.warning("needs human: %s", e)
                    self.requeue(batch, atts)
                    await self.note(S("needs_human_ask", problem=e))
                except Exception as e:
                    # Never drop the batch: put it back, then retry (or roll over to a new chat).
                    self.requeue(batch, atts)
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
                            async with self.page_lock:
                                if isinstance(e, ReplyFailed) and self.gpt.alive():
                                    await self.gpt.open_chat()  # reload: shows the chat as the server has it
                                else:
                                    await self.gpt.start()
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

        async def post_reply(self, text: str, finished: bool, outputs: list[Path] = ()) -> None:
            text = re.sub(r"@(?:bot)?claude\b", f"<@{self.claude_id}>", text, flags=re.I)
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
                            sent = await self.channel.send(head, files=[discord.File(str(p), filename=p.name) for p in chunk],
                                                           suppress_embeds=True)
                            first_msg = first_msg or sent
                        except discord.HTTPException as e:
                            if e.status != 413:
                                raise
                            log.warning("upload rejected as too large: %s", [p.name for p in chunk])
                            await self.channel.send(((head + "\n\n") if head else "") + self.too_big_text(chunk),
                                                    suppress_embeds=True)
                else:
                    sent = await self.channel.send(part, suppress_embeds=True)
                    first_msg = first_msg or sent
            if first_msg is not None:
                self.state["last_reply_msg_id"] = str(first_msg.id)
                self.state["recent_replies"] = (self.state.get("recent_replies", []) + [text[:1500]])[-3:]
                save_json(STATE_FILE, self.state)
            self.streak += 1
            log.info("gpt posted %d chars, %d files (streak=%d)", len(text), len(files), self.streak)

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
    except NeedsHuman as e:
        print("NEEDS HUMAN:", e, "| url:", gpt.page.url if gpt.page else None, flush=True)
    finally:
        await gpt.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
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
