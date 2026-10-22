"""Drive one chat turn on a LOCAL main agent the way the proxy turn gate will (projects-access task 10.2):
proxy-authored `__turn_context` in the body, the Project_Token only in the ProjectToken header.

  uv run python scripts/local_search_turn.py --project p<32hex> "where is someone on a boat?"
  options: --collection <id> (lie about collection_id in the turn context, to show the Cedar denial)
           --no-token (project_access unavailable path)   --exp-in -60 (expired token)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import uuid

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import local_search_gateway as L  # noqa: E402

HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--project", required=True)
    ap.add_argument("--name", default="Launch Q3")
    ap.add_argument("--collection")
    ap.add_argument("--no-token", action="store_true")
    ap.add_argument("--exp-in", type=int, default=3600)
    ap.add_argument("--url", default="http://127.0.0.1:8080/invocations")
    ap.add_argument("--arm", choices=("A", "B"), default="A")
    a = ap.parse_args()
    sid = f"local-search-demo-{uuid.uuid4()}"
    body = {"prompt": a.prompt, "session_id": sid, "__actor_id": "local-dev-user",
            "__turn_context": {"turn_id": f"t-{uuid.uuid4().hex[:12]}",
                               "active_project": {"id": a.project, "name": a.name,
                                                  "collection_id": a.project, "personal": False},
                               "web_search": False,
                               "project_access": "unavailable" if a.no_token else "ok", "search_arm": a.arm}}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
               "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid}
    if not a.no_token:
        headers[HEADER] = L.mint(a.project, "local-dev-user", a.exp_in)
    if a.collection:  # simulate a model that ignores the turn context: tell it a different collection
        body["prompt"] += f"\n\n(Use collection_id {a.collection} for the search.)"
    req = urllib.request.Request(a.url, data=json.dumps(body).encode(), headers=headers, method="POST")
    text = []
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                v = json.loads(data)
            except ValueError:
                continue
            if isinstance(v, str):
                try:
                    v2 = json.loads(v)
                except ValueError:
                    v2 = None
                if isinstance(v2, dict) and v2.get("type") == "step":
                    print(f"  [step] {v2.get('status')}: {v2.get('label')}", flush=True)
                    continue
                text.append(v)
            elif isinstance(v, dict) and v.get("type") == "step":
                print(f"  [step] {v.get('status')}: {v.get('label')}", flush=True)
    print("\n--- answer ---\n" + "".join(text))


if __name__ == "__main__":
    main()
