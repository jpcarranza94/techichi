"""Check for the web board: each endpoint, the Host check, each POST guard, the error path. No herdr: the planner send
is a stub. Run: python3 test_board.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import contextlib
import http.client
import io
import json
import sys
import threading
import types

import board
import config
import factory
import herdr
import planner_chat
import registry
import status


def no_herdr(*a, **k):
    raise AssertionError("the board test reached herdr")


herdr.call = herdr.run = no_herdr
factory.agent_names = lambda: {"s1": "app-1-worker"}
sent = []
planner_chat.send = lambda con, text: sent.append(text) or {"ok": True}

# The defaults: loopback, port 8377, and the Host list follows the port.
pol = config.policy()
assert (pol["board"]["host"], pol["board"]["port"]) == ("127.0.0.1", 8377)
assert pol["board"]["hosts"] == ["127.0.0.1:8377", "localhost:8377"]
assert config.complete({**{k: v for k, v in pol.items() if k != "board"}, "board": {"port": 9000}})["board"]["hosts"] \
    == ["127.0.0.1:9000", "localhost:9000"]

con = registry.connect()
con.execute("INSERT INTO sessions (session_id, role, cwd, pane_id, agent_status, state) "
            "VALUES ('s1', 'worker', '/w', 'w1:p1', 'blocked', 'live')")
con.execute("INSERT INTO links (session_id, kind, ref, created_by) VALUES ('s1', 'ticket', 'APP-1', 'claim')")

pol["board"]["port"] = 0
srv = board.server(pol)
port = srv.server_address[1]
HOST = f"127.0.0.1:{port}"
srv.hosts = {HOST.lower(), f"localhost:{port}"}
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(method, path, body=None, host=HOST, **headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers={"Host": host, **headers})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def post(text="Plan APP-1 first.", **headers):
    h = {"Content-Type": "application/json", "X-Techichi": "1", **headers}
    return req("POST", "/api/planner/send", json.dumps({"text": text}), **h)


# The page, from disk, with the security headers and no CORS header.
code, h, page = req("GET", "/")
assert code == 200 and h["Content-Type"].startswith("text/html") and b"<title>" in page
assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
assert not [k for k in h if k.lower().startswith("access-control")], h

# The board: the dict of status.board, with the names from factory.agent_names.
code, h, data = req("GET", "/api/board")
b = json.loads(data)
assert code == 200 and h["Cache-Control"] == "no-store" and h["Content-Type"] == "application/json"
you = b["groups"][0]
assert you["name"] == "YOU" and you["rows"][0]["name"] == "app-1-worker" and you["rows"][0]["ticket"] == "APP-1", b

# Metrics: the days go to compute(). A missing module gives [].
calls = []
sys.modules["metrics"] = types.SimpleNamespace(compute=lambda c, p, days: calls.append(days) or [{"id": "x"}])
assert json.loads(req("GET", "/api/metrics?days=7")[2]) == [{"id": "x"}] and calls == [7]
req("GET", "/api/metrics")
assert calls == [7, 30]
assert req("GET", "/api/metrics?days=abc")[0] == 400
sys.modules["metrics"] = None  # import raises ModuleNotFoundError, as with no metrics.py
assert json.loads(req("GET", "/api/metrics")[2]) == []

# The planner: state and messages. No planner Session here.
code, _, data = req("GET", "/api/planner?after=abc")
assert code == 200 and json.loads(data) == {"state": {"present": False, "agent_status": None, "session_id": None},
                                            "messages": []}

# Host check: a foreign or missing Host gets 403, also on the page (DNS rebinding).
assert req("GET", "/", host="evil.example")[0] == 403
assert req("GET", "/api/board", host=f"evil.example:{port}")[0] == 403
assert post(host="evil.example")[0] == 403
assert req("GET", "/", host=f"LOCALHOST:{port}")[0] == 200

# The send: it reaches planner_chat.send with the text. A same-site Origin passes.
code, h, data = post(Origin=f"http://{HOST}")
assert code == 200 and json.loads(data) == {"ok": True} and sent == ["Plan APP-1 first."], (code, data)
assert h["Cache-Control"] == "no-store" and not [k for k in h if k.lower().startswith("access-control")]

# Each POST guard. None of these reaches send.
assert post(**{"X-Techichi": ""})[0] == 403
assert req("POST", "/api/planner/send", json.dumps({"text": "x"}), **{"Content-Type": "application/json"})[0] == 403
assert post(**{"Content-Type": "text/plain"})[0] == 415
assert post(**{"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
assert post(Origin="https://evil.example")[0] == 403
assert post(Origin="null")[0] == 403
assert post("x" * (board.MAX_BODY + 10))[0] == 413
assert req("POST", "/api/planner/send", "not json", **{"Content-Type": "application/json", "X-Techichi": "1"})[0] == 400
assert req("POST", "/api/planner/send", "[1]", **{"Content-Type": "application/json", "X-Techichi": "1"})[0] == 400
assert req("OPTIONS", "/api/planner/send", Origin="https://evil.example")[0] == 501  # no preflight answer
assert req("GET", "/api/planner/send")[0] == 404 and req("POST", "/api/board", "{}")[0] == 404
assert sent == ["Plan APP-1 first."], sent

# The error path: the exception type, no traceback, and the server keeps serving.
real = status.board
status.board = lambda *a: 1 / 0
with contextlib.redirect_stderr(io.StringIO()) as err:
    code, _, data = req("GET", "/api/board")
assert "ZeroDivisionError" in err.getvalue() and "Traceback" not in err.getvalue()
assert code == 500 and json.loads(data) == {"error": "ZeroDivisionError"} and b"Traceback" not in data
status.board = real
assert req("GET", "/api/board")[0] == 200

srv.shutdown()
srv.server_close()
print("ok")
