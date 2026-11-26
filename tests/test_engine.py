"""Offline tests for the parts of the bridge where messages used to get lost.

No Discord connection, no browser, no real Codex or agy: the Discord client is constructed but never
logged in, and Codex and agy are replaced by tests/fake_codex.py and tests/fake_agy.py. Run with the bridge's Python:

    python tests/test_engine.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
import gpt_bridge as gb  # noqa: E402
import codex_agent as ca  # noqa: E402
import cli_agent as cli  # noqa: E402
import gemini_agent as ga  # noqa: E402

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def make_bridge(tmp: Path, **cfg_extra):
    gb.STATE_FILE = tmp / "state.json"
    gb.OUTBOX_DIR = tmp / "outbox"
    gb.REPORTS_DIR = tmp / "reports"
    cfg = {"channel_id": "1", "log_channel_id": "2", "owner_id": "10", "claude_bot_id": "20",
           "context_max_chars": 200, "selectors": dict(gb.DEFAULT_SELECTORS), "quiet_period_s": 0,
           "reply_timeout_s": 5, "loop_cap": 4, "browser_mode": "headless", "browser_channel": "chrome",
           "chat_url": "https://chatgpt.com/c/aaaaaaaa-0000-0000-0000-000000000001"}
    cfg.update(cfg_extra)
    return gb.make_bridge(cfg, {})


# ------------------------------------------------------------------ GPT buffer

async def test_buffer(tmp: Path) -> None:
    b = make_bridge(tmp)
    check("buffer: a message is remembered once", b.remember("User", "hello", (), 101) and not b.remember("User", "hello", (), 101))
    check("buffer: one entry after a duplicate", len(b.buffer) == 1)
    b.remember("Claude", "x" * 150, (), 102)
    text, atts, newest = b.take_buffer()
    check("buffer: newest message id is returned", newest == 102, str(newest))
    check("buffer: emptied by take_buffer", not b.buffer)

    # A batch that failed is put back; new chatter must not push it out of the size limit.
    b.requeue("[User] @GPT the original request", [], 103)
    for i in range(5):
        b.buffer.append([f"P{i}", "y" * 120, time.monotonic() - 60, [], 200 + i])
    text, atts, newest = b.take_buffer()
    check("buffer: a requeued request survives truncation", "the original request" in text)
    check("buffer: older chatter is marked as omitted", gb.S("omitted") in text)
    check("buffer: requeued batch keeps its message id", newest == 204, str(newest))

    # Catch-up adds old messages after live ones: order is restored by id.
    b.remember("A", "new", (), 500)
    b.buffer.append(["B", "old", time.monotonic(), [], 400])
    b.buffer.sort(key=lambda e: e[4])
    text, _, _ = b.take_buffer()
    check("buffer: catch-up order is by message id", text.index("old") < text.index("new"))
    await b.close()


async def test_halt(tmp: Path) -> None:
    b = make_bridge(tmp)
    b.halted = {"since": time.time()}
    b.remember("User", "@GPT hi", (), 1)
    b.request_gpt()
    check("halt: a request while paused is held", b.gpt_pending and not b.trigger.is_set())
    b.gpt_pending = False
    await b.retry_later(0)
    check("halt: a retry while paused does not send", b.gpt_pending and not b.trigger.is_set())
    await b.close()


class FakeSent:
    def __init__(self, n):
        self.id = n
        self.channel = None


class FakeChannel:
    def __init__(self, fail_times: int = 0):
        self.id, self.sent, self.fail_times, self.name = 1, [], fail_times, "main"

    async def send(self, content=None, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("discord is down")
        self.sent.append(content)
        return FakeSent(1000 + len(self.sent))

    def typing(self):
        return cli._Null()


async def test_web_turns(tmp: Path) -> None:
    b = make_bridge(tmp)
    b.channel = FakeChannel(fail_times=1)
    b.cw_wake = False
    found = {"key": "k3", "items": [{"key": "k1", "user": "q1", "text": "a1", "outputs": []},
                                    {"key": "k2", "user": "q2", "text": "a2", "outputs": []}]}
    b.state["last_turn_key"] = "k0"
    try:
        await b.post_web_turns(found)
        check("web turns: a failed post raises", False)
    except RuntimeError:
        check("web turns: position is not moved past a turn that was not posted", b.state["last_turn_key"] == "k0",
              b.state["last_turn_key"])
    await b.post_web_turns(found)  # the next look finds the same turns again
    check("web turns: both turns posted on the next look", len(b.channel.sent) == 2, str(len(b.channel.sent)))
    check("web turns: position ends at the newest turn", b.state["last_turn_key"] == "k3")
    b.state["last_turn_key"] = "k0"  # the page showed an older turn last, so the same turns look new again
    await b.post_web_turns(found)
    check("web turns: a posted turn is never posted again", len(b.channel.sent) == 2, str(len(b.channel.sent)))
    await b.close()


async def test_mirror_rules(tmp: Path) -> None:
    """A web turn is posted once, only when it is new since the last look, and never empty."""
    T = lambda k, text="a", user="q", state="complete": {"key": k, "state": state, "userText": user, "text": text}
    new, late, first = gb.pick_web_turns([T("a"), T("b"), T("c")], set())
    check("mirror: a chat with no turn seen before is all history", first and not new and not late)
    new, late, first = gb.pick_web_turns([T("a"), T("b"), T("c")], {"b"})
    check("mirror: only turns after the newest seen one are news", [t["key"] for t in new] == ["c"] and late == ["a"] and not first)
    new, late, first = gb.pick_web_turns([T("a"), T("b")], {"a", "b", "zz"})
    check("mirror: a page that ends at a seen turn has no news", not new and not late and not first)
    new, late, first = gb.pick_web_turns([T("a"), T("p"), T("b")], {"a", "b"}, {"p"})
    check("mirror: a turn that was waiting stays news after the bridge's own turn", [t["key"] for t in new] == ["p"] and not late)

    b = make_bridge(tmp, mirror_interval_s=60)
    g = b.gpt
    g.page = FakePage(url=g.chat_url or "https://chatgpt.com/c/aaaaaaaa-0000-0000-0000-000000000001")
    page = {"turns": [], "last": {}, "copied": None, "outputs": []}

    async def fake_open_chat(url=None):
        g.last_used = g.loaded_at = time.monotonic()

    async def fake_last_turn():
        t = page["turns"][-1]
        return {"key": t["key"], "state": t["state"], "count": len(page["turns"]), "userText": t["userText"],
                "text": t["text"], "fallback": "", **page["last"]}

    async def fake_turns():
        return [dict(t) for t in page["turns"]]

    async def fake_copied():
        return page["copied"]

    async def fake_outputs():
        return list(page["outputs"])

    async def fast_sleep(s, _real=asyncio.sleep):
        await _real(0)

    g.open_chat, g.last_turn, g.page_turns = fake_open_chat, fake_last_turn, fake_turns
    g.copied_reply, g.outputs_of_last_turn = fake_copied, fake_outputs
    g.alive = lambda: True
    b.channel = FakeChannel()
    b.cw_wake = False
    real_sleep, gb.asyncio.sleep = gb.asyncio.sleep, fast_sleep
    try:
        async def look(name="periodic"):
            found = await g.check_for_web_turns(b.mirrored())
            if found:
                await b.post_web_turns(found, name)
            return found

        # First look at a chat: history is recorded, nothing is posted.
        b.state.pop("mirrored_keys", None)
        b.state["last_turn_key"] = ""
        page["turns"] = [T("h1"), T("h2"), T("h3")]
        await look()
        check("mirror: the first look at a chat posts nothing", len(b.channel.sent) == 0, str(b.channel.sent))
        check("mirror: the first look records every turn", {"h1", "h2", "h3"} <= b.mirrored(), str(b.mirrored()))
        await look()
        check("mirror: the second look posts nothing either", len(b.channel.sent) == 0)

        # The saved position is not on the page (it showed fewer turns): the last turn is not posted.
        b.state["last_turn_key"] = "gone"
        page["turns"] = [T("h1"), T("h2")]
        await look()
        check("mirror: a lost position does not post the last turn", len(b.channel.sent) == 0)

        # An old turn the page had not shown before appears in the middle: history.
        page["turns"] = [T("h1"), T("old"), T("h2"), T("h3")]
        await look()
        await look()
        check("mirror: an old turn that appears late is not posted", len(b.channel.sent) == 0 and "old" in b.mirrored())

        # A new turn is posted once, also when the page later ends at an older turn and comes back.
        page["turns"] = [T("h1"), T("old"), T("h2"), T("h3"), T("n1", "answer one", "question one")]
        await look()
        check("mirror: a new turn is posted", len(b.channel.sent) == 1 and "answer one" in b.channel.sent[0])
        page["turns"] = page["turns"][:-1]
        await look()
        page["turns"].append(T("n1", "answer one", "question one"))
        await look()
        check("mirror: a posted turn is not posted again", len(b.channel.sent) == 1, str(len(b.channel.sent)))

        # A turn the bridge sent is recorded, not posted.
        page["turns"].append(T("own", "reply", "[Tano] hello"))
        await look()
        check("mirror: the bridge's own turn is not posted", len(b.channel.sent) == 1 and "own" in b.mirrored())

        # A finished last turn without a reply waits, then is given up without a post.
        page["turns"].append(T("blank", "", "what now"))
        for _ in range(gb.WEB_BLANK_LOOKS - 1):
            await look()
        check("mirror: an empty reply is not posted and not recorded while it may load",
              len(b.channel.sent) == 1 and "blank" not in b.mirrored())
        page["turns"][-1]["text"] = "late answer"
        await look()
        check("mirror: the reply is posted once it is there", len(b.channel.sent) == 2 and "late answer" in b.channel.sent[1])

        page["turns"].append(T("blank2", "", "and now"))
        for _ in range(gb.WEB_BLANK_LOOKS):
            await look()
        check("mirror: a reply that stays empty is given up without a post",
              len(b.channel.sent) == 2 and "blank2" in b.mirrored(), str(len(b.channel.sent)))

        # A turn that was waiting is still posted after the bridge's own turn came after it.
        page["turns"].append(T("wait", "", "slow one"))
        await look()
        page["turns"][-1]["text"] = "slow answer"
        page["turns"].append(T("own2", "reply", "[Tano] next"))
        b.mark_seen(["own2"])
        await look()
        check("mirror: a waiting turn is posted after the bridge's own turn", len(b.channel.sent) == 3
              and "slow answer" in b.channel.sent[2], str(len(b.channel.sent)))

        # No Copy button on the last turn: it may be unfinished; wait, then post what is there.
        page["last"] = {"copy": 0}
        page["turns"].append(T("part", "half an answer", "long one"))
        await look()
        check("mirror: a reply without its Copy button waits", len(b.channel.sent) == 3)
        page["last"] = {"copy": 1}
        page["copied"] = "the whole answer"
        await look()
        check("mirror: the finished reply is posted whole", len(b.channel.sent) == 4 and "the whole answer" in b.channel.sent[3])
        page["copied"] = None

        # A turn still being answered is left alone.
        page["turns"].append(T("busy", "so far", "thinking", state="streaming"))
        await look()
        check("mirror: a turn still being answered is not posted", len(b.channel.sent) == 4 and "busy" not in b.mirrored())
        page["turns"][-1].update(state="complete", text="done now")
        await look()
        check("mirror: it is posted when finished", len(b.channel.sent) == 5 and "done now" in b.channel.sent[4])

        # Another chat, never looked at: history only; a turn typed there afterwards is news.
        b.switch_chat("https://chatgpt.com/c/bbbbbbbb-0000-0000-0000-000000000002")
        g.page = FakePage(url=g.chat_url)
        page["turns"] = [T("x1"), T("x2"), T("x3")]
        await look()
        check("mirror: a chat followed for the first time posts none of its turns", len(b.channel.sent) == 5, str(len(b.channel.sent)))
        page["turns"].append(T("x4", "fresh", "new question"))
        await look()
        check("mirror: a turn typed there afterwards is posted", len(b.channel.sent) == 6 and "fresh" in b.channel.sent[5])

        # post_web_turns itself never posts an empty reply.
        await b.post_web_turns({"key": "e1", "items": [{"key": "e1", "user": "q", "text": " ", "outputs": []}], "seen": ["s9"]})
        check("mirror: an empty item is never posted", len(b.channel.sent) == 6 and {"e1", "s9"} <= b.mirrored())
    finally:
        gb.asyncio.sleep = real_sleep
    await b.close()


async def test_reports(tmp: Path) -> None:
    b = make_bridge(tmp)
    b.cw_wake = False
    (gb.REPORTS_DIR / "sent").mkdir(parents=True)
    old = time.time() - 60
    for name in ("a.md", "b.md"):
        f = gb.REPORTS_DIR / name
        f.write_text(f"# report {name}\nbody", encoding="utf-8")
        os.utime(f, (old, old))
    ch = FakeChannel(fail_times=1)
    b.log_channel = ch
    b.channel = ch
    await b.post_reports()
    check("reports: a failing report does not block the next one", len(ch.sent) == 1 and "b.md" in ch.sent[0],
          str(ch.sent))
    check("reports: the failed report stays queued", (gb.REPORTS_DIR / "a.md").exists())
    await b.post_reports()
    check("reports: the failed report goes out on the next round", len(ch.sent) == 2)
    check("reports: nothing left in the queue", not list(gb.REPORTS_DIR.glob("*.md")) and not list(gb.REPORTS_DIR.glob("*.sending")))
    await b.close()


# ------------------------------------------------------------------ Codex

# ------------------------------------------------------------------ page loads and rate limits

class FakeLocator:
    first = None

    def __init__(self):
        self.first = self

    async def is_visible(self):
        return False


class FakePage:
    def __init__(self, url="https://chatgpt.com/c/aaaaaaaa-0000-0000-0000-000000000001", title="ChatGPT"):
        self.url, self._title = url, title

    def locator(self, sel):
        return FakeLocator()

    async def title(self):
        return self._title

    async def evaluate(self, *a):
        return ""

    def is_closed(self):
        return False


async def test_page_loads(tmp: Path) -> None:
    b = make_bridge(tmp, mirror_interval_s=60)
    g = b.gpt

    # No input box and no login page: not the user's job.
    g.page = FakePage()
    try:
        await g.wait_composer(timeout_s=0.2)
        check("page: no input box raises", False)
    except gb.PageNotLoaded:
        check("page: a page without input box and without login is PageNotLoaded", True)
    except gb.NeedsHuman:
        check("page: a page without input box and without login is PageNotLoaded", False, "NeedsHuman")
    g.page = FakePage(url="https://auth.openai.com/log-in")
    try:
        await g.wait_composer(timeout_s=0.2)
        check("page: a login page raises", False)
    except gb.NeedsHuman:
        check("page: a login page still needs the user", True)
    except gb.PageNotLoaded:
        check("page: a login page still needs the user", False, "PageNotLoaded")

    # A look that has to start the browser loads the chat once, not twice.
    loads = []
    g.page = None

    async def fake_open_chat(url=None):
        loads.append(url)
        g.last_used = g.loaded_at = time.monotonic()

    async def fake_start(mode=None):
        g.page = FakePage()
        await fake_open_chat()

    async def fake_last_turn():
        return {"key": "k1", "state": g.sel["turn_done"], "count": 1, "userText": "", "text": ""}

    async def fake_page_turns():
        return [{"key": "k1", "state": g.sel["turn_done"], "userText": "", "text": ""}]

    g.open_chat, g.start, g.last_turn, g.page_turns = fake_open_chat, fake_start, fake_last_turn, fake_page_turns
    await g.check_for_web_turns({"k1"})
    check("page: a look that starts the browser loads the chat once", len(loads) == 1, str(len(loads)))
    g.loaded_at -= 100
    await g.check_for_web_turns({"k1"})
    check("page: a look on an open, old page reloads it", len(loads) == 2, str(len(loads)))

    # The worker keeps the batch and tries again by itself; the user is not asked.
    b.channel = FakeChannel()
    delays = []

    async def no_wait():
        return None

    async def fake_retry(delay):
        delays.append(delay)

    async def not_loaded(*a, **kw):
        raise gb.PageNotLoaded(gb.S("problem_no_composer"))

    b.wait_until_ready, b.retry_later, g.check_for_web_turns = no_wait, fake_retry, not_loaded
    worker = asyncio.create_task(b.worker())
    for n in range(gb.PAGE_FAILS_NOTE):
        if n == 0:
            b.remember("User", "@GPT hello", (), 900)
        b.trigger.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(delays) > n:
                break
        if n == 0:
            check("page: nothing is posted for the first failed load", not b.channel.sent, str(b.channel.sent))
    worker.cancel()
    check("page: the batch is kept", any("hello" in e[1] for e in b.buffer))
    check("page: every failed load schedules a retry", len(delays) == gb.PAGE_FAILS_NOTE, str(delays))
    check("page: retries come later each time", delays == sorted(delays) and delays[0] < delays[-1], str(delays))
    ask = gb.S("needs_human_ask", problem=gb.S("problem_no_composer"))
    check("page: the user is not asked to open the browser", ask not in b.channel.sent, str(b.channel.sent))
    check("page: one note says the bridge is retrying", len(b.channel.sent) == 1, str(b.channel.sent))
    g.throttled_until = time.time() + 200
    check("page: a retry waits past the rate limit window", b.page_retry_delay(1) > 200, str(b.page_retry_delay(1)))
    await b.close()


class FakeMsg:
    def __init__(self, mid, author_id=10, bot=False, channel_id=1, content="@Codex do it"):
        self.id, self.content, self.attachments, self.mentions, self.role_mentions = mid, content, [], [], []
        self.author = type("A", (), {"id": author_id, "bot": bot, "display_name": "x"})()
        self.channel = type("C", (), {"id": channel_id})()
        self.reference = None


class FakeBridge:
    channel_id, log_channel_id, halted, streak, cap, claude_id, user, owner_id = 1, 2, None, 0, 4, 20, None, 10

    def __init__(self):
        self.channel = FakeChannel()
        self.notes = []

    def get_channel(self, _):
        return self.channel

    def file_limit(self):
        return 10 ** 7

    async def note(self, text, log_channel=False):
        self.notes.append(text)

    async def download(self, atts):
        return [], []


def make_codex(tmp: Path, scenario: str, **extra):
    fake = tmp / "codex.cmd"
    fake.write_text(f'@echo off\r\n"{sys.executable}" "{ROOT / "tests" / "fake_codex.py"}" "{tmp}" {scenario} %*\r\n', encoding="ascii")
    cfg = {"quiet_period_s": 0, "codex": {"enabled": True, "bot_id": "30", "workdir": str(tmp), "executable": str(fake),
                                          "retries": 1, **extra}}
    state = {}
    helpers = {"split_message": gb.split_message, "fence_tables": gb.fence_tables, "outbox": tmp / "outbox",
               "root": tmp, "S": gb.S}
    agent = ca.CodexAgent(FakeBridge(), None, cfg, state, lambda: None, logging.getLogger("test"), helpers)
    agent.discord = type("D", (), {"AllowedMentions": lambda **kw: None, "File": None})
    return agent


async def run_one_turn(agent, timeout=60):
    task = asyncio.create_task(agent.worker())
    end = time.time() + timeout
    await asyncio.sleep(0.2)
    while (agent.busy or agent.trigger.is_set()) and time.time() < end:
        await asyncio.sleep(0.1)
    task.cancel()


async def test_codex_buffer(tmp: Path) -> None:
    agent = make_codex(tmp, "ok")
    agent.remember("User", "@Codex fix X")
    agent.taken = len(agent.s["buffer"])  # a turn has taken the buffer
    agent.remember("User", "and also Y")  # same speaker within 10 s
    check("codex: a line said during a turn is not merged into the taken entry", len(agent.s["buffer"]) == 2)
    del agent.s["buffer"][:agent.taken]
    check("codex: it is still there after the turn", agent.s["buffer"] and agent.s["buffer"][0][1] == "and also Y")

    agent.s["buffer"][:] = [["P", str(i), 0] for i in range(200)]
    agent.taken = 200
    agent.remember("Q", "new during turn")
    del agent.s["buffer"][:agent.taken]
    check("codex: a full buffer does not drop what arrives during a turn",
          [e[1] for e in agent.s["buffer"]] == ["new during turn"], str(len(agent.s["buffer"])))

    agent.taken = 0
    agent.s["buffer"].clear()
    m = FakeMsg(900)
    await agent.observe(m, "User", "@Codex do it", may_trigger=False)
    await agent.observe(m, "User", "@Codex do it", may_trigger=False)
    check("codex: the same Discord message is observed once", len(agent.s["buffer"]) == 1)
    check("codex: last seen message id is kept for catch-up", agent.s.get("seen_msg_id") == "900")


async def test_codex_runs(tmp: Path) -> None:
    # 1. normal run
    d = tmp / "ok"; d.mkdir()
    agent = make_codex(d, "ok")
    agent.remember("User", "@Codex do it")
    agent.pending = True
    agent.trigger.set()
    await run_one_turn(agent)
    check("codex: a normal run posts the final message", agent.bridge.channel.sent[-1:] == ["**[Codex]** final answer"],
          str(agent.bridge.channel.sent))
    check("codex: the buffer is cleared after a good turn", not agent.s["buffer"])

    # 2. first run dies after an interim message, without turn.completed: retried, continuing the thread
    d = tmp / "cut"; d.mkdir()
    agent = make_codex(d, "cut_then_ok")
    agent.remember("User", "@Codex do it")
    agent.pending = True
    agent.trigger.set()
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s, *a: real_sleep(min(s, 0.05))  # do not wait 20 s for the retry
    try:
        await run_one_turn(agent)
    finally:
        asyncio.sleep = real_sleep
    sent = agent.bridge.channel.sent
    check("codex: an interrupted run is not posted as the answer", "**[Codex]** working on it" not in sent, str(sent))
    check("codex: the retry's answer is posted", sent[-1:] == ["**[Codex]** final answer"], str(sent))
    calls = json.loads((d / "calls.json").read_text())
    check("codex: the retry resumes the thread", len(calls) == 2 and "resume" in calls[1]["args"], str(calls))
    check("codex: the retry sends one line, not the whole context", len(calls[1]["stdin"]) < 400 and "do it" not in calls[1]["stdin"],
          calls[1]["stdin"][:80] if len(calls) > 1 else "")

    # 3. always failing: the request stays in the buffer, and a failing notice does not end the worker
    d = tmp / "fail"; d.mkdir()
    agent = make_codex(d, "fail", retries=0)
    agent.bridge.channel.fail_times = 5
    agent.remember("User", "@Codex do it")
    agent.pending = True
    agent.trigger.set()
    task = asyncio.create_task(agent.worker())
    await real_sleep(0.3)
    end = time.time() + 30
    while agent.busy and time.time() < end:
        await real_sleep(0.1)
    await real_sleep(0.2)
    check("codex: the worker survives a failed failure notice", not task.done())
    check("codex: a failed turn keeps the messages", len(agent.s["buffer"]) == 1)
    task.cancel()

    # 4. no output at all: stopped by the stall timeout
    d = tmp / "hang"; d.mkdir()
    agent = make_codex(d, "hang", retries=0, stall_timeout_s=3)
    agent.remember("User", "@Codex do it")
    agent.pending = True
    agent.trigger.set()
    t0 = time.time()
    await run_one_turn(agent, timeout=40)
    check("codex: a silent run is stopped by the stall timeout", time.time() - t0 < 30 and not agent.busy,
          f"{time.time() - t0:.0f}s busy={agent.busy}")
    check("codex: the stall is reported", any("Codex" in (x or "") for x in agent.bridge.channel.sent), str(agent.bridge.channel.sent))


# ------------------------------------------------------------------ Gemini (Antigravity CLI)

MODELS = {"easy": "gemini-4-hydrogen-high", "medium": "gemini-4-neon-high", "hard": "gemini-4-carbon-high"}


def make_gemini(tmp: Path, scenario: str, **extra):
    fake = tmp / "agy.cmd"
    fake.write_text(f'@echo off\r\n"{sys.executable}" "{ROOT / "tests" / "fake_agy.py"}" "{tmp}" {scenario} %*\r\n', encoding="ascii")
    cfg = {"quiet_period_s": 0, "gemini": {"enabled": True, "bot_id": "40", "workdir": str(tmp), "executable": str(fake),
                                           "retries": 1, "models": dict(MODELS), **extra}}
    helpers = {"split_message": gb.split_message, "fence_tables": gb.fence_tables, "outbox": tmp / "outbox",
               "root": tmp, "S": gb.S}
    agent = ga.GeminiAgent(FakeBridge(), None, cfg, {}, lambda: None, logging.getLogger("test"), helpers)
    agent.discord = type("D", (), {"AllowedMentions": lambda **kw: None, "File": None})
    return agent


async def gemini_turn(agent, request="@Gemini do it", timeout=60):
    agent.remember("User", request)
    agent.s["request"] = request
    agent.pending = True
    agent.trigger.set()
    await run_one_turn(agent, timeout)
    return json.loads((Path(agent.workdir) / "calls.json").read_text())


def arg(call, flag):
    a = call["args"]
    return a[a.index(flag) + 1] if flag in a else None


async def test_gemini_routing(tmp: Path) -> None:
    for rating, want in (("easy", "hydrogen"), ("medium", "neon"), ("hard", "carbon")):
        d = tmp / rating; d.mkdir()
        agent = make_gemini(d, f"{rating}_ok")
        calls = await gemini_turn(agent)
        check(f"gemini: the request is rated by the cheap model first ({rating})",
              len(calls) == 2 and "Rate how hard" in calls[0]["stdin"] and arg(calls[0], "--model") == MODELS["easy"],
              str([c["args"] for c in calls]))
        check(f"gemini: the rating run gets no permissions ({rating})", calls and "--mode" not in calls[0]["args"]
              and "--dangerously-skip-permissions" not in calls[0]["args"], str(calls[0]["args"] if calls else ""))
        check(f"gemini: a {rating} request runs on {want}", len(calls) == 2 and want in (arg(calls[1], "--model") or ""),
              str(calls[-1]["args"]))
        check(f"gemini: the answer is posted ({rating})", agent.bridge.channel.sent[-1:] == ["**[Gemini]** final answer"],
              str(agent.bridge.channel.sent))

    d = tmp / "named"; d.mkdir()
    agent = make_gemini(d, "easy_ok")
    calls = await gemini_turn(agent, "@Gemini carbon refactor the parser")
    check("gemini: a model named in the request is used without rating", len(calls) == 1
          and arg(calls[0], "--model") == MODELS["hard"], str([c["args"] for c in calls]))

    d = tmp / "tier"; d.mkdir()
    agent = make_gemini(d, "easy_ok")
    calls = await gemini_turn(agent, "@Gemini medium: look at this")
    check("gemini: a tier named in the request is used", len(calls) == 1 and arg(calls[0], "--model") == MODELS["medium"],
          str([c["args"] for c in calls]))

    d = tmp / "fixed"; d.mkdir()
    agent = make_gemini(d, "easy_ok", model="gemini-4-argon-high")
    calls = await gemini_turn(agent)
    check("gemini: a fixed model skips the rating", len(calls) == 1 and arg(calls[0], "--model") == "gemini-4-argon-high",
          str([c["args"] for c in calls]))

    d = tmp / "junk"; d.mkdir()
    agent = make_gemini(d, "junk_ok")
    calls = await gemini_turn(agent)
    check("gemini: an unusable rating falls back to medium", arg(calls[-1], "--model") == MODELS["medium"],
          str(calls[-1]["args"]))

    agent = make_gemini(tmp, "easy_ok")
    check("gemini: a model word must follow the mention",
          agent.named_model("@Gemini please look at the carbon tax module later") is None)


async def test_gemini_runs(tmp: Path) -> None:
    # 1. permissions, prompt on stdin, conversation resumed on the next turn, token count
    d = tmp / "ok"; d.mkdir()
    agent = make_gemini(d, "easy_ok", permissions="skip")
    calls = await gemini_turn(agent)
    main = calls[-1]
    check("gemini: 'skip' allows everything", "--dangerously-skip-permissions" in main["args"], str(main["args"]))
    line = json.loads(main["stdin"].splitlines()[0]) if main["stdin"].strip() else {}
    check("gemini: the prompt goes in on stdin as a stream-json user event",
          line.get("event") == "user" and "do it" in line.get("message", {}).get("content", ""), main["stdin"][:120])
    check("gemini: the prompt is not on the command line", not any("do it" in a for a in main["args"]))
    check("gemini: the conversation id is kept", agent.s.get("thread") == "conv-1", str(agent.s.get("thread")))
    calls = await gemini_turn(agent, "@Gemini and now this")
    check("gemini: the next turn resumes the conversation", arg(calls[-1], "--conversation") == "conv-1",
          str(calls[-1]["args"]))
    check("gemini: a resumed turn does not resend the intro", "Antigravity" not in calls[-1]["stdin"])
    check("gemini: router and turn tokens are counted", agent.s.get("tokens_total") == 240, str(agent.s.get("tokens_total")))
    status = await agent.command("status")
    check("gemini: status names the last model", MODELS["easy"] in status, status)

    d = tmp / "default"; d.mkdir()
    agent = make_gemini(d, "easy_ok")
    calls = await gemini_turn(agent)
    check("gemini: the default permission mode is auto", arg(calls[-1], "--mode") == "auto", str(calls[-1]["args"]))

    # 2. a conversation that cannot be resumed: a new one, with the full prompt
    d = tmp / "lost"; d.mkdir()
    agent = make_gemini(d, "easy_lost", model="m")
    agent.s.update(thread="old-conv", turns=1)
    calls = await gemini_turn(agent)
    check("gemini: a lost conversation is started again", len(calls) == 2 and "--conversation" not in calls[1]["args"],
          str([c["args"] for c in calls]))
    check("gemini: the new conversation gets the intro", len(calls) == 2 and "Antigravity" in calls[1]["stdin"])
    check("gemini: the stray conversation agy opened silently is not kept", agent.s.get("thread") != "conv-stray")
    check("gemini: the answer from the new conversation is posted",
          agent.bridge.channel.sent[-1:] == ["**[Gemini]** final answer"], str(agent.bridge.channel.sent))
    check("gemini: the new conversation is kept", agent.s.get("thread") == "conv-2", str(agent.s.get("thread")))

    # 3. quota used up: reported, not retried, messages kept
    d = tmp / "quota"; d.mkdir()
    agent = make_gemini(d, "easy_quota", model="m")
    calls = await gemini_turn(agent)
    check("gemini: a quota error is not retried", len(calls) == 1, str(len(calls)))
    sent = agent.bridge.channel.sent
    check("gemini: a quota error is reported as a limit", sent and "limit" in sent[-1] and "Gemini" in sent[-1], str(sent))
    check("gemini: the messages are kept after a quota error", len(agent.s["buffer"]) == 1)

    # 4. files the reply names are attached only from the outbox or the workdir
    d = tmp / "files"; d.mkdir()
    agent = make_gemini(d, "easy_ok")
    (d / "outbox").mkdir()
    good = d / "outbox" / "plot.png"
    good.write_bytes(b"x")
    text, files = agent.outputs(f"see [[discord-file:{good}]] and [[discord-file:C:\\Windows\\win.ini]]")
    check("gemini: an outbox file is attached and the marker removed", files == [good.resolve()] and "discord-file" not in text,
          f"{files} {text!r}")

    # 5. other members' @Name become real mentions
    agent = make_gemini(d, "easy_ok")
    codex = make_codex(d, "ok")
    agent.bridge.members = [agent, codex]
    check("gemini: @Codex in a reply mentions the Codex bot", "<@30>" in agent.link_mentions("@Codex please review"))


async def test_switching(tmp: Path) -> None:
    # 1. a reply in a turn the owner asked for switches the member from its next turn
    d = tmp / "gem"; d.mkdir()
    agent = make_gemini(d, "easy_switch", model="gemini-4-hydrogen-high")
    agent.remember("User", "@Gemini switch to xhigh effort")
    agent.s.update(request="@Gemini switch to xhigh effort", by_owner=True)
    agent.pending = True
    agent.trigger.set()
    await run_one_turn(agent)
    calls = json.loads((d / "calls.json").read_text())
    check("switch: the switch help is in the prompt when the request talks about it",
          "[[effort:" in calls[0]["stdin"], calls[0]["stdin"][:200])
    sent = agent.bridge.channel.sent
    check("switch: the markers are removed from the posted reply", sent and "[[" not in sent[0], str(sent))
    check("switch: the change is announced", sent and "xhigh" in sent[-1], str(sent))
    check("switch: model and effort are kept", agent.current_model() == "gemini-4-hydrogen-low"
          and agent.current_effort() == "xhigh", f"{agent.current_model()} {agent.current_effort()}")
    calls = await gemini_turn(agent, "@Gemini go on")
    # agy refuses --effort next to a slug that carries its own effort: the effort goes into the slug
    check("switch: the next run uses them", arg(calls[-1], "--model") == "gemini-4-hydrogen-xhigh"
          and arg(calls[-1], "--effort") is None, str(calls[-1]["args"]))
    check("switch: effort into a suffixed slug", agent.with_effort("gemini-4-hydrogen-high", "low")
          == ("gemini-4-hydrogen-low", ""), str(agent.with_effort("gemini-4-hydrogen-high", "low")))
    check("switch: no effort keeps the slug", agent.with_effort("gemini-4-hydrogen-high", "")
          == ("gemini-4-hydrogen-high", ""))
    check("switch: a slug without an effort part gets --effort",
          agent.with_effort("claude-opus-4-6-thinking", "high") == ("claude-opus-4-6-thinking", "high"))

    # 2. in a turn someone else asked for, the member still picks its own setting, but not a premium one
    d = tmp / "gem2"; d.mkdir()
    agent = make_gemini(d, "easy_switch", model="gemini-4-hydrogen-high", premium=["xhigh"])
    agent.remember("Codex", "@Gemini switch to xhigh")
    agent.s.update(request="@Gemini switch to xhigh", by_owner=False)
    agent.pending = True
    agent.trigger.set()
    await run_one_turn(agent)
    check("switch: a member may pick an ordinary model itself", agent.current_model() == "gemini-4-hydrogen-low",
          agent.current_model())
    check("switch: a premium effort needs the owner", not agent.current_effort(), agent.current_effort())
    check("switch: the refusal is said", "owner" in (agent.bridge.channel.sent or [""])[-1], str(agent.bridge.channel.sent))
    agent.s["model"] = None

    # 3. owner commands, a bad effort, back to default
    note = await agent.command("effort turbo")
    check("switch: an unknown effort is refused", "turbo" in note and not agent.current_effort(), note)
    await agent.command("effort low")
    await agent.command("model gemini-3.8-flash-low")
    check("switch: !gemini model/effort set them", agent.current_model() == "gemini-3.8-flash-low"
          and agent.current_effort() == "low")
    await agent.command("model default")
    check("switch: 'default' returns to the config", agent.current_model() == "gemini-4-hydrogen-high")
    check("switch: status shows model and effort", "low" in await agent.command("status"))

    # 4. Codex: effort goes in as a config override, model as -m
    d = tmp / "codex"; d.mkdir()
    codex = make_codex(d, "switch")
    codex.remember("User", "@Codex use high effort")
    codex.s.update(request="@Codex use high effort", by_owner=True)
    codex.pending = True
    codex.trigger.set()
    await run_one_turn(codex)
    check("switch: Codex switched its effort", codex.current_effort() == "high", codex.current_effort())
    await codex.command("model gpt-test")
    args = codex.command_args(None)
    check("switch: Codex runs get -m and the effort override", "gpt-test" in args
          and 'model_reasoning_effort="high"' in args, str(args))

    # 5. a mention inside backticks becomes a real mention
    codex.bridge.members = [codex, agent]
    check("switch: backticks around a mention are dropped", codex.link_mentions("ask `@BotGemini` and `@Claude`")
          == f"ask <@40> and <@20>", codex.link_mentions("ask `@BotGemini` and `@Claude`"))


async def test_progress_board(tmp: Path) -> None:
    b = make_bridge(tmp, claude_watch={"progress_heading": "Doing", "progress_empty": ["nil"]})
    gb.STATUS_FILE = tmp / "status.md"
    gb.STATUS_FILE.write_text("**Doing**\n- rerun the strategy, results at 20:50\n- Gemini checks the list\n\n**Done**\n- other\n\n**Decide**\nnil\n",
                              encoding="utf-8")
    check("progress: the in-progress items are read", b.in_progress() == ["rerun the strategy, results at 20:50", "Gemini checks the list"],
          str(b.in_progress()))
    gb.STATUS_FILE.write_text("**Doing**\nnil\n\n**Done**\n- other\n", encoding="utf-8")
    check("progress: a configured empty word means nothing in progress", b.in_progress() == [], str(b.in_progress()))
    await b.close()


async def test_new_session(tmp: Path) -> None:
    d = tmp / "ns"; d.mkdir()
    agent = make_gemini(d, "easy_newsession", model="m")
    agent.s.update(thread="conv-1", turns=3)  # a session from before the rules
    calls = await gemini_turn(agent)
    check("session: an old session gets the rules once", "[[new-session]]" in calls[-1]["stdin"], calls[-1]["stdin"][:200])
    check("session: the marker is not posted", "[[" not in agent.bridge.channel.sent[0], str(agent.bridge.channel.sent))
    check("session: the request is acknowledged", "fresh session" in agent.bridge.channel.sent[-1], str(agent.bridge.channel.sent))
    calls = await gemini_turn(agent, "@Gemini next")
    check("session: the next turn starts a fresh session", "--conversation" not in calls[-1]["args"], str(calls[-1]["args"]))
    check("session: the fresh session gets the intro", "Antigravity" in calls[-1]["stdin"])
    calls = await gemini_turn(agent, "@Gemini and again")
    check("session: the rules are not repeated in a resumed turn", "[[new-session]]" not in calls[-1]["stdin"]
          and "--conversation" in calls[-1]["args"], str(calls[-1]["args"]))


async def test_self_heal(tmp: Path) -> None:
    # 1. a stored switch the CLI refuses: dropped for good, the config's model is used, at once
    d = tmp / "stored"; d.mkdir()
    agent = make_gemini(d, "easy_reject", model="gemini-4-hydrogen-high")
    agent.s["model"] = "gemini-bogus-x"
    t0 = time.time()
    calls = await gemini_turn(agent)
    sent = agent.bridge.channel.sent
    check("heal: a refused stored model is retried on the config's", [arg(c, "--model") for c in calls]
          == ["gemini-bogus-x", "gemini-4-hydrogen-high"], str([c["args"] for c in calls]))
    check("heal: the refused switch is dropped for good", "model" not in agent.s and agent.current_model()
          == "gemini-4-hydrogen-high", str(agent.s.get("model")))
    check("heal: the switch back is said once", sum("refused" in (x or "") for x in sent) == 1 and "gemini-bogus-x" in sent[0],
          str(sent))
    check("heal: the turn's answer is posted, no failure", sent[-1:] == ["**[Gemini]** final answer"]
          and not any("did not finish" in (x or "") for x in sent), str(sent))
    check("heal: no 20 s retry wait for a refused model", time.time() - t0 < 15, f"{time.time() - t0:.0f}s")

    # 2. the config's own model is refused: this turn runs on agy's default (no --model, no --effort)
    d = tmp / "config"; d.mkdir()
    agent = make_gemini(d, "easy_reject", model="gemini-bogus-cfg", effort="low")
    calls = await gemini_turn(agent)
    check("heal: a refused config model falls back to the CLI default", len(calls) == 2 and arg(calls[1], "--model") is None
          and arg(calls[1], "--effort") is None, str([c["args"] for c in calls]))
    check("heal: the config's choice is not changed", agent.current_model() == "gemini-bogus-cfg"
          and agent.current_effort() == "low", f"{agent.current_model()} {agent.current_effort()}")
    check("heal: the answer is posted after the fallback", agent.bridge.channel.sent[-1:] == ["**[Gemini]** final answer"],
          str(agent.bridge.channel.sent))
    calls = await gemini_turn(agent, "@Gemini next")
    check("heal: the next turn tries the config again", arg(calls[2], "--model") == "gemini-bogus-cfg", str(calls[2]["args"]))

    # 3. refused whatever it gets: bounded, and reported as a failure
    d = tmp / "all"; d.mkdir()
    agent = make_gemini(d, "easy_rejectall", model="gemini-bogus-cfg")
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s, *a: real_sleep(min(s, 0.05))
    try:
        calls = await gemini_turn(agent)
    finally:
        asyncio.sleep = real_sleep
    check("heal: a CLI that refuses everything is not retried forever", len(calls) == 3, str(len(calls)))
    check("heal: then the failure is reported", "did not finish" in (agent.bridge.channel.sent or [""])[-1],
          str(agent.bridge.channel.sent))

    # 4. Codex refuses a stored model: back to the default (no -m)
    d = tmp / "codex"; d.mkdir()
    codex = make_codex(d, "badmodel")
    codex.s["model"] = "bogus-model"
    codex.remember("User", "@Codex do it")
    codex.pending = True
    codex.trigger.set()
    await run_one_turn(codex)
    calls = json.loads((d / "calls.json").read_text())
    check("heal: Codex retries without the refused model", len(calls) == 2 and "bogus-model" in calls[0]["args"]
          and "-m" not in calls[1]["args"], str([c["args"] for c in calls]))
    check("heal: Codex's answer is posted", codex.bridge.channel.sent[-1:] == ["**[Codex]** final answer"],
          str(codex.bridge.channel.sent))
    check("heal: the patterns know both CLIs", all(cli.CHOICE_RE.search(x) for x in (
        'invalid model selection (--model "gemini-4-hydrogen-high" --effort "low"): --model gemini-4-hydrogen-high conflicts with --effort=low',
        "The 'gpt-x' model is not supported when using Codex with a ChatGPT account."))
          and not cli.CHOICE_RE.search("conversation abc not found") and not cli.CHOICE_RE.search("boom"))


async def test_failure_mention(tmp: Path) -> None:
    # 1. a member's failed turn mentions whoever asked: Claude and the owner here
    d = tmp / "fail"; d.mkdir()
    agent = make_codex(d, "fail", retries=0)
    await agent.observe(FakeMsg(901, author_id=20, bot=True), "Claude", "@Codex do it", may_trigger=True)
    await agent.observe(FakeMsg(902, author_id=10), "User", "@Codex and this", may_trigger=True)
    check("mention: the requesters are kept with the request", agent.s.get("requesters") == [20, 10], str(agent.s.get("requesters")))
    await run_one_turn(agent)
    sent = agent.bridge.channel.sent
    check("mention: the failure notice mentions the requesters", sent and "<@20>" in sent[-1] and "<@10>" in sent[-1]
          and "did not finish" in sent[-1], str(sent))
    check("mention: the requesters are forgotten after the turn", "requesters" not in agent.s, str(agent.s.get("requesters")))

    # 2. a good turn mentions nobody extra
    d = tmp / "ok"; d.mkdir()
    agent = make_codex(d, "ok")
    await agent.observe(FakeMsg(903, author_id=20, bot=True), "Claude", "@Codex do it", may_trigger=True)
    await run_one_turn(agent)
    check("mention: a good answer adds no mention", agent.bridge.channel.sent[-1:] == ["**[Codex]** final answer"],
          str(agent.bridge.channel.sent))

    # 3. the AI-streak cap still holds: a bot's request at the cap starts nothing and records no requester
    d = tmp / "cap"; d.mkdir()
    agent = make_codex(d, "fail", retries=0)
    agent.bridge.streak = agent.bridge.cap
    await agent.observe(FakeMsg(904, author_id=40, bot=True), "Gemini", "@Codex do it", may_trigger=True)
    check("mention: at the cap a bot's request starts no turn", not agent.pending and "requesters" not in agent.s
          and not agent.trigger.is_set(), str(agent.s))

    # 4. a post the GPT bot made for a member without its own bot counts as that member's request
    gem = make_gemini(d, "easy_ok")
    agent.bridge.user = type("U", (), {"id": 50})()
    agent.bridge.members = [agent, gem]
    m = FakeMsg(905, author_id=50, bot=True, content="**[Gemini]** @Codex please check")
    check("mention: a GPT-bot post for Gemini is Gemini's request", agent.requester_of(m) == 40, str(agent.requester_of(m)))


async def test_progress_wake(tmp: Path) -> None:
    b = make_bridge(tmp, claude_watch={"progress_check_min": 15, "idle_stop_min": 30, "wake_on_mention": True,
                                       "work_dir": str(tmp), "progress_heading": "Doing"})
    gb.STATUS_FILE = tmp / "status.md"
    gb.STATUS_FILE.write_text("**Doing**\n- rerun the strategy\n\n**Done**\n- other\n", encoding="utf-8")
    woken, controls, running = [], [], [False]

    async def fake_running():
        return running[0]

    async def fake_wake(msg, text=None):
        woken.append((msg, text))
        return True

    async def fake_control(tag, text):
        controls.append((tag, text))

    b.claude_running, b.wake_claude, b.control, b._cw_find = fake_running, fake_wake, fake_control, lambda: None
    now = time.time()
    b.cw_progress_sent = 0
    await b.progress_check(now)
    check("wake: a stopped Claude is woken while the board lists work", len(woken) == 1 and woken[0][0] is None
          and woken[0][1] == gb.S("progress_claude", min=15), str(woken))
    await b.progress_check(now + 60)
    check("wake: not again before the next interval", len(woken) == 1, str(len(woken)))

    b.cw_progress_sent = 0
    b.halted = {"since": now}
    await b.progress_check(now)
    check("wake: not while the group is halted", len(woken) == 1, str(len(woken)))
    b.halted = None
    (tmp / "restart.request").write_text("", encoding="utf-8")
    await b.progress_check(now)
    check("wake: not while a restart is requested", len(woken) == 1, str(len(woken)))
    (tmp / "restart.request").unlink()
    gb.STATUS_FILE.write_text("**Doing**\n\n**Done**\n- other\n", encoding="utf-8")
    await b.progress_check(now)
    check("wake: not when nothing is in progress", len(woken) == 1, str(len(woken)))

    # running and quiet: the usual progress prompt, no wake
    gb.STATUS_FILE.write_text("**Doing**\n- rerun the strategy\n", encoding="utf-8")
    transcript = tmp / "t.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    os.utime(transcript, (now - 3600, now - 3600))
    b._cw_find = lambda: transcript
    running[0] = True
    await b.progress_check(now)
    check("wake: a running, quiet Claude gets the progress prompt", len(woken) == 1 and controls
          and controls[-1][0] == gb.S("tag_progress"), str(controls))

    # the idle stop leaves Claude running while the board lists work
    real_exec = asyncio.create_subprocess_exec
    spawned = []

    async def fake_exec(*a, **kw):
        spawned.append(a)
        raise RuntimeError("no process in tests")

    asyncio.create_subprocess_exec = fake_exec
    try:
        await b.stop_idle_claude()
        check("wake: no idle stop while work is in progress", not spawned, str(spawned))
        gb.STATUS_FILE.write_text("**Doing**\n\n", encoding="utf-8")
        try:
            await b.stop_idle_claude()
        except RuntimeError:
            pass
        check("wake: the idle stop still works with nothing in progress", len(spawned) == 1, str(spawned))
    finally:
        asyncio.create_subprocess_exec = real_exec

    # the real wake path with no message: starts Claude and hands it the progress prompt
    b2 = make_bridge(tmp, claude_watch={"progress_check_min": 15, "wake_on_mention": True, "work_dir": str(tmp)})
    states = iter([False, True, True])

    async def running_seq():
        return next(states, True)

    class Proc:
        async def wait(self):
            return 0

    async def fake_exec2(*a, **kw):
        return Proc()

    real_sleep = asyncio.sleep
    b2.claude_running, b2.control = running_seq, fake_control
    controls.clear()
    asyncio.create_subprocess_exec = fake_exec2
    asyncio.sleep = lambda s, *a: real_sleep(0)
    try:
        ok = await b2.wake_claude(None, "check the board")
    finally:
        asyncio.create_subprocess_exec, asyncio.sleep = real_exec, real_sleep
    check("wake: a wake without a message comes up", ok is True, str(ok))
    check("wake: it gets the progress hand-over", controls and controls[-1][0] == gb.S("tag_progress")
          and "check the board" in controls[-1][1], str(controls))
    await b.close()
    await b2.close()


async def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for i, test in enumerate((test_buffer, test_halt, test_web_turns, test_mirror_rules, test_page_loads, test_reports, test_codex_buffer, test_codex_runs,
                                  test_gemini_routing, test_gemini_runs,
                                  test_switching, test_progress_board, test_new_session,
                                  test_self_heal, test_failure_mention, test_progress_wake)):
            d = tmp / str(i)
            d.mkdir()
            try:
                await test(d)
            except Exception as e:  # a crash in a test is a failure of that test
                import traceback
                traceback.print_exc()
                check(f"{test.__name__} ran to the end", False, repr(e))
    print()
    print("ALL PASSED" if not FAILED else f"{len(FAILED)} FAILED: " + "; ".join(FAILED))
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
