// tmux-dash frontend — vanilla JS, no frameworks
// All DOM IDs must match index.html exactly.

let currentPage = 1;
let currentSession = null;  // session name while in session view, null for dashboard
let sessionPollTimer = null;

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function initTheme() {
    const saved = localStorage.getItem('theme');
    if (saved === 'dark' || saved === 'light') {
        applyTheme(saved);
    } else {
        // Follow system preference when no explicit choice is stored.
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(prefersDark ? 'dark' : 'light');
    }

    // React to OS-level theme changes — only when the user hasn't overridden.
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
        // Sun for dark mode (click to go light), moon for light mode (click to go dark).
        btn.textContent = theme === 'dark' ? '\u2600' : '\u263E';
        btn.title = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }
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

/**
 * Returns a timestamp bucket that changes every ~30 seconds.
 * Used as a cache-buster for thumbnail URLs so the browser
 * re-fetches roughly every 30s without hammering on every poll.
 */
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
    const banner = document.getElementById('warning-banner');
    banner.classList.add('hidden');
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

// ---------------------------------------------------------------------------
// Session list
// ---------------------------------------------------------------------------

async function fetchSessions(page = 1) {
    try {
        const resp = await fetch(`/api/sessions?page=${page}&page_size=8`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        const data = await resp.json();

        hideBanner();
        renderSessions(data);

        const countEl = document.getElementById('session-count');
        countEl.textContent = `${data.total} session${data.total !== 1 ? 's' : ''}`;

        const refreshedEl = document.getElementById('last-refreshed');
        refreshedEl.textContent = `Updated ${formatTime(new Date())}`;
    } catch (err) {
        showBanner(`Failed to fetch sessions: ${err.message}`);
    }
}

function renderSessions(data) {
    const grid = document.getElementById('session-grid');
    const emptyState = document.getElementById('empty-state');
    const paginationEl = document.getElementById('pagination');

    grid.innerHTML = '';

    if (data.sessions.length === 0) {
        emptyState.classList.remove('hidden');
        grid.classList.add('hidden');
        paginationEl.classList.add('hidden');
        return;
    }

    emptyState.classList.add('hidden');
    grid.classList.remove('hidden');

    for (const session of data.sessions) {
        const card = document.createElement('div');
        card.className = 'session-card';

        const attachedClass = session.attached ? 'active' : '';
        // Encode name for the onclick attribute; single-quote-safe via encodeURIComponent.
        const safeName = encodeURIComponent(session.name);

        card.innerHTML = `
            <div class="session-thumbnail-wrap">
                <img class="session-thumbnail"
                     src="/api/sessions/${safeName}/thumbnail.svg?t=${thumbnailBucket()}"
                     alt=""
                     loading="lazy"
                     onerror="this.style.display='none'">
            </div>
            <div class="session-card-body">
                <div class="session-card-header">
                    <span class="session-name">${escapeHtml(session.name)}</span>
                    <span class="attached-indicator ${attachedClass}"></span>
                </div>
                <div class="session-meta">
                    <span class="window-badge">${session.windows} window${session.windows !== 1 ? 's' : ''}</span>
                    <span class="session-age">${formatAge(session.created_epoch)}</span>
                </div>
                <button class="open-btn" data-session="${safeName}">Open</button>
            </div>
        `;

        // Attach listener rather than inlining onclick to avoid XSS via session names.
        card.querySelector('.open-btn').addEventListener('click', () => {
            openSession(session.name);
        });

        grid.appendChild(card);
    }

    renderPagination(data.page, data.pages);
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
            fetchSessions(i);
        });
        paginationEl.appendChild(btn);
    }
}

// ---------------------------------------------------------------------------
// Session view — single terminal iframe
// ---------------------------------------------------------------------------

async function openSession(sessionName) {
    currentSession = sessionName;

    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('session-view').classList.remove('hidden');
    document.getElementById('session-title').textContent = sessionName;

    stopSessionPolling();

    history.pushState(null, '', '/?session=' + encodeURIComponent(sessionName));

    await loadSessionTerminal(sessionName);
}

async function loadSessionTerminal(sessionName) {
    const iframe = document.getElementById('terminal-iframe');

    try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}`);
        if (!resp.ok) {
            iframe.removeAttribute('src');
            showBanner(
                resp.status === 404
                    ? `Session "${sessionName}" no longer exists.`
                    : `Failed to load session: HTTP ${resp.status}`
            );
            return;
        }

        const data = await resp.json();

        if (!data.ttyd_url) {
            iframe.removeAttribute('src');
            showBanner(`Session "${sessionName}" has no terminal available (port not assigned).`);
            return;
        }

        hideBanner();
        iframe.src = data.ttyd_url;
    } catch (err) {
        iframe.removeAttribute('src');
        showBanner(`Failed to connect to session: ${err.message}`);
    }
}

function closeSession() {
    currentSession = null;

    document.getElementById('session-view').classList.add('hidden');
    document.getElementById('dashboard-view').classList.remove('hidden');

    // Clear iframe src to drop the WebSocket connection to ttyd.
    const iframe = document.getElementById('terminal-iframe');
    iframe.removeAttribute('src');

    history.pushState(null, '', '/');

    startSessionPolling();
}

// ---------------------------------------------------------------------------
// HTML escaping — required when building innerHTML from server data
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// New session modal
// ---------------------------------------------------------------------------

const SESSION_NAME_RE = /^[A-Za-z0-9_-]+$/;

let _cwdAbort = null;       // AbortController for in-flight completion fetch
let _cwdActiveIndex = -1;   // keyboard-navigated index inside autocomplete list

function openNewSessionModal() {
    document.getElementById('new-session-modal').classList.remove('hidden');
    document.getElementById('session-name-input').value = '';
    document.getElementById('session-cwd-input').value = '';
    document.getElementById('layout-spec-input').value = '';
    document.getElementById('session-name-error').classList.add('hidden');
    document.getElementById('session-name-input').classList.remove('invalid');
    setActiveLayoutType('none');
    hideCompletions();
    clearLayoutPreview();
    document.getElementById('session-name-input').focus();
}

function closeNewSessionModal() {
    document.getElementById('new-session-modal').classList.add('hidden');
    hideCompletions();
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

function validateSessionName(name) {
    const errorEl = document.getElementById('session-name-error');
    const input = document.getElementById('session-name-input');
    if (!name) {
        errorEl.classList.add('hidden');
        input.classList.remove('invalid');
        return false;
    }
    if (!SESSION_NAME_RE.test(name)) {
        errorEl.textContent = 'Only letters, digits, hyphens, and underscores allowed.';
        errorEl.classList.remove('hidden');
        input.classList.add('invalid');
        return false;
    }
    errorEl.classList.add('hidden');
    input.classList.remove('invalid');
    return true;
}

// ---------------------------------------------------------------------------
// Path autocompletion
// ---------------------------------------------------------------------------

async function fetchCompletions(prefix) {
    if (_cwdAbort) _cwdAbort.abort();
    _cwdAbort = new AbortController();

    try {
        const resp = await fetch(
            `/api/completions/path?prefix=${encodeURIComponent(prefix)}`,
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
            // mousedown so it fires before blur hides the dropdown.
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
    // Trigger another fetch since the selected path is a directory — show its children.
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
    if (!spec) return null;
    const parts = spec.split(':');
    const counts = [];
    for (const p of parts) {
        const n = parseInt(p, 10);
        if (isNaN(n) || n < 1) return null;
        counts.push(n);
    }
    return counts.length > 0 ? counts : null;
}

function updateLayoutPreview() {
    const type = getActiveLayoutType();
    const spec = document.getElementById('layout-spec-input').value.trim();
    const previewEl = document.getElementById('layout-preview');

    if (type === 'none' || !spec) {
        previewEl.classList.add('hidden');
        previewEl.innerHTML = '';
        return;
    }

    const counts = parseLayoutSpec(spec);
    if (!counts) {
        previewEl.classList.add('hidden');
        previewEl.innerHTML = '';
        return;
    }

    previewEl.classList.remove('hidden');
    previewEl.innerHTML = '';

    if (type === 'row') {
        // Each entry in counts = number of horizontal panes in that row.
        for (const paneCount of counts) {
            const row = document.createElement('div');
            row.className = 'layout-preview-row';
            for (let p = 0; p < paneCount; p++) {
                const pane = document.createElement('div');
                pane.className = 'layout-preview-pane';
                row.appendChild(pane);
            }
            previewEl.appendChild(row);
        }
    } else {
        // Column layout: one row containing N columns, each column has M stacked panes.
        const row = document.createElement('div');
        row.className = 'layout-preview-row';
        for (const paneCount of counts) {
            const col = document.createElement('div');
            col.className = 'layout-preview-col';
            for (let p = 0; p < paneCount; p++) {
                const pane = document.createElement('div');
                pane.className = 'layout-preview-pane';
                col.appendChild(pane);
            }
            row.appendChild(col);
        }
        previewEl.appendChild(row);
    }
}

function clearLayoutPreview() {
    const previewEl = document.getElementById('layout-preview');
    previewEl.classList.add('hidden');
    previewEl.innerHTML = '';
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

    const name = nameInput.value.trim();
    if (!validateSessionName(name)) {
        if (!name) {
            errorEl.textContent = 'Session name is required.';
            errorEl.classList.remove('hidden');
            nameInput.classList.add('invalid');
        }
        return;
    }

    const layoutType = getActiveLayoutType();
    const layoutSpec = specInput.value.trim();

    if (layoutType !== 'none' && layoutSpec) {
        if (!parseLayoutSpec(layoutSpec)) {
            showBanner('Invalid layout spec: must be colon-separated positive integers (e.g. 2:1:3).');
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

    createBtn.disabled = true;
    createBtn.textContent = 'Creating\u2026';

    try {
        const resp = await fetch('/api/sessions', {
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

        // Open the newly created session directly.
        openSession(data.name);
    } catch (err) {
        showBanner(`Failed to create session: ${err.message}`);
    } finally {
        createBtn.disabled = false;
        createBtn.textContent = 'Create';
    }
}

// ---------------------------------------------------------------------------
// Browser history (back/forward)
// ---------------------------------------------------------------------------

window.addEventListener('popstate', () => {
    const params = new URLSearchParams(window.location.search);
    const session = params.get('session');

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

    // Session name validation on input
    document.getElementById('session-name-input').addEventListener('input', (e) => {
        validateSessionName(e.target.value.trim());
    });

    // Path autocompletion
    const cwdInput = document.getElementById('session-cwd-input');
    let cwdDebounce = null;
    cwdInput.addEventListener('input', () => {
        clearTimeout(cwdDebounce);
        cwdDebounce = setTimeout(onCwdInput, 200);
    });
    cwdInput.addEventListener('keydown', onCwdKeydown);
    cwdInput.addEventListener('blur', () => {
        // Small delay so mousedown on dropdown item fires first.
        setTimeout(hideCompletions, 150);
    });

    // Layout type toggle
    document.querySelectorAll('#layout-type-toggle .layout-type-btn').forEach(btn => {
        btn.addEventListener('click', () => setActiveLayoutType(btn.dataset.layout));
    });

    // Layout spec preview on input
    document.getElementById('layout-spec-input').addEventListener('input', updateLayoutPreview);

    // Close modal on Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            const modal = document.getElementById('new-session-modal');
            if (!modal.classList.contains('hidden')) {
                closeNewSessionModal();
            }
        }
    });

    // Check URL for ?session= param to restore session view on direct load/reload.
    const params = new URLSearchParams(window.location.search);
    const session = params.get('session');
    if (session) {
        openSession(session);
    } else {
        startSessionPolling();
    }
});
