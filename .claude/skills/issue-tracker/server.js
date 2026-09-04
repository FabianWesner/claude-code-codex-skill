#!/usr/bin/env node
// Board server for the issue tracker. View-only: it reads ~/.claude/issues and
// streams changes. Writes go through issues.js, which works with or without
// this process running.

const http = require('http');
const fs = require('fs');
const path = require('path');
const S = require('./store');

const PORT = Number(process.env.ISSUES_PORT || 2345);
const HOST = '127.0.0.1';

function snapshot() {
  const projects = S.listProjects();
  const now = Date.now();
  const boards = {};
  for (const p of projects) boards[p.slug] = S.buildBoard(p.slug, now);
  return { projects, boards, generatedAt: now, windowHours: S.RESOLVED_WINDOW_MS / 3600000 };
}

// --- change detection ------------------------------------------------------

const clients = new Set();
let lastSignature = '';
let debounce = null;

function signature(snap) {
  return JSON.stringify(
    snap.projects.map((p) => [
      p.slug,
      p.name,
      Object.entries(snap.boards[p.slug].columns).map(([k, v]) => [
        k,
        snap.boards[p.slug].hidden[k],
        v.map((i) => [i.id, i.status, i.title, i.type, i.size, i.resolvedAt]),
      ]),
    ]),
  );
}

function pushIfChanged() {
  let snap;
  try {
    snap = snapshot();
  } catch {
    return;
  }
  const sig = signature(snap);
  if (sig === lastSignature) return;
  lastSignature = sig;
  if (!clients.size) return;
  const payload = `data: ${JSON.stringify(snap)}\n\n`;
  for (const res of clients) {
    try {
      res.write(payload);
    } catch {
      clients.delete(res);
    }
  }
}

function scheduleCheck() {
  clearTimeout(debounce);
  debounce = setTimeout(pushIfChanged, 120);
}

function startWatching() {
  try {
    fs.mkdirSync(S.ISSUES_DIR, { recursive: true });
    fs.watch(S.ISSUES_DIR, { recursive: true }, scheduleCheck);
  } catch (err) {
    console.error(`[issues] fs.watch unavailable (${err.code || err.message}); polling only`);
  }
  // Also re-checks so issues age out of the 12h Done/Cancelled window on their
  // own, with no file change to trigger it.
  setInterval(pushIfChanged, 5000).unref?.();
}

// --- http ------------------------------------------------------------------

const HTML_PATH = path.join(__dirname, 'board.html');

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);

  if (url.pathname === '/api/board') {
    const snap = snapshot();
    const slug = url.searchParams.get('project');
    const column = url.searchParams.get('column');
    let body = snap;
    if (slug) {
      const board = snap.boards[S.slugify(slug)];
      if (!board) {
        res.writeHead(404, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: `unknown project "${slug}"`, projects: snap.projects.map((p) => p.slug) }));
        return;
      }
      body = column
        ? { project: slug, column, issues: board.columns[column] ?? [], hidden: board.hidden[column] ?? 0 }
        : { project: slug, ...board };
    }
    res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    res.end(JSON.stringify(body, null, 2));
    return;
  }

  if (url.pathname === '/events') {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store', connection: 'keep-alive' });
    res.write(`data: ${JSON.stringify(snapshot())}\n\n`);
    clients.add(res);
    const ping = setInterval(() => {
      try {
        res.write(': ping\n\n');
      } catch {
        /* closed */
      }
    }, 25000);
    req.on('close', () => {
      clearInterval(ping);
      clients.delete(res);
    });
    return;
  }

  if (url.pathname === '/') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
    res.end(fs.readFileSync(HTML_PATH, 'utf8'));
    return;
  }

  res.writeHead(404, { 'content-type': 'text/plain' });
  res.end('not found');
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`[issues] port ${PORT} is already in use — the board may already be running.`);
    process.exit(2);
  }
  throw err;
});

server.listen(PORT, HOST, () => {
  try {
    lastSignature = signature(snapshot());
  } catch {
    lastSignature = '';
  }
  startWatching();
  console.log(`[issues] http://${HOST}:${PORT}  (reading ${S.ISSUES_DIR})`);
});
