#!/usr/bin/env python3
"""CI subset of smoke_test.py: offline checks plus a local server health and
agent-card check. Never calls the live Marx MCP."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import smoke_test as st  # noqa: E402


def main():
    st.check_offline()
    print("offline checks passed")

    port = st.free_port()
    expected = f"http://127.0.0.1:{port}/a2a"
    with tempfile.TemporaryDirectory() as tmp:
        events = os.path.join(tmp, "events.jsonl")
        log_path = os.path.join(tmp, "server.log")
        env = st.child_env(port, expected, "https://should-not-win.example", events)
        proc, log = st.start(port, env, log_path)
        try:
            base = f"http://127.0.0.1:{port}"
            health = st.wait_health(base, proc, log_path)
            if health != {"ok": True}:
                raise st.Fail(f"unexpected health body: {health}")
            st.check_cards(base, expected)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            log.close()
    print("local server health and cards passed")


if __name__ == "__main__":
    try:
        main()
    except st.Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
