"""Stand-in for the Antigravity CLI (`agy -p ... --output-format stream-json`) in tests/test_engine.py.

    fake_agy.py <state dir> <scenario> <the arguments the bridge passes to agy...>

Records every call (arguments and stdin) in <state dir>/calls.json and prints stream-json
events like agy. A call with `--mode plan` is the bridge rating a request; the scenario's
first word is the rating ("hard_ok" rates hard, "junk_..." rates nonsense).
Like agy 1.5.17, `--conversation` with an unknown id ("lost") warns on stderr and starts a new
conversation instead of failing.
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
rating, _, behaviour = scenario.partition("_")


def emit(event, **payload):
    print(json.dumps({"event": event, **payload}), flush=True)


def result(status, response="", error=None, conversation="conv-1", tokens=100):
    r = {"conversation_id": conversation, "status": status, "response": response, "duration_seconds": 1,
         "num_turns": 1, "usage": {"input_tokens": tokens - 10, "output_tokens": 10, "total_tokens": tokens}}
    if error:
        r["error"] = error
    emit("result", result=r)


if "-p" in args or "--print" in args:
    print('Error: -p took "--input-format" as its prompt', file=sys.stderr)
    sys.exit(2)
if "Rate how hard" in calls[-1]["stdin"]:
    emit("init", conversation_id="conv-router", init={"model": "x"})
    result("SUCCESS", "Probably not much." if rating == "junk" else rating.capitalize() + ".", conversation="conv-router", tokens=20)
    sys.exit(0)
conv = "conv-1"
if behaviour == "lost":
    conv = "conv-2"
    if "--conversation" in args:
        print(f'warning: conversation "{args[args.index("--conversation") + 1]}" not found', file=sys.stderr)
        conv = "conv-stray"
emit("init", conversation_id=conv, init={"model": "x"})
emit("step_update", step_update={"step_type": "tool", "state": "ACTIVE", "tool_name": "read_file"})
if behaviour == "hang":
    time.sleep(120)
elif behaviour == "quota":
    result("ERROR", error="RESOURCE_EXHAUSTED: quota exceeded for this model")
    sys.exit(1)
elif behaviour == "newsession" and len(calls) == 1:
    result("SUCCESS", "done, see notes [[new-session]]", conversation=conv)
elif behaviour == "switch":
    result("SUCCESS", "sure [[effort:xhigh]] [[model:gemini-4-hydrogen-low]]", conversation=conv)
else:
    result("SUCCESS", "final answer", conversation=conv)
