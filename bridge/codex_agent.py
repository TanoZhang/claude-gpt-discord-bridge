"""Codex CLI as a member of the Discord group (optional); the shared part is in cli_agent.py.

@Codex in the channel starts a Codex turn (`codex exec --json`); the Codex thread is
resumed between turns.

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
    "model": "",                      optional; default is the model in ~/.codex/config.toml
    "disable_features": [],           Codex feature flags to turn off for these runs (`codex features list`),
                                      e.g. ["apps", "plugins", "computer_use", "browser_use", "image_generation",
                                      "multi_agent", "goals", "realtime_conversation"]: a smaller fixed context
    "config_overrides": [],           extra `-c key=value` settings
    "intro": "..."                    optional; sent at the start of each Codex thread
  }
Owner commands: !codex status | !codex stop | !codex new
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from cli_agent import CliAgent

MENTION_RE = re.compile(r"@(?:bot)?codex\b", re.I)

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


class CodexAgent(CliAgent):
    key = "codex"
    name = "Codex"
    token_var = "CODEX_DISCORD_BOT_TOKEN"
    mention_re = MENTION_RE
    default_intro = DEFAULT_INTRO
    models_hint = "Codex model names (the default is `model` in ~/.codex/config.toml)"

    def is_codex(self, msg) -> bool:
        return self.is_mine(msg)

    def command_args(self, thread: str | None) -> list[str]:
        sandbox = self.cfg.get("sandbox", "workspace-write")
        args = [find_codex(self.cfg.get("executable")), "-a", "never", "-s", sandbox, "-C", str(self.workdir)]
        if self.current_model():
            args += ["-m", self.current_model()]
        if self.current_effort():
            args += ["-c", f'model_reasoning_effort="{self.current_effort()}"']
        # Each feature brings tool descriptions that are sent with every model call. A group
        # member that edits code and runs commands needs few of them: turning the rest off
        # cut the fixed context by about a third (codex-cli 0.167).
        for feature in self.cfg.get("disable_features", []):
            args += ["--disable", str(feature)]
        for override in self.cfg.get("config_overrides", []):
            args += ["-c", str(override)]
        if sandbox == "workspace-write" and self.cfg.get("network", True):
            args += ["-c", "sandbox_workspace_write.network_access=true"]
        args += ["exec"]
        if thread:
            args += ["resume", "--json", "--skip-git-repo-check", thread, "-"]
        else:
            args += ["--json", "--skip-git-repo-check", "-"]
        return args

    async def exec_cli(self, prompt: str, thread: str | None) -> tuple[str | None, list[str], str | None, str]:
        lines, code, stderr, timed_out = await self.run_process(self.command_args(thread), prompt.encode("utf-8"))
        self.completed = any('"type":"turn.completed"' in line.replace(" ", "") for line in lines)
        self.progress = any('"type":"item.' in line.replace(" ", "") for line in lines)
        new_thread, messages, error = parse_events(lines)
        if timed_out:
            error = timed_out
        elif code and not error:
            error = (stderr.splitlines() or [f"exit code {code}"])[-1]
        elif not self.completed and not error:
            error = "the run ended before Codex finished its turn"
        return new_thread, messages, error, stderr
