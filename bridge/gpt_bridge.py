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
    "rollover_done": "New chat: <{url}>",
    "mirror_header": "**(asked directly in ChatGPT)**",
    "attach_failed": "[these attachments could not be passed on: {names}]",
    "needs_human_ask": "{problem}; this message was not sent to GPT. Send `!gpt show` to open the browser window, `!gpt hide` when done, then @GPT again.",
    "ask_failed": "GPT bridge error ({err}); no reply for this one. Restarting the browser, please @GPT again in a moment.",
    "too_big": "(These files are over Discord's 10 MB limit and were not sent: {files})",
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
    "stop": "[data-chatgpt-composer] button[aria-label^='Stop'], button[data-testid='stop-button']",
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
  const imgs = Array.from(last.querySelectorAll('img')).filter(i => reply(i) && i.alt !== 'User attachment' && i.naturalWidth >= 100 && i.naturalHeight >= 100).length;
  const downloads = Array.from(last.querySelectorAll(sel.download_button)).filter(reply).length;
  return {
    key: last.getAttribute('data-turn-key') || String(turns.length),
    count: turns.length,
    state: st ? st.getAttribute(stAttr) : null,
    userText: user ? user.innerText : '',
    text: contents.map(conv).filter(s => s.trim()).join('\n\n'),
    imgs, downloads,
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
    if (!src || seen.has(src) || img.closest(sel.user_part) || img.alt === 'User attachment') continue;
    if (img.naturalWidth < 100 || img.naturalHeight < 100) continue;
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

    async def _send(self, text: str, files: list[Path] | None = None) -> None:
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
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("message did not appear in the chat after sending")

    async def _wait_reply(self) -> tuple[str, bool]:
        """Return (text, finished). finished=False means timeout with partial text."""
        start = time.monotonic()
        stop_sel = self.sel["stop"]
        prev, stable = None, 0
        while True:
            elapsed = time.monotonic() - start
            turn = await self.last_turn()
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
            if not has_output and not generating and elapsed > self.cfg["first_token_timeout_s"]:
                if done_state:
                    return "", True  # finished with nothing we can show
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
        await self._send(text, files)
        reply, finished = await self._wait_reply()
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
        text = turn["text"]
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
            lines = [f"[{s}] {t}" for s, t, _, _ in self.buffer]
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
                    await self.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                log.warning("note failed: %s", e)

        def request_gpt(self) -> None:
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
                      turns=self.state.get("turns_in_chat", 0), limit=f"/{limit}" if limit else "", url=self.gpt.chat_url))
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
                summary, _, _ = await self.gpt.ask(self.cfg["handoff_prompt"], collect=False)
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

        async def worker(self) -> None:
            await self.wait_until_ready()
            while not self.is_closed():
                await self.trigger.wait()
                while time.monotonic() - self.last_msg_at < self.cfg["quiet_period_s"]:
                    await asyncio.sleep(0.5)  # wait for the rest of a multi-chunk message
                self.trigger.clear()
                prompt, atts = self.take_buffer()
                if not prompt:
                    continue
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
                    self.state.update(intro_sent=True, intro_version=version, last_turn_key=self.gpt.last_key,
                                      turns_in_chat=int(self.state.get("turns_in_chat", 0)) + 1)
                    save_json(STATE_FILE, self.state)
                    await self.post_reply(text, finished, outputs)
                except NeedsHuman as e:
                    log.warning("needs human: %s", e)
                    await self.note(S("needs_human_ask", problem=e))
                except Exception as e:
                    log.exception("ask failed")
                    await self.note(S("ask_failed", err=type(e).__name__))
                    try:
                        await self.gpt.start()
                    except Exception:
                        log.exception("browser restart failed")
                finally:
                    self.busy = False

        async def post_reply(self, text: str, finished: bool, outputs: list[Path] = ()) -> None:
            text = re.sub(r"@(?:bot)?claude\b", f"<@{self.claude_id}>", text, flags=re.I)
            text = fence_tables(text)
            text = re.sub(r"@(everyone|here)\b", "@​\\1", text)
            files = [p for p in outputs if p.stat().st_size <= MAX_DISCORD_FILE]
            too_big = [p for p in outputs if p.stat().st_size > MAX_DISCORD_FILE]
            if too_big:
                text += "\n\n" + S("too_big", files=", ".join(
                    f"{p.name} {p.stat().st_size / 1048576:.1f} MB" for p in too_big))
            if not text.strip() and not files:
                await self.note(S("empty"))
                return
            if not finished:
                text += "\n\n" + S("timeout")
            parts = split_message(text) or [""]
            if len(parts) > 1:  # numbered, so a reader who got only some parts knows to fetch the rest
                parts = [f"{p}\n({i + 1}/{len(parts)})" for i, p in enumerate(parts)]
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if last and files:
                    # Discord takes at most 10 files per message.
                    for j in range(0, len(files), 10):
                        batch = [discord.File(str(p), filename=p.name) for p in files[j:j + 10]]
                        await self.channel.send(part if j == 0 else None, files=batch)
                else:
                    await self.channel.send(part)
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
