// Shared store for the issue tracker.
// Layout:
//   ~/.claude/issues/<project-slug>/project.json   { name, path }
//   ~/.claude/issues/<project-slug>/<n>.json       one issue per file
//
// One file per issue means concurrent Claude sessions never clobber each
// other, and ids are allocated with an exclusive create ('wx') rather than a
// lock file.

const fs = require('fs');
const path = require('path');
const os = require('os');

const CLAUDE_DIR = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
const ISSUES_DIR = path.join(CLAUDE_DIR, 'issues');

const TYPES = ['bug', 'feature'];
const SIZES = ['Small', 'Medium', 'Big'];
const STATUSES = ['open', 'planned', 'in_progress', 'deployed', 'done', 'cancelled'];
const RESOLVED = ['done', 'cancelled'];

// Done and Cancelled only show what was resolved inside this window.
const RESOLVED_WINDOW_MS = 12 * 60 * 60 * 1000;

const slugify = (s) => String(s).replace(/[^A-Za-z0-9_.-]/g, '-').replace(/^-+|-+$/g, '') || 'project';
const num = (id) => `#${String(id).padStart(2, '0')}`;

// --- project resolution ----------------------------------------------------

// The project is the git repo the cwd sits in, else the cwd itself.
function detectProject(cwd = process.cwd()) {
  let dir = path.resolve(cwd);
  for (;;) {
    if (fs.existsSync(path.join(dir, '.git'))) break;
    const up = path.dirname(dir);
    if (up === dir) {
      dir = path.resolve(cwd);
      break;
    }
    dir = up;
  }
  const name = path.basename(dir);
  return { slug: slugify(name), name, path: dir };
}

function projectDir(slug) {
  return path.join(ISSUES_DIR, slugify(slug));
}

function ensureProject(project) {
  const dir = projectDir(project.slug);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'project.json');
  if (!fs.existsSync(file)) {
    fs.writeFileSync(file, JSON.stringify({ name: project.name, path: project.path }, null, 2));
  }
  return dir;
}

function readProjectMeta(slug) {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(projectDir(slug), 'project.json'), 'utf8'));
    return { slug, name: raw.name || slug, path: raw.path || null };
  } catch {
    return { slug, name: slug, path: null };
  }
}

function listProjects() {
  let slugs;
  try {
    slugs = fs.readdirSync(ISSUES_DIR);
  } catch {
    return [];
  }
  return slugs
    .filter((s) => !s.startsWith('.'))
    .filter((s) => {
      try {
        return fs.statSync(path.join(ISSUES_DIR, s)).isDirectory();
      } catch {
        return false;
      }
    })
    .map((slug) => {
      const issues = readIssues(slug);
      const open = issues.filter((i) => !RESOLVED.includes(i.status)).length;
      const updatedAt = issues.reduce((m, i) => Math.max(m, Date.parse(i.updatedAt) || 0), 0);
      return { ...readProjectMeta(slug), total: issues.length, open, updatedAt };
    })
    .sort((a, b) => b.updatedAt - a.updatedAt || a.name.localeCompare(b.name));
}

// Accepts a slug, a display name, or a path. Used by --project.
function resolveProject(hint) {
  if (!hint) return detectProject();
  const wanted = slugify(hint);
  const existing = listProjects();
  const hit =
    existing.find((p) => p.slug === wanted) ||
    existing.find((p) => p.name === hint) ||
    existing.find((p) => p.path === path.resolve(hint));
  if (hit) return hit;
  if (hint.includes(path.sep) || hint.startsWith('.')) return detectProject(hint);
  return { slug: wanted, name: hint, path: null };
}

// --- issues ----------------------------------------------------------------

function coerce(raw, id) {
  return {
    id: Number(raw.id ?? id),
    number: num(raw.id ?? id),
    title: String(raw.title ?? '').trim() || `Issue ${num(raw.id ?? id)}`,
    type: TYPES.includes(raw.type) ? raw.type : 'feature',
    size: SIZES.includes(raw.size) ? raw.size : 'Medium',
    status: STATUSES.includes(raw.status) ? raw.status : 'open',
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    // "On hold" — work started but is parked on a question or a blocker. The
    // reason is the point of the flag, so it is never optional.
    flagged: raw.flagged === true,
    flagReason: typeof raw.flagReason === 'string' ? raw.flagReason : '',
    flaggedAt: raw.flaggedAt || null,
    createdAt: raw.createdAt || null,
    updatedAt: raw.updatedAt || raw.createdAt || null,
    resolvedAt: raw.resolvedAt || null,
  };
}

function readIssues(slug) {
  const dir = projectDir(slug);
  let entries;
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const out = [];
  for (const name of entries) {
    if (!/^\d+\.json$/.test(name)) continue;
    try {
      out.push(coerce(JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8')), Number(name.slice(0, -5))));
    } catch {
      // Mid-write or corrupt: skip; the caller re-reads on the next tick.
    }
  }
  // Newest first, everywhere — the board and every CLI listing order by
  // creation date, youngest on top.
  return out.sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}

function readIssue(slug, id) {
  try {
    return coerce(JSON.parse(fs.readFileSync(path.join(projectDir(slug), `${Number(id)}.json`), 'utf8')), id);
  } catch {
    return null;
  }
}

function writeIssue(slug, issue) {
  const file = path.join(projectDir(slug), `${issue.id}.json`);
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(issue, null, 2));
  fs.renameSync(tmp, file); // atomic, so a reader never sees a half file
  return issue;
}

function createIssue(project, { title, type, size, notes = '' }) {
  const dir = ensureProject(project);
  const now = new Date().toISOString();
  const existing = fs
    .readdirSync(dir)
    .filter((n) => /^\d+\.json$/.test(n))
    .map((n) => Number(n.slice(0, -5)));
  let id = existing.length ? Math.max(...existing) + 1 : 1;

  const body = (n) =>
    JSON.stringify(
      {
        id: n, title: title.trim(), type, size, status: 'open', notes,
        flagged: false, flagReason: '', flaggedAt: null,
        createdAt: now, updatedAt: now, resolvedAt: null,
      },
      null,
      2,
    );

  // 'wx' fails if the id was taken between the readdir and the write, which is
  // exactly the race another session would cause.
  for (;;) {
    try {
      fs.writeFileSync(path.join(dir, `${id}.json`), body(id), { flag: 'wx' });
      return coerce(JSON.parse(body(id)), id);
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      id++;
    }
  }
}

function updateIssue(slug, id, patch) {
  const current = readIssue(slug, id);
  if (!current) return null;
  const now = new Date().toISOString();
  const next = { ...current, ...patch, id: current.id, updatedAt: now };

  // Re-stamp whenever the status changes into a resolved state — including
  // cancelled -> done, where both sides are resolved but the 12h window has to
  // restart from the new decision, not the old one. An edit that leaves the
  // status alone keeps the original timestamp.
  if (RESOLVED.includes(next.status)) {
    if (next.status !== current.status || !current.resolvedAt) next.resolvedAt = now;
  } else {
    next.resolvedAt = null;
  }

  // Flagged means "this needs your attention" — works on any active column,
  // not just In Progress. Resolving or cancelling the issue answers whatever
  // it was flagged for, so the flag clears automatically at that point.
  if (RESOLVED.includes(next.status)) {
    next.flagged = false;
    next.flagReason = '';
    next.flaggedAt = null;
  } else if (next.flagged) {
    if (!current.flagged) next.flaggedAt = now; // newly flagged: hold starts now
    // already flagged and still flagged: keep the original flaggedAt, even if
    // the reason text changed — that timestamp answers "how long on hold".
  } else {
    next.flagReason = '';
    next.flaggedAt = null;
  }


  delete next.number;
  writeIssue(slug, next);
  return coerce(next, id);
}

// --- board -----------------------------------------------------------------

function withinWindow(issue, now) {
  const at = Date.parse(issue.resolvedAt || issue.updatedAt || 0);
  return Number.isFinite(at) && now - at < RESOLVED_WINDOW_MS;
}

// Done and Cancelled are windowed to the last 12h; hidden counts the rest so
// the column can say what it is not showing.
function buildBoard(slug, now = Date.now()) {
  const issues = readIssues(slug);
  const columns = { open: [], planned: [], in_progress: [], deployed: [], done: [], cancelled: [] };
  const hidden = { open: 0, planned: 0, in_progress: 0, deployed: 0, done: 0, cancelled: 0 };

  for (const issue of issues) {
    const col = columns[issue.status] ? issue.status : 'open';
    if (RESOLVED.includes(col) && !withinWindow(issue, now)) hidden[col]++;
    else columns[col].push(issue);
  }

  // Column order already comes out of readIssues() newest-created first, so
  // no further sorting is needed here — every column, including Done and
  // Cancelled, shows youngest on top.
  return { columns, hidden, windowHours: RESOLVED_WINDOW_MS / 3600000 };
}

module.exports = {
  CLAUDE_DIR,
  ISSUES_DIR,
  TYPES,
  SIZES,
  STATUSES,
  RESOLVED,
  RESOLVED_WINDOW_MS,
  num,
  slugify,
  detectProject,
  resolveProject,
  listProjects,
  readProjectMeta,
  ensureProject,
  projectDir,
  readIssues,
  readIssue,
  createIssue,
  updateIssue,
  buildBoard,
};
