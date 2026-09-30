
(() => {
'use strict';

/* ==========================================================================
   Server Control Center — panel script
   1. Config & state        5. Console tabs (logs / env / files)
   2. Helpers               6. Server Assistant
   3. Toasts                7. Bot controls & redeploy
   4. Status polling        8. Init
   ========================================================================== */

/* 1. Config & state --------------------------------------------------------- */
const POLL_STATUS_MS = 3000;
const POLL_LOGS_MS = 5000;
const POLL_ASSISTANT_MS = 15000;
const BOT_TARGETS = [];   // registry-managed bots are discovered from the server at runtime
const TAB_IDS = ['assistant', 'logs', 'env', 'files'];
const DEFAULT_CHAT_TITLE = 'New chat';
const MOBILE_QUERY = window.matchMedia('(max-width: 520px)');

// Fallback until GET /api/assistant/settings returns the server's list (ids match assistant_settings.PROVIDERS).
const PROVIDER_FALLBACK = [
    { id: 'openrouter', name: 'OpenRouter' },
    { id: 'openai', name: 'OpenAI' },
    { id: 'anthropic', name: 'Anthropic' },
    { id: 'gemini', name: 'Gemini' },
    { id: 'xai', name: 'xAI' },
    { id: 'deepseek', name: 'DeepSeek' },
    { id: 'groq', name: 'Groq' },
    { id: 'copilot', name: 'GitHub Copilot' },
    { id: 'ninerouter', name: '9Router' },
    { id: 'custom', name: 'Custom OpenAI-Compatible' },
];

const ICON_ATTRS = 'viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
const ICONS = {
    pencil: `<svg ${ICON_ATTRS}><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`,
    trash: `<svg ${ICON_ATTRS}><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>`,
};

const state = {
    tab: 'logs',
    sshCommand: '',
    bots: {},
    pendingBots: new Set(),   // bots with a start/stop/restart request in flight
    logs: { raw: '', paused: false, target: '', health: 'connecting' },   // health: connecting | live | error
    env: { data: {}, masked: true, notice: '' },
    files: { path: '', parent: null },
    assistant: {
        messages: [],
        abort: null,
        requestId: '',
        sessionId: sessionStorage.getItem('assistant_session_id') || '',
        autoTitle: false,         // session still has its placeholder title; rename after the first reply
        providers: PROVIDER_FALLBACK,
        modelOptions: [],
        modelActive: -1,
    },
};

/* 2. Helpers ---------------------------------------------------------------- */
const $ = id => document.getElementById(id);

/** Build a DOM node. `html` is for trusted static markup only (icons); everything else uses textContent. */
function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'html') node.innerHTML = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children);
    return node;
}

/** fetch + JSON. Never throws on HTTP status; throws on network errors, unparsable bodies and expired sessions. */
async function fetchJson(url, options, fallback = 'Request failed') {
    const res = await fetch(url, options);
    if (res.status === 401) {
        window.location.href = '/login';
        throw new Error('Session expired');
    }
    const text = await res.text();
    let data = {};
    if (text) {
        try { data = JSON.parse(text); }
        catch { throw new Error(`${fallback} (HTTP ${res.status})`); }
    }
    return { res, data };
}

/** Like fetchJson, but throws Error(data.error) on non-2xx responses (the parsed body stays on error.data). */
async function apiJson(url, options, fallback = 'Request failed') {
    const { res, data } = await fetchJson(url, options, fallback);
    if (!res.ok) {
        const message = typeof data.error === 'string' ? data.error : data.error?.message;
        throw Object.assign(new Error(message || `${fallback} (HTTP ${res.status})`), { data });
    }
    return data;
}

const jsonOptions = (method, body, headers = {}) => ({
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
});

const formatBytes = n => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

async function copyText(text, successMessage) {
    try {
        await navigator.clipboard.writeText(text);
        showToast(successMessage);
    } catch {
        showToast('Could not copy to the clipboard.', 'error');
    }
}

function appendChatInline(target, text) {
    let cursor = 0;
    while (cursor < text.length) {
        const opening = text.indexOf('`', cursor);
        if (opening === -1) {
            target.append(document.createTextNode(text.slice(cursor)));
            return;
        }

        let tickCount = 1;
        while (opening + tickCount < text.length && text[opening + tickCount] === '`') tickCount += 1;
        const delimiter = '`'.repeat(tickCount);
        const closing = text.indexOf(delimiter, opening + tickCount);
        if (closing === -1) {
            target.append(document.createTextNode(text.slice(opening)));
            return;
        }

        target.append(document.createTextNode(text.slice(cursor, opening)));
        target.append(h('code', { class: 'message-code-inline' }, text.slice(opening + tickCount, closing)));
        cursor = closing + tickCount;
    }
}

function appendChatLines(target, lines) {
    lines.forEach((line, index) => {
        if (index) target.append(h('br'));
        appendChatInline(target, line);
    });
}

function parseChatFence(line) {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (!match) return null;
    return { character: match[1][0], length: match[1].length };
}

function isChatFenceClosing(line, fence) {
    const match = line.match(/^ {0,3}(`+|~+) *$/);
    return !!match && match[1][0] === fence.character && match[1].length >= fence.length;
}

function appendChatCodeBlock(target, lines) {
    target.append(h('pre', {}, h('code', { class: 'message-code-block' }, lines.join('\n'))));
}

function appendChatIndentedCode(target, lines, state) {
    const codeLines = [];
    while (state.index < lines.length) {
        const line = lines[state.index];
        if (/^(?:    |\t)/.test(line)) {
            codeLines.push(line.replace(/^(?:    |\t)/, ''));
            state.index += 1;
            continue;
        }
        if (!line.trim()) {
            const next = state.index + 1;
            if (next < lines.length && /^(?:    |\t)/.test(lines[next])) {
                codeLines.push('');
                state.index = next;
                continue;
            }
        }
        break;
    }
    if (codeLines.length) appendChatCodeBlock(target, codeLines);
}

function parseChatListItem(line) {
    const match = line.match(/^([ \t]*)([-*+]|\d{1,9}[.)])([ \t]+)([\s\S]*)$/);
    if (!match) return null;
    const indent = [...match[1]].reduce((total, character) => total + (character === '\t' ? 4 : 1), 0);
    return {
        indent,
        ordered: /^\d/.test(match[2]),
        marker: match[2],
        content: match[4],
    };
}

function appendChatListItem(listItem, lines, state, parentIndent) {
    const paragraph = h('p');
    listItem.append(paragraph);
    appendChatInline(paragraph, parseChatListItem(lines[state.index - 1]).content);

    while (state.index < lines.length) {
        const line = lines[state.index];
        if (!line.trim()) {
            let next = state.index + 1;
            while (next < lines.length && !lines[next].trim()) next += 1;
            if (next >= lines.length) return;

            const nextItem = parseChatListItem(lines[next]);
            const nextIndent = nextItem?.indent ?? [...lines[next].match(/^[ \t]*/)[0]]
                .reduce((total, character) => total + (character === '\t' ? 4 : 1), 0);
            if (nextIndent <= parentIndent) return;

            state.index = next;
            if (nextItem) appendChatListBlocks(listItem, lines, state, nextItem.indent);
            else {
                paragraph.append(h('br'));
                appendChatInline(paragraph, lines[state.index].replace(/^(?:    |\t)/, ''));
                state.index += 1;
            }
            continue;
        }

        const item = parseChatListItem(line);
        if (item && item.indent > parentIndent) {
            appendChatListBlocks(listItem, lines, state, item.indent);
            continue;
        }
        if (item || [...line.match(/^[ \t]*/)[0]]
            .reduce((total, character) => total + (character === '\t' ? 4 : 1), 0) <= parentIndent) return;

        paragraph.append(h('br'));
        appendChatInline(paragraph, line.replace(/^(?:    |\t)/, ''));
        state.index += 1;
    }
}

function appendChatListBlocks(target, lines, state, parentIndent) {
    let list = null;
    let listType = '';
    let listItem = null;

    while (state.index < lines.length) {
        const line = lines[state.index];
        if (!line.trim()) {
            let next = state.index + 1;
            while (next < lines.length && !lines[next].trim()) next += 1;
            if (next >= lines.length) return;

            const nextItem = parseChatListItem(lines[next]);
            const nextIndent = nextItem?.indent ?? [...lines[next].match(/^[ \t]*/)[0]]
                .reduce((total, character) => total + (character === '\t' ? 4 : 1), 0);
            if (nextIndent <= parentIndent) return;
            state.index = next;
            continue;
        }

        const item = parseChatListItem(line);
        if (!item || item.indent < parentIndent) return;
        if (item.indent === parentIndent) {
            const type = item.ordered ? 'ol' : 'ul';
            if (list && type !== listType) return;
            if (!list) {
                listType = type;
                list = h(type);
                target.append(list);
            }
            listItem = h('li');
            list.append(listItem);
            state.index += 1;
            appendChatListItem(listItem, lines, state, parentIndent);
        } else if (listItem) {
            appendChatListBlocks(listItem, lines, state, item.indent);
        } else {
            return;
        }
    }
}

function appendChatParagraph(target, lines, state) {
    const paragraphLines = [];
    while (state.index < lines.length) {
        const line = lines[state.index];
        if (!line.trim() || parseChatListItem(line) || parseChatFence(line)) break;
        paragraphLines.push(line);
        state.index += 1;
    }
    if (!paragraphLines.length) return;

    const paragraph = h('p');
    appendChatLines(paragraph, paragraphLines);
    target.append(paragraph);
}

/** Render the supported Markdown subset without injecting model text as HTML. */
function renderChatMessage(target, text) {
    const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
    const state = { index: 0 };

    while (state.index < lines.length) {
        while (state.index < lines.length && !lines[state.index].trim()) state.index += 1;
        if (state.index >= lines.length) break;

        const fence = parseChatFence(lines[state.index]);
        if (fence) {
            let closing = state.index + 1;
            while (closing < lines.length && !isChatFenceClosing(lines[closing], fence)) closing += 1;
            if (closing < lines.length) {
                appendChatCodeBlock(target, lines.slice(state.index + 1, closing));
                state.index = closing + 1;
                continue;
            }
        }

        if (/^(?:    |\t)/.test(lines[state.index])) {
            appendChatIndentedCode(target, lines, state);
            continue;
        }

        const listItem = parseChatListItem(lines[state.index]);
        if (listItem) {
            appendChatListBlocks(target, lines, state, listItem.indent);
            continue;
        }
        appendChatParagraph(target, lines, state);
    }
}

/** Disable a button while an async action runs (prevents double submits). */
async function withBusy(button, action) {
    button.disabled = true;
    try { return await action(); }
    finally { button.disabled = false; }
}

/* 3. Toasts ----------------------------------------------------------------- */
function showToast(message, type = 'success') {
    const toast = h('div', { class: `toast ${type}`, text: message });
    $('toast-container').appendChild(toast);
    setTimeout(() => toast.remove(), 3500);
}

/* 4. Status polling --------------------------------------------------------- */
function setConnection(online) {
    $('connection-dot').classList.toggle('offline', !online);
    $('connection-status').textContent = online ? 'CONNECTED' : 'OFFLINE';
}

/** Start is only usable while a bot is stopped, Stop only while it runs; all three lock during a request. */
function syncBotButtons(key) {
    const running = !!state.bots[key]?.running;
    const busy = state.pendingBots.has(key);
    document.querySelectorAll(`[data-bot="${key}"][data-action]`).forEach(button => {
        const { action } = button.dataset;
        button.disabled = busy || (action === 'start' && running) || (action === 'stop' && !running);
    });
}

function renderBot(key, bot) {
    const badge = $(`status-${key}`);
    if (badge) {
        badge.className = `status ${bot.running ? 'running' : 'stopped'}`;
        badge.replaceChildren(h('span', { class: 'status-dot' }), bot.running ? 'RUNNING' : 'STOPPED');
    }
    const fields = { pid: bot.pid, uptime: bot.uptime, restarts: bot.restarts };
    for (const [field, value] of Object.entries(fields)) {
        const node = $(`${field}-${key}`);
        if (node) node.textContent = value;
    }
    syncBotButtons(key);
}

async function refreshStatus() {
    try {
        const { res, data } = await fetchJson('/api/status', { cache: 'no-store' }, 'Status unavailable');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const { system, fleet, bots } = data;

        $('cpu-val').textContent = `${Math.round(system.cpu_percent)}%`;
        $('cpu-progress').style.width = `${Math.min(system.cpu_percent, 100)}%`;
        $('ram-val').textContent = `${system.ram_used_gb} GB`;
        $('ram-detail').textContent = `${system.ram_used_gb} / ${system.ram_total_gb} GB`;
        $('ram-progress').style.width = `${system.ram_percent}%`;
        $('fleet-val').textContent = `${fleet.online} / ${fleet.total}`;
        $('fleet-progress').style.width = `${fleet.total ? (fleet.online / fleet.total) * 100 : 0}%`;
        $('uptime-val').textContent = `Uptime: ${system.uptime}`;
        $('ssh-val').textContent = system.ssh_command;
        state.sshCommand = system.ssh_command;

        state.bots = bots;
        for (const [key, bot] of Object.entries(bots)) renderBot(key, bot);

        $('last-update').textContent = new Date().toLocaleTimeString();
        setConnection(true);
    } catch (error) {
        console.error('Status error', error);
        setConnection(false);
    }
}

/* 5. Console tabs ----------------------------------------------------------- */
const selectedTarget = () => {
    const select = $('bot-select');
    return { key: select.value, label: select.selectedOptions[0]?.text || select.value };
};

function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    TAB_IDS.forEach(id => { $(`tab-${id}`).hidden = id !== tab; });
    $('bot-select-wrapper').hidden = tab === 'files' || tab === 'assistant';
    reloadActiveTab();
}

function reloadActiveTab() {
    if (state.tab === 'logs') refreshLogs();
    else if (state.tab === 'env') refreshEnv();
    else if (state.tab === 'files') loadFiles();
    else if (state.tab === 'assistant') openAssistantTab();
}

/* Logs */
function renderLogs({ forceBottom = false } = {}) {
    const box = $('logs-content');
    const query = $('log-search').value.toLowerCase();
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = query
        ? state.logs.raw.split('\n').filter(line => line.toLowerCase().includes(query)).join('\n')
        : state.logs.raw;
    if (forceBottom || atBottom) box.scrollTop = box.scrollHeight;   // keep the view where the user left it otherwise
}

/** Indicator next to the log filter: LIVE (green), PAUSED (white, manual) or ERROR (red, last fetch failed). */
function renderLogState(hint = '') {
    const status = state.logs.paused ? 'paused' : state.logs.health;
    const indicator = $('log-state');
    indicator.dataset.state = status;
    indicator.title = status === 'error' ? hint : '';
    $('log-state-text').textContent = { connecting: 'CONNECTING', live: 'LIVE', paused: 'PAUSED', error: 'ERROR' }[status];
}

async function refreshLogs() {
    const { key, label } = selectedTarget();
    const targetChanged = state.logs.target !== key;
    // While paused, only a target switch fetches (once) so the view never shows another bot's logs.
    if (state.tab !== 'logs' || (state.logs.paused && !targetChanged)) return;
    state.logs.target = key;
    $('log-target').textContent = label;
    try {
        const data = await apiJson(`/api/logs/${encodeURIComponent(key)}?lines=200`, { cache: 'no-store' }, 'Unable to load logs');
        state.logs.raw = data.logs || '';
        state.logs.health = 'live';
        $('log-count').textContent = `${data.total_lines || 0} lines`;
        renderLogState();
    } catch (error) {
        state.logs.raw = `Unable to load ${label} logs: ${error.message}`;
        state.logs.health = 'error';
        $('log-count').textContent = 'error';
        renderLogState(error.message);
    }
    renderLogs({ forceBottom: targetChanged });
}

/** Pause is white (matches the PAUSED indicator); Resume is green. */
function togglePauseLogs() {
    state.logs.paused = !state.logs.paused;
    const button = $('pause-logs');
    button.textContent = state.logs.paused ? '▶ Resume' : '⏸ Pause';
    button.classList.toggle('btn-pause', !state.logs.paused);
    button.classList.toggle('btn-start', state.logs.paused);
    renderLogState();
    if (!state.logs.paused) refreshLogs();   // catch up immediately instead of waiting for the next poll
}

/* Environment */
function renderEnv() {
    const rows = Object.entries(state.env.data).map(([name, value]) =>
        h('tr', {}, h('td', { text: name }), h('td', { text: state.env.masked ? '••••••••••••' : value })));
    if (!rows.length) {
        rows.push(h('tr', {}, h('td', { class: 'env-empty', colspan: 2, text: state.env.notice || 'No variables found.' })));
    }
    $('env-content').replaceChildren(...rows);
    const count = Object.keys(state.env.data).length;
    $('env-count').textContent = state.env.notice ? '—' : `${count} variable${count === 1 ? '' : 's'}`;
}

async function refreshEnv() {
    const { key, label } = selectedTarget();
    state.env.data = {};
    state.env.notice = '';
    if (!BOT_TARGETS.includes(key)) {
        state.env.notice = `No .env file is exposed for ${label}.`;
        renderEnv();
        return;
    }
    try {
        const data = await apiJson(`/api/env/${encodeURIComponent(key)}`, undefined, 'Unable to load environment');
        state.env.data = data.env || {};
    } catch (error) {
        state.env.notice = error.message;
    }
    renderEnv();
}

/* Files */
async function loadFiles(path = state.files.path) {
    try {
        const data = await apiJson(`/api/files?path=${encodeURIComponent(path)}`, undefined, 'Unable to list files');
        if (data.is_file) return previewFile(data.path);
        state.files.path = data.current_path;
        state.files.parent = data.parent_path;
        $('current-file-path').textContent = `Path: ${data.current_path}`;
        $('parent-dir-btn').disabled = !data.parent_path;
        $('file-preview-box').hidden = true;
        $('file-entries').replaceChildren(...data.entries.map(entry => h('div', {
            class: 'file-item',
            onclick: () => (entry.is_dir ? loadFiles(entry.path) : previewFile(entry.path)),
        },
            h('div', {}, h('span', { class: 'file-icon', text: entry.is_dir ? '📁' : '📄' }), entry.name),
            h('span', { class: 'log-meta', text: entry.is_dir ? 'DIR' : formatBytes(entry.size_bytes) }))));
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function previewFile(path) {
    try {
        const data = await apiJson(`/api/file/content?path=${encodeURIComponent(path)}`, undefined, 'Unable to preview file');
        const box = $('file-preview-box');
        box.textContent = data.content;
        box.hidden = false;
    } catch (error) {
        // The backend reports oversize files as { content: "File too large…" } with HTTP 400.
        if (error.data?.content) {
            const box = $('file-preview-box');
            box.textContent = error.data.content;
            box.hidden = false;
        } else {
            showToast(error.message, 'error');
        }
    }
}

/* 6. Server Assistant ------------------------------------------------------- */
const assistant = state.assistant;

/* 6a. Health pill ("Active model") */
function setModelStatus(status, { model, hint = '' } = {}) {
    const pill = $('assistant-model-status');
    pill.dataset.state = status;
    pill.title = hint;
    pill.querySelector('.model-status-text').textContent = status.toUpperCase();
    if (model !== undefined) $('assistant-model-name').textContent = model || '—';
}

async function checkAssistantHealth() {
    try {
        const { data } = await fetchJson('/api/assistant/health');   // 503 bodies still carry { available, error }
        if (data.available) setModelStatus('ready', { model: data.model, hint: data.version ? `Hermes ${data.version}` : '' });
        else setModelStatus('offline', { model: data.model, hint: data.error || 'Hermes is unavailable' });
    } catch (error) {
        setModelStatus('offline', { hint: error.message });
    }
}

/* 6b. Sidebar layout: collapse (desktop), drawer (mobile), provider panel */
const sidebar = () => $('assistant-sidebar');

function setDrawerOpen(open) {
    $('assistant-chat').classList.toggle('drawer-open', open);
}

function toggleSidebar() {
    if (MOBILE_QUERY.matches) return setDrawerOpen(false);
    const collapsed = sidebar().classList.toggle('collapsed');
    const button = $('sidebar-toggle-btn');
    button.textContent = collapsed ? '▶' : '◀';
    button.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    if (collapsed) sidebar().classList.remove('settings-open');
}

function setProviderPanelOpen(open) {
    sidebar().classList.toggle('settings-open', open);
    if (open) {
        if (!$('assistant-provider-select').options.length) renderProviderOptions(assistant.providers);
        updateProviderFields();
        loadCurrentSettings();
    } else {
        closeModelList();
    }
}

function openProvidersFromRail() {
    if (sidebar().classList.contains('collapsed')) toggleSidebar();
    setProviderPanelOpen(true);
}

/* 6c. Chat sessions (stored in Hermes, listed in the sidebar) */
async function assistantCsrf(refresh = false) {
    let token = refresh ? '' : sessionStorage.getItem('assistant_csrf');
    if (!token) {
        const data = await apiJson('/api/assistant/capabilities', undefined, 'Could not get a security token');
        token = data.csrf_token || '';
        if (token) sessionStorage.setItem('assistant_csrf', token);
    }
    return token;
}

/** JSON request with the CSRF header; retries once with a fresh token if the stored one went stale. */
async function assistantApi(method, url, body) {
    for (let attempt = 0; attempt < 2; attempt++) {
        const token = await assistantCsrf(attempt > 0);
        const { res, data } = await fetchJson(url, jsonOptions(method, body, { 'X-CSRF-Token': token }));
        if (res.status === 403 && attempt === 0) continue;
        if (!res.ok) throw Object.assign(new Error(data.error || 'Request failed'), { data });
        return data;
    }
}

function setActivity(text = '') {
    $('assistant-activity').textContent = text;
}

function highlightActiveSession() {
    document.querySelectorAll('.assistant-session-item').forEach(item => {
        item.classList.toggle('active', !!assistant.sessionId && item.dataset.sessionId === assistant.sessionId);
    });
}

function renderSessionList(sessions) {
    const list = $('assistant-session-list');
    if (!sessions.length) {
        list.replaceChildren(h('div', { class: 'assistant-session-empty', text: 'No chats yet' }));
        return;
    }
    list.replaceChildren(...sessions.map(session => {
        const label = session.title || session.id || 'Session';
        const item = h('div', { class: 'assistant-session-item', 'data-session-id': session.id },
            h('span', { class: 'session-title', text: label, title: label }),
            h('span', { class: 'session-actions' },
                h('button', {
                    class: 'session-action', type: 'button', title: 'Rename chat', 'aria-label': 'Rename chat', html: ICONS.pencil,
                    onclick: event => { event.stopPropagation(); startSessionRename(item, session); },
                }),
                h('button', {
                    class: 'session-action danger', type: 'button', title: 'Delete chat', 'aria-label': 'Delete chat', html: ICONS.trash,
                    onclick: event => { event.stopPropagation(); deleteSession(session.id, label); },
                })));
        item.addEventListener('click', () => {
            if (item.classList.contains('editing')) return;
            loadSession(session.id).then(ok => { if (ok && MOBILE_QUERY.matches) setDrawerOpen(false); });
        });
        return item;
    }));
    highlightActiveSession();
}

async function loadSessions() {
    try {
        const data = await apiJson('/api/assistant/sessions?limit=100', undefined, 'Could not load sessions');
        renderSessionList(Array.isArray(data.data) ? data.data : []);
    } catch (error) {
        $('assistant-session-list').replaceChildren(h('div', { class: 'assistant-session-empty', text: error.message }));
    }
}

function startSessionRename(item, session) {
    const titleEl = item.querySelector('.session-title');
    if (!titleEl || item.classList.contains('editing')) return;
    const original = session.title || '';
    const input = h('input', { class: 'session-title-input', type: 'text', maxlength: 120, value: original });
    item.classList.add('editing');
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let finished = false;
    const finish = async save => {
        if (finished) return;
        finished = true;
        const value = input.value.trim();
        item.classList.remove('editing');
        if (save && value && value !== original) {
            try {
                await assistantApi('PATCH', `/api/assistant/sessions/${encodeURIComponent(session.id)}`, { title: value });
                if (session.id === assistant.sessionId) assistant.autoTitle = false;
                showToast('Chat renamed');
            } catch (error) {
                showToast(`Rename failed: ${error.message}`, 'error');
            }
        }
        await loadSessions();
    };
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('click', event => event.stopPropagation());
}

async function deleteSession(sessionId, label) {
    if (assistant.abort && sessionId === assistant.sessionId) {
        showToast('Stop the current response before deleting this chat.', 'error');
        return;
    }
    if (!confirm(`Delete "${label}"?\n\nThis permanently removes the chat and its messages.`)) return;
    try {
        await assistantApi('DELETE', `/api/assistant/sessions/${encodeURIComponent(sessionId)}`);
        showToast('Chat deleted');
    } catch (error) {
        if (!/not found/i.test(error.message)) {
            showToast(`Delete failed: ${error.message}`, 'error');
            return;
        }
    }
    if (sessionId === assistant.sessionId) resetChatView();
    await loadSessions();
}

/** Hermes also stores tool calls/results; the chat view only replays plain user/assistant text. */
function normalizeSessionMessages(rows) {
    return (Array.isArray(rows) ? rows : [])
        .map(row => {
            let content = row?.content;
            if (Array.isArray(content)) content = content.map(part => (typeof part === 'string' ? part : part?.text || '')).join('');
            return { role: row?.role, content };
        })
        .filter(m => (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string' && m.content.trim());
}

function rememberSession(sessionId) {
    assistant.sessionId = sessionId;
    if (sessionId) sessionStorage.setItem('assistant_session_id', sessionId);
    else sessionStorage.removeItem('assistant_session_id');
}

async function loadSession(sessionId) {
    if (assistant.abort) {
        showToast('Stop the current response before switching chats.', 'error');
        return false;
    }
    setActivity('Loading session...');
    try {
        const data = await apiJson(`/api/assistant/sessions/${encodeURIComponent(sessionId)}`, undefined, 'Could not load session');
        rememberSession(sessionId);
        assistant.messages = normalizeSessionMessages(data.messages);
        const title = data.session?.title || '';
        assistant.autoTitle = assistant.messages.length === 0 && /^New chat( \(\d+\))?$/.test(title);
        renderMessages();
        setActivity();
        highlightActiveSession();
        return true;
    } catch (error) {
        setActivity(`Error loading session: ${error.message}`);
        return false;
    }
}

function resetChatView() {
    assistant.messages = [];
    assistant.autoTitle = false;
    rememberSession('');
    setActivity();
    renderMessages();
    highlightActiveSession();
}

async function createSession(title, autoTitle) {
    const data = await assistantApi('POST', '/api/assistant/sessions', title ? { title } : {});
    const created = data.session || {};
    if (!created.id) throw new Error('Hermes did not return a session id');
    rememberSession(created.id);
    assistant.autoTitle = !!autoTitle;
    return created;
}

function titleFromMessage(text) {
    const line = text.split('\n').map(part => part.trim()).find(Boolean) || DEFAULT_CHAT_TITLE;
    const compact = line.replace(/\s+/g, ' ');
    return compact.length > 48 ? `${compact.slice(0, 47)}…` : compact;
}

/** After a turn: give a placeholder-titled chat a name from its first message, then refresh the list. */
async function finalizeSessionTurn(sessionId, replied, firstMessage) {
    if (replied && assistant.autoTitle && sessionId === assistant.sessionId) {
        assistant.autoTitle = false;
        try {
            await assistantApi('PATCH', `/api/assistant/sessions/${encodeURIComponent(sessionId)}`, { title: titleFromMessage(firstMessage) });
        } catch { /* keep the placeholder title */ }
    }
    await loadSessions();
}

/** "+" button: start a separate saved chat (it shows up in the sidebar) instead of clearing the current one. */
async function newChat() {
    if (assistant.abort) {
        showToast('Stop the current response before starting a new chat.', 'error');
        return;
    }
    if (assistant.sessionId && assistant.autoTitle && assistant.messages.length === 0) {
        $('assistant-input').focus();   // already on a fresh, empty chat
    } else {
        resetChatView();
        try {
            await createSession(DEFAULT_CHAT_TITLE, true);
        } catch (error) {
            showToast(`Could not create a saved chat: ${error.message}`, 'error');
        }
        await loadSessions();
        $('assistant-input').focus();
    }
    if (MOBILE_QUERY.matches) setDrawerOpen(false);
}

/** Runs when the assistant tab opens: health, chat list, and restoring the chat that was open before a reload. */
async function openAssistantTab() {
    checkAssistantHealth();
    await loadSessions();
    if (assistant.sessionId && assistant.messages.length === 0 && !assistant.abort) {
        if (!(await loadSession(assistant.sessionId))) {
            rememberSession('');
            highlightActiveSession();
            setActivity();
        }
    }
}

/* 6d. Provider settings + model combobox */
function renderProviderOptions(providers, selected) {
    const select = $('assistant-provider-select');
    const current = selected || select.value;
    select.replaceChildren(...providers.map(p => h('option', { value: p.id, text: p.id === 'custom' ? 'Custom OpenAI-Compatible' : p.name })));
    if (current && providers.some(p => p.id === current)) select.value = current;
}

/** The base URL is only editable for the custom provider; every other provider has a fixed endpoint. */
function updateProviderFields() {
    const provider = $('assistant-provider-select').value;
    $('assistant-base-url-row').hidden = provider !== 'custom';
    const entry = assistant.providers.find(p => p.id === provider);
    $('assistant-api-key').placeholder = entry?.key_configured ? 'API key saved - leave blank to keep' : 'API Key or Token';
}

function onProviderChange() {
    const baseUrl = $('assistant-base-url');
    const knownDefaults = assistant.providers.map(p => p.base_url).filter(Boolean);
    if (knownDefaults.includes(baseUrl.value.trim())) baseUrl.value = '';
    setModelOptions([]);
    updateProviderFields();
}

async function loadCurrentSettings() {
    try {
        const data = await apiJson('/api/assistant/settings', undefined, 'Could not load settings');
        if (data.csrf_token) sessionStorage.setItem('assistant_csrf', data.csrf_token);
        if (Array.isArray(data.providers) && data.providers.length) {
            assistant.providers = data.providers;
            renderProviderOptions(assistant.providers, data.provider);
        } else if (data.provider) {
            $('assistant-provider-select').value = data.provider;
        }
        $('assistant-base-url').value = data.provider === 'custom' ? data.base_url || '' : '';
        if (data.model) $('assistant-model').value = data.model;
        updateProviderFields();
    } catch (error) {
        console.error('Load settings error', error);
    }
}

function setModelOptions(models) {
    assistant.modelOptions = Array.isArray(models) ? models : [];
    const hasOptions = assistant.modelOptions.length > 0;
    $('assistant-model-combo').classList.toggle('has-options', hasOptions);
    $('assistant-model-toggle').hidden = !hasOptions;
    if (hasOptions) renderModelList(false);
    else closeModelList();
}

function renderModelList(filter) {
    const value = $('assistant-model').value.trim();
    const query = filter ? value.toLowerCase() : '';
    const matches = assistant.modelOptions.filter(id => !query || id.toLowerCase().includes(query));
    assistant.modelActive = -1;
    $('assistant-model-list').replaceChildren(...(matches.length
        ? matches.map(id => h('div', {
            class: `model-combo-option${id === value ? ' selected' : ''}`,
            role: 'option',
            text: id,
            onmousedown: event => { event.preventDefault(); chooseModel(id); },
        }))
        : [h('div', { class: 'model-combo-empty', text: 'No matching models - your typed ID will be used as-is.' })]));
}

function openModelList(filter) {
    if (!assistant.modelOptions.length) return;
    renderModelList(filter);
    $('assistant-model-list').classList.add('open');
    $('assistant-model').setAttribute('aria-expanded', 'true');
    $('assistant-model-list').querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

function closeModelList() {
    $('assistant-model-list').classList.remove('open');
    $('assistant-model').setAttribute('aria-expanded', 'false');
    assistant.modelActive = -1;
}

function chooseModel(id) {
    $('assistant-model').value = id;
    closeModelList();
}

function moveModelHighlight(step) {
    const items = $('assistant-model-list').querySelectorAll('.model-combo-option');
    if (!items.length) return;
    items.forEach(item => item.classList.remove('active'));
    assistant.modelActive = (assistant.modelActive + step + items.length) % items.length;
    items[assistant.modelActive].classList.add('active');
    items[assistant.modelActive].scrollIntoView({ block: 'nearest' });
}

function onModelKeydown(event) {
    const list = $('assistant-model-list');
    const isOpen = list.classList.contains('open');
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        if (!assistant.modelOptions.length) return;
        event.preventDefault();
        if (!isOpen) openModelList(false);
        moveModelHighlight(event.key === 'ArrowDown' ? 1 : -1);
    } else if (event.key === 'Enter' && isOpen && assistant.modelActive >= 0) {
        event.preventDefault();
        chooseModel(list.querySelectorAll('.model-combo-option')[assistant.modelActive].textContent);
    } else if (event.key === 'Escape' && isOpen) {
        event.preventDefault();
        closeModelList();
    }
}

async function fetchModels() {
    const provider = $('assistant-provider-select').value;
    const baseUrl = $('assistant-base-url').value.trim();
    const status = $('assistant-settings-status');
    if (provider === 'custom' && !baseUrl) {
        status.textContent = 'Enter a base URL first';
        $('assistant-base-url').focus();
        return;
    }
    const button = $('assistant-fetch-btn');
    button.textContent = 'Fetching...';
    status.textContent = 'Fetching models...';
    await withBusy(button, async () => {
        try {
            const data = await apiJson('/api/assistant/models', jsonOptions('POST', {
                provider,
                base_url: provider === 'custom' ? baseUrl : '',
                api_key: $('assistant-api-key').value.trim(),
            }), 'Model fetch failed');
            const models = Array.isArray(data.models) ? data.models : [];
            setModelOptions(models);
            status.textContent = models.length ? `Loaded ${models.length} models - pick one or type your own` : 'Provider returned no models';
            if (models.length) {
                $('assistant-model').focus();
                openModelList(false);
            }
        } catch (error) {
            status.textContent = error.message;
            showToast(error.message, 'error');
        } finally {
            button.textContent = '↻ Fetch';
        }
    });
}

async function saveProvider() {
    const provider = $('assistant-provider-select').value;
    const status = $('assistant-settings-status');
    status.textContent = 'Saving and restarting Hermes...';
    setModelStatus('checking', { hint: 'Restarting Hermes' });
    await withBusy($('assistant-save-btn'), async () => {
        try {
            await apiJson('/api/assistant/settings/model', jsonOptions('POST', {
                provider,
                base_url: provider === 'custom' ? $('assistant-base-url').value.trim() : '',
                api_key: $('assistant-api-key').value.trim(),
                model: $('assistant-model').value.trim(),
            }, { 'X-CSRF-Token': await assistantCsrf() }), 'Save failed');
            status.textContent = 'Saved and Hermes restarted';
            $('assistant-api-key').value = '';
            showToast(`${provider} configured.`);
            loadCurrentSettings();
        } catch (error) {
            status.textContent = error.message;
            showToast(error.message, 'error');
        } finally {
            checkAssistantHealth();
        }
    });
}

/* 6e. Chat */
function renderMessages() {
    const box = $('assistant-messages');
    box.replaceChildren(...assistant.messages.map((message, index) => {
        const messageElement = h('div', { class: `assistant-message ${message.role}` });
        renderChatMessage(messageElement, message.content);
        const group = h('div', { class: `assistant-message-group ${message.role}` },
            messageElement,
            h('button', {
                class: 'assistant-message-action', type: 'button', title: 'Copy message', 'aria-label': 'Copy message', text: '⧉',
                onclick: () => copyText(message.content, 'Message copied to clipboard!'),
            }));
        if (message.role === 'assistant') {
            group.append(h('button', {
                class: 'assistant-message-action', type: 'button', title: 'Regenerate response', 'aria-label': 'Regenerate response', text: '↻',
                onclick: () => regenerate(index),
            }));
        }
        return group;
    }));
    box.scrollTop = box.scrollHeight;
}

/** One button is Send (green) or Stop (red) depending on whether a reply is being generated. */
function setBusy(busy) {
    const button = $('assistant-send');
    button.classList.toggle('btn-start', !busy);
    button.classList.toggle('btn-stop', busy);
    button.textContent = busy ? '■ Stop' : '➤ Send';
    button.title = busy ? 'Stop response' : 'Send message';
}

function onSendClick() {
    if (assistant.abort) stopGenerating();
    else sendMessage();
}

function stopGenerating() {
    if (assistant.requestId) {
        fetch('/api/assistant/stop', jsonOptions('POST', { request_id: assistant.requestId })).catch(() => {});
    }
    assistant.abort?.abort();
    setBusy(false);
    setActivity('Stopped');
}

/**
 * Reads the relayed OpenAI-style SSE stream, appending deltas to `message`.
 * `progress` is updated live so callers still know text arrived if the stream is aborted mid-way.
 */
async function readReplyStream(response, message, progress) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop();
        for (const event of events) {
            const line = event.split('\n').find(entry => entry.startsWith('data: '));
            if (!line || line === 'data: [DONE]') continue;
            let data;
            try { data = JSON.parse(line.slice(6)); } catch { continue; }   // ignore non-JSON stream metadata
            if (data.error) {
                const reason = typeof data.error === 'string' ? data.error : data.error.message;
                message.content = `Assistant error: ${reason || 'Hermes returned an error.'}`;
                setActivity('Hermes request failed');
                progress.failed = true;
                renderMessages();
                continue;
            }
            const delta = data.choices?.[0]?.delta?.content || '';
            if (!delta) continue;
            if (!progress.started) {
                progress.started = true;
                message.content = '';
                setActivity('Writing response');
            }
            message.content += delta;
            renderMessages();
        }
    }
}

async function sendMessage(reuseLastUserMessage = false) {
    const input = $('assistant-input');
    const last = assistant.messages[assistant.messages.length - 1];
    const content = reuseLastUserMessage && last?.role === 'user' ? last.content : input.value.trim();
    if (!content || assistant.abort) return;
    input.value = '';

    if (!reuseLastUserMessage) assistant.messages.push({ role: 'user', content });
    const reply = { role: 'assistant', content: 'Sending to Hermes...' };
    assistant.messages.push(reply);
    renderMessages();
    setActivity('Message received');

    const controller = new AbortController();
    assistant.abort = controller;
    assistant.requestId = crypto.randomUUID();
    setBusy(true);

    const progress = { started: false, failed: false };
    let sessionForRequest = '';
    const slowTimer = setTimeout(() => {
        if (progress.started) return;
        reply.content = 'Connecting to your provider...';
        setActivity('Provider connection in progress');
        renderMessages();
    }, 900);

    try {
        // Regenerating replays a turn Hermes already stored, so it runs without a session to avoid duplicate history rows.
        if (!reuseLastUserMessage) {
            if (!assistant.sessionId) {
                try {
                    await createSession(titleFromMessage(content), false);
                    loadSessions();   // list the new chat right away
                } catch (error) {
                    showToast(`This chat won't be saved to history: ${error.message}`, 'error');
                }
            }
            sessionForRequest = assistant.sessionId;
        }

        const body = { messages: assistant.messages.slice(0, -1) };
        if (sessionForRequest) body.session_id = sessionForRequest;
        const response = await fetch('/api/assistant/chat', {
            ...jsonOptions('POST', body, { 'X-Assistant-Request-Id': assistant.requestId }),
            signal: controller.signal,
        });
        if (response.status === 401) { window.location.href = '/login'; return; }
        if (!response.ok) {
            const { error } = await response.json().catch(() => ({}));
            throw new Error((typeof error === 'string' ? error : error?.message) || 'Assistant request failed');
        }

        reply.content = 'Hermes is thinking...';
        setActivity('Hermes is working');
        renderMessages();
        await readReplyStream(response, reply, progress);
        if (!progress.started && !progress.failed) {
            reply.content = 'Hermes returned no response. Check Logs → Hermes Gateway.';
            renderMessages();
            setActivity('No response received');
        } else if (progress.started) {
            setActivity();
        }
    } catch (error) {
        if (error.name !== 'AbortError') {
            reply.content = `Assistant error: ${error.message}`;
            renderMessages();
        } else if (!progress.started) {
            reply.content = 'Stopped.';
            renderMessages();
        }
    } finally {
        clearTimeout(slowTimer);
        if (assistant.abort === controller) {
            assistant.abort = null;
            assistant.requestId = '';
        }
        setBusy(!!assistant.abort);
        if (sessionForRequest) await finalizeSessionTurn(sessionForRequest, progress.started, content);
    }
}

async function regenerate(index) {
    if (assistant.abort) return;
    if (assistant.messages[index - 1]?.role !== 'user') return;
    assistant.messages = assistant.messages.slice(0, index);
    await sendMessage(true);
}

/* 6f. Composer keyboard: Enter sends, Shift+Enter adds a line */
function onInputKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

/* 7. Bot controls & redeploy ------------------------------------------------ */
async function controlBot(bot, action) {
    state.pendingBots.add(bot);
    syncBotButtons(bot);
    try {
        const data = await apiJson(`/api/bot/${encodeURIComponent(bot)}/${action}`, { method: 'POST' }, 'Action failed');
        showToast(data.message || 'Done');
        setTimeout(refreshLogs, 1000);
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        state.pendingBots.delete(bot);
        syncBotButtons(bot);
        refreshStatus();   // re-syncs the buttons with the bot's real state
    }
}

async function redeployServer(button) {
    const confirmed = confirm('Are you sure you want to redeploy the server?\n\nThis will launch a fresh GitHub runner instance with the latest code and gracefully terminate the current VM.');
    if (!confirmed) return;
    await withBusy(button, async () => {
        showToast('Triggering server redeploy...');
        try {
            await apiJson('/api/server/redeploy', { method: 'POST' }, 'Redeploy failed');
            showToast('Redeploy initiated! Launching fresh GitHub runner...');
        } catch (error) {
            showToast(`Redeploy failed: ${error.message}`, 'error');
        }
    });
}

/* 8. Init ------------------------------------------------------------------- */
function bindEvents() {
    // Header, fleet, metrics
    $('redeploy-btn').addEventListener('click', event => redeployServer(event.currentTarget));
    $('copy-ssh-btn').addEventListener('click', () => (state.sshCommand
        ? copyText(state.sshCommand, 'SSH command copied to clipboard!')
        : showToast('SSH command is not available yet.', 'error')));
    document.querySelectorAll('[data-bot][data-action]').forEach(button =>
        button.addEventListener('click', () => controlBot(button.dataset.bot, button.dataset.action)));

    // Console
    document.querySelectorAll('.tab-btn').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));
    $('bot-select').addEventListener('change', reloadActiveTab);
    $('log-search').addEventListener('input', () => renderLogs());
    $('pause-logs').addEventListener('click', togglePauseLogs);
    $('toggle-env-mask').addEventListener('click', () => { state.env.masked = !state.env.masked; renderEnv(); });
    $('parent-dir-btn').addEventListener('click', () => { if (state.files.parent) loadFiles(state.files.parent); });

    // Assistant: sidebar
    $('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    $('new-chat-btn').addEventListener('click', newChat);
    $('sessions-open-btn').addEventListener('click', () => setDrawerOpen(true));
    $('assistant-backdrop').addEventListener('click', () => setDrawerOpen(false));
    $('providers-btn').addEventListener('click', () => setProviderPanelOpen(true));
    $('rail-providers-btn').addEventListener('click', openProvidersFromRail);
    $('assistant-cancel-btn').addEventListener('click', () => setProviderPanelOpen(false));
    // Clicking blank space in the sidebar collapses the provider settings.
    sidebar().addEventListener('click', event => {
        if (!sidebar().classList.contains('settings-open')) return;
        if (event.target.closest('.assistant-settings-panel, .assistant-session-item, .assistant-sidebar-footer, button, input, select, a')) return;
        setProviderPanelOpen(false);
    });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') setDrawerOpen(false); });

    // Assistant: provider settings
    $('assistant-provider-select').addEventListener('change', onProviderChange);
    $('assistant-fetch-btn').addEventListener('click', fetchModels);
    $('assistant-save-btn').addEventListener('click', saveProvider);

    // Assistant: model combobox
    const modelInput = $('assistant-model');
    modelInput.addEventListener('focus', () => openModelList(false));
    modelInput.addEventListener('click', () => { if (!$('assistant-model-list').classList.contains('open')) openModelList(false); });
    modelInput.addEventListener('input', () => openModelList(true));
    modelInput.addEventListener('keydown', onModelKeydown);
    $('assistant-model-toggle').addEventListener('mousedown', event => {
        event.preventDefault();
        if ($('assistant-model-list').classList.contains('open')) closeModelList();
        else { modelInput.focus(); openModelList(false); }
    });
    document.addEventListener('mousedown', event => {
        if (!$('assistant-model-combo').contains(event.target)) closeModelList();
    });

    // Assistant: chat
    $('assistant-send').addEventListener('click', onSendClick);
    $('assistant-input').addEventListener('keydown', onInputKeydown);
}

function startPolling() {
    const whenVisible = task => () => { if (!document.hidden) task(); };
    setInterval(whenVisible(refreshStatus), POLL_STATUS_MS);
    setInterval(whenVisible(refreshLogs), POLL_LOGS_MS);
    setInterval(whenVisible(() => { if (state.tab === 'assistant' && !assistant.abort) checkAssistantHealth(); }), POLL_ASSISTANT_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshStatus(); refreshLogs(); } });
}

bindEvents();
BOT_TARGETS.forEach(syncBotButtons);   // initial state until the first status arrives (bots assumed stopped)
startPolling();
refreshStatus();
refreshLogs();
})();
