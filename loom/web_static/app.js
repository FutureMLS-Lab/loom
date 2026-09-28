/**
 * loom web client.
 *
 * One task surface: an agent tmux pane plus a read-only Markdown viewer
 * that can switch between scanned task/worktree Markdown files.
 *
 * Project-scoped NOTES.md is reached via the sidebar's Notes button.
 *
 * The client talks to /api/projects, /api/tasks, /api/tmux/*,
 * /api/tasks/<slug>/(interview|claude)/*, and template GET/PUT for
 * PLAN.md.
 */

const FILES = {
  plan: 'PLAN.md',
};

// "interview" is the embedded read-only Markdown viewer on the agent pane.
const MARKDOWN_PANELS = ['interview'];

// Tab labels are computed per task so the agent pane name matches the
// task's agent setting (Claude / Codex).
const TABS = [
  { id: 'claude', label: 'Claude', getLabel: (meta) => agentLabel(meta?.agent) },
  { id: 'changes', label: 'Code Diff' },
];
const DEFAULT_TAB = TABS[0].id;

// An AR task keeps the normal tabs: its author agent runs in the same tmux
// pane as any other task, and the paper lives in the worktree the Code Diff
// tab shows. The AR tab just leads.
const AR_TAB = { id: 'ar', label: 'AR' };
function tabsFor(meta) { return isArKind(meta && meta.kind) ? [AR_TAB, ...TABS] : TABS; }
function defaultTabFor(meta) { return isArKind(meta && meta.kind) ? AR_TAB.id : DEFAULT_TAB; }

const AGENT_LABELS = { cursor: 'Agent', claude: 'Claude', codex: 'Codex' };
function agentLabel(name) { return AGENT_LABELS[(name || '').toLowerCase()] || 'Agent'; }
function normalizeAgent(name) { return AGENT_LABELS[(name || '').toLowerCase()] ? name.toLowerCase() : 'cursor'; }
function taskBackendLabel(meta) {
  meta = meta || {};
  const base = `${agentLabel(meta.agent)}${meta.interview_model ? ' · ' + meta.interview_model : ''}`;
  if (isAuthorKind(meta.kind)) return `Author · ${base}`;
  return isArKind(meta.kind) ? `AR · ${base}` : base;
}

// Tasks created before the rename carry kind "aris"; they are AR tasks too.
function isArKind(kind) {
  const k = String(kind || '').toLowerCase();
  return k === 'ar' || k === 'aris';
}
function isAuthorKind(kind) { return String(kind || '').toLowerCase() === 'author'; }

// Lightweight non-blocking toast (replaces jarring native alert() for transient
// errors/notices). Stacks bottom-right, auto-dismisses; aria-live for SR users.
function toast(message, opts = {}) {
  if (message == null || message === '') return;
  let host = document.getElementById('app-toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'app-toast-host';
    host.className = 'toast-host';
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'toast' + (opts.type === 'error' ? ' toast--error' : opts.type === 'success' ? ' toast--success' : '');
  el.setAttribute('role', opts.type === 'error' ? 'alert' : 'status');
  el.textContent = String(message);
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('is-show'));
  const ttl = opts.ttl || (opts.type === 'error' ? 6000 : 3500);
  setTimeout(() => {
    el.classList.remove('is-show');
    setTimeout(() => el.remove(), 250);
  }, ttl);
}

const STATE = {
  slug: null,
  projectId: null,
  // Per-project last-selected task slug, so switching projects (or reloading)
  // restores the task you were on instead of defaulting to none. Persisted in
  // localStorage; see loadLastTaskMap / rememberSelectedTask / restoreSelectedTaskForProject.
  lastTaskByProject: {},
  projects: [],
  skillsPath: '',
  skillsOptions: [],
  skillsMissing: [],
  codeRootPattern: '.',
  codeRootPath: '',
  serverReachable: true,
  modelDefaults: { cursor: 'grok-4.7-high-fast', claude: 'claude-fable-5', codex: 'gpt-5.5' },
  modelOptions: { cursor: [], claude: [], codex: [] },
  tasks: [],
  currentMeta: null,
  worktreeStatuses: [],
  taskRoot: '',
  planPath: '',
  launchRoot: '',
  launchRootChildren: [],
  paneTimer: null,
  activePanel: TABS[0].id,
  previewCache: {},
  previewDebounce: {},
  sidebarOpen: false,
  activity: null,
  activityTimer: null,
  budgetStopsSeen: {},
  notesDirty: false,
  notesSaving: false,
  taskFilter: '',
  pollInFlight: {
    capture: false,
    templates: false,
    sessions: false,
  },
  // Per-task unsent text in the terminal input box. Keep this client-side
  // only: drafts can contain arbitrary user text and shouldn't be written
  // into task metadata or markdown files.
  paneDrafts: {},
  // Per-task unsent text in the Chinese/compose box (same client-only rationale).
  composeDrafts: {},
  // Embedded read-only markdown viewer on the Claude tab. The picker
  // lets the user flip between any top-level *.md file in the task root;
  // PLAN.md is the default.
  interviewMdFile: FILES.plan,
  interviewMdFiles: [],
  interviewMdContents: {},
  // True while the user is editing PLAN.md inline in the Claude-tab viewer.
  // Guards the 12s poll from clobbering in-progress edits (loadInterviewMdIntoEditor).
  planEditing: false,
  // Changes tab: cached diff payload + which file is selected.
  changesData: null,
  changesSelected: '',
  changesLoading: false,
  // True while a monitor enable/disable request is in flight, so the 4s
  // poll's loadMonitor() doesn't reset the toggle the user just clicked.
  monitorBusy: false,
};

let PROJECT_DRAG_ID = '';
let PROJECT_JUST_DRAGGED = false;
let TASK_DRAG_SLUG = '';
let TASK_JUST_DRAGGED = false;

function withProjectQuery(path) {
  if (!STATE.projectId) return path;
  if (path.startsWith('/api/projects')) return path;
  if (
    !path.startsWith('/api/project')
    && !path.startsWith('/api/tasks')
    && !path.startsWith('/api/asset')
  ) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}project=${encodeURIComponent(STATE.projectId)}`;
}

async function apiFetch(url, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body !== undefined && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(url, { ...opts, headers });
  } catch (err) {
    // fetch only rejects when the request never reached the server: Loom was
    // stopped, or the SSH tunnel it is served over went away.
    setServerReachable(false);
    throw err;
  }
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!res.ok) {
    if (res.status >= 500) setServerReachable(false);
    throw makeApiError(res, data);
  }
  setServerReachable(true);
  return data;
}

async function apiNoProject(path, opts = {}) {
  return apiFetch(path, opts);
}

async function api(path, opts = {}) {
  return apiFetch(withProjectQuery(path), opts);
}

// An unreachable server used to look exactly like an empty workspace — no
// projects, no tasks, "Select Or Create A Task" — which reads as data loss.
// Say so instead, and keep whatever was last loaded on screen.
function setServerReachable(ok) {
  if (STATE.serverReachable === ok) return;
  STATE.serverReachable = ok;
  const bar = document.getElementById('offline-bar');
  if (bar) bar.hidden = ok;
  document.body.classList.toggle('is-offline', !ok);
}

function makeApiError(res, data) {
  const err = new Error((data && data.error) || res.statusText || `HTTP ${res.status}`);
  err.status = res.status;
  err.body = data;
  return err;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isTransientApiError(err) {
  // These usually mean the local web server is restarting, the proxy timed
  // out, or a long-running tmux/git request temporarily blocked the route.
  // Retrying is safe for GETs and avoids surfacing noisy "Bad Gateway" text
  // in the task terminal.
  return [502, 503, 504].includes(Number(err && err.status));
}

async function apiWithRetry(path, opts = {}, retryOpts = {}) {
  const attempts = retryOpts.attempts ?? 3;
  const delayMs = retryOpts.delayMs ?? 250;
  let lastErr;
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await api(path, opts);
    } catch (err) {
      lastErr = err;
      if (!isTransientApiError(err) || i === attempts - 1) break;
      await sleep(delayMs * (i + 1));
    }
  }
  throw lastErr;
}

function $(sel) { return document.querySelector(sel); }

// ===== Tabs =====

function showPanel(id) {
  const hasTabs = !!document.getElementById('main-tabs');
  if (hasTabs) {
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', t.dataset.tab === id);
    });
  }
  document.querySelectorAll('.tab-panel').forEach((p) => {
    const on = p.dataset.panel === id;
    p.classList.toggle('active', on);
    p.hidden = !on;
  });
  STATE.activePanel = id;
  if (id === 'claude') {
    // The Claude tab embeds a read-only viewer for any scanned *.md
    // file in the task root - defaults to PLAN.md.
    updateMarkdownPreview('interview');
    // The terminal may have fit to a default width while this tab was hidden;
    // now that it's visible, refit so Claude fills the full screen width (and
    // the tmux pane resizes to match).
    termHandleResize();
    deferIdle(refreshInterviewPreview);
    deferIdle(refreshTaskTemplates);
    deferIdle(refreshClaudeSessions);
    deferIdle(loadMonitor);
  }
  if (id === 'changes') {
    refreshChangesView();
  }
  if (id === 'ar') {
    deferIdle(() => refreshAr(true));
  }
}

function deferIdle(fn) {
  if (typeof requestIdleCallback === 'function') {
    requestIdleCallback(() => { try { fn(); } catch (_) {} }, { timeout: 200 });
  } else {
    setTimeout(() => { try { fn(); } catch (_) {} }, 0);
  }
}

function buildTabs(meta) {
  const nav = $('#main-tabs');
  if (!nav) return;
  nav.hidden = false;
  nav.innerHTML = '';
  const active = defaultTabFor(meta);
  for (const t of tabsFor(meta)) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tab' + (t.id === active ? ' active' : '');
    b.dataset.tab = t.id;
    b.textContent = typeof t.getLabel === 'function' ? t.getLabel(meta) : t.label;
    b.addEventListener('click', () => showPanel(t.id));
    nav.appendChild(b);
  }
}

// ===== Agent activity (which agent just stopped) =====
//
// Classes are toggled on the existing nodes rather than folded into the
// renderers: re-rendering a list restarts every CSS animation, so a ring on a
// four-second poll would stutter instead of spin.

async function pollActivity() {
  try {
    STATE.activity = await apiNoProject('/api/activity');
  } catch {
    return; // a blip should not clear the rings
  }
  applyActivity();
}

function applyActivity() {
  const data = STATE.activity;
  if (!data) return;
  const tasks = data.tasks || {};
  const projects = data.projects || {};

  Object.entries(tasks).forEach(([key, entry]) => {
    const stop = entry && entry.budget_stop;
    const stamp = stop && stop.stopped_at;
    if (!stamp || STATE.budgetStopsSeen[key] === stamp) return;
    STATE.budgetStopsSeen[key] = stamp;
    const reason = stop.reason || 'the configured usage limit was reached';
    toast(`Loom paused ${entry.slug}: ${reason}. Edits were preserved; the next message will use a fresh session.`, { ttl: 9000 });
  });

  document.querySelectorAll('#task-list li[data-slug]').forEach((li) => {
    const entry = tasks[`${STATE.projectId}/${li.dataset.slug}`];
    // Running is plain status, so it shows even on the open task; a finish is
    // an attention ask, and the open task is already being looked at.
    const working = !!(entry && entry.working);
    const finished = !!(entry && entry.finished_at) && li.dataset.slug !== STATE.slug;
    li.classList.toggle('is-finished', finished);
    li.classList.toggle('is-working', working && !finished);
  });

  document.querySelectorAll('.project-toggle[data-project-id]').forEach((chip) => {
    const pid = chip.dataset.projectId;
    const agg = projects[pid] || {};
    // Blinking on the project you are already in would duplicate the task
    // rings; the steady running light is status, not a request, so it stays.
    const finished = (agg.finished || 0) > 0 && pid !== STATE.projectId;
    const working = (agg.working || 0) > 0;
    chip.classList.toggle('is-finished', finished);
    chip.classList.toggle('is-working', working && !finished);
  });
}

function ackActivity(slug) {
  if (!slug || !STATE.projectId) return;
  const entry = (STATE.activity && STATE.activity.tasks) || {};
  if (!entry[`${STATE.projectId}/${slug}`]) return;
  delete entry[`${STATE.projectId}/${slug}`];
  applyActivity();
  api('/api/activity/ack', { method: 'POST', body: JSON.stringify({ slug }) })
    .catch(() => {});
}

// ===== Projects =====

async function loadProjectsList() {
  const d = await apiNoProject('/api/projects');
  STATE.projects = d.projects || [];
  STATE.launchRoot = String(d.launchRoot || '').trim();
  STATE.launchRootChildren = Array.isArray(d.launchRootChildren) ? d.launchRootChildren : [];
  const addBtn = document.getElementById('btn-add-project');
  if (addBtn) addBtn.title = STATE.launchRoot ? `Add a folder inside ${STATE.launchRoot}` : 'Add a folder';
  const cur = String(d.currentProjectId || d.defaultProjectId || '').trim();
  if (cur && STATE.projects.some((p) => p.id === cur)) {
    STATE.projectId = cur;
  } else {
    STATE.projectId = null;
  }
  renderProjectToggleBar();
}

function renderProjectToggleBar() {
  const scroll = document.getElementById('project-toggle-scroll');
  if (!scroll) return;
  scroll.innerHTML = '';
  const list = STATE.projects || [];
  if (!list.length) {
    const em = document.createElement('span');
    em.className = 'project-bar__empty-msg';
    em.textContent = 'No repos yet — use + Add repo to register a project root.';
    scroll.appendChild(em);
    syncProjectBarFades();
    return;
  }
  list.forEach((p) => {
    const item = document.createElement('div');
    item.className = 'project-toggle' + (p.id === STATE.projectId ? ' is-active' : '');
    item.dataset.projectId = p.id;
    item.title = p.path || p.name || p.id;
    item.draggable = true;
    item.addEventListener('dragstart', (ev) => {
      PROJECT_DRAG_ID = p.id;
      PROJECT_JUST_DRAGGED = true;
      item.classList.add('is-dragging');
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', p.id);
    });
    item.addEventListener('dragover', (ev) => {
      if (!PROJECT_DRAG_ID || PROJECT_DRAG_ID === p.id) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      const rect = item.getBoundingClientRect();
      const after = ev.clientX > rect.left + (rect.width / 2);
      clearProjectDropMarkers(scroll);
      item.classList.toggle('is-drop-before', !after);
      item.classList.toggle('is-drop-after', after);
    });
    item.addEventListener('drop', async (ev) => {
      if (!PROJECT_DRAG_ID || PROJECT_DRAG_ID === p.id) return;
      ev.preventDefault();
      const dragId = ev.dataTransfer.getData('text/plain') || PROJECT_DRAG_ID;
      const after = item.classList.contains('is-drop-after');
      clearProjectDropMarkers(scroll);
      await reorderProjectsByDrag(dragId, p.id, after);
    });
    item.addEventListener('dragend', () => {
      PROJECT_DRAG_ID = '';
      item.classList.remove('is-dragging');
      clearProjectDropMarkers(scroll);
      setTimeout(() => { PROJECT_JUST_DRAGGED = false; }, 0);
    });
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'project-toggle__main';
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', p.id === STATE.projectId ? 'true' : 'false');
    const label = document.createElement('span');
    label.className = 'project-toggle__label';
    label.textContent = p.name || p.id;
    btn.appendChild(label);
    btn.addEventListener('click', () => {
      if (PROJECT_JUST_DRAGGED) return;
      if (p.id !== STATE.projectId) switchProject(p.id);
    });
    item.appendChild(btn);
    const controls = document.createElement('span');
    controls.className = 'project-toggle__controls';
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'project-toggle__rm';
    rm.setAttribute('aria-label', `Remove ${p.name || p.id} from list`);
    rm.textContent = '×';
    rm.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      removeProject(p.id);
    });
    controls.appendChild(rm);
    item.appendChild(controls);
    scroll.appendChild(item);
  });
  requestAnimationFrame(() => {
    const active = scroll.querySelector('.project-toggle.is-active');
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
    syncProjectBarFades();
    applyActivity();
  });
}

// Fade whichever edge of the project bar still has chips behind it, so a
// half-visible chip reads as "scrollable" rather than clipped.
function syncProjectBarFades() {
  const scroll = document.getElementById('project-toggle-scroll');
  if (!scroll) return;
  const max = scroll.scrollWidth - scroll.clientWidth;
  scroll.classList.toggle('is-fade-start', scroll.scrollLeft > 4);
  scroll.classList.toggle('is-fade-end', scroll.scrollLeft < max - 4);
  if (scroll.dataset.fadeBound) return;
  scroll.dataset.fadeBound = '1';
  scroll.addEventListener('scroll', syncProjectBarFades, { passive: true });
  window.addEventListener('resize', syncProjectBarFades, { passive: true });
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(syncProjectBarFades).observe(scroll);
  }
}

function clearProjectDropMarkers(root = document) {
  root.querySelectorAll('.project-toggle.is-drop-before, .project-toggle.is-drop-after').forEach((el) => {
    el.classList.remove('is-drop-before', 'is-drop-after');
  });
}

async function reorderProjectsByDrag(dragId, targetId, afterTarget) {
  const activeId = STATE.projectId;
  const ids = (STATE.projects || []).map((p) => p.id);
  const from = ids.indexOf(dragId);
  const target = ids.indexOf(targetId);
  if (from < 0 || target < 0 || dragId === targetId) return;
  ids.splice(from, 1);
  const targetAfterRemoval = ids.indexOf(targetId);
  ids.splice(targetAfterRemoval + (afterTarget ? 1 : 0), 0, dragId);
  if (ids.every((id, idx) => id === (STATE.projects[idx] && STATE.projects[idx].id))) return;
  const byId = new Map((STATE.projects || []).map((p) => [p.id, p]));
  STATE.projects = ids.map((id) => byId.get(id)).filter(Boolean);
  renderProjectToggleBar();
  try {
    const d = await apiNoProject('/api/projects/reorder', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    STATE.projects = d.projects || STATE.projects || [];
    if (activeId && STATE.projects.some((p) => p.id === activeId)) {
      STATE.projectId = activeId;
    }
    renderProjectToggleBar();
  } catch (e) {
    toast(e.message, { type: 'error' });
    await loadProjectsList();
  }
}

async function switchProject(id) {
  if (!id || id === STATE.projectId) return;
  await apiNoProject(`/api/projects/${encodeURIComponent(id)}/activate`, { method: 'POST', body: '{}' });
  STATE.projectId = id;
  clearTaskSelection();
  await loadProjectsList();
  await loadProject();
  await loadTasks();
  await restoreSelectedTaskForProject();
}

async function removeProject(id) {
  if (!confirm('Remove this project from the web UI list? Task files on disk are not deleted.')) return;
  try {
    await apiNoProject(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
    clearTaskSelection();
    await loadProjectsList();
    await loadProject();
    await loadTasks();
    await restoreSelectedTaskForProject();
  } catch (e) {
    toast(e.message, { type: 'error' });
  }
}

async function openAddProjectModal() {
  const modal = $('#add-project-modal');
  if (!modal) return;
  modal.hidden = false;
  $('#add-project-status').textContent = '';
  $('#new-project-path').value = '';
  const codeRoot = document.getElementById('new-project-code-root');
  if (codeRoot) codeRoot.value = '.';
  const repoEl = document.getElementById('new-project-repo');
  if (repoEl) repoEl.value = '';
  try {
    await loadProjectsList();
  } catch (e) {
    $('#add-project-status').textContent = e.message;
  }
  const titleEl = $('#add-project-modal-title');
  if (titleEl) titleEl.textContent = STATE.launchRoot ? `Add a folder in ${STATE.launchRoot}` : 'Add a folder';
  renderAddProjectChips();
  setAddProjectMode('existing');
  requestAnimationFrame(() => $('#new-project-path').focus());
}

function setAddProjectMode(mode) {
  STATE.addProjectMode = mode;
  document.querySelectorAll('#add-project-modes .add-project-mode').forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  const repoRow = document.getElementById('add-project-repo-row');
  const label = document.getElementById('new-project-path-label');
  const hint = document.getElementById('add-project-path-hint');
  const pathInput = document.getElementById('new-project-path');
  const saveBtn = document.getElementById('btn-add-project-save');
  const root = (STATE.launchRoot || '').trim();
  const prefix = root ? root.replace(/\/+$/, '') + '/' : '';
  if (repoRow) repoRow.hidden = mode !== 'clone';
  if (mode === 'empty') {
    if (label) label.textContent = 'New folder path';
    if (pathInput) pathInput.placeholder = prefix ? prefix + 'my-new-project' : '/path/inside/launch-dir';
    if (hint) hint.textContent = root ? `Creates the folder inside ${root} and registers it.` : 'Creates the folder and registers it.';
    if (saveBtn) saveBtn.textContent = 'Create & add';
    if (pathInput && prefix && !pathInput.value.trim()) pathInput.value = prefix;
  } else if (mode === 'clone') {
    if (label) label.textContent = 'Clone into (destination folder)';
    if (pathInput) pathInput.placeholder = prefix ? prefix + 'repo' : '/path/inside/launch-dir';
    if (hint) hint.textContent = root ? `git clone the URL into this folder inside ${root}, then register it.` : 'git clone the URL into this folder, then register it.';
    if (saveBtn) saveBtn.textContent = 'Clone & add';
    if (pathInput && prefix && !pathInput.value.trim()) pathInput.value = prefix;
  } else {
    if (label) label.textContent = 'Project directory';
    if (pathInput) pathInput.placeholder = prefix ? prefix + 'my-repo' : '/home/you/MyRepo';
    if (hint) hint.textContent = 'Register an existing folder (any absolute path).';
    if (saveBtn) saveBtn.textContent = 'Add';
  }
}

function renderAddProjectChips() {
  const wrap = document.getElementById('add-project-launch-wrap');
  const host = document.getElementById('add-project-chips');
  if (!wrap || !host) return;
  host.innerHTML = '';
  const kids = STATE.launchRootChildren || [];
  const root = (STATE.launchRoot || '').trim();
  if (!kids.length || !root) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const elRoot = document.getElementById('add-project-launch-root');
  if (elRoot) elRoot.textContent = root;
  for (const k of kids) {
    const name = k && k.name != null ? String(k.name) : '';
    const path = k && k.path != null ? String(k.path) : '';
    if (!name || !path) continue;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'add-project-chip';
    b.textContent = name;
    b.title = path;
    b.addEventListener('click', () => {
      $('#new-project-path').value = path;
      $('#add-project-status').textContent = '';
      const inp = $('#new-project-path');
      inp.focus();
      inp.select();
    });
    host.appendChild(b);
  }
}

function closeAddProjectModal() {
  const m = $('#add-project-modal');
  if (m) m.hidden = true;
}

async function submitAddProject() {
  const mode = STATE.addProjectMode || 'existing';
  const path = $('#new-project-path').value.trim();
  const repoEl = document.getElementById('new-project-repo');
  const repo_url = repoEl ? repoEl.value.trim() : '';
  const code_root_pattern = (document.getElementById('new-project-code-root')?.value || '.').trim() || '.';
  const status = $('#add-project-status');
  const btn = $('#btn-add-project-save');
  if (mode === 'clone' && !repo_url) {
    status.textContent = 'Enter a git repo URL to clone.';
    return;
  }
  if (!path) {
    status.textContent = mode === 'existing' ? 'Enter a directory path.' : 'Enter a destination folder path.';
    return;
  }
  btn.disabled = true;
  status.textContent = mode === 'clone'
    ? 'Cloning… (large repos can take a while)'
    : (mode === 'empty' ? 'Creating…' : 'Adding…');
  try {
    const created = await apiNoProject('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ path, mode, repo_url, code_root_pattern }),
    });
    if (created.id) STATE.projectId = created.id;
    else if (created.defaultProjectId) STATE.projectId = created.defaultProjectId;
    closeAddProjectModal();
    await loadProjectsList();
    await loadProject();
    await loadTasks();
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

function updateCodeRootPreview() {
  const pattern = (document.getElementById('project-code-root-pattern')?.value || '.').trim() || '.';
  const project = (STATE.projects || []).find((p) => p.id === STATE.projectId);
  const root = (project && project.path) || '';
  const resolved = pattern === '.' ? root : `${root.replace(/\/+$/, '')}/${pattern.replace(/^\/+/, '')}`;
  const target = document.getElementById('project-code-root-resolved');
  if (target) target.textContent = resolved || '—';
}

function openCodeRootModal() {
  if (!STATE.projectId) return;
  const modal = document.getElementById('code-root-modal');
  const input = document.getElementById('project-code-root-pattern');
  const status = document.getElementById('code-root-status');
  if (input) input.value = STATE.codeRootPattern || '.';
  if (status) status.textContent = '';
  updateCodeRootPreview();
  if (modal) modal.hidden = false;
  requestAnimationFrame(() => input && input.focus());
}

function closeCodeRootModal() {
  const modal = document.getElementById('code-root-modal');
  if (modal) modal.hidden = true;
}

async function saveCodeRootPattern() {
  if (!STATE.projectId) return;
  const input = document.getElementById('project-code-root-pattern');
  const status = document.getElementById('code-root-status');
  const button = document.getElementById('btn-code-root-save');
  const pattern = (input?.value || '.').trim() || '.';
  if (button) button.disabled = true;
  if (status) status.textContent = 'Saving…';
  try {
    const result = await apiNoProject(`/api/projects/${encodeURIComponent(STATE.projectId)}/code-root`, {
      method: 'POST',
      body: JSON.stringify({ pattern }),
    });
    STATE.codeRootPattern = result.pattern || '.';
    STATE.codeRootPath = result.path || '';
    await loadProjectsList();
    closeCodeRootModal();
    toast(`Code root: ${STATE.codeRootPath}`, { type: 'success' });
  } catch (error) {
    if (status) status.textContent = error.message || 'Failed to save code root.';
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadProject() {
  if (!STATE.projectId) {
    $('#hdr-project').textContent = '(select a project above)';
    STATE.skillsPath = '';
    STATE.skillsOptions = [];
    STATE.codeRootPattern = '.';
    STATE.codeRootPath = '';
    STATE.modelDefaults = { cursor: 'grok-4.7-high-fast', claude: 'claude-fable-5', codex: 'gpt-5.5' };
    STATE.modelOptions = { cursor: [], claude: [], codex: [] };
    renderSkillsPicker();
    renderTaskSkillsPicker();
    return;
  }
  const d = await api('/api/project');
  const meta = (STATE.projects || []).find((x) => x.id === STATE.projectId);
  const pathLine = d.projectRoot || '';
  $('#hdr-project').textContent = meta ? `${meta.name} — ${pathLine}` : pathLine;
  STATE.skillsPath = d.skillsPath || '';
  STATE.skillsOptions = Array.isArray(d.skillsOptions) ? d.skillsOptions : [];
  STATE.codeRootPattern = d.codeRootPattern || '.';
  STATE.codeRootPath = d.codeRootPath || d.projectRoot || '';
  const codeRootButton = document.getElementById('btn-code-root-open');
  if (codeRootButton) {
    codeRootButton.title = `Code root: ${STATE.codeRootPath}`;
  }
  STATE.modelDefaults = d.modelDefaults || STATE.modelDefaults;
  STATE.modelOptions = d.modelOptions || STATE.modelOptions;
  renderSkillsPicker();
  renderTaskSkillsPicker(STATE.currentMeta || {});
  renderTaskModelPicker(STATE.currentMeta || {});
}

// skills_path holds one or more ;-joined paths (multiple skills used together).
function splitSkillsValue(v) {
  return String(v || '').split(';').map((s) => s.trim()).filter(Boolean);
}

function selectedSkillsValue(sel) {
  return [...sel.selectedOptions].map((o) => o.value).join(';');
}

function applySkillsSelection(sel, joined) {
  const wanted = new Set(splitSkillsValue(joined));
  for (const opt of sel.options) opt.selected = wanted.has(opt.value);
}


// Chip labels drop the directory and the .md suffix ("aris/ARIS.md" -> "ARIS"),
// widening to the parent directory only as far as needed to tell two
// same-named skills apart. Never the whole path: a task can carry an absolute
// one, and a chip that long blows out the row.
function skillChipLabels(options) {
  const parts = options.map(
    (o) => (o.textContent || o.value).replace(/\.md$/i, '').split('/').filter(Boolean),
  );
  const at = (i, depth) => parts[i].slice(-depth).join('/');
  return parts.map((_, i) => {
    let depth = 1;
    while (depth < 3 && parts.some((__, j) => j !== i && at(j, depth) === at(i, depth))) depth++;
    return at(i, depth);
  });
}

// A <select multiple> is a poor fit for this: Cmd/Ctrl-click is undiscoverable
// and the native list box can't be styled. The select stays in the DOM as the
// source of truth and this mirrors it as toggle chips.
function renderSkillChips(sel) {
  const host = document.getElementById(sel.dataset.chips || '');
  if (!host) return;
  const options = [...sel.options];
  const missing = new Set(STATE.skillsMissing || []);
  host.innerHTML = '';
  if (!options.length) {
    const empty = document.createElement('span');
    empty.className = 'skill-chips__empty';
    empty.textContent = 'No skills found for this project.';
    host.appendChild(empty);
    return;
  }
  const labels = skillChipLabels(options);
  const requireOne = sel.dataset.requireOne === '1';
  options.forEach((opt, i) => {
    const gone = missing.has(opt.value);
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'skill-chip' + (opt.selected ? ' is-on' : '') + (gone ? ' is-missing' : '');
    chip.setAttribute('role', 'checkbox');
    chip.setAttribute('aria-checked', opt.selected ? 'true' : 'false');
    chip.title = gone
      ? `${opt.value}\nThis file does not exist on this host, so the agent falls`
        + ' back to the default skill. Click to remove it.'
      : (opt.title || opt.value);
    chip.disabled = sel.disabled;
    const mark = document.createElement('span');
    mark.className = 'skill-chip__mark';
    mark.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'skill-chip__label';
    label.textContent = labels[i];
    chip.append(mark, label);
    chip.addEventListener('click', () => {
      const lastOne = requireOne && opt.selected
        && options.filter((o) => o.selected).length === 1;
      if (lastOne) {
        chip.classList.add('is-refused');
        setTimeout(() => chip.classList.remove('is-refused'), 400);
        return;
      }
      opt.selected = !opt.selected;
      renderSkillChips(sel);
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
    host.appendChild(chip);
  });
}

function renderSkillsPicker() {
  const sel = document.getElementById('new-skills');
  if (!sel) return;
  sel.multiple = true;
  const current = selectedSkillsValue(sel) || STATE.skillsPath || '';
  sel.innerHTML = '';
  const options = STATE.skillsOptions.length
    ? STATE.skillsOptions
    : (STATE.skillsPath ? [{ label: STATE.skillsPath, path: STATE.skillsPath }] : []);
  for (const opt of options) {
    const path = String(opt.path || '').trim();
    if (!path) continue;
    const option = document.createElement('option');
    option.value = path;
    option.textContent = opt.label ? `${opt.label}` : path;
    option.title = path;
    sel.appendChild(option);
  }
  applySkillsSelection(sel, current);
  if (!sel.selectedOptions.length && STATE.skillsPath) applySkillsSelection(sel, STATE.skillsPath);
  renderSkillChips(sel);
}


// ===== Markdown rendering =====

const HTML_ESCAPE_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch]);
}

// Set by renderMarkdownWithAssets for the duration of one render: maps a
// relative image path to a URL Loom can serve. Documents rendered without one
// (an AR review, say) simply leave relative images as literal text.
let MD_ASSET_RESOLVER = null;

// escapeHtml runs before the inline rules, so a URL taken back out of the
// escaped text has to be decoded before it can be used as a real path.
function unescapeHtml(s) {
  return String(s)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

// `![alt](path)`, `![alt](path "title")`, absolute or relative.
function renderMarkdownImage(match, alt, target) {
  const url = String(target).trim().split(/\s+/)[0];
  if (/^(https?:|data:)/i.test(url)) {
    return `<img src="${url}" alt="${alt}" class="md-img" loading="lazy">`;
  }
  const resolved = MD_ASSET_RESOLVER ? MD_ASSET_RESOLVER(unescapeHtml(url)) : null;
  // No resolver (or a path we can't serve): keep the source text rather than
  // silently dropping the figure.
  if (!resolved) return match;
  return `<img src="${escapeHtml(resolved)}" alt="${alt}" class="md-img" loading="lazy">`;
}

function renderInlineMarkdown(text) {
  return escapeHtml(text)
    // Images must run before links (![alt](url) shares the [text](url) shape).
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, renderMarkdownImage)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

// URL for an image referenced from `docPath`, resolved relative to that
// document's own directory the way every markdown viewer does it.
function markdownAssetUrl(relPath, docPath, slug) {
  const dir = String(docPath || '').split('/').slice(0, -1).filter(Boolean).join('/');
  const joined = dir ? `${dir}/${relPath}` : relPath;
  const params = new URLSearchParams({ path: joined });
  if (slug) params.set('task', slug);
  return withProjectQuery(`/api/asset?${params.toString()}`);
}

function markdownAssetResolver(which) {
  if (which === 'interview' && STATE.slug) {
    return (rel) => markdownAssetUrl(rel, STATE.interviewMdFile, STATE.slug);
  }
  if (which === 'notes') return (rel) => markdownAssetUrl(rel, 'NOTES.md', '');
  return null;
}

function renderMarkdownWithAssets(md, resolveAsset) {
  MD_ASSET_RESOLVER = resolveAsset || null;
  try {
    return renderMarkdown(md);
  } finally {
    MD_ASSET_RESOLVER = null;
  }
}

// TeX spans must cross the markdown renderer untouched: `x_i * y_j` inside
// $...$ would otherwise sprout <em> tags before KaTeX ever sees it. They are
// swapped for private-use-area placeholders up front and restored - escaped -
// into the final HTML, where KaTeX (loaded from vendor/) typesets them.
// Inside code fences the restore is literal text and KaTeX's default
// ignoredTags skip <pre>/<code>, so code keeps its dollar signs.
function protectMath(md) {
  const spans = [];
  const stash = (m) => {
    spans.push(m);
    return `${spans.length - 1}`;
  };
  let s = String(md || '');
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, stash);
  s = s.replace(/(?<![\\$\w])\$([^$\n]+?)\$(?!\w)/g, stash);
  return [s, spans];
}

function restoreMath(html, spans) {
  if (!spans.length) return html;
  return html.replace(/(\d+)/g, (_, i) => escapeHtml(spans[Number(i)] || ''));
}

function typesetMath(el) {
  if (!el || typeof window.renderMathInElement !== 'function') return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true },
      ],
      throwOnError: false,
    });
  } catch { /* malformed TeX stays as its source text */ }
}

function renderMarkdown(md) {
  const [safeMd, mathSpans] = protectMath(md);
  const lines = (safeMd || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let paragraph = [];
  let listType = null;
  let codeLines = null;

  function flushParagraph() {
    if (!paragraph.length) return;
    out.push(`<p>${renderInlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  }
  function flushList() {
    if (!listType) return;
    out.push(`</${listType}>`);
    listType = null;
  }
  // Split on unescaped pipes so a literal "\|" survives inside a cell, then
  // drop the empty cells created by the optional leading/trailing pipe.
  function splitTableCells(line) {
    const s = line.trim();
    const cells = [];
    let cur = '';
    for (let i = 0; i < s.length; i += 1) {
      if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i += 1; continue; }
      if (s[i] === '|') { cells.push(cur); cur = ''; continue; }
      cur += s[i];
    }
    cells.push(cur);
    if (s.startsWith('|')) cells.shift();
    if (cells.length && s.endsWith('|') && !s.endsWith('\\|')) cells.pop();
    return cells;
  }
  // A delimiter row is one or more cells of dashes with optional alignment
  // colons. One dash is enough (GitHub allows "|-|-|"); requiring a pipe keeps
  // a bare "---" a horizontal rule rather than a one-column table.
  function isTableSeparator(line) {
    if (!line.includes('|')) return false;
    const cells = splitTableCells(line);
    return cells.length > 0 && cells.every((c) => /^\s*:?-+:?\s*$/.test(c));
  }
  function parseTableRow(line) {
    return splitTableCells(line).map((cell) => cell.trim());
  }
  function tableAlignments(line) {
    return parseTableRow(line).map((c) => {
      const left = c.startsWith(':');
      const right = c.endsWith(':');
      if (left && right) return 'center';
      if (right) return 'right';
      if (left) return 'left';
      return '';
    });
  }
  function renderTable(headers, rows, aligns) {
    const attr = (i) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : '');
    const head = headers.map((cell, i) => `<th${attr(i)}>${renderInlineMarkdown(cell)}</th>`).join('');
    const body = rows
      .map((row) => {
        // Pad short rows so the grid stays rectangular instead of ragged.
        const cells = headers.map(
          (_, i) => `<td${attr(i)}>${renderInlineMarkdown(row[i] || '')}</td>`,
        );
        return `<tr>${cells.join('')}</tr>`;
      })
      .join('');
    // A wide table scrolls inside its own box rather than stretching the pane.
    return '<div class="md-table-wrap"><table>'
      + `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (codeLines) {
      if (/^```/.test(line.trim())) {
        out.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = null;
      } else {
        codeLines.push(line);
      }
      continue;
    }
    if (/^```/.test(line.trim())) {
      flushParagraph();
      flushList();
      codeLines = [];
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    // Horizontal rule: a line of 3+ -, *, or _ (no other content). Checked
    // before lists/tables; bare "---" has no trailing space so it can't be a
    // list item, and a table separator always contains "|".
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushParagraph();
      flushList();
      out.push('<hr>');
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      flushParagraph();
      flushList();
      const headers = parseTableRow(line);
      const aligns = tableAlignments(lines[i + 1]);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(parseTableRow(lines[i]));
        i += 1;
      }
      i -= 1;
      out.push(renderTable(headers, rows, aligns));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      out.push(`<h${heading[1].length}>${renderInlineMarkdown(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (listType !== 'ul') { flushList(); listType = 'ul'; out.push('<ul>'); }
      // GitHub-style task list: "- [ ] todo" / "- [x] done" -> a (disabled)
      // checkbox so PLAN.md / NOTES checklists render as real checkboxes.
      const task = unordered[1].match(/^\[([ xX])\]\s+(.*)$/);
      if (task) {
        const checked = task[1].toLowerCase() === 'x' ? ' checked' : '';
        out.push(
          `<li class="md-task"><input type="checkbox" disabled${checked}> ` +
          `${renderInlineMarkdown(task[2])}</li>`
        );
      } else {
        out.push(`<li>${renderInlineMarkdown(unordered[1])}</li>`);
      }
      continue;
    }
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (listType !== 'ol') { flushList(); listType = 'ol'; out.push('<ol>'); }
      out.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`);
      continue;
    }
    const quote = line.match(/^>\s?(.+)$/);
    if (quote) {
      flushParagraph();
      flushList();
      out.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }
    paragraph.push(line.trim());
  }
  if (codeLines) out.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  flushParagraph();
  flushList();
  const html = restoreMath(out.join('\n'), mathSpans);
  return html || '<p class="empty-preview">Nothing to preview yet.</p>';
}

function updateMarkdownPreview(which, force = false) {
  const editor = $(`#editor-${which}`);
  const preview = $(`#preview-${which}`);
  if (!editor || !preview) return;
  const text = editor.value || '';
  if (!force && STATE.previewCache[which] === text) return;
  STATE.previewCache[which] = text;
  preview.innerHTML = renderMarkdownWithAssets(text, markdownAssetResolver(which));
  typesetMath(preview);
}

function updateActiveMarkdownPreview() {
  const which = STATE.activePanel;
  if (MARKDOWN_PANELS.includes(which)) updateMarkdownPreview(which);
}

function invalidatePreviewCache() {
  STATE.previewCache = {};
}

function initMarkdownPreviews() {
  MARKDOWN_PANELS.forEach((which) => {
    const editor = $(`#editor-${which}`);
    if (!editor) return;
    editor.addEventListener('input', () => {
      if (STATE.previewDebounce[which]) cancelAnimationFrame(STATE.previewDebounce[which]);
      STATE.previewDebounce[which] = requestAnimationFrame(() => {
        STATE.previewDebounce[which] = 0;
        updateMarkdownPreview(which, true);
      });
    });
  });
  updateActiveMarkdownPreview();
  injectMarkdownViewSwitchers();
}

function injectMarkdownViewSwitchers() {
  document.querySelectorAll('.markdown-workbench').forEach((wb) => {
    if (wb.querySelector('.md-view-switch')) return;
    wb.classList.add('markdown-workbench--view-edit');
    const bar = document.createElement('div');
    bar.className = 'md-view-switch';
    bar.setAttribute('role', 'tablist');
    bar.setAttribute('aria-label', 'Editor or preview');
    for (const view of ['edit', 'preview']) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'md-view-tab' + (view === 'edit' ? ' is-active' : '');
      btn.dataset.view = view;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', view === 'edit' ? 'true' : 'false');
      btn.textContent = view === 'edit' ? 'Edit' : 'Preview';
      btn.addEventListener('click', () => setMarkdownView(wb, view));
      bar.appendChild(btn);
    }
    wb.insertBefore(bar, wb.firstChild);
  });
}

function setMarkdownView(wb, view) {
  wb.classList.toggle('markdown-workbench--view-edit', view === 'edit');
  wb.classList.toggle('markdown-workbench--view-preview', view === 'preview');
  wb.querySelectorAll('.md-view-tab').forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (view === 'preview') {
    const which = STATE.activePanel;
    if (MARKDOWN_PANELS.includes(which)) updateMarkdownPreview(which, true);
  }
}

function previewTitle(which) {
  // "interview" reflects whichever Markdown file the user picked in the
  // embedded read-only viewer on the Claude tab.
  const names = {
    notes: 'NOTES.md',
    interview: STATE.interviewMdFile || 'PLAN.md',
  };
  const taskTitle = $('#task-title')?.textContent?.trim() || 'Task';
  return `${names[which] || 'Preview'} · ${taskTitle}`;
}

// ===== Embedded markdown picker (Claude tab) =====

// Apply a new task payload's file list + contents to STATE, refresh the
// <select> options, and load the currently selected file into the embed.
// Falls back to PLAN.md when the previous selection has disappeared.
function applyInterviewMdPayload(d) {
  const planText = d.templates && d.templates[FILES.plan] != null
    ? d.templates[FILES.plan]
    : '';
  let files = Array.isArray(d.task_markdown_files) && d.task_markdown_files.length
    ? d.task_markdown_files.slice()
    : [FILES.plan];
  if (!files.includes(FILES.plan)) files = [FILES.plan, ...files];
  const contents = {};
  for (const name of files) {
    if (d.templates && Object.prototype.hasOwnProperty.call(d.templates, name)) {
      contents[name] = d.templates[name];
    } else if (name === FILES.plan) {
      contents[name] = planText;
    } else {
      contents[name] = '';
    }
  }
  STATE.interviewMdFiles = files;
  STATE.interviewMdContents = contents;
  if (!files.includes(STATE.interviewMdFile)) {
    STATE.interviewMdFile = FILES.plan;
  }
  populateInterviewMdSelect();
  loadInterviewMdIntoEditor();
}

function populateInterviewMdSelect() {
  const sel = document.getElementById('interview-md-select');
  if (!sel) return;
  const current = STATE.interviewMdFile;
  const wanted = STATE.interviewMdFiles.map((name) => `${name}`).join('\u0000');
  if (sel.dataset.options !== wanted) {
    sel.innerHTML = '';
    for (const name of STATE.interviewMdFiles) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
    sel.dataset.options = wanted;
  }
  if (sel.value !== current) sel.value = current;
  updatePlanEditUI();
  if (!sel.dataset.bound) {
    sel.dataset.bound = '1';
    sel.addEventListener('change', onInterviewMdSelectChange);
  }
}

function onInterviewMdSelectChange(ev) {
  const name = ev.target.value;
  if (!name || !STATE.interviewMdFiles.includes(name)) return;
  // Switching files abandons any in-progress PLAN edit.
  STATE.planEditing = false;
  STATE.interviewMdFile = name;
  loadInterviewMdIntoEditor();
  updatePlanEditUI();
}

function loadInterviewMdIntoEditor() {
  const editor = document.getElementById('editor-interview');
  if (!editor) return;
  // Never overwrite the textarea while the user is mid-edit (the 12s poll calls
  // this); their unsaved changes would vanish.
  if (STATE.planEditing) return;
  const name = STATE.interviewMdFile;
  const text = STATE.interviewMdContents[name] || '';
  const changed = editor.value !== text;
  if (changed) {
    editor.value = text;
    STATE.previewCache.interview = null;
  }
  // Only re-render the preview when the text actually changed and the
  // user is looking at the panel - polling otherwise wastefully runs
  // the markdown renderer every 4s on unchanged content.
  if (changed && (STATE.activePanel === 'claude' || STATE.activePanel === 'interview')) {
    updateMarkdownPreview('interview', true);
  }
}

function updateInterviewMdHint() {
  const hint = document.getElementById('interview-md-hint');
  if (!hint) return;
  if (STATE.planEditing) {
    hint.textContent = 'Editing PLAN.md — Ctrl/Cmd+S to save. The agent can also write this file.';
  } else if (STATE.interviewMdFile === FILES.plan) {
    hint.textContent = 'PLAN.md — click Edit to modify it here.';
  } else {
    hint.textContent = `Read-only preview of ${STATE.interviewMdFile}.`;
  }
}

// Show Edit only for PLAN.md (the one writable template); Save/Cancel only while
// editing. Also toggles the textarea's readonly state.
function updatePlanEditUI() {
  const editBtn = document.getElementById('btn-plan-edit');
  const saveBtn = document.getElementById('btn-plan-save');
  const cancelBtn = document.getElementById('btn-plan-cancel');
  const editor = document.getElementById('editor-interview');
  const isPlan = STATE.interviewMdFile === FILES.plan;
  const editing = STATE.planEditing;
  if (editBtn) editBtn.hidden = !isPlan || editing;
  if (saveBtn) saveBtn.hidden = !editing;
  if (cancelBtn) cancelBtn.hidden = !editing;
  if (editor) editor.readOnly = !editing;
  updateInterviewMdHint();
}

function startPlanEdit() {
  if (STATE.interviewMdFile !== FILES.plan || !STATE.slug) return;
  STATE.planEditing = true;
  updatePlanEditUI();
  const editor = document.getElementById('editor-interview');
  if (editor) { editor.readOnly = false; try { editor.focus(); } catch (_) {} }
}

function cancelPlanEdit() {
  if (!STATE.planEditing) return;
  STATE.planEditing = false;
  updatePlanEditUI();
  // Reload the last-known file content, discarding edits.
  loadInterviewMdIntoEditor();
}

async function savePlanEdit() {
  if (!STATE.planEditing || !STATE.slug) return;
  const editor = document.getElementById('editor-interview');
  if (!editor) return;
  const content = editor.value;
  const saveBtn = document.getElementById('btn-plan-save');
  if (saveBtn) saveBtn.disabled = true;
  const slug = STATE.slug;
  try {
    await api('/api/tasks/' + encodeURIComponent(slug) + '/template', {
      method: 'PUT',
      body: JSON.stringify({ name: FILES.plan, content }),
    });
    // Reflect the saved content locally so the next poll doesn't show a diff.
    STATE.interviewMdContents[FILES.plan] = content;
    STATE.planEditing = false;
    updatePlanEditUI();
    STATE.previewCache.interview = null;
    updateMarkdownPreview('interview', true);
    toast('Saved PLAN.md', { type: 'success' });
  } catch (err) {
    toast(err.message || 'Failed to save PLAN.md', { type: 'error' });
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function openFullscreenPreview(which) {
  updateMarkdownPreview(which);
  const source = $(`#preview-${which}`);
  const modal = $('#preview-modal');
  const card = modal.querySelector('.preview-modal__card');
  const title = $('#preview-modal-title');
  const content = $('#preview-modal-content');
  if (!source || !modal || !card || !title || !content) return;
  title.textContent = previewTitle(which);
  content.innerHTML = source.innerHTML;
  modal.dataset.preview = which;
  modal.hidden = false;
  document.body.classList.add('preview-open');
  requestAnimationFrame(() => {
    content.scrollTop = 0;
    card.scrollTop = 0;
  });
  try {
    if (card.requestFullscreen && !document.fullscreenElement) await card.requestFullscreen();
  } catch { /* fullscreen may be blocked */ }
}

async function closeFullscreenPreview() {
  const modal = $('#preview-modal');
  if (!modal) return;
  modal.hidden = true;
  document.body.classList.remove('preview-open');
  if (document.fullscreenElement) {
    try { await document.exitFullscreen(); } catch { /* ignore */ }
  }
}

function printFullscreenPreview() {
  const modal = $('#preview-modal');
  if (!modal || modal.hidden) return;
  // Some browsers print the wrong viewport when triggered from within
  // requestFullscreen(); drop fullscreen first, let layout settle, then
  // print.  Two RAFs are enough for Chrome/Firefox to lay out the @media
  // print rules before window.print() snapshots them.
  const fire = () => {
    requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
  };
  if (document.fullscreenElement) {
    document.exitFullscreen().then(fire, fire);
  } else {
    fire();
  }
}

function initFullscreenPreviews() {
  MARKDOWN_PANELS.forEach((which) => {
    const preview = $(`#preview-${which}`);
    if (!preview) return;
    preview.title = 'Double-click to open fullscreen preview';
    preview.addEventListener('dblclick', () => openFullscreenPreview(which));
  });
}

// ===== Tasks =====

async function loadTasks() {
  if (!STATE.projectId) {
    STATE.tasks = [];
    renderTasksFromState();
    return;
  }
  // Replace the list only once the new one arrives; clearing first made a
  // failed refresh look like the project had lost all its tasks.
  const { tasks } = await api('/api/tasks');
  STATE.tasks = tasks || [];
  renderTasksFromState();
}

function clearTaskDropMarkers(root = document) {
  root.querySelectorAll('.task-list li.is-drop-before, .task-list li.is-drop-after').forEach((el) => {
    el.classList.remove('is-drop-before', 'is-drop-after');
  });
}

async function reorderTasksByDrag(dragSlug, targetSlug, afterTarget) {
  const slugs = (STATE.tasks || []).map((t) => t.slug);
  const from = slugs.indexOf(dragSlug);
  const target = slugs.indexOf(targetSlug);
  if (from < 0 || target < 0 || dragSlug === targetSlug) return;
  slugs.splice(from, 1);
  const targetAfterRemoval = slugs.indexOf(targetSlug);
  slugs.splice(targetAfterRemoval + (afterTarget ? 1 : 0), 0, dragSlug);
  if (slugs.every((slug, idx) => slug === (STATE.tasks[idx] && STATE.tasks[idx].slug))) return;
  const bySlug = new Map((STATE.tasks || []).map((t) => [t.slug, t]));
  STATE.tasks = slugs.map((slug) => bySlug.get(slug)).filter(Boolean);
  renderTasksFromState();
  try {
    const d = await api('/api/tasks/reorder', {
      method: 'POST',
      body: JSON.stringify({ slugs }),
    });
    STATE.tasks = d.tasks || STATE.tasks || [];
    renderTasksFromState();
  } catch (e) {
    toast(e.message, { type: 'error' });
    await loadTasks();
  }
}

function renderTasksFromState() {
  const ul = $('#task-list');
  if (!ul) return;
  const selected = STATE.slug;
  ul.innerHTML = '';
  const all = STATE.tasks || [];
  const filter = (STATE.taskFilter || '').trim().toLowerCase();
  const tasks = filter
    ? all.filter((t) => `${t.title || ''} ${t.slug || ''}`.toLowerCase().includes(filter))
    : all;
  const countEl = document.getElementById('task-count');
  if (countEl) {
    countEl.textContent = filter && all.length !== tasks.length
      ? `${tasks.length}/${all.length}`
      : (all.length ? String(all.length) : '');
  }
  if (!tasks.length) {
    const li = document.createElement('li');
    li.className = 'task-list__empty';
    if (!STATE.projectId) li.textContent = 'Select or add a project';
    else if (filter) li.textContent = `No tasks match "${filter}"`;
    else li.textContent = 'No tasks yet';
    ul.appendChild(li);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const t of tasks) {
    const li = document.createElement('li');
    li.dataset.slug = t.slug;
    li.draggable = true;
    li.tabIndex = 0;
    li.title = `${t.slug} · ${taskBackendLabel(t)}`;
    if (t.slug === selected) li.classList.add('active');
    const typeLabel = isArKind(t.kind) ? 'AR' : (isAuthorKind(t.kind) ? 'Author' : agentLabel(t.agent));
    const kindClass = isArKind(t.kind) ? 'ar' : (t.kind || 'agent');
    li.innerHTML =
      `<div class="task-title-row"><span class="task-title">${escapeHtml(t.title)}</span>` +
      `<span class="task-kind task-kind--${escapeHtml(kindClass)}">${escapeHtml(typeLabel)}</span></div>`;
    li.addEventListener('dragstart', (ev) => {
      TASK_DRAG_SLUG = t.slug;
      TASK_JUST_DRAGGED = true;
      li.classList.add('is-dragging');
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', t.slug);
    });
    li.addEventListener('dragover', (ev) => {
      if (!TASK_DRAG_SLUG || TASK_DRAG_SLUG === t.slug) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      const rect = li.getBoundingClientRect();
      const after = ev.clientY > rect.top + (rect.height / 2);
      clearTaskDropMarkers(ul);
      li.classList.toggle('is-drop-before', !after);
      li.classList.toggle('is-drop-after', after);
    });
    li.addEventListener('drop', async (ev) => {
      if (!TASK_DRAG_SLUG || TASK_DRAG_SLUG === t.slug) return;
      ev.preventDefault();
      const dragSlug = ev.dataTransfer.getData('text/plain') || TASK_DRAG_SLUG;
      const after = li.classList.contains('is-drop-after');
      clearTaskDropMarkers(ul);
      await reorderTasksByDrag(dragSlug, t.slug, after);
    });
    li.addEventListener('dragend', () => {
      TASK_DRAG_SLUG = '';
      li.classList.remove('is-dragging');
      clearTaskDropMarkers(ul);
      setTimeout(() => { TASK_JUST_DRAGGED = false; }, 0);
    });
    li.addEventListener('click', () => {
      if (TASK_JUST_DRAGGED) return;
      if (STATE.slug === t.slug) {
        // User explicitly deselected -> stop auto-restoring it for this project.
        forgetSelectedTask();
        clearTaskSelection();
      } else {
        selectTask(t.slug);
        if (isMobileViewport()) setSidebarOpen(false);
      }
    });
    li.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        li.click();
      }
    });
    fragment.appendChild(li);
  }
  ul.appendChild(fragment);
  applyActivity();
}

function clearTaskSelection() {
  savePaneDraftForTask(STATE.slug);
  STATE.slug = null;
  STATE.currentMeta = null;
  STATE.worktreeStatuses = [];
  STATE.taskRoot = '';
  STATE.planPath = '';
  if (STATE.paneTimer) {
    clearInterval(STATE.paneTimer);
    STATE.paneTimer = null;
  }
  document.querySelectorAll('#task-list li').forEach((li) => li.classList.remove('active'));
  restorePaneDraftForTask(null);
  $('#task-view').hidden = true;
  $('#task-empty').hidden = false;
  renderTaskSkillsPicker({});
}

const _LAST_TASK_LS_KEY = 'loom.lastTaskByProject';

function loadLastTaskMap() {
  try {
    const raw = localStorage.getItem(_LAST_TASK_LS_KEY);
    const obj = raw ? JSON.parse(raw) : null;
    if (obj && typeof obj === 'object') STATE.lastTaskByProject = obj;
  } catch (_) { /* localStorage unavailable / corrupt - ignore */ }
}

function persistLastTaskMap() {
  try { localStorage.setItem(_LAST_TASK_LS_KEY, JSON.stringify(STATE.lastTaskByProject || {})); }
  catch (_) { /* ignore */ }
}

function rememberSelectedTask(slug) {
  if (!slug) return;
  STATE.lastTaskByProject[STATE.projectId || 'default'] = slug;
  persistLastTaskMap();
}

function forgetSelectedTask() {
  const pid = STATE.projectId || 'default';
  if (STATE.lastTaskByProject[pid] != null) {
    delete STATE.lastTaskByProject[pid];
    persistLastTaskMap();
  }
}

// Re-select the task remembered for the current project (if it still exists and
// nothing is selected yet). Called after a project's tasks finish loading so
// switching projects / reloading restores the task you were on.
async function restoreSelectedTaskForProject() {
  if (!STATE.projectId || STATE.slug) return;
  const slug = STATE.lastTaskByProject[STATE.projectId];
  if (!slug) return;
  if (!(STATE.tasks || []).some((t) => t.slug === slug)) {
    forgetSelectedTask();   // stale (task deleted/renamed) - drop it
    return;
  }
  await selectTask(slug);
}

async function selectTask(slug) {
  if (STATE.slug && STATE.slug !== slug) {
    savePaneDraftForTask(STATE.slug);
  }
  STATE.slug = slug;
  rememberSelectedTask(slug);
  STATE.planEditing = false;  // never carry an in-progress PLAN edit across tasks
  STATE.changesData = null;
  STATE.changesSelected = '';
  resetArLab();
  ackActivity(slug);
  document.querySelectorAll('#task-list li').forEach((li) => {
    li.classList.toggle('active', li.dataset.slug === slug);
  });

  // ---------- Optimistic render ----------
  // The sidebar's loadTasks() already cached the full TaskMeta for every
  // task. Render the header / tab bar / agent labels from that cache
  // BEFORE awaiting the API so the click feels instant; the heavier
  // /api/tasks/<slug> response (worktree git status + claude session
  // enrichment + markdown contents) then enriches the view in-place.
  const cached = (STATE.tasks || []).find((t) => t.slug === slug) || null;
  if (cached) {
    STATE.currentMeta = cached;
    STATE.worktreeStatuses = [];
    $('#task-empty').hidden = true;
    $('#task-view').hidden = false;
    $('#task-title').textContent = cached.title || slug;
    $('#task-backend').textContent = taskBackendLabel(cached);
    $('#task-goal').textContent = cached.general_goal || '';
    $('#inp-interview-target').value = cached.tmux_interview_target || '';
    setTmuxOutputText(cached.tmux_interview_target
      ? 'Loading agent pane…'
      : `Click Start ${agentLabel(cached.agent)} to launch a tmux pane in the worktree.`);
    // Empty out the markdown viewer so the previous task's content
    // doesn't briefly flash through.
    $('#editor-interview').value = '';
    restorePaneDraftForTask(slug);
    STATE.interviewMdContents = {};
    STATE.previewCache = {};
    applyAgentLabels(cached);
    buildTabs(cached);
    if (isArKind(cached.kind)) showPanel('ar');
    else showPanel(DEFAULT_TAB);
  }

  let d;
  try {
    d = await apiWithRetry('/api/tasks/' + encodeURIComponent(slug), {}, { attempts: 4, delayMs: 300 });
  } catch (err) {
    if (STATE.slug !== slug) return;
    console.debug('selectTask detail load failed', err);
    const msg = isTransientApiError(err)
      ? 'Temporary gateway error while refreshing task details; kept cached task view.'
      : `Failed to refresh task details: ${err.message || err}`;
    const backend = document.getElementById('task-backend');
    if (backend && cached) backend.textContent = `${backend.textContent.replace(/ · refresh failed.*$/, '')} · refresh failed`;
    if (!cached) setTmuxOutputText(msg);
    return;
  }
  // The user may have clicked a different task while we were awaiting -
  // abort cleanly so we don't trample the newer selection.
  if (STATE.slug !== slug) return;

  // ---------- Full render with fresh server data ----------
  $('#task-empty').hidden = true;
  $('#task-view').hidden = false;
  $('#task-title').textContent = d.meta.title || slug;
  $('#task-backend').textContent = taskBackendLabel(d.meta);
  $('#task-goal').textContent = d.meta.general_goal || '';
  STATE.currentMeta = d.meta || null;
  STATE.worktreeStatuses = d.worktree_statuses || [];
  STATE.skillsMissing = d.skills_missing || [];
  STATE.taskRoot = d.task_root || '';
  STATE.planPath = d.plan_path || '';
  applyInterviewMdPayload(d);
  invalidatePreviewCache();
  updateActiveMarkdownPreview();
  $('#inp-interview-target').value = d.meta.tmux_interview_target || '';
  if (!d.meta.tmux_interview_target) {
    disconnectTerminal();
    setTmuxOutputText(`Click Start ${agentLabel(d.meta.agent)} to launch a tmux pane in the worktree.`);
  }
  restorePaneDraftForTask(slug);
  renderClaudeInfo(d.meta, d.claude || null, STATE.worktreeStatuses);
  applyAgentLabels(d.meta || {});
  buildTabs(d.meta);
  // Keep the user on whatever panel optimistic-render showed (DEFAULT_TAB
  // by default); calling showPanel again would re-trigger the deferred
  // refresh callbacks unnecessarily.
  if (!cached) {
    if (isArKind(d.meta.kind)) { showPanel('ar'); initArLab(d.meta); }
    else showPanel(DEFAULT_TAB);
  } else if (isArKind(d.meta.kind)) {
    initArLab(d.meta);
  }
  refreshInterviewPreview(true);
  refreshClaudeSessions();
  startPanePolling();
}

function applyAgentLabels(meta) {
  const label = agentLabel(meta.agent);
  const startBtn = document.getElementById('btn-interview-start');
  const pasteBtn = document.getElementById('btn-interview-paste');
  const stopBtn = document.getElementById('btn-interview-stop');
  if (startBtn) startBtn.textContent = `Start ${label}`;
  if (pasteBtn) pasteBtn.textContent = isArKind(meta && meta.kind) ? 'Send AR prompt' : 'Start Deep Interview';
  if (stopBtn) stopBtn.textContent = `Stop ${label}`;
  const heading = document.querySelector('.tab-panel[data-panel="claude"] .terminal-card__bar h4');
  if (heading) heading.textContent = `${label} Terminal`;
  if (!TERM.connected && !termTarget()) {
    setTmuxOutputText(`Click Start ${label} to launch a tmux pane in the worktree.`);
  }
  // Show agent in info card + bind change.
  const sel = document.getElementById('claude-info-agent');
  if (sel) {
    sel.value = normalizeAgent(meta.agent);
    // bind once
    if (!sel.dataset.bound) {
      sel.dataset.bound = '1';
      sel.addEventListener('change', onAgentChange);
    }
  }
  renderTaskModelPicker(meta);
  renderTaskSkillsPicker(meta);
}

function renderTaskModelPicker(meta = STATE.currentMeta || {}) {
  const input = document.getElementById('claude-info-model');
  const select = document.getElementById('claude-info-model-select');
  if (!input || !select) return;
  const agent = normalizeAgent(meta.agent);
  populateModelPicker(
    input,
    select,
    agent,
    String(meta.interview_model || '').trim()
      || (STATE.modelDefaults && STATE.modelDefaults[agent])
      || '',
  );
  input.disabled = !STATE.slug;
  select.disabled = !STATE.slug;
  if (!input.dataset.bound) {
    input.dataset.bound = '1';
    input.addEventListener('change', onTaskModelChange);
  }
}

async function onTaskModelChange(ev) {
  const input = ev.target;
  if (!STATE.slug) return;
  const previous = STATE.currentMeta?.interview_model || '';
  input.disabled = true;
  try {
    const r = await saveTaskMeta({ interview_model: input.value.trim() });
    if (r?.meta) {
      STATE.currentMeta = r.meta;
      renderTaskModelPicker(r.meta);
      const backend = document.getElementById('task-backend');
      if (backend) backend.textContent = taskBackendLabel(r.meta);
      const hint = document.getElementById('claude-info-model-hint');
      if (hint) {
        hint.textContent = 'saved for next start';
        setTimeout(() => { hint.textContent = 'used on next start'; }, 1800);
      }
    }
  } catch (err) {
    toast(err.message || 'failed to update model', { type: 'error' });
    input.value = previous;
  } finally {
    input.disabled = false;
  }
}

function renderTaskSkillsPicker(meta = STATE.currentMeta || {}) {
  const sel = document.getElementById('claude-info-skills');
  if (!sel) return;
  sel.multiple = true;
  const current = String(meta.skills_path || STATE.skillsPath || '').trim();
  const currentPaths = splitSkillsValue(current);
  const options = STATE.skillsOptions.length
    ? STATE.skillsOptions.slice()
    : currentPaths.map((p) => ({ label: p, path: p }));
  for (const p of currentPaths) {
    if (!options.some((opt) => String(opt.path || '') === p)) {
      options.unshift({ label: p, path: p });
    }
  }
  const wanted = options.map((opt) => String(opt.path || '')).join('\u0000');
  if (sel.dataset.options !== wanted) {
    sel.innerHTML = '';
    for (const opt of options) {
      const path = String(opt.path || '').trim();
      if (!path) continue;
      const option = document.createElement('option');
      option.value = path;
      option.textContent = opt.label || path;
      option.title = path;
      sel.appendChild(option);
    }
    sel.dataset.options = wanted;
  }
  sel.disabled = !STATE.slug || sel.options.length === 0;
  applySkillsSelection(sel, current);
  renderSkillChips(sel);
  const hint = document.getElementById('claude-info-skills-hint');
  if (hint) {
    const gone = currentPaths.filter((p) => (STATE.skillsMissing || []).includes(p));
    const plural = gone.length > 1 ? 's' : '';
    hint.textContent = !gone.length
      ? 'used when starting deep interview'
      : (gone.length === currentPaths.length
        ? `file${plural} missing — the agent falls back to the default skill`
        : `${gone.length} missing file${plural} ignored`);
    hint.classList.toggle('claude-info__hint--warn', gone.length > 0);
  }
  if (!sel.dataset.bound) {
    sel.dataset.bound = '1';
    sel.addEventListener('change', onTaskSkillsChange);
  }
}

async function onTaskSkillsChange(ev) {
  const sel = ev.target;
  const skillsPath = selectedSkillsValue(sel);
  if (!STATE.slug || !skillsPath) return;  // keep at least one skill selected
  const previous = STATE.currentMeta?.skills_path || STATE.skillsPath || '';
  sel.disabled = true;
  renderSkillChips(sel);
  try {
    const r = await saveTaskMeta({ skills_path: skillsPath });
    if (r?.meta) {
      renderTaskSkillsPicker(r.meta);
      const hint = document.getElementById('claude-info-skills-hint');
      if (hint) {
        hint.textContent = 'saved for next deep interview';
        setTimeout(() => {
          hint.textContent = 'used when starting deep interview';
        }, 1800);
      }
    }
  } catch (err) {
    toast(err.message || 'failed to update skills', { type: 'error' });
    if (previous) applySkillsSelection(sel, previous);
  } finally {
    sel.disabled = false;
    renderSkillChips(sel);
  }
}

async function onAgentChange(ev) {
  const sel = ev.target;
  const next = sel.value;
  if (!STATE.slug) return;
  // Look at the current meta to confirm change is meaningful.
  if (!confirm(`Switch agent to ${agentLabel(next)}? Stop any running pane first; the new pane will use the ${agentLabel(next)} CLI.`)) {
    sel.value = sel.dataset.previous || 'claude';
    return;
  }
  sel.dataset.previous = next;
  try {
    const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/meta', {
      method: 'PUT',
      body: JSON.stringify({ agent: next }),
    });
    if (r.meta) await selectTask(STATE.slug);
  } catch (err) {
    toast(err.message || 'agent switch failed', { type: 'error' });
  }
}

// ===== Per-task terminal drafts =====

function paneDraftKey(slug = STATE.slug) {
  if (!slug) return '';
  // Slugs can repeat across projects, so include the active project id.
  return `${STATE.projectId || 'default'}::${slug}`;
}

function savePaneDraftForTask(slug = STATE.slug) {
  const key = paneDraftKey(slug);
  if (!key) return;
  const compose = document.getElementById('term-compose-input');
  if (compose) STATE.composeDrafts[key] = compose.value;
}

function restorePaneDraftForTask(slug = STATE.slug) {
  const key = paneDraftKey(slug);
  const compose = document.getElementById('term-compose-input');
  if (compose) {
    compose.value = key ? (STATE.composeDrafts[key] || '') : '';
    // Resize the auto-growing textarea to fit the restored draft.
    compose.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

function clearPaneDraftForTask(slug = STATE.slug) {
  const key = paneDraftKey(slug);
  if (!key) return;
  STATE.paneDrafts[key] = '';
  STATE.composeDrafts[key] = '';
}

async function deleteSelectedTask() {
  if (!STATE.slug) return;
  const slug = STATE.slug;
  const title = $('#task-title')?.textContent || slug;
  const ok = confirm(
    `Delete task "${title}" (${slug})?\n\n` +
    `This permanently removes .RUD/${slug}/, including PLAN.md, the worktree, and task metadata. ` +
    `Running tmux sessions are not stopped automatically.`
  );
  if (!ok) return;
  const btn = document.getElementById('btn-delete-task');
  if (btn) btn.disabled = true;
  try {
    await api('/api/tasks/' + encodeURIComponent(slug), { method: 'DELETE' });
    clearTaskSelection();
    await loadTasks();
  } catch (e) {
    toast(e.message, { type: 'error' });
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refreshTaskTemplates() {
  if (!STATE.slug) return;
  if (STATE.pollInFlight.templates) return;
  const slug = STATE.slug;
  STATE.pollInFlight.templates = true;
  try {
    const d = await apiWithRetry('/api/tasks/' + encodeURIComponent(slug), {}, { attempts: 2, delayMs: 300 });
    if (STATE.slug !== slug) return;
    STATE.currentMeta = d.meta || STATE.currentMeta;
    STATE.worktreeStatuses = d.worktree_statuses || STATE.worktreeStatuses || [];
    STATE.taskRoot = d.task_root || STATE.taskRoot || '';
    STATE.planPath = d.plan_path || STATE.planPath || '';
    // Sync the picker payload (file list + content) and reload the embed
    // from the freshly fetched contents.
    applyInterviewMdPayload(d);
    if (d.meta) renderClaudeInfo(d.meta, d.claude || null, STATE.worktreeStatuses);
  } catch (err) {
    console.debug('refreshTaskTemplates failed', err);
  } finally {
    STATE.pollInFlight.templates = false;
  }
}

async function saveTemplate(name, textareaId, statusId) {
  if (!STATE.slug) return;
  const content = $(textareaId).value;
  await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/template', {
    method: 'PUT',
    body: JSON.stringify({ name, content }),
  });
  if (statusId) {
    $(statusId).textContent = 'Saved';
    setTimeout(() => { $(statusId).textContent = ''; }, 2000);
  }
}

// ===== Interview pane (tmux) =====

// Smart updater for the captured-tmux <pre>. By default we scroll to the
// bottom (most-recent output, the way a real terminal feels). If the
// user has scrolled up to read earlier output, we leave their position
// alone so polling doesn't yank them away.
function setTmuxOutputText(text) {
  // Status messages only show when no live PTY stream is attached; once the
  // terminal is streaming, the real output owns the screen.
  const term = ensureTerminal();
  if (!term || TERM.connected) return;
  try {
    term.reset();
    term.write(String(text == null ? '' : text).replace(/\r?\n/g, '\r\n'));
  } catch (e) {}
}

function scrollTmuxOutputToBottom() { /* xterm.js auto-scrolls on write */ }

function revealInterviewTerminal(block = 'center') {
  const card = document.querySelector('.terminal-card--interview');
  if (!card) return;
  requestAnimationFrame(() => {
    card.scrollIntoView({ block, inline: 'nearest', behavior: 'smooth' });
  });
  // Showing the terminal can change its available width (tab becomes visible,
  // sidebar drawer closes); refit so it matches the now-visible size.
  setTimeout(() => termHandleResize(), 80);
}

async function refreshInterviewPreview() {
  // With xterm + a live PTY stream there's nothing to poll: just ensure the
  // terminal is attached to the current pane (idempotent; reconnects if dropped).
  const target = termTarget();
  if (!target) { disconnectTerminal(); return; }
  connectTerminal(target, false);
}

// ===== Changes (read-only git diff) tab =====

const CHANGES_STATUS_GLYPH = { added: 'A', deleted: 'D', renamed: 'R', modified: 'M' };

function changesBaseName(p) {
  const s = String(p || '').replace(/\/+$/, '');
  const i = s.lastIndexOf('/');
  return i >= 0 ? s.slice(i + 1) : s;
}

function changesFileByKey(d, key) {
  if (!d || !key) return null;
  const [wi, fi] = key.split(':').map(Number);
  const wt = d.worktrees && d.worktrees[wi];
  if (!wt) return null;
  return (wt.files && wt.files[fi]) || null;
}

async function refreshChangesView(force = false) {
  if (!STATE.slug) return;
  const body = document.getElementById('changes-body');
  const statusEl = document.getElementById('changes-status');
  if (!body) return;
  if (STATE.changesData && !force) {
    renderChanges(STATE.changesData);
  } else if (!STATE.changesData) {
    body.innerHTML = '<div class="changes-empty">Loading changes…</div>';
  }
  if (STATE.changesLoading) return;
  STATE.changesLoading = true;
  if (statusEl) statusEl.textContent = 'Loading…';
  const slug = STATE.slug;
  try {
    const d = await apiWithRetry(
      '/api/tasks/' + encodeURIComponent(slug) + '/diff', {}, { attempts: 2 }
    );
    if (STATE.slug !== slug) return;
    STATE.changesData = d;
    renderChanges(d);
    if (statusEl) statusEl.textContent = '';
  } catch (err) {
    if (statusEl) statusEl.textContent = '';
    if (!STATE.changesData) {
      body.innerHTML = '<div class="changes-empty">Failed to load changes: ' + escapeHtml(err.message) + '</div>';
    }
  } finally {
    STATE.changesLoading = false;
  }
}

function changesFileRowHtml(f, key, sel) {
  const st = f.status || 'modified';
  const glyph = CHANGES_STATUS_GLYPH[st] || 'M';
  const stat = (f.additions || f.deletions)
    ? '<span class="changes-file__stat"><span class="add">+' + (f.additions || 0) + '</span><span class="del">-' + (f.deletions || 0) + '</span></span>'
    : '';
  const name = f.old_path
    ? (escapeHtml(f.old_path) + ' → ' + escapeHtml(f.path))
    : escapeHtml(f.path);
  return '<button type="button" class="changes-file' + (key === sel ? ' is-active' : '') + '" data-key="' + key + '" title="' + escapeHtml(f.path) + '">' +
    '<span class="changes-file__status changes-file__status--' + st + '">' + glyph + '</span>' +
    '<span class="changes-file__path">' + name + '</span>' + stat + '</button>';
}

function renderChanges(d) {
  const body = document.getElementById('changes-body');
  if (!body) return;
  const previousListTop = body.querySelector('.changes-filelist')?.scrollTop || 0;
  const previousDiffTop = body.querySelector('.diffview')?.scrollTop || 0;
  const worktrees = (d && d.worktrees) || [];
  const totalFiles = worktrees.reduce((n, wt) => n + ((wt.files || []).length), 0);
  if (!worktrees.length) {
    const agent = agentLabel(STATE.currentMeta && STATE.currentMeta.agent);
    body.innerHTML = '<div class="changes-empty">No worktree registered for this task yet. Create one from the ' + escapeHtml(agent) + ' tab to see changes here.</div>';
    return;
  }
  if (!totalFiles) {
    body.innerHTML = '<div class="changes-empty">No changes — every worktree matches its base branch with a clean working tree.</div>';
    return;
  }
  let sel = STATE.changesSelected;
  if (!changesFileByKey(d, sel)) {
    sel = '';
    for (let wi = 0; wi < worktrees.length && !sel; wi++) {
      if ((worktrees[wi].files || []).length) sel = wi + ':0';
    }
    STATE.changesSelected = sel;
  }
  const listParts = [];
  worktrees.forEach((wt, wi) => {
    const files = wt.files || [];
    listParts.push('<div class="changes-wt">');
    if (worktrees.length > 1) {
      listParts.push(
        '<div class="changes-scope" title="' + escapeHtml(wt.path) + '">' +
        escapeHtml(changesBaseName(wt.path)) + '</div>'
      );
    }
    if (!files.length) {
      listParts.push('<div class="changes-wt__empty">clean</div>');
    } else {
      files.forEach((file, index) => {
        listParts.push(changesFileRowHtml(file, wi + ':' + index, sel));
      });
    }
    listParts.push('</div>');
  });
  body.innerHTML =
    '<div class="changes-summary">' + totalFiles + ' changed file' + (totalFiles === 1 ? '' : 's') + '</div>' +
    '<div class="changes-layout">' +
      '<div class="changes-filelist">' + listParts.join('') + '</div>' +
      '<div class="changes-diff" id="changes-diff"></div>' +
    '</div>';
  body.querySelectorAll('.changes-file').forEach((btn) => {
    btn.addEventListener('click', () => {
      STATE.changesSelected = btn.dataset.key;
      body.querySelectorAll('.changes-file').forEach((b) => b.classList.toggle('is-active', b === btn));
      renderChangesDiffPanel();
    });
    btn.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      const files = [...body.querySelectorAll('.changes-file')];
      const current = files.indexOf(btn);
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      const next = files[Math.max(0, Math.min(files.length - 1, current + delta))];
      if (next && next !== btn) {
        next.focus();
        next.click();
      }
    });
  });
  renderChangesDiffPanel();
  const list = body.querySelector('.changes-filelist');
  const diff = body.querySelector('.diffview');
  if (list) list.scrollTop = previousListTop;
  if (diff) diff.scrollTop = previousDiffTop;
}

function renderDiffBody(f) {
  if (!f.patch || !f.patch.trim()) {
    return '<div class="changes-empty">' + (f.binary ? 'Binary file — no text preview.' : 'No diff preview available.') + '</div>';
  }
  const out = [];
  let oldLine = null;
  let newLine = null;
  for (const line of f.patch.split('\n')) {
    let cls = 'ctx';
    if (line.startsWith('@@')) {
      cls = 'hunk';
      const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
    }
    else if (
      line.startsWith('+++') || line.startsWith('---') ||
      line.startsWith('diff --git') || line.startsWith('index ') ||
      line.startsWith('new file') || line.startsWith('deleted file') ||
      line.startsWith('rename ') || line.startsWith('similarity ') ||
      line.startsWith('old mode') || line.startsWith('new mode') ||
      line.startsWith('Binary ') || line.startsWith('\\ No newline')
    ) continue; // show code, not git transport metadata
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    if (cls === 'hunk') {
      out.push(
        '<div class="diffline diffline--hunk"><span class="diffline__hunk">' +
        (escapeHtml(line) || '&#8203;') + '</span></div>'
      );
      continue;
    }
    let oldNo = '';
    let newNo = '';
    if (cls === 'add') {
      newNo = newLine != null ? String(newLine++) : '';
    } else if (cls === 'del') {
      oldNo = oldLine != null ? String(oldLine++) : '';
    } else {
      oldNo = oldLine != null ? String(oldLine++) : '';
      newNo = newLine != null ? String(newLine++) : '';
    }
    out.push(
      '<div class="diffline diffline--' + cls + '">' +
      '<span class="diffline__no">' + oldNo + '</span>' +
      '<span class="diffline__no">' + newNo + '</span>' +
      '<span class="diffline__code">' + (escapeHtml(line) || '&#8203;') + '</span>' +
      '</div>'
    );
  }
  return '<div class="diffview">' + out.join('') + '</div>';
}

function renderChangesDiffPanel() {
  const host = document.getElementById('changes-diff');
  if (!host) return;
  const f = changesFileByKey(STATE.changesData, STATE.changesSelected);
  if (!f) {
    host.innerHTML = '<div class="changes-empty">Select a file to view its diff.</div>';
    return;
  }
  const glyph = CHANGES_STATUS_GLYPH[f.status] || 'M';
  const name = f.old_path ? (escapeHtml(f.old_path) + ' → ' + escapeHtml(f.path)) : escapeHtml(f.path);
  host.innerHTML =
    '<div class="changes-diff__head">' +
      '<span class="changes-file__status changes-file__status--' + (f.status || 'modified') + '">' + glyph + '</span>' +
      '<span class="changes-diff__path">' + name + '</span>' +
    '</div>' +
    renderDiffBody(f);
}

// ===== Run monitor (tmux watcher -> OpenClaw) =====

function formatMonitorTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch (_) { return iso; }
}

function applyMonitorState(d) {
  const toggle = document.getElementById('monitor-toggle');
  if (!toggle) return;
  // The toggle itself shows on/off state - no extra status text.
  toggle.checked = !!(d && d.running);
}

async function loadMonitor() {
  if (!STATE.slug) return;
  const slug = STATE.slug;
  try {
    const d = await api('/api/tasks/' + encodeURIComponent(slug) + '/monitor');
    // Don't clobber the toggle/input while the user is mid-action or has
    // switched tasks - the in-flight setMonitor() is the source of truth.
    if (STATE.slug !== slug || STATE.monitorBusy) return;
    applyMonitorState(d);
  } catch (err) {
    console.debug('loadMonitor failed', err);
  }
}

async function setMonitor(enabled) {
  if (!STATE.slug) return;
  const slug = STATE.slug;
  STATE.monitorBusy = true;
  try {
    let d;
    if (enabled) {
      // No pattern from the UI - the backend uses its hardcoded default.
      d = await api('/api/tasks/' + encodeURIComponent(slug) + '/monitor', {
        method: 'POST',
        body: JSON.stringify({}),
      });
    } else {
      d = await api('/api/tasks/' + encodeURIComponent(slug) + '/monitor', { method: 'DELETE' });
    }
    if (STATE.slug !== slug) return;
    applyMonitorState(d);
  } catch (err) {
    console.debug('setMonitor failed', err);
    STATE.monitorBusy = false;
    loadMonitor();
  } finally {
    STATE.monitorBusy = false;
  }
}

// ===== Real terminal (xterm.js) bound to the tmux pane via a live PTY stream.
// Output: GET /api/tmux/stream chunks the actual terminal bytes from a
// `tmux attach` PTY. Input is written back to THAT SAME PTY via stream-input.
// This matters because xterm also emits automatic terminal capability/color
// replies through onData; sending those directly to the pane makes Cursor show
// them as literal `^[[?1;2c...` prompt text instead of tmux consuming them. =====
const TERM = {
  term: null,
  fit: null,
  target: '',
  streamId: '',
  abort: null,
  connected: false,
  inputQueue: [],   // one entry per xterm onData datum (keystroke/sequence/paste)
  inputSending: false,
  resizeTimer: null,
  heartbeatTimer: null,
  lastSelection: '',
};

function termTarget() {
  const el = document.getElementById('inp-interview-target');
  const v = el ? el.value.trim() : '';
  // While the live stream is attached, keep sending keys to ITS pane even if a
  // task-meta refresh momentarily rewrote or cleared the target input box —
  // otherwise keystrokes are silently dropped ("terminal stops responding").
  return v || (TERM.connected ? TERM.target : '');
}

// --- clipboard (works over plain http:// where navigator.clipboard is blocked) -
function termClipboardWrite(text) {
  if (!text) return;
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => termFlashCopied())
      .catch(() => { if (termExecCopy(text)) termFlashCopied(); });
  } else {
    if (termExecCopy(text)) termFlashCopied();
  }
}

function termExecCopy(text) {
  let ok = false;
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // Must be IN the viewport (not off-screen) or execCommand('copy') is a
    // silent no-op in several browsers. Keep it invisible but on-screen.
    ta.style.cssText =
      'position:fixed;left:0;top:0;width:2em;height:2em;padding:0;border:0;' +
      'margin:0;opacity:0;background:transparent;z-index:-1;';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { ta.setSelectionRange(0, text.length); } catch (e) {}
    ok = document.execCommand('copy');
    document.body.removeChild(ta);
  } catch (e) { console.debug('copy failed', e); ok = false; }
  if (TERM.term) { try { TERM.term.focus(); } catch (e) {} }
  return ok;
}

function termCopySelection() {
  if (!TERM.term) return;
  // Prefer the snapshot captured when the selection last changed: a full-screen
  // app repaints under a fixed selection, so a fresh getSelection() here can be
  // offset by a few rows. Fall back to a live read only if we have no snapshot.
  const sel = TERM.lastSelection || TERM.term.getSelection() || '';
  if (sel) termClipboardWrite(sel);
}

// Terminal-style copy/paste shortcuts. Returns false to stop xterm from also
// sending the key to the pane.
function termKeyEvent(e, term) {
  if (e.type !== 'keydown') return true;
  const k = (e.key || '').toLowerCase();
  // Own bare Escape explicitly. Page-level modal shortcuts and browser
  // handlers otherwise sometimes consume it before xterm emits `\x1b`,
  // making Cursor Agent's "Esc to go back" menus appear stuck.
  if (k === 'escape' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
    e.preventDefault();
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    termQueueInput('\x1b');
    return false;
  }
  // Copy: Cmd+C (mac), Ctrl+Shift+C, or Ctrl+C while text is selected.
  if ((e.metaKey && k === 'c')
      || (e.ctrlKey && e.shiftKey && k === 'c')
      || (e.ctrlKey && !e.shiftKey && k === 'c' && term.hasSelection())) {
    termCopySelection();
    return false;
  }
  // Paste: only suppress xterm's ^V on Ctrl+V (Linux/Win). Cmd+V (mac) makes
  // xterm send nothing, and intercepting it here swallows the browser's native
  // 'paste' event - so let Cmd+V through; the 'paste' listener does the paste.
  if (e.ctrlKey && !e.metaKey && k === 'v') {
    return false;
  }
  // Arrow keys are left to xterm's native handling on purpose: it emits the
  // correct sequence for the pane's cursor-key mode AND scrolls the viewport to
  // the bottom on input (so a menu at the bottom stays in view). Intercepting
  // them and returning false dropped that scroll-to-bottom and made the screen
  // jump up to the transcript while navigating Claude's menus.
  return true;
}

function ensureTerminal() {
  if (TERM.term) return TERM.term;
  const host = document.getElementById('interview-term');
  if (!host || typeof Terminal === 'undefined') return null;
  const term = new Terminal({
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 13,
    lineHeight: 1.15,
    cursorBlink: true,
    cursorInactiveStyle: 'outline',
    macOptionIsMeta: true,
    scrollback: 8000,
    theme: {
      // Warm-dark: Claude's TUI assumes a dark terminal, so white/bright text
      // needs a dark background to stay readable.
      background: '#211d1a', foreground: '#e7ddcf',
      cursor: '#f59e0b', cursorAccent: '#211d1a',
      selectionBackground: 'rgba(245,158,11,0.30)',
      black: '#2b2620', red: '#e06c5a', green: '#9ec46a', yellow: '#e0af68',
      blue: '#7aa2f7', magenta: '#c79bf0', cyan: '#79c7c7', white: '#d8cfc2',
      brightBlack: '#7a6f60', brightRed: '#f08a7a', brightGreen: '#b6d98a', brightYellow: '#f0c987',
      brightBlue: '#9bb8fa', brightMagenta: '#d4b3f5', brightCyan: '#9bd9d9', brightWhite: '#fdf6ea',
    },
  });
  let fit = null;
  try { fit = new FitAddon.FitAddon(); term.loadAddon(fit); } catch (err) { console.debug('fit addon missing', err); }
  term.open(host);
  try { if (fit) fit.fit(); } catch (e) {}
  term.onData((data) => termQueueInput(data));
  term.attachCustomKeyEventHandler((e) => termKeyEvent(e, term));
  // Snapshot the selected TEXT the instant the selection geometry changes. In a
  // full-screen TUI (Claude's fullscreen / alternate-screen renderer) the live
  // stream repaints in place: the selection stays anchored to fixed row/col
  // coordinates while the characters under them change. Reading getSelection()
  // later (at mouseup / Cmd+C) then returns text that's offset by however many
  // rows the app repainted — the "copy is misaligned" bug. The change-time
  // snapshot matches what the user actually highlighted.
  let dragSel = '';
  host.addEventListener('mousedown', () => { dragSel = ''; });
  term.onSelectionChange(() => {
    const s = term.getSelection();
    if (s) { TERM.lastSelection = s; dragSel = s; }
  });
  // Copy-on-select (tmux / iTerm style): copy the moment a selection finishes,
  // using the drag-scoped snapshot (set only if THIS drag produced a selection,
  // so a plain click doesn't re-copy a stale one). dragSel survives a redraw
  // that clears xterm's live selection, so the copy still works.
  host.addEventListener('mouseup', () => {
    if (dragSel && dragSel.trim()) termClipboardWrite(dragSel);
  });
  // Native paste: clipboardData works even over plain http:// where the async
  // navigator.clipboard API is blocked. Capture phase so we beat xterm's own
  // paste handler and avoid a double paste.
  if (term.textarea) {
    term.textarea.addEventListener('paste', (e) => {
      const cd = e.clipboardData || window.clipboardData;
      if (!cd) return;
      const t = cd.getData('text');
      if (t) { e.preventDefault(); e.stopImmediatePropagation(); termPaste(t); }
    }, true);
  }
  // Mouse/touchpad wheel -> scroll tmux's own scrollback (copy-mode), instead of
  // letting xterm convert the wheel into arrow keys for the full-screen app.
  // Scrolling back down to the bottom auto-exits copy-mode (live output resumes).
  // Negative dy = up (older output), positive dy = down. Coalesced so a flick
  // doesn't fire dozens of requests.
  let scrollAccum = 0;
  let scrollTimer = null;
  function termScrollBy(dy) {
    if (!TERM.connected || !termTarget()) return;
    scrollAccum += dy;
    if (scrollTimer) return;
    scrollTimer = setTimeout(() => {
      const total = scrollAccum; scrollAccum = 0; scrollTimer = null;
      if (!total) return;
      const lines = Math.max(1, Math.min(80, Math.round(Math.abs(total) / 24)));
      api('/api/tmux/scroll', {
        method: 'POST',
        body: JSON.stringify({ target: termTarget(), dir: total < 0 ? 'up' : 'down', lines }),
      }).catch(() => {});
    }, 40);
  }
  host.addEventListener('wheel', (e) => {
    if (!TERM.connected || !termTarget()) return;
    e.preventDefault();
    e.stopPropagation();
    termScrollBy(e.deltaMode === 1 ? e.deltaY * 18 : e.deltaY);
  }, { passive: false, capture: true });
  // Touch (phones/tablets): a single-finger drag scrolls tmux scrollback the
  // same way the wheel does. A long-press (finger held still) instead arms text
  // selection, where the following drag is translated into xterm's own mouse
  // selection so copy-on-select still fires. Two-finger gestures are left to the
  // browser. Without this, touch drags did nothing (xterm only listens for
  // mouse/wheel), so the pane couldn't be scrolled or selected on mobile.
  const screenEl = () => (term.element && term.element.querySelector('.xterm-screen')) || term.element;
  function synthMouse(type, t) {
    try {
      const el = screenEl();
      if (!el) return;
      el.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window,
        clientX: t.clientX, clientY: t.clientY,
        button: 0, buttons: type === 'mouseup' ? 0 : 1,
      }));
    } catch (_) {}
  }
  const LONG_PRESS_MS = 380;
  const MOVE_SLOP = 10; // px before a press is treated as a drag (scroll)
  let touchMode = '';   // '' | 'scroll' | 'select'
  let touchStartX = 0, touchStartY = 0, touchLastY = 0;
  let pressTimer = null;
  function clearPressTimer() { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } }
  host.addEventListener('touchstart', (e) => {
    if (!TERM.connected || !termTarget()) return;
    if (e.touches.length !== 1) { clearPressTimer(); touchMode = ''; return; }
    const t = e.touches[0];
    touchStartX = t.clientX; touchStartY = t.clientY; touchLastY = t.clientY;
    touchMode = '';
    clearPressTimer();
    pressTimer = setTimeout(() => {
      pressTimer = null;
      touchMode = 'select';
      try { term.clearSelection(); } catch (_) {}
      synthMouse('mousedown', { clientX: touchStartX, clientY: touchStartY });
      if (navigator.vibrate) { try { navigator.vibrate(15); } catch (_) {} }
    }, LONG_PRESS_MS);
    // preventDefault here stops the browser from panning the page and from
    // synthesizing its own mouse events (which would double up with synthMouse).
    e.preventDefault();
  }, { passive: false });
  host.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    if (touchMode === '') {
      // Decide scroll vs (pending) select based on how far the finger moved.
      if (Math.abs(t.clientY - touchStartY) > MOVE_SLOP
          || Math.abs(t.clientX - touchStartX) > MOVE_SLOP) {
        clearPressTimer();
        touchMode = 'scroll';
      } else {
        return; // still might become a long-press select
      }
    }
    if (touchMode === 'scroll') {
      e.preventDefault();
      // Finger down (clientY grows) reveals older output above -> scroll up.
      termScrollBy(touchLastY - t.clientY);
      touchLastY = t.clientY;
    } else if (touchMode === 'select') {
      e.preventDefault();
      synthMouse('mousemove', t);
    }
  }, { passive: false });
  function endTouch(e) {
    clearPressTimer();
    if (touchMode === 'select') {
      const last = (e.changedTouches && e.changedTouches[0]) || { clientX: touchStartX, clientY: touchLastY };
      // Bubbles to the host's copy-on-select handler, which copies the
      // change-time snapshot (matching what was highlighted, not a shifted
      // fresh read). The earlier synthetic mousedown/mousemove fed that snapshot.
      synthMouse('mouseup', last);
    } else if (touchMode === '') {
      // A quick tap (no drag, no long-press). We swallowed the synthesized click
      // in touchstart, so focus the terminal here to raise the soft keyboard.
      try { term.focus(); } catch (_) {}
    }
    touchMode = '';
  }
  host.addEventListener('touchend', endTouch, { passive: true });
  host.addEventListener('touchcancel', () => { clearPressTimer(); touchMode = ''; }, { passive: true });
  TERM.term = term;
  TERM.fit = fit;
  const live = document.getElementById('interview-live');
  host.addEventListener('focusin', () => { if (live) live.hidden = false; });
  host.addEventListener('focusout', () => { if (live) live.hidden = true; });
  // Clicking the host padding (outside the xterm screen) should still focus the
  // terminal so keys go to Claude. Guarded to the host itself so click-drag text
  // selection on the screen is left to xterm.
  host.addEventListener('pointerdown', (e) => {
    if (e.target === host) { try { term.focus(); } catch (_) {} }
  });
  try {
    const ro = new ResizeObserver(() => termHandleResize());
    ro.observe(host);
  } catch (e) {}
  window.addEventListener('resize', termHandleResize);
  // Monospace metrics finalize after the web font loads; refit so column count
  // (and the tmux pane width) matches the real glyph width.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => termHandleResize()).catch(() => {});
  }
  return term;
}

function termHandleResize() {
  if (!TERM.fit || !TERM.term) return;
  if (TERM.resizeTimer) clearTimeout(TERM.resizeTimer);
  TERM.resizeTimer = setTimeout(() => {
    TERM.resizeTimer = null;
    const host = document.getElementById('interview-term');
    // Skip while the terminal isn't really laid out (a tab switch / mobile
    // URL-bar animation / scroll repaint can momentarily report a tiny width),
    // so we never fit to a sliver.
    if (!host || host.clientWidth < 120) return;
    const prevCols = TERM.term.cols;
    const prevRows = TERM.term.rows;
    try { TERM.fit.fit(); } catch (e) {}
    // Reconnect only when the size actually changed, so tmux resizes the pane to
    // match xterm (Claude re-renders to fill the width). Kept deliberately simple
    // — extra "smart" refit passes caused reconnect churn that dropped the client
    // and left the pane stuck at a stale width.
    if (TERM.connected && TERM.target
        && (TERM.term.cols !== prevCols || TERM.term.rows !== prevRows)) {
      connectTerminal(TERM.target, true);
    }
  }, 300);
}

// Forward input to the pane. Keystrokes queued while a request is in flight
// are merged into one send for efficiency — EXCEPT a bare Esc keypress
// ("\x1b" on its own). Glued to neighbouring bytes in a single write, the
// TUI's input parser (Claude/Ink) reads ESC+<bytes> as an Alt-combo or a
// half-finished escape sequence and the Esc keypress is silently swallowed
// ("Esc sometimes doesn't work"). So a bare Esc is always sent as its own
// request, followed by a short pause so the pane receives it as an isolated
// read (and parses it as a real Escape).
async function termQueueInput(data) {
  TERM.inputQueue.push(String(data));
  if (TERM.inputSending) return;
  TERM.inputSending = true;
  try {
    while (TERM.inputQueue.length) {
      const target = termTarget();
      if (!target) { TERM.inputQueue.length = 0; break; }
      let chunk = '';
      let bareEsc = false;
      while (TERM.inputQueue.length) {
        const next = TERM.inputQueue[0];
        if (next === '\x1b') {
          if (chunk) break;          // flush what we already merged first
          TERM.inputQueue.shift();
          chunk = next;
          bareEsc = true;
          break;                     // send the Esc alone
        }
        if (chunk && chunk.length + next.length > 4000) break;
        TERM.inputQueue.shift();
        chunk += next;
      }
      if (!chunk) continue;
      try {
        if (TERM.streamId) {
          await api('/api/tmux/stream-input', {
            method: 'POST',
            body: JSON.stringify({ stream_id: TERM.streamId, text: chunk }),
          });
        } else if (!TERM.connected) {
          // Legacy/fallback path when there is no live attach stream.
          await api('/api/tmux/send-literal', {
            method: 'POST',
            body: JSON.stringify({ target, text: chunk }),
          });
        }
      } catch (err) { console.debug('input send failed', err); }
      // Give the TUI a beat to see the lone ESC before any following bytes,
      // so it can't be re-glued into a sequence at the pty read level.
      if (bareEsc) await new Promise((resolve) => setTimeout(resolve, 40));
    }
  } finally {
    TERM.inputSending = false;
  }
}

// Send raw bytes to the pane (used by paste + the mobile key bar). Reuses the
// same batched queue as keystrokes so ordering with live typing is preserved.
function termSendLiteral(text) {
  if (text == null || text === '') return;
  termQueueInput(String(text));
}

// Paste into the pane. A short single line goes through the fast literal path
// (keeps ordering with live typing); anything multi-line or large goes through
// tmux bracketed paste so the TUI (Claude) treats it as ONE paste and doesn't
// submit on every embedded newline (and we avoid the send-literal size cap).
function termPaste(text) {
  if (!text) return;
  const target = termTarget();
  if (!target) return;
  if (!/[\r\n]/.test(text) && text.length <= 2000) {
    termSendLiteral(text);
    return;
  }
  api('/api/tmux/send-text', {
    method: 'POST',
    body: JSON.stringify({ target, text, submit: false }),
  }).catch((err) => console.debug('paste failed', err));
}

// The live PTY stream repaints on its own, so there's nothing to poll here; this
// just nudges the read-only markdown preview after a compose-box message in case
// it changed PLAN.md (or another scanned file).
function termScheduleRefresh() {
  if (termScheduleRefresh._t) clearTimeout(termScheduleRefresh._t);
  termScheduleRefresh._t = setTimeout(() => {
    termScheduleRefresh._t = null;
    try { refreshInterviewPreview(true); } catch (e) {}
  }, 800);
}

// Small transient "Copied" toast shown after a successful clipboard copy.
function termFlashCopied() {
  const card = document.querySelector('.terminal-card--interview');
  if (!card) return;
  let el = card.querySelector('.term-copied-toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'term-copied-toast';
    el.textContent = 'Copied';
    card.appendChild(el);
  }
  el.classList.add('is-show');
  if (termFlashCopied._t) clearTimeout(termFlashCopied._t);
  termFlashCopied._t = setTimeout(() => el.classList.remove('is-show'), 900);
}

const TERMINAL_HEARTBEAT_MS = 20000;

function terminalStreamPost(path, streamId, keepalive = false) {
  if (!streamId) return Promise.resolve();
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stream_id: streamId }),
    cache: 'no-store',
    keepalive,
  });
}

function stopTerminalHeartbeat() {
  if (TERM.heartbeatTimer) {
    clearTimeout(TERM.heartbeatTimer);
    TERM.heartbeatTimer = null;
  }
}

function startTerminalHeartbeat(streamId, ctrl) {
  stopTerminalHeartbeat();
  const beat = () => {
    TERM.heartbeatTimer = null;
    if (TERM.abort !== ctrl || TERM.streamId !== streamId) return;
    terminalStreamPost('/api/tmux/stream-heartbeat', streamId)
      .catch(() => {})
      .finally(() => {
        if (TERM.abort === ctrl && TERM.streamId === streamId) {
          TERM.heartbeatTimer = setTimeout(beat, TERMINAL_HEARTBEAT_MS);
        }
      });
  };
  TERM.heartbeatTimer = setTimeout(beat, TERMINAL_HEARTBEAT_MS);
}

function closeTerminalStream(streamId) {
  // keepalive lets this tiny request survive pagehide/iframe teardown. It is
  // deliberately independent of the AbortController for the output stream.
  terminalStreamPost('/api/tmux/stream-close', streamId, true).catch(() => {});
}

function disconnectTerminal() {
  const streamId = TERM.streamId;
  TERM.streamId = '';
  stopTerminalHeartbeat();
  if (streamId) closeTerminalStream(streamId);
  if (TERM.abort) { try { TERM.abort.abort(); } catch (e) {} TERM.abort = null; }
  TERM.connected = false;
  TERM.target = '';
}

// Put the keyboard back in the pane. Skipped on touch devices, where focusing
// xterm raises the soft keyboard over the screen the user wants to read.
function focusTerminalSoon() {
  if (!TERM.term) return;
  if (window.matchMedia && window.matchMedia('(hover: none), (pointer: coarse)').matches) return;
  setTimeout(() => { try { TERM.term.focus(); } catch (e) {} }, 0);
}

// Escape is how you back out of an agent TUI menu, but it only reaches the pane
// when xterm holds focus — and after clicking a button such as Run /goal, focus
// sits on that button, so the keypress died in the page. With the pane visible,
// no modal open and no text field in use, nothing else wants Escape: send it on
// and hand focus back so the keys that follow land there too.
function forwardEscapeToPane(target) {
  if (!TERM.connected || !termTarget()) return false;
  const panel = document.querySelector('.tab-panel.active');
  if (!panel || panel.dataset.panel !== 'claude') return false;
  if (target && target.closest
      && target.closest('input, textarea, select, [contenteditable="true"]')) return false;
  termQueueInput('\x1b');
  focusTerminalSoon();
  return true;
}

// Strip the app's MOUSE-mode enable/disable sequences from the PTY byte stream so
// xterm never enters application-mouse mode — then a plain drag does native text
// selection (-> copy-on-select -> browser clipboard) instead of being forwarded
// to the app (which copies into the server-side tmux buffer). Deliberately
// byte-level and surgical: it only drops a COMPLETE `ESC [ ? <params> (h|l)`
// whose params are ALL mouse modes, never decodes, never reconstructs, never
// buffers across chunks — so it cannot corrupt other sequences / full-screen
// rendering (which an earlier decode+carry approach did). A mouse sequence split
// across two read chunks is simply left in; apps re-emit modes on redraw so it
// self-heals. Scrolling is unaffected (server-side, and tmux still sees the
// app's mouse mode). Clicks no longer reach the app — an accepted trade-off.
const _MOUSE_MODE_SET = new Set([1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016]);
function stripMouseModeBytes(u8) {
  if (!u8 || u8.length < 4) return u8;
  let hit = false;
  for (let i = 0; i + 3 < u8.length; i++) {
    if (u8[i] === 0x1b && u8[i + 1] === 0x5b && u8[i + 2] === 0x3f) { hit = true; break; }
  }
  if (!hit) return u8;
  const n = u8.length;
  const out = new Uint8Array(n);
  let w = 0;
  let i = 0;
  while (i < n) {
    if (i + 3 < n && u8[i] === 0x1b && u8[i + 1] === 0x5b && u8[i + 2] === 0x3f) {
      let j = i + 3;
      let params = '';
      while (j < n && ((u8[j] >= 0x30 && u8[j] <= 0x39) || u8[j] === 0x3b)) {
        params += String.fromCharCode(u8[j]); j++;
      }
      if (j < n && (u8[j] === 0x68 || u8[j] === 0x6c)) {  // 'h' set / 'l' reset
        const nums = params.split(';').filter(Boolean).map(Number);
        if (nums.length && nums.every((x) => _MOUSE_MODE_SET.has(x))) {
          i = j + 1;  // drop the whole mouse-mode sequence
          continue;
        }
      }
    }
    out[w++] = u8[i++];
  }
  return out.subarray(0, w);
}

async function connectTerminal(target, force = false) {
  const term = ensureTerminal();
  if (!term || !target) return;
  if (!force && TERM.connected && TERM.target === target) return;
  // If the host isn't laid out yet (tab just shown / mid-layout), don't fit to a
  // tiny width and pin the pane to ~20 cols. Wait until it has a real width,
  // keeping any existing stream alive in the meantime.
  const hostEl = document.getElementById('interview-term');
  if (hostEl && hostEl.clientWidth < 120) {
    TERM._sizeWaits = (TERM._sizeWaits || 0) + 1;
    if (TERM._sizeWaits <= 50) {
      clearTimeout(TERM._sizeWaitTimer);
      TERM._sizeWaitTimer = setTimeout(() => connectTerminal(target, force), 100);
      return;
    }
  }
  TERM._sizeWaits = 0;
  const preserveScreen = TERM.target === target;
  disconnectTerminal();
  TERM.target = target;
  TERM.connected = true;
  const ctrl = new AbortController();
  TERM.abort = ctrl;
  try { if (TERM.fit) TERM.fit.fit(); } catch (e) {}
  const cols = term.cols || 80;
  const rows = term.rows || 24;
  // A same-pane resize needs a new PTY attachment, but clearing xterm first
  // makes Cursor's full-screen UI visibly disappear until tmux redraws.
  if (!preserveScreen) {
    try { term.reset(); } catch (e) {}
  }
  try {
    const resp = await fetch(
      '/api/tmux/stream?target=' + encodeURIComponent(target) + '&cols=' + cols + '&rows=' + rows,
      { signal: ctrl.signal, cache: 'no-store' },
    );
    if (!resp.ok || !resp.body) {
      if (TERM.abort === ctrl) {
        TERM.connected = false; TERM.abort = null;
        // Session not running (task not started, or the pane was stopped/died).
        // Show a friendly note instead of tmux's raw "can't find session: …".
        setTmuxOutputText(`tmux is not alive — click Start ${agentLabel(STATE.currentMeta?.agent)} to launch the pane.`);
      }
      return;
    }
    const streamId = resp.headers.get('X-Loom-Terminal-Stream') || '';
    if (TERM.abort !== ctrl) {
      closeTerminalStream(streamId);
      try { await resp.body.cancel(); } catch (e) {}
      return;
    }
    TERM.streamId = streamId;
    startTerminalHeartbeat(streamId, ctrl);
    const reader = resp.body.getReader();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value && TERM.abort === ctrl && TERM.term && TERM.target === target) {
        TERM.term.write(stripMouseModeBytes(value));
      }
    }
  } catch (err) {
    if (err && err.name !== 'AbortError') console.debug('terminal stream error', err);
  } finally {
    if (TERM.abort === ctrl) {
      const streamId = TERM.streamId;
      TERM.streamId = '';
      stopTerminalHeartbeat();
      TERM.connected = false;
      TERM.abort = null;
      closeTerminalStream(streamId);
    }
  }
}

function startPanePolling() {
  if (STATE.paneTimer) clearInterval(STATE.paneTimer);
  STATE.paneTick = 0;
  STATE.paneTimer = setInterval(() => {
    // Don't poll while the browser tab is in the background — saves a steady
    // stream of requests when nobody's looking. visibilitychange re-syncs on return.
    if (document.hidden) return;
    if (!STATE.slug) return;
    const claudeTab = document.querySelector('.tab-panel[data-panel="claude"]');
    if (!claudeTab || claudeTab.hidden) return;
    // The markdown preview tracks PLAN.md edits, so refresh it every cycle (4s).
    refreshInterviewPreview(true);
    // Sessions list + scanned md files change rarely — poll them ~every 12s
    // instead of every cycle to cut redundant requests by ~2/3.
    STATE.paneTick = (STATE.paneTick || 0) + 1;
    if (STATE.paneTick % 3 === 0) {
      refreshTaskTemplates();
      refreshClaudeSessions();
    }
  }, 4000);
}

// When the tab becomes visible again, refresh once immediately instead of
// waiting up to a full poll interval (and the paused pollers resume on their own).
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !STATE.slug) return;
  const claudeTab = document.querySelector('.tab-panel[data-panel="claude"]');
  if (claudeTab && !claudeTab.hidden) {
    refreshInterviewPreview(true);
    refreshTaskTemplates();
    refreshClaudeSessions();
  }
});

window.addEventListener('pagehide', () => {
  disconnectTerminal();
});

window.addEventListener('pageshow', (event) => {
  if (event.persisted && STATE.slug) refreshInterviewPreview(true);
});

async function startInterviewPane() {
  if (!STATE.slug) return;
  showPanel('claude');
  const label = agentLabel(STATE.currentMeta?.agent);
  setTmuxOutputText(`Starting ${label} pane…\nWhen it is ready, click Start Deep Interview to paste the prompt.`);
  revealInterviewTerminal();
  const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/interview/start', {
    method: 'POST',
    body: '{}',
  });
  $('#inp-interview-target').value = r.target || '';
  await refreshInterviewPreview(true, 'top');
  revealInterviewTerminal();
  setTimeout(() => {
    refreshInterviewPreview(true, 'top');
    revealInterviewTerminal('nearest');
  }, 6500);
  setTimeout(() => {
    refreshInterviewPreview(true, 'top');
    revealInterviewTerminal('nearest');
  }, 10000);
}

async function pasteInterviewPrompt() {
  if (!STATE.slug) return;
  const target = $('#inp-interview-target').value.trim();
  if (!target) {
    toast('Start the agent pane first.', { type: 'error' });
    return;
  }
  const btn = document.getElementById('btn-interview-paste');
  if (btn) btn.disabled = true;
  try {
    setTmuxOutputText('Pasting deep-interview prompt with general goal + skills…');
    const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/paste-prompt', {
      method: 'POST',
      body: '{}',
    });
    $('#inp-interview-target').value = r.target || target;
    setTmuxOutputText(
      `Pasted deep-interview prompt (${r.prompt_chars || 0} chars, skills: ${r.has_skills ? 'yes' : 'no'}).\n` +
      'Refreshing terminal capture…'
    );
    setTimeout(refreshInterviewPreview, 700);
    setTimeout(refreshInterviewPreview, 2000);
  } catch (err) {
    setTmuxOutputText(`Failed to paste prompt: ${err.message || err}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function currentPlanPathForPrompt() {
  return STATE.planPath || `.RUD/${STATE.slug || '<task>'}/PLAN.md`;
}

async function sendWorkflowPrompt(kind, text) {
  const target = $('#inp-interview-target').value.trim();
  if (!STATE.slug || !target) {
    toast('Start the agent pane first.', { type: 'error' });
    return;
  }
  try {
    setTmuxOutputText(`Sending workflow prompt: ${kind}…`);
    await sendTaskMessage(text);
    setTimeout(() => refreshInterviewPreview(true), 500);
    setTimeout(() => refreshTaskTemplates(), 1800);
  } catch (err) {
    setTmuxOutputText(`Failed to send ${kind}: ${err.message || err}`);
  }
}

async function writeInterviewToPlan() {
  const planPath = currentPlanPathForPrompt();
  await sendWorkflowPrompt(
    'write PLAN.md',
    `Please finish the interview phase now and write the result directly into ${planPath}.

Use this structure:
- Goal
- Context / Decisions from the interview
- Constraints / non-goals
- Acceptance criteria
- Next steps as checkbox items
- Progress Log / Result

Keep it concise and executable. Do not create INTERVIEW.md, TODO.md, PROGRESS.md, or any other task-state file.`
  );
}

async function runGoalFromPlan() {
  const planPath = currentPlanPathForPrompt();
  await sendWorkflowPrompt(
    'run /goal',
    `/goal Execute the task plan in ${planPath}. Keep ${planPath} updated with useful progress, blockers, decisions, and final results. Do not create separate status files.`
  );
}

async function writeResultToPlan() {
  const planPath = currentPlanPathForPrompt();
  await sendWorkflowPrompt(
    'write result',
    `Please summarize the current execution result back into ${planPath}.

Update only useful information:
- what was done
- important decisions
- test/eval results
- blockers or follow-up work
- final status

Remove obsolete noisy details, but preserve unrelated prior sections. Do not create separate status files.`
  );
}

// ===== Modals & sidebar =====

function openCreateModal() {
  if (!STATE.projectId) {
    toast('Select or add a project first.', { type: 'error' });
    return;
  }
  const modal = $('#create-modal');
  modal.hidden = false;
  $('#new-task-status').textContent = '';
  updateCreateAgentHint();
  requestAnimationFrame(() => $('#new-title').focus());
}

function closeCreateModal() {
  $('#create-modal').hidden = true;
}

const AGENT_HINTS = {
  cursor: 'Cursor Agent pane (agent CLI). Resume a past chat by chat ID.',
  claude: 'Claude Code pane. Resume a past session by UUID.',
  codex: 'Codex CLI pane. Resume with codex resume <id>.',
  author: 'Existing-paper Author: one Cursor Agent with isolated manuscript and experiment worktrees plus paper-writing skills.',
  ar: 'Automated research: mine a direction for ideas, then each idea you pick becomes a task that drafts a paper, runs its experiments, and iterates against a reviewer agent.',
};

function effectiveCreateAgent() {
  const type = document.getElementById('new-agent-select')?.value || 'cursor';
  if (type === 'codex') return 'codex';
  if (type === 'claude') return 'claude';
  return 'cursor';
}

// Cursor advertises ~200 model ids, so group them by family; a flat list is
// unreadable and a datalist hides it behind typing.
const MODEL_FAMILIES = [
  [/^gpt-[\d.]+-codex/, 'Codex'],
  [/^(gpt|o\d)/, 'GPT'],
  [/^claude|^opus|^sonnet|^haiku|^fable/, 'Claude'],
  [/grok/, 'Grok'],
  [/^gemini/, 'Gemini'],
  [/^composer/, 'Composer'],
  [/^kimi/, 'Kimi'],
  [/^glm/, 'GLM'],
  [/^deepseek/, 'DeepSeek'],
];
const MODEL_CUSTOM = '\u0000custom';

function modelFamily(id) {
  if (id === 'auto') return 'Auto';
  for (const [pattern, name] of MODEL_FAMILIES) if (pattern.test(id)) return name;
  return 'Other';
}

// The <select> is the visible control; `input` stays as the value holder that
// every existing read/save path already uses, revealed only to type an id the
// CLI doesn't advertise.
function populateModelPicker(input, select, agent, value) {
  if (!input || !select) return;
  agent = normalizeAgent(agent);
  const options = ((STATE.modelOptions && STATE.modelOptions[agent]) || [])
    .map((m) => (typeof m === 'string' ? { id: m, label: '' } : m))
    .filter((m) => m && m.id);
  const current = String(
    value != null ? value : ((STATE.modelDefaults && STATE.modelDefaults[agent]) || ''),
  ).trim();
  input.value = current;
  input.placeholder = agent === 'codex'
    ? 'e.g. gpt-5.5 (or blank for Codex config)'
    : 'Model id';

  const families = new Map();
  for (const m of options) {
    const family = modelFamily(m.id);
    if (!families.has(family)) families.set(family, []);
    families.get(family).push(m);
  }
  const option = (id, label) => {
    const el = document.createElement('option');
    el.value = id;
    el.textContent = label ? `${id} — ${label}` : id;
    el.title = label || id;
    return el;
  };
  select.innerHTML = '';
  // A task can hold a model the CLI no longer lists; keep it selectable so
  // opening the picker never silently rewrites it.
  if (current && !options.some((m) => m.id === current)) {
    const group = document.createElement('optgroup');
    group.label = 'Current';
    group.appendChild(option(current, ''));
    select.appendChild(group);
  }
  for (const [name, list] of families) {
    const group = document.createElement('optgroup');
    group.label = name;
    for (const m of list) group.appendChild(option(m.id, m.label));
    select.appendChild(group);
  }
  const custom = document.createElement('option');
  custom.value = MODEL_CUSTOM;
  custom.textContent = 'Custom id…';
  select.appendChild(custom);
  select.value = current;
  input.hidden = true;

  if (!select.dataset.bound) {
    select.dataset.bound = '1';
    select.addEventListener('change', () => {
      if (select.value === MODEL_CUSTOM) {
        input.hidden = false;
        input.focus();
        input.select();
        return;
      }
      input.hidden = true;
      input.value = select.value;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    input.addEventListener('input', () => {
      // Keep the select in step while a custom id is typed.
      select.value = [...select.options].some((o) => o.value === input.value)
        ? input.value
        : MODEL_CUSTOM;
    });
  }
}

function updateCreateAgentHint(resetModel = false) {
  const sel = document.getElementById('new-agent-select');
  const hint = document.getElementById('new-agent-hint');
  if (!sel || !hint) return;
  hint.textContent = AGENT_HINTS[sel.value] || '';
  const input = document.getElementById('new-model');
  const select = document.getElementById('new-model-select');
  populateModelPicker(
    input,
    select,
    effectiveCreateAgent(),
    resetModel ? null : (input?.value || null),
  );
  updateAuthorCreateFields();
  updateArCreateFields();
}

function updateAuthorCreateFields() {
  const wrap = document.getElementById('author-create-fields');
  if (!wrap) return;
  wrap.hidden = document.getElementById('new-agent-select')?.value !== 'author';
}

// The AR-only half of the create form: direction, venue, mode and rounds.
async function updateArCreateFields() {
  const wrap = document.getElementById('ar-create-fields');
  if (!wrap) return;
  const isAr = document.getElementById('new-agent-select')?.value === 'ar';
  wrap.hidden = !isAr;
  // An AR task has no "general goal" to interview about: what it needs is the
  // paper's actual content, which the AR block asks for directly.
  const goalWrap = document.getElementById('new-goal-wrap');
  if (goalWrap) goalWrap.hidden = isAr;
  if (!isAr) return;
  const cat = await loadArCatalog();
  const dir = document.getElementById('ar-direction');
  if (dir && !dir.options.length) {
    dir.innerHTML = (cat.directions || [])
      .map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.label)}</option>`)
      .join('');
    dir.addEventListener('change', syncArCustomDirection);
  }
  const venue = document.getElementById('ar-venue');
  if (venue && !venue.options.length) {
    venue.innerHTML = (cat.venues || [])
      .map((v) => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.label)}</option>`)
      .join('');
    if (cat.default_venue) venue.value = cat.default_venue;
  }
  const rounds = document.getElementById('ar-max-rounds');
  if (rounds && cat.default_max_rounds && !rounds.dataset.init) {
    rounds.value = cat.default_max_rounds;
    rounds.max = cat.max_rounds_limit || 50;
    rounds.dataset.init = '1';
  }
  syncArCustomDirection();
  syncArMode();
}

function syncArCustomDirection() {
  const dir = document.getElementById('ar-direction');
  const custom = document.getElementById('ar-custom-direction');
  if (!dir || !custom) return;
  custom.hidden = dir.value !== 'custom';
}

// The paper-content box is shown in both modes; it is required when the user
// is starting from their own idea and extra context when the studio is mining
// a direction on its own.
function syncArMode() {
  const mode = document.querySelector('input[name="ar-mode"]:checked')?.value || 'auto';
  const seeded = mode === 'seed';
  const label = document.getElementById('ar-seed-label');
  const note = document.getElementById('ar-seed-note');
  const box = document.getElementById('ar-seed-idea');
  if (label) label.textContent = seeded
    ? 'What the paper should be about'
    : 'What the paper should be about (optional)';
  if (note) note.textContent = seeded
    ? 'The studio sharpens this into concrete, testable variants — one paper task per variant you pick.'
    : 'Leave this empty to let the studio propose ideas purely from recent work in the direction above.';
  if (box) box.placeholder = seeded
    ? 'The concrete content you want the paper to establish.'
    : 'Optional: constraints, a dataset you must use, an angle you want covered.';
}

document.querySelectorAll('input[name="ar-mode"]').forEach((el) => {
  el.addEventListener('change', syncArMode);
});

function resetCreateForm() {
  $('#new-title').value = '';
  $('#new-goal').value = '';
  $('#new-task-status').textContent = '';
  renderSkillsPicker();
  const sel = document.getElementById('new-agent-select');
  if (sel) sel.value = 'cursor';
  const seed = document.getElementById('ar-seed-idea');
  if (seed) seed.value = '';
  const custom = document.getElementById('ar-custom-direction');
  if (custom) custom.value = '';
  const authorVenue = document.getElementById('author-venue');
  if (authorVenue) authorVenue.value = 'iclr';
  const authorTex = document.getElementById('author-main-tex');
  if (authorTex) authorTex.value = 'main.tex';
  const authorManuscript = document.getElementById('author-manuscript-repo');
  if (authorManuscript) authorManuscript.value = '';
  const authorExperiments = document.getElementById('author-experiment-repo');
  if (authorExperiments) authorExperiments.value = '';
  updateCreateAgentHint(true);
}

function isMobileViewport() {
  return window.matchMedia('(max-width: 820px)').matches;
}

function setSidebarOpen(open) {
  STATE.sidebarOpen = !!open;
  document.body.classList.toggle('sidebar-open', STATE.sidebarOpen);
  const toggle = document.getElementById('btn-sidebar-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', STATE.sidebarOpen ? 'true' : 'false');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.hidden = !STATE.sidebarOpen;
}

function toggleSidebar() {
  setSidebarOpen(!STATE.sidebarOpen);
}

// ===== Wire-up =====

(function initSidebarToggle() {
  const toggle = document.getElementById('btn-sidebar-toggle');
  if (toggle) toggle.addEventListener('click', toggleSidebar);
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.addEventListener('click', () => setSidebarOpen(false));
  window.addEventListener('resize', () => {
    if (!isMobileViewport() && STATE.sidebarOpen) setSidebarOpen(false);
  });
})();

document.getElementById('btn-add-project').addEventListener('click', openAddProjectModal);
document.getElementById('btn-add-project-close').addEventListener('click', closeAddProjectModal);
document.getElementById('btn-add-project-cancel').addEventListener('click', closeAddProjectModal);
document.getElementById('btn-add-project-save').addEventListener('click', submitAddProject);
document.getElementById('btn-code-root-open').addEventListener('click', openCodeRootModal);
document.getElementById('btn-code-root-close').addEventListener('click', closeCodeRootModal);
document.getElementById('btn-code-root-cancel').addEventListener('click', closeCodeRootModal);
document.getElementById('btn-code-root-save').addEventListener('click', saveCodeRootPattern);
document.getElementById('project-code-root-pattern').addEventListener('input', updateCodeRootPreview);
(() => {
  const modesEl = document.getElementById('add-project-modes');
  if (!modesEl) return;
  modesEl.addEventListener('click', (e) => {
    const b = e.target.closest('.add-project-mode');
    if (!b || !b.dataset.mode) return;
    setAddProjectMode(b.dataset.mode);
    const focusEl = b.dataset.mode === 'clone'
      ? document.getElementById('new-project-repo')
      : document.getElementById('new-project-path');
    if (focusEl) focusEl.focus();
  });
})();
$('#add-project-modal').addEventListener('click', (event) => {
  if (event.target.id === 'add-project-modal') closeAddProjectModal();
});
$('#code-root-modal').addEventListener('click', (event) => {
  if (event.target.id === 'code-root-modal') closeCodeRootModal();
});

document.getElementById('btn-tasks-refresh').addEventListener('click', loadTasks);

(function initTaskFilter() {
  const inp = document.getElementById('task-filter');
  if (!inp) return;
  let timer = 0;
  inp.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      STATE.taskFilter = inp.value;
      renderTasksFromState();
    }, 80);
  });
})();

// ===== Inline edit: task title + goal =====

function makeEditable(el, { multiline = false, placeholder = '', onSave }) {
  if (!el) return;
  el.classList.add('editable');
  el.title = 'Click to edit';
  el.addEventListener('click', (ev) => {
    if (el.dataset.editing === '1') return;
    ev.stopPropagation();
    el.dataset.editing = '1';
    const current = el.textContent || '';
    const input = document.createElement(multiline ? 'textarea' : 'input');
    if (!multiline) input.type = 'text';
    input.value = current;
    input.placeholder = placeholder;
    input.className = 'editable__input';
    if (multiline) input.rows = 3;
    el.innerHTML = '';
    el.appendChild(input);
    input.focus();
    if (!multiline) input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      el.dataset.editing = '';
      const next = input.value.trim();
      if (!commit || next === current.trim()) {
        el.textContent = current;
        return;
      }
      el.textContent = next;
      try { await onSave(next); } catch (err) {
        el.textContent = current;
        toast(err.message || 'save failed', { type: 'error' });
      }
    };
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('keydown', (kev) => {
      if (kev.key === 'Escape') { kev.preventDefault(); finish(false); }
      if (kev.key === 'Enter' && !multiline) { kev.preventDefault(); finish(true); }
      if (kev.key === 'Enter' && (kev.ctrlKey || kev.metaKey) && multiline) {
        kev.preventDefault(); finish(true);
      }
    });
  });
}

async function saveTaskMeta(patch) {
  if (!STATE.slug) return;
  const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/meta', {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
  if (r.meta) {
    STATE.currentMeta = r.meta;
    // Update the local task list cache so the sidebar reflects the change.
    STATE.tasks = (STATE.tasks || []).map((t) => (t.slug === r.meta.slug ? r.meta : t));
    renderTasksFromState();
  }
  return r;
}

(function initTaskHeaderEditing() {
  makeEditable($('#task-title'), {
    placeholder: 'Task title',
    onSave: (title) => saveTaskMeta({ title }),
  });
  makeEditable($('#task-goal'), {
    multiline: true,
    placeholder: 'General goal',
    onSave: (general_goal) => saveTaskMeta({ general_goal }),
  });
})();
document.getElementById('btn-create-open').addEventListener('click', openCreateModal);
document.getElementById('btn-empty-create').addEventListener('click', openCreateModal);
document.getElementById('new-agent-select').addEventListener(
  'change', () => updateCreateAgentHint(true)
);
document.getElementById('btn-create-close').addEventListener('click', closeCreateModal);
document.getElementById('btn-create-cancel').addEventListener('click', closeCreateModal);
document.getElementById('create-modal').addEventListener('click', (event) => {
  if (event.target.id === 'create-modal') closeCreateModal();
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  // The terminal owns Escape; it is a core navigation key in Cursor/Claude
  // TUIs, not a request to close a Loom modal.
  if (event.target && event.target.closest && event.target.closest('.xterm')) return;
  if (!$('#ar-review-modal').hidden) closeArReview();
  else if (!$('#preview-modal').hidden) closeFullscreenPreview();
  else if (!$('#notes-modal').hidden) closeNotesModal();
  else if (!$('#worktree-modal').hidden) closeWorktreeModal();
  else if (!$('#create-modal').hidden) closeCreateModal();
  else if (!$('#code-root-modal').hidden) closeCodeRootModal();
  else if (!$('#add-project-modal').hidden) closeAddProjectModal();
  else if (forwardEscapeToPane(event.target)) event.preventDefault();
});

document.getElementById('btn-preview-close').addEventListener('click', closeFullscreenPreview);
document.getElementById('btn-preview-exit-fullscreen').addEventListener('click', closeFullscreenPreview);
document.getElementById('btn-preview-print').addEventListener('click', printFullscreenPreview);
document.getElementById('preview-modal').addEventListener('click', (event) => {
  if (event.target.id === 'preview-modal') closeFullscreenPreview();
});

document.getElementById('btn-worktree-push-all').addEventListener('click', pushAllWorktrees);

// ===== Project NOTES.md modal =====

// Tab / Shift+Tab indent for a <textarea> markdown editor. Tab inserts 2 spaces
// (or indents all selected lines); Shift+Tab removes up to 2 leading spaces from
// each selected line. Returns true if it handled the key. Preserves undo where
// the browser supports execCommand('insertText').
const _INDENT = '  ';
function handleEditorTab(ev, editor) {
  if (ev.key !== 'Tab' || ev.ctrlKey || ev.metaKey || ev.altKey) return false;
  ev.preventDefault();
  const start = editor.selectionStart;
  const end = editor.selectionEnd;
  const val = editor.value;
  const lineStart = val.lastIndexOf('\n', start - 1) + 1;
  const multiLine = val.slice(start, end).includes('\n');
  if (!ev.shiftKey && !multiLine) {
    // Simple insert at caret (keeps native undo via execCommand when available).
    if (document.execCommand) { document.execCommand('insertText', false, _INDENT); }
    else {
      editor.value = val.slice(0, start) + _INDENT + val.slice(end);
      editor.selectionStart = editor.selectionEnd = start + _INDENT.length;
    }
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }
  // Block (un)indent across the selected lines.
  const block = val.slice(lineStart, end);
  const lines = block.split('\n');
  let delta = 0; let firstDelta = 0;
  const newLines = lines.map((ln, idx) => {
    if (ev.shiftKey) {
      const m = ln.match(/^( {1,2}|\t)/);
      const removed = m ? m[0].length : 0;
      if (idx === 0) firstDelta = -removed;
      delta -= removed;
      return removed ? ln.slice(removed) : ln;
    }
    if (idx === 0) firstDelta = _INDENT.length;
    delta += _INDENT.length;
    return _INDENT + ln;
  });
  const replacement = newLines.join('\n');
  editor.value = val.slice(0, lineStart) + replacement + val.slice(end);
  editor.selectionStart = Math.max(lineStart, start + firstDelta);
  editor.selectionEnd = end + delta;
  editor.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
}

async function openNotesModal() {
  if (!STATE.projectId) {
    toast('Select or add a project first.', { type: 'error' });
    return;
  }
  const modal = $('#notes-modal');
  if (!modal) return;
  modal.hidden = false;
  document.body.classList.add('preview-open');
  const editor = $('#editor-notes');
  const preview = $('#preview-notes');
  const status = $('#notes-modal-status');
  const pathEl = $('#notes-modal-path');
  status.textContent = 'Loading…';
  editor.disabled = true;
  try {
    const project = await api('/api/project');
    const projectRoot = project.projectRoot || '';
    if (pathEl) pathEl.textContent = projectRoot ? `${projectRoot}/.RUD/NOTES.md` : '.RUD/NOTES.md';
    const d = await api('/api/notes');
    editor.value = d.content || '';
    preview.innerHTML = renderMarkdownWithAssets(editor.value, markdownAssetResolver('notes'));
    typesetMath(preview);
    STATE.notesDirty = false;
    status.textContent = '';
  } catch (err) {
    status.textContent = err.message || 'Failed to load notes';
  } finally {
    editor.disabled = false;
    requestAnimationFrame(() => editor.focus());
  }
}

function closeNotesModal() {
  if (STATE.notesDirty && !confirm('Discard unsaved Notes changes?')) return;
  const modal = $('#notes-modal');
  if (modal) modal.hidden = true;
  document.body.classList.remove('preview-open');
}

async function saveNotes() {
  const editor = $('#editor-notes');
  const status = $('#notes-modal-status');
  if (!editor) return;
  STATE.notesSaving = true;
  status.textContent = 'Saving…';
  try {
    await api('/api/notes', {
      method: 'PUT',
      body: JSON.stringify({ content: editor.value }),
    });
    STATE.notesDirty = false;
    status.textContent = 'Saved';
    setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 1800);
  } catch (err) {
    status.textContent = err.message || 'Save failed';
  } finally {
    STATE.notesSaving = false;
  }
}

(function initNotesModalEditor() {
  const editor = $('#editor-notes');
  const preview = $('#preview-notes');
  if (!editor || !preview) return;
  editor.addEventListener('input', () => {
    STATE.notesDirty = true;
    requestAnimationFrame(() => {
      preview.innerHTML = renderMarkdownWithAssets(editor.value, markdownAssetResolver('notes'));
      typesetMath(preview);
    });
  });
  editor.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 's' || ev.key === 'S')) {
      ev.preventDefault();
      saveNotes();
    } else {
      handleEditorTab(ev, editor);
    }
  });
})();

// Inline PLAN.md editing on the Claude tab: Edit toggles the read-only preview
// textarea into an editor, Save PUTs it, Ctrl/Cmd+S saves, live-preview updates.
(function initPlanEditor() {
  const editor = document.getElementById('editor-interview');
  const editBtn = document.getElementById('btn-plan-edit');
  const saveBtn = document.getElementById('btn-plan-save');
  const cancelBtn = document.getElementById('btn-plan-cancel');
  if (editBtn) editBtn.addEventListener('click', startPlanEdit);
  if (saveBtn) saveBtn.addEventListener('click', savePlanEdit);
  if (cancelBtn) cancelBtn.addEventListener('click', cancelPlanEdit);
  if (editor) {
    editor.addEventListener('input', () => {
      if (!STATE.planEditing) return;
      STATE.previewCache.interview = null;
      requestAnimationFrame(() => updateMarkdownPreview('interview', true));
    });
    editor.addEventListener('keydown', (ev) => {
      if (!STATE.planEditing) return;
      if ((ev.ctrlKey || ev.metaKey) && (ev.key === 's' || ev.key === 'S')) {
        ev.preventDefault();
        savePlanEdit();
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        cancelPlanEdit();
      } else {
        handleEditorTab(ev, editor);
      }
    });
  }
})();

document.getElementById('btn-notes-open').addEventListener('click', openNotesModal);
document.getElementById('btn-notes-close').addEventListener('click', closeNotesModal);
document.getElementById('btn-notes-save').addEventListener('click', saveNotes);
document.getElementById('notes-modal').addEventListener('click', (event) => {
  if (event.target.id === 'notes-modal') closeNotesModal();
});

// ===== AR (Automated Research) =====
//
// One panel serves both AR roles. A studio task mines papers and turns ideas
// into child tasks; a paper task walks draft -> your gate -> author/reviewer
// rounds -> your gate -> delivery. Which half renders is decided by
// state.role, which the server keeps in .RUD/<slug>/ar.json.

const AR = {
  catalog: null,
  data: null,
  slug: null,
  selected: new Set(),
  timer: null,
  busy: false,
  studioFp: '',   // last-rendered lists, so unchanged polls skip the rebuild
};

const AR_STAGES = [
  ['draft', 'Draft'],
  ['await_draft_review', 'Your review'],
  ['loop', 'Rounds'],
  ['await_final_review', 'Final review'],
  ['delivered', 'Delivered'],
];

function arTaskPath(suffix) {
  return '/api/tasks/' + encodeURIComponent(AR.slug) + '/ar' + (suffix || '');
}

async function loadArCatalog() {
  if (AR.catalog) return AR.catalog;
  try {
    AR.catalog = await api('/api/ar/catalog');
  } catch (err) {
    console.debug('AR catalog load failed', err);
    AR.catalog = { directions: [], venues: [], default_venue: '', default_max_rounds: 10 };
  }
  return AR.catalog;
}

function resetArLab() {
  if (AR.timer) { clearInterval(AR.timer); AR.timer = null; }
  AR.data = null;
  AR.slug = null;
  AR.selected = new Set();
}

async function initArLab(meta) {
  const slug = STATE.slug;
  AR.slug = slug;
  AR.selected = new Set();
  AR.studioFp = '';
  await loadArCatalog();
  await refreshAr();
  if (AR.timer) clearInterval(AR.timer);
  AR.timer = setInterval(() => {
    if (document.hidden || STATE.slug !== AR.slug) return;
    const panel = document.querySelector('.tab-panel[data-panel="ar"]');
    if (!panel || panel.hidden) return;
    refreshAr(true);
  }, 5000);
}

async function refreshAr(quiet = false) {
  const slug = AR.slug;
  if (!slug) return;
  try {
    const d = await api(arTaskPath());
    if (AR.slug !== slug) return;
    AR.data = d;
    renderAr(d);
  } catch (err) {
    if (!quiet) toast(err.message || 'Could not load AR state', { type: 'error' });
  }
}

async function arPost(action, body, label) {
  if (AR.busy) return null;
  AR.busy = true;
  try {
    const d = await api(arTaskPath('/' + action), {
      method: 'POST',
      body: JSON.stringify(body || {}),
    });
    if (d && d.state) { AR.data = d; renderAr(d); }
    else await refreshAr(true);
    return d;
  } catch (err) {
    toast(`${label || action} failed: ${err.message}`, { type: 'error' });
    return null;
  } finally {
    AR.busy = false;
  }
}

function renderAr(d) {
  const state = (d && d.state) || {};
  const isPaper = state.role === 'paper';
  $('#ar-studio').hidden = isPaper;
  $('#ar-paper').hidden = !isPaper;
  const dirEl = $('#ar-direction-label');
  if (dirEl) dirEl.textContent = d.direction_label || state.direction || '—';
  const badge = $('#ar-stage-badge');
  if (badge) {
    badge.textContent = isPaper ? (d.stage_label || state.stage || '') : 'studio';
    badge.dataset.state = isPaper ? String(state.stage || '') : 'studio';
  }
  if (isPaper) renderArPaper(d, state);
  else renderArStudio(d, state);
}

// --- Studio ---

// Tail of a job's progress log. Kept pinned to the bottom while the job runs
// so new lines are visible without scrolling, but left alone once it is done
// so a user reading back through it isn't yanked away.
function renderArLog(elId, lines, running) {
  const el = document.getElementById(elId);
  if (!el) return;
  const text = (lines || []).join('\n');
  el.hidden = !text;
  if (!text) return;
  if (el.textContent !== text) {
    const pinned = running
      || el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
    el.textContent = text;
    if (pinned) el.scrollTop = el.scrollHeight;
  }
  el.classList.toggle('is-running', !!running);
}

function renderArStudio(d, state) {
  const logs = d.logs || {};
  renderArLog('ar-papers-log', logs.papers, state.papers_status === 'running');
  renderArLog('ar-ideas-log', logs.ideas, state.ideas_status === 'running');
  const papers = Array.isArray(state.papers) ? state.papers : [];
  const pStatus = $('#ar-papers-status');
  if (pStatus) {
    if (state.papers_status === 'running') pStatus.textContent = 'Mining arXiv…';
    else if (state.papers_error) pStatus.textContent = state.papers_error;
    else if (papers.length) pStatus.textContent = `${papers.length} paper(s), newest first.`;
    else pStatus.textContent = 'Nothing mined yet.';
  }
  const ideas = Array.isArray(state.ideas) ? state.ideas : [];
  const iStatus = $('#ar-ideas-status');
  if (iStatus) {
    if (state.ideas_status === 'running') iStatus.textContent = 'Generating ideas — this takes a few minutes…';
    else if (state.ideas_error) iStatus.textContent = state.ideas_error;
    else if (ideas.length) iStatus.textContent = `${ideas.length} idea(s). Select the ones worth a paper, then create tasks.`;
    else iStatus.textContent = 'No ideas yet. Generate them here, or paste a JSON array from the agent pane.';
  }
  // Statuses and logs update every poll; the lists rebuild only when their
  // data changed, so reading or ticking boxes survives the five-second poll.
  const fp = JSON.stringify([papers, ideas]);
  if (fp === AR.studioFp) return;
  AR.studioFp = fp;
  renderArStudioLists(papers, ideas);
}

function renderArStudioLists(papers, ideas) {
  const list = $('#ar-paper-list');
  if (list) {
    list.innerHTML = papers.map((p) => {
      const venue = p.venue ? `<span class="ar-tag">${escapeHtml(p.venue)}</span>` : '';
      return `<li class="ar-paper">
        <div class="ar-paper__head">
          <a href="${escapeHtml(p.url || '#')}" target="_blank" rel="noreferrer">${escapeHtml(p.title || '')}</a>
          ${venue}<span class="ar-paper__date">${escapeHtml(p.published || '')}</span>
        </div>
        <p class="ar-paper__summary">${escapeHtml((p.summary || '').slice(0, 320))}</p>
      </li>`;
    }).join('');
  }

  const host = $('#ar-idea-list');
  if (!host) return;
  host.innerHTML = ideas.map((idea) => {
    const spawned = idea.status === 'spawned';
    const checked = AR.selected.has(idea.id) ? ' checked' : '';
    const experiments = (idea.experiments || []).map((x) => `<li>${escapeHtml(x)}</li>`).join('');
    const link = spawned && idea.child_slug
      ? `<button type="button" class="btn btn--sm ar-idea__open" data-slug="${escapeHtml(idea.child_slug)}">Open task</button>`
      : '';
    return `<article class="ar-idea${spawned ? ' is-spawned' : ''}">
      <header class="ar-idea__head">
        <label class="ar-idea__pick">
          <input type="checkbox" data-idea="${escapeHtml(idea.id)}"${checked}${spawned ? ' disabled' : ''} />
          <strong>${escapeHtml(idea.title)}</strong>
        </label>
        <span class="ar-idea__score">${Number(idea.score || 0).toFixed(2)}</span>
        ${link}
      </header>
      <dl class="ar-idea__body">
        ${idea.hypothesis ? `<dt>Hypothesis</dt><dd>${escapeHtml(idea.hypothesis)}</dd>` : ''}
        ${idea.novelty ? `<dt>Why it is new</dt><dd>${escapeHtml(idea.novelty)}</dd>` : ''}
        ${idea.metric ? `<dt>Metric</dt><dd>${escapeHtml(idea.metric)}</dd>` : ''}
        ${experiments ? `<dt>Experiments</dt><dd><ul>${experiments}</ul></dd>` : ''}
        ${idea.risk ? `<dt>Risk</dt><dd>${escapeHtml(idea.risk)}</dd>` : ''}
      </dl>
    </article>`;
  }).join('');

  host.querySelectorAll('input[data-idea]').forEach((box) => {
    box.addEventListener('change', () => {
      if (box.checked) AR.selected.add(box.dataset.idea);
      else AR.selected.delete(box.dataset.idea);
      updateArSpawnLabel();
    });
  });
  host.querySelectorAll('.ar-idea__open').forEach((btn) => {
    btn.addEventListener('click', () => selectTask(btn.dataset.slug));
  });
  updateArSpawnLabel();
}

// Keeping the count in the button label means the row needs no separate
// "N selected" text competing for the same line.
function updateArSpawnLabel() {
  const btn = document.getElementById('btn-ar-spawn');
  if (!btn) return;
  const n = AR.selected.size;
  btn.textContent = n ? `Create ${n} task${n === 1 ? '' : 's'}` : 'Create tasks';
}

// --- Paper ---

function renderArPaper(d, state) {
  const title = $('#ar-paper-title');
  if (title) title.textContent = (state.idea && state.idea.title) || 'Paper';

  const stepper = $('#ar-stepper');
  if (stepper) {
    const currentIdx = AR_STAGES.findIndex(([id]) => id === state.stage);
    stepper.innerHTML = AR_STAGES.map(([id, label], i) => {
      const cls = i < currentIdx ? 'is-done' : (i === currentIdx ? 'is-current' : '');
      const extra = id === 'loop' ? ` (${state.round || 0}/${state.max_rounds || 0})` : '';
      return `<li class="ar-step ${cls}">${escapeHtml(label)}${extra}</li>`;
    }).join('');
  }

  const hint = $('#ar-paper-hint');
  if (hint) {
    const bits = [];
    if (d.paper_dir) bits.push(d.paper_dir);
    if (Number(state.cost_usd) > 0) bits.push(`$${Number(state.cost_usd).toFixed(2)} spent`);
    if (state.stop_reason) bits.push(`stopped early: ${state.stop_reason}`);
    if (state.pdf_error) bits.push(state.pdf_error);
    else if (state.pdf_built_at) bits.push(`PDF built ${state.pdf_built_at}`);
    hint.textContent = bits.join(' · ');
  }

  const brief = $('#ar-idea-brief');
  if (brief) {
    const idea = state.idea || {};
    brief.innerHTML = idea.hypothesis
      ? `<p><strong>Hypothesis.</strong> ${escapeHtml(idea.hypothesis)}</p>` +
        (idea.metric ? `<p><strong>Metric.</strong> ${escapeHtml(idea.metric)}</p>` : '')
      : '';
  }

  renderArGate(state);
  renderArSubmission(d.submission);
  renderArRounds(d, state);
}

function renderArSubmission(sub) {
  const host = $('#ar-submission');
  if (!host) return;
  host.hidden = !sub;
  if (!sub) return;
  const list = $('#ar-checklist');
  if (list) {
    list.innerHTML = (sub.checks || []).map((c) =>
      `<li class="ar-check-item ${c.ok ? 'is-ok' : 'is-bad'}">
        <span class="ar-check-item__mark" aria-hidden="true">${c.ok ? '✓' : '✗'}</span>
        <span>${escapeHtml(c.label)}${c.detail ? ` <em>${escapeHtml(c.detail)}</em>` : ''}</span>
      </li>`).join('');
  }
  const title = $('#ar-submission__title') || host.querySelector('.ar-submission__title');
  if (title) {
    title.textContent = sub.ready
      ? `Submission readiness — ready for ${sub.venue_label}`
      : 'Submission readiness — not ready yet';
  }
  const cmd = $('#ar-submission-command');
  if (cmd) cmd.textContent = sub.command || '';
}

function renderArGate(state) {
  const card = $('#ar-gate-card');
  if (!card) return;
  const atDraft = state.stage === 'await_draft_review';
  const atFinal = state.stage === 'await_final_review';
  card.hidden = !(atDraft || atFinal);
  if (card.hidden) return;
  card.dataset.gate = atDraft ? 'draft' : 'final';
  $('#ar-gate-title').textContent = atDraft ? 'Draft gate' : 'Final gate';
  $('#ar-gate-hint').textContent = atDraft
    ? 'The skeleton draft is ready. Approve to open the author/reviewer rounds, or send it back with notes.'
    : `All ${state.max_rounds} rounds are done. Approve to deliver the paper, or send it back for another batch of rounds.`;
  $('#btn-ar-approve').textContent = atDraft ? 'Approve draft' : 'Approve and deliver';
}

function arReviewerSummaryCards(review, cssPrefix = 'ar') {
  const reviewers = review && Array.isArray(review.reviewers) ? review.reviewers : [];
  if (!reviewers.length) return '';
  const deciding = String((review && review.deciding_model) || '');
  return `<div class="${cssPrefix}-reviewer-grid">${reviewers.map((item) => {
    const model = String(item.model || 'reviewer');
    const scores = item.scores || {};
    const rating = scores.rating == null ? '–' : `${scores.rating}/10`;
    const recommendation = String(scores.recommendation || '');
    const winner = deciding && model === deciding;
    return `<div class="${cssPrefix}-reviewer-card ${winner ? 'is-deciding' : ''}">
      <div class="${cssPrefix}-reviewer-card__model">${escapeHtml(model)}</div>
      <div class="${cssPrefix}-reviewer-card__score">${escapeHtml(rating)}</div>
      <div class="${cssPrefix}-reviewer-card__verdict">${escapeHtml(recommendation)}</div>
      ${winner ? `<span class="${cssPrefix}-reviewer-card__badge">lowest · final</span>` : ''}
    </div>`;
  }).join('')}</div>`;
}

function renderArReviewerReports(payload) {
  const reviewers = Array.isArray(payload.reviewers) ? payload.reviewers : [];
  if (!reviewers.length) return renderMarkdown(payload.review || '');
  const deciding = String(payload.deciding_model || '');
  const verdict = deciding
    ? `<div class="ar-panel-verdict">Final round score uses the lowest-Rating reviewer: <strong>${escapeHtml(deciding)}</strong>.</div>`
    : '';
  return verdict + reviewers.map((item) => {
    const model = String(item.model || 'reviewer');
    const scores = item.scores || {};
    const winner = deciding && model === deciding;
    const chips = Object.entries(scores)
      .map(([key, value]) => `<span class="ar-score">${escapeHtml(key)} ${escapeHtml(String(value))}</span>`)
      .join('');
    return `<section class="ar-reviewer-report ${winner ? 'is-deciding' : ''}">
      <header class="ar-reviewer-report__head">
        <div>
          <p class="ar-reviewer-report__eyebrow">Independent reviewer${winner ? ' · lowest score' : ''}</p>
          <h3>${escapeHtml(model)}</h3>
        </div>
        <div class="ar-round__scores">${chips}</div>
      </header>
      <div class="ar-reviewer-report__body">${renderMarkdown(item.review || '')}</div>
    </section>`;
  }).join('');
}

function renderArRounds(d, state) {
  renderArLog(
    'ar-review-log',
    (d.logs || {}).review,
    state.review_status === 'running' || !!(d.loop || {}).running,
  );
  const status = $('#ar-loop-status');
  if (status) {
    const loop = d.loop || {};
    const bits = [];
    if (loop.running) bits.push('Loop running');
    else bits.push('Loop stopped');
    if (loop.last_action) bits.push(loop.last_action);
    if (loop.last_error) bits.push(`error: ${loop.last_error}`);
    if (state.review_status === 'running') bits.push('review in progress…');
    if (d.plateaued) bits.push('score has stalled — the author was told to change tack');
    status.textContent = bits.join(' · ');
  }

  const host = $('#ar-round-list');
  if (!host) return;
  const rounds = Array.isArray(state.rounds) ? state.rounds : [];
  // The round summaries scroll; rebuilding them on an unchanged poll resets
  // that scroll (and any text selection) every five seconds.
  const fp = JSON.stringify(rounds);
  if (fp === host.dataset.fp) return;
  host.dataset.fp = fp;
  if (!rounds.length) {
    host.innerHTML = '<li class="ar-round ar-round--empty">No rounds yet.</li>';
    return;
  }
  host.innerHTML = rounds.slice().reverse().map((r) => {
    const label = r.n === 0 ? 'Draft' : `Round ${r.n}`;
    const author = r.author || null;
    const review = r.review || null;
    const readiness = r.readiness || null;
    const readinessFailures = readiness && Array.isArray(readiness.failed)
      ? readiness.failed
      : [];
    const readinessHeadline = readiness
      ? (readiness.ready
        ? 'readiness passed'
        : `review blocked: ${readinessFailures.length} readiness check(s) failed`)
      : '';
    const scores = (review && review.scores) || {};
    const chips = Object.entries(scores)
      .map(([k, v]) => `<span class="ar-score">${escapeHtml(k)} ${escapeHtml(String(v))}</span>`)
      .join('');
    const reviewBtn = review
      ? `<button type="button" class="btn btn--sm ar-round__review" data-round="${r.n}">Read review</button>`
      : '';
    const reviewerCards = arReviewerSummaryCards(review);
    const authorSummary = author && author.summary
      ? `<pre class="ar-round__summary">${escapeHtml(author.summary.slice(0, 900))}</pre>`
      : '<p class="ar-round__pending">Author working…</p>';
    return `<li class="ar-round">
      <div class="ar-round__head">
        <strong>${escapeHtml(label)}</strong>
        <span class="ar-round__headline">${escapeHtml((review && review.headline) || (r.review_error ? `review failed: ${r.review_error}` : readinessHeadline))}</span>
        ${reviewBtn}
      </div>
      <div class="ar-round__scores">${chips}</div>
      ${reviewerCards}
      ${authorSummary}
    </li>`;
  }).join('');

  host.querySelectorAll('.ar-round__review').forEach((btn) => {
    btn.addEventListener('click', () => openArReview(Number(btn.dataset.round)));
  });
}

async function openArReview(n) {
  try {
    const d = await api(arTaskPath('/review/' + n));
    $('#ar-review-title').textContent = n === 0 ? 'Draft review' : `Round ${n} review`;
    $('#ar-review-content').innerHTML = renderArReviewerReports(d);
    typesetMath($('#ar-review-content'));
    $('#ar-review-modal').hidden = false;
    document.body.classList.add('preview-open');
  } catch (err) {
    toast(err.message || 'Could not load the review', { type: 'error' });
  }
}

function closeArReview() {
  $('#ar-review-modal').hidden = true;
  document.body.classList.remove('preview-open');
}

function downloadArPdf() {
  if (!AR.slug) return;
  // A plain navigation lets the browser handle the PDF stream and the
  // Content-Disposition filename; fetch + blob would lose both.
  window.open(withProjectQuery(arTaskPath('/pdf')), '_blank');
}

// --- AR panel wire-up ---

document.getElementById('btn-ar-refresh').addEventListener('click', () => refreshAr());
document.getElementById('btn-ar-mine').addEventListener('click', () => {
  arPost('mine', { venue_only: $('#ar-venue-only').checked }, 'Mining');
});
document.getElementById('btn-ar-ideas').addEventListener('click', () => {
  arPost('ideas', { count: Number($('#ar-idea-count').value || 6) }, 'Idea generation');
});
document.getElementById('btn-ar-spawn').addEventListener('click', async () => {
  const ids = [...AR.selected];
  if (!ids.length) { toast('Select at least one idea first.'); return; }
  const d = await arPost('spawn', { idea_ids: ids }, 'Creating tasks');
  if (!d) return;
  AR.selected = new Set();
  updateArSpawnLabel();
  const made = (d.spawned || []).length;
  if (made) toast(`Created ${made} paper task(s).`);
  (d.errors || []).forEach((e) => toast(e, { type: 'error' }));
  await loadTasks();
});
document.getElementById('btn-ar-build').addEventListener('click', async () => {
  const d = await arPost('build', {}, 'Build');
  if (d && d.build && !d.build.ok) toast(d.build.error || 'Build failed', { type: 'error' });
  else if (d && d.build && !d.build.clean) toast('PDF built, but LaTeX reported errors.');
});
document.getElementById('btn-ar-download').addEventListener('click', downloadArPdf);
document.getElementById('btn-ar-submission').addEventListener('click', async () => {
  const d = await arPost('submission', {}, 'Submission prep');
  if (d && d.submission) {
    toast(d.submission.ready
      ? `Ready for ${d.submission.venue_label}. submission.json written.`
      : 'submission.json written — see the checklist for what still blocks it.');
  }
});
document.getElementById('btn-ar-loop-start').addEventListener('click', () => arPost('loop/start', {}, 'Start loop'));
document.getElementById('btn-ar-loop-stop').addEventListener('click', () => arPost('loop/stop', {}, 'Stop loop'));
document.getElementById('btn-ar-review-now').addEventListener('click', () => arPost('review', {}, 'Review'));
document.getElementById('btn-ar-approve').addEventListener('click', () => {
  const gate = $('#ar-gate-card').dataset.gate || 'draft';
  arPost('gate', { gate, decision: 'approve', note: $('#ar-gate-note').value }, 'Approve');
  $('#ar-gate-note').value = '';
});
document.getElementById('btn-ar-reject').addEventListener('click', () => {
  const gate = $('#ar-gate-card').dataset.gate || 'draft';
  arPost('gate', { gate, decision: 'reject', note: $('#ar-gate-note').value }, 'Request changes');
  $('#ar-gate-note').value = '';
});
document.getElementById('btn-ar-review-close').addEventListener('click', closeArReview);
document.getElementById('ar-review-modal').addEventListener('click', (event) => {
  if (event.target.id === 'ar-review-modal') closeArReview();
});

// ===== Claude pane info card (worktree + session history + Resume) =====

function formatSessionMtime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  } catch { return ''; }
}

function shortSessionId(sid) {
  const s = String(sid || '');
  if (s.length <= 12) return s;
  return s.slice(0, 8);
}

function formatContextBytes(bytes) {
  const n = Number(bytes || 0);
  if (!n) return '0 KB';
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

// Build the worktree rows (path + branch + status + push/remove) into
// *wtHost*, and toggle *pushAllBtn* visibility.
function renderWorktreeListInto(wtHost, pushAllBtn, meta, statuses, primaryLabel) {
  meta = meta || {};
  const worktrees = Array.isArray(meta.worktrees) && meta.worktrees.length
    ? meta.worktrees
    : (meta.worktree_path ? [meta.worktree_path] : []);
  const branches = Array.isArray(meta.branches) && meta.branches.length
    ? meta.branches
    : (meta.branch ? [meta.branch] : []);
  const statusByPath = {};
  for (const s of (Array.isArray(statuses) ? statuses : [])) {
    if (s && s.path) statusByPath[s.path] = s;
  }
  if (pushAllBtn) pushAllBtn.hidden = worktrees.length <= 1;
  if (!wtHost) return;
  wtHost.innerHTML = '';
  if (!worktrees.length) {
    const hint = document.createElement('span');
    hint.className = 'claude-info__hint';
    hint.textContent = '(none — click + Add worktree, or git worktree add manually under .RUD/<slug>/work/)';
    wtHost.appendChild(hint);
    return;
  }
  // Compact picker: a dropdown to choose a worktree, its live status, and the
  // Push / remove buttons on the right acting on the *selected* worktree.
  const row = document.createElement('div');
  row.className = 'wt-picker';

  const sel = document.createElement('select');
  sel.className = 'wt-select';
  worktrees.forEach((path, i) => {
    const opt = document.createElement('option');
    opt.value = path;
    // Prefer the LIVE branch from `git status` (so a manual `git checkout`
    // inside the worktree is reflected); fall back to the persisted meta
    // branch only when a live status isn't available yet.
    let liveBranch = ((statusByPath[path] && statusByPath[path].branch) || '').trim();
    if (/\(no branch\)/.test(liveBranch) || liveBranch === 'HEAD') liveBranch = '(detached)';
    const br = liveBranch || (branches[i] || '').trim() || '—';
    opt.textContent = `${changesBaseName(path)} · ${br}${i === 0 ? ' · primary' : ''}`;
    opt.title = path;
    sel.appendChild(opt);
  });
  row.appendChild(sel);

  const badgeHost = document.createElement('span');
  badgeHost.className = 'wt-picker__status';
  const renderBadge = () => {
    badgeHost.innerHTML = '';
    const st = statusByPath[sel.value];
    if (st) badgeHost.appendChild(renderWorktreeStatusBadge(st));
  };
  sel.addEventListener('change', renderBadge);
  row.appendChild(badgeHost);

  const push = document.createElement('button');
  push.type = 'button';
  push.className = 'btn btn--sm wt-picker__push';
  push.textContent = 'Push';
  push.title = 'git push -u origin <selected branch>';
  push.addEventListener('click', () => pushWorktree(sel.value, push));
  row.appendChild(push);

  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'wt-list-row__remove';
  rm.title = 'Remove the selected worktree (git worktree remove + delete dir)';
  rm.setAttribute('aria-label', 'Remove selected worktree');
  rm.textContent = '×';
  rm.addEventListener('click', () => removeWorktree(sel.value));
  row.appendChild(rm);

  wtHost.appendChild(row);
  renderBadge();
}


function renderClaudeInfo(meta, claude, statuses) {
  meta = meta || {};
  claude = claude || {};
  const tmuxEl = $('#claude-info-tmux');
  const pillEl = $('#claude-info-tmux-state');
  const sessHost = $('#claude-info-sessions');
  renderWorktreeListInto(
    $('#claude-info-worktree-list'),
    document.getElementById('btn-worktree-push-all'),
    meta,
    statuses,
    'Primary — the agent pane opens in this worktree',
  );
  if (tmuxEl) tmuxEl.textContent = claude.tmux_target || meta.tmux_interview_target || '(not started)';
  if (pillEl) {
    const alive = !!claude.tmux_alive;
    pillEl.textContent = alive ? 'alive' : 'down';
    pillEl.dataset.state = alive ? 'alive' : 'down';
  }
  if (!sessHost) return;
  const sessions = Array.isArray(claude.sessions) ? claude.sessions : [];
  const running = claude.agent_running === true;
  // The 12s poll lands here: rebuilding an unchanged picker snaps the
  // dropdown shut under the user's pointer and resets their selection.
  const fp = JSON.stringify([
    STATE.currentMeta?.agent, running, claude.pane_command || '',
    sessions.map((s) => [s.id, s.mtime, s.size, s.near_budget, s.over_budget, s.path ? 1 : 0]),
  ]);
  if (sessHost.dataset.fp === fp) return;
  sessHost.dataset.fp = fp;
  const prevPick = sessHost.querySelector('select.session-select')?.value || '';
  sessHost.innerHTML = '';
  if (!sessions.length) {
    const span = document.createElement('span');
    span.className = 'claude-info__hint';
    const label = agentLabel(STATE.currentMeta?.agent);
    span.textContent = `No ${label} sessions captured yet. Start ${label} to bind one.`;
    sessHost.appendChild(span);
    return;
  }
  // Dropdown of sessions (newest first) + a single Resume button on the right,
  // mirroring the worktree picker. Resuming acts on the selected session.
  const row = document.createElement('div');
  row.className = 'session-picker';

  const sel = document.createElement('select');
  sel.className = 'session-select';
  sel.setAttribute('aria-label', 'Claude session to resume');
  sessions.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id || '';
    const bits = [shortSessionId(s.id)];
    if (s.mtime) bits.push(formatSessionMtime(s.mtime));
    if (s.size) bits.push(formatContextBytes(s.size));
    if (s.near_budget) bits.push('CONTEXT GROWING');
    if (s.over_budget) bits.push('OVER CONTEXT BUDGET');
    if (!s.path) bits.push('no transcript');
    opt.textContent = bits.join(' · ');
    opt.title = s.id || '';
    if (!s.id) opt.disabled = true;
    sel.appendChild(opt);
  });
  // A rebuild forced by a state change keeps the user's pick when possible.
  if (prevPick && sessions.some((s) => s.id === prevPick)) sel.value = prevPick;
  row.appendChild(sel);

  const resume = document.createElement('button');
  resume.type = 'button';
  resume.className = 'btn btn--sm session-picker__resume';
  resume.textContent = 'Resume';
  resume.disabled = running;
  resume.title = running
    ? `Stop the running pane command (${claude.pane_command || 'agent'}) before resuming.`
    : 'Resume the selected session in a fresh or idle tmux pane.';
  resume.addEventListener('click', () => { if (sel.value) resumeClaudeSession(sel.value); });
  row.appendChild(resume);

  const fresh = document.createElement('button');
  fresh.type = 'button';
  fresh.className = 'btn btn--sm session-picker__fresh';
  fresh.textContent = 'New session';
  fresh.title = 'Start a clean session and reload task state from PLAN.md.';
  fresh.addEventListener('click', rotateClaudeSession);
  row.appendChild(fresh);

  sessHost.appendChild(row);
}

// ===== Worktree picker modal =====

async function openWorktreeModal() {
  if (!STATE.slug) return;
  const modal = $('#worktree-modal');
  if (!modal) return;
  modal.hidden = false;
  $('#wt-modal-branch').textContent = `loom/${STATE.slug}`;
  $('#wt-modal-dest').textContent = `.RUD/${STATE.slug}/work/<repo>/`;
  const host = $('#wt-candidates');
  const status = $('#wt-status');
  status.textContent = '';
  host.innerHTML = '<div class="claude-info__hint">Scanning project root for git repos…</div>';
  try {
    const d = await api(`/api/tasks/${encodeURIComponent(STATE.slug)}/worktree-candidates`);
    renderWorktreeCandidates(d.candidates || [], d.projectRoot || '');
  } catch (err) {
    host.innerHTML = `<div class="status-bad">${escapeHtml(err.message || 'failed')}</div>`;
  }
}

function renderWorktreeCandidates(candidates, projectRoot) {
  const host = $('#wt-candidates');
  host.innerHTML = '';
  if (!candidates.length) {
    const help = document.createElement('div');
    help.className = 'claude-info__hint';
    help.innerHTML = `No git repos found at <code>${escapeHtml(projectRoot)}</code> or its immediate subdirectories. <br>Either register a git-repo path as the project, or <code>git worktree add</code> manually into <code>.RUD/${escapeHtml(STATE.slug)}/work/</code> and reopen the Claude tab.`;
    host.appendChild(help);
    return;
  }
  for (const c of candidates) {
    const row = document.createElement('div');
    row.className = 'wt-candidate' + (c.already_created ? ' wt-candidate--done' : '');
    const info = document.createElement('div');
    info.className = 'wt-candidate__info';
    const name = document.createElement('div');
    name.className = 'wt-candidate__name';
    name.innerHTML = `<strong>${escapeHtml(c.name)}</strong> <span class="wt-candidate__kind">${escapeHtml(c.kind)}</span>`;
    if (c.already_created) {
      name.innerHTML += ' <span class="wt-candidate__kind wt-candidate__kind--done">already added</span>';
    }
    info.appendChild(name);
    const src = document.createElement('div');
    src.className = 'wt-candidate__path';
    src.innerHTML = `<span>source </span><code>${escapeHtml(c.path)}</code>`;
    info.appendChild(src);
    if (c.destination) {
      const dst = document.createElement('div');
      dst.className = 'wt-candidate__path';
      dst.innerHTML = `<span>landing </span><code>${escapeHtml(c.destination)}</code>`;
      info.appendChild(dst);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--primary btn--sm';
    btn.textContent = c.already_created ? 'Added' : 'Create';
    btn.disabled = !!c.already_created;
    if (!c.already_created) {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        $('#wt-status').textContent = `Creating ${c.name}…`;
        try {
          const r = await api(`/api/tasks/${encodeURIComponent(STATE.slug)}/worktree`, {
            method: 'POST',
            body: JSON.stringify({ source_repo: c.path }),
          });
          if (!r.ok) throw new Error(r.error || 'create failed');
          $('#wt-status').textContent = `Created at ${r.worktree_path}`;
          // Refresh both the Claude info card and the modal candidate list
          // so the user can keep adding more.
          await selectTask(STATE.slug);
          const fresh = await api(`/api/tasks/${encodeURIComponent(STATE.slug)}/worktree-candidates`);
          renderWorktreeCandidates(fresh.candidates || [], fresh.projectRoot || projectRoot);
        } catch (err) {
          $('#wt-status').textContent = err.message || 'create failed';
          btn.disabled = false;
        }
      });
    }
    row.appendChild(info);
    row.appendChild(btn);
    host.appendChild(row);
  }
}

function renderWorktreeStatusBadge(st) {
  const span = document.createElement('span');
  span.className = 'wt-status';
  const parts = [];
  if (st.error) {
    span.classList.add('wt-status--error');
    span.textContent = `error: ${st.error}`;
    return span;
  }
  if (st.clean) {
    span.classList.add('wt-status--clean');
    parts.push('● clean');
  } else {
    span.classList.add('wt-status--dirty');
    const breakdown = [];
    if (st.staged) breakdown.push(`${st.staged} staged`);
    if (st.unstaged) breakdown.push(`${st.unstaged} modified`);
    if (st.untracked) breakdown.push(`${st.untracked} untracked`);
    parts.push(breakdown.join(', ') || `${st.dirty_count} changes`);
  }
  if (st.has_remote) {
    if (st.ahead) parts.push(`↑${st.ahead}`);
    if (st.behind) parts.push(`↓${st.behind}`);
    if (!st.ahead && !st.behind) parts.push('in sync');
  } else {
    parts.push('no remote');
    span.classList.add('wt-status--noremote');
  }
  span.textContent = parts.join(' · ');
  return span;
}

async function pushWorktree(path, btn) {
  if (!STATE.slug || !path) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Pushing…';
  try {
    const r = await api(
      `/api/tasks/${encodeURIComponent(STATE.slug)}/worktree/push`,
      { method: 'POST', body: JSON.stringify({ path }) },
    );
    if (!r.ok) throw new Error(r.error || r.message || 'push failed');
    btn.textContent = 'Pushed';
    setTimeout(() => {
      btn.textContent = original;
      btn.disabled = false;
    }, 1500);
    // Refresh the info card so the ahead/behind badge updates.
    await refreshTaskTemplates();
  } catch (err) {
    btn.textContent = original;
    btn.disabled = false;
    toast(err.message || 'push failed', { type: 'error' });
  }
}

async function pushAllWorktrees() {
  if (!STATE.slug) return;
  const btn = document.getElementById('btn-worktree-push-all');
  if (!btn) return;
  if (!confirm('Push all worktree branches to origin?')) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Pushing all…';
  try {
    const r = await api(
      `/api/tasks/${encodeURIComponent(STATE.slug)}/worktrees/push-all`,
      { method: 'POST', body: '{}' },
    );
    const lines = (r.results || []).map((row) => {
      const tag = row.ok ? 'ok' : 'failed';
      return `${tag}: ${row.path} → ${row.branch || '(no branch)'}\n  ${row.message || row.error || ''}`;
    });
    toast(`Pushed ${r.results.filter((x) => x.ok).length}/${r.count}\n\n${lines.join('\n\n')}`, { type: 'success', ttl: 9000 });
    await refreshTaskTemplates();
  } catch (err) {
    toast(err.message || 'push-all failed', { type: 'error' });
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
}

async function removeWorktree(path) {
  if (!STATE.slug || !path) return;
  if (!confirm(`Remove worktree?\n\n${path}\n\nRuns "git worktree remove" and deletes the directory.`)) return;
  try {
    await api(
      `/api/tasks/${encodeURIComponent(STATE.slug)}/worktree?path=${encodeURIComponent(path)}`,
      { method: 'DELETE' },
    );
    await selectTask(STATE.slug);
  } catch (err) {
    toast(err.message || 'remove failed', { type: 'error' });
  }
}

function closeWorktreeModal() {
  const m = $('#worktree-modal');
  if (m) m.hidden = true;
}

document.getElementById('btn-create-worktree').addEventListener('click', openWorktreeModal);
document.getElementById('btn-wt-close').addEventListener('click', closeWorktreeModal);
document.getElementById('btn-wt-cancel').addEventListener('click', closeWorktreeModal);
$('#worktree-modal').addEventListener('click', (event) => {
  if (event.target.id === 'worktree-modal') closeWorktreeModal();
});

async function refreshClaudeSessions() {
  if (!STATE.slug) return;
  if (STATE.pollInFlight.sessions) return;
  const slug = STATE.slug;
  STATE.pollInFlight.sessions = true;
  try {
    const d = await apiWithRetry(
      '/api/tasks/' + encodeURIComponent(slug) + '/claude-sessions',
      {},
      { attempts: 2, delayMs: 300 },
    );
    if (STATE.slug !== slug) return;
    renderClaudeInfo(STATE.currentMeta || {}, d, STATE.worktreeStatuses || []);
  } catch (err) {
    console.debug('refreshClaudeSessions failed', err);
  } finally {
    STATE.pollInFlight.sessions = false;
  }
}

async function resumeClaudeSession(sessionId) {
  if (!STATE.slug || !sessionId) return;
  if (!confirm(`Resume Claude session ${sessionId} in a fresh or idle tmux pane?`)) return;
  try {
    let r;
    try {
      r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/resume', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch (err) {
      if (err?.body?.code !== 'context_budget_exceeded') throw err;
      const s = err.body.session || {};
      const warning = `This history is ${formatContextBytes(s.size)} and exceeds the ` +
        `${formatContextBytes(s.budget_bytes)} context budget. Resuming it can be very expensive.\n\n` +
        'Choose Cancel and use New session unless the old chat itself is essential. Resume anyway?';
      if (!confirm(warning)) return;
      r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/resume', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId, allow_oversize: true }),
      });
    }
    if (!r.ok) throw new Error(r.error || 'resume failed');
    $('#inp-interview-target').value = r.target || '';
    setTmuxOutputText(`Resuming Claude session ${sessionId}\nNew tmux target: ${r.target || '(pending)'}`);
    await refreshInterviewPreview(true);
    await refreshClaudeSessions();
  } catch (err) {
    toast(err.message || 'resume failed', { type: 'error' });
  }
}

async function rotateClaudeSession() {
  if (!STATE.slug) return;
  if (!confirm('Start a clean agent session? Loom will reload task state from PLAN.md and stop the current pane.')) return;
  try {
    const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/rotate', {
      method: 'POST',
      body: JSON.stringify({ paste_prompt: true }),
    });
    $('#inp-interview-target').value = r.target || '';
    setTmuxOutputText('Started a fresh context window and reloaded task state from PLAN.md.');
    await refreshInterviewPreview(true);
    await refreshClaudeSessions();
  } catch (err) {
    toast(err.message || 'could not start a fresh session', { type: 'error' });
  }
}

async function sendTaskMessage(text) {
  const endpoint = '/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/send';
  try {
    return await api(endpoint, {
      method: 'POST',
      body: JSON.stringify({ text, submit: true }),
    });
  } catch (err) {
    if (err?.body?.code !== 'context_budget_exceeded') throw err;
    const s = err.body.context?.session || {};
    const live = err.body.context?.live_usage || {};
    const reason = err.body.context?.reason ||
      `the transcript reached ${formatContextBytes(s.size)} of ${formatContextBytes(s.budget_bytes)}`;
    const detail = live.turn_tokens ? ` Current turn: ${Number(live.turn_tokens).toLocaleString()} tokens.` : '';
    const warning = `This session reached Loom's usage budget: ${reason}.${detail}\n\n` +
      'Start a clean session, reload PLAN.md, and send this message there?';
    if (!confirm(warning)) throw err;
    return api(endpoint, {
      method: 'POST',
      body: JSON.stringify({ text, submit: true, rotate_context: true }),
    });
  }
}

// These four hand control to the agent, so the keyboard belongs in the pane
// once they finish — otherwise the next keystroke dies on the button.
function clickThenFocusPane(handler) {
  return async (event) => {
    try { await handler(event); } finally { focusTerminalSoon(); }
  };
}
document.getElementById('btn-interview-start').addEventListener('click', clickThenFocusPane(startInterviewPane));
document.getElementById('btn-interview-paste').addEventListener('click', clickThenFocusPane(pasteInterviewPrompt));
document.getElementById('btn-run-goal').addEventListener('click', clickThenFocusPane(runGoalFromPlan));
document.getElementById('btn-write-result').addEventListener('click', clickThenFocusPane(writeResultToPlan));
document.getElementById('btn-changes-refresh').addEventListener('click', () => refreshChangesView(true));

// Mobile/touch terminal keys: phone keyboards have no Esc/Ctrl-C/arrows/Tab, so
// this bar sends the raw byte sequences into the pane (send_pane_literal also
// leaves copy-mode first, so it works even after scrolling).
// Compose box: a normal input where the OS IME works reliably (xterm's built-in
// CJK input is buggy - repeats/loses characters). Enter sends the line into the
// pane and clears. English/keys can still be typed straight into the terminal.
(function initTermCompose() {
  const input = document.getElementById('term-compose-input');
  const sendBtn = document.getElementById('term-compose-send');
  const toggle = document.getElementById('term-compose-toggle');
  const box = document.getElementById('term-compose');
  if (!input) return;
  const MAX_H = 168;
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, MAX_H) + 'px';
  }
  function setOpen(open) {
    if (!box) return;
    box.hidden = !open;
    if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) { autoGrow(); input.focus(); }
    else if (TERM.term) { try { TERM.term.focus(); } catch (e) {} }
  }
  async function submitCompose() {
    const text = input.value;
    const target = termTarget();
    if (!text.trim() || !target) return;
    input.value = '';
    const key = paneDraftKey();
    if (key) STATE.composeDrafts[key] = '';
    autoGrow();
    try {
      const result = await sendTaskMessage(text);
      if (result?.target) $('#inp-interview-target').value = result.target;
    } catch (err) {
      input.value = text;
      if (key) STATE.composeDrafts[key] = text;
      autoGrow();
      console.debug('compose send failed', err);
    }
    termScheduleRefresh();
    input.focus();
  }
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', (e) => {
    // Enter sends; Shift+Enter inserts a newline (default textarea behaviour);
    // the Enter that commits an IME composition has isComposing=true and must
    // be left alone so Chinese input still works.
    if (e.key === 'Enter' && !e.isComposing && !e.shiftKey) {
      e.preventDefault();
      submitCompose();
    }
  });
  if (sendBtn) sendBtn.addEventListener('click', submitCompose);
  // Collapsed by default; the Chinese Input button reveals the reliable-IME compose box.
  if (toggle) toggle.addEventListener('click', () => setOpen(box && box.hidden));
})();

(function initTermMobileKeys() {
  const bar = document.getElementById('term-mobile-keys');
  if (!bar) return;
  const SEQ = {
    esc: '\x1b', 'c-c': '\x03', tab: '\t', enter: '\r',
    up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  };
  bar.addEventListener('click', (e) => {
    const btn = e.target.closest('.term-key');
    if (!btn) return;
    const seq = SEQ[btn.dataset.key];
    if (seq != null) termSendLiteral(seq);
  });
})();

(() => {
  const toggle = document.getElementById('monitor-toggle');
  if (toggle) toggle.addEventListener('change', () => setMonitor(toggle.checked));
})();

// Kill the current task's tmux pane (kill-session + every brand/agent alias).
// Shared by the toolbar "Stop" button and the terminal-bar "Close Tmux" button.
async function stopClaudePane() {
  if (!STATE.slug) return;
  if (!confirm('Close the current tmux session? Any running agent will be terminated too.')) return;
  const killBtn = document.getElementById('term-kill');
  const stopBtn = document.getElementById('btn-interview-stop');
  if (killBtn) killBtn.disabled = true;
  if (stopBtn) stopBtn.disabled = true;
  try {
    const r = await api('/api/tasks/' + encodeURIComponent(STATE.slug) + '/claude/stop', {
      method: 'POST',
      body: '{}',
    });
    const tgt = $('#inp-interview-target');
    if (tgt) tgt.value = '';
    disconnectTerminal();
    setTmuxOutputText(`Closed ${r.tmux_session || ''}\n${r.tmux_message || ''}`.trim());
    refreshInterviewPreview(true);
    refreshClaudeSessions();
  } catch (err) {
    toast((err && err.message) || 'Failed to close tmux', { type: 'error' });
  } finally {
    if (killBtn) killBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = false;
  }
}

document.getElementById('btn-interview-stop').addEventListener('click', stopClaudePane);
const termKillBtn = document.getElementById('term-kill');
if (termKillBtn) termKillBtn.addEventListener('click', stopClaudePane);

document.getElementById('btn-delete-task').addEventListener('click', deleteSelectedTask);

document.getElementById('btn-new-task').addEventListener('click', async () => {
  const title = $('#new-title').value.trim();
  const skillsEl = document.getElementById('new-skills');
  const skills_path = skillsEl ? selectedSkillsValue(skillsEl) : '';
  const agentSel = document.getElementById('new-agent-select');
  const agent = agentSel ? agentSel.value : 'cursor';
  const interviewModel = (document.getElementById('new-model')?.value || '').trim();
  const btn = $('#btn-new-task');
  const status = $('#new-task-status');
  // AR tasks have no general goal: the paper's content plays that role, and
  // the server derives the stored goal from the AR fields below.
  const general_goal = agent === 'ar' ? '' : $('#new-goal').value.trim();
  if (!title) {
    status.textContent = 'A title is required.';
    return;
  }
  if (agent !== 'ar' && !general_goal) {
    status.textContent = 'Title and general goal are required.';
    return;
  }
  btn.disabled = true;
  status.textContent = 'Creating…';
  try {
    const special = agent === 'ar' || agent === 'author';
    const body = {
      title,
      general_goal,
      agent: special ? 'cursor' : agent,
      interview_model: interviewModel,
    };
    if (agent === 'author') {
      body.kind = 'author';
      body.author_venue = $('#author-venue')?.value.trim() || '';
      body.author_main_tex = $('#author-main-tex')?.value.trim() || 'main.tex';
      body.author_manuscript_repo = $('#author-manuscript-repo')?.value.trim() || '';
      body.author_experiment_repo = $('#author-experiment-repo')?.value.trim() || '';
      if (!body.author_venue || !body.author_manuscript_repo) {
        status.textContent = 'Target venue and manuscript repository are required.';
        btn.disabled = false;
        return;
      }
    }
    if (agent === 'ar') {
      body.kind = 'ar';
      body.ar_direction = $('#ar-direction')?.value || '';
      body.ar_custom_direction = $('#ar-custom-direction')?.value.trim() || '';
      body.ar_venue = $('#ar-venue')?.value || '';
      body.ar_mode = document.querySelector('input[name="ar-mode"]:checked')?.value || 'auto';
      body.ar_seed_idea = $('#ar-seed-idea')?.value.trim() || '';
      body.ar_max_rounds = Number($('#ar-max-rounds')?.value || 10);
      if (body.ar_mode === 'seed' && !body.ar_seed_idea) {
        status.textContent = 'Describe what the paper should be about, or switch to auto direction.';
        btn.disabled = false;
        return;
      }
      delete body.general_goal;
    }
    if (skills_path) body.skills_path = skills_path;
    const { meta } = await api('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
    resetCreateForm();
    closeCreateModal();
    await loadTasks();
    await selectTask(meta.slug);
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

async function loadWorkspace() {
  await loadProjectsList();
  await loadProject();
  await loadTasks();
  await restoreSelectedTaskForProject();
}

(function initOfflineRetry() {
  const btn = document.getElementById('btn-offline-retry');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await loadWorkspace();
    } catch (e) {
      toast(e.message, { type: 'error' });
    } finally {
      btn.disabled = false;
    }
  });
})();

(async function init() {
  buildTabs();
  initMarkdownPreviews();
  initFullscreenPreviews();
  loadLastTaskMap();
  pollActivity();
  STATE.activityTimer = setInterval(() => {
    if (!document.hidden) pollActivity();
  }, 4000);
  try {
    await loadWorkspace();
  } catch (e) {
    console.error(e);
    toast(e.message, { type: 'error' });
  }
})();


/* ---- Loom server switcher -------------------------------------------------
   Every machine runs its own Loom and serves its own console, so "switching"
   means going to the other one's address; nothing is fetched across origins
   and each keeps its own token. The list lives in this browser, because no
   one server owns it.
--------------------------------------------------------------------------- */
(function () {
  var KEY = 'loom.servers.v1';
  var el = document.getElementById('server-switch');
  if (!el) return;

  function here() { return window.location.origin.replace(/\/$/, ''); }
  function label(url) {
    try { return new URL(url).host; } catch (e) { return url; }
  }
  function load() {
    var list = [];
    try { list = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { list = []; }
    if (!Array.isArray(list)) { list = []; }
    list = list.filter(function (s) { return s && s.url; });
    // Always know where you are, even before anything has been added.
    if (!list.some(function (s) { return s.url === here(); })) {
      list.unshift({ name: label(here()), url: here() });
    }
    return list;
  }
  function save(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) {}
  }
  function render() {
    var list = load();
    el.innerHTML = '';
    list.forEach(function (s) {
      var o = document.createElement('option');
      o.value = s.url;
      o.textContent = (s.url === here() ? '\u25CF ' : '\u25CB ') + (s.name || label(s.url));
      if (s.url === here()) { o.selected = true; }
      el.appendChild(o);
    });
    var sep = document.createElement('option');
    sep.disabled = true;
    sep.textContent = '\u2500\u2500\u2500\u2500\u2500\u2500';
    el.appendChild(sep);
    var add = document.createElement('option');
    add.value = '__add';
    add.textContent = 'Add a Loom\u2026';
    el.appendChild(add);
    if (list.length > 1) {
      var drop = document.createElement('option');
      drop.value = '__forget';
      drop.textContent = 'Forget this one\u2026';
      el.appendChild(drop);
    }
  }
  el.addEventListener('change', function () {
    var value = el.value;
    if (value === '__add') {
      var url = (window.prompt('Address of the other Loom\n\ne.g. http://192.168.1.20:8765', 'http://') || '').trim();
      render();
      if (!url) { return; }
      url = url.replace(/\/$/, '');
      var name = (window.prompt('A name for it', label(url)) || label(url)).trim();
      var list = load();
      if (!list.some(function (s) { return s.url === url; })) { list.push({ name: name, url: url }); }
      save(list);
      render();
      return;
    }
    if (value === '__forget') {
      save(load().filter(function (s) { return s.url !== here(); }));
      render();
      return;
    }
    if (value && value !== here()) { window.location.href = value; return; }
    render();
  });
  render();
})();
