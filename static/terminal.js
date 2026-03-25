/**
 * terminal.js — Direct xterm.js terminal that speaks ttyd's WebSocket protocol.
 *
 * Replaces the previous iframe-based approach so the terminal lives in the
 * same document context as the dashboard.  This gives us reliable clipboard
 * access (Shift+select → copy, OSC 52) and avoids iframe permission issues.
 *
 * ttyd protocol (binary WebSocket, subprotocol "tty"):
 *   Server → Client: prefix byte  '0' = output, '1' = title, '2' = prefs
 *   Client → Server: prefix byte  '0' = input,  '1' = resize, '2' = pause, '3' = resume
 *   First client message after open: JSON { AuthToken, columns, rows }
 */

import { Terminal }       from '/static/vendor/xterm/xterm.mjs';
import { FitAddon }       from '/static/vendor/xterm/addon-fit.mjs';
import { ClipboardAddon } from '/static/vendor/xterm/addon-clipboard.mjs';
import { WebglAddon }     from '/static/vendor/xterm/addon-webgl.mjs';
import { WebLinksAddon }  from '/static/vendor/xterm/addon-web-links.mjs';

// ---------------------------------------------------------------------------
// ttyd command bytes (ASCII character codes)
// ---------------------------------------------------------------------------
const CMD = {
    // server → client
    OUTPUT:     '0',
    TITLE:      '1',
    PREFS:      '2',
    // client → server
    INPUT:      '0',
    RESIZE:     '1',
    PAUSE:      '2',
    RESUME:     '3',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** Send a prefixed binary message over the WebSocket. */
function wsSend(ws, cmdChar, payload) {
    if (ws.readyState !== WebSocket.OPEN) return;

    if (typeof payload === 'string') {
        const buf = new Uint8Array(payload.length * 3 + 1);
        buf[0] = cmdChar.charCodeAt(0);
        const { written } = encoder.encodeInto(payload, buf.subarray(1));
        ws.send(buf.subarray(0, written + 1));
    } else {
        const buf = new Uint8Array(payload.length + 1);
        buf[0] = cmdChar.charCodeAt(0);
        buf.set(payload, 1);
        ws.send(buf);
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Create and connect a terminal inside `container`.
 *
 * @param {HTMLElement} container  — DOM element to host the terminal
 * @param {string}      baseUrl   — ttyd base path, e.g. "/terminal/local/mysess/"
 * @param {object}     [opts]     — optional overrides
 * @param {string}     [opts.fontFamily]
 * @param {number}     [opts.fontSize]
 * @returns {TerminalHandle}
 */
export function createTerminal(container, baseUrl, opts = {}) {
    const handle = new TerminalHandle(container, baseUrl, opts);
    handle._connect();
    return handle;
}

export class TerminalHandle {
    constructor(container, baseUrl, opts) {
        this._container = container;
        this._baseUrl = baseUrl.endsWith('/') ? baseUrl : baseUrl + '/';
        this._opts = opts;
        this._disposed = false;

        // Will be populated during connect
        this._terminal = null;
        this._fitAddon = null;
        this._ws = null;
        this._resizeObserver = null;

        // Flow control state (populated from server PREFS message)
        this._flowLimit    = 128 * 1024;  // bytes before engaging flow control
        this._flowHigh     = 5;           // pending writes before PAUSE
        this._flowLow      = 2;           // pending writes before RESUME
        this._written      = 0;
        this._pending      = 0;
    }

    /** @internal */
    async _connect() {
        if (this._disposed) return;

        // 1. Fetch auth token from ttyd's token endpoint.
        let token = '';
        try {
            const resp = await fetch(this._baseUrl + 'token');
            if (resp.ok) {
                const json = await resp.json();
                token = json.token || '';
            }
        } catch { /* ttyd may not require auth */ }

        if (this._disposed) return;

        // 2. Create the xterm.js terminal.
        const term = new Terminal({
            allowProposedApi: true,
            fontFamily:  this._opts.fontFamily || '"SF Mono", Menlo, Consolas, monospace',
            fontSize:    this._opts.fontSize   || 13,
            cursorBlink: true,
            scrollback:  5000,
            theme: {
                background: '#000000',
            },
        });
        this._terminal = term;

        const fitAddon  = new FitAddon();
        const clipAddon = new ClipboardAddon();
        const linksAddon = new WebLinksAddon();
        this._fitAddon = fitAddon;

        term.loadAddon(fitAddon);
        term.loadAddon(clipAddon);
        term.loadAddon(linksAddon);

        term.open(this._container);

        // Try WebGL renderer; fall back to canvas/DOM silently.
        try {
            const webgl = new WebglAddon();
            webgl.onContextLoss(() => { webgl.dispose(); });
            term.loadAddon(webgl);
        } catch { /* WebGL unavailable — DOM renderer is fine */ }

        fitAddon.fit();

        // 3. Copy-on-select: write selected text to clipboard automatically.
        term.onSelectionChange(() => {
            const sel = term.getSelection();
            if (!sel) return;
            navigator.clipboard.writeText(sel).catch(() => {
                // Fallback for non-secure contexts: use legacy execCommand.
                try { document.execCommand('copy'); } catch { /* swallow */ }
            });
        });

        // 4. Observe container resize → refit terminal.
        this._resizeObserver = new ResizeObserver(() => {
            if (!this._disposed) fitAddon.fit();
        });
        this._resizeObserver.observe(this._container);

        // 5. Open WebSocket to ttyd.
        const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProto}//${location.host}${this._baseUrl}ws`;
        const ws = new WebSocket(wsUrl, ['tty']);
        ws.binaryType = 'arraybuffer';
        this._ws = ws;

        ws.addEventListener('open', () => {
            // First message: auth + initial dimensions.
            const auth = JSON.stringify({
                AuthToken: token,
                columns: term.cols,
                rows: term.rows,
            });
            ws.send(encoder.encode(auth));

            // Forward terminal input to ttyd.
            term.onData(data  => wsSend(ws, CMD.INPUT, data));
            term.onBinary(data => {
                const bytes = Uint8Array.from(data, c => c.charCodeAt(0));
                wsSend(ws, CMD.INPUT, bytes);
            });

            // Forward resize events.
            term.onResize(({ cols, rows }) => {
                wsSend(ws, CMD.RESIZE, JSON.stringify({ columns: cols, rows: rows }));
            });

            term.focus();
        });

        ws.addEventListener('message', (evt) => {
            const raw  = new Uint8Array(evt.data);
            const cmd  = String.fromCharCode(raw[0]);
            const data = evt.data.slice(1);

            switch (cmd) {
                case CMD.OUTPUT:
                    this._writeWithFlowControl(data);
                    break;
                case CMD.TITLE:
                    // Ignore — dashboard manages its own title.
                    break;
                case CMD.PREFS: {
                    try {
                        const prefs = JSON.parse(decoder.decode(data));
                        if (prefs.fontFamily)  term.options.fontFamily = prefs.fontFamily;
                        if (prefs.fontSize)    term.options.fontSize   = prefs.fontSize;
                        if (prefs.flowControl) {
                            this._flowLimit = prefs.flowControl.limit    ?? this._flowLimit;
                            this._flowHigh  = prefs.flowControl.highWater ?? this._flowHigh;
                            this._flowLow   = prefs.flowControl.lowWater  ?? this._flowLow;
                        }
                        fitAddon.fit();
                    } catch { /* malformed prefs — ignore */ }
                    break;
                }
            }
        });

        ws.addEventListener('close', () => {
            if (!this._disposed) {
                term.options.disableStdin = true;
            }
        });

        ws.addEventListener('error', () => {
            // Error events are always followed by close; nothing extra to do.
        });
    }

    /** Write output with ttyd-compatible flow control. */
    _writeWithFlowControl(data) {
        const term = this._terminal;
        const ws   = this._ws;
        if (!term || !ws) return;

        this._written += data.byteLength;
        if (this._written > this._flowLimit) {
            term.write(new Uint8Array(data), () => {
                this._pending = Math.max(this._pending - 1, 0);
                if (this._pending < this._flowLow) {
                    wsSend(ws, CMD.RESUME, '');
                }
            });
            this._pending++;
            this._written = 0;
            if (this._pending > this._flowHigh) {
                wsSend(ws, CMD.PAUSE, '');
            }
        } else {
            term.write(new Uint8Array(data));
        }
    }

    /** Refit the terminal to its container. Call after layout changes. */
    fit() {
        if (this._fitAddon && !this._disposed) {
            this._fitAddon.fit();
        }
    }

    /** Focus the terminal. */
    focus() {
        if (this._terminal && !this._disposed) {
            this._terminal.focus();
        }
    }

    /** Tear down the terminal, close the WebSocket, remove DOM nodes. */
    dispose() {
        if (this._disposed) return;
        this._disposed = true;

        this._resizeObserver?.disconnect();
        this._resizeObserver = null;

        if (this._ws) {
            if (this._ws.readyState === WebSocket.OPEN ||
                this._ws.readyState === WebSocket.CONNECTING) {
                this._ws.close();
            }
            this._ws = null;
        }

        if (this._terminal) {
            this._terminal.dispose();
            this._terminal = null;
        }

        this._fitAddon = null;

        // Clear any DOM children xterm.js left behind.
        this._container.replaceChildren();
    }
}
