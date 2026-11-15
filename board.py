#!/usr/bin/env python3
"""The web board: board.html and a small JSON API on [board] host and port. See DESIGN.md "Web board".

It has no login. The network boundary is the access control: it binds loopback, and a private proxy such as
`tailscale serve` can expose it. It reads the registry and never writes it. The only Action is a prompt to the
planner pane through planner_chat.send().

  board.py      serve until stopped
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import config
import factory
import planner_chat
import registry
import status

PAGE = config.HERE / "board.html"
MAX_BODY = 16 * 1024
HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
           "Content-Security-Policy": "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'"}


class Bad(Exception):
    """A request that the server refuses: (code, message)."""


def metrics(con, pol, days):
    try:
        import metrics as m
    except ModuleNotFoundError as e:
        if e.name != "metrics":
            raise
        return []
    return m.compute(con, pol, days)


def api(method, path, query, handler):
    """The JSON reply for one API request. One registry connection for each request."""
    con = registry.connect()
    try:
        pol = handler.server.pol
        if method == "GET" and path == "/api/board":
            return status.board(con, pol, factory.agent_names())
        if method == "GET" and path == "/api/metrics":
            try:
                days = min(max(int(query.get("days", ["30"])[0]), 1), 366)
            except ValueError:
                raise Bad(400, "days must be a number")
            return metrics(con, pol, days)
        if method == "GET" and path == "/api/planner":
            return {"state": planner_chat.state(con),
                    "messages": planner_chat.messages(con, query.get("after", [None])[0])}
        if method == "POST" and path == "/api/planner/send":
            return planner_chat.send(con, handler.body()["text"])
        raise Bad(404, "not found")
    finally:
        con.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "techichi"
    sys_version = ""

    def log_message(self, *a):  # quiet: no access log
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        """The JSON body of a POST, after the guards that force a CORS preflight. The server never answers one."""
        h = self.headers
        if (h.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise Bad(415, "Content-Type must be application/json")
        if h.get("X-Techichi") != "1":
            raise Bad(403, "missing X-Techichi header")
        origin = h.get("Origin")
        if origin is not None and urlsplit(origin).netloc.lower() != (h.get("Host") or "").lower():
            raise Bad(403, "foreign Origin")
        try:
            n = int(h.get("Content-Length") or "")
        except ValueError:
            raise Bad(411, "Content-Length required")
        if n > MAX_BODY or n < 0:
            raise Bad(413, f"body over {MAX_BODY} bytes")
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            raise Bad(400, "body is not JSON")
        if not isinstance(d, dict) or not isinstance(d.get("text"), str):
            raise Bad(400, 'body must be {"text": "..."}')
        return d

    def serve(self, method):
        try:
            if (self.headers.get("Host") or "").lower() not in self.server.hosts:  # DNS rebinding
                raise Bad(403, "unknown Host")
            u = urlsplit(self.path)
            if method == "GET" and u.path == "/":
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return self.reply(200, api(method, u.path, parse_qs(u.query), self))
        except Bad as e:
            self.close_connection = True  # an unread body must not become the next request
            return self.reply(e.args[0], {"error": e.args[1]})
        except Exception as e:  # the type only, never a traceback. The server keeps running.
            sys.stderr.write(f"board: {method} {self.path}: {type(e).__name__}: {e}\n")
            return self.reply(500, {"error": type(e).__name__})

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")


def server(pol):
    """The server, bound and not yet serving. Port 0 picks a free port: the Host list then needs the real one."""
    s = ThreadingHTTPServer((pol["board"]["host"], pol["board"]["port"]), Handler)
    s.pol, s.hosts = pol, {h.lower() for h in pol["board"]["hosts"]}
    return s


if __name__ == "__main__":
    try:
        pol = config.policy()
    except config.PolicyError as e:
        sys.exit(str(e))
    s = server(pol)
    print(f"board: http://{pol['board']['host']}:{s.server_address[1]}/  hosts: {', '.join(sorted(s.hosts))}",
          flush=True)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
