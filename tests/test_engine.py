"""Offline tests for the parts of the bridge where messages used to get lost.

No Discord connection, no browser, no real Codex: the Discord client is constructed but never
logged in, and Codex is replaced by tests/fake_codex.py. Run with the bridge's Python:

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
        return ca._Null()


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
    channel_id, log_channel_id, halted, streak, cap, claude_id, user = 1, 2, None, 0, 4, 20, None

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


async def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for i, test in enumerate((test_buffer, test_halt, test_web_turns, test_mirror_rules, test_page_loads, test_reports, test_codex_buffer, test_codex_runs)):
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
