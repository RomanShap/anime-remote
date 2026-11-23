#!/usr/bin/env python3
"""Local stand-in for ntfy.sh, for tests: POST /<topic> publishes, GET /<topic>/sse subscribes.

Same wire format as ntfy (SSE "event: open" first, then "data: {json}" per message), CORS open.
GET /_stats returns how many messages were published, so tests can check the relay is used sparingly.
"""
import argparse
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
SUBS = {}          # topic -> set of queues
STATS = {"posts": 0, "bytes": 0, "by_topic": {}}


def cors(h):
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Headers", "*")
    h.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH, DELETE")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        cors(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/_stats":
            body = json.dumps(STATS).encode()
            self.send_response(200)
            cors(self)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.endswith("/sse"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        topic = path[1:-4]
        q = queue.Queue()
        with LOCK:
            SUBS.setdefault(topic, set()).add(q)
        try:
            self.send_response(200)
            cors(self)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            opened = {"id": secrets.token_hex(6), "time": int(time.time()), "event": "open", "topic": topic}
            self.wfile.write(("event: open\ndata: %s\n\n" % json.dumps(opened)).encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(("data: %s\n\n" % json.dumps(msg)).encode())
                except queue.Empty:
                    ka = {"id": secrets.token_hex(6), "time": int(time.time()), "event": "keepalive", "topic": topic}
                    self.wfile.write(("event: keepalive\ndata: %s\n\n" % json.dumps(ka)).encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            with LOCK:
                SUBS.get(topic, set()).discard(q)

    def do_POST(self):
        topic = self.path.split("?")[0].strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        msg = {"id": secrets.token_hex(6), "time": int(time.time()), "event": "message", "topic": topic, "message": body}
        with LOCK:
            STATS["posts"] += 1
            STATS["bytes"] += len(body)
            STATS["by_topic"][topic] = STATS["by_topic"].get(topic, 0) + 1
            for q in list(SUBS.get(topic, ())):
                q.put(msg)
        out = json.dumps(msg).encode()
        self.send_response(200)
        cors(self)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    print("mock relay on http://127.0.0.1:%d" % args.port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
