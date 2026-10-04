/**
 * The Files tab: the task directory as a small editor, the way the desktop
 * app shows it — a folder tree on the left (the worktree is under work/), the
 * selected file on the right, opening on PLAN.md.
 *
 * The tree is read one directory at a time through /api/tasks/<slug>/files,
 * so a task holding a large worktree costs a single listing until a folder is
 * opened. Markdown reads rendered or as source; PLAN.md and WIKI.md save back
 * through /template; everything else is read-only.
 */

const FILETAB = {
  pid: null,
  slug: null,
  children: {},
  expanded: new Set(),
  loading: new Set(),
  selected: '',
  file: null,
  editing: false,
  error: '',
};

const FILETAB_WRITABLE = new Set(['PLAN.md', 'WIKI.md']);
const FILETAB_MODE_KEY = 'loom.filesMode';
const FILETAB_IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i;
const FILETAB_MARKDOWN = /\.(md|markdown|mdx)$/i;

function filesScope() {
  return `project=${encodeURIComponent(FILETAB.pid)}`;
}

function filesMode() {
  try { return localStorage.getItem(FILETAB_MODE_KEY) === 'source' ? 'source' : 'read'; } catch (_) { return 'read'; }
}

function filesSetMode(mode) {
  try { localStorage.setItem(FILETAB_MODE_KEY, mode); } catch (_) { /* private mode */ }
  filesRenderView();
}

// ===== Lifecycle (called from app.js / workspace.js) =====

function filesShow() {
  if (!STATE.slug || !STATE.projectId) return;
  if (FILETAB.slug !== STATE.slug || FILETAB.pid !== STATE.projectId) {
    filesReset();
    FILETAB.pid = STATE.projectId;
    FILETAB.slug = STATE.slug;
    const root = document.getElementById('files-root-label');
    if (root) root.textContent = STATE.slug;
    filesLoadRoot();
  } else if (!FILETAB.editing) {
    filesRefresh();
  }
}

function filesReset() {
  if (FILETAB.editing && filesDirty()) {
    // Leaving a task with unsaved PLAN.md edits: say so rather than drop them.
    toast('Unsaved edits in the Files tab were discarded.', { type: 'error' });
  }
  Object.assign(FILETAB, {
    pid: null,
    slug: null,
    children: {},
    selected: '',
    file: null,
    editing: false,
    error: '',
  });
  FILETAB.expanded = new Set();
  FILETAB.loading = new Set();
  const tree = document.getElementById('files-tree');
  if (tree) tree.innerHTML = '';
  filesRenderView();
}

async function filesRefresh() {
  if (!FILETAB.slug) return;
  const dirs = ['', ...[...FILETAB.expanded]];
  await Promise.all(dirs.map((d) => filesLoadDir(d, true)));
  filesRenderTree();
  if (FILETAB.selected && !FILETAB.editing) await filesOpen(FILETAB.selected, { quiet: true });
}

// ===== Tree =====

async function filesLoadDir(path, quiet = false) {
  const slug = FILETAB.slug;
  FILETAB.loading.add(path);
  if (!quiet) filesRenderTree();
  try {
    const query = path ? `&path=${encodeURIComponent(path)}` : '';
    const d = await apiNoProject(`/api/tasks/${encodeURIComponent(slug)}/files?${filesScope()}${query}`);
    if (FILETAB.slug !== slug) return;
    FILETAB.children[path] = Array.isArray(d.entries) ? d.entries : [];
    FILETAB.error = '';
  } catch (err) {
    if (FILETAB.slug !== slug) return;
    if (!path) FILETAB.error = (err && err.message) || 'Files unavailable';
    else toast((err && err.message) || `Could not open ${path}`, { type: 'error' });
  } finally {
    FILETAB.loading.delete(path);
  }
}

async function filesLoadRoot() {
  await filesLoadDir('');
  filesRenderTree();
  const root = FILETAB.children[''] || [];
  const plan = root.find((e) => !e.dir && e.name === 'PLAN.md');
  const firstMd = root.find((e) => !e.dir && FILETAB_MARKDOWN.test(e.name));
  const pick = plan || firstMd;
  if (pick) await filesOpen(pick.name);
  else filesRenderView();
}

function filesRows() {
  const out = [];
  const walk = (dir, depth) => {
    for (const entry of FILETAB.children[dir] || []) {
      const path = dir ? `${dir}/${entry.name}` : entry.name;
      out.push({ path, name: entry.name, dir: !!entry.dir, depth, size: entry.size || 0 });
      if (entry.dir && FILETAB.expanded.has(path)) walk(path, depth + 1);
    }
  };
  walk('', 0);
  return out;
}

function filesRenderTree() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  if (!FILETAB.slug) {
    tree.innerHTML = '';
    return;
  }
  const rows = filesRows();
  if (!rows.length) {
    let text = 'Loading files…';
    if (!FILETAB.loading.has('')) text = FILETAB.error || 'Nothing here yet';
    tree.innerHTML = `<li class="files__empty">${escapeHtml(text)}</li>`;
    return;
  }
  tree.innerHTML = rows.map((r) => {
    const open = r.dir && FILETAB.expanded.has(r.path);
    const cls = ['files__row', r.dir ? 'is-dir' : 'is-file'];
    if (r.path === FILETAB.selected) cls.push('is-selected');
    if (open) cls.push('is-open');
    const twisty = r.dir
      ? `<svg class="ico ico--xs files__twisty" aria-hidden="true"><use href="#i-chevron-right"/></svg>`
      : '<span class="files__twisty" aria-hidden="true"></span>';
    const icon = r.dir ? 'folder' : 'file';
    const busy = r.dir && FILETAB.loading.has(r.path) ? '<span class="spinner-dot" aria-hidden="true"></span>' : '';
    return `<li class="${cls.join(' ')}" role="treeitem" tabindex="0" style="--depth:${r.depth}"`
      + ` data-path="${escapeHtml(r.path)}" data-dir="${r.dir ? '1' : ''}"`
      + `${r.dir ? ` aria-expanded="${open ? 'true' : 'false'}"` : ''} title="${escapeHtml(r.path)}">`
      + `${twisty}<svg class="ico ico--sm files__icon" aria-hidden="true"><use href="#i-${icon}"/></svg>`
      + `<span class="files__name">${escapeHtml(r.name)}</span>${busy}</li>`;
  }).join('');
}

async function filesToggleDir(path) {
  if (FILETAB.expanded.has(path)) {
    FILETAB.expanded.delete(path);
    filesRenderTree();
    return;
  }
  FILETAB.expanded.add(path);
  if (!FILETAB.children[path]) await filesLoadDir(path);
  filesRenderTree();
}

// ===== Viewer =====

function filesDirty() {
  const editor = document.getElementById('files-editor');
  return !!(FILETAB.editing && editor && FILETAB.file && editor.value !== (FILETAB.file.body || ''));
}

async function filesOpen(path, opts = {}) {
  if (!FILETAB.slug) return;
  if (FILETAB.editing && path !== FILETAB.selected && filesDirty()
      && !confirm(`Discard your unsaved edits to ${FILETAB.selected}?`)) return;
  const slug = FILETAB.slug;
  if (path !== FILETAB.selected) FILETAB.editing = false;
  FILETAB.selected = path;
  filesRenderTree();
  if (FILETAB_IMAGE.test(path) && !/\.svg$/i.test(path)) {
    FILETAB.file = { path, body: '', image: true };
    filesRenderView();
    return;
  }
  if (!opts.quiet) {
    FILETAB.file = { path, body: '', loading: true };
    filesRenderView();
  }
  try {
    const d = await apiNoProject(
      `/api/tasks/${encodeURIComponent(slug)}/files?${filesScope()}&path=${encodeURIComponent(path)}`,
    );
    if (FILETAB.slug !== slug || FILETAB.selected !== path) return;
    if (opts.quiet && FILETAB.file && FILETAB.file.body === d.body && !FILETAB.file.loading) return;
    FILETAB.file = { path, body: d.body || '', error: d.error || '', size: d.size || 0 };
  } catch (err) {
    if (FILETAB.slug !== slug) return;
    FILETAB.file = { path, body: '', error: (err && err.message) || 'unreadable' };
  }
  if (!FILETAB.editing) filesRenderView();
}

function filesAssetUrl(path) {
  const name = path.split('/').pop();
  return markdownAssetUrl(name, path, FILETAB.slug);
}

function filesRenderView() {
  const pathEl = document.getElementById('files-path');
  const preview = document.getElementById('files-preview');
  const source = document.getElementById('files-source');
  const editor = document.getElementById('files-editor');
  const status = document.getElementById('files-status');
  const modeEl = document.getElementById('files-mode');
  const editBtn = document.getElementById('files-edit');
  const saveBtn = document.getElementById('files-save');
  const cancelBtn = document.getElementById('files-cancel');
  if (!preview || !source || !editor) return;
  const file = FILETAB.file;
  const path = file ? file.path : '';
  if (pathEl) pathEl.textContent = path || (FILETAB.slug ? 'Pick a file' : '');
  const markdown = FILETAB_MARKDOWN.test(path);
  const writable = FILETAB_WRITABLE.has(path);
  const mode = filesMode();
  if (modeEl) {
    modeEl.hidden = !markdown || FILETAB.editing;
    modeEl.querySelectorAll('.seg__btn').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === mode));
  }
  if (editBtn) editBtn.hidden = !writable || FILETAB.editing || !file || !!file.error || !!file.loading;
  if (saveBtn) saveBtn.hidden = !FILETAB.editing;
  if (cancelBtn) cancelBtn.hidden = !FILETAB.editing;
  preview.hidden = true;
  source.hidden = true;
  editor.hidden = true;
  let note = '';
  if (!file) {
    note = FILETAB.slug ? (FILETAB.error || 'Run the deep interview and the agent will write PLAN.md.') : '';
  } else if (file.loading) {
    note = 'Loading…';
  } else if (file.image) {
    preview.hidden = false;
    preview.innerHTML = `<p><img class="md-img" src="${escapeHtml(filesAssetUrl(path))}" alt="${escapeHtml(path)}"></p>`;
  } else if (file.error) {
    note = {
      binary: 'A binary file — nothing to show here.',
      'too large': `Too large to show here (${Math.round((file.size || 0) / 1024)} KB).`,
      unreadable: 'This file could not be read.',
    }[file.error] || file.error;
  } else if (FILETAB.editing) {
    editor.hidden = false;
  } else if (markdown && mode === 'read') {
    preview.hidden = false;
    preview.innerHTML = renderMarkdownWithAssets(file.body, (rel) => markdownAssetUrl(rel, path, FILETAB.slug));
    typesetMath(preview);
  } else {
    source.hidden = false;
    source.textContent = file.body;
  }
  if (status) {
    status.hidden = !note;
    status.textContent = note;
  }
}

function filesStartEdit() {
  const file = FILETAB.file;
  if (!file || !FILETAB_WRITABLE.has(file.path)) return;
  const editor = document.getElementById('files-editor');
  FILETAB.editing = true;
  if (editor) editor.value = file.body || '';
  filesRenderView();
  if (editor) editor.focus();
}

function filesCancelEdit() {
  if (filesDirty() && !confirm('Discard your edits?')) return;
  FILETAB.editing = false;
  filesRenderView();
}

async function filesSave() {
  const file = FILETAB.file;
  const editor = document.getElementById('files-editor');
  const saveBtn = document.getElementById('files-save');
  if (!file || !editor || !FILETAB.editing) return;
  if (saveBtn) saveBtn.disabled = true;
  try {
    await apiNoProject(`/api/tasks/${encodeURIComponent(FILETAB.slug)}/template?${filesScope()}`, {
      method: 'PUT',
      body: JSON.stringify({ name: file.path, content: editor.value }),
    });
    file.body = editor.value;
    FILETAB.editing = false;
    filesRenderView();
    toast(`Saved ${file.path}`, { type: 'success', ttl: 1800 });
    // The terminal tab's PLAN.md viewer reads the same file.
    if (typeof refreshTaskTemplates === 'function') refreshTaskTemplates();
  } catch (err) {
    toast((err && err.message) || 'Save failed', { type: 'error' });
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

// ===== Wire-up =====

(function initFiles() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  const activate = (row) => {
    if (!row) return;
    if (row.dataset.dir) filesToggleDir(row.dataset.path);
    else filesOpen(row.dataset.path);
  };
  tree.addEventListener('click', (ev) => activate(ev.target.closest('.files__row')));
  tree.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    activate(ev.target.closest('.files__row'));
  });
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', fn);
  };
  on('files-refresh', () => filesRefresh());
  on('files-edit', filesStartEdit);
  on('files-cancel', filesCancelEdit);
  on('files-save', filesSave);
  const mode = document.getElementById('files-mode');
  if (mode) {
    mode.addEventListener('click', (ev) => {
      const b = ev.target.closest('.seg__btn');
      if (b) filesSetMode(b.dataset.mode);
    });
  }
  const editor = document.getElementById('files-editor');
  if (editor) {
    editor.addEventListener('keydown', (ev) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's') {
        ev.preventDefault();
        filesSave();
      }
    });
  }
})();
