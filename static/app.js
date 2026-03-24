// tmux-dash frontend — vanilla JS, no frameworks
// All DOM IDs must match index.html exactly.

let currentPage = 1;
let currentSession = null;  // session name while in session view, null for dashboard
let sessionPollTimer = null;
let panePollTimer = null;

// Track pane IDs currently rendered to avoid recreating iframes unnecessarily.
// Recreating an iframe resets the embedded ttyd terminal state.
let renderedPaneIds = [];

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

function startPanePolling(sessionName) {
    stopPanePolling();
    fetchPanes(sessionName);
    panePollTimer = setInterval(() => fetchPanes(sessionName), 5_000);
}

function stopPanePolling() {
    if (panePollTimer !== null) {
        clearInterval(panePollTimer);
        panePollTimer = null;
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
            <div class="session-card-header">
                <span class="session-name">${escapeHtml(session.name)}</span>
                <span class="attached-indicator ${attachedClass}"></span>
            </div>
            <div class="session-meta">
                <span class="window-badge">${session.windows} window${session.windows !== 1 ? 's' : ''}</span>
                <span class="session-age">${formatAge(session.created_epoch)}</span>
            </div>
            <button class="open-btn" data-session="${safeName}">Open</button>
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
// Session view
// ---------------------------------------------------------------------------

async function openSession(sessionName) {
    currentSession = sessionName;

    document.getElementById('dashboard-view').classList.add('hidden');
    document.getElementById('session-view').classList.remove('hidden');
    document.getElementById('session-title').textContent = sessionName;

    stopSessionPolling();
    renderedPaneIds = [];

    history.pushState(null, '', '/?session=' + encodeURIComponent(sessionName));

    startPanePolling(sessionName);
}

async function fetchPanes(sessionName) {
    try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(sessionName)}/panes`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        const data = await resp.json();

        hideBanner();
        renderPanes(data);
    } catch (err) {
        showBanner(`Failed to fetch panes for "${sessionName}": ${err.message}`);
    }
}

function renderPanes(data) {
    const grid = document.getElementById('pane-grid');
    const panes = data.panes;

    if (panes.length === 0) {
        grid.innerHTML = '';
        renderedPaneIds = [];
        return;
    }

    const incomingIds = panes.map(p => p.id);
    const paneSetChanged = !arraysEqual(renderedPaneIds, incomingIds);

    if (paneSetChanged) {
        // Full rebuild: pane set changed (splits, closes). Iframes are recreated.
        grid.innerHTML = '';
        renderedPaneIds = incomingIds;
        applyGridLayout(grid, panes);

        for (const pane of panes) {
            const container = buildPaneContainer(pane);
            grid.appendChild(container);
        }
    } else {
        // Pane set is unchanged — update layout and labels only; leave iframes alone.
        applyGridLayout(grid, panes);

        const containers = grid.querySelectorAll('.pane-container');
        containers.forEach((container, idx) => {
            const pane = panes[idx];
            if (!pane) return;

            // Update active class
            container.className = 'pane-container' + (pane.active ? ' active' : '');

            // Update label
            const label = container.querySelector('.pane-label');
            if (label) label.textContent = paneLabel(pane);
        });
    }
}

function paneLabel(pane) {
    return `${pane.title} (pane ${pane.index})`;
}

function buildPaneContainer(pane) {
    const container = document.createElement('div');
    container.className = 'pane-container' + (pane.active ? ' active' : '');

    const label = document.createElement('div');
    label.className = 'pane-label';
    label.textContent = paneLabel(pane);

    const iframe = document.createElement('iframe');
    iframe.src = pane.ttyd_url;
    iframe.className = 'pane-iframe';
    iframe.setAttribute('allowfullscreen', '');

    container.appendChild(label);
    container.appendChild(iframe);
    return container;
}

function applyGridLayout(grid, panes) {
    if (panes.length === 1) {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = '1fr';
        return;
    }

    // Derive proportional column widths from pane widths.
    // Panes in tmux share rows; group them by their horizontal bands.
    // Simple heuristic: unique widths define columns proportionally.
    // Sum of all pane widths in a row equals the terminal width.
    // Use total width from first pane row as denominator.
    const totalWidth = panes.reduce((sum, p) => sum + p.width, 0);
    const totalHeight = panes.reduce((sum, p) => sum + p.height, 0);

    // Build proportional fr units: each pane's column share.
    const colFrs = panes.map(p => `${p.width}fr`).join(' ');
    const rowFrs = panes.map(p => `${p.height}fr`).join(' ');

    // For a simple single-row layout: one column per pane.
    // For a multi-row layout the grid will auto-flow.
    // This covers the most common tmux split patterns.
    const uniqueWidths = [...new Set(panes.map(p => p.width))];
    if (uniqueWidths.length === 1) {
        // All panes same width → single column, stacked vertically.
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = panes.map(p => `${p.height}fr`).join(' ');
    } else {
        // Mixed widths → lay out proportionally in a single row
        // (covers left/right vertical split cases).
        grid.style.gridTemplateColumns = panes.map(p => `${p.width}fr`).join(' ');
        grid.style.gridTemplateRows = '1fr';
    }
}

function closeSession() {
    currentSession = null;

    document.getElementById('session-view').classList.add('hidden');
    document.getElementById('dashboard-view').classList.remove('hidden');

    // Clear pane grid
    document.getElementById('pane-grid').innerHTML = '';
    renderedPaneIds = [];

    stopPanePolling();

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
// Array equality (order-sensitive, shallow)
// ---------------------------------------------------------------------------

function arraysEqual(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Browser history (back/forward)
// ---------------------------------------------------------------------------

window.addEventListener('popstate', () => {
    const params = new URLSearchParams(window.location.search);
    const session = params.get('session');

    if (session) {
        // If already in session view for a different session, switch.
        if (currentSession !== session) {
            stopPanePolling();
            renderedPaneIds = [];
            currentSession = session;
            document.getElementById('dashboard-view').classList.add('hidden');
            document.getElementById('session-view').classList.remove('hidden');
            document.getElementById('session-title').textContent = session;
            stopSessionPolling();
            startPanePolling(session);
        }
    } else {
        // Navigate back to dashboard.
        if (currentSession !== null) {
            currentSession = null;
            document.getElementById('session-view').classList.add('hidden');
            document.getElementById('dashboard-view').classList.remove('hidden');
            document.getElementById('pane-grid').innerHTML = '';
            renderedPaneIds = [];
            stopPanePolling();
            startSessionPolling();
        }
    }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('back-btn').addEventListener('click', closeSession);

    // Check URL for ?session= param to restore session view on direct load/reload.
    const params = new URLSearchParams(window.location.search);
    const session = params.get('session');
    if (session) {
        openSession(session);
    } else {
        startSessionPolling();
    }
});
