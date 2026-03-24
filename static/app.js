// tmux-dash frontend — vanilla JS, no frameworks
// All DOM IDs must match index.html exactly.

let currentPage = 1;
let currentSession = null;  // session name while in session view, null for dashboard
let sessionPollTimer = null;

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
