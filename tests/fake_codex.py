"""Stand-in for `codex exec --json` in tests/test_engine.py.

    fake_codex.py <state dir> <scenario> <the arguments the bridge passes to codex...>

Records every call (arguments and stdin) in <state dir>/calls.json and prints events like Codex.
"""
import json
import sys
import time
from pathlib import Path

state, scenario, args = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
log = state / "calls.json"
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({"args": args, "stdin": sys.stdin.read()})
log.write_text(json.dumps(calls))


def emit(**event):
    print(json.dumps(event), flush=True)


def message(text):
    emit(type="item.completed", item={"id": "item_0", "type": "agent_message", "text": text})


emit(type="thread.started", thread_id="thread-1")
if scenario == "hang":
    time.sleep(120)
elif scenario == "fail":
    emit(type="turn.failed", error={"message": "boom"})
    sys.exit(1)
elif scenario == "cut_then_ok" and len(calls) == 1:
    emit(type="turn.started")
    message("working on it")
    sys.exit(0)  # ends without turn.completed
else:
    emit(type="turn.started")
    message("final answer")
    emit(type="turn.completed", usage={})
