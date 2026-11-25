"""Gemini (Antigravity CLI, `agy`) as a member of the Discord group (optional); the shared
part is in cli_agent.py.

@Gemini in the channel starts a turn: one headless `agy` run that resumes the member's
conversation. Before the turn a small, cheap run picks the model for the task (see pick_model).

config.json:
  "gemini": {
    "enabled": true,
    "workdir": "C:\\\\path\\\\to\\\\your\\\\project",
    "bot_id": "...",                   Gemini bot user id (native @Gemini mention)
    "token_file": "gemini-bot.env",    GEMINI_DISCORD_BOT_TOKEN=... (default: gemini-bot.env in the repository root)
    "executable": "",                  optional; found automatically (%LOCALAPPDATA%\\agy\\bin)
    "permissions": "auto",             "skip" = --dangerously-skip-permissions (everything allowed);
                                       otherwise the value of --mode: "auto", "accept-edits" or "plan"
    "model": "",                       a fixed model slug; empty = pick one per turn:
    "models": {"easy": "...", "medium": "...", "hard": "..."},   slugs from `agy models`
    "router_model": "",                the model that rates the request (default: models.easy)
    "router_fallback": "medium",       the tier used when rating fails
    "effort": "",                      optional --effort for the turns (low|medium|high|xhigh|max)
    "context_max_chars": 20000,
    "rotate_turns": 40,                start a new conversation after this many turns
    "stall_timeout_s": 1800,           stop a run that produced no event for this long (0 = never)
    "turn_timeout_s": 10800,           stop a run that takes longer than this in total (0 = never)
    "retries": 1,                      automatic retries of a failed run (not for usage limits)
    "extra_args": [],                  more `agy` arguments
    "intro": "..."                     optional; sent at the start of each conversation
  }
A request can name the model itself: "@Gemini carbon ..." (any word of a configured slug such
as hydrogen, argon, neon or carbon, or a tier name easy / medium / hard).
Owner commands: !gemini status | !gemini stop | !gemini new
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from cli_agent import CliAgent

MENTION_RE = re.compile(r"@(?:bot)?gemini\b", re.I)
TIERS = ("easy", "medium", "hard")

DEFAULT_INTRO = (
    "[Group chat] You are Gemini (Antigravity CLI), a member of a Discord group working on this project "
    "together with {user}, {claude}, GPT and Codex. Your working directory is {workdir}. "
    "I relay the channel messages since your last reply, each with its speaker in front, "
    "like [{user}], [{claude}], [GPT] or [Codex]; a tag such as \"· #channel-name\" marks a message from that other channel. "
    "You are only called when someone writes @Gemini: the task is the @Gemini request among the last messages, "
    "the earlier messages are background. The first time, read CLAUDE.md, AGENTS.md, GEMINI.md and README.md in the "
    "working directory (whichever exist) to learn the project.\n"
    "Your final reply is posted to the channel as is: short, with the result, which files you changed, "
    "how you verified it, and what needs whose decision. Your reply goes to the channel where you were asked; "
    "if there is a separate log channel, keep technical detail there and give only conclusions in the main channel. "
    "Write @Claude, @GPT or @Codex in your reply only when you need them to take over or review something, and say "
    "clearly what you need from them; otherwise leave them out so the group does not ping-pong. "
    "To send files or images, save them in {outbox} and write [[discord-file:FULL_PATH]] in your reply, up to 10. "
    "Do not push, publish, or change Discord, credential or permission settings unless the owner explicitly asks."
)

ROUTER_PROMPT = (
    "Rate how hard this request to a coding agent is. Do not use any tools and do not do the task. "
    "Answer with exactly one word:\n"
    "easy = a question, a lookup, a small edit or a short explanation;\n"
    "medium = a normal feature, a bug fix or a review of a few files;\n"
    "hard = a large or long task, a refactor across files, a difficult bug, or careful design work.\n\n"
    "[Recent messages]\n{context}\n\n[The request]\n{request}"
)


def find_agy(configured: str | None = None) -> str:
    if configured and Path(configured).is_file():
        return str(configured)
    found = shutil.which("agy.exe") or shutil.which("agy")
    if found:
        return found
    # A hidden service does not inherit the desktop app's PATH.
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "agy/bin"
    for name in ("agy.exe", "agy.cmd", "agy"):
        if (root / name).is_file():
            return str(root / name)
    raise RuntimeError("Antigravity CLI (agy) not found")


def parse_events(lines: list[str]) -> dict:
    """The result of a headless run, from `--output-format stream-json` events or the single
    `json` envelope: {conversation, status, response, error, usage, progress}."""
    out = {"conversation": None, "status": None, "response": "", "error": None, "usage": {}, "progress": False}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("event")
        if kind == "step_update":
            out["progress"] = True
            continue
        if kind == "init":
            out["conversation"] = event.get("conversation_id") or out["conversation"]
            continue
        result = event.get("result") if kind == "result" else event if "status" in event else None
        if not isinstance(result, dict):
            continue
        out["conversation"] = result.get("conversation_id") or out["conversation"]
        out["status"] = result.get("status")
        out["response"] = (result.get("response") or "").strip()
        out["usage"] = result.get("usage") or {}
        err = result.get("error")
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)
        out["error"] = err or None
    return out


class GeminiAgent(CliAgent):
    key = "gemini"
    name = "Gemini"
    token_var = "GEMINI_DISCORD_BOT_TOKEN"
    mention_re = MENTION_RE
    default_intro = DEFAULT_INTRO
    efforts = ("low", "medium", "high", "xhigh", "max")
    models_hint = "the slugs `agy models` lists"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.turn_model = ""
        self.turn_tier = ""
        self.session_tokens = 0

    # -- model choice
    def models(self) -> dict:
        return {k: v for k, v in (self.cfg.get("models") or {}).items() if v}

    def named_model(self, request: str) -> tuple[str, str] | None:
        """A model the request names right after the mention: "@Gemini carbon ...", "@Gemini hard: ..."."""
        m = MENTION_RE.search(request)
        head = request[m.end():] if m else request
        words = re.findall(r"[a-z][a-z0-9.-]*", head[:40].lower())[:2]
        models = self.models()
        for word in words:
            if word in models:
                return models[word], word
            for tier, slug in models.items():
                if word in re.split(r"[-_.]", slug.lower()):
                    return slug, tier
        return None

    async def pick_model(self, context: str) -> tuple[str, str]:
        """(model slug, why). A fixed `model` wins, then a model named in the request, then a
        rating of the request by the router model; '' means the CLI's default."""
        if self.current_model():
            return self.current_model(), "fixed"
        models = self.models()
        if not models:
            return "", "default"
        request = self.s.get("request") or context[-2000:]
        named = self.named_model(request)
        if named:
            return named[0], f"named {named[1]}"
        router = self.cfg.get("router_model") or models.get("easy") or next(iter(models.values()))
        prompt = ROUTER_PROMPT.format(context=context[-3000:], request=request[-2000:])
        # The default permission mode: headless, anything that needs approval is refused, so the
        # rating cannot change files. (Plan mode is no good here: it writes a plan instead of answering.)
        args = self.base_args(router)
        fallback = self.cfg.get("router_fallback", "medium")
        try:
            lines, code, stderr, timed_out = await self.run_process(args + self.prompt_args(prompt), self.prompt_stdin(prompt))
        except Exception as e:
            self.log.warning("gemini: rating the request failed (%s); using %s", e, fallback)
            lines, code, stderr, timed_out = [], 1, str(e), None
        res = parse_events(lines)
        self.count_usage(res["usage"], router, "router")
        words = re.findall(r"\b(easy|medium|hard)\b", res["response"].lower())
        tier = words[0] if words and not timed_out else fallback
        if not words:
            self.log.warning("gemini: no rating from %s (%s); using %s", router,
                             (res["error"] or stderr or res["response"] or f"exit {code}")[:200], fallback)
        for t in (tier, *TIERS[TIERS.index(tier) if tier in TIERS else 0:], *TIERS):
            if models.get(t):
                return models[t], t
        return "", "default"

    async def prepare_turn(self, context: str) -> None:
        self.turn_model, self.turn_tier = await self.pick_model(context)
        self.log.info("gemini: model %s (%s)", self.turn_model or "default", self.turn_tier)

    # -- the CLI
    def base_args(self, model: str) -> list[str]:
        args = [find_agy(self.cfg.get("executable")), "--output-format", "stream-json"]
        if model:
            args += ["--model", model]
        return args

    def prompt_args(self, prompt: str) -> list[str]:
        """The prompt goes in on stdin (stream-json); a long prompt does not fit a command line.
        This alone makes a headless run (agy 1.5.17): -p would take the next argument as the prompt."""
        return ["--input-format", "stream-json"]

    def prompt_stdin(self, prompt: str) -> bytes:
        return (json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n").encode("utf-8")

    def command_args(self, prompt: str, thread: str | None) -> list[str]:
        args = self.base_args(self.turn_model)
        if self.current_effort():
            args += ["--effort", self.current_effort()]
        perm = self.cfg.get("permissions", "auto")
        args += ["--dangerously-skip-permissions"] if perm == "skip" else ["--mode", str(perm)]
        if thread:
            args += ["--conversation", thread]
        args += [str(a) for a in self.cfg.get("extra_args", [])]
        return args + self.prompt_args(prompt)

    def count_usage(self, usage: dict, model: str, what: str) -> int:
        n = int(usage.get("total_tokens") or sum(int(usage.get(k) or 0) for k in
                                                ("input_tokens", "output_tokens", "thinking_tokens")))
        if n:
            self.session_tokens += n
            self.s["tokens_total"] = int(self.s.get("tokens_total", 0)) + n
            self.log.info("gemini: %s used %d tokens on %s (in %s, out %s, thinking %s, cached %s)", what, n,
                          model or "default", usage.get("input_tokens"), usage.get("output_tokens"),
                          usage.get("thinking_tokens"), usage.get("cache_read_tokens"))
        return n

    async def exec_cli(self, prompt: str, thread: str | None) -> tuple[str | None, list[str], str | None, str]:
        lines, code, stderr, timed_out = await self.run_process(self.command_args(prompt, thread), self.prompt_stdin(prompt))
        res = parse_events(lines)
        self.completed = res["status"] == "SUCCESS"
        self.progress = res["progress"] or bool(res["response"])
        n = self.count_usage(res["usage"], self.turn_model, "turn")
        self.s.update(last_model=self.turn_model or "default", last_tier=self.turn_tier, last_tokens=n)
        error = res["error"]
        if thread and res["conversation"] and res["conversation"] != thread and not timed_out:
            # agy 1.5.17 does not fail on a conversation it cannot find: it warns on stderr and
            # starts a new one, which then lacks the intro. Report it lost so the turn is run
            # again in a new conversation with the full prompt.
            self.log.warning("gemini: asked for conversation %s, got %s", thread, res["conversation"])
            return None, [], f"conversation {thread} not found", stderr
        if timed_out:
            error = timed_out
        elif not self.completed and not error:
            error = (res["status"] and f"status {res['status']}") or (stderr.splitlines() or [f"exit code {code}"])[-1]
        return res["conversation"], [res["response"]] if res["response"] else [], error, stderr

    def status_extra(self) -> str:
        if not self.s.get("last_model"):
            return super().status_extra()
        return super().status_extra() + self.t("usage", model=self.s["last_model"], tier=self.s.get("last_tier", ""),
                      last=self.s.get("last_tokens", 0), total=self.session_tokens)
