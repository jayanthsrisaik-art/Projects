"""End-to-end demo: a local flaky receiver that verifies signatures, and the real httpx transport.

Run: python demo.py   (prints the delivery log; exits non-zero if the outcome is unexpected)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import store, worker
from app.signing import verify

SECRET = "demo-secret-123"
received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ok = verify(SECRET, body, self.headers["X-Signature"], now=int(self.headers["X-Signature"].split(",")[0][2:]))
        received.append(ok)
        # fail the first request with 503 to exercise a retry
        self.send_response(503 if len(received) == 1 else (200 if ok else 401))
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    conn = store.connect()
    store.add_endpoint(conn, url, SECRET, ["order.created"])
    store.enqueue_event(conn, "order-1", "order.created", {"order_id": 1, "total": 42.5}, now=0)
    policy = worker.RetryPolicy(base_delay=30)
    for now in (0, 30):  # simulated clock; real HTTP
        print(f"t={now}", worker.run_once(conn, now, policy=policy))
    row = dict(store.list_deliveries(conn)[0])
    print(json.dumps({k: row[k] for k in ("status", "attempts", "last_status_code")}))
    server.shutdown()
    assert row["status"] == "delivered" and row["attempts"] == 2 and received == [True, True]


if __name__ == "__main__":
    main()
