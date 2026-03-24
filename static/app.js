// tmux-dash frontend — vanilla JS, no frameworks
// All DOM IDs must match index.html exactly.

let currentPage = 1;
let currentSession = null;  // session name while in session view, null for dashboard
let sessionPollTimer = null;

// Keyed reconciliation state: session name → card DOM element.
const _cardMap = new Map();

// Fetch sequence counter — prevents stale responses from overwriting newer UI.
let _fetchSeq = 0;

// Pending delete state for the confirmation modal.
let _pendingDelete = null;  // { name, attached, source: 'gallery'|'session' }

// Auto-hide timer for success banner.
let _successTimer = null;

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

// ---------------------------------------------------------------------------
// Refresh status (spinner + last-updated)
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
// Session list — keyed reconciliation
// ---------------------------------------------------------------------------

async function fetchSessions(page = 1) {
    const seq = ++_fetchSeq;
    setRefreshing(true);

    try {
        const resp = await fetch(`/api/sessions?page=${page}&page_size=8`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        const data = await resp.json();

        // Guard: discard if a newer fetch already landed.
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
        // Clear card map since we're showing empty state.
        _cardMap.forEach((card) => card.remove());
        _cardMap.clear();
        return;
    }

    emptyState.classList.add('hidden');
    grid.classList.remove('hidden');

    // Build set of incoming session names for this page.
    const incoming = new Set(data.sessions.map(s => s.name));

    // Remove cards no longer present on this page.
    for (const [name, card] of _cardMap) {
        if (!incoming.has(name)) {
            card.remove();
            _cardMap.delete(name);
        }
    }

    // Create or update cards, then reorder by appending in server order.
    for (const session of data.sessions) {
        let card = _cardMap.get(session.name);
        if (card) {
            updateCard(card, session);
        } else {
            card = createCard(session);
            _cardMap.set(session.name, card);
        }
        // Appending an already-parented node just moves it — no flicker.
        grid.appendChild(card);
    }

    renderPagination(data.page, data.pages);
}

function createCard(session) {
    const card = document.createElement('div');
    card.className = 'session-card';
    card.dataset.sessionName = session.name;

    const safeName = encodeURIComponent(session.name);
    const attachedClass = session.attached ? 'active' : '';

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

    // Wire event listeners.
    card.querySelector('.open-btn').addEventListener('click', () => {
        openSession(session.name);
    });
    card.querySelector('.card-actions-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        toggleCardMenu(card);
    });
    card.querySelector('.card-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        closeAllMenus();
        requestSessionDelete(session.name, session.attached, 'gallery');
    });

    return card;
}

function updateCard(card, session) {
    const safeName = encodeURIComponent(session.name);

    // Update attached indicator.
    const indicator = card.querySelector('.attached-indicator');
    indicator.classList.toggle('active', session.attached);

    // Update window count.
    const badge = card.querySelector('.window-badge');
    const badgeText = `${session.windows} window${session.windows !== 1 ? 's' : ''}`;
    if (badge.textContent !== badgeText) badge.textContent = badgeText;

    // Update session age.
    const age = card.querySelector('.session-age');
    const ageText = formatAge(session.created_epoch);
    if (age.textContent !== ageText) age.textContent = ageText;

    // Refresh thumbnail src only when the bucket changes (every ~30s).
    const img = card.querySelector('.session-thumbnail');
    if (img) {
        const expectedSrc = `/api/sessions/${safeName}/thumbnail.svg?t=${thumbnailBucket()}`;
        if (!img.src.endsWith(expectedSrc)) {
            img.src = expectedSrc;
            img.style.display = '';
        }
    }

    // Update the delete button's closure with current attached state.
    const deleteBtn = card.querySelector('.card-delete-btn');
    if (deleteBtn) {
        // Replace listener by cloning — avoids stale closure on `attached`.
        const fresh = deleteBtn.cloneNode(true);
        fresh.addEventListener('click', (e) => {
            e.stopPropagation();
            closeAllMenus();
            requestSessionDelete(session.name, session.attached, 'gallery');
        });
        deleteBtn.replaceWith(fresh);
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
            // Clear card map on page change — different page, different cards.
            _cardMap.forEach((card) => card.remove());
            _cardMap.clear();
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
    closeAllMenus();

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
    closeAllMenus();

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

    try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(name)}`, {
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
            // Deleting the session we're currently viewing — return to gallery.
            closeSession();
            showSuccessBanner(`Session "${name}" deleted.`);
        } else {
            // Gallery view: remove card immediately, then reconcile via poll.
            const card = _cardMap.get(name);
            if (card) {
                card.remove();
                _cardMap.delete(name);
            }
            showSuccessBanner(`Session "${name}" deleted.`);
            // Trigger an immediate refresh to reconcile counts and pagination.
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

    // Session-view actions dropdown
    document.getElementById('session-actions-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        toggleSessionViewMenu();
    });
    document.getElementById('session-view-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        closeAllMenus();
        if (currentSession) {
            // Look up attached state from the card map or fetch it.
            // The session-view doesn't cache attached state, so default to
            // checking the card if it exists; otherwise assume possibly attached.
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

    // Global: close menus on outside click
    document.addEventListener('click', () => {
        closeAllMenus();
    });

    // Global: Escape key closes modals and menus
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAllMenus();

            const deleteModal = document.getElementById('delete-session-modal');
            if (!deleteModal.classList.contains('hidden')) {
                closeDeleteModal();
                return;
            }

            const newModal = document.getElementById('new-session-modal');
            if (!newModal.classList.contains('hidden')) {
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
