#!/usr/bin/env node
// CLI for the issue tracker. Writes the store directly, so it works whether or
// not the board server is running.

const path = require('path');
const fs = require('fs');
const net = require('net');
const { spawn } = require('child_process');
const S = require('./store');

const ARGV = process.argv.slice(2);
const PORT = Number(process.env.ISSUES_PORT || 2345);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function portOpen(port, timeout = 250) {
  return new Promise((resolve) => {
    const sock = net.connect({ port, host: '127.0.0.1' });
    const done = (v) => {
      sock.destroy();
      resolve(v);
    };
    sock.setTimeout(timeout);
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
  });
}

// The board should exist whenever the skill is used, so every command brings it
// up first. Detached + unref means it outlives this process and the Claude turn
// that spawned it. A race between two CLI calls is harmless: the loser hits
// EADDRINUSE and exits 2.
async function ensureServer() {
  if (process.env.ISSUES_NO_AUTOSTART === '1' || flags['no-serve']) return null;
  if (await portOpen(PORT)) return null;

  let out = 'ignore';
  try {
    fs.mkdirSync(S.ISSUES_DIR, { recursive: true });
    out = fs.openSync(path.join(S.ISSUES_DIR, '.server.log'), 'a');
  } catch {
    /* logging is best-effort */
  }
  spawn(process.execPath, [path.join(__dirname, 'server.js')], {
    detached: true,
    stdio: ['ignore', out, out],
  }).unref();

  for (let i = 0; i < 30; i++) {
    if (await portOpen(PORT, 120)) return `http://127.0.0.1:${PORT}`;
    await sleep(60);
  }
  return null; // couldn't bind; the command still ran, see .server.log
}

function parseArgs(args) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq !== -1) flags[a.slice(2, eq)] = a.slice(eq + 1);
      else if (args[i + 1] && !args[i + 1].startsWith('--')) flags[a.slice(2)] = args[++i];
      else flags[a.slice(2)] = true;
    } else positional.push(a);
  }
  return { flags, positional };
}

const { flags, positional } = parseArgs(ARGV);
const cmd = (positional.shift() || 'list').toLowerCase();

function fail(msg) {
  console.error(`error: ${msg}`);
  process.exit(1);
}

function normType(v) {
  if (!v) return 'feature';
  const s = String(v).toLowerCase();
  if (s.startsWith('b')) return 'bug';
  if (s.startsWith('f')) return 'feature';
  fail(`--type must be bug or feature (got "${v}")`);
}

function normSize(v) {
  if (!v) return 'Medium';
  const s = String(v).toLowerCase();
  if (s.startsWith('s')) return 'Small';
  if (s.startsWith('m')) return 'Medium';
  if (s.startsWith('b') || s.startsWith('l')) return 'Big';
  fail(`--size must be Small, Medium or Big (got "${v}")`);
}

const project = () => S.resolveProject(flags.project);

function relTime(iso) {
  if (!iso) return '';
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  const m = Math.round(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

const COLUMN_LABELS = { open: 'Open', planned: 'Planned', in_progress: 'In Progress', deployed: 'Deployed', done: 'Done', cancelled: 'Cancelled' };

function line(issue) {
  const type = issue.type === 'bug' ? 'bug' : 'feat';
  const flag = issue.flagged ? `  \u2691 ON HOLD — ${issue.flagReason}` : '';
  const cancelReason = issue.status === 'cancelled' && issue.reason ? `  — ${issue.reason}` : '';
  return `  ${issue.number}  [${type}/${issue.size.toLowerCase()}]  ${issue.title}${cancelReason}${flag}`;
}

function printColumn(board, key, p) {
  const items = board.columns[key];
  const hid = board.hidden[key];
  const suffix = hid ? `  (${hid} older than ${board.windowHours}h hidden)` : '';
  console.log(`${COLUMN_LABELS[key]} (${items.length})${suffix}`);
  if (!items.length) console.log('  —');
  for (const i of items) console.log(line(i));
}

function resolveId(raw) {
  const n = Number(String(raw).replace(/^#/, ''));
  if (!Number.isFinite(n) || n < 1) fail(`expected an issue number like 3 or #03 (got "${raw}")`);
  return n;
}

function transition(status, verb) {
  const p = project();
  const id = resolveId(positional[0] ?? fail(`usage: issues.js ${cmd} <number>`));
  const patch = { status };
  if (status === 'cancelled' && typeof flags.reason === 'string') patch.reason = flags.reason;
  const updated = S.updateIssue(p.slug, id, patch);
  if (!updated) fail(`${S.num(id)} not found in project "${p.name}"`);
  console.log(`${updated.number} ${verb}: ${updated.title}`);
}

function run() {
    switch (cmd) {
    case 'add': {
      const title = positional.join(' ').trim() || (typeof flags.title === 'string' ? flags.title : '');
      if (!title) fail('usage: issues.js add "Brief title" --type bug|feature --size Small|Medium|Big');
      const p = project();
      const issue = S.createIssue(p, {
        title,
        type: normType(flags.type),
        size: normSize(flags.size),
        notes: typeof flags.notes === 'string' ? flags.notes : '',
      });
      console.log(`${issue.number} opened in ${p.name}: ${issue.title}  [${issue.type}/${issue.size}]`);
      break;
    }

    case 'plan':
      transition('planned', 'planned');
      break;

    case 'start':
      transition('in_progress', 'in progress');
      break;
    case 'done':
    case 'close':
      transition('done', 'done');
      break;
    case 'cancel':
      transition('cancelled', 'cancelled');
      break;
    case 'reopen':
      transition('open', 'reopened');
      break;

    case 'flag': {
      const p = project();
      const id = resolveId(positional[0] ?? fail('usage: issues.js flag <number> --reason "..."'));
      const reason = typeof flags.reason === 'string' ? flags.reason.trim() : positional.slice(1).join(' ').trim();
      if (!reason) fail('flag needs a reason: issues.js flag <number> --reason "..."');
      const current = S.readIssue(p.slug, id);
      if (!current) fail(`${S.num(id)} not found in project "${p.name}"`);
      if (['done', 'cancelled'].includes(current.status)) fail(`${S.num(id)} is already ${current.status} — nothing to flag`);
      const updated = S.updateIssue(p.slug, id, { flagged: true, flagReason: reason });
      console.log(`${updated.number} flagged: ${updated.title}\n  on hold — ${updated.flagReason}`);
      break;
    }

    case 'unflag': {
      const p = project();
      const id = resolveId(positional[0] ?? fail('usage: issues.js unflag <number>'));
      const updated = S.updateIssue(p.slug, id, { flagged: false });
      if (!updated) fail(`${S.num(id)} not found in project "${p.name}"`);
      console.log(`${updated.number} unflagged: ${updated.title}`);
      break;
    }

    case 'deploy':
      transition('deployed', 'deployed');
      break;

    case 'undeploy':
      transition('in_progress', 'back in progress');
      break;

    case 'edit': {
      const p = project();
      const id = resolveId(positional[0] ?? fail('usage: issues.js edit <number> [--title ...] [--type ...] [--size ...]'));
      const patch = {};
      if (typeof flags.title === 'string') patch.title = flags.title;
      if (flags.type) patch.type = normType(flags.type);
      if (flags.size) patch.size = normSize(flags.size);
      if (typeof flags.notes === 'string') patch.notes = flags.notes;
      if (!Object.keys(patch).length) fail('nothing to change');
      const updated = S.updateIssue(p.slug, id, patch);
      if (!updated) fail(`${S.num(id)} not found in project "${p.name}"`);
      console.log(`${updated.number} updated: ${updated.title}  [${updated.type}/${updated.size}]`);
      break;
    }

    case 'show': {
      const p = project();
      const id = resolveId(positional[0] ?? fail('usage: issues.js show <number>'));
      const issue = S.readIssue(p.slug, id);
      if (!issue) fail(`${S.num(id)} not found in project "${p.name}"`);
      if (flags.json) {
        console.log(JSON.stringify(issue, null, 2));
        break;
      }
      console.log(`${issue.number}  ${issue.title}`);
      console.log(`  project   ${p.name}`);
      console.log(`  type      ${issue.type}`);
      console.log(`  size      ${issue.size}`);
      console.log(`  status    ${COLUMN_LABELS[issue.status]}${issue.flagged ? '  \u2691 ON HOLD' : ''}`);
      console.log(`  created   ${relTime(issue.createdAt)}`);
      if (issue.resolvedAt) console.log(`  resolved  ${relTime(issue.resolvedAt)}`);
      if (issue.flagged) console.log(`  on hold   since ${relTime(issue.flaggedAt)} — ${issue.flagReason}`);
      if (issue.reason) console.log(`  reason    ${issue.reason}`);
      if (issue.notes) console.log(`  notes     ${issue.notes}`);
      break;
    }

    case 'list':
    case 'board': {
      const p = project();
      const board = S.buildBoard(p.slug);
      const only = positional[0] ? String(positional[0]).toLowerCase().replace(/[\s-]/g, '_') : null;
      if (only && !COLUMN_LABELS[only]) fail(`unknown column "${positional[0]}" (open, planned, in_progress, deployed, done, cancelled)`);

      if (flags.json) {
        console.log(JSON.stringify(only ? { column: only, issues: board.columns[only], hidden: board.hidden[only] } : { project: p, ...board }, null, 2));
        break;
      }

      console.log(`${p.name}${p.path ? `  (${p.path})` : ''}`);
      console.log('');
      for (const key of only ? [only] : ['open', 'planned', 'in_progress', 'deployed', 'done', 'cancelled']) {
        printColumn(board, key, p);
        console.log('');
      }
      break;
    }

    case 'projects': {
      const all = S.listProjects();
      if (flags.json) {
        console.log(JSON.stringify(all, null, 2));
        break;
      }
      if (!all.length) {
        console.log('No projects yet. `issues.js add "..."` creates one from the current directory.');
        break;
      }
      const here = S.detectProject().slug;
      for (const p of all) {
        console.log(`${p.slug === here ? '*' : ' '} ${p.name.padEnd(24)} ${String(p.open).padStart(3)} open / ${String(p.total).padStart(3)} total   ${p.path || ''}`);
      }
      break;
    }

    case 'serve':
      process.argv = [process.argv[0], path.join(__dirname, 'server.js')];
      require('./server.js');
      break;

    default:
      console.log(`Usage: issues.js <command>

    add "Brief title" --type bug|feature --size Small|Medium|Big [--notes "..."]
    plan <n>                        move to Planned
    start <n>                       move to In Progress
    deploy <n>                      move to Deployed (shipped, still needs prod verification)
    undeploy <n>                    move back to In Progress
    done <n>                        move to Done
    cancel <n> [--reason "..."]     move to Cancelled
    reopen <n>                      move back to Open
    flag <n> --reason "..."          flag it for your attention, on any active column
    unflag <n>                      take it off hold
    edit <n> [--title|--type|--size|--notes]
    show <n> [--json]
    list [open|planned|in_progress|deployed|done|cancelled] [--json]
    projects [--json]
    serve                           start the board on http://127.0.0.1:2345

    --project <name|path>           target another project (default: the git repo containing the cwd)
  `);
      process.exit(cmd === 'help' || flags.help ? 0 : 1);
  }
}

(async () => {
  const board = ['serve', 'help'].includes(cmd) ? null : await ensureServer();
  run();
  if (board) console.log(`board: ${board}`);
})();
