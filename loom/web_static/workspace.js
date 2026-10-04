/**
 * The workspace around the task in front, laid out like the desktop app:
 *
 * - the sidebar: which Loom this is, the All / Working / To review tiles, and
 *   one section per project with its tasks beneath (collapsible, draggable);
 * - the workspace overview, when no task is open;
 * - the task header's agent and status chips and its "more" menu;
 * - the dock on the right: the tasks asking for something, as pills that
 *   open them, folded to a strip of counts until you expand it;
 * - quick switch (⌘K), the menus, and the keyboard shortcuts.
 *
 * app.js owns the task surface itself and calls in here through the ws*
 * hooks. The running / finished rings are app.css's, unchanged: rows and
 * pills just carry .is-working / .is-finished.
 */

const WS = {
  stateFilter: 'all',
  // Projects start folded; the ones you open stay open.
  expanded: new Set(),
  dock: { expanded: false, width: 288, hidden: false },
  sidebarHidden: false,
  drag: null,
  justDragged: false,
  menuCleanup: null,
  quickItems: [],
  quickIndex: 0,
  filterSig: '',
  dockSig: '',
  refreshTimer: null,
};

const WS_KEYS = {
  expanded: 'loom.sidebar.expanded',
  sidebarHidden: 'loom.sidebar.hidden',
  dock: 'loom.dock',
  selection: 'loom.lastSelection',
};

function wsRead(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch (_) {
    return fallback;
  }
}

function wsWrite(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ }
}

function wsIcon(name, cls = 'ico') {
  return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

const WS_AGENT_NAMES = { claude: 'Claude', codex: 'Codex', cursor: 'Cursor' };
const WS_IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
const WS_QUICK_KEY = WS_IS_MAC ? '⌘K' : 'Ctrl K';

function wsAgentIcon(meta) {
  if (isArKind(meta && meta.kind)) return 'paper';
  return normalizeAgent(meta && meta.agent);
}

function wsAgentName(meta) {
  if (isArKind(meta && meta.kind)) return 'AR';
  return WS_AGENT_NAMES[normalizeAgent(meta && meta.agent)] || 'Cursor';
}

function wsProject(pid) {
  return (STATE.projects || []).find((p) => p.id === pid) || null;
}

function wsProjectLabel(pid) {
  const p = wsProject(pid);
  return p ? (p.name || p.id) : pid;
}

function wsTaskCount() {
  return Object.values(STATE.tasksByProject || {}).reduce((n, list) => n + (list ? list.length : 0), 0);
}

// ===== Task state from the activity snapshot =====
//
// finished — the agent stopped while you were elsewhere (the open task is
//            being looked at, so it never counts);
// working  — generating right now;
// idle     — a pane is alive and waiting;
// null     — no pane at all.

function wsTaskState(pid, slug) {
  const entry = ((STATE.activity && STATE.activity.tasks) || {})[`${pid}/${slug}`];
  if (!entry) return null;
  const open = pid === STATE.projectId && slug === STATE.slug;
  if (entry.finished_at && !open) return 'finished';
  if (entry.working) return 'working';
  return 'idle';
}

// Tasks asking for something: finished first, then working, by title.
function wsAttentionTasks() {
  const out = [];
  for (const p of STATE.projects || []) {
    for (const t of STATE.tasksByProject[p.id] || []) {
      const state = wsTaskState(p.id, t.slug);
      if (state === 'finished' || state === 'working') out.push({ pid: p.id, meta: t, state });
    }
  }
  out.sort((a, b) => {
    if (a.state !== b.state) return a.state === 'finished' ? -1 : 1;
    return String(a.meta.title || a.meta.slug).localeCompare(String(b.meta.title || b.meta.slug));
  });
  return out;
}

// ===== Loading =====

async function wsLoadAllTasks() {
  const queue = (STATE.projects || []).map((p) => p.id);
  // A handful at a time: forty projects should not mean forty requests in
  // flight against a server that is also streaming terminals.
  const workers = Array.from({ length: Math.min(6, queue.length) }, async () => {
    while (queue.length) {
      const pid = queue.shift();
      try {
        const d = await apiNoProject(`/api/tasks?project=${encodeURIComponent(pid)}`);
        STATE.tasksByProject[pid] = d.tasks || [];
      } catch (_) {
        // Keep whatever was listed before; a blip should not empty a project.
      }
    }
  });
  await Promise.all(workers);
  if (STATE.projectId) STATE.tasks = STATE.tasksByProject[STATE.projectId] || [];
  renderProjectTree();
}

async function wsReloadProjectTasks(pid) {
  if (!pid) return;
  try {
    const d = await apiNoProject(`/api/tasks?project=${encodeURIComponent(pid)}`);
    STATE.tasksByProject[pid] = d.tasks || [];
    if (pid === STATE.projectId) STATE.tasks = STATE.tasksByProject[pid];
    renderProjectTree();
  } catch (e) {
    toast(e.message, { type: 'error' });
  }
}

async function wsRefreshAll() {
  const btn = document.getElementById('btn-refresh');
  if (btn) btn.classList.add('is-spinning');
  try {
    await loadProjectsList();
    await wsLoadAllTasks();
    await pollActivity();
    if (STATE.slug) {
      refreshClaudeSessions();
      if (typeof chatPoke === 'function') chatPoke();
      if (STATE.activePanel === 'changes') refreshChangesView(true);
      if (STATE.activePanel === 'files' && typeof filesRefresh === 'function') filesRefresh();
    }
  } catch (e) {
    toast(e.message, { type: 'error' });
  } finally {
    if (btn) setTimeout(() => btn.classList.remove('is-spinning'), 300);
  }
}

// ===== Opening tasks =====

async function openTask(pid, slug) {
  if (!pid || !slug) return;
  if (isMobileViewport()) setSidebarOpen(false);
  if (pid !== STATE.projectId) {
    // Drafts are keyed by project, so the outgoing task's is saved before the
    // project changes under it.
    if (STATE.slug) {
      savePaneDraftForTask(STATE.slug);
      STATE.slug = null;
    }
    STATE.projectId = pid;
    STATE.tasks = STATE.tasksByProject[pid] || [];
    // Keeps the server's idea of the current project in step, as switching
    // projects always has; nothing waits on it.
    apiNoProject(`/api/projects/${encodeURIComponent(pid)}/activate`, { method: 'POST', body: '{}' })
      .catch(() => {});
    try {
      await loadProject();
    } catch (e) {
      toast(e.message, { type: 'error' });
    }
    if (!STATE.tasksByProject[pid]) await loadTasks();
  }
  await selectTask(slug);
}

function showOverview() {
  if (STATE.slug) {
    forgetSelectedTask();
    wsWrite(WS_KEYS.selection, null);
    clearTaskSelection();
  } else {
    renderOverview();
  }
  if (isMobileViewport()) setSidebarOpen(false);
}

async function wsRestoreSelection() {
  const last = wsRead(WS_KEYS.selection, null);
  if (last && last.projectId && last.slug
      && (STATE.tasksByProject[last.projectId] || []).some((t) => t.slug === last.slug)) {
    await openTask(last.projectId, last.slug);
    return;
  }
  let overviewLast = false;
  try { overviewLast = localStorage.getItem(WS_KEYS.selection) === 'null'; } catch (_) { /* private mode */ }
  if (last === null && overviewLast) {
    // The overview was the last thing open: stay there.
    renderOverview();
    wsUpdateBrand();
    return;
  }
  await restoreSelectedTaskForProject();
  if (!STATE.slug) {
    renderOverview();
    wsUpdateBrand();
  }
}

// app.js hooks: a task was opened / closed.
function wsTaskOpened(slug) {
  wsWrite(WS_KEYS.selection, { projectId: STATE.projectId, slug });
  closeMenu();
  wsToggleAgentPopover(false);
  wsSyncSelection(true);
}

function wsTaskClosed() {
  closeMenu();
  wsToggleAgentPopover(false);
  wsSyncSelection(false);
  renderOverview();
  wsUpdateBrand();
  wsApplyActivity();
}

// ===== Sidebar tree =====

function wsMatches(meta, pid, terms) {
  if (!terms.length) return true;
  const text = [
    wsProjectLabel(pid), meta.title, meta.slug, meta.general_goal, meta.agent, meta.kind,
  ].join(' ').toLowerCase();
  return terms.every((t) => text.includes(t));
}

function wsStateMatches(pid, slug) {
  if (WS.stateFilter === 'all') return true;
  return wsTaskState(pid, slug) === WS.stateFilter;
}

function wsFilteredTasks(pid, terms) {
  return (STATE.tasksByProject[pid] || []).filter((t) => wsMatches(t, pid, terms) && wsStateMatches(pid, t.slug));
}

function wsFilterSignature() {
  if (WS.stateFilter === 'all') return 'all';
  const keys = [];
  for (const p of STATE.projects || []) {
    for (const t of STATE.tasksByProject[p.id] || []) {
      if (wsStateMatches(p.id, t.slug)) keys.push(`${p.id}/${t.slug}`);
    }
  }
  return `${WS.stateFilter}:${keys.join(',')}`;
}

function renderProjectTree() {
  const tree = document.getElementById('project-tree');
  if (!tree) return;
  const terms = (STATE.taskFilter || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filtering = terms.length > 0 || WS.stateFilter !== 'all';
  const scrollTop = tree.scrollTop;
  WS.filterSig = wsFilterSignature();
  tree.innerHTML = '';
  const fragment = document.createDocumentFragment();
  let shown = 0;
  for (const p of STATE.projects || []) {
    const all = STATE.tasksByProject[p.id] || [];
    const tasks = wsFilteredTasks(p.id, terms);
    if (filtering && !tasks.length) continue;
    shown += 1;
    fragment.appendChild(wsProjectSection(p, tasks, all.length, filtering));
  }
  if (!shown) {
    const empty = document.createElement('div');
    empty.className = 'tree-empty';
    let message = 'No projects yet';
    if (!STATE.serverReachable) message = "Can't reach Loom";
    else if ((STATE.projects || []).length) {
      message = WS.stateFilter === 'finished' && !terms.length ? 'You’re all caught up' : 'No matching tasks';
    }
    empty.innerHTML = `${wsIcon(WS.stateFilter === 'finished' ? 'check-circle' : 'search', 'ico tree-empty__ico')}`
      + `<span>${escapeHtml(message)}</span>`;
    if ((STATE.projects || []).length) {
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'link-btn';
      link.textContent = 'Show all tasks';
      link.addEventListener('click', () => {
        const input = document.getElementById('task-filter');
        if (input) input.value = '';
        STATE.taskFilter = '';
        wsSetStateFilter('all');
      });
      empty.appendChild(link);
    } else {
      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'link-btn';
      add.textContent = 'Add a project';
      add.addEventListener('click', () => openAddProjectModal());
      empty.appendChild(add);
    }
    fragment.appendChild(empty);
  }
  tree.appendChild(fragment);
  tree.scrollTop = scrollTop;
  wsSyncSelection(false);
  wsApplyActivity();
}

function wsProjectSection(p, tasks, total, filtering) {
  const section = document.createElement('section');
  section.className = 'tree-project';
  section.dataset.projectId = p.id;
  const collapsed = !WS.expanded.has(p.id) && !filtering;
  section.classList.toggle('is-collapsed', collapsed);
  section.classList.toggle('has-selection', p.id === STATE.projectId && !!STATE.slug);

  const head = document.createElement('div');
  head.className = 'tree-project__head';
  head.draggable = true;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'tree-project__toggle';
  toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  toggle.title = p.path || p.name || p.id;
  toggle.innerHTML = `${wsIcon('chevron-right', 'ico ico--xs tree-project__chevron')}`
    + `${wsIcon('folder', 'ico ico--sm tree-project__folder')}`
    + `<span class="tree-project__name">${escapeHtml(p.name || p.id)}</span>`
    + `<span class="tree-project__counts">`
    + `<span class="tree-count tree-count--finished" data-count="finished" hidden></span>`
    + `<span class="tree-count tree-count--working" data-count="working" hidden></span>`
    + `<span class="tree-count tree-count--total">${total}</span>`
    + `</span>`;
  toggle.addEventListener('click', () => {
    if (WS.justDragged) return;
    if (filtering) return;
    if (WS.expanded.has(p.id)) WS.expanded.delete(p.id);
    else WS.expanded.add(p.id);
    wsWrite(WS_KEYS.expanded, [...WS.expanded]);
    renderProjectTree();
  });
  head.appendChild(toggle);
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'tree-project__more';
  more.title = 'Project actions';
  more.setAttribute('aria-label', `Actions for ${p.name || p.id}`);
  more.innerHTML = wsIcon('more', 'ico ico--sm');
  more.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openMenu(wsProjectMenuItems(p), more);
  });
  head.appendChild(more);
  head.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    openMenu(wsProjectMenuItems(p), { x: ev.clientX, y: ev.clientY });
  });
  wsBindProjectDrag(head, section, p.id);
  section.appendChild(head);

  if (!collapsed) {
    const ul = document.createElement('ul');
    ul.className = 'task-list';
    ul.setAttribute('role', 'list');
    for (const t of tasks) ul.appendChild(wsTaskRow(p.id, t));
    if (!tasks.length) {
      const li = document.createElement('li');
      li.className = 'task-list__empty';
      li.textContent = 'No tasks';
      ul.appendChild(li);
    }
    section.appendChild(ul);
  }
  return section;
}

function wsTaskRow(pid, t) {
  const li = document.createElement('li');
  li.className = 'task-row';
  li.dataset.project = pid;
  li.dataset.slug = t.slug;
  li.draggable = true;
  li.tabIndex = 0;
  li.setAttribute('role', 'button');
  li.title = t.title || t.slug;
  // Two lines of title, then the tooltip: a paper title running to four lines
  // would push the rest of the project off the screen.
  li.innerHTML =
    `<span class="task-row__agent" title="${escapeHtml(wsAgentName(t))}">${wsIcon(wsAgentIcon(t), 'ico')}</span>`
    + `<span class="task-row__title">${escapeHtml(t.title || t.slug)}</span>`;
  li.addEventListener('click', () => {
    if (WS.justDragged) return;
    if (pid === STATE.projectId && t.slug === STATE.slug) return;
    openTask(pid, t.slug);
  });
  li.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      li.click();
    }
  });
  li.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    openMenu(wsTaskMenuItems(pid, t), { x: ev.clientX, y: ev.clientY });
  });
  wsBindTaskDrag(li, pid, t.slug);
  return li;
}

function wsSyncSelection(scroll) {
  document.querySelectorAll('#project-tree .tree-project').forEach((section) => {
    section.classList.toggle('has-selection', section.dataset.projectId === STATE.projectId && !!STATE.slug);
  });
  let active = null;
  document.querySelectorAll('#project-tree li.task-row').forEach((li) => {
    const on = li.dataset.project === STATE.projectId && li.dataset.slug === STATE.slug;
    li.classList.toggle('active', on);
    if (on) active = li;
  });
  if (scroll && active) active.scrollIntoView({ block: 'nearest' });
}

function wsSetStateFilter(filter) {
  WS.stateFilter = filter;
  document.querySelectorAll('#filter-tiles .filter-tile').forEach((tile) => {
    const on = tile.dataset.filter === filter;
    tile.classList.toggle('is-active', on);
    tile.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  renderProjectTree();
}

// ----- drag and drop: tasks within their project, projects among projects -----

function wsClearDropMarkers() {
  document.querySelectorAll('#project-tree .is-drop-before, #project-tree .is-drop-after').forEach((el) => {
    el.classList.remove('is-drop-before', 'is-drop-after');
  });
}

function wsEndDrag() {
  WS.drag = null;
  wsClearDropMarkers();
  document.querySelectorAll('#project-tree .is-dragging').forEach((el) => el.classList.remove('is-dragging'));
  setTimeout(() => { WS.justDragged = false; }, 0);
}

function wsBindTaskDrag(li, pid, slug) {
  li.addEventListener('dragstart', (ev) => {
    WS.drag = { kind: 'task', pid, slug };
    WS.justDragged = true;
    li.classList.add('is-dragging');
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', `loom-task:${pid}/${slug}`);
  });
  li.addEventListener('dragover', (ev) => {
    const d = WS.drag;
    // Tasks do not move between projects, and one dropped on itself goes nowhere.
    if (!d || d.kind !== 'task' || d.pid !== pid || d.slug === slug) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer.dropEffect = 'move';
    const rect = li.getBoundingClientRect();
    const after = ev.clientY > rect.top + rect.height / 2;
    wsClearDropMarkers();
    li.classList.add(after ? 'is-drop-after' : 'is-drop-before');
  });
  li.addEventListener('drop', async (ev) => {
    const d = WS.drag;
    if (!d || d.kind !== 'task' || d.pid !== pid || d.slug === slug) return;
    ev.preventDefault();
    ev.stopPropagation();
    const after = li.classList.contains('is-drop-after');
    wsEndDrag();
    await wsReorderTask(pid, d.slug, slug, after);
  });
  li.addEventListener('dragend', wsEndDrag);
}

function wsBindProjectDrag(head, section, pid) {
  head.addEventListener('dragstart', (ev) => {
    if (ev.target.closest('.task-row')) return;
    WS.drag = { kind: 'project', pid };
    WS.justDragged = true;
    section.classList.add('is-dragging');
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', `loom-project:${pid}`);
  });
  head.addEventListener('dragend', wsEndDrag);
  // A project dropped anywhere on another project's section lands as if
  // dropped on its heading, not silently nowhere.
  section.addEventListener('dragover', (ev) => {
    const d = WS.drag;
    if (!d || d.kind !== 'project' || d.pid === pid) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    const ids = (STATE.projects || []).map((p) => p.id);
    const after = ids.indexOf(d.pid) < ids.indexOf(pid);
    wsClearDropMarkers();
    section.classList.add(after ? 'is-drop-after' : 'is-drop-before');
  });
  section.addEventListener('drop', async (ev) => {
    const d = WS.drag;
    if (!d || d.kind !== 'project' || d.pid === pid) return;
    ev.preventDefault();
    const after = section.classList.contains('is-drop-after');
    wsEndDrag();
    await reorderProjectsByDrag(d.pid, pid, after);
  });
}

async function wsReorderTask(pid, dragSlug, targetSlug, afterTarget) {
  const list = STATE.tasksByProject[pid] || [];
  const slugs = list.map((t) => t.slug);
  const from = slugs.indexOf(dragSlug);
  if (from < 0 || slugs.indexOf(targetSlug) < 0) return;
  slugs.splice(from, 1);
  slugs.splice(slugs.indexOf(targetSlug) + (afterTarget ? 1 : 0), 0, dragSlug);
  const bySlug = new Map(list.map((t) => [t.slug, t]));
  STATE.tasksByProject[pid] = slugs.map((s) => bySlug.get(s)).filter(Boolean);
  if (pid === STATE.projectId) STATE.tasks = STATE.tasksByProject[pid];
  renderProjectTree();
  try {
    const d = await apiNoProject(`/api/tasks/reorder?project=${encodeURIComponent(pid)}`, {
      method: 'POST',
      body: JSON.stringify({ slugs }),
    });
    if (d.tasks) {
      STATE.tasksByProject[pid] = d.tasks;
      if (pid === STATE.projectId) STATE.tasks = d.tasks;
      renderProjectTree();
    }
  } catch (e) {
    toast(e.message, { type: 'error' });
    await wsReloadProjectTasks(pid);
  }
}

// ===== Activity: rings, counts, overview, dock, the open task's chip =====

function wsApplyActivity() {
  if (WS.stateFilter !== 'all' && wsFilterSignature() !== WS.filterSig) {
    renderProjectTree();  // calls back here once the membership is redrawn
    return;
  }
  // Classes are toggled on the existing rows rather than re-rendered: a
  // re-render restarts every ring's animation, so a ring on a four-second
  // poll would stutter instead of spin.
  document.querySelectorAll('#project-tree li.task-row').forEach((li) => {
    const state = wsTaskState(li.dataset.project, li.dataset.slug);
    li.classList.toggle('is-finished', state === 'finished');
    li.classList.toggle('is-working', state === 'working');
  });
  let working = 0;
  let finished = 0;
  document.querySelectorAll('#project-tree .tree-project').forEach((section) => {
    const pid = section.dataset.projectId;
    let w = 0;
    let f = 0;
    for (const t of STATE.tasksByProject[pid] || []) {
      const state = wsTaskState(pid, t.slug);
      if (state === 'working') w += 1;
      else if (state === 'finished') f += 1;
    }
    const fEl = section.querySelector('[data-count="finished"]');
    const wEl = section.querySelector('[data-count="working"]');
    if (fEl) { fEl.hidden = !f; fEl.textContent = String(f); fEl.title = `${f} finished, not yet seen`; }
    if (wEl) { wEl.hidden = !w; wEl.textContent = String(w); wEl.title = `${w} working`; }
    // A folded project keeps its tasks' light on its heading.
    const head = section.querySelector('.tree-project__head');
    const folded = section.classList.contains('is-collapsed');
    if (head) {
      head.classList.toggle('is-finished', folded && f > 0);
      head.classList.toggle('is-working', folded && w > 0 && !f);
    }
  });
  for (const p of STATE.projects || []) {
    for (const t of STATE.tasksByProject[p.id] || []) {
      const state = wsTaskState(p.id, t.slug);
      if (state === 'working') working += 1;
      else if (state === 'finished') finished += 1;
    }
  }
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(value);
  };
  setText('filter-count-all', wsTaskCount());
  setText('filter-count-working', working);
  setText('filter-count-finished', finished);
  if (!STATE.slug) renderOverview();
  renderDock();
  wsUpdateStatusChip();
  wsSyncServerBar();
}

// ===== Overview =====

function renderOverview() {
  const sub = document.getElementById('overview-sub');
  const projects = STATE.projects || [];
  const count = wsTaskCount();
  if (sub) {
    const host = window.location.host || 'this Loom';
    sub.textContent = `${projects.length} ${projects.length === 1 ? 'project' : 'projects'} · `
      + `${count} ${count === 1 ? 'task' : 'tasks'} on ${host}`;
  }
  const attention = wsAttentionTasks();
  const f = attention.filter((a) => a.state === 'finished').length;
  const w = attention.length - f;
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(value);
  };
  setText('overview-count-finished', f);
  setText('overview-count-working', w);
  setText('btn-empty-create-label', projects.length ? 'New task' : 'Add project');
  const open = document.getElementById('btn-overview-open');
  if (open) open.disabled = !count;
  const host = document.getElementById('overview-focus');
  if (!host) return;
  const sig = JSON.stringify(attention.slice(0, 8).map((a) => [a.pid, a.meta.slug, a.state, a.meta.title]));
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.innerHTML = '';
  if (!attention.length) {
    host.innerHTML = '<div class="focus-quiet"><strong>Everything is quiet.</strong>'
      + '<span>Open a task to continue, or start something new. Finished work will appear here.</span></div>';
    return;
  }
  for (const a of attention.slice(0, 8)) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `focus-card focus-card--${a.state}`;
    card.innerHTML =
      `${wsIcon(a.state === 'finished' ? 'check-circle' : 'wave', 'ico focus-card__ico')}`
      + `<span class="focus-card__text"><span class="focus-card__title">${escapeHtml(a.meta.title || a.meta.slug)}</span>`
      + `<span class="focus-card__project">${escapeHtml(wsProjectLabel(a.pid))}</span></span>`
      + `<span class="focus-card__state">${a.state === 'finished' ? 'To review' : 'Working'}</span>`
      + `${wsIcon('arrow-up-right', 'ico ico--sm focus-card__go')}`;
    card.addEventListener('click', () => openTask(a.pid, a.meta.slug));
    host.appendChild(card);
  }
}

// ===== Dock =====

function wsApplyDockLayout() {
  const dock = document.getElementById('dock');
  if (!dock) return;
  const active = wsAttentionTasks().length > 0;
  // With nothing to show the dock folds back to its strip, as the desktop's
  // does; expanding is remembered for when there is.
  const expanded = WS.dock.expanded && active;
  dock.classList.toggle('is-expanded', expanded);
  dock.hidden = WS.dock.hidden;
  document.body.classList.toggle('dock-hidden', WS.dock.hidden);
  dock.style.setProperty('--dock-width', `${WS.dock.width}px`);
  const toggle = document.getElementById('btn-dock-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    toggle.disabled = !active;
    toggle.title = expanded ? 'Collapse to the status strip' : (active ? 'Show active tasks' : 'Nothing active');
    const use = toggle.querySelector('use');
    if (use) use.setAttribute('href', expanded ? '#i-chevron-right' : '#i-chevron-left');
  }
  const show = document.getElementById('btn-dock-show');
  if (show) show.setAttribute('aria-pressed', WS.dock.hidden ? 'false' : 'true');
}

function renderDock() {
  const attention = wsAttentionTasks();
  const f = attention.filter((a) => a.state === 'finished').length;
  const w = attention.length - f;
  const fEl = document.getElementById('dock-count-finished');
  const wEl = document.getElementById('dock-count-working');
  if (fEl) { fEl.hidden = !f; fEl.textContent = String(f); }
  if (wEl) { wEl.hidden = !w; wEl.textContent = String(w); }
  const summary = document.getElementById('dock-summary');
  if (summary) {
    const bits = [];
    if (f) bits.push(`${f} to review`);
    if (w) bits.push(`${w} working`);
    summary.textContent = bits.join(' · ');
  }
  const seen = document.getElementById('btn-dock-seen');
  if (seen) seen.hidden = !f;
  const empty = document.getElementById('dock-empty');
  if (empty) empty.hidden = attention.length > 0;
  wsApplyDockLayout();
  const host = document.getElementById('dock-pills');
  if (!host) return;
  // Rebuilt only when the set changes: rebuilding restarts every ring.
  const sig = JSON.stringify(attention.map((a) => [a.pid, a.meta.slug, a.state, a.meta.title, a.meta.agent]));
  if (WS.dockSig === sig) return;
  WS.dockSig = sig;
  host.innerHTML = '';
  const manyProjects = new Set(attention.map((a) => a.pid)).size > 1
    || (STATE.projects || []).length > 1;
  for (const a of attention) {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = `dock-pill is-${a.state}`;
    pill.setAttribute('role', 'listitem');
    pill.dataset.project = a.pid;
    pill.dataset.slug = a.meta.slug;
    pill.title = `${wsProjectLabel(a.pid)} / ${a.meta.slug} — ${a.state === 'finished' ? 'finished, not yet seen' : 'working'}`;
    pill.innerHTML =
      `${wsIcon(wsAgentIcon(a.meta), 'ico ico--sm dock-pill__agent')}`
      + `<span class="dock-pill__text">`
      + (manyProjects ? `<span class="dock-pill__project">${escapeHtml(wsProjectLabel(a.pid))}</span>` : '')
      + `<span class="dock-pill__title">${escapeHtml(a.meta.title || a.meta.slug)}</span>`
      + `</span>`;
    pill.addEventListener('click', () => openTask(a.pid, a.meta.slug));
    pill.addEventListener('contextmenu', (ev) => {
      ev.preventDefault();
      const items = [{ label: 'Open', action: () => openTask(a.pid, a.meta.slug) }];
      if (a.state === 'finished') {
        items.push({ label: 'Mark as Seen', action: () => ackActivity(a.meta.slug, a.pid) });
      }
      items.push({ label: 'Copy Slug', action: () => wsCopy(a.meta.slug) });
      openMenu(items, { x: ev.clientX, y: ev.clientY });
    });
    host.appendChild(pill);
  }
}

function wsSetDock(patch) {
  WS.dock = { ...WS.dock, ...patch };
  wsWrite(WS_KEYS.dock, WS.dock);
  wsApplyDockLayout();
}

function wsInitDockResize() {
  const handle = document.getElementById('dock-resize');
  const dock = document.getElementById('dock');
  if (!handle || !dock) return;
  handle.addEventListener('pointerdown', (ev) => {
    if (!dock.classList.contains('is-expanded')) return;
    ev.preventDefault();
    handle.setPointerCapture(ev.pointerId);
    document.body.classList.add('is-resizing');
    const move = (e) => {
      const width = Math.round(Math.min(560, Math.max(220, window.innerWidth - e.clientX)));
      WS.dock.width = width;
      dock.style.setProperty('--dock-width', `${width}px`);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      document.body.classList.remove('is-resizing');
      wsSetDock({ width: WS.dock.width });
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
}

// ===== Task header: agent chip, status chip, brand =====

function wsRenderTaskHeader(meta) {
  meta = meta || {};
  const project = document.getElementById('task-project-label');
  if (project) project.textContent = wsProjectLabel(STATE.projectId);
  // The slug earns its place only when it says something the title does not.
  const slugWrap = document.getElementById('task-slug-wrap');
  const slugEl = document.getElementById('task-slug-label');
  const title = String(meta.title || meta.slug || '');
  if (slugEl) slugEl.textContent = meta.slug || '';
  if (slugWrap) slugWrap.hidden = !meta.slug || title.toLowerCase() === String(meta.slug).toLowerCase();
  const name = document.getElementById('task-backend');
  if (name) name.textContent = wsAgentName(meta);
  const icon = document.getElementById('task-agent-icon');
  if (icon) icon.setAttribute('href', `#i-${wsAgentIcon(meta)}`);
  const chip = document.getElementById('btn-agent-chip');
  if (chip) {
    const model = meta.interview_model ? ` · ${meta.interview_model}` : '';
    chip.title = `${taskBackendLabel(meta)} — agent, model, skills and sessions`;
    chip.setAttribute('aria-label', `${wsAgentName(meta)}${model}: agent settings`);
  }
  wsUpdateStatusChip();
  wsUpdateBrand();
}

function wsUpdateStatusChip() {
  const chip = document.getElementById('task-status-chip');
  const label = document.getElementById('task-status-label');
  const start = document.getElementById('btn-interview-start');
  if (!chip || !STATE.slug) return;
  const meta = STATE.currentMeta || {};
  const entry = ((STATE.activity && STATE.activity.tasks) || {})[`${STATE.projectId}/${STATE.slug}`];
  const chat = typeof CHAT !== 'undefined' && CHAT.slug === STATE.slug && CHAT.loaded ? CHAT : null;
  const working = !!((entry && entry.working) || (chat && chat.working));
  let online;
  if (chat) online = chat.online;
  else if (STATE.paneAlive !== null && STATE.paneAlive !== undefined) online = STATE.paneAlive;
  else online = !!(entry || meta.tmux_interview_target);
  const state = working ? 'working' : (online ? 'ready' : 'offline');
  chip.dataset.state = state;
  if (label) label.textContent = state === 'working' ? 'Working' : (state === 'ready' ? 'Ready' : 'Offline');
  if (start) start.hidden = state !== 'offline';
}

function wsUpdateBrand() {
  const ctx = document.getElementById('brand-context');
  const count = wsTaskCount();
  if (STATE.slug && STATE.currentMeta) {
    const label = wsProjectLabel(STATE.projectId);
    if (ctx) ctx.textContent = label;
    document.title = `${STATE.currentMeta.title || STATE.slug} · ${label} — Loom`;
  } else {
    if (ctx) ctx.textContent = count ? `Workspace · ${count} ${count === 1 ? 'task' : 'tasks'}` : 'Workspace';
    document.title = 'Loom';
  }
}

function wsSyncServerBar() {
  const bar = document.getElementById('server-bar');
  if (!bar) return;
  const select = document.getElementById('server-switch');
  const option = select && select.selectedOptions && select.selectedOptions[0];
  const name = option ? option.textContent.replace(/^[●○]\s*/, '') : window.location.host;
  const nameEl = document.getElementById('server-name');
  if (nameEl && nameEl.textContent !== name) nameEl.textContent = name;
  const sub = document.getElementById('server-sub');
  const count = wsTaskCount();
  const online = STATE.serverReachable !== false;
  bar.dataset.state = online ? (STATE.projects ? 'online' : 'connecting') : 'offline';
  if (sub) {
    sub.textContent = online ? `${count} ${count === 1 ? 'task' : 'tasks'}` : "Can't reach this Loom";
  }
  bar.title = window.location.origin;
}

// ===== Agent settings popover (the old info card) =====

function wsToggleAgentPopover(force) {
  const pop = document.getElementById('agent-popover');
  const chip = document.getElementById('btn-agent-chip');
  if (!pop || !chip) return;
  const open = typeof force === 'boolean' ? force : pop.hidden;
  if (open === !pop.hidden) return;
  pop.hidden = !open;
  chip.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (!open) return;
  const view = document.getElementById('task-view');
  const chipRect = chip.getBoundingClientRect();
  const viewRect = view.getBoundingClientRect();
  pop.style.top = `${Math.round(chipRect.bottom - viewRect.top + 8)}px`;
  pop.style.right = `${Math.max(12, Math.round(viewRect.right - chipRect.right))}px`;
  refreshClaudeSessions();
  loadMonitor();
}

// ===== Menus =====

function closeMenu() {
  const menu = document.getElementById('menu');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  menu.innerHTML = '';
  if (WS.menuCleanup) WS.menuCleanup();
  WS.menuCleanup = null;
}

// items: [{ label, action, danger, checked, disabled, hint }] or { separator }
// or { heading }; anchor: an element (menu opens under it) or { x, y }.
function openMenu(items, anchor) {
  const menu = document.getElementById('menu');
  if (!menu) return;
  closeMenu();
  menu.innerHTML = '';
  for (const item of items) {
    if (item.separator) {
      const sep = document.createElement('div');
      sep.className = 'menu__sep';
      menu.appendChild(sep);
      continue;
    }
    if (item.heading) {
      const h = document.createElement('div');
      h.className = 'menu__heading';
      h.textContent = item.heading;
      menu.appendChild(h);
      continue;
    }
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'menu__item' + (item.danger ? ' menu__item--danger' : '');
    b.setAttribute('role', typeof item.checked === 'boolean' ? 'menuitemcheckbox' : 'menuitem');
    if (typeof item.checked === 'boolean') b.setAttribute('aria-checked', item.checked ? 'true' : 'false');
    b.disabled = !!item.disabled;
    b.innerHTML = `<span class="menu__check">${item.checked ? wsIcon('check-circle', 'ico ico--xs') : ''}</span>`
      + `<span class="menu__label">${escapeHtml(item.label)}</span>`
      + (item.hint ? `<span class="menu__hint">${escapeHtml(item.hint)}</span>` : '');
    b.addEventListener('click', () => {
      closeMenu();
      try {
        const r = item.action && item.action();
        if (r && typeof r.catch === 'function') r.catch((e) => toast(e.message, { type: 'error' }));
      } catch (e) {
        toast(e.message, { type: 'error' });
      }
    });
    menu.appendChild(b);
  }
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  let x;
  let y;
  if (anchor && typeof anchor.getBoundingClientRect === 'function') {
    const a = anchor.getBoundingClientRect();
    x = a.right - rect.width;
    y = a.bottom + 6;
    if (x < 8) x = a.left;
    anchor.setAttribute('aria-expanded', 'true');
  } else {
    x = (anchor && anchor.x) || 0;
    y = (anchor && anchor.y) || 0;
  }
  x = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
  if (y + rect.height > window.innerHeight - 8) y = Math.max(8, y - rect.height - 12);
  menu.style.left = `${Math.round(x)}px`;
  menu.style.top = `${Math.round(y)}px`;
  const first = menu.querySelector('.menu__item:not(:disabled)');
  if (first) first.focus({ preventScroll: true });
  const onDown = (ev) => { if (!menu.contains(ev.target)) closeMenu(); };
  const onKey = (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const items = [...menu.querySelectorAll('.menu__item:not(:disabled)')];
      const i = items.indexOf(document.activeElement);
      const next = ev.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
      if (items[next]) items[next].focus();
    }
  };
  const onScroll = (ev) => { if (!menu.contains(ev.target)) closeMenu(); };
  setTimeout(() => document.addEventListener('mousedown', onDown, true), 0);
  menu.addEventListener('keydown', onKey);
  window.addEventListener('resize', closeMenu);
  document.addEventListener('scroll', onScroll, true);
  WS.menuCleanup = () => {
    document.removeEventListener('mousedown', onDown, true);
    menu.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', closeMenu);
    document.removeEventListener('scroll', onScroll, true);
    if (anchor && typeof anchor.setAttribute === 'function') anchor.setAttribute('aria-expanded', 'false');
  };
}

async function wsCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied', { type: 'success', ttl: 1500 });
  } catch (_) {
    window.prompt('Copy:', text);
  }
}

function wsProjectMenuItems(p) {
  return [
    { label: 'New Task Here…', action: () => openCreateModal(p.id) },
    { label: 'Project Notes…', action: () => openNotesModal(p.id) },
    { label: 'Set Code Root…', action: () => openCodeRootModal(p.id) },
    { label: 'Copy Path', action: () => wsCopy(p.path || '') },
    { separator: true },
    { label: 'Remove from Loom…', danger: true, action: () => removeProject(p.id) },
  ];
}

function wsTaskMenuItems(pid, t) {
  return [
    { label: 'Open Task', action: () => openTask(pid, t.slug) },
    { label: 'Rename…', action: () => wsRenameTask(pid, t) },
    { label: 'Copy Slug', action: () => wsCopy(t.slug) },
    { separator: true },
    { label: 'Delete Task…', danger: true, action: () => wsDeleteTask(pid, t) },
  ];
}

// Only the title changes; the slug and the task's directory keep their names,
// so nothing running is disturbed.
async function wsRenameTask(pid, t) {
  const current = t.title || t.slug;
  const next = (window.prompt('Rename task', current) || '').trim();
  if (!next || next === current) return;
  if (pid === STATE.projectId && t.slug === STATE.slug) {
    await saveTaskMeta({ title: next });
    const titleEl = document.getElementById('task-title');
    if (titleEl) titleEl.textContent = next;
    wsUpdateBrand();
    return;
  }
  const r = await apiNoProject(`/api/tasks/${encodeURIComponent(t.slug)}/meta?project=${encodeURIComponent(pid)}`, {
    method: 'PUT',
    body: JSON.stringify({ title: next }),
  });
  if (r.meta) {
    STATE.tasksByProject[pid] = (STATE.tasksByProject[pid] || []).map((x) => (x.slug === t.slug ? r.meta : x));
    renderProjectTree();
  }
}

async function wsDeleteTask(pid, t) {
  if (pid === STATE.projectId && t.slug === STATE.slug) {
    await deleteSelectedTask();
    return;
  }
  const title = t.title || t.slug;
  const ok = confirm(
    `Delete task "${title}" (${t.slug})?\n\n`
    + `The task's folder is deleted - .RUD/${t.slug}/, its plan, notes and worktree checkout. `
    + 'A running agent is stopped first. Branches and commits stay in the repository they came from; '
    + 'push first if they only exist here.'
  );
  if (!ok) return;
  const scope = `project=${encodeURIComponent(pid)}`;
  if (t.tmux_interview_target) {
    await apiNoProject(`/api/tasks/${encodeURIComponent(t.slug)}/claude/stop?${scope}`, { method: 'POST', body: '{}' })
      .catch(() => {});
  }
  await apiNoProject(`/api/tasks/${encodeURIComponent(t.slug)}?${scope}`, { method: 'DELETE' });
  await wsReloadProjectTasks(pid);
}

function wsTaskHeaderMenuItems() {
  const meta = STATE.currentMeta || {};
  const chip = document.getElementById('task-status-chip');
  const online = chip && chip.dataset.state !== 'offline';
  const label = agentLabel(meta.agent);
  const monitor = document.getElementById('monitor-toggle');
  const items = [];
  if (online) items.push({ label: `Stop ${label}`, action: () => stopClaudePane() });
  else items.push({ label: `Start ${label}`, action: () => startInterviewPane() });
  items.push({ label: 'Agent settings…', action: () => wsToggleAgentPopover(true) });
  items.push({ separator: true });
  items.push({
    label: 'Notify when finished',
    checked: !!(monitor && monitor.checked),
    disabled: !!STATE.monitorBusy,
    action: () => setMonitor(!(monitor && monitor.checked)),
  });
  const sessions = ((STATE.claudeInfo && STATE.claudeInfo.sessions) || []).filter((s) => s.id).slice(0, 10);
  items.push({ separator: true });
  if (!sessions.length) {
    items.push({ label: 'No past sessions', disabled: true });
  } else {
    items.push({ heading: 'Resume session' });
    for (const s of sessions) {
      const when = s.mtime ? formatSessionMtime(s.mtime) : '';
      items.push({
        label: when ? `${when} · ${shortSessionId(s.id)}` : shortSessionId(s.id),
        disabled: STATE.claudeInfo && STATE.claudeInfo.agent_running === true,
        action: () => resumeClaudeSession(s.id),
      });
    }
  }
  items.push({ separator: true });
  items.push({ label: 'Rename…', action: () => wsRenameTask(STATE.projectId, meta.slug ? meta : { slug: STATE.slug, title: '' }) });
  items.push({ label: 'Copy Slug', action: () => wsCopy(STATE.slug) });
  items.push({ separator: true });
  items.push({ label: 'Delete Task…', danger: true, action: () => deleteSelectedTask() });
  return items;
}

// ===== Quick switch (⌘K) =====

function openQuickOpen() {
  const modal = document.getElementById('quick-open');
  const input = document.getElementById('quick-open-input');
  if (!modal || !input) return;
  closeMenu();
  modal.hidden = false;
  input.value = '';
  WS.quickIndex = 0;
  renderQuickOpen();
  requestAnimationFrame(() => input.focus());
}

function closeQuickOpen() {
  const modal = document.getElementById('quick-open');
  if (modal) modal.hidden = true;
}

function renderQuickOpen() {
  const input = document.getElementById('quick-open-input');
  const list = document.getElementById('quick-open-list');
  if (!input || !list) return;
  const terms = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rank = { finished: 0, working: 1, idle: 2 };
  const items = [];
  for (const p of STATE.projects || []) {
    for (const t of STATE.tasksByProject[p.id] || []) {
      if (!wsMatches(t, p.id, terms)) continue;
      items.push({ pid: p.id, meta: t, state: wsTaskState(p.id, t.slug) });
    }
  }
  items.sort((a, b) => (rank[a.state] ?? 3) - (rank[b.state] ?? 3));
  WS.quickItems = items.slice(0, 60);
  if (WS.quickIndex >= WS.quickItems.length) WS.quickIndex = Math.max(0, WS.quickItems.length - 1);
  list.innerHTML = '';
  if (!WS.quickItems.length) {
    list.innerHTML = '<li class="quick-open__empty">No matching tasks</li>';
    return;
  }
  WS.quickItems.forEach((item, i) => {
    const li = document.createElement('li');
    li.className = 'quick-open__item' + (i === WS.quickIndex ? ' is-active' : '');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', i === WS.quickIndex ? 'true' : 'false');
    const state = item.state === 'finished' ? 'To review' : (item.state === 'working' ? 'Working' : '');
    li.innerHTML =
      `${wsIcon(wsAgentIcon(item.meta), 'ico ico--sm quick-open__agent')}`
      + `<span class="quick-open__text"><span class="quick-open__title">${escapeHtml(item.meta.title || item.meta.slug)}</span>`
      + `<span class="quick-open__project">${escapeHtml(wsProjectLabel(item.pid))}</span></span>`
      + (state ? `<span class="quick-open__state quick-open__state--${item.state}">${state}</span>` : '');
    li.addEventListener('mousemove', () => {
      if (WS.quickIndex === i) return;
      WS.quickIndex = i;
      list.querySelectorAll('.quick-open__item').forEach((el, j) => {
        el.classList.toggle('is-active', j === i);
        el.setAttribute('aria-selected', j === i ? 'true' : 'false');
      });
    });
    li.addEventListener('click', () => {
      closeQuickOpen();
      openTask(item.pid, item.meta.slug);
    });
    list.appendChild(li);
  });
  const active = list.querySelector('.is-active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function wsInitQuickOpen() {
  const modal = document.getElementById('quick-open');
  const input = document.getElementById('quick-open-input');
  if (!modal || !input) return;
  input.addEventListener('input', () => {
    WS.quickIndex = 0;
    renderQuickOpen();
  });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const n = WS.quickItems.length;
      if (!n) return;
      WS.quickIndex = ev.key === 'ArrowDown' ? (WS.quickIndex + 1) % n : (WS.quickIndex - 1 + n) % n;
      renderQuickOpen();
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      const item = WS.quickItems[WS.quickIndex];
      if (item) {
        closeQuickOpen();
        openTask(item.pid, item.meta.slug);
      }
    }
  });
  modal.addEventListener('mousedown', (ev) => {
    if (ev.target === modal) closeQuickOpen();
  });
}

// ===== Escape and shortcuts =====

// Called first by app.js's Escape handler; true when something here closed.
function wsHandleEscape() {
  const menu = document.getElementById('menu');
  if (menu && !menu.hidden) { closeMenu(); return true; }
  const quick = document.getElementById('quick-open');
  if (quick && !quick.hidden) { closeQuickOpen(); return true; }
  const pop = document.getElementById('agent-popover');
  if (pop && !pop.hidden) { wsToggleAgentPopover(false); return true; }
  return false;
}

function wsToggleSidebar() {
  if (isMobileViewport()) {
    toggleSidebar();
    return;
  }
  WS.sidebarHidden = !WS.sidebarHidden;
  wsWrite(WS_KEYS.sidebarHidden, WS.sidebarHidden);
  document.body.classList.toggle('sidebar-hidden', WS.sidebarHidden);
  const btn = document.getElementById('btn-sidebar-collapse');
  if (btn) btn.setAttribute('aria-pressed', WS.sidebarHidden ? 'true' : 'false');
  if (STATE.activePanel === 'claude') termHandleResize();
}

function wsInitShortcuts() {
  document.addEventListener('keydown', (ev) => {
    const target = ev.target;
    const inTerminal = !!(target && target.closest && target.closest('.xterm'));
    const key = (ev.key || '').toLowerCase();
    // ⌘K on a Mac, Ctrl+K elsewhere - anywhere but the terminal, where
    // Ctrl+K is the shell's kill-line.
    const mod = WS_IS_MAC ? (ev.metaKey && !ev.ctrlKey) : (ev.ctrlKey && !ev.metaKey);
    if (key === 'k' && mod && !ev.altKey && !ev.shiftKey && !inTerminal) {
      ev.preventDefault();
      openQuickOpen();
      return;
    }
    if (!ev.altKey || ev.metaKey || ev.ctrlKey || inTerminal) return;
    const code = ev.code || '';
    if (/^Digit[1-9]$/.test(code) && STATE.slug) {
      const tabs = tabsFor(STATE.currentMeta || {});
      const tab = tabs[Number(code.slice(5)) - 1];
      if (tab) {
        ev.preventDefault();
        showPanel(tab.id);
      }
    } else if (code === 'KeyN') {
      ev.preventDefault();
      openCreateModal();
    } else if (code === 'KeyH') {
      ev.preventDefault();
      showOverview();
    } else if (code === 'KeyS') {
      ev.preventDefault();
      wsToggleSidebar();
    }
  });
}

// ===== Wire-up =====

function wsInit() {
  if (!WS_IS_MAC) {
    document.querySelectorAll('#btn-go-quick kbd, .overview__hint').forEach((el) => {
      el.textContent = el.textContent.replace('⌘K', WS_QUICK_KEY);
    });
    const quick = document.getElementById('btn-quick-open');
    if (quick) quick.title = `Quick switch tasks (${WS_QUICK_KEY})`;
  }
  WS.expanded = new Set(wsRead(WS_KEYS.expanded, []));
  WS.sidebarHidden = !!wsRead(WS_KEYS.sidebarHidden, false);
  WS.dock = { ...WS.dock, ...(wsRead(WS_KEYS.dock, {}) || {}) };
  document.body.classList.toggle('sidebar-hidden', WS.sidebarHidden);
  const collapseBtn = document.getElementById('btn-sidebar-collapse');
  if (collapseBtn) {
    collapseBtn.setAttribute('aria-pressed', WS.sidebarHidden ? 'true' : 'false');
    collapseBtn.addEventListener('click', wsToggleSidebar);
  }

  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', fn);
  };
  on('btn-brand', showOverview);
  on('btn-go-overview', showOverview);
  on('btn-quick-open', openQuickOpen);
  on('btn-go-quick', openQuickOpen);
  on('btn-overview-open', openQuickOpen);
  on('btn-go-new', () => openCreateModal());
  on('btn-go-notes', () => openNotesModal());
  on('btn-refresh', wsRefreshAll);
  on('btn-new-menu', (ev) => {
    openMenu([
      { label: 'New Task…', hint: 'Alt N', action: () => openCreateModal() },
      { label: 'Add Project…', action: () => openAddProjectModal() },
    ], ev.currentTarget);
  });
  on('btn-task-menu', (ev) => openMenu(wsTaskHeaderMenuItems(), ev.currentTarget));
  on('btn-agent-chip', (ev) => {
    ev.stopPropagation();
    wsToggleAgentPopover();
  });
  document.addEventListener('mousedown', (ev) => {
    const pop = document.getElementById('agent-popover');
    if (!pop || pop.hidden) return;
    if (pop.contains(ev.target) || ev.target.closest('#btn-agent-chip')) return;
    wsToggleAgentPopover(false);
  });

  document.querySelectorAll('#filter-tiles .filter-tile').forEach((tile) => {
    tile.addEventListener('click', () => wsSetStateFilter(tile.dataset.filter));
  });
  document.querySelectorAll('.overview .summary-card').forEach((card) => {
    card.addEventListener('click', () => {
      if (WS.sidebarHidden) wsToggleSidebar();
      if (isMobileViewport()) setSidebarOpen(true);
      wsSetStateFilter(card.dataset.filter);
    });
  });

  on('btn-dock-toggle', () => wsSetDock({ expanded: !WS.dock.expanded }));
  on('btn-dock-show', () => wsSetDock({ hidden: !WS.dock.hidden }));
  on('btn-dock-seen', () => {
    for (const a of wsAttentionTasks()) {
      if (a.state === 'finished') ackActivity(a.meta.slug, a.pid);
    }
  });
  wsInitDockResize();
  wsApplyDockLayout();

  const server = document.getElementById('server-switch');
  if (server) server.addEventListener('change', () => setTimeout(wsSyncServerBar, 0));
  wsSyncServerBar();

  wsInitQuickOpen();
  wsInitShortcuts();

  // Titles, new tasks and other clients' changes arrive on a slower cycle
  // than the activity rings.
  WS.refreshTimer = setInterval(() => {
    if (!document.hidden && STATE.serverReachable !== false) wsLoadAllTasks().catch(() => {});
  }, 30000);
}
