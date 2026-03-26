// panoptic frontend — vanilla JS, no frameworks
// All DOM IDs must match index.html exactly.

import { createTerminal } from '/static/terminal.js';

let currentHostId = 'localhost';  // active host tab
let currentPage = 1;
let currentSession = null;  // session name while in session view, null for dashboard
let sessionPollTimer = null;
let hostPollTimer = null;

// Active terminal handle (non-null while session view is open).
let _terminalHandle = null;

// Keyed reconciliation state: session name -> card DOM element.
const _cardMap = new Map();

// Fetch sequence counter — prevents stale responses from overwriting newer UI.
let _fetchSeq = 0;
let _terminalLoadEpoch = 0;

// Pending delete state for the confirmation modal.
let _pendingDelete = null;  // { name, attached, source: 'gallery'|'session' }

// Auto-hide timer for success banner.
let _successTimer = null;

// Cached host list from last fetch.
let _hosts = [];

// Template state
let _templates = [];            // cached template list from API
let _selectedTemplate = null;   // currently selected template object (with variables[])
let _paneCommands = [];         // per-pane command strings indexed by pane order

// Tracks which fields the user has actively edited (removes prefilled styling).
const _editedFields = new Set();

// Macro placeholder regex — matches backend template_macros.py _PLACEHOLDER_RE.
const MACRO_PLACEHOLDER_RE = /\{([^}]*)\}/g;
const MACRO_VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function initTheme() {
    const saved = localStorage.getItem('theme');
    if (saved === 'dark' || saved === 'light') {
        applyTheme(saved);
    } else {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(prefersDark ? 'dark' : 'light');
    }

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!localStorage.getItem('theme')) {
            applyTheme(e.matches ? 'dark' : 'light');
        }
    });
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.textContent = theme === 'dark' ? '\u2600' : '\u263E';
        btn.title = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }
    // Keep the PWA theme-color in sync so the browser chrome matches.
    const metaTheme = document.querySelector('meta[name="theme-color"]:not([media])') ||
                      document.querySelector('meta[name="theme-color"]');
    if (metaTheme) metaTheme.content = theme === 'dark' ? 'hsl(217, 51%, 14%)' : 'hsl(20, 10%, 97%)';
    // Swap favicon and header icon to match active theme.
    const iconSrc = theme === 'dark' ? '/static/icon-dark.svg' : '/static/icon-light.svg';
    const favicon = document.querySelector('link[rel="icon"]');
    if (favicon) favicon.href = iconSrc;
    const headerIcon = document.getElementById('header-icon');
    if (headerIcon) headerIcon.src = iconSrc;
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    localStorage.setItem('theme', next);
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function formatAge(epochSeconds) {
    const diffSeconds = Math.floor(Date.now() / 1000) - epochSeconds;
    if (diffSeconds < 60) return `${diffSeconds}s ago`;
    const diffMinutes = Math.floor(diffSeconds / 60);
    if (diffMinutes < 60) return `${diffMinutes}m ago`;
    const diffHours = Math.floor(diffMinutes / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
}

function thumbnailBucket() {
    return Math.floor(Date.now() / 30000);
}

function formatTime(date) {
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    const ss = String(date.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
}

function showBanner(message) {
    const banner = document.getElementById('warning-banner');
    banner.textContent = message;
    banner.classList.remove('hidden');
}

function hideBanner() {
    document.getElementById('warning-banner').classList.add('hidden');
}

function showSuccessBanner(message) {
    if (_successTimer) clearTimeout(_successTimer);
    const banner = document.getElementById('success-banner');
    banner.textContent = message;
    banner.classList.remove('hidden');
    _successTimer = setTimeout(() => {
        banner.classList.add('hidden');
        _successTimer = null;
    }, 4000);
}

function hideSuccessBanner() {
    if (_successTimer) { clearTimeout(_successTimer); _successTimer = null; }
    document.getElementById('success-banner').classList.add('hidden');
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// Refresh status
// ---------------------------------------------------------------------------

function setRefreshing(active) {
    const spinner = document.getElementById('refresh-spinner');
    if (active) {
        spinner.classList.remove('hidden');
    } else {
        spinner.classList.add('hidden');
    }
}

// ---------------------------------------------------------------------------
// Host tabs
// ---------------------------------------------------------------------------

async function fetchHosts() {
    try {
        const resp = await fetch('/api/hosts');
        if (!resp.ok) return;
        const data = await resp.json();
        _hosts = data.hosts || [];
        renderHostTabs();
    } catch (err) {
        // Silently fail; host tabs stay as-is.
    }
}

function renderHostTabs() {
    const container = document.getElementById('host-tabs');
    container.innerHTML = '';

    for (const host of _hosts) {
        const tab = document.createElement('button');
        tab.className = 'host-tab' + (host.id === currentHostId ? ' active' : '');
        tab.role = 'tab';
        tab.setAttribute('aria-selected', host.id === currentHostId ? 'true' : 'false');
        tab.dataset.hostId = host.id;

        // Status indicator
        const dot = document.createElement('span');
        dot.className = 'host-tab-status';
        if (host.status === 'ok') {
            dot.classList.add('ok');
        } else if (host.status === 'auth_error' || host.status === 'unreachable' || host.status === 'error') {
            dot.classList.add('error');
            tab.title = host.status_message || host.status;
        }
        tab.appendChild(dot);

        const label = document.createElement('span');
        label.className = 'host-tab-label';
        label.textContent = host.label;
        tab.appendChild(label);

        tab.addEventListener('click', () => switchHost(host.id));
        container.appendChild(tab);
    }

    // "+" button to add a new host
    const addBtn = document.createElement('button');
    addBtn.className = 'host-tab host-tab-add';
    addBtn.title = 'Add SSH host';
    addBtn.textContent = '+';
    addBtn.addEventListener('click', openAddHostModal);
    container.appendChild(addBtn);
}

function switchHost(hostId) {
    if (hostId === currentHostId && !currentSession) return;

    currentHostId = hostId;
    currentPage = 1;

    // If in session view, close it.
    if (currentSession) {
        closeSession();
    }

    // Clear card map for the new host.
    _cardMap.forEach((card) => card.remove());
    _cardMap.clear();

    // Update tab appearance.
    renderHostTabs();

    // Update URL.
    history.pushState(null, '', '/?host=' + encodeURIComponent(hostId));

    // Restart polling for the new host.
    startSessionPolling();
}

// ---------------------------------------------------------------------------
// Polling helpers
// ---------------------------------------------------------------------------

function startSessionPolling() {
    stopSessionPolling();
    fetchSessions(currentPage);
    sessionPollTimer = setInterval(() => fetchSessions(currentPage), 10_000);
}

function stopSessionPolling() {
    if (sessionPollTimer !== null) {
        clearInterval(sessionPollTimer);
        sessionPollTimer = null;
    }
}

function startHostPolling() {
    stopHostPolling();
    fetchHosts();
    hostPollTimer = setInterval(fetchHosts, 60_000);
}

function stopHostPolling() {
    if (hostPollTimer !== null) {
        clearInterval(hostPollTimer);
        hostPollTimer = null;
    }
}

// ---------------------------------------------------------------------------
// Session list — keyed reconciliation
// ---------------------------------------------------------------------------

async function fetchSessions(page = 1) {
    const seq = ++_fetchSeq;
    setRefreshing(true);

    const hostId = encodeURIComponent(currentHostId);

    try {
        const resp = await fetch(`/api/hosts/${hostId}/sessions?page=${page}&page_size=8`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        const data = await resp.json();

        if (seq !== _fetchSeq) return;

        hideBanner();
        renderSessions(data);

        const countEl = document.getElementById('session-count');
        countEl.textContent = `${data.total} session${data.total !== 1 ? 's' : ''}`;

        const refreshedEl = document.getElementById('last-refreshed');
        refreshedEl.textContent = `Updated ${formatTime(new Date())}`;
    } catch (err) {
        if (seq !== _fetchSeq) return;
        showBanner(`Failed to fetch sessions: ${err.message}`);
    } finally {
        if (seq === _fetchSeq) setRefreshing(false);
    }

}

function renderSessions(data) {
    const grid = document.getElementById('session-grid');
    const emptyState = document.getElementById('empty-state');
    const paginationEl = document.getElementById('pagination');

    if (data.sessions.length === 0) {
        emptyState.classList.remove('hidden');
        grid.classList.add('hidden');
        paginationEl.classList.add('hidden');
        _cardMap.forEach((card) => card.remove());
        _cardMap.clear();
        return;
    }

    emptyState.classList.add('hidden');
    grid.classList.remove('hidden');

    const incoming = new Set(data.sessions.map(s => s.name));

    for (const [name, card] of _cardMap) {
        if (!incoming.has(name)) {
            card.remove();
            _cardMap.delete(name);
        }
    }

    for (const session of data.sessions) {
        let card = _cardMap.get(session.name);
        if (card) {
            updateCard(card, session);
        } else {
            card = createCard(session);
            _cardMap.set(session.name, card);
        }
        grid.appendChild(card);
    }

    renderPagination(data.page, data.pages);
}

function createCard(session) {
    const card = document.createElement('div');
    card.className = 'session-card';
    card.dataset.sessionName = session.name;

    const safeName = encodeURIComponent(session.name);
    const safeHost = encodeURIComponent(currentHostId);
    const attachedClass = session.attached ? 'active' : '';

    card.innerHTML = `
        <div class="session-thumbnail-wrap">
            <img class="session-thumbnail"
                 src="/api/hosts/${safeHost}/sessions/${safeName}/thumbnail.svg?t=${thumbnailBucket()}"
                 alt=""
                 loading="lazy"
                 onerror="this.style.display='none'">
        </div>
        <div class="session-card-body">
            <div class="session-card-header">
                <span class="session-name">${escapeHtml(session.name)}</span>
                <span class="attached-indicator ${attachedClass}"></span>
                <div class="card-actions-wrap">
                    <button class="card-actions-btn" title="Actions">&#8942;</button>
                    <div class="card-actions-menu actions-menu hidden">
                        <button class="actions-menu-item destructive card-delete-btn">Delete</button>
                    </div>
                </div>
            </div>
            <div class="session-meta">
                <span class="window-badge">${session.windows} window${session.windows !== 1 ? 's' : ''}</span>
                <span class="session-age">${formatAge(session.created_epoch)}</span>
            </div>
            <button class="open-btn" data-session="${safeName}">Open</button>
        </div>
    `;

    card.querySelector('.open-btn').addEventListener('click', () => {
        openSession(session.name);
    });
    card.querySelector('.card-actions-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        toggleCardMenu(card);
    });
    const delBtn = card.querySelector('.card-delete-btn');
    delBtn.dataset.sessionName = session.name;
    delBtn.dataset.attached = session.attached ? '1' : '';
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeAllMenus();
        const btn = e.currentTarget;
        requestSessionDelete(btn.dataset.sessionName, btn.dataset.attached === '1', 'gallery');
    });

    return card;
}

function updateCard(card, session) {
    const safeName = encodeURIComponent(session.name);
    const safeHost = encodeURIComponent(currentHostId);

    const indicator = card.querySelector('.attached-indicator');
    indicator.classList.toggle('active', session.attached);

    const badge = card.querySelector('.window-badge');
    const badgeText = `${session.windows} window${session.windows !== 1 ? 's' : ''}`;
    if (badge.textContent !== badgeText) badge.textContent = badgeText;

    const age = card.querySelector('.session-age');
    const ageText = formatAge(session.created_epoch);
    if (age.textContent !== ageText) age.textContent = ageText;

    const img = card.querySelector('.session-thumbnail');
    if (img) {
        const expectedSrc = `/api/hosts/${safeHost}/sessions/${safeName}/thumbnail.svg?t=${thumbnailBucket()}`;
        if (!img.src.endsWith(expectedSrc)) {
            img.src = expectedSrc;
            img.style.display = '';
        }
    }

    // Update data attributes for event delegation instead of cloning.
    const deleteBtn = card.querySelector('.card-delete-btn');
    if (deleteBtn) {
        deleteBtn.dataset.sessionName = session.name;
        deleteBtn.dataset.attached = session.attached ? '1' : '';
    }
}

function renderPagination(page, totalPages) {
    const paginationEl = document.getElementById('pagination');

    if (totalPages <= 1) {
        paginationEl.classList.add('hidden');
        return;
    }

    paginationEl.classList.remove('hidden');
    paginationEl.innerHTML = '';

    for (let i = 1; i <= totalPages; i++) {
        const btn = document.createElement('button');
        btn.className = 'page-btn' + (i === page ? ' active' : '');
        btn.textContent = String(i);
        btn.addEventListener('click', () => {
            currentPage = i;
            _cardMap.forEach((card) => card.remove());
            _cardMap.clear();
            fetchSessions(i);
        });
        paginationEl.appendChild(btn);
    }
}

// ---------------------------------------------------------------------------
// Session view — direct xterm.js terminal (no iframe)
// ---------------------------------------------------------------------------

async function openSession(sessionName) {
    currentSession = sessionName;

    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('session-view').classList.remove('hidden');
    document.getElementById('session-title').textContent = sessionName;

    stopSessionPolling();
    closeAllMenus();

    history.pushState(null, '', '/?host=' + encodeURIComponent(currentHostId) + '&session=' + encodeURIComponent(sessionName));

    await loadSessionTerminal(sessionName);
}

async function loadSessionTerminal(sessionName) {
    // Dispose any previous terminal before creating a new one.
    if (_terminalHandle) {
        _terminalHandle.dispose();
        _terminalHandle = null;
    }

    const loadEpoch = ++_terminalLoadEpoch;
    const container = document.getElementById('terminal-container');
    const hostId = encodeURIComponent(currentHostId);
    const safeName = encodeURIComponent(sessionName);

    try {
        const resp = await fetch(`/api/hosts/${hostId}/sessions/${safeName}`);

        // Stale response — user navigated away or opened a different session.
        if (loadEpoch !== _terminalLoadEpoch) return;

        if (!resp.ok) {
            showBanner(
                resp.status === 404
                    ? `Session "${sessionName}" no longer exists.`
                    : `Failed to load session: HTTP ${resp.status}`
            );
            return;
        }

        const data = await resp.json();

        // Check again after JSON parse (another await boundary).
        if (loadEpoch !== _terminalLoadEpoch) return;

        if (!data.ttyd_url) {
            showBanner(`Session "${sessionName}" has no terminal available (port not assigned).`);
            return;
        }

        hideBanner();
        _terminalHandle = createTerminal(container, data.ttyd_url);
    } catch (err) {
        if (loadEpoch !== _terminalLoadEpoch) return;
        showBanner(`Failed to connect to session: ${err.message}`);
    }
}

function closeSession() {
    currentSession = null;
    _terminalLoadEpoch++;  // Invalidate any in-flight loadSessionTerminal.
    closeAllMenus();

    if (_terminalHandle) {
        _terminalHandle.dispose();
        _terminalHandle = null;
    }

    document.getElementById('session-view').classList.add('hidden');
    document.getElementById('dashboard-view').classList.remove('hidden');

    history.pushState(null, '', '/?host=' + encodeURIComponent(currentHostId));

    startSessionPolling();
}

// ---------------------------------------------------------------------------
// Dropdown menus (card + session-view)
// ---------------------------------------------------------------------------

function toggleCardMenu(card) {
    const menu = card.querySelector('.card-actions-menu');
    const wasOpen = !menu.classList.contains('hidden');
    closeAllMenus();
    if (!wasOpen) menu.classList.remove('hidden');
}

function toggleSessionViewMenu() {
    const menu = document.getElementById('session-actions-menu');
    const wasOpen = !menu.classList.contains('hidden');
    closeAllMenus();
    if (!wasOpen) menu.classList.remove('hidden');
}

function closeAllMenus() {
    document.querySelectorAll('.actions-menu').forEach(m => m.classList.add('hidden'));
}

// ---------------------------------------------------------------------------
// Delete session flow
// ---------------------------------------------------------------------------

function requestSessionDelete(name, attached, source) {
    _pendingDelete = { name, attached, source };

    const modal = document.getElementById('delete-session-modal');
    const confirmText = document.getElementById('delete-confirm-text');
    const warningText = document.getElementById('delete-warning-text');
    const confirmBtn = document.getElementById('confirm-delete-btn');

    confirmText.textContent = `Are you sure you want to delete session "${name}"? This will kill the tmux session and cannot be undone.`;

    if (attached) {
        warningText.textContent = 'This session is currently attached. Deleting it will disconnect any active clients.';
        warningText.classList.remove('hidden');
    } else {
        warningText.classList.add('hidden');
    }

    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Delete';
    modal.classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-session-modal').classList.add('hidden');
    _pendingDelete = null;
}

async function confirmDeleteSession() {
    if (!_pendingDelete) return;
    const { name, source } = _pendingDelete;

    const confirmBtn = document.getElementById('confirm-delete-btn');
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Deleting\u2026';

    const hostId = encodeURIComponent(currentHostId);
    const safeName = encodeURIComponent(name);

    try {
        const resp = await fetch(`/api/hosts/${hostId}/sessions/${safeName}`, {
            method: 'DELETE',
        });
        const data = await resp.json();

        if (!resp.ok) {
            closeDeleteModal();
            showBanner(data.error || `Failed to delete session: HTTP ${resp.status}`);
            return;
        }

        closeDeleteModal();

        if (source === 'session' && currentSession === name) {
            closeSession();
            showSuccessBanner(`Session "${name}" deleted.`);
        } else {
            const card = _cardMap.get(name);
            if (card) {
                card.remove();
                _cardMap.delete(name);
            }
            showSuccessBanner(`Session "${name}" deleted.`);
            fetchSessions(currentPage);
        }
    } catch (err) {
        closeDeleteModal();
        showBanner(`Failed to delete session: ${err.message}`);
    }
}

// ---------------------------------------------------------------------------
// New session modal
// ---------------------------------------------------------------------------

const SESSION_NAME_RE = /^[A-Za-z0-9_-]+$/;

let _cwdAbort = null;
let _cwdActiveIndex = -1;

function openNewSessionModal() {
    document.getElementById('new-session-modal').classList.remove('hidden');
    document.getElementById('session-name-input').value = '';
    document.getElementById('layout-spec-input').value = '';
    document.getElementById('session-name-error').classList.add('hidden');
    document.getElementById('session-name-input').classList.remove('invalid');
    setActiveLayoutType('none');
    hideCompletions();
    clearLayoutPreview();
    _paneCommands = [];
    _selectedTemplate = null;
    _editedFields.clear();
    document.getElementById('template-select').value = '';
    document.getElementById('rename-template-btn').disabled = true;
    document.getElementById('delete-template-btn').disabled = true;
    hideMacroVariables();

    // Initialize Working Directory from active host's default_cwd.
    const activeHost = _hosts.find(h => h.id === currentHostId);
    const defaultCwd = activeHost?.default_cwd || '';
    document.getElementById('session-cwd-input').value = defaultCwd;

    // Reset macro status and Create button state.
    document.getElementById('macro-status-row').classList.add('hidden');
    document.getElementById('create-session-btn').disabled = false;

    // Fetch templates when modal opens.
    fetchTemplates();
    recomputeFormState();
    document.getElementById('session-name-input').focus();
}
function closeNewSessionModal() {
    document.getElementById('new-session-modal').classList.add('hidden');
    hideCompletions();
    _selectedTemplate = null;
    _paneCommands = [];
    _editedFields.clear();
}

function setActiveLayoutType(type) {
    const btns = document.querySelectorAll('#layout-type-toggle .layout-type-btn');
    btns.forEach(b => b.classList.toggle('active', b.dataset.layout === type));

    const specGroup = document.getElementById('layout-spec-group');
    if (type === 'none') {
        specGroup.classList.add('hidden');
    } else {
        specGroup.classList.remove('hidden');
        document.getElementById('layout-spec-input').focus();
    }
    updateLayoutPreview();
}

function getActiveLayoutType() {
    const active = document.querySelector('#layout-type-toggle .layout-type-btn.active');
    return active ? active.dataset.layout : 'none';
}

// ---------------------------------------------------------------------------
// Session name validation
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Centralized macro analysis + form state
// ---------------------------------------------------------------------------

/**
 * Analyze a text value for macro placeholders.
 * Returns { placeholders: string[], malformed: boolean, hasBraces: boolean }.
 */
function analyzeMacros(text) {
    if (!text) return { placeholders: [], malformed: false, hasBraces: false };
    const hasBraces = text.includes('{') || text.includes('}');
    const placeholders = [];
    let malformed = false;
    // Check for unclosed braces.
    if (/\{(?![^}]*\})/.test(text)) malformed = true;
    let m;
    const re = /\{([^}]*)\}/g;
    while ((m = re.exec(text)) !== null) {
        const name = m[1];
        if (!name || !MACRO_VAR_NAME_RE.test(name)) {
            malformed = true;
        } else {
            placeholders.push(name);
        }
    }
    return { placeholders, malformed, hasBraces };
}

/**
 * Gather form field values + analyze all for macros.
 * Returns a comprehensive state object consumed by recomputeFormState.
 */
function analyzeFormMacros() {
    const sessionName = document.getElementById('session-name-input').value;
    const cwd = document.getElementById('session-cwd-input').value;
    const layoutSpec = document.getElementById('layout-spec-input').value;
    const fields = [
        { id: 'session-name-input', label: 'Session Name', value: sessionName },
        { id: 'session-cwd-input', label: 'Working Directory', value: cwd },
        { id: 'layout-spec-input', label: 'Layout Spec', value: layoutSpec },
    ];
    for (let i = 0; i < _paneCommands.length; i++) {
        fields.push({ id: `pane-cmd-${i}`, label: `Pane Command ${i}`, value: _paneCommands[i] || '' });
    }

    let anyBraces = false;
    let anyMalformed = false;
    const allPlaceholders = new Set();
    const fieldResults = [];

    for (const f of fields) {
        const a = analyzeMacros(f.value);
        fieldResults.push({ ...f, ...a });
        if (a.hasBraces) anyBraces = true;
        if (a.malformed) anyMalformed = true;
        for (const p of a.placeholders) allPlaceholders.add(p);
    }

    // Determine new placeholders not in the selected template's variable set.
    const templateVars = new Set((_selectedTemplate?.variables || []));
    const newPlaceholders = [];
    for (const p of allPlaceholders) {
        if (!templateVars.has(p)) newPlaceholders.push(p);
    }

    return {
        fields: fieldResults,
        anyBraces,
        anyMalformed,
        allPlaceholders: [...allPlaceholders],
        newPlaceholders,
        templateVars: [...templateVars],
        isTemplateMode: !!_selectedTemplate,
    };
}

/**
 * Single entry point for updating all button states, field highlights,
 * macro status message, and error display. Called on every relevant input.
 */
function recomputeFormState() {
    const analysis = analyzeFormMacros();
    const createBtn = document.getElementById('create-session-btn');
    const statusRow = document.getElementById('macro-status-row');
    const nameError = document.getElementById('session-name-error');
    const nameInput = document.getElementById('session-name-input');

    // Clear previous macro-related classes from form inputs.
    for (const f of analysis.fields) {
        const el = document.getElementById(f.id);
        if (el) {
            el.classList.remove('macro-highlight', 'invalid');
        }
    }

    // Reset status row.
    statusRow.classList.add('hidden');
    statusRow.textContent = '';

    // Reset name error (macro-related; not session-name-format).
    // We selectively re-show it below if needed.

    let canCreate = true;
    let statusMessage = '';

    if (analysis.anyMalformed) {
        canCreate = false;
        statusMessage = 'Invalid macro placeholder detected. Use {variable_name} format.';
    }

    if (!analysis.isTemplateMode) {
        // --- Direct mode ---
        // Session name format validation (only when no braces).
        const nameVal = nameInput.value.trim();
        if (nameVal && !analysis.fields[0].hasBraces && !SESSION_NAME_RE.test(nameVal)) {
            nameError.textContent = 'Only letters, digits, hyphens, and underscores allowed.';
            nameError.classList.remove('hidden');
            nameInput.classList.add('invalid');
            canCreate = false;
        } else if (!analysis.anyMalformed) {
            nameError.classList.add('hidden');
        }

        if (analysis.anyBraces && !analysis.anyMalformed) {
            canCreate = false;
            // Highlight fields that contain braces.
            for (const f of analysis.fields) {
                if (f.hasBraces) {
                    const el = document.getElementById(f.id);
                    if (el) el.classList.add('macro-highlight');
                }
            }
            statusMessage = 'Macro placeholders ({...}) found. Save as a template to use macros, or remove them to create directly.';
        }
    } else {
        // --- Template mode ---
        // Allow placeholders that are in the template variable set.
        // Disable Create if:
        //   1) malformed placeholders
        //   2) new placeholders not in template variable set
        //   3) required macro variable inputs are empty
        if (analysis.newPlaceholders.length > 0 && !analysis.anyMalformed) {
            canCreate = false;
            // Highlight fields with new placeholders.
            for (const f of analysis.fields) {
                for (const p of f.placeholders) {
                    if (analysis.newPlaceholders.includes(p)) {
                        const el = document.getElementById(f.id);
                        if (el) el.classList.add('macro-highlight');
                    }
                }
            }
            statusMessage = `New placeholder(s) {${analysis.newPlaceholders.join('}, {')}} not in template. Save or update the template first.`;
        }

        // Check if all required macro variable inputs are filled.
        const varInputs = document.querySelectorAll('#macro-variables-container .macro-var-input');
        for (const input of varInputs) {
            if (!input.value.trim()) {
                canCreate = false;
                // Don't show status message for empty vars — the field itself shows invalid.
                break;
            }
        }

        // Session name validation for template mode: after rendering,
        // the result must still be valid. But we skip format errors while placeholders exist.
    }

    if (statusMessage) {
        statusRow.textContent = statusMessage;
        statusRow.classList.remove('hidden');
    }

    createBtn.disabled = !canCreate;

    // Update token preview overlays for template mode.
    if (analysis.isTemplateMode) {
        updateAllTokenPreviews();
    }

    return analysis;
}

/**
 * Collect current macro variable values from the template variable inputs.
 */
function getCurrentMacroValues() {
    const vars = {};
    const inputs = document.querySelectorAll('#macro-variables-container .macro-var-input');
    for (const input of inputs) {
        vars[input.dataset.varName] = input.value;
    }
    return vars;
}

/**
 * Render a field value with token highlighting as HTML.
 * Substituted tokens get .macro-token; unresolved get .macro-token-unresolved.
 */
function renderTokenPreviewHtml(text, vars) {
    if (!text) return '';
    let result = '';
    let lastIndex = 0;
    const re = /\{([^}]*)\}/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        // Text before this match.
        result += escapeHtml(text.slice(lastIndex, m.index));
        const name = m[1];
        if (name && MACRO_VAR_NAME_RE.test(name) && vars[name] !== undefined && vars[name] !== '') {
            result += `<span class="macro-token">${escapeHtml(vars[name])}</span>`;
        } else if (name && MACRO_VAR_NAME_RE.test(name)) {
            result += `<span class="macro-token-unresolved">{${escapeHtml(name)}}</span>`;
        } else {
            result += escapeHtml(m[0]);
        }
        lastIndex = m.index + m[0].length;
    }
    result += escapeHtml(text.slice(lastIndex));
    return result;
}

/**
 * Update all token preview overlays for template-mode fields.
 * Shows rendered preview when field is not focused; hides when focused.
 */
function updateAllTokenPreviews() {
    if (!_selectedTemplate) return;
    const vars = getCurrentMacroValues();
    const fieldIds = ['session-name-input', 'session-cwd-input', 'layout-spec-input'];
    for (const id of fieldIds) {
        const input = document.getElementById(id);
        if (!input) continue;
        const wrap = input.closest('.macro-preview-wrap');
        if (!wrap) continue;
        let display = wrap.querySelector('.macro-preview-display');
        if (!display) {
            display = document.createElement('div');
            display.className = 'macro-preview-display';
            wrap.appendChild(display);
        }
        const html = renderTokenPreviewHtml(input.value, vars);
        display.innerHTML = html;
        // Only show overlay when the field is not focused and has macro content.
        const hasMacros = /\{[^}]*\}/.test(input.value);
        display.style.display = (hasMacros && document.activeElement !== input) ? '' : 'none';
    }
}

/**
 * Wire focus/blur on a field to toggle token preview overlay visibility.
 */
function setupPreviewToggle(input) {
    input.addEventListener('focus', () => {
        const wrap = input.closest('.macro-preview-wrap');
        if (wrap) {
            const display = wrap.querySelector('.macro-preview-display');
            if (display) display.style.display = 'none';
        }
        // Remove prefilled styling on first focus.
        input.classList.remove('prefilled');
        _editedFields.add(input.id);
    });
    input.addEventListener('blur', () => {
        // Slight delay to avoid flicker on re-focus.
        setTimeout(() => {
            if (document.activeElement === input) return;
            updateAllTokenPreviews();
        }, 50);
    });
}

// ---------------------------------------------------------------------------
// Path autocompletion
// ---------------------------------------------------------------------------

async function fetchCompletions(prefix) {
    if (_cwdAbort) _cwdAbort.abort();
    _cwdAbort = new AbortController();

    const hostId = encodeURIComponent(currentHostId);

    try {
        const resp = await fetch(
            `/api/hosts/${hostId}/completions/path?prefix=${encodeURIComponent(prefix)}`,
            { signal: _cwdAbort.signal }
        );
        if (!resp.ok) return [];
        const data = await resp.json();
        return data.completions || [];
    } catch (e) {
        if (e.name === 'AbortError') return [];
        return [];
    }
}

function showCompletions(items) {
    const dropdown = document.getElementById('cwd-completions');
    dropdown.innerHTML = '';
    _cwdActiveIndex = -1;

    if (items.length === 0) {
        dropdown.classList.add('hidden');
        return;
    }

    for (let i = 0; i < items.length; i++) {
        const el = document.createElement('div');
        el.className = 'autocomplete-item';
        el.textContent = items[i];
        el.addEventListener('mousedown', (e) => {
            e.preventDefault();
            selectCompletion(items[i]);
        });
        dropdown.appendChild(el);
    }
    dropdown.classList.remove('hidden');
}

function hideCompletions() {
    document.getElementById('cwd-completions').classList.add('hidden');
    _cwdActiveIndex = -1;
}

function selectCompletion(path) {
    document.getElementById('session-cwd-input').value = path;
    hideCompletions();
    onCwdInput();
}

async function onCwdInput() {
    const val = document.getElementById('session-cwd-input').value;
    if (!val) {
        hideCompletions();
        return;
    }
    const items = await fetchCompletions(val);
    showCompletions(items);
}

function onCwdKeydown(e) {
    const dropdown = document.getElementById('cwd-completions');
    if (dropdown.classList.contains('hidden')) return;
    const items = dropdown.querySelectorAll('.autocomplete-item');
    if (items.length === 0) return;

    if (e.key === 'ArrowDown') {
        e.preventDefault();
        _cwdActiveIndex = Math.min(_cwdActiveIndex + 1, items.length - 1);
        highlightCompletion(items);
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        _cwdActiveIndex = Math.max(_cwdActiveIndex - 1, 0);
        highlightCompletion(items);
    } else if (e.key === 'Enter' && _cwdActiveIndex >= 0) {
        e.preventDefault();
        selectCompletion(items[_cwdActiveIndex].textContent);
    } else if (e.key === 'Escape') {
        hideCompletions();
    } else if (e.key === 'Tab' && _cwdActiveIndex >= 0) {
        e.preventDefault();
        selectCompletion(items[_cwdActiveIndex].textContent);
    }
}

function highlightCompletion(items) {
    items.forEach((el, i) => {
        el.classList.toggle('active', i === _cwdActiveIndex);
    });
    if (_cwdActiveIndex >= 0 && items[_cwdActiveIndex]) {
        items[_cwdActiveIndex].scrollIntoView({ block: 'nearest' });
    }
}

// ---------------------------------------------------------------------------
// Layout preview
// ---------------------------------------------------------------------------

function parseLayoutSpec(spec) {
    if (!spec || !spec.trim()) return null;
    const segments = spec.split(':');
    const counts = [];
    const commands = [];

    for (const seg of segments) {
        const s = seg.trim();
        if (!s) return null;

        // Try pure integer first.
        const n = parseInt(s, 10);
        if (!isNaN(n) && String(n) === s && n >= 1) {
            counts.push(n);
            for (let i = 0; i < n; i++) commands.push('');
            continue;
        }

        // Command segment: comma-separated.
        const cmds = s.split(',').map(c => c.trim());
        if (cmds.length === 0 || cmds.some(c => c === '')) return null;
        counts.push(cmds.length);
        commands.push(...cmds);
    }

    return counts.length > 0 ? { counts, commands } : null;
}

function updateLayoutPreview() {
    const type = getActiveLayoutType();
    const spec = document.getElementById('layout-spec-input').value.trim();
    const previewEl = document.getElementById('layout-preview');
    const cmdSection = document.getElementById('pane-commands-section');

    if (type === 'none' || !spec) {
        previewEl.classList.add('hidden');
        previewEl.innerHTML = '';
        cmdSection.classList.add('hidden');
        _paneCommands = [];
        return;
    }

    const parsed = parseLayoutSpec(spec);
    if (!parsed) {
        previewEl.classList.add('hidden');
        previewEl.innerHTML = '';
        cmdSection.classList.add('hidden');
        _paneCommands = [];
        return;
    }

    const { counts, commands } = parsed;
    const totalPanes = counts.reduce((a, b) => a + b, 0);

    // Ensure _paneCommands array covers all panes.
    while (_paneCommands.length < totalPanes) _paneCommands.push('');
    _paneCommands.length = totalPanes;

    // Pre-fill from spec commands where no overlay exists.
    for (let i = 0; i < totalPanes; i++) {
        if (!_paneCommands[i] && commands[i]) {
            _paneCommands[i] = commands[i];
        }
    }

    previewEl.classList.remove('hidden');
    previewEl.innerHTML = '';
    let paneIdx = 0;

    function makePaneEl(idx) {
        const pane = document.createElement('div');
        pane.className = 'layout-preview-pane clickable';
        pane.dataset.paneIndex = idx;
        const label = document.createElement('span');
        label.className = 'pane-index';
        label.textContent = _paneCommands[idx] ? '\u2713' : String(idx);
        pane.appendChild(label);
        pane.addEventListener('click', () => focusPaneCommand(idx));
        return pane;
    }

    if (type === 'row') {
        for (const paneCount of counts) {
            const row = document.createElement('div');
            row.className = 'layout-preview-row';
            for (let p = 0; p < paneCount; p++) {
                row.appendChild(makePaneEl(paneIdx++));
            }
            previewEl.appendChild(row);
        }
    } else {
        const row = document.createElement('div');
        row.className = 'layout-preview-row';
        for (const paneCount of counts) {
            const col = document.createElement('div');
            col.className = 'layout-preview-col';
            for (let p = 0; p < paneCount; p++) {
                col.appendChild(makePaneEl(paneIdx++));
            }
            row.appendChild(col);
        }
        previewEl.appendChild(row);
    }

    // Render pane command editor.
    renderPaneCommandEditor(totalPanes);
}

function clearLayoutPreview() {
    const previewEl = document.getElementById('layout-preview');
    previewEl.classList.add('hidden');
    previewEl.innerHTML = '';
    document.getElementById('pane-commands-section').classList.add('hidden');
    _paneCommands = [];
}

function renderPaneCommandEditor(totalPanes) {
    const section = document.getElementById('pane-commands-section');
    const container = document.getElementById('pane-commands-container');
    container.innerHTML = '';

    if (totalPanes <= 1) {
        // For single-pane, show a simple input.
        section.classList.remove('hidden');
        const row = document.createElement('div');
        row.className = 'pane-command-row';
        const label = document.createElement('span');
        label.className = 'pane-command-label';
        label.textContent = 'Pane 0';
        const input = document.createElement('input');
        input.className = 'pane-command-input';
        input.type = 'text';
        input.placeholder = 'startup command (optional)';
        input.value = _paneCommands[0] || '';
        input.addEventListener('input', () => {
            _paneCommands[0] = input.value;
            updateLayoutPreview();
        });
        row.appendChild(label);
        row.appendChild(input);
        container.appendChild(row);
        return;
    }

    section.classList.remove('hidden');
    for (let i = 0; i < totalPanes; i++) {
        const row = document.createElement('div');
        row.className = 'pane-command-row';
        const label = document.createElement('span');
        label.className = 'pane-command-label';
        label.textContent = `Pane ${i}`;
        const input = document.createElement('input');
        input.className = 'pane-command-input';
        input.type = 'text';
        input.placeholder = 'startup command (optional)';
        input.value = _paneCommands[i] || '';
        input.dataset.paneIndex = i;
        input.addEventListener('input', () => {
            _paneCommands[i] = input.value;
            // Update preview pane indicators.
            const paneEl = document.querySelector(`.layout-preview-pane[data-pane-index="${i}"] .pane-index`);
            if (paneEl) paneEl.textContent = input.value ? '\u2713' : String(i);
        });
        row.appendChild(label);
        row.appendChild(input);
        container.appendChild(row);
    }
}

function focusPaneCommand(idx) {
    const section = document.getElementById('pane-commands-section');
    if (section.classList.contains('hidden')) return;
    const input = section.querySelector(`.pane-command-input[data-pane-index="${idx}"]`);
    if (input) {
        input.focus();
        input.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
}

// ---------------------------------------------------------------------------
// Template management
// ---------------------------------------------------------------------------

async function fetchTemplates() {
    try {
        const resp = await fetch('/api/templates');
        if (!resp.ok) return;
        const data = await resp.json();
        _templates = data.templates || [];
        renderTemplateSelect();
    } catch (e) {
        // Silently fail.
    }
}

function renderTemplateSelect() {
    const sel = document.getElementById('template-select');
    const current = sel.value;
    // Clear all except the first "None" option.
    while (sel.options.length > 1) sel.remove(1);
    for (const t of _templates) {
        const opt = document.createElement('option');
        opt.value = t.template_name;
        opt.textContent = t.template_name;
        sel.appendChild(opt);
    }
    // Restore selection if it still exists.
    if (current && _templates.some(t => t.template_name === current)) {
        sel.value = current;
    }
}

function onTemplateSelect() {
    const sel = document.getElementById('template-select');
    const name = sel.value;
    const renameBtn = document.getElementById('rename-template-btn');
    const deleteBtn = document.getElementById('delete-template-btn');

    if (!name) {
        _selectedTemplate = null;
        renameBtn.disabled = true;
        deleteBtn.disabled = true;
        hideMacroVariables();
        // Clear prefilled styling and remove token previews.
        for (const id of ['session-name-input', 'session-cwd-input', 'layout-spec-input']) {
            const el = document.getElementById(id);
            if (el) el.classList.remove('prefilled');
            const wrap = el?.closest('.macro-preview-wrap');
            if (wrap) {
                const display = wrap.querySelector('.macro-preview-display');
                if (display) display.style.display = 'none';
            }
        }
        recomputeFormState();
        return;
    }

    _selectedTemplate = _templates.find(t => t.template_name === name) || null;
    renameBtn.disabled = !_selectedTemplate;
    deleteBtn.disabled = !_selectedTemplate;

    if (_selectedTemplate) {
        loadTemplateIntoForm(_selectedTemplate);
    }
}

function loadTemplateIntoForm(tpl) {
    _editedFields.clear();

    const nameInput = document.getElementById('session-name-input');
    const cwdInput = document.getElementById('session-cwd-input');
    const specInput = document.getElementById('layout-spec-input');

    nameInput.value = tpl.name || '';
    cwdInput.value = tpl.directory || '';

    const layoutType = tpl.layout_type || 'none';
    setActiveLayoutType(layoutType);

    specInput.value = tpl.layout_spec || '';

    // Pre-fill pane commands from template.
    _paneCommands = [...(tpl.pane_commands || [])];
    updateLayoutPreview();

    // Apply prefilled styling to template-loaded fields.
    for (const input of [nameInput, cwdInput, specInput]) {
        if (input.value) {
            input.classList.add('prefilled');
        } else {
            input.classList.remove('prefilled');
        }
    }

    // Render macro variables if the template has any.
    const vars = tpl.variables || [];
    if (vars.length > 0) {
        showMacroVariables(vars);
    } else {
        hideMacroVariables();
    }

    recomputeFormState();
}
function showMacroVariables(vars) {
    const section = document.getElementById('macro-variables-section');
    const container = document.getElementById('macro-variables-container');
    container.innerHTML = '';

    for (const v of vars) {
        const field = document.createElement('div');
        field.className = 'macro-var-field';

        const label = document.createElement('span');
        label.className = 'macro-var-name';
        label.textContent = `{${v}}`;

        const input = document.createElement('input');
        input.className = 'macro-var-input';
        input.type = 'text';
        input.placeholder = `Value for ${v}`;
        input.dataset.varName = v;
        input.required = true;

        // Live: recompute form state and update token previews on input.
        input.addEventListener('input', () => {
            recomputeFormState();
        });

        field.appendChild(label);
        field.appendChild(input);
        container.appendChild(field);
    }
    section.classList.remove('hidden');
}

function hideMacroVariables() {
    const section = document.getElementById('macro-variables-section');
    section.classList.add('hidden');
    document.getElementById('macro-variables-container').innerHTML = '';
}

function collectMacroVariables() {
    const inputs = document.querySelectorAll('#macro-variables-container .macro-var-input');
    const vars = {};
    let valid = true;
    for (const input of inputs) {
        const name = input.dataset.varName;
        const value = input.value.trim();
        if (!value) {
            input.classList.add('invalid');
            valid = false;
        } else {
            input.classList.remove('invalid');
        }
        vars[name] = value;
    }
    return valid ? vars : null;
}

// --- Save template ---

// --- Shared template payload builder (used by save/update/save-as) ---

function buildTemplatePayload() {
    return {
        name: document.getElementById('session-name-input').value.trim(),
        directory: document.getElementById('session-cwd-input').value.trim(),
        layout_type: getActiveLayoutType(),
        layout_spec: document.getElementById('layout-spec-input').value.trim(),
        pane_commands: _paneCommands.slice(),
    };
}

/**
 * Top Save button handler:
 *  - If a template is selected: update it in place (PUT).
 *  - If no template selected: open save-name modal (new template).
 */
function onTopSaveClick() {
    if (_selectedTemplate) {
        updateTemplateInPlace();
    } else {
        openSaveTemplateModal();
    }
}

/**
 * Update the currently selected template in place via PUT.
 */
async function updateTemplateInPlace() {
    if (!_selectedTemplate) return;
    const templateName = _selectedTemplate.template_name;
    const body = buildTemplatePayload();

    try {
        const resp = await fetch(`/api/templates/${encodeURIComponent(templateName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (!resp.ok) {
            showBanner(data.error || `Failed to update template (HTTP ${resp.status})`);
            return;
        }

        showSuccessBanner(`Template "${templateName}" updated.`);
        await fetchTemplates();
        // Re-select and reload the updated template.
        document.getElementById('template-select').value = templateName;
        onTemplateSelect();
    } catch (err) {
        showBanner(`Failed to update template: ${err.message}`);
    }
}

function openSaveTemplateModal() {
    document.getElementById('save-template-modal').classList.remove('hidden');
    const input = document.getElementById('save-template-name-input');
    input.value = '';
    document.getElementById('save-template-error').classList.add('hidden');
    input.focus();
}

function closeSaveTemplateModal() {
    document.getElementById('save-template-modal').classList.add('hidden');
}

async function confirmSaveTemplate() {
    const input = document.getElementById('save-template-name-input');
    const errorEl = document.getElementById('save-template-error');
    const templateName = input.value.trim();

    if (!templateName) {
        errorEl.textContent = 'Template name is required.';
        errorEl.classList.remove('hidden');
        return;
    }

    if (!/^[A-Za-z0-9_-]+$/.test(templateName)) {
        errorEl.textContent = 'Only letters, digits, hyphens, and underscores allowed.';
        errorEl.classList.remove('hidden');
        return;
    }

    const body = {
        template_name: templateName,
        ...buildTemplatePayload(),
    };

    try {
        const resp = await fetch('/api/templates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || `HTTP ${resp.status}`;
            errorEl.classList.remove('hidden');
            return;
        }

        closeSaveTemplateModal();
        showSuccessBanner(`Template "${templateName}" saved.`);
        await fetchTemplates();
        document.getElementById('template-select').value = templateName;
        onTemplateSelect();
    } catch (err) {
        errorEl.textContent = `Failed: ${err.message}`;
        errorEl.classList.remove('hidden');
    }
}

// --- Rename template ---

function openRenameTemplateModal() {
    if (!_selectedTemplate) return;
    document.getElementById('rename-template-modal').classList.remove('hidden');
    const input = document.getElementById('rename-template-name-input');
    input.value = _selectedTemplate.template_name;
    document.getElementById('rename-template-error').classList.add('hidden');
    input.focus();
    input.select();
}

function closeRenameTemplateModal() {
    document.getElementById('rename-template-modal').classList.add('hidden');
}

async function confirmRenameTemplate() {
    if (!_selectedTemplate) return;
    const input = document.getElementById('rename-template-name-input');
    const errorEl = document.getElementById('rename-template-error');
    const newName = input.value.trim();

    if (!newName) {
        errorEl.textContent = 'Name is required.';
        errorEl.classList.remove('hidden');
        return;
    }

    if (!/^[A-Za-z0-9_-]+$/.test(newName)) {
        errorEl.textContent = 'Only letters, digits, hyphens, and underscores allowed.';
        errorEl.classList.remove('hidden');
        return;
    }

    const oldName = _selectedTemplate.template_name;
    try {
        const resp = await fetch(`/api/templates/${encodeURIComponent(oldName)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_name: newName }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || `HTTP ${resp.status}`;
            errorEl.classList.remove('hidden');
            return;
        }

        closeRenameTemplateModal();
        showSuccessBanner(`Template renamed to "${newName}".`);
        await fetchTemplates();
        document.getElementById('template-select').value = newName;
        onTemplateSelect();
    } catch (err) {
        errorEl.textContent = `Failed: ${err.message}`;
        errorEl.classList.remove('hidden');
    }
}

// --- Delete template ---

function openDeleteTemplateModal() {
    if (!_selectedTemplate) return;
    const name = _selectedTemplate.template_name;
    document.getElementById('delete-template-confirm-text').textContent =
        `Are you sure you want to delete template "${name}"? This cannot be undone.`;
    document.getElementById('delete-template-modal').classList.remove('hidden');
}

function closeDeleteTemplateModal() {
    document.getElementById('delete-template-modal').classList.add('hidden');
}

async function confirmDeleteTemplate() {
    if (!_selectedTemplate) return;
    const name = _selectedTemplate.template_name;

    try {
        const resp = await fetch(`/api/templates/${encodeURIComponent(name)}`, {
            method: 'DELETE',
        });

        if (!resp.ok) {
            const data = await resp.json();
            showBanner(data.error || `Failed to delete template (HTTP ${resp.status})`);
            closeDeleteTemplateModal();
            return;
        }

        closeDeleteTemplateModal();
        showSuccessBanner(`Template "${name}" deleted.`);
        _selectedTemplate = null;
        document.getElementById('template-select').value = '';
        onTemplateSelect();
        await fetchTemplates();
    } catch (err) {
        closeDeleteTemplateModal();
        showBanner(`Failed to delete template: ${err.message}`);
    }
}

// ---------------------------------------------------------------------------
// Create session submission
// ---------------------------------------------------------------------------

async function submitNewSession(e) {
    e.preventDefault();

    const nameInput = document.getElementById('session-name-input');
    const cwdInput = document.getElementById('session-cwd-input');
    const specInput = document.getElementById('layout-spec-input');
    const createBtn = document.getElementById('create-session-btn');
    const errorEl = document.getElementById('session-name-error');

    // Run centralized validation.
    const analysis = recomputeFormState();

    // If Create is disabled, do not proceed.
    if (createBtn.disabled) return;

    // --- Template-based launch ---
    if (_selectedTemplate) {
        const vars = collectMacroVariables();
        if (vars === null) {
            errorEl.textContent = 'All template variables must be filled.';
            errorEl.classList.remove('hidden');
            return;
        }

        createBtn.disabled = true;
        createBtn.textContent = 'Creating\u2026';
        const hostId = encodeURIComponent(currentHostId);

        const body = {
            template_name: _selectedTemplate.template_name,
            variables: vars,
        };
        // Include pane command overlay if any are set.
        if (_paneCommands.some(c => c)) {
            body.pane_commands = _paneCommands.slice();
        }

        try {
            const resp = await fetch(`/api/hosts/${hostId}/sessions/from-template`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json();

            if (!resp.ok) {
                errorEl.textContent = data.error || `HTTP ${resp.status}`;
                errorEl.classList.remove('hidden');
                return;
            }

            closeNewSessionModal();
            hideBanner();
            openSession(data.name);
        } catch (err) {
            showBanner(`Failed to create session: ${err.message}`);
        } finally {
            createBtn.disabled = false;
            createBtn.textContent = 'Create';
        }
        return;
    }

    // --- Direct create ---
    const name = nameInput.value.trim();
    if (!name) {
        errorEl.textContent = 'Session name is required.';
        errorEl.classList.remove('hidden');
        nameInput.classList.add('invalid');
        return;
    }
    // Session name format is already validated by recomputeFormState.
    // But double-check the regex to prevent submission of invalid names.
    if (!SESSION_NAME_RE.test(name)) return;

    const layoutType = getActiveLayoutType();
    const layoutSpec = specInput.value.trim();

    if (layoutType !== 'none' && layoutSpec) {
        if (!parseLayoutSpec(layoutSpec)) {
            showBanner('Invalid layout spec: use colon-separated integers or command segments (e.g. 2:1 or vim,jest:3).');
            return;
        }
    }

    const body = { name };
    const cwd = cwdInput.value.trim();
    if (cwd) body.cwd = cwd;
    if (layoutType !== 'none' && layoutSpec) {
        body.layout_type = layoutType;
        body.layout_spec = layoutSpec;
    }
    // Include pane commands if any are set.
    if (_paneCommands.some(c => c)) {
        body.pane_commands = _paneCommands.slice();
    }

    createBtn.disabled = true;
    createBtn.textContent = 'Creating\u2026';

    const hostId = encodeURIComponent(currentHostId);

    try {
        const resp = await fetch(`/api/hosts/${hostId}/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || `HTTP ${resp.status}`;
            errorEl.classList.remove('hidden');
            nameInput.classList.add('invalid');
            return;
        }

        closeNewSessionModal();
        hideBanner();
        openSession(data.name);
    } catch (err) {
        showBanner(`Failed to create session: ${err.message}`);
    } finally {
        createBtn.disabled = false;
        createBtn.textContent = 'Create';
    }
}

// ---------------------------------------------------------------------------
// Add SSH host modal
// ---------------------------------------------------------------------------

function openAddHostModal() {
    document.getElementById('add-host-modal').classList.remove('hidden');
    document.getElementById('host-label-input').value = '';
    document.getElementById('host-alias-input').value = '';
    document.getElementById('add-host-error').classList.add('hidden');
    document.getElementById('host-label-input').focus();
}

function closeAddHostModal() {
    document.getElementById('add-host-modal').classList.add('hidden');
}

async function submitAddHost(e) {
    e.preventDefault();

    const labelInput = document.getElementById('host-label-input');
    const aliasInput = document.getElementById('host-alias-input');
    const addBtn = document.getElementById('add-host-btn');
    const errorEl = document.getElementById('add-host-error');

    const label = labelInput.value.trim();
    const ssh_alias = aliasInput.value.trim();

    if (!label || !ssh_alias) {
        errorEl.textContent = 'Both label and SSH alias are required.';
        errorEl.classList.remove('hidden');
        return;
    }

    addBtn.disabled = true;
    addBtn.textContent = 'Adding\u2026';

    try {
        const resp = await fetch('/api/hosts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label, ssh_alias }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            errorEl.textContent = data.error || `HTTP ${resp.status}`;
            errorEl.classList.remove('hidden');
            return;
        }

        closeAddHostModal();
        showSuccessBanner(`Host "${data.label}" added.`);

        // Refresh hosts and switch to the new host.
        await fetchHosts();
        switchHost(data.id);
    } catch (err) {
        errorEl.textContent = `Failed to add host: ${err.message}`;
        errorEl.classList.remove('hidden');
    } finally {
        addBtn.disabled = false;
        addBtn.textContent = 'Add Host';
    }
}

// ---------------------------------------------------------------------------
// Browser history (back/forward)
// ---------------------------------------------------------------------------

window.addEventListener('popstate', () => {
    const params = new URLSearchParams(window.location.search);
    const host = params.get('host') || 'localhost';
    const session = params.get('session');

    if (host !== currentHostId) {
        currentHostId = host;
        currentPage = 1;
        _cardMap.forEach((card) => card.remove());
        _cardMap.clear();
        renderHostTabs();
    }

    if (session) {
        if (currentSession !== session) {
            currentSession = session;
            document.getElementById('dashboard-view').classList.add('hidden');
            document.getElementById('session-view').classList.remove('hidden');
            document.getElementById('session-title').textContent = session;
            stopSessionPolling();
            loadSessionTerminal(session);
        }
    } else {
        if (currentSession !== null) {
            closeSession();
        } else {
            startSessionPolling();
        }
    }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    // Theme
    initTheme();
    document.getElementById('theme-toggle').addEventListener('click', toggleTheme);

    // Navigation
    document.getElementById('back-btn').addEventListener('click', closeSession);

    // New session modal
    document.getElementById('new-session-btn').addEventListener('click', openNewSessionModal);
    document.getElementById('cancel-new-session').addEventListener('click', closeNewSessionModal);
    document.getElementById('modal-backdrop').addEventListener('click', closeNewSessionModal);
    document.getElementById('new-session-form').addEventListener('submit', submitNewSession);

    // Template controls
    document.getElementById('template-select').addEventListener('change', onTemplateSelect);
    document.getElementById('save-template-btn').addEventListener('click', onTopSaveClick);
    document.getElementById('rename-template-btn').addEventListener('click', openRenameTemplateModal);
    document.getElementById('delete-template-btn').addEventListener('click', openDeleteTemplateModal);

    // Save template modal
    document.getElementById('confirm-save-template').addEventListener('click', confirmSaveTemplate);
    document.getElementById('cancel-save-template').addEventListener('click', closeSaveTemplateModal);
    document.getElementById('save-template-backdrop').addEventListener('click', closeSaveTemplateModal);

    // Rename template modal
    document.getElementById('confirm-rename-template').addEventListener('click', confirmRenameTemplate);
    document.getElementById('cancel-rename-template').addEventListener('click', closeRenameTemplateModal);
    document.getElementById('rename-template-backdrop').addEventListener('click', closeRenameTemplateModal);

    // Delete template modal
    document.getElementById('confirm-delete-template').addEventListener('click', confirmDeleteTemplate);
    document.getElementById('cancel-delete-template').addEventListener('click', closeDeleteTemplateModal);
    document.getElementById('delete-template-backdrop').addEventListener('click', closeDeleteTemplateModal);

    // Session name validation on input — use centralized recompute.
    document.getElementById('session-name-input').addEventListener('input', () => {
        recomputeFormState();
    });

    // Path autocompletion + recompute form state on cwd change.
    const cwdInput = document.getElementById('session-cwd-input');
    let cwdDebounce = null;
    cwdInput.addEventListener('input', () => {
        clearTimeout(cwdDebounce);
        cwdDebounce = setTimeout(onCwdInput, 200);
        recomputeFormState();
    });
    cwdInput.addEventListener('keydown', onCwdKeydown);
    cwdInput.addEventListener('blur', () => {
        setTimeout(hideCompletions, 150);
    });

    // Layout type toggle
    document.querySelectorAll('#layout-type-toggle .layout-type-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            setActiveLayoutType(btn.dataset.layout);
            recomputeFormState();
        });
    });

    // Layout spec preview on input + recompute.
    document.getElementById('layout-spec-input').addEventListener('input', () => {
        updateLayoutPreview();
        recomputeFormState();
    });

    // Save as Template button — always opens save-name modal for a new template.
    document.getElementById('save-as-template-btn').addEventListener('click', openSaveTemplateModal);

    // Setup focus/blur token preview toggles on macro-relevant inputs.
    setupPreviewToggle(document.getElementById('session-name-input'));
    setupPreviewToggle(document.getElementById('session-cwd-input'));
    setupPreviewToggle(document.getElementById('layout-spec-input'));

    // Session-view actions dropdown
    document.getElementById('session-actions-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        toggleSessionViewMenu();
    });
    document.getElementById('session-view-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        closeAllMenus();
        if (currentSession) {
            const card = _cardMap.get(currentSession);
            const indicator = card ? card.querySelector('.attached-indicator') : null;
            const attached = indicator ? indicator.classList.contains('active') : true;
            requestSessionDelete(currentSession, attached, 'session');
        }
    });

    // Delete confirmation modal
    document.getElementById('confirm-delete-btn').addEventListener('click', confirmDeleteSession);
    document.getElementById('cancel-delete-btn').addEventListener('click', closeDeleteModal);
    document.getElementById('delete-modal-backdrop').addEventListener('click', closeDeleteModal);

    // Add host modal
    document.getElementById('add-host-form').addEventListener('submit', submitAddHost);
    document.getElementById('cancel-add-host').addEventListener('click', closeAddHostModal);
    document.getElementById('add-host-backdrop').addEventListener('click', closeAddHostModal);

    // Global: close menus on outside click
    document.addEventListener('click', () => {
        closeAllMenus();
    });

    // Global: Escape key closes modals and menus
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAllMenus();

            // Template modals (check first since they stack on top of new-session modal)
            const saveTemplateModal = document.getElementById('save-template-modal');
            if (!saveTemplateModal.classList.contains('hidden')) {
                closeSaveTemplateModal();
                return;
            }
            const renameTemplateModal = document.getElementById('rename-template-modal');
            if (!renameTemplateModal.classList.contains('hidden')) {
                closeRenameTemplateModal();
                return;
            }
            const deleteTemplateModal = document.getElementById('delete-template-modal');
            if (!deleteTemplateModal.classList.contains('hidden')) {
                closeDeleteTemplateModal();
                return;
            }

            const deleteModal = document.getElementById('delete-session-modal');
            if (!deleteModal.classList.contains('hidden')) {
                closeDeleteModal();
                return;
            }

            const newModal = document.getElementById('new-session-modal');
            if (!newModal.classList.contains('hidden')) {
                closeNewSessionModal();
                return;
            }

            const addHostModal = document.getElementById('add-host-modal');
            if (!addHostModal.classList.contains('hidden')) {
                closeAddHostModal();
                return;
            }
        }
    });

    // Read URL params and init host + session.
    const params = new URLSearchParams(window.location.search);
    const hostParam = params.get('host');
    const sessionParam = params.get('session');

    if (hostParam) {
        currentHostId = hostParam;
    }

    // Fetch hosts, render tabs, then start session polling or open session.
    fetchHosts().then(() => {
        if (sessionParam) {
            openSession(sessionParam);
        } else {
            startSessionPolling();
        }
        startHostPolling();
    });

    // Pause polling when the tab is hidden to save resources.
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopSessionPolling();
            stopHostPolling();
        } else {
            // Tab became visible — resume the appropriate polling.
            if (!currentSession) {
                startSessionPolling();
            }
            startHostPolling();
        }
    });
});
