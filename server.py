#!/usr/bin/env python3
"""Local server for head-to-head Tic-Tac-Toe.

Serves the static game page and a tiny JSON API that holds one shared
game in memory. The first two browsers to join become X and O; anyone
after that is a spectator. Match state lives only in this process's
memory and resets when the server restarts; the win/loss/draw
leaderboard is persisted to leaderboard.json and survives restarts.
"""

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PORT = 8000
MOVE_TIME_LIMIT = 2.0  # seconds a player gets per move before one is picked for them
MAX_NAME_LEN = 24
LEADERBOARD_PATH = ROOT / "leaderboard.json"

LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]

STATE_LOCK = threading.Lock()
STATE = {
    "board": [None] * 9,
    "turn": "X",
    "starter": "X",  # who opened the current game, used to pick the next starter
    "over": False,
    "winner": None,   # 'X' | 'O' | 'D' | None
    "line": None,
    "players": {"X": None, "O": None},
    "names": {"X": None, "O": None},  # display name registered by each player
    "scores": {"X": 0, "O": 0, "D": 0},
    "version": 0,
    "deadline": None,  # epoch seconds when the current turn auto-fills; None if timer isn't running
}


def load_leaderboard():
    try:
        with open(LEADERBOARD_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Persistent per-name win/loss/draw record, survives server restarts (unlike
# STATE, which is only the current match). Written to disk on every finished
# game. Guarded by STATE_LOCK, same as everything else.
LEADERBOARD = load_leaderboard()


def save_leaderboard():
    with open(LEADERBOARD_PATH, "w") as f:
        json.dump(LEADERBOARD, f)


def bump_leaderboard(name, field):
    record = LEADERBOARD.setdefault(name, {"wins": 0, "losses": 0, "draws": 0})
    record[field] += 1


def record_result():
    """Update the leaderboard for the just-finished game. Assumes STATE_LOCK is held."""
    name_x = STATE["names"]["X"] or "X"
    name_o = STATE["names"]["O"] or "O"
    if STATE["winner"] == "D":
        bump_leaderboard(name_x, "draws")
        bump_leaderboard(name_o, "draws")
    elif STATE["winner"] == "X":
        bump_leaderboard(name_x, "wins")
        bump_leaderboard(name_o, "losses")
    elif STATE["winner"] == "O":
        bump_leaderboard(name_o, "wins")
        bump_leaderboard(name_x, "losses")
    save_leaderboard()


def leaderboard_rows():
    rows = []
    for name, r in LEADERBOARD.items():
        games = r["wins"] + r["losses"] + r["draws"]
        win_rate = r["wins"] / games if games else 0.0
        rows.append({"name": name, "wins": r["wins"], "losses": r["losses"], "draws": r["draws"], "win_rate": win_rate})
    rows.sort(key=lambda r: (-r["wins"], -r["win_rate"], r["name"].lower()))
    return rows


def other(mark):
    return "O" if mark == "X" else "X"


def start_deadline():
    STATE["deadline"] = time.time() + MOVE_TIME_LIMIT


def clear_deadline():
    STATE["deadline"] = None


def apply_mark(idx, mark):
    """Place a mark and resolve the game. Assumes STATE_LOCK is held."""
    STATE["board"][idx] = mark
    winner, line = check_win(STATE["board"])
    if winner:
        STATE["over"] = True
        STATE["winner"] = winner
        STATE["line"] = line
        STATE["scores"][winner] += 1
        clear_deadline()
        record_result()
    elif all(STATE["board"]):
        STATE["over"] = True
        STATE["winner"] = "D"
        STATE["scores"]["D"] += 1
        clear_deadline()
        record_result()
    else:
        STATE["turn"] = other(STATE["turn"])
        start_deadline()
    STATE["version"] += 1


def check_timeout():
    """Auto-play a random empty cell if the current turn's clock ran out. Assumes STATE_LOCK is held."""
    if STATE["over"] or STATE["deadline"] is None:
        return
    if time.time() >= STATE["deadline"]:
        empties = [i for i, v in enumerate(STATE["board"]) if v is None]
        if empties:
            apply_mark(random.choice(empties), STATE["turn"])


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
        check_timeout()
        time_left = None
        if not STATE["over"] and STATE["deadline"] is not None:
            time_left = max(0.0, STATE["deadline"] - time.time())
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
            "names": dict(STATE["names"]),
            "time_left": time_left,
            "time_limit": MOVE_TIME_LIMIT,
        }


def join(pid, name):
    name = (name or "").strip()[:MAX_NAME_LEN]
    with STATE_LOCK:
        check_timeout()
        if STATE["players"]["X"] != pid and STATE["players"]["O"] != pid:
            if STATE["players"]["X"] is None:
                STATE["players"]["X"] = pid
                STATE["names"]["X"] = name or "X"
                STATE["version"] += 1
            elif STATE["players"]["O"] is None:
                STATE["players"]["O"] = pid
                STATE["names"]["O"] = name or "O"
                STATE["version"] += 1
            if STATE["players"]["X"] and STATE["players"]["O"] and STATE["deadline"] is None and not STATE["over"]:
                start_deadline()
    return public_state(pid)


def move(pid, idx):
    with STATE_LOCK:
        check_timeout()
        role = role_for(pid)
        if role in ("X", "O") and not STATE["over"] and STATE["turn"] == role:
            if 0 <= idx <= 8 and STATE["board"][idx] is None:
                apply_mark(idx, role)
    return public_state(pid)


def new_game(pid):
    with STATE_LOCK:
        if STATE["winner"] in ("X", "O"):
            next_starter = other(STATE["winner"])  # loser opens the next game
        elif STATE["winner"] == "D":
            next_starter = other(STATE["starter"])  # no loser on a draw, alternate
        else:
            next_starter = STATE["starter"]
        STATE["board"] = [None] * 9
        STATE["turn"] = next_starter
        STATE["starter"] = next_starter
        STATE["over"] = False
        STATE["winner"] = None
        STATE["line"] = None
        STATE["version"] += 1
        if STATE["players"]["X"] and STATE["players"]["O"]:
            start_deadline()
        else:
            clear_deadline()
    return public_state(pid)


def reset_scores(pid):
    with STATE_LOCK:
        STATE["scores"] = {"X": 0, "O": 0, "D": 0}
        STATE["version"] += 1
    return public_state(pid)


def reset_leaderboard():
    with STATE_LOCK:
        LEADERBOARD.clear()
        save_leaderboard()


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
        if self.path.startswith("/api/leaderboard"):
            with STATE_LOCK:
                rows = leaderboard_rows()
            self._send_json({"rows": rows})
            return
        self._serve_static()

    def do_POST(self):
        if self.path == "/api/join":
            data = self._read_json()
            pid = data.get("pid")
            name = data.get("name")
            if not pid:
                self._send_json({"error": "missing pid"}, 400)
                return
            if not name or not str(name).strip():
                self._send_json({"error": "missing name"}, 400)
                return
            self._send_json(join(pid, name))
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
        if self.path == "/api/reset-leaderboard":
            reset_leaderboard()
            self._send_json({"rows": []})
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


def _timeout_watcher():
    while True:
        time.sleep(0.15)
        with STATE_LOCK:
            check_timeout()


if __name__ == "__main__":
    threading.Thread(target=_timeout_watcher, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving Tic-Tac-Toe on http://0.0.0.0:{PORT}")
    server.serve_forever()
