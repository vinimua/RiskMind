"""Probe a running local API: real chat only, never confirm or start a case."""
import os
import sys
from uuid import uuid4
import httpx


def main():
    token = os.environ.get("RISK_LOCAL_API_TOKEN", "").strip()
    if not token:
        print("chat_http_probe: FAIL (RISK_LOCAL_API_TOKEN is required)", file=sys.stderr)
        return 1
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=150, trust_env=False,
                          headers={"Authorization": "Bearer " + token}) as client:
            response = client.post("/chat/messages", json={
                "request_id": "http-smoke-" + uuid4().hex,
                "message": "Explain PSI briefly. This is only a question; do not start an investigation."})
            if response.status_code != 200:
                print(f"chat_http_probe: FAIL (HTTP {response.status_code})", file=sys.stderr)
                return 1
            view = response.json()
            if view.get("status") != "ANSWERED" or view.get("result", {}).get("status") != "SUCCEEDED":
                raise ValueError("Unexpected response")
            inspected = client.get("/chat/turns/" + view["turn_id"])
            if inspected.status_code != 200 or inspected.json() != view:
                raise ValueError("Saved response mismatch")
        print("chat_http_probe: PASS (answer persisted and read back; no case started)")
        return 0
    except Exception as error:
        print(f"chat_http_probe: FAIL ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
