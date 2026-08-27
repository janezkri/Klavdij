#!/usr/bin/env python3
"""Local server for head-to-head Tic-Tac-Toe.

Serves the static game page and a tiny JSON API that holds one shared
game in memory. The first two browsers to join become X and O; anyone
after that is a spectator. State lives only in this process's memory
and resets when the server restarts.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PORT = 8000

LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]

STATE_LOCK = threading.Lock()
STATE = {
    "board": [None] * 9,
    "turn": "X",
    "over": False,
    "winner": None,   # 'X' | 'O' | 'D' | None
    "line": None,
    "players": {"X": None, "O": None},
    "scores": {"X": 0, "O": 0, "D": 0},
    "version": 0,
}


def check_win(board):
    for line in LINES:
        a, b, c = board[line[0]], board[line[1]], board[line[2]]
        if a and a == b == c:
            return a, line
    return None, None


def role_for(pid):
    if STATE["players"]["X"] == pid:
        return "X"
    if STATE["players"]["O"] == pid:
        return "O"
    return "spectator"


def public_state(pid):
    with STATE_LOCK:
        return {
            "board": STATE["board"],
            "turn": STATE["turn"],
            "over": STATE["over"],
            "winner": STATE["winner"],
            "line": STATE["line"],
            "scores": STATE["scores"],
            "version": STATE["version"],
            "role": role_for(pid),
            "players": {
                "X": STATE["players"]["X"] is not None,
                "O": STATE["players"]["O"] is not None,
            },
        }


def join(pid):
    with STATE_LOCK:
        if STATE["players"]["X"] != pid and STATE["players"]["O"] != pid:
            if STATE["players"]["X"] is None:
                STATE["players"]["X"] = pid
                STATE["version"] += 1
            elif STATE["players"]["O"] is None:
                STATE["players"]["O"] = pid
                STATE["version"] += 1
    return public_state(pid)


def move(pid, idx):
    with STATE_LOCK:
        role = role_for(pid)
        if role in ("X", "O") and not STATE["over"] and STATE["turn"] == role:
            if 0 <= idx <= 8 and STATE["board"][idx] is None:
                STATE["board"][idx] = role
                winner, line = check_win(STATE["board"])
                if winner:
                    STATE["over"] = True
                    STATE["winner"] = winner
                    STATE["line"] = line
                    STATE["scores"][winner] += 1
                elif all(STATE["board"]):
                    STATE["over"] = True
                    STATE["winner"] = "D"
                    STATE["scores"]["D"] += 1
                else:
                    STATE["turn"] = "O" if STATE["turn"] == "X" else "X"
                STATE["version"] += 1
    return public_state(pid)


def new_game(pid):
    with STATE_LOCK:
        STATE["board"] = [None] * 9
        STATE["turn"] = "X"
        STATE["over"] = False
        STATE["winner"] = None
        STATE["line"] = None
        STATE["version"] += 1
    return public_state(pid)


def reset_scores(pid):
    with STATE_LOCK:
        STATE["scores"] = {"X": 0, "O": 0, "D": 0}
        STATE["version"] += 1
    return public_state(pid)


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _pid_from_query(self):
        if "?" not in self.path:
            return None
        qs = self.path.split("?", 1)[1]
        for part in qs.split("&"):
            if part.startswith("pid="):
                return part[len("pid="):]
        return None

    def do_GET(self):
        if self.path.startswith("/api/state"):
            pid = self._pid_from_query()
            if not pid:
                self._send_json({"error": "missing pid"}, 400)
            else:
                self._send_json(public_state(pid))
            return
        self._serve_static()

    def do_POST(self):
        if self.path == "/api/join":
            pid = self._read_json().get("pid")
            if not pid:
                self._send_json({"error": "missing pid"}, 400)
                return
            self._send_json(join(pid))
            return
        if self.path == "/api/move":
            data = self._read_json()
            pid = data.get("pid")
            idx = data.get("index")
            if not pid or not isinstance(idx, int):
                self._send_json({"error": "bad request"}, 400)
                return
            self._send_json(move(pid, idx))
            return
        if self.path == "/api/new-game":
            self._send_json(new_game(self._read_json().get("pid")))
            return
        if self.path == "/api/reset-scores":
            self._send_json(reset_scores(self._read_json().get("pid")))
            return
        self.send_response(404)
        self.end_headers()

    def _serve_static(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/tictactoe.html"
        candidate = (ROOT / path.lstrip("/")).resolve()
        if candidate != ROOT and ROOT not in candidate.parents:
            self.send_response(403)
            self.end_headers()
            return
        if not candidate.is_file():
            self.send_response(404)
            self.end_headers()
            return
        content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving Tic-Tac-Toe on http://0.0.0.0:{PORT}")
    server.serve_forever()
