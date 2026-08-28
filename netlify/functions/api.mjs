// Serverless backend for head-to-head Tic-Tac-Toe on Netlify.
// Mirrors server.py's logic, but state lives in Netlify Blobs instead of
// process memory, since functions are stateless between invocations.

import { getStore } from '@netlify/blobs';

const LINES = [
  [0, 1, 2], [3, 4, 5], [6, 7, 8],
  [0, 3, 6], [1, 4, 7], [2, 5, 8],
  [0, 4, 8], [2, 4, 6],
];
const MOVE_TIME_LIMIT = 2; // seconds
const MAX_NAME_LEN = 24;
const STATE_KEY = 'state';
const LEADERBOARD_KEY = 'leaderboard';

function defaultState() {
  return {
    board: Array(9).fill(null),
    turn: 'X',
    starter: 'X',
    over: false,
    winner: null,
    line: null,
    players: { X: null, O: null },
    names: { X: null, O: null }, // display name registered by each player
    scores: { X: 0, O: 0, D: 0 },
    version: 0,
    deadline: null, // epoch ms when the current turn auto-fills; null if timer isn't running
  };
}

function other(mark) {
  return mark === 'X' ? 'O' : 'X';
}

function checkWin(board) {
  for (const line of LINES) {
    const [a, b, c] = line;
    if (board[a] && board[a] === board[b] && board[b] === board[c]) {
      return { winner: board[a], line };
    }
  }
  return { winner: null, line: null };
}

function roleFor(state, pid) {
  if (state.players.X === pid) return 'X';
  if (state.players.O === pid) return 'O';
  return 'spectator';
}

function startDeadline(state) {
  state.deadline = Date.now() + MOVE_TIME_LIMIT * 1000;
}

function clearDeadline(state) {
  state.deadline = null;
}

function applyMark(state, idx, mark) {
  state.board[idx] = mark;
  const { winner, line } = checkWin(state.board);
  if (winner) {
    state.over = true;
    state.winner = winner;
    state.line = line;
    state.scores[winner] += 1;
    clearDeadline(state);
  } else if (state.board.every((v) => v)) {
    state.over = true;
    state.winner = 'D';
    state.scores.D += 1;
    clearDeadline(state);
  } else {
    state.turn = other(state.turn);
    startDeadline(state);
  }
  state.version += 1;
}

function checkTimeout(state) {
  if (state.over || state.deadline == null) return;
  if (Date.now() >= state.deadline) {
    const empties = [];
    state.board.forEach((v, i) => { if (!v) empties.push(i); });
    if (empties.length) {
      const idx = empties[Math.floor(Math.random() * empties.length)];
      applyMark(state, idx, state.turn);
    }
  }
}

function publicState(state, pid) {
  const timeLeft = !state.over && state.deadline != null
    ? Math.max(0, (state.deadline - Date.now()) / 1000)
    : null;
  return {
    board: state.board,
    turn: state.turn,
    over: state.over,
    winner: state.winner,
    line: state.line,
    scores: state.scores,
    version: state.version,
    role: roleFor(state, pid),
    players: { X: !!state.players.X, O: !!state.players.O },
    names: { X: state.names.X, O: state.names.O },
    time_left: timeLeft,
    time_limit: MOVE_TIME_LIMIT,
  };
}

async function loadState(store) {
  const raw = await store.get(STATE_KEY, { type: 'json' });
  if (!raw) return defaultState();
  if (!raw.names) raw.names = { X: null, O: null }; // tolerate state saved before names existed
  return raw;
}

async function saveState(store, state) {
  await store.setJSON(STATE_KEY, state);
}

// Persistent per-name win/loss/draw record, kept in its own blob so it
// survives reset-all (which only wipes the current match's STATE_KEY blob).
async function loadLeaderboard(store) {
  const raw = await store.get(LEADERBOARD_KEY, { type: 'json' });
  return raw || {};
}

async function saveLeaderboard(store, board) {
  await store.setJSON(LEADERBOARD_KEY, board);
}

function bumpLeaderboard(board, name, field) {
  if (!board[name]) board[name] = { wins: 0, losses: 0, draws: 0 };
  board[name][field] += 1;
}

async function recordResult(store, state) {
  const nameX = state.names.X || 'X';
  const nameO = state.names.O || 'O';
  const board = await loadLeaderboard(store);
  if (state.winner === 'D') {
    bumpLeaderboard(board, nameX, 'draws');
    bumpLeaderboard(board, nameO, 'draws');
  } else if (state.winner === 'X') {
    bumpLeaderboard(board, nameX, 'wins');
    bumpLeaderboard(board, nameO, 'losses');
  } else if (state.winner === 'O') {
    bumpLeaderboard(board, nameO, 'wins');
    bumpLeaderboard(board, nameX, 'losses');
  }
  await saveLeaderboard(store, board);
}

function leaderboardRows(board) {
  const rows = Object.entries(board).map(([name, r]) => {
    const games = r.wins + r.losses + r.draws;
    return { name, wins: r.wins, losses: r.losses, draws: r.draws, win_rate: games ? r.wins / games : 0 };
  });
  rows.sort((a, b) => b.wins - a.wins || b.win_rate - a.win_rate || a.name.localeCompare(b.name));
  return rows;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });
}

export default async (req) => {
  // @netlify/blobs defaults to eventual consistency, which lets a request
  // read a stale snapshot, mutate it, and write it back — silently
  // reverting more-recent state (e.g. a just-joined player disappearing).
  // This is a single small JSON blob read on every request, so the latency
  // cost of strong consistency is negligible.
  const store = getStore({ name: 'tictactoe', consistency: 'strong' });
  const url = new URL(req.url);
  const route = url.pathname.replace(/^\/api\/?/, '');

  if (req.method === 'GET' && route === 'leaderboard') {
    const board = await loadLeaderboard(store);
    return json({ rows: leaderboardRows(board) });
  }

  if (req.method === 'GET' && route === 'state') {
    const pid = url.searchParams.get('pid');
    if (!pid) return json({ error: 'missing pid' }, 400);
    const state = await loadState(store);
    const wasOver = state.over;
    checkTimeout(state);
    if (!wasOver && state.over) await recordResult(store, state);
    await saveState(store, state);
    return json(publicState(state, pid));
  }

  if (req.method === 'POST') {
    let body = {};
    try { body = await req.json(); } catch { /* empty body */ }
    const pid = body.pid;

    if (route === 'join') {
      const name = typeof body.name === 'string' ? body.name.trim().slice(0, MAX_NAME_LEN) : '';
      if (!pid) return json({ error: 'missing pid' }, 400);
      if (!name) return json({ error: 'missing name' }, 400);
      const state = await loadState(store);
      const wasOver = state.over;
      checkTimeout(state);
      if (!wasOver && state.over) await recordResult(store, state);
      if (state.players.X !== pid && state.players.O !== pid) {
        if (!state.players.X) { state.players.X = pid; state.names.X = name; state.version += 1; }
        else if (!state.players.O) { state.players.O = pid; state.names.O = name; state.version += 1; }
        if (state.players.X && state.players.O && state.deadline == null && !state.over) {
          startDeadline(state);
        }
      }
      await saveState(store, state);
      return json(publicState(state, pid));
    }

    if (route === 'move') {
      const idx = body.index;
      if (!pid || typeof idx !== 'number') return json({ error: 'bad request' }, 400);
      const state = await loadState(store);
      const wasOver = state.over;
      checkTimeout(state);
      if (!wasOver && state.over) {
        await recordResult(store, state);
      } else if (!state.over) {
        const role = roleFor(state, pid);
        if ((role === 'X' || role === 'O') && role === state.turn
          && idx >= 0 && idx <= 8 && state.board[idx] == null) {
          applyMark(state, idx, role);
          if (state.over) await recordResult(store, state);
        }
      }
      await saveState(store, state);
      return json(publicState(state, pid));
    }

    if (route === 'new-game') {
      const state = await loadState(store);
      let nextStarter;
      if (state.winner === 'X' || state.winner === 'O') nextStarter = other(state.winner);
      else if (state.winner === 'D') nextStarter = other(state.starter);
      else nextStarter = state.starter;
      state.board = Array(9).fill(null);
      state.turn = nextStarter;
      state.starter = nextStarter;
      state.over = false;
      state.winner = null;
      state.line = null;
      state.version += 1;
      if (state.players.X && state.players.O) startDeadline(state); else clearDeadline(state);
      await saveState(store, state);
      return json(publicState(state, pid));
    }

    if (route === 'reset-scores') {
      const state = await loadState(store);
      state.scores = { X: 0, O: 0, D: 0 };
      state.version += 1;
      await saveState(store, state);
      return json(publicState(state, pid));
    }

    if (route === 'reset-leaderboard') {
      await saveLeaderboard(store, {});
      return json({ rows: [] });
    }

    if (route === 'reset-all') {
      // Functions never restart the way the local server.py process does,
      // so this replicates a server restart: wipe board, players, and scores.
      const state = defaultState();
      await saveState(store, state);
      return json(publicState(state, pid));
    }
  }

  return json({ error: 'not found' }, 404);
};

export const config = { path: '/api/*' };
