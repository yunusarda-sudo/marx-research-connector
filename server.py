#!/usr/bin/env python3
"""Marx Research Connector: minimal A2A (1.0 + 0.3 JSON-RPC) server.

Serves the Agent Card at /.well-known/agent-card.json and the 0.3 card at
/.well-known/agent.json. The public URL is PUBLIC_A2A_URL, else
RENDER_EXTERNAL_URL + /a2a, else a non-resolving placeholder.

POST /a2a accepts SendMessage (1.0) and message/send (0.3), GetTask / tasks/get,
and CancelTask / tasks/cancel. Answers come only from Marx's public read-only
MCP tools that this server actually calls: search_feed, list_open_threads, and
how_to_join. Write tools are refused before any network call. Key-like strings
are redacted and never forwarded.

Listens on 0.0.0.0 and $PORT (default 8787). Stdlib only.
"""
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_URL = "https://marx.finance/mcp"
SETUP_URL = "https://marx.finance/docs/mcp"
PLACEHOLDER = "https://REPLACE-WITH-LIVE-HOST.invalid/a2a"
# Tools this process is allowed to call. The handler only invokes the first
# three; the rest of Marx's API (writes, and reads this card does not offer)
# stays outside the set and is rejected before a request is built.
READ_TOOLS = {"search_feed", "list_open_threads", "how_to_join"}
FOOTER = "Registration is free; contributing requires your own agent identity."
TZ = datetime.timezone(datetime.timedelta(hours=3))
STOP = set("""a an the and or of for to in on at by with from about into over is are was were be been i im i'm my me we our you your
it its this that these those can could would should will do does did any some what which who how why when where please help find
discussions discussion relevant analysis market markets research researching researcher looking look interested agent agents marx
thread threads moves move news mcp open key keys redacted api endpoint setup install register join contribute first post reply discover discovering connect connecting would via driven based using use show me tell give us want need around related topic topics on""".split())
CONNECT_RX = re.compile(r"\b(mcp|connect|integrat\w*|setup|set up|install|endpoint|api)\b", re.I)
JOIN_RX = re.compile(r"\b(register\w*|sign ?up|join|contribut\w*|first (post|reply|analysis)|onboard\w*)\b", re.I)
KEYLIKE = re.compile(r"\b(sk|mk|key|tok)[-_][A-Za-z0-9_\-]{12,}\b|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9_\-]{40,}\b")
TASKS, LOCK = {}, threading.Lock()
RATE = {}


def public_url():
    """URL advertised on the agent cards. Never the placeholder when an env URL is set."""
    explicit = (os.environ.get("PUBLIC_A2A_URL") or "").strip()
    if explicit:
        return explicit
    render = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if render:
        return render + "/a2a"
    return PLACEHOLDER


def events_path():
    return os.environ.get("A2A_EVENTS_PATH") or "/tmp/a2a_events.jsonl"


def now():
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def redact(s):
    return KEYLIKE.sub("[REDACTED]", s or "")


def mcp_call(tool, args):
    if tool not in READ_TOOLS:
        raise AssertionError("write tool blocked")
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "MarxResearchConnector/0.1 (A2A onboarding, read-only)",
    })  # no x-api-key, ever
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode()
    if raw.startswith("event:") or raw.startswith("data:"):
        raw = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")][-1]
    res = json.loads(raw)["result"]
    return json.loads(res["content"][0]["text"])


def extract(text):
    tickers = sorted(set(
        t for t in re.findall(r"\$?\b([A-Z]{2,5})\b", text)
        if t not in {"MCP", "API", "AI", "US", "A2A", "FED", "ETF", "CEO", "SEC"}
    ))
    words = [w.lower().strip("'") for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", text)]
    kws = []
    for w in words:
        if w in STOP or w.upper() in tickers or w in kws:
            continue
        kws.append(w)
    if re.search(r"\bfed\b|federal reserve|fomc", text, re.I) and "fed" not in kws:
        kws.insert(0, "fed")
    return tickers[:3], kws[:4]


def _safe(call):
    try:
        return mcp_call(*call)
    except Exception:
        return None


def find_discussions(text):
    tickers, kws = extract(text)
    calls = [("search_feed", {"ticker": t, "limit": 5}) for t in tickers]
    calls += [("search_feed", {"query": k, "limit": 5}) for k in kws]
    calls.append(("list_open_threads", {"limit": 50}))
    with ThreadPoolExecutor(6) as ex:
        outs = list(ex.map(lambda c: (c, _safe(c)), calls))
    hits, open_ids, used = {}, set(), []
    for (tool, args), out in outs:
        used.append({"tool": tool, "arguments": args, "ok": out is not None})
        if out is None:
            continue
        if tool == "list_open_threads":
            open_ids = {t["postId"] for t in out.get("threads", [])}
            continue
        for p in out.get("posts", []):
            h = hits.setdefault(p["id"], {"post": p, "matched": []})
            h["matched"].append(args.get("ticker") or args.get("query"))
    tk = set(tickers)
    ranked = sorted(
        hits.values(),
        key=lambda h: (
            len(h["matched"]) + sum(m in tk for m in h["matched"]),
            h["post"].get("createdAt", ""),
        ),
        reverse=True,
    )[:6]
    results = [{
        "postId": h["post"]["id"],
        "title": h["post"]["title"],
        "url": h["post"].get("url"),
        "tickers": h["post"].get("tickers", [])[:8],
        "createdAt": h["post"].get("createdAt"),
        "openForAgentReplies": h["post"]["id"] in open_ids,
        "matchedTerms": h["matched"],
    } for h in ranked]
    return {"tickers": tickers, "keywords": kws, "results": results, "mcpCalls": used}


def handle_text(text, meta):
    skills, data, lines = [], {}, []
    wants_connect, wants_join = bool(CONNECT_RX.search(text)), bool(JOIN_RX.search(text))
    d = find_discussions(text)
    skills.append("find_relevant_market_discussions")
    data["discussions"] = d
    if d["results"]:
        lines.append(
            "Marx Finance discussions matching "
            + ", ".join(d["tickers"] + d["keywords"])
            + " (from Marx MCP search_feed):"
        )
        for r in d["results"]:
            lines.append(
                f"- {r['title']} | {r['url']} | {r['createdAt'][:10]}"
                + (" | open for agent replies" if r["openForAgentReplies"] else "")
            )
    else:
        lines.append(
            "The Marx MCP returned no posts for "
            + (", ".join(d["tickers"] + d["keywords"]) or "that message")
            + ". Try a ticker (e.g. NVDA) or one keyword (e.g. liquidity)."
        )
    if wants_connect or wants_join:
        hj = _safe(("how_to_join", {}))
        if hj:
            skills.append("guide_first_contribution" if wants_join else "explain_marx_mcp")
            data["howToJoin"] = hj
            lines.append("")
            lines.append("How to connect (from Marx MCP how_to_join): " + hj.get("summary", ""))
            wt = hj.get("withThisServer", {})
            if wt.get("connect"):
                lines.append("Connect: " + wt["connect"])
            for s in hj.get("steps", []):
                lines.append(
                    f"{s['step']}. {s['action']}: {s['request']}"
                    + (f" ({s['note']})" if s.get("note") else "")
                )
    lines += [
        "",
        f"MCP: {MCP_URL}",
        f"Setup: {SETUP_URL}",
        "Reading public posts needs no registration. " + FOOTER,
        "This connector never asks for, receives, or stores API keys and does not register agents for you. "
        "If you register and want us to look for your public contributions, tell us your Marx agent name.",
    ]
    return "\n".join(lines), data, skills, wants_connect, wants_join


def log_event(ev):
    path = events_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def card(legacy=False):
    name = "agent-card.v0.3.json" if legacy else "agent-card.json"
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        c = json.load(f)
    url = public_url()
    if legacy:
        c["url"] = url
    else:
        for interface in c["supportedInterfaces"]:
            interface["url"] = url
    return c


def text_of(msg):
    out = []
    for p in msg.get("parts", []):
        if "text" in p:
            out.append(p["text"])
        elif isinstance(p.get("data"), dict):
            out.append(json.dumps(p["data"]))
    return " ".join(out).strip()


def send_message(params, legacy, caller_hdr, ip):
    msg = params.get("message") or {}
    raw_text = text_of(msg)
    if not raw_text:
        raise RPCError(-32602, "message.parts must contain text")
    text = redact(raw_text)[:2000]
    meta = {**(params.get("metadata") or {}), **(msg.get("metadata") or {})}
    caller = redact(str(meta.get("callerName") or meta.get("agentName") or caller_hdr or ""))[:80] or None
    marx_name = redact(str(meta.get("marxAgentName") or ""))[:80] or None
    t0 = time.time()
    reply, data, skills, wc, wj = handle_text(text, meta)
    key_redacted = text != raw_text[:2000] or KEYLIKE.search(raw_text) is not None
    if text != raw_text[:2000]:
        reply = (
            "Note: your message looked like it contained a secret or API key. It was redacted and not stored. "
            "Never share your Marx API key with anyone, including this connector.\n\n"
        ) + reply
    tid, cid = str(uuid.uuid4()), msg.get("contextId") or str(uuid.uuid4())
    ev = {
        "ts": now(),
        "event": "A2A_REQUEST",
        "channel": "A2A Inbound",
        "protocol": "0.3" if legacy else "1.0",
        "caller_name": caller,
        "topic": text[:300],
        "tickers": data["discussions"]["tickers"],
        "keywords": data["discussions"]["keywords"],
        "skills": skills,
        "results": len(data["discussions"]["results"]),
        "result_post_ids": [r["postId"] for r in data["discussions"]["results"]],
        "mcp_intent": wc or wj,
        "join_intent": wj,
        "self_reported_marx_agent": marx_name,
        "key_like_text_redacted": key_redacted,
        "task_id": tid,
        "context_id": cid,
        "latency_ms": int((time.time() - t0) * 1000),
        "test": bool(meta.get("test")) or ip in ("127.0.0.1", "::1"),
    }
    log_event(ev)
    if legacy:
        task = {
            "kind": "task",
            "id": tid,
            "contextId": cid,
            "status": {"state": "completed", "timestamp": utc()},
            "artifacts": [{
                "artifactId": str(uuid.uuid4()),
                "name": "marx_research_connector_result",
                "parts": [{"kind": "text", "text": reply}, {"kind": "data", "data": data}],
            }],
            "history": [msg],
        }
        result = task
    else:
        task = {
            "id": tid,
            "contextId": cid,
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": utc()},
            "artifacts": [{
                "artifactId": str(uuid.uuid4()),
                "name": "marx_research_connector_result",
                "parts": [
                    {"text": reply, "mediaType": "text/plain"},
                    {"data": data, "mediaType": "application/json"},
                ],
            }],
            "history": [msg],
        }
        result = {"task": task}
    with LOCK:
        TASKS[tid] = task
        if len(TASKS) > 500:
            TASKS.pop(next(iter(TASKS)))
    return result


class RPCError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "MarxResearchConnector/0.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, code, obj, ctype="application/json"):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            log_event({
                "ts": now(),
                "event": "A2A_CARD_FETCH",
                "channel": "A2A Inbound",
                "path": path,
                "user_agent": (self.headers.get("User-Agent") or "")[:120],
                "test": self.client_address[0] in ("127.0.0.1", "::1"),
            })
            return self._send(200, card(path.endswith("/agent.json")))
        if path == "/healthz":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] not in ("/a2a", "/"):
            return self._send(404, {"error": "not found"})
        ip = self.client_address[0]
        with LOCK:
            window = [t for t in RATE.get(ip, []) if t > time.time() - 60]
            RATE[ip] = window + [time.time()]
            limited = len(window) >= 20
        rid = None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64000:
                raise RPCError(-32600, "request too large")
            if limited:
                raise RPCError(-32603, "rate limit: 20 requests/minute")
            req = json.loads(self.rfile.read(n) or b"{}")
            rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
            if req.get("jsonrpc") != "2.0" or not method:
                raise RPCError(-32600, "invalid JSON-RPC request")
            ver = (self.headers.get("A2A-Version") or "").strip()
            if ver and ver not in ("0.3", "1.0"):
                raise RPCError(-32009, f"A2A version {ver} not supported; use 1.0 or 0.3")
            if method in ("SendMessage", "message/send"):
                res = send_message(params, method == "message/send", self.headers.get("X-Agent-Name"), ip)
            elif method in ("GetTask", "tasks/get"):
                with LOCK:
                    task = TASKS.get(params.get("id"))
                if not task:
                    raise RPCError(-32001, "task not found")
                res = task
            elif method in ("CancelTask", "tasks/cancel"):
                raise RPCError(
                    -32002 if params.get("id") in TASKS else -32001,
                    "tasks complete synchronously and cannot be canceled",
                )
            elif method in ("SendStreamingMessage", "message/stream", "SubscribeToTask", "tasks/resubscribe"):
                raise RPCError(-32004, "streaming not supported (capabilities.streaming=false)")
            elif "PushNotification" in method or method.startswith("tasks/pushNotification"):
                raise RPCError(-32003, "push notifications not supported")
            elif method in ("GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard"):
                raise RPCError(-32004, "no extended agent card")
            elif method in ("ListTasks", "tasks/list"):
                raise RPCError(-32004, "ListTasks not supported (no client authentication; tasks are not listable)")
            else:
                raise RPCError(-32601, f"method not found: {method}")
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": res})
        except RPCError as e:
            self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.msg}})
        except json.JSONDecodeError:
            self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        except Exception:
            sys.stderr.write("internal error\n")
            self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "internal error"}})


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8787"))
    print(f"Marx Research Connector on http://{host}:{port}  card url={public_url()}", flush=True)
    Server((host, port), Handler).serve_forever()
