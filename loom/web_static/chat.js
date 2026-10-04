/**
 * The Chat tab: the task's conversation as the desktop and iOS apps render it
 * — user / assistant / tool / question / event rows from
 * /api/tasks/<slug>/conversation — with a composer that types straight into
 * the agent's tmux pane.
 *
 * Only the visible task polls, and only while this tab is on screen: every
 * 1.5 s while the agent works, every 5 s while it waits, backing off when the
 * server does not answer. Updates merge by message id, and a send is followed
 * by a short burst of reads so the reply lands quickly.
 */

const CHAT = {
  pid: null,
  slug: null,
  messages: [],
  total: 0,
  hasMore: false,
  limit: 60,
  available: true,
  online: false,
  working: false,
  sessionId: null,
  loaded: false,
  error: '',
  timer: null,
  inFlight: false,
  failureStreak: 0,
  pending: null,
  sending: false,
  answering: false,
  answerFeedback: '',
  expandedRuns: new Set(),
  expandedTools: new Set(),
  selected: {},
  custom: {},
  stick: true,
  blocks: new Map(),
};

const CHAT_WORKING_MS = 1500;
const CHAT_IDLE_MS = 5000;
const CHAT_MAX_LIMIT = 500;
const CHAT_BURST_MS = [0, 250, 500, 1000];
// Below this many finished tool calls in a row, folding hides more than it helps.
const CHAT_FOLD_FROM = 3;

function chatDraftKey(pid = CHAT.pid, slug = CHAT.slug) {
  return `loom.chatDraft.${pid}/${slug}`;
}

function chatScope() {
  return `project=${encodeURIComponent(CHAT.pid)}`;
}

function chatActive() {
  return STATE.activePanel === 'chat' && !!STATE.slug;
}

// ===== Lifecycle (called from app.js) =====

function chatShow() {
  if (!STATE.slug || !STATE.projectId) return;
  if (CHAT.slug !== STATE.slug || CHAT.pid !== STATE.projectId) {
    chatReset();
    CHAT.pid = STATE.projectId;
    CHAT.slug = STATE.slug;
    const input = document.getElementById('chat-input');
    if (input) {
      try { input.value = localStorage.getItem(chatDraftKey()) || ''; } catch (_) { input.value = ''; }
      chatAutoGrow();
    }
    chatRender();
    chatLoad(true);
  } else if (!CHAT.timer) {
    chatLoad(false);
  }
  chatSchedule();
}

function chatHide() {
  if (CHAT.timer) clearTimeout(CHAT.timer);
  CHAT.timer = null;
}

function chatReset() {
  chatHide();
  chatSaveDraft();
  Object.assign(CHAT, {
    pid: null,
    slug: null,
    messages: [],
    total: 0,
    hasMore: false,
    limit: 60,
    available: true,
    online: false,
    working: false,
    sessionId: null,
    loaded: false,
    error: '',
    failureStreak: 0,
    pending: null,
    sending: false,
    answering: false,
    answerFeedback: '',
    selected: {},
    custom: {},
    stick: true,
  });
  CHAT.expandedRuns = new Set();
  CHAT.expandedTools = new Set();
  CHAT.blocks = new Map();
  const column = document.getElementById('chat-column');
  if (column) column.innerHTML = '';
  const latest = document.getElementById('chat-latest');
  if (latest) latest.hidden = true;
}

// After a send, a flow step, or a start: re-read a few times in quick
// succession, which catches an agent reply landing right after it.
function chatPoke() {
  if (!chatActive() || CHAT.slug !== STATE.slug) return;
  const slug = CHAT.slug;
  CHAT_BURST_MS.forEach((ms) => setTimeout(() => {
    if (CHAT.slug === slug) chatLoad(false);
  }, ms));
}

function chatSchedule() {
  if (CHAT.timer) clearTimeout(CHAT.timer);
  if (!chatActive()) {
    CHAT.timer = null;
    return;
  }
  let delay = CHAT.working ? CHAT_WORKING_MS : CHAT_IDLE_MS;
  if (CHAT.failureStreak > 0) {
    delay = Math.min(CHAT_IDLE_MS * (2 ** Math.min(CHAT.failureStreak, 4)), 60000);
  }
  CHAT.timer = setTimeout(async () => {
    if (!document.hidden) await chatLoad(false);
    chatSchedule();
  }, delay);
}

async function chatLoad(full) {
  if (!CHAT.slug || CHAT.inFlight) return;
  const slug = CHAT.slug;
  const pid = CHAT.pid;
  CHAT.inFlight = true;
  try {
    const limit = full ? CHAT.limit : 20;
    const d = await apiNoProject(
      `/api/tasks/${encodeURIComponent(slug)}/conversation?${chatScope()}&limit=${limit}`,
    );
    if (CHAT.slug !== slug || CHAT.pid !== pid) return;
    chatApply(d, !full);
    CHAT.error = '';
    CHAT.failureStreak = 0;
  } catch (err) {
    if (CHAT.slug !== slug) return;
    CHAT.failureStreak += 1;
    if (!CHAT.messages.length) CHAT.error = (err && err.message) || 'Conversation unavailable';
  } finally {
    CHAT.inFlight = false;
    if (CHAT.slug === slug) {
      CHAT.loaded = true;
      chatRender();
      if (typeof wsUpdateStatusChip === 'function') wsUpdateStatusChip();
    }
  }
}

function chatApply(feed, updateOnly) {
  CHAT.available = feed.available !== false;
  CHAT.online = !!feed.online;
  CHAT.working = !!feed.working;
  if (typeof feed.total === 'number') CHAT.total = feed.total;
  const incoming = Array.isArray(feed.messages) ? feed.messages : [];
  if (!updateOnly || CHAT.sessionId !== feed.session_id) {
    CHAT.sessionId = feed.session_id || null;
    CHAT.messages = incoming;
    CHAT.hasMore = !!feed.has_more;
  } else {
    const index = new Map(CHAT.messages.map((m, i) => [m.id, i]));
    const merged = CHAT.messages.slice();
    for (const item of incoming) {
      const i = index.get(item.id);
      if (i === undefined) merged.push(item);
      else merged[i] = item;
    }
    CHAT.messages = merged;
    CHAT.hasMore = CHAT.hasMore || !!feed.has_more;
  }
  if (CHAT.pending && incoming.some((m) => m.kind === 'user' && (m.text || '').trim() === CHAT.pending)) {
    CHAT.pending = null;
  }
}

// ===== Rendering =====

// A run of tool calls that all finished is scaffolding, not conversation.
// Folded into one line you can open; anything running or failed stays a card.
function chatGroup(messages) {
  const items = [];
  let run = [];
  const flush = () => {
    if (run.length >= CHAT_FOLD_FROM) items.push({ kind: 'run', key: `run-${run[0].id}`, tools: run });
    else run.forEach((m) => items.push({ kind: 'message', key: m.id, message: m }));
    run = [];
  };
  for (const m of messages) {
    if (m.kind === 'tool' && m.tool && m.tool.status === 'completed') run.push(m);
    else {
      flush();
      items.push({ kind: 'message', key: m.id, message: m });
    }
  }
  flush();
  return items;
}

function chatTextHtml(text) {
  return escapeHtml(String(text || '')).replace(/\n/g, '<br>');
}

function chatToolHtml(m) {
  const tool = m.tool || {};
  const status = {
    running: ['Running', 'running'],
    error: ['Error', 'error'],
    canceled: ['Stopped', 'stopped'],
  }[tool.status] || ['Done', 'done'];
  const details = !!(tool.input || tool.output);
  const open = details && CHAT.expandedTools.has(m.id);
  const summary = tool.summary && tool.summary !== tool.name
    ? `<span class="chat-tool__summary">${escapeHtml(tool.summary)}</span>` : '';
  let body = '';
  if (open) {
    if (tool.input) {
      body += `<div class="chat-tool__detail"><span>Input</span><pre>${escapeHtml(tool.input)}</pre></div>`;
    }
    if (tool.output) {
      body += `<div class="chat-tool__detail"><span>Result</span><pre>${escapeHtml(tool.output)}</pre></div>`;
    }
  }
  return `<div class="chat-tool chat-tool--${status[1]}${open ? ' is-open' : ''}" data-tool="${escapeHtml(m.id)}">`
    + `<button type="button" class="chat-tool__head"${details ? '' : ' disabled'}>`
    + `<svg class="ico ico--sm chat-tool__ico" aria-hidden="true"><use href="#i-terminal"/></svg>`
    + `<span class="chat-tool__text"><span class="chat-tool__name">${escapeHtml(tool.name || 'Tool')}</span>${summary}</span>`
    + `<span class="chat-tool__status">${status[0]}</span>`
    + (details ? `<svg class="ico ico--xs chat-tool__chevron" aria-hidden="true"><use href="#i-chevron-down"/></svg>` : '')
    + `</button>${body}</div>`;
}

function chatRunHtml(item) {
  const order = [];
  const counts = {};
  for (const t of item.tools) {
    const name = (t.tool && t.tool.name) || 'Tool';
    if (!(name in counts)) order.push(name);
    counts[name] = (counts[name] || 0) + 1;
  }
  const summary = order.map((n) => (counts[n] > 1 ? `${n} ×${counts[n]}` : n)).join(', ');
  const open = CHAT.expandedRuns.has(item.key);
  return `<div class="chat-run${open ? ' is-open' : ''}" data-run="${escapeHtml(item.key)}">`
    + `<button type="button" class="chat-run__toggle">`
    + `<svg class="ico ico--xs chat-run__chevron" aria-hidden="true"><use href="#i-chevron-right"/></svg>`
    + `<svg class="ico ico--sm" aria-hidden="true"><use href="#i-check-circle"/></svg>`
    + `<span class="chat-run__count">${item.tools.length} steps</span>`
    + `<span class="chat-run__summary">${escapeHtml(summary)}</span></button>`
    + (open ? `<div class="chat-run__tools">${item.tools.map(chatToolHtml).join('')}</div>` : '')
    + `</div>`;
}

function chatIsOther(option) {
  return String(option.label || '').trim().toLowerCase().startsWith('other');
}

function chatQuestionHtml(m) {
  const q = m.question || {};
  const pending = q.status === 'pending';
  const prompts = Array.isArray(q.questions) ? q.questions : [];
  const qid = m.id;
  const chosen = CHAT.selected[qid] || {};
  const statusLabel = {
    pending: 'Waiting for your answer',
    answered: 'Answered',
    error: 'Could not submit',
  }[q.status] || 'No longer active';
  let html = `<div class="chat-question${pending ? ' is-pending' : ''}" data-question="${escapeHtml(qid)}">`
    + `<div class="chat-question__head"><svg class="ico" aria-hidden="true"><use href="#i-interview"/></svg>`
    + `<span><strong>${escapeHtml(q.title || 'Input needed')}</strong><small>${statusLabel}</small></span></div>`;
  let otherPicked = false;
  for (const prompt of prompts) {
    const picked = new Set(chosen[prompt.id] || []);
    html += '<div class="chat-question__prompt">';
    if (prompt.header) html += `<div class="chat-question__header">${escapeHtml(prompt.header)}</div>`;
    html += `<div class="chat-question__text">${chatTextHtml(prompt.prompt)}</div>`;
    if (prompt.allow_multiple) html += '<div class="chat-question__hint">Select all that apply</div>';
    for (const option of prompt.options || []) {
      const on = picked.has(option.value);
      if (on && chatIsOther(option)) otherPicked = true;
      html += `<button type="button" class="chat-option${on ? ' is-on' : ''}" data-prompt="${escapeHtml(prompt.id)}"`
        + ` data-value="${escapeHtml(option.value)}"${pending && !CHAT.answering ? '' : ' disabled'}>`
        + `<span class="chat-option__box" aria-hidden="true"></span>`
        + `<span class="chat-option__text"><span>${escapeHtml(option.label)}</span>`
        + (option.description ? `<small>${escapeHtml(option.description)}</small>` : '')
        + `</span></button>`;
    }
    html += '</div>';
  }
  if (pending && otherPicked) {
    html += `<textarea class="chat-question__custom" rows="2" placeholder="Type a custom answer…">`
      + `${escapeHtml(CHAT.custom[qid] || '')}</textarea>`;
  }
  if (pending) {
    // A question the server can see in the pane is answered there by key; a
    // numbered list the agent wrote in prose is answered with a reply. One
    // read back from the transcript belongs to the terminal.
    const answerable = !!q.id || q.source === 'numbered';
    const complete = prompts.every((p) => (chosen[p.id] || []).length)
      && (!otherPicked || String(CHAT.custom[qid] || '').trim());
    html += '<div class="chat-question__actions">';
    if (answerable) {
      html += `<button type="button" class="btn btn--primary btn--sm chat-question__send"`
        + `${complete && !CHAT.answering ? '' : ' disabled'}>${CHAT.answering ? 'Sending…' : 'Send answer'}</button>`;
    } else {
      html += '<span class="chat-question__hint">Answer in the terminal, or type below.</span>';
    }
    if (CHAT.answerFeedback) html += `<span class="chat-question__error">${escapeHtml(CHAT.answerFeedback)}</span>`;
    html += '</div>';
  } else if (q.answer) {
    html += `<div class="chat-question__answer">${escapeHtml(q.answer)}</div>`;
  }
  return `${html}</div>`;
}

function chatMessageHtml(m) {
  switch (m.kind) {
    case 'user':
      return `<div class="chat-msg chat-msg--user"><div class="chat-bubble">${chatTextHtml(m.text)}</div>`
        + `<button type="button" class="chat-copy" title="Copy message">Copy</button></div>`;
    case 'tool':
      return chatToolHtml(m);
    case 'question':
      return chatQuestionHtml(m);
    case 'event':
      return `<div class="chat-event">${escapeHtml(m.text || '')}</div>`;
    default:
      return `<div class="chat-msg chat-msg--assistant"><span class="chat-rule" aria-hidden="true"></span>`
        + `<div class="markdown-preview chat-md">${renderMarkdown(String(m.text || ''))}</div>`
        + `<button type="button" class="chat-copy" title="Copy message">Copy</button></div>`;
  }
}

function chatBlocks() {
  const blocks = [];
  if (CHAT.hasMore && CHAT.limit < CHAT_MAX_LIMIT) {
    const remaining = Math.max(0, CHAT.total - CHAT.messages.length);
    blocks.push({
      key: '__more',
      html: `<button type="button" class="chat-more">Load earlier${remaining ? ` · ${remaining} remaining` : ''}</button>`,
    });
  }
  if (!CHAT.messages.length) {
    let title;
    let detail;
    if (!CHAT.loaded) { title = 'Loading conversation…'; detail = ''; }
    else if (CHAT.error) { title = 'Conversation unavailable'; detail = CHAT.error; }
    else if (!CHAT.available) { title = 'No structured transcript'; detail = 'This session has terminal output only.'; }
    else {
      title = 'No messages yet';
      detail = CHAT.online ? 'The agent is ready for a follow-up.' : 'Start the agent to begin.';
    }
    blocks.push({
      key: '__empty',
      html: `<div class="chat-empty"><svg class="ico" aria-hidden="true"><use href="#i-chat"/></svg>`
        + `<strong>${escapeHtml(title)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ''}</div>`,
    });
  } else {
    for (const item of chatGroup(CHAT.messages)) {
      const html = item.kind === 'run' ? chatRunHtml(item) : chatMessageHtml(item.message);
      blocks.push({ key: item.key, html, markdown: item.kind === 'message' && item.message.kind === 'assistant' });
    }
  }
  if (CHAT.pending) {
    blocks.push({
      key: '__pending',
      html: `<div class="chat-msg chat-msg--user is-pending"><div class="chat-bubble">${chatTextHtml(CHAT.pending)}</div>`
        + `<span class="chat-delivery">${CHAT.sending ? 'Sending…' : 'Queued'}</span></div>`,
    });
  }
  if (CHAT.working) {
    blocks.push({ key: '__state', html: '<div class="chat-state"><span class="spinner-dot" aria-hidden="true"></span>Agent is working…</div>' });
  } else if (CHAT.online && CHAT.messages.length) {
    blocks.push({
      key: '__state',
      html: '<div class="chat-state chat-state--ready"><svg class="ico ico--sm" aria-hidden="true"><use href="#i-check-circle"/></svg>Agent ready</div>',
    });
  }
  return blocks;
}

// Rebuilds only the blocks whose markup changed, so a poll that brings one
// new message does not re-typeset the whole conversation.
function chatRender() {
  const column = document.getElementById('chat-column');
  const feed = document.getElementById('chat-feed');
  if (!column || !feed) return;
  const blocks = chatBlocks();
  const next = new Map();
  let anchor = null;
  for (const block of blocks) {
    let entry = CHAT.blocks.get(block.key);
    if (!entry || entry.html !== block.html) {
      const wrap = document.createElement('div');
      wrap.className = 'chat-block';
      wrap.dataset.key = block.key;
      wrap.innerHTML = block.html;
      if (block.markdown) {
        const md = wrap.querySelector('.chat-md');
        if (md) typesetMath(md);
      }
      if (entry && entry.el.parentNode === column) column.replaceChild(wrap, entry.el);
      entry = { html: block.html, el: wrap };
    }
    const want = anchor ? anchor.nextSibling : column.firstChild;
    if (entry.el !== want) column.insertBefore(entry.el, want);
    anchor = entry.el;
    next.set(block.key, entry);
  }
  for (const [key, entry] of CHAT.blocks) {
    if (!next.has(key) && entry.el.parentNode === column) column.removeChild(entry.el);
  }
  CHAT.blocks = next;
  if (CHAT.stick) feed.scrollTop = feed.scrollHeight;
  chatSyncComposer();
}

function chatSyncComposer() {
  const target = chatPaneTarget();
  const interrupt = document.getElementById('chat-interrupt');
  if (interrupt) {
    interrupt.disabled = !target;
    interrupt.classList.toggle('is-hot', CHAT.working);
  }
  const send = document.getElementById('chat-send');
  const input = document.getElementById('chat-input');
  if (send && input) send.disabled = !input.value.trim() || CHAT.sending;
}

function chatPaneTarget() {
  const field = document.getElementById('inp-interview-target');
  return (field && field.value.trim()) || ((STATE.currentMeta && STATE.currentMeta.tmux_interview_target) || '');
}

// ===== Actions =====

async function chatSend(reply) {
  const input = document.getElementById('chat-input');
  if (!input || !CHAT.slug || CHAT.sending) return;
  const typed = typeof reply !== 'string';
  const text = (typed ? input.value : reply).trim();
  if (!text) return;
  CHAT.sending = true;
  CHAT.pending = text;
  CHAT.stick = true;
  if (typed) {
    input.value = '';
    chatAutoGrow();
    chatSaveDraft();
  }
  chatRender();
  try {
    await apiNoProject(`/api/tasks/${encodeURIComponent(CHAT.slug)}/claude/send?${chatScope()}`, {
      method: 'POST',
      body: JSON.stringify({ text, submit: true }),
    });
    chatPoke();
  } catch (err) {
    CHAT.pending = null;
    if (typed) {
      input.value = text;
      chatAutoGrow();
    }
    toast((err && err.message) || 'Send failed — is the agent running?', { type: 'error' });
  } finally {
    CHAT.sending = false;
    chatRender();
  }
}

async function chatInterrupt() {
  const target = chatPaneTarget();
  if (!target) return;
  try {
    await apiNoProject('/api/tmux/send-key', {
      method: 'POST',
      body: JSON.stringify({ target, key: 'Escape' }),
    });
    chatPoke();
  } catch (err) {
    toast((err && err.message) || 'Could not reach the pane', { type: 'error' });
  }
}

async function chatAnswer(qid) {
  const message = CHAT.messages.find((m) => m.id === qid);
  if (!message || CHAT.answering) return;
  const q = message.question || {};
  const chosen = CHAT.selected[qid] || {};
  const ids = (q.questions || []).flatMap((p) => chosen[p.id] || []);
  if (!q.id) {
    // A numbered list in prose: the reply is the choice itself.
    const custom = String(CHAT.custom[qid] || '').trim();
    const picked = ids.filter((v) => !chatIsOther({ label: v }));
    const reply = [...picked, ...(custom ? [custom] : [])].join(', ');
    if (reply) await chatSend(reply);
    return;
  }
  CHAT.answering = true;
  CHAT.answerFeedback = '';
  chatRender();
  try {
    await apiNoProject(`/api/tasks/${encodeURIComponent(CHAT.slug)}/conversation/answer?${chatScope()}`, {
      method: 'POST',
      body: JSON.stringify({
        question_id: q.id,
        selected_ids: ids,
        custom_text: CHAT.custom[qid] || '',
      }),
    });
    chatPoke();
  } catch (err) {
    CHAT.answerFeedback = (err && err.message) || 'Could not submit';
  } finally {
    CHAT.answering = false;
    chatRender();
  }
}

function chatToggleOption(qid, promptId, value) {
  const message = CHAT.messages.find((m) => m.id === qid);
  if (!message) return;
  const prompt = ((message.question || {}).questions || []).find((p) => String(p.id) === String(promptId));
  if (!prompt) return;
  const option = (prompt.options || []).find((o) => o.value === value);
  if (!option) return;
  const chosen = CHAT.selected[qid] || (CHAT.selected[qid] = {});
  let active = chosen[prompt.id] || [];
  const other = chatIsOther(option);
  const otherValues = (prompt.options || []).filter(chatIsOther).map((o) => o.value);
  if (!other) CHAT.custom[qid] = '';
  if (prompt.allow_multiple) {
    if (other) active = active.includes(value) ? [] : [value];
    else if (active.includes(value)) active = active.filter((v) => v !== value);
    else active = active.filter((v) => !otherValues.includes(v)).concat(value);
  } else {
    active = [value];
  }
  chosen[prompt.id] = active;
  chatRender();
}

function chatSaveDraft() {
  if (!CHAT.slug) return;
  const input = document.getElementById('chat-input');
  if (!input) return;
  try {
    if (input.value) localStorage.setItem(chatDraftKey(), input.value);
    else localStorage.removeItem(chatDraftKey());
  } catch (_) { /* private mode */ }
}

function chatAutoGrow() {
  const input = document.getElementById('chat-input');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  chatSyncComposer();
}

// ===== Wire-up =====

(function initChat() {
  const feed = document.getElementById('chat-feed');
  const column = document.getElementById('chat-column');
  const input = document.getElementById('chat-input');
  const latest = document.getElementById('chat-latest');
  if (!feed || !column || !input) return;

  feed.addEventListener('scroll', () => {
    const fromBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight;
    CHAT.stick = fromBottom < 120;
    if (latest) latest.hidden = CHAT.stick || !CHAT.messages.length;
  }, { passive: true });
  if (latest) {
    latest.addEventListener('click', () => {
      CHAT.stick = true;
      feed.scrollTo({ top: feed.scrollHeight, behavior: 'smooth' });
      latest.hidden = true;
    });
  }

  column.addEventListener('click', (ev) => {
    const more = ev.target.closest('.chat-more');
    if (more) {
      CHAT.limit = Math.min(CHAT.limit * 2, CHAT_MAX_LIMIT);
      CHAT.stick = false;
      chatLoad(true);
      return;
    }
    const run = ev.target.closest('.chat-run__toggle');
    if (run) {
      const key = run.closest('.chat-run').dataset.run;
      if (CHAT.expandedRuns.has(key)) CHAT.expandedRuns.delete(key);
      else CHAT.expandedRuns.add(key);
      chatRender();
      return;
    }
    const tool = ev.target.closest('.chat-tool__head');
    if (tool && !tool.disabled) {
      const id = tool.closest('.chat-tool').dataset.tool;
      if (CHAT.expandedTools.has(id)) CHAT.expandedTools.delete(id);
      else CHAT.expandedTools.add(id);
      chatRender();
      return;
    }
    const option = ev.target.closest('.chat-option');
    if (option && !option.disabled) {
      const qid = option.closest('.chat-question').dataset.question;
      chatToggleOption(qid, option.dataset.prompt, option.dataset.value);
      return;
    }
    const send = ev.target.closest('.chat-question__send');
    if (send && !send.disabled) {
      chatAnswer(send.closest('.chat-question').dataset.question);
      return;
    }
    const copy = ev.target.closest('.chat-copy');
    if (copy) {
      const block = copy.closest('.chat-block');
      const message = block && CHAT.messages.find((m) => m.id === block.dataset.key);
      if (message && typeof wsCopy === 'function') wsCopy(message.text || '');
    }
  });
  column.addEventListener('input', (ev) => {
    const custom = ev.target.closest('.chat-question__custom');
    if (!custom) return;
    const qid = custom.closest('.chat-question').dataset.question;
    CHAT.custom[qid] = custom.value;
    // Re-rendering would steal the caret; only the send button needs to know.
    const send = custom.closest('.chat-question').querySelector('.chat-question__send');
    if (send) send.disabled = !custom.value.trim() || CHAT.answering;
  });

  let draftTimer = 0;
  input.addEventListener('input', () => {
    chatAutoGrow();
    clearTimeout(draftTimer);
    draftTimer = setTimeout(chatSaveDraft, 300);
  });
  input.addEventListener('keydown', (ev) => {
    // An input method mid-composition owns Enter: accepting a Chinese
    // candidate must not send the half-written message.
    if (ev.key !== 'Enter' || ev.shiftKey || ev.isComposing || ev.keyCode === 229) return;
    ev.preventDefault();
    chatSend();
  });
  const send = document.getElementById('chat-send');
  if (send) send.addEventListener('click', () => chatSend());
  const interrupt = document.getElementById('chat-interrupt');
  if (interrupt) interrupt.addEventListener('click', chatInterrupt);
})();
