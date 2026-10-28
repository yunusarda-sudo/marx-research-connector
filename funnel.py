#!/usr/bin/env python3
"""Summarize the local A2A events log.

Reads A2A_EVENTS_PATH (default /tmp/a2a_events.jsonl). Events marked test:true
are excluded. Prints a JSON summary and does not contact Marx or write a file.
"""
import json
import os
import sys

PATH = os.environ.get("A2A_EVENTS_PATH") or "/tmp/a2a_events.jsonl"


def load(path):
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def who(event):
    return (event.get("caller_name") or "").lower() or event.get("context_id")


def main():
    events = load(PATH)
    live = [e for e in events if not e.get("test")]
    reqs = [e for e in live if e.get("event") == "A2A_REQUEST"]
    cards = [e for e in live if e.get("event") == "A2A_CARD_FETCH"]
    qual = {who(e) for e in reqs if e.get("results", 0) > 0 and (e.get("tickers") or e.get("keywords"))}
    intent = {who(e) for e in reqs if e.get("mcp_intent")}
    names = sorted({e["self_reported_marx_agent"] for e in reqs if e.get("self_reported_marx_agent")})
    out = {
        "events_path": PATH,
        "card_fetches": len(cards),
        "a2a_requests": len(reqs),
        "unique_requesters": len({who(e) for e in reqs}),
        "qualified": len(qual),
        "mcp_intent": len(intent),
        "self_reported_marx_agents": names,
        "excluded_test_events": len(events) - len(live),
    }
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
