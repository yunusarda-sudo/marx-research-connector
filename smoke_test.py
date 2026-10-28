#!/usr/bin/env python3
"""Start the connector locally and check cards, health, and one SendMessage.

The SendMessage uses test_request.json and therefore calls Marx's public read
tools (search_feed, list_open_threads, how_to_join). It does not call write tools.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
WRITE_TOOLS = ("register_agent", "post_reply", "vote", "heartbeat", "set_webhook", "ask_marx_pro")
IMPLEMENTED = {"search_feed", "list_open_threads", "how_to_join"}
SKILLS = {
    "find_relevant_market_discussions",
    "explain_marx_mcp",
    "guide_first_contribution",
}


class Fail(Exception):
    pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None, timeout=90, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_health(base, proc, log_path):
    deadline = time.time() + 10
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise Fail(f"server exited {proc.returncode}\n{open(log_path, encoding='utf-8').read()}")
        try:
            status, raw = http("GET", base + "/healthz", timeout=1)
            if status == 200:
                return json.loads(raw)
        except Exception as e:
            last = e
        time.sleep(0.1)
    raise Fail(f"health check did not become ready: {last}\n{open(log_path, encoding='utf-8').read()}")


def start(port, env, log_path):
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py")],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, log


def check_cards(base, expected):
    status, raw = http("GET", base + "/.well-known/agent-card.json")
    if status != 200:
        raise Fail(f"agent card status {status}: {raw[:300]!r}")
    card = json.loads(raw)
    urls = [i["url"] for i in card["supportedInterfaces"]]
    if not urls or any(u != expected for u in urls):
        raise Fail(f"1.0 card URL {urls!r} != {expected}")
    if "REPLACE-WITH-LIVE-HOST" in raw.decode():
        raise Fail("1.0 card still contains the placeholder")
    caps = card["capabilities"]
    if caps.get("streaming") or caps.get("pushNotifications") or caps.get("extendedAgentCard"):
        raise Fail(f"1.0 card advertises a capability the server refuses: {caps}")
    if {s["id"] for s in card["skills"]} != SKILLS:
        raise Fail(f"unexpected skills: {card['skills']}")

    status, raw = http("GET", base + "/.well-known/agent.json")
    if status != 200:
        raise Fail(f"0.3 card status {status}: {raw[:300]!r}")
    legacy = json.loads(raw)
    if legacy.get("url") != expected:
        raise Fail(f"0.3 card URL {legacy.get('url')!r} != {expected}")
    if "REPLACE-WITH-LIVE-HOST" in raw.decode():
        raise Fail("0.3 card still contains the placeholder")
    if legacy["capabilities"].get("streaming") or legacy["capabilities"].get("pushNotifications"):
        raise Fail(f"0.3 card advertises streaming or push: {legacy['capabilities']}")
    return card, legacy


def check_send(base):
    payload = open(os.path.join(ROOT, "test_request.json"), "rb").read()
    status, raw = http(
        "POST",
        base + "/a2a",
        body=payload,
        timeout=90,
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    )
    if status != 200:
        raise Fail(f"SendMessage status {status}: {raw[:500]!r}")
    body = json.loads(raw)
    if body.get("jsonrpc") != "2.0" or body.get("id") != "t1" or "error" in body:
        raise Fail(f"SendMessage was not a successful JSON-RPC result: {raw[:800]!r}")
    task = body.get("result", {}).get("task")
    if not isinstance(task, dict):
        raise Fail("result.task missing")
    if task.get("status", {}).get("state") != "TASK_STATE_COMPLETED":
        raise Fail(f"unexpected task state: {task.get('status')}")
    if not task.get("id") or not task.get("contextId"):
        raise Fail("task id or contextId missing")
    parts = task["artifacts"][0]["parts"]
    texts = [p.get("text", "") for p in parts if "text" in p]
    datas = [p.get("data") for p in parts if "data" in p]
    if not texts or not texts[0].strip():
        raise Fail("text part missing")
    if not datas or "discussions" not in datas[0]:
        raise Fail("data part missing discussions")
    calls = datas[0]["discussions"].get("mcpCalls") or []
    if not calls:
        raise Fail("mcpCalls empty; SendMessage did not record tool calls")
    used = {c["tool"] for c in calls}
    if not used <= IMPLEMENTED:
        raise Fail(f"SendMessage called tools outside the allow-list: {used}")
    if "howToJoin" not in datas[0]:
        raise Fail("sample request should have called how_to_join")
    # A write-method name on the JSON-RPC endpoint must not be forwarded.
    status, raw = http(
        "POST",
        base + "/a2a",
        body={"jsonrpc": "2.0", "id": "nope", "method": "register_agent", "params": {}},
        headers={"Content-Type": "application/json"},
    )
    blocked = json.loads(raw)
    if blocked.get("error", {}).get("code") != -32601:
        raise Fail(f"register_agent was not refused as an unknown method: {raw[:400]!r}")
    return len(datas[0]["discussions"]["results"]), sorted(used)


def check_offline():
    import server
    import urllib.request as ureq

    saved = {k: os.environ.get(k) for k in ("PUBLIC_A2A_URL", "RENDER_EXTERNAL_URL")}
    original_open = ureq.urlopen

    def fail_open(*_a, **_k):
        raise RuntimeError("network used")

    try:
        os.environ.pop("PUBLIC_A2A_URL", None)
        os.environ.pop("RENDER_EXTERNAL_URL", None)
        if server.public_url() != server.PLACEHOLDER:
            raise Fail("expected placeholder when no URL env is set")
        os.environ["RENDER_EXTERNAL_URL"] = "https://demo.onrender.com/"
        if server.public_url() != "https://demo.onrender.com/a2a":
            raise Fail(f"RENDER_EXTERNAL_URL fallback was {server.public_url()}")
        if "REPLACE-WITH-LIVE-HOST" in server.public_url():
            raise Fail("fallback URL still contains the placeholder")
        os.environ["PUBLIC_A2A_URL"] = "https://custom.example/a2a"
        if server.public_url() != "https://custom.example/a2a":
            raise Fail("PUBLIC_A2A_URL did not win over RENDER_EXTERNAL_URL")
        sample = "prefix sk-abcdefghijklmnopqrstuvwxyz suffix"
        red = server.redact(sample)
        if "sk-" in red or "abcdefghijklmnopqrstuvwxyz" in red or "[REDACTED]" not in red:
            raise Fail(f"redaction failed: {red}")
        if server.READ_TOOLS != IMPLEMENTED:
            raise Fail(f"allow-list changed: {server.READ_TOOLS}")
        ureq.urlopen = fail_open
        for tool in WRITE_TOOLS:
            try:
                server.mcp_call(tool, {"name": "should-not-send"})
            except AssertionError as e:
                if "blocked" not in str(e):
                    raise Fail(f"{tool} raised the wrong assertion: {e}")
            except RuntimeError as e:
                raise Fail(f"{tool} reached the network: {e}")
            else:
                raise Fail(f"{tool} was not blocked")
    finally:
        ureq.urlopen = original_open
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def child_env(port, public, render, events):
    env = os.environ.copy()
    env.pop("HOST", None)
    env["PORT"] = str(port)
    env["A2A_EVENTS_PATH"] = events
    if public is None:
        env.pop("PUBLIC_A2A_URL", None)
    else:
        env["PUBLIC_A2A_URL"] = public
    if render is None:
        env.pop("RENDER_EXTERNAL_URL", None)
    else:
        env["RENDER_EXTERNAL_URL"] = render
    return env


def main():
    check_offline()
    print("offline checks passed")

    port = free_port()
    expected = f"http://127.0.0.1:{port}/a2a"
    events = f"/tmp/a2a_events_smoke_{os.getpid()}.jsonl"
    log_path = f"/tmp/a2a_smoke_{os.getpid()}.log"
    env = child_env(port, expected, "https://should-not-win.example", events)
    proc, log = start(port, env, log_path)
    try:
        base = f"http://127.0.0.1:{port}"
        health = wait_health(base, proc, log_path)
        if health != {"ok": True}:
            raise Fail(f"unexpected health body: {health}")
        startup = open(log_path, encoding="utf-8").read()
        if f"http://0.0.0.0:{port}" not in startup:
            raise Fail(f"server did not bind 0.0.0.0:\n{startup}")
        check_cards(base, expected)
        n, used = check_send(base)
        print(f"SendMessage passed ({n} discussions, tools={used})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()

    port2 = free_port()
    render_base = "https://marx-research-connector.onrender.com"
    events2 = f"/tmp/a2a_events_fallback_{os.getpid()}.jsonl"
    log_path2 = f"/tmp/a2a_smoke_fallback_{os.getpid()}.log"
    env2 = child_env(port2, None, render_base + "/", events2)
    proc2, log2 = start(port2, env2, log_path2)
    try:
        base2 = f"http://127.0.0.1:{port2}"
        wait_health(base2, proc2, log_path2)
        check_cards(base2, render_base + "/a2a")
        print("RENDER_EXTERNAL_URL card fallback passed")
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.wait(timeout=5)
        log2.close()

    print("smoke test passed")


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
