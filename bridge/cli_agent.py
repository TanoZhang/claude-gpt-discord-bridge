"""A coding-agent CLI as a member of the Discord group: the shared part.

A member (Codex, Gemini, ...) is called with @Name in the channel or by a reply to one of its
messages. Like GPT, it receives everything said in the channels since its own last reply, with
the speaker in front of each line, so a request needs no restated background. Its session is
resumed between turns. The reply is posted under the member's own bot account (REST only, no
gateway); without that bot it goes out through the GPT bot with a "[Name]" prefix.

A subclass sets the class attributes below and implements exec_cli(); everything else
(buffering, catch-up, retries, timeouts, attachments, owner commands) is shared.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

FILE_RE = re.compile(r"\[\[discord-file:([^\]]+)\]\]")
LIMIT_RE = re.compile(r"usage.?limit|rate.?limit|quota|hit your .*limit|resource.?exhausted|out of credits", re.I)
RESUME_LOST_RE = re.compile(r"not found|no (such )?(session|thread|rollout|conversation)|unknown conversation|"
                            r"invalid conversation", re.I)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# In a reply: [[model:NAME]] / [[effort:LEVEL]] switches the member from its next turn ("default" = the config's).
SWITCH_RE = re.compile(r"\[\[(model|effort):\s*([^\]\s]+)\s*\]\]", re.I)
NEW_SESSION_RE = re.compile(r"\[\[new-session\]\]", re.I)  # in a reply: a fresh session from the next turn
HELP_VERSION = 1  # raise when switch_help / session_help change: running sessions get them once more


def read_token(path: Path, var: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == var and value.strip():
            return value.strip()
    raise RuntimeError(f"no {var} in {path}")


class CliAgent:
    key = ""            # config section, state key, owner command (!key) and string prefix in logs
    name = ""           # how the group calls it: @Name, [Name]
    token_var = ""      # variable in the bot token file
    mention_re: re.Pattern = re.compile(r"(?!)")
    default_intro = ""  # placeholders: {workdir}, {outbox}, {user}, {claude}
    efforts: tuple = ()  # effort levels the CLI accepts
    models_hint = ""     # where the member finds the model names it may use

    def __init__(self, bridge, discord, cfg: dict, state: dict, save, log, helpers: dict):
        self.bridge = bridge
        self.discord = discord
        self.cfg = cfg.get(self.key, {})
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
        self.closing = False  # the bridge is shutting down
        self.taken = 0  # buffer entries the running turn took
        self.seen_ids: set[int] = set()  # Discord messages already observed (live or from catch-up)
        self.completed = False  # the last run finished its turn
        self.progress = False  # the last run got as far as working on the prompt
        self.workdir = Path(os.path.expanduser(self.cfg.get("workdir") or "."))
        self.bot_id = int(self.cfg.get("bot_id") or 0)
        self.trigger = asyncio.Event()
        self.last_msg_at = 0.0
        self.target_id: int | None = None
        self.pending = False
        self.busy = False
        self.process = None
        self.poster = None  # a REST-only client logged in as the member's bot
        self.attachments: list = []  # Discord attachments since the last turn (not persisted)
        s = self.state.setdefault(self.key, {})
        s.setdefault("buffer", [])

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and self.bot_id > 0

    @property
    def s(self) -> dict:
        return self.state[self.key]

    @property
    def prefix(self) -> str:
        """Marks the member's posts when they go out through the GPT bot."""
        return f"**[{self.name}]** "

    def t(self, key: str, **kw) -> str:
        return self.S("agent_" + key, name=self.name, **kw)

    # -- model and effort: the config's, unless the owner switched them (kept in the state)
    def current_model(self) -> str:
        return str(self.s.get("model") or self.cfg.get("model") or "")

    def current_effort(self) -> str:
        return str(self.s.get("effort") or self.cfg.get("effort") or "")

    def switch(self, what: str, value: str) -> str:
        """Set the model or effort for the next turns; 'default' returns to the config. Returns the note."""
        what, value = what.lower(), value.strip()
        if what == "effort" and value.lower() != "default":
            value = value.lower()
            if self.efforts and value not in self.efforts:
                return self.t("switch_bad", what=what, value=value, choices=", ".join(self.efforts))
        if value.lower() == "default":
            self.s.pop(what, None)
        else:
            self.s[what] = value
        self.save()
        now = self.current_model() if what == "model" else self.current_effort()
        self.log.info("%s: %s set to %s", self.key, what, now or "default")
        return self.t("switched", what=what, value=now or self.t("default_value"))

    def switch_help(self) -> str:
        return self.t("switch_help", model=self.current_model() or self.t("default_value"),
                      effort=self.current_effort() or self.t("default_value"),
                      efforts=", ".join(self.efforts) or "-", models=self.models_hint, user=self.S("user_label"),
                      premium=", ".join(sorted(self.premium())) or "-")

    @property
    def token_file(self) -> Path:
        configured = self.cfg.get("token_file")
        return Path(os.path.expanduser(configured)) if configured else self.root / f"{self.key}-bot.env"

    # -- start
    async def start(self) -> None:
        try:
            client = self.discord.Client(intents=self.discord.Intents.none())
            await client.login(read_token(self.token_file, self.token_var))
            self.poster = client
            self.log.info("%s: posting as bot %s", self.key, client.user)
        except Exception as e:
            self.poster = None
            self.log.warning("%s: bot login failed, replies go through GPT: %s", self.key, e)
        asyncio.get_running_loop().create_task(self.worker())
        if self.s.get("pending"):  # asked just before the bridge stopped: do it now
            self.log.info("%s: a request from before the restart is pending", self.key)
            self.pending, self.target_id = True, self.s.get("target")
            self.trigger.set()

    async def close(self) -> None:
        self.stopped = self.closing = True
        await self.kill()
        if self.poster:
            try:
                await self.poster.close()
            except Exception:
                pass

    # -- channel traffic
    def is_mine(self, msg) -> bool:
        return bool(self.bot_id) and msg.author.id == self.bot_id

    async def addressed(self, msg) -> bool:
        if any(u.id == self.bot_id for u in msg.mentions):
            return True
        if any(r.name.lower() == self.key or getattr(getattr(r, "tags", None), "bot_id", None) == self.bot_id
               for r in msg.role_mentions):
            return True
        if self.mention_re.search(msg.content or ""):
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
        # self.taken = how many entries the running turn took; those are deleted when it ends,
        # so a later chunk must not be appended to them.
        if buf and len(buf) > self.taken and buf[-1][0] == speaker and now - buf[-1][2] < 10:
            buf[-1][1] += "\n" + text  # one long message split into chunks
            buf[-1][2] = now
        else:
            buf.append([speaker, text, now])
        extra = len(buf) - 200
        if extra > 0:
            del buf[:extra]
            self.taken = max(0, self.taken - extra)
        self.attachments.extend(attachments)
        self.last_msg_at = time.monotonic()

    async def observe(self, msg, speaker: str, text: str, may_trigger: bool) -> None:
        """Every channel message except the member's own goes into its context."""
        if not self.enabled or self.is_mine(msg) or msg.id in self.seen_ids:
            return
        if len(self.seen_ids) > 5000:
            self.seen_ids.clear()
        self.seen_ids.add(msg.id)
        self.s["seen_msg_id"] = str(max(msg.id, int(self.s.get("seen_msg_id") or 0)))  # a restart catches up from here
        self.remember(speaker, text, msg.attachments)
        if may_trigger and await self.addressed(msg):
            if self.target_id is None or self.target_id != self.bridge.channel_id:
                self.target_id = msg.channel.id  # the main channel wins if both asked
            if msg.author.bot and self.bridge.streak >= self.bridge.cap:
                self.log.info("%s: loop cap hit, not starting", self.key)
                await self.bridge.note(self.t("loop_cap", n=self.bridge.streak))
                if not self.pending:
                    self.target_id = None  # or a later request from the other channel would be answered here
                self.save()
                return
            self.log.info("%s: requested by %s", self.key, speaker)
            self.pending = True
            # by_owner: only a turn the owner asked for may switch the member's model or effort
            self.s.update(pending=True, target=self.target_id, request=text[-2000:],
                          by_owner=msg.author.id == getattr(self.bridge, "owner_id", None))  # survives a bridge restart
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
            self.taken = len(self.s["buffer"])
            context = self.take_context()
            atts, self.attachments = self.attachments[-10:], []
            if not self.closing:
                self.stopped = False
            self.busy = True
            try:
                await self.run_turn(context, atts, target)
                del self.s["buffer"][:self.taken]  # what arrived during the turn stays for next time
            except Exception as e:
                self.attachments = atts + self.attachments  # they go out again with the next request
                if self.closing:  # the bridge is stopping mid-turn: the request stays pending for the next start
                    self.log.info("%s: turn interrupted by the bridge stopping; it runs again after the restart", self.key)
                    self.s.update(pending=True, target=target)
                    self.taken = 0
                    self.save()
                    return
                if self.stopped:  # `!name stop`: the owner already got the answer to that command
                    self.log.info("%s: turn stopped by the owner", self.key)
                else:
                    self.log.exception("%s turn failed", self.key)
                    try:
                        await self.post(target, self.t("failed", err=str(e)[:300]), plain=True)
                    except Exception:  # Discord trouble must not end this worker: nobody would run the member again
                        self.log.exception("%s: could not post the failure notice", self.key)
            finally:
                self.taken = 0
                self.busy = False
                self.process = None
                if not self.closing and not self.pending:
                    for k in ("pending", "target", "request", "by_owner"):
                        self.s.pop(k, None)
                self.save()

    def build_prompt(self, context: str, files: list[Path], target: int, fresh: bool) -> str:
        S = self.S
        parts = []
        if fresh:
            intro = self.cfg.get("intro") or self.default_intro
            parts.append(intro.format(workdir=self.workdir, outbox=self.outbox,
                                      user=S("user_label"), claude=S("claude_label")))
            parts.append(self.switch_help())
            parts.append(self.t("session_help"))
            self.s["help_version"] = HELP_VERSION
            recent = self.s.get("recent", [])
            if recent:
                parts.append(self.t("recent_header") + "\n" + "\n---\n".join(recent))
        elif self.s.get("help_version") != HELP_VERSION:  # a session that started before these rules
            parts.append(self.switch_help())
            parts.append(self.t("session_help"))
            self.s["help_version"] = HELP_VERSION
        elif re.search(self.t("model_words"), self.s.get("request") or "", re.I):
            parts.append(self.switch_help())
        parts.append(self.t("context_header") + "\n" + context)
        if files:
            parts.append(self.t("files_header") + "\n" + "\n".join(str(p) for p in files))
        where = self.t("log") if target == self.bridge.log_channel_id else self.t("main")
        parts.append(self.t("target", where=where))
        return "\n\n".join(parts)

    async def run_process(self, args: list[str], stdin: bytes) -> tuple[list[str], int, str, str | None]:
        """Run the CLI, reading its event stream line by line so a hung run is noticed: no output
        for stall_timeout_s (one long shell command is silent meanwhile, so keep it generous) or
        the whole turn over turn_timeout_s stops it. 0 turns a limit off.
        Returns (stdout lines, exit code, stderr, timeout text or None)."""
        self.process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.workdir), creationflags=NO_WINDOW, limit=16 * 1024 * 1024)
        proc = self.process
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()
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
                timed_out = (self.t("timeout", min=int(total // 60)) if over
                             else self.t("stalled", min=int(stall // 60)))
                self.log.warning("%s: %s; stopping the process", self.key, timed_out)
                await self.kill()
                break
            except ValueError:  # one event longer than the stream limit
                continue
            if not raw:
                break
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        await proc.wait()
        try:  # a grandchild that inherited the pipe could keep it open after the CLI is gone
            err = await asyncio.wait_for(err_task, 15)
        except asyncio.TimeoutError:
            err = b""
        journal = self.outbox.parent / f"{self.key}-runs"
        journal.mkdir(parents=True, exist_ok=True)
        (journal / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl").write_text("\n".join(lines), encoding="utf-8")
        return lines, proc.returncode, err.decode("utf-8", "replace").strip(), timed_out

    async def exec_cli(self, prompt: str, thread: str | None) -> tuple[str | None, list[str], str | None, str]:
        """One run of the CLI: (session id, reply messages in order, error text, stderr).
        Sets self.completed and self.progress."""
        raise NotImplementedError

    async def prepare_turn(self, context: str) -> None:
        """Hook before a turn (a subclass picks its model here)."""

    async def kill(self) -> None:
        """Stop the CLI process and everything it started (shells, test runs)."""
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
            self.log.info("%s: %s turns in the session, starting a new one", self.key, self.s.get("turns"))
            thread = None
        if self.s.pop("fresh_next", False) and thread:
            self.log.info("%s: starting a new session as it asked", self.key)
            thread = None
        if self.stopped or self.closing:  # stopped while the attachments were downloading
            raise RuntimeError("stopped")
        tries = 1 + max(0, int(self.cfg.get("retries", 1)))
        async with self.typing(target):
            await self.prepare_turn(context)
            prompt = self.build_prompt(context, files, target, fresh=not thread)
            for attempt in range(tries):
                new_thread, messages, error, stderr = await self.exec_cli(prompt, thread)
                if thread and error and not messages and RESUME_LOST_RE.search(error + stderr):
                    self.log.warning("%s: session %s cannot be resumed (%s); starting a new one", self.key, thread, error)
                    thread = None
                    prompt = self.build_prompt(context, files, target, fresh=True)
                    new_thread, messages, error, stderr = await self.exec_cli(prompt, None)
                if new_thread and new_thread != thread:
                    self.s.update(thread=new_thread, turns=0)
                    thread = new_thread
                failed = bool(error) and not (messages and self.completed)
                if not failed or self.stopped or LIMIT_RE.search(error) or attempt == tries - 1:
                    break
                # One automatic retry. If the failed run got as far as a session, that session already
                # holds the whole prompt: continue it with one line instead of sending it all again.
                self.log.warning("%s: turn failed (%s); retrying in 20 s", self.key, error[:200])
                await asyncio.sleep(20)
                if self.stopped:
                    break
                if new_thread and self.progress:
                    prompt = self.t("continue", err=error[:200])
        if error and not (messages and self.completed):
            if LIMIT_RE.search(error):
                raise RuntimeError(self.t("limit", err=error[:200]))
            raise RuntimeError(error[:300])
        self.s["turns"] = int(self.s.get("turns", 0)) + 1
        reply = messages[-1] if messages else self.t("empty")
        reply, notes = self.apply_switches(reply)
        if NEW_SESSION_RE.search(reply):  # the member asks for a fresh session from its next turn
            reply = NEW_SESSION_RE.sub("", reply).strip()
            self.s["fresh_next"] = True
            notes.append(self.t("session_next"))
            self.log.info("%s: asked for a new session from its next turn", self.key)
        self.s["recent"] = (self.s.get("recent", []) + [reply[:1200]])[-3:]
        self.save()
        await self.post(target, reply + "".join("\n\n" + n for n in notes))

    def premium(self) -> set[str]:
        """Models and effort levels (config `premium`) only the owner may ask for."""
        return {str(x).lower() for x in self.cfg.get("premium", [])}

    def apply_switches(self, reply: str) -> tuple[str, list[str]]:
        """[[model:...]] / [[effort:...]] in a reply. The member picks what fits the task itself;
        a premium model or effort only in a turn the owner asked for."""
        found = SWITCH_RE.findall(reply)
        if not found:
            return reply, []
        reply = SWITCH_RE.sub("", reply).strip()
        notes = []
        for what, value in found:
            if value.lower() in self.premium() and not self.s.get("by_owner"):
                self.log.warning("%s: ignored a switch to premium %s %s outside an owner's turn", self.key, what, value)
                notes.append(self.t("switch_refused", value=value))
            else:
                notes.append(self.switch(what, value))
        return reply, notes

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

    def link_mentions(self, text: str) -> str:
        """@Claude, @GPT and the other members' @Name become real mentions."""
        text = re.sub(r"@(?:bot)?claude\b", f"<@{self.bridge.claude_id}>", text, flags=re.I)
        if self.bridge.user:
            text = re.sub(r"@gpt\b", f"<@{self.bridge.user.id}>", text, flags=re.I)
        for other in getattr(self.bridge, "members", []):
            if other is not self and other.enabled:
                text = other.mention_re.sub(f"<@{other.bot_id}>", text)
        return re.sub(r"`\s*(<@!?\d+>)\s*`", r"\1", text)  # a mention inside backticks is not a mention

    async def post(self, channel_id: int, text: str, plain: bool = False) -> None:
        discord = self.discord
        files = []
        if not plain:
            text, files = self.outputs(text)
            text = self.link_mentions(text)
            text = self.fence_tables(text)
        text = text.replace("⚙️", "").replace("⚙", "")  # only the bridge writes control messages
        text = re.sub(r"@(everyone|here)\b", "@​\\1", text)
        limit = self.bridge.file_limit()
        big = [p for p in files if p.stat().st_size > limit]
        files = [p for p in files if p.stat().st_size <= limit]
        if big:
            text += "\n\n" + self.t("too_big", files=", ".join(str(p) for p in big))
        if self.poster:
            channel = self.poster.get_partial_messageable(channel_id)
            prefix = ""
        else:
            channel = self.bridge.get_channel(channel_id) or self.bridge.channel
            prefix = self.prefix
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        parts = self.split_message(text) or [""]
        if len(parts) > 1:
            parts = [f"{p}\n({i + 1}/{len(parts)})" for i, p in enumerate(parts)]
        parts = [prefix + p for p in parts]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last and files:
                for j in range(0, len(files), 10):
                    chunk = files[j:j + 10]
                    await channel.send(part if j == 0 else None, allowed_mentions=mentions, suppress_embeds=True,
                                       files=[discord.File(str(p), filename=p.name) for p in chunk])
            else:
                await channel.send(part, allowed_mentions=mentions, suppress_embeds=True)
        self.log.info("%s posted %d chars, %d files", self.key, len(text), len(files))

    # -- owner commands
    def status_extra(self) -> str:
        return self.t("model_line", model=self.current_model() or self.t("default_value"),
                      effort=self.current_effort() or self.t("default_value"))

    async def command(self, arg: str) -> str:
        words = arg.split()
        cmd = (words or ["status"])[0].lower()
        if cmd in ("model", "effort"):
            if len(words) < 2:
                return self.status_extra().strip()
            return self.switch(cmd, words[1])
        if cmd == "stop":
            if self.busy:
                self.stopped = True  # also ends a pending retry
                await self.kill()
                return self.t("stopped")
            return self.t("not_running")
        if cmd == "new":
            self.s.pop("thread", None)
            self.s["turns"] = 0
            self.save()
            return self.t("new")
        return self.t("status", state=self.t("running") if self.busy else self.t("idle"),
                      session=self.s.get("thread") or self.t("new_session"), turns=self.s.get("turns", 0),
                      pending=len(self.s["buffer"]),
                      poster=self.t("poster_bot") if self.poster else self.t("poster_gpt")) + self.status_extra()


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
