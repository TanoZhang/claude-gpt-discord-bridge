"""Codex CLI as a member of the Discord group (optional).

@Codex in the channel starts a Codex turn. Like GPT, Codex receives everything said
in the channels since its own last reply, with the speaker in front of each line,
so a request needs no restated background. The Codex thread is resumed between
turns. The reply is posted under the Codex bot account (REST only, no gateway).

config.json:
  "codex": {
    "enabled": true,
    "workdir": "C:\\\\path\\\\to\\\\your\\\\project",
    "bot_id": "...",                  Codex bot user id (native @Codex mention)
    "token_file": "codex-bot.env",    CODEX_DISCORD_BOT_TOKEN=... (default: codex-bot.env in the repository root)
    "executable": "",                 optional; found automatically
    "sandbox": "workspace-write",     or "danger-full-access"
    "network": true,                  network access inside workspace-write
    "context_max_chars": 20000,
    "rotate_turns": 40,               start a new Codex thread after this many turns
    "stall_timeout_s": 1800,          stop a run that produced no event for this long (0 = never)
    "turn_timeout_s": 10800,          stop a run that takes longer than this in total (0 = never)
    "retries": 1,                     automatic retries of a failed run (not for usage limits)
    "intro": "..."                    optional; sent at the start of each Codex thread
  }
Owner commands: !codex status | !codex stop | !codex new
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

MENTION_RE = re.compile(r"@(?:bot)?codex\b", re.I)
FILE_RE = re.compile(r"\[\[discord-file:([^\]]+)\]\]")
LIMIT_RE = re.compile(r"usage.?limit|rate.?limit|quota|hit your .*limit", re.I)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Placeholders: {workdir}, {outbox}, {user} (the owner's label), {claude} (Claude's label).
DEFAULT_INTRO = (
    "[Group chat] You are Codex (Codex CLI), a member of a Discord group working on this project "
    "together with {user}, {claude} and GPT. Your working directory is {workdir}. "
    "I relay the channel messages since your last reply, each with its speaker in front, "
    "like [{user}], [{claude}] or [GPT]; a tag such as \"· #channel-name\" marks a message from that other channel. "
    "You are only called when someone writes @Codex: the task is the @Codex request among the last messages, "
    "the earlier messages are background. The first time, read CLAUDE.md, AGENTS.md and README.md in the "
    "working directory (whichever exist) to learn the project.\n"
    "Your final reply is posted to the channel as is: short, with the result, which files you changed, "
    "how you verified it, and what needs whose decision. Your reply goes to the channel where you were asked; "
    "if there is a separate log channel, keep technical detail there and give only conclusions in the main channel. "
    "Write @Claude or @GPT in your reply only when you need them to take over or review something, and say clearly "
    "what you need from them; otherwise leave them out so the group does not ping-pong. "
    "To send files or images, save them in {outbox} and write [[discord-file:FULL_PATH]] in your reply, up to 10. "
    "Do not push, publish, or change Discord, credential or permission settings unless the owner explicitly asks."
)


def find_codex(configured: str | None = None) -> str:
    if configured and Path(configured).is_file():
        return str(configured)
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    # A hidden service does not inherit the desktop app's PATH.
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OpenAI/Codex/bin"
    candidates = list(root.glob("*/codex.exe"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise RuntimeError("Codex CLI not found")


def read_token(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "CODEX_DISCORD_BOT_TOKEN" and value.strip():
            return value.strip()
    raise RuntimeError(f"no CODEX_DISCORD_BOT_TOKEN in {path}")


def parse_events(lines: list[str]) -> tuple[str | None, list[str], str | None]:
    """(thread id, agent messages in order, error text) from `codex exec --json` output."""
    thread, messages, error = None, [], None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread = event.get("thread_id") or thread
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            text = (event["item"].get("text") or "").strip()
            if text:
                messages.append(text)
        elif kind in ("turn.failed", "error"):
            err = event.get("error") or {}
            error = (err.get("message") if isinstance(err, dict) else str(err)) or event.get("message") or kind
    return thread, messages, error


class CodexAgent:
    def __init__(self, bridge, discord, cfg: dict, state: dict, save, log, helpers: dict):
        self.bridge = bridge
        self.discord = discord
        self.cfg = cfg.get("codex", {})
        self.full_cfg = cfg
        self.state = state
        self.save = save
        self.log = log
        self.split_message = helpers["split_message"]
        self.fence_tables = helpers["fence_tables"]
        self.S = helpers["S"]  # the bridge's string lookup: S("key", **values)
        self.root = Path(helpers["root"])
        self.outbox = Path(helpers["outbox"])
        self.stopped = False  # the owner stopped the current turn: no retry
        self.workdir = Path(os.path.expanduser(self.cfg.get("workdir") or "."))
        self.bot_id = int(self.cfg.get("bot_id") or 0)
        self.trigger = asyncio.Event()
        self.last_msg_at = 0.0
        self.target_id: int | None = None
        self.pending = False
        self.busy = False
        self.process = None
        self.poster = None  # a REST-only client logged in as the Codex bot
        self.attachments: list = []  # Discord attachments since the last turn (not persisted)
        s = self.state.setdefault("codex", {})
        s.setdefault("buffer", [])

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and self.bot_id > 0

    @property
    def s(self) -> dict:
        return self.state["codex"]

    # -- start
    async def start(self) -> None:
        configured = self.cfg.get("token_file")
        token_file = Path(os.path.expanduser(configured)) if configured else self.root / "codex-bot.env"
        try:
            client = self.discord.Client(intents=self.discord.Intents.none())
            await client.login(read_token(token_file))
            self.poster = client
            self.log.info("codex: posting as bot %s", client.user)
        except Exception as e:
            self.poster = None
            self.log.warning("codex: bot login failed, replies go through GPT: %s", e)
        asyncio.get_running_loop().create_task(self.worker())
        if self.s.get("pending"):  # asked just before the bridge stopped: do it now
            self.log.info("codex: a request from before the restart is pending")
            self.pending, self.target_id = True, self.s.get("target")
            self.trigger.set()

    async def close(self) -> None:
        self.stopped = True
        await self.kill()
        if self.poster:
            try:
                await self.poster.close()
            except Exception:
                pass

    # -- channel traffic
    def is_codex(self, msg) -> bool:
        return bool(self.bot_id) and msg.author.id == self.bot_id

    async def addressed(self, msg) -> bool:
        if any(u.id == self.bot_id for u in msg.mentions):
            return True
        if any(r.name.lower() == "codex" or getattr(getattr(r, "tags", None), "bot_id", None) == self.bot_id
               for r in msg.role_mentions):
            return True
        if MENTION_RE.search(msg.content or ""):
            return True
        ref = msg.reference
        if ref and ref.message_id:
            target = ref.resolved if isinstance(ref.resolved, self.discord.Message) else None
            if target is None:
                try:
                    target = await msg.channel.fetch_message(ref.message_id)
                except Exception:
                    target = None
            if target is not None and target.author.id == self.bot_id:
                return True
        return False

    def remember(self, speaker: str, text: str, attachments=()) -> None:
        buf = self.s["buffer"]
        now = time.time()
        if buf and buf[-1][0] == speaker and now - buf[-1][2] < 10:
            buf[-1][1] += "\n" + text  # one long message split into chunks
            buf[-1][2] = now
        else:
            buf.append([speaker, text, now])
        del buf[:-200]
        self.attachments.extend(attachments)
        self.last_msg_at = time.monotonic()

    async def observe(self, msg, speaker: str, text: str, may_trigger: bool) -> None:
        """Every channel message except Codex's own goes into Codex's context."""
        if not self.enabled or self.is_codex(msg):
            return
        self.remember(speaker, text, msg.attachments)
        if may_trigger and await self.addressed(msg):
            if self.target_id is None or self.target_id != self.bridge.channel_id:
                self.target_id = msg.channel.id  # the main channel wins if both asked
            if msg.author.bot and self.bridge.streak >= self.bridge.cap:
                self.log.info("codex: loop cap hit, not starting")
                await self.bridge.note(self.S("codex_loop_cap", n=self.bridge.streak))
                self.save()
                return
            self.log.info("codex: requested by %s", speaker)
            self.pending = True
            self.s.update(pending=True, target=self.target_id)  # survives a bridge restart
            self.trigger.set()
        self.save()

    def resume(self) -> None:
        if self.pending:
            self.trigger.set()

    # -- worker
    def take_context(self) -> str:
        limit = int(self.cfg.get("context_max_chars", 20000))
        lines = [f"[{s}] {t}" for s, t, _ in self.s["buffer"]]
        out, total = [], 0
        for line in reversed(lines):
            if total + len(line) > limit and out:
                out.append(self.S("omitted"))
                break
            out.append(line)
            total += len(line)
        return "\n\n".join(reversed(out))

    async def worker(self) -> None:
        while True:
            await self.trigger.wait()
            while time.monotonic() - self.last_msg_at < float(self.full_cfg.get("quiet_period_s", 3)):
                await asyncio.sleep(0.5)
            self.trigger.clear()
            if not self.pending:
                continue
            if self.bridge.halted:
                continue  # resume() picks it up
            self.pending = False
            target = self.target_id or self.bridge.channel_id
            self.target_id = None
            self.s.pop("pending", None)
            self.s.pop("target", None)
            n = len(self.s["buffer"])
            context = self.take_context()
            atts, self.attachments = self.attachments[-10:], []
            self.busy = True
            try:
                await self.run_turn(context, atts, target)
                del self.s["buffer"][:n]  # what arrived during the turn stays for next time
            except Exception as e:
                self.log.exception("codex turn failed")
                await self.post(target, self.S("codex_failed", err=str(e)[:300]), plain=True)
            finally:
                self.busy = False
                self.process = None
                self.save()

    def build_prompt(self, context: str, files: list[Path], target: int, fresh: bool) -> str:
        S = self.S
        parts = []
        if fresh:
            intro = self.cfg.get("intro") or DEFAULT_INTRO
            parts.append(intro.format(workdir=self.workdir, outbox=self.outbox,
                                      user=S("user_label"), claude=S("claude_label")))
            recent = self.s.get("recent", [])
            if recent:
                parts.append(S("codex_recent_header") + "\n" + "\n---\n".join(recent))
        parts.append(S("codex_context_header") + "\n" + context)
        if files:
            parts.append(S("codex_files_header") + "\n" + "\n".join(str(p) for p in files))
        name = S("codex_log") if target == self.bridge.log_channel_id else S("codex_main")
        parts.append(S("codex_target", name=name))
        return "\n\n".join(parts)

    def command_args(self, thread: str | None) -> list[str]:
        sandbox = self.cfg.get("sandbox", "workspace-write")
        args = [find_codex(self.cfg.get("executable")), "-a", "never", "-s", sandbox, "-C", str(self.workdir)]
        if sandbox == "workspace-write" and self.cfg.get("network", True):
            args += ["-c", "sandbox_workspace_write.network_access=true"]
        args += ["exec"]
        if thread:
            args += ["resume", "--json", "--skip-git-repo-check", thread, "-"]
        else:
            args += ["--json", "--skip-git-repo-check", "-"]
        return args

    async def exec_codex(self, prompt: str, thread: str | None) -> tuple[str | None, list[str], str | None, str]:
        self.process = await asyncio.create_subprocess_exec(
            *self.command_args(thread), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.workdir), creationflags=NO_WINDOW, limit=16 * 1024 * 1024)
        proc = self.process
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        # Read the event stream as it comes, so a hung run is noticed: no event for stall_timeout_s
        # (one long shell command is silent meanwhile, so keep it generous) or the whole turn over
        # turn_timeout_s stops the process. 0 turns a limit off.
        stall = float(self.cfg.get("stall_timeout_s", 1800))
        total = float(self.cfg.get("turn_timeout_s", 10800))
        started, lines, timed_out = time.monotonic(), [], None
        err_task = asyncio.create_task(proc.stderr.read())
        while True:
            wait = stall if stall > 0 else None
            if total > 0:
                left = total - (time.monotonic() - started)
                wait = left if wait is None else min(wait, left)
            try:
                if wait is not None and wait <= 0:
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(proc.stdout.readline(), wait)
            except asyncio.TimeoutError:
                over = total > 0 and time.monotonic() - started >= total
                timed_out = (self.S("codex_timeout", min=int(total // 60)) if over
                             else self.S("codex_stalled", min=int(stall // 60)))
                self.log.warning("codex: %s; stopping the process", timed_out)
                await self.kill()
                break
            except ValueError:  # one event longer than the stream limit
                continue
            if not raw:
                break
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        await proc.wait()
        err = await err_task
        journal = self.outbox.parent / "codex-runs"
        journal.mkdir(parents=True, exist_ok=True)
        (journal / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl").write_text("\n".join(lines), encoding="utf-8")
        new_thread, messages, error = parse_events(lines)
        stderr = err.decode("utf-8", "replace").strip()
        if timed_out:
            error = timed_out
        elif proc.returncode and not error:
            error = (stderr.splitlines() or [f"exit code {proc.returncode}"])[-1]
        return new_thread, messages, error, stderr

    async def kill(self) -> None:
        """Stop the Codex process and everything it started (shells, test runs)."""
        proc = self.process
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                k = await asyncio.create_subprocess_exec(
                    "taskkill.exe", "/PID", str(proc.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
                await k.wait()
            if proc.returncode is None:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass

    async def run_turn(self, context: str, atts: list, target: int) -> None:
        files, failed = await self.bridge.download(atts) if atts else ([], [])
        if failed:
            context += "\n\n" + self.S("attach_failed", names=", ".join(failed))
        thread = self.s.get("thread")
        if thread and int(self.s.get("turns", 0)) >= int(self.cfg.get("rotate_turns", 40)):
            self.log.info("codex: %s turns in thread, starting a new one", self.s.get("turns"))
            thread = None
        self.stopped = False
        tries = 1 + max(0, int(self.cfg.get("retries", 1)))
        async with self.typing(target):
            prompt = self.build_prompt(context, files, target, fresh=not thread)
            for attempt in range(tries):
                new_thread, messages, error, stderr = await self.exec_codex(prompt, thread)
                if thread and error and not messages and re.search(r"not found|no (such )?(session|thread|rollout)", error + stderr, re.I):
                    self.log.warning("codex: thread %s cannot be resumed (%s); starting a new one", thread, error)
                    thread = None
                    prompt = self.build_prompt(context, files, target, fresh=True)
                    new_thread, messages, error, stderr = await self.exec_codex(prompt, None)
                if new_thread and new_thread != thread:
                    self.s.update(thread=new_thread, turns=0)
                    thread = new_thread
                if messages or not error or self.stopped or LIMIT_RE.search(error) or attempt == tries - 1:
                    break
                # One automatic retry. If the failed run got as far as a thread, that thread already
                # holds the whole prompt: continue it with one line instead of sending it all again.
                self.log.warning("codex: turn failed (%s); retrying in 20 s", error[:200])
                await asyncio.sleep(20)
                if self.stopped:
                    break
                if new_thread:
                    prompt = self.S("codex_continue", err=error[:200])
        if error and not messages:
            if LIMIT_RE.search(error):
                raise RuntimeError(self.S("codex_limit", err=error[:200]))
            raise RuntimeError(error[:300])
        self.s["turns"] = int(self.s.get("turns", 0)) + 1
        reply = messages[-1] if messages else self.S("codex_empty")
        self.s["recent"] = (self.s.get("recent", []) + [reply[:1200]])[-3:]
        self.save()
        await self.post(target, reply)

    # -- Discord output
    def typing(self, channel_id: int):
        if self.poster:
            return self.poster.get_partial_messageable(channel_id).typing()
        ch = self.bridge.get_channel(channel_id)
        return ch.typing() if ch else _Null()

    def outputs(self, text: str) -> tuple[str, list[Path]]:
        files = []
        roots = [self.outbox.resolve(), self.workdir.resolve()]
        for raw in FILE_RE.findall(text):
            p = Path(raw.strip())
            p = (self.workdir / p) if not p.is_absolute() else p
            try:
                p = p.resolve()
            except OSError:
                continue
            if p.is_file() and any(p == r or r in p.parents for r in roots) and p not in files:
                files.append(p)
        return FILE_RE.sub("", text).strip(), files[:10]

    async def post(self, channel_id: int, text: str, plain: bool = False) -> None:
        discord = self.discord
        files = []
        if not plain:
            text, files = self.outputs(text)
            text = re.sub(r"@(?:bot)?claude\b", f"<@{self.bridge.claude_id}>", text, flags=re.I)
            if self.bridge.user:
                text = re.sub(r"@gpt\b", f"<@{self.bridge.user.id}>", text, flags=re.I)
            text = self.fence_tables(text)
        text = text.replace("\u2699\ufe0f", "").replace("\u2699", "")  # only the bridge writes control messages
        text = re.sub(r"@(everyone|here)\b", "@\u200b\\1", text)
        limit = self.bridge.file_limit()
        big = [p for p in files if p.stat().st_size > limit]
        files = [p for p in files if p.stat().st_size <= limit]
        if big:
            text += "\n\n" + self.S("codex_too_big", files=", ".join(str(p) for p in big))
        if self.poster:
            channel = self.poster.get_partial_messageable(channel_id)
            prefix = ""
        else:
            channel = self.bridge.get_channel(channel_id) or self.bridge.channel
            prefix = "**[Codex]** "
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        parts = self.split_message(prefix + text) or [""]
        if len(parts) > 1:
            parts = [f"{p}\n({i + 1}/{len(parts)})" for i, p in enumerate(parts)]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last and files:
                for j in range(0, len(files), 10):
                    chunk = files[j:j + 10]
                    await channel.send(part if j == 0 else None, allowed_mentions=mentions, suppress_embeds=True,
                                       files=[discord.File(str(p), filename=p.name) for p in chunk])
            else:
                await channel.send(part, allowed_mentions=mentions, suppress_embeds=True)
        self.log.info("codex posted %d chars, %d files", len(text), len(files))

    # -- owner commands
    async def command(self, arg: str) -> str:
        S = self.S
        cmd = (arg.split() or ["status"])[0].lower()
        if cmd == "stop":
            if self.busy:
                self.stopped = True  # also ends a pending retry
                await self.kill()
                return S("codex_stopped")
            return S("codex_not_running")
        if cmd == "new":
            self.s.pop("thread", None)
            self.s["turns"] = 0
            self.save()
            return S("codex_new")
        return S("codex_status", state=S("codex_running") if self.busy else S("codex_idle"),
                 session=self.s.get("thread") or S("codex_new_session"), turns=self.s.get("turns", 0),
                 pending=len(self.s["buffer"]),
                 poster=S("codex_poster_bot") if self.poster else S("codex_poster_gpt"))


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
