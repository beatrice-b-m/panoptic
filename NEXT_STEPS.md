# Next Steps

Roadmap for evolving tmux-dash from a single-host session monitor into an extensible agent coordination platform.

---

## 1. Extensible Core Architecture

The overarching goal: build tmux-dash as a lean core, then layer agent coordination and monitoring on top as extensions rather than embedding that logic directly into the core system.

- Define a plugin/extension interface that external modules can hook into.
- Keep session discovery, ttyd lifecycle, and the REST API as core concerns.
- Agent-specific workflows (OMP coordination, test orchestration) belong in the extension layer.

### Feasibility Assessment

**Effort: High | Risk: Medium | Dependencies: None (but shapes everything else)**

The current codebase has no extension mechanism. `server.py` has ~500 lines of handlers, `session_manager.py` has ~720 lines of lifecycle management, and `app.js` has ~960 lines of frontend logic. All are monolithic — no hook points, no event bus, no plugin registration.

**What exists today that helps:**
- The server already has clean separation between `server.py` (HTTP routing), `session_manager.py` (business logic), and `config.py` (constants). This is a reasonable starting point to draw an extension boundary.
- `aiohttp` supports middleware chains and signal handlers (`on_startup`, `on_cleanup`), which are natural hook points.

**What needs to be built:**

1. **Extension loader.** A discovery mechanism — likely a directory (e.g. `extensions/`) scanned at startup. Each extension is a Python module with a conventional entry point (e.g. `register(app, manager)`). The loader imports each module, validates its interface, and calls its registration function.

2. **Lifecycle hooks on SessionManager.** Extensions need to react to session events: `session_discovered`, `session_removed`, `ttyd_spawned`, `ttyd_died`, `poll_completed`. Today, these are just inline code in `_poll_sessions_inner()` and `_spawn_ttyd()`/`_kill_ttyd()`. Refactoring to an observer pattern (callback list or `asyncio` signals) is straightforward but touches the core polling loop.

3. **Route registration for extensions.** Extensions need to add their own HTTP routes. `aiohttp` supports `add_subapp()` or `add_routes()` after app construction — the `build_app()` function would need to call extension registration after core routes.

4. **Frontend extension points.** This is the hard part. The frontend is vanilla JS with no component system. Extension UI (custom panels, sidebar items, card annotations) would require defining DOM mount points and a minimal API for extensions to render into them. Options: (a) named `<div>` slots that extensions populate via their own `<script>` tags, or (b) a thin message-passing API between the core SPA and extension scripts loaded dynamically. Either way, this is the most design-intensive piece.

5. **Extension configuration.** Extensions need their own config namespace. This creates a dependency on item 8b (settings file) unless each extension manages its own config files.

**Implementation sequence:**
1. Define the Python extension interface (abstract base class or protocol).
2. Add lifecycle hook points to `SessionManager` (observer pattern).
3. Build the extension loader in a new `extension_loader.py`.
4. Wire extension route registration into `build_app()`.
5. Define frontend slot convention and dynamic script loading.
6. Extract one existing feature (e.g. thumbnails) as a built-in extension to prove the interface.

**Risks:**
- Designing the hook interface too early locks you into contracts before you know what extensions actually need. The OMP coordination use case should be the driving design input.
- Frontend extensibility in vanilla JS without a component framework is workable but ad hoc. If extensions need complex UI, this becomes a pressure point toward adopting a lightweight framework (Preact, Lit).

**Recommendation:** Start with backend-only extensibility (hooks + routes) and defer frontend extension points until a concrete extension demands them. The first real extension (likely OMP coordination) will reveal what hooks are actually needed.

---

## 2. Session Profiles

Add the ability to save and load session profiles. A profile is a declarative definition (script or config) that specifies:

- Session name.
- Window/pane layout and parameters.
- Commands to inject into specific panes at creation time.

This replaces the current model where sessions must already exist in tmux before the dashboard can see them. Profiles let the dashboard itself create fully configured sessions.

### Feasibility Assessment

**Effort: Medium | Risk: Low | Dependencies: 8b (settings file) for persistence**

**What exists today that helps:**
- `SessionManager.create_session()` already creates tmux sessions with optional `cwd`, `layout_type` (row/col), and `layout_spec` (colon-separated pane counts). The backend machinery for session creation and layout splitting is fully implemented (`_apply_row_layout`, `_apply_col_layout`).
- The frontend already has a "New Session" modal with name input, working directory autocomplete, and layout type/spec controls with live preview.
- The `_run_tmux()` helper makes it trivial to inject commands into panes via `tmux send-keys`.

**What needs to be built:**

1. **Profile schema definition.** A JSON/YAML structure describing a session profile:
   ```json
   {
     "name": "omp-dev",
     "cwd": "~/projects/omp",
     "layout_type": "row",
     "layout_spec": "2:1",
     "pane_commands": {
       "0": "nvim .",
       "1": "npm run dev",
       "2": "tail -f logs/app.log"
     }
   }
   ```
   This is straightforward — it's an extension of the existing `create_session` parameters plus a `pane_commands` map.

2. **Command injection.** After layout creation, iterate `pane_commands` and run `tmux send-keys -t {session}:{window}.{pane} "{command}" Enter` for each. Add a new method to `SessionManager` or extend `create_session()`. The only subtlety is timing — panes need to be ready (shell prompt available) before sending keys. A brief `asyncio.sleep` or a smarter readiness check (capture-pane looking for a prompt) may be needed.

3. **Profile storage.** Two options: (a) individual JSON files in a `profiles/` directory, or (b) entries in the settings file from item 8b. Directory-of-files is simpler and has no dependencies. Settings file is more integrated but couples this to item 8b.

4. **API endpoints.** `GET /api/profiles` (list), `GET /api/profiles/{name}` (detail), `POST /api/profiles` (save), `DELETE /api/profiles/{name}` (remove), `POST /api/profiles/{name}/launch` (create session from profile). All standard CRUD — nothing unusual.

5. **Frontend.** A profile picker in the "New Session" modal or a separate "Profiles" section. The existing modal structure with form groups, validation, and layout preview provides a clear pattern to follow.

6. **"Save current session as profile."** Capture the current session's pane layout and working directories via `tmux list-panes` with appropriate format strings, and generate a profile from it. This is a nice-to-have that makes profiles useful without manually writing JSON.

**Implementation sequence:**
1. Define profile schema and add `profiles/` directory.
2. Add `pane_commands` support to `create_session()` (or a new `launch_profile()` method).
3. Add CRUD API endpoints for profiles.
4. Add "Launch from profile" flow in the frontend.
5. (Optional) "Save as profile" from an existing session.

**Risks:**
- Command injection timing: if a pane's shell hasn't initialized yet, `send-keys` will buffer but may produce unexpected results with slow-starting shells. Mitigation: add a configurable startup delay per pane, or send keys only after detecting a prompt.
- No risk of architectural disruption — this builds cleanly on top of existing create_session infrastructure.

**Recommendation:** This is the most immediately useful item with the lowest risk. It can be implemented incrementally and delivers value at each step. The directory-of-files storage approach avoids blocking on item 8b.

---

## 3. External API for Session Interaction

Expose a backend API that allows external callers to communicate with running sessions:

- Query session state (beyond what `/api/sessions` currently returns).
- Post commands or input into a running session from outside the browser UI.
- Primary use case: an external agent or script talking to a running OMP session without needing the dashboard open.

### Feasibility Assessment

**Effort: Medium | Risk: Medium | Dependencies: None**

**What exists today that helps:**
- `GET /api/sessions` returns name, window count, attached state, creation epoch, and port.
- `GET /api/sessions/{name}/panes` returns pane geometry and ttyd URLs.
- `GET /api/sessions/{name}` returns session detail with proxy-relative ttyd URL.
- `GET /api/sessions/{name}/thumbnail.svg` returns a text snapshot of the session.
- `SessionManager._run_tmux()` is a general-purpose helper for running arbitrary tmux subcommands.
- `SessionManager._capture_pane()` already captures pane content (used for thumbnails).

**What needs to be built:**

1. **Rich session state endpoint.** Extend or add a new endpoint returning: current working directory per pane (`#{pane_current_path}`), running command per pane (`#{pane_current_command}`), pane content (last N lines), tmux options, environment variables. All available via `tmux display-message -p` or `tmux list-panes` with additional format strings. Implementation: add more format fields to the existing `list-panes` call, or add a dedicated `GET /api/sessions/{name}/state` endpoint.

2. **Send-keys endpoint.** `POST /api/sessions/{name}/send-keys` with body `{"pane": 0, "keys": "ls -la\n"}`. Implementation: call `tmux send-keys -t {name}:{window}.{pane} {keys}`. This is roughly 20 lines of handler code plus validation. The existing `_run_tmux()` helper does the heavy lifting.

3. **Capture-pane endpoint.** `GET /api/sessions/{name}/panes/{index}/content` — return the text content of a specific pane. The existing `_capture_pane()` method captures the active pane; it would need to accept a pane target argument. Adding `-t {session}:{window}.{pane}` to the `capture-pane` command is trivial.

4. **Wait-for-content endpoint (optional but valuable).** `POST /api/sessions/{name}/panes/{index}/wait` with body `{"pattern": "\\$\\s*$", "timeout": 10}` — poll pane content until a regex matches or timeout expires. This is the key primitive for reliable automation: "send a command, wait for the prompt to return." Implementation: a polling loop calling `capture-pane` on an interval, matching against the pattern. ~40 lines.

5. **Session environment query.** `tmux show-environment -t {name}` exposes session-level environment variables. Useful for agents that need to inspect session context.

**Security considerations:**
- Send-keys is a command injection surface. Any caller who can POST to this endpoint can execute arbitrary commands in any tmux session. Today the security model is "Tailscale handles it," which is consistent, but this makes the attack surface explicit. Document it clearly. Consider an optional API key (item 7) for this endpoint specifically.
- Rate limiting on send-keys may be warranted to prevent accidental flooding.

**Implementation sequence:**
1. Add `GET /api/sessions/{name}/state` with enriched pane info (cwd, command, content).
2. Add `POST /api/sessions/{name}/send-keys`.
3. Add `GET /api/sessions/{name}/panes/{index}/content` (parameterized capture).
4. (Optional) Add `POST /api/sessions/{name}/panes/{index}/wait` for automation.

**Risks:**
- The send-keys endpoint makes tmux-dash an authenticated remote command execution proxy. If item 7 (auth) is deferred, this is only safe under the current Tailscale-only access model.
- Pane index addressing assumes a single window. Multi-window sessions would need `{window}.{pane}` addressing. The current codebase only models single-window sessions — extending to multi-window is a separate scope expansion.

**Recommendation:** High-value for the OMP coordination use case. Items 1-3 above are quick wins (a few hours each). The wait-for-content primitive (#4) is what makes the API genuinely useful for automation rather than fire-and-forget.

---

## 4. Custom Toolbar Commands

Allow users to register bash commands that appear as clickable buttons in a toolbar above each tmux pane.

- Example: a "Git Pull" button that runs `git pull` in the pane's working directory.
- Commands should be configurable per-profile or globally.
- Each registered command should also be exposed as an API route, so external callers can trigger the same actions programmatically.

### Feasibility Assessment

**Effort: Medium | Risk: Low | Dependencies: 3 (send-keys), 8b (settings file) or 2 (profiles)**

**What exists today that helps:**
- The session view (`session-view` div) has a header with a back button and actions dropdown. A toolbar can be inserted between the header and the terminal iframe.
- Once item 3's send-keys endpoint exists, executing a toolbar command is just a POST to that endpoint.
- The "New Session" modal pattern demonstrates how the frontend handles configurable UI elements.

**What needs to be built:**

1. **Command definition schema.** A list of command objects:
   ```json
   {
     "toolbar_commands": [
       {
         "id": "git-pull",
         "label": "Git Pull",
         "icon": "arrow-down",
         "command": "git pull",
         "confirm": false
       },
       {
         "id": "restart-server",
         "label": "Restart",
         "command": "npm run dev",
         "confirm": true
       }
     ]
   }
   ```
   Storage: in the settings file (item 8b) or in profile definitions (item 2). Without either, a `toolbar.json` file works.

2. **Backend: command registry + execution endpoint.** `GET /api/toolbar-commands` returns available commands. `POST /api/sessions/{name}/exec/{command_id}` runs the command in the session's active pane. The exec endpoint is effectively sugar over the send-keys endpoint from item 3 — it looks up the command by ID, resolves the template (possibly interpolating `{cwd}` or other variables), and calls send-keys.

3. **Frontend: toolbar rendering.** A horizontal bar above the terminal iframe with icon buttons. Fetches command list from the API on session open. Click handler calls the exec endpoint. If `confirm: true`, show a confirmation dialog first. CSS is straightforward — the existing `.session-view-header` pattern extends naturally.

4. **Per-profile commands.** If item 2 (profiles) exists, profiles can include a `toolbar_commands` array that supplements or overrides the global set. The API merges global and profile-specific commands.

5. **Working directory resolution.** The `{cwd}` variable in a command template needs the pane's current working directory. This requires item 3's enriched state endpoint (`#{pane_current_path}`). Without it, commands run relative to wherever the shell happens to be.

**Implementation sequence:**
1. Define command schema and create `toolbar.json` (or integrate into settings file).
2. Add `GET /api/toolbar-commands` endpoint.
3. Add `POST /api/sessions/{name}/exec/{command_id}` endpoint (depends on item 3's send-keys).
4. Render toolbar in session view frontend.
5. Add per-profile command overrides (depends on item 2).

**Risks:**
- Without send-keys (item 3), toolbar commands have no execution mechanism. This item is genuinely blocked on at least the send-keys portion of item 3.
- Command templates with variable interpolation (`{cwd}`, `{session}`) need careful escaping to avoid shell injection. Use `shlex.quote()` on interpolated values.

**Recommendation:** Depends on item 3 for its core functionality. Scoped as a follow-on to the external API work. The UI portion is small; the real work is the command registry and safe execution pipeline.

---

## 5. Native and Mobile App via Tauri

Wrap the current frontend in a Tauri application to support desktop, tablet, and mobile platforms.

**Motivation:** The web-based xterm.js terminal is difficult to use on mobile/tablet devices. There is no access to arrow keys, modifier keys, or other terminal-specific input without a native keyboard layer. Editing input requires deleting everything and retyping. A Tauri wrapper can provide:

- A proper on-screen keyboard with terminal-aware keys.
- Native input handling for arrow keys, Ctrl sequences, etc.
- Installable app for iOS/Android/desktop.

This is a longer-term goal that builds on the other work being stable first.

### Feasibility Assessment

**Revised effort rating: depends entirely on approach chosen (see below)**

My initial evaluation of this item was too pessimistic in some areas and missed a viable alternative that doesn't require Tauri at all. The key insight is architectural: since tmux-dash already reverse-proxies ttyd through `/terminal/{name}/`, the parent page and the ttyd iframe are **same-origin**. This means the dashboard's JavaScript can reach into the iframe and call `window.term.input()` on ttyd's exposed xterm.js instance to inject keystrokes programmatically. This changes the design space significantly.

#### The actual problem

The core issue is not "we need a native app." The core issue is: **mobile soft keyboards do not provide Esc, Tab, Ctrl, Alt, arrow keys, or function keys**, and xterm.js has no built-in solution. The xterm.js project has had an open issue for mobile support since 2017 (xtermjs/xterm.js#1101) and as of 2025/2026, still characterizes mobile touch support as "limited" with "no mobile-optimized interface elements" (xtermjs/xterm.js#5377). Additional mobile problems include predictive text interference (xtermjs/xterm.js#2403) and iOS Smart Keyboard arrow keys not registering due to how iOS dispatches keydown events.

What's needed is an **extra-keys bar** — the pattern used by Termux, Blink Shell, and every serious mobile terminal app. A row of buttons (Esc, Tab, Ctrl, Alt, ←, →, ↑, ↓, etc.) that inject the corresponding escape sequences into the terminal.

There are three approaches to providing this, with dramatically different effort levels:

#### Approach A: Web-only extra-keys toolbar (no Tauri)

**Effort: Low-Medium (1-3 days) | Risk: Low | Dependencies: None**

Add an HTML toolbar to the session view (above or below the terminal iframe) with touch-friendly buttons for terminal-critical keys. On button press, reach into the iframe and call `iframe.contentWindow.term.input(escapeSequence)` to inject the keystroke.

The escape sequences are well-known ANSI codes:
- Arrow keys: `\x1b[A` (up), `\x1b[B` (down), `\x1b[C` (right), `\x1b[D` (left)
- Esc: `\x1b`
- Tab: `\t`
- Ctrl+C: `\x03`
- Ctrl+D: `\x04`
- Ctrl+Z: `\x1a`

Implementation:
1. Add a `<div class="extra-keys-bar">` with buttons to the session view HTML.
2. CSS: fixed-position bar, flexbox row of `min-height: 44px` touch targets, styled consistently with the existing theme. Show only on touch devices (media query or user toggle).
3. JS: on button tap, grab the terminal iframe reference and call `iframe.contentWindow.term.input(sequence)`. For modifier keys (Ctrl, Alt), implement sticky-toggle behavior — tap Ctrl, it highlights, the next key press sends Ctrl+{key}.
4. ~100-150 lines of JS, ~60-80 lines of CSS. No backend changes. No build tools.

**Limitation:** The toolbar sits in the web page, not attached to the OS soft keyboard. On mobile you see: `[header] [extra-keys bar] [terminal iframe] [soft keyboard]`. Some vertical space is consumed. The bar doesn't appear/disappear in sync with the soft keyboard — it's always visible (or toggled manually). This is a cosmetic compromise, not a functional one.

**This approach solves the stated problem (missing terminal keys on mobile) with minimal effort and zero platform dependencies.** It works on any mobile browser, any tablet, any OS. No app stores, no Rust toolchain, no Xcode.

#### Approach B: Tauri with native keyboard accessory (iOS inputAccessoryView / Android toolbar)

**Effort: Medium (5-10 days total, both platforms) | Risk: Medium | Dependencies: Tauri toolchain, Xcode, Android Studio**

Wrap the dashboard in a Tauri app and use native platform APIs to attach the extra-keys row directly to the soft keyboard. On iOS this is `inputAccessoryView` (a UIView attached above the keyboard); on Android it's a custom view in the keyboard area.

My initial estimate of "2-4 weeks per platform" was wrong. An `inputAccessoryView` with terminal key buttons is a standard iOS pattern — roughly 100-200 lines of Swift. It is not a custom IME or custom keyboard extension (those genuinely are multi-week projects). The distinction:

- **inputAccessoryView** (what we need): A UIView attached to the text input's keyboard. When the keyboard appears, your view appears above it. When it dismisses, your view goes with it. This is what Termux, Blink Shell, and iSH use. Standard UIKit, extensively documented, no special entitlements or capabilities required. Effort: ~2-3 days for iOS including Tauri plugin bridge.
- **Custom keyboard extension** (what we do NOT need): A full replacement keyboard distributed as a system extension. Requires a separate app extension target, entitlements, App Store review. Weeks of work. Not applicable here.
- **Custom IME** (also not needed): An input method editor that replaces the system text input pipeline. Even more complex. Irrelevant.

Similarly, on Android, a custom view above the keyboard (not a full InputMethodService) is ~150-250 lines of Kotlin plus the Tauri plugin bridge (~200 lines of Rust + JS glue). Effort: ~3-4 days.

Total realistic effort: **5-10 days for both platforms**, not 4-8 weeks as I initially stated.

However, Tauri introduces significant overhead beyond the keyboard plugin itself:
- Rust toolchain setup and maintenance.
- Tauri project scaffolding (`tauri.conf.json`, build config).
- Xcode project for iOS builds; Android Studio + NDK for Android builds.
- App signing: iOS requires an Apple Developer account ($99/year) and either TestFlight or Ad Hoc provisioning. Android can side-load APKs.
- The frontend can load from the remote server URL (no bundling needed), but Tauri's mobile webview may have quirks with same-origin iframe access that need debugging.
- Ongoing maintenance: OS updates, webview API changes, Tauri version updates.

**Advantage over Approach A:** The extra-keys bar moves with the soft keyboard, feels native, doesn't consume viewport space when the keyboard is hidden. This is a genuine UX improvement, but it's a polish improvement on top of Approach A's functional solution.

#### Approach C: Replace ttyd with a custom xterm.js terminal frontend

**Effort: High (1-3 weeks) | Risk: Medium-High | Dependencies: None**

Instead of embedding ttyd via iframe and trying to inject keys across the boundary, replace ttyd entirely. Build a custom terminal page that includes xterm.js, the WebSocket connection to tmux (via a Python WebSocket-to-pty bridge), and the extra-keys bar — all in one page, no iframes.

This eliminates the iframe indirection and gives full control over the terminal UI, but means:
- Writing a WebSocket-to-pty bridge in Python (or using an existing library like `aiohttp` websockets + `pty` module).
- Managing xterm.js directly (addons, fit, WebGL renderer, etc.) instead of letting ttyd handle it.
- Losing ttyd's built-in features: file transfer (zmodem), reconnection, flow control.
- A real build step for the frontend (xterm.js is an npm package, needs bundling).

This is the cleanest long-term architecture but the highest upfront cost. Not recommended unless the iframe approach (A or B) hits fundamental limitations.

#### Summary comparison

| Approach | Effort | Extra-keys UX | Requires native tooling | Ongoing cost |
| --- | --- | --- | --- | --- |
| A: Web toolbar | 1-3 days | Functional, static bar | No | Near zero |
| B: Tauri + native accessory | 5-10 days | Polished, keyboard-attached | Yes (Rust, Xcode, Android Studio) | Moderate |
| C: Replace ttyd | 1-3 weeks | Full control | No (but adds npm/bundler) | Moderate |

#### Other considerations

- **Desktop Tauri wrapper:** Minimal value. An installable desktop app that opens the dashboard provides almost nothing over a browser bookmark for a single-user tool. The only marginal win is auto-discovery of the server's Tailscale address. Not worth the toolchain overhead on its own.
- **SSH-from-mobile alternative:** For ad-hoc mobile access, using Blink Shell or Termius on iOS (both support Tailscale) to SSH directly into the Mac Mini and `tmux attach` gives native terminal input with zero development effort. This doesn't provide the dashboard's session gallery or monitoring UI, but if the goal is just "interact with a session from my phone," it's immediate.
- **Predictive text interference:** Even with an extra-keys bar, iOS/Android predictive text misbehaves in xterm.js (text appears ahead of cursor, backspace deletes unpredictably). This is an xterm.js bug that neither Tauri nor a web toolbar fixes. Workarounds exist (`<input type="password">` on the hidden textarea, or `inputmode="none"` to suppress the soft keyboard entirely when using only the extra-keys bar) but they need testing.

**Recommendation:** Start with Approach A (web-only extra-keys toolbar). It's 1-3 days of work in the existing vanilla JS codebase with no new dependencies, and it solves the core problem. If the static-bar UX proves insufficient on daily use, Approach B (Tauri native accessory) is a targeted upgrade at 5-10 days — but only pursue it after Approach A has been used and its limitations are concretely felt, not hypothetically anticipated.
---

## 6. Remote Host Support (SSH)

Decouple the system from localhost-only tmux sessions. The dashboard should support multiple configurable SSH hosts:

- Multiple top-level "gallery" views, each corresponding to a different remote host.
- Automatic session discovery across all configured hosts.
- Configurable SSH connection parameters per host.

This is a significant architectural shift from the current single-host design.

### Feasibility Assessment

**Effort: Very High | Risk: High | Dependencies: 8b (settings file), 7 (security)**

**Current architecture assumptions that break:**

The entire codebase assumes localhost. This is not a superficial assumption — it is load-bearing at every layer:

- **SessionManager** calls `tmux list-sessions` as a local subprocess. Remote discovery requires SSH command execution: `ssh user@host tmux list-sessions`. Every `_run_tmux()` call would need to route through either a local or SSH subprocess depending on the host.
- **ttyd processes** are spawned locally and bind to local ports. For remote sessions, you cannot spawn ttyd on the local machine for a remote tmux session. Instead, you need ttyd running on the *remote* host, or you need to proxy the terminal connection over SSH.
- **The port pool** (`7681-7699`) is a local resource. Remote hosts need their own port management — either managed remotely (tmux-dash SSHs in and spawns ttyd on the remote host) or proxied through the dashboard server (SSH tunnels).
- **Pane content capture** (`tmux capture-pane`) runs locally. Remote capture needs SSH.
- **Session creation** (`tmux new-session`) is local. Remote creation needs SSH.

**Two architectural approaches:**

**(A) SSH tunnel approach — remote ttyd:**
- tmux-dash SSHs into each remote host and spawns ttyd there (or discovers already-running ttyd instances).
- Browser connects to remote ttyd ports via SSH tunnel or direct Tailscale access to the remote host.
- Pros: Each host is self-contained; ttyd runs next to tmux (low latency).
- Cons: Requires ttyd installed on every remote host. Port management on remote hosts. Firewall/Tailscale configuration per host.

**(B) SSH proxy approach — local ttyd:**
- tmux-dash spawns local ttyd instances that run `ssh -t user@host tmux attach -t {session}` instead of `tmux attach -t {session}`.
- All ttyd processes are local; SSH handles the remote terminal connection.
- Pros: Only the dashboard host needs ttyd. No port management on remote hosts.
- Cons: Every keystroke goes through an SSH hop. Latency is additive. SSH connection stability becomes critical.

**What needs to be built (either approach):**

1. **Host configuration.** A `hosts` config structure in the settings file (item 8b):
   ```json
   {
     "hosts": [
       {"id": "local", "type": "local", "label": "Mac Mini"},
       {"id": "pi", "type": "ssh", "label": "Raspberry Pi", "host": "pi.ts.net", "user": "pi", "key": "~/.ssh/id_ed25519"}
     ]
   }
   ```

2. **Refactor SessionManager to be per-host.** Today there's one `SessionManager` singleton. With multiple hosts, you need either: (a) one `SessionManager` per host (each with its own session registry, port pool, and poll loop), or (b) a single manager with host-scoped session namespaces. Option (a) is cleaner — instantiate a `LocalSessionManager` and `SSHSessionManager(host_config)` per host, both implementing a common interface.

3. **SSH command execution layer.** Replace direct `asyncio.create_subprocess_exec("tmux", ...)` with an abstraction: `LocalExecutor` for local commands, `SSHExecutor(host_config)` for remote commands. The SSH executor uses `asyncio.create_subprocess_exec("ssh", host, "tmux", ...)`. SSH connection pooling (ControlMaster) is important for performance.

4. **Frontend: host selector / multi-gallery.** The dashboard needs a top-level host picker or tabbed view. Each host has its own session grid. The URL structure changes from `/?session=name` to `/?host=pi&session=name`. The session card and terminal view code needs host context threaded through.

5. **ttyd lifecycle for remote sessions.** For approach (B), ttyd spawning changes from `ttyd ... tmux attach -t {name}` to `ttyd ... ssh -t user@host tmux attach -t {name}`. For approach (A), tmux-dash needs to SSH into the remote host, spawn ttyd there, and track remote PIDs — significantly more complex.

6. **Health checking and reconnection.** SSH connections fail. The system needs to handle: SSH auth failures, network timeouts, connection drops, host unreachable. Each needs a distinct UI state and recovery strategy.

**Implementation sequence:**
1. Define host configuration schema (requires item 8b).
2. Extract `SessionManager` into an interface; implement `LocalSessionManager`.
3. Implement `SSHExecutor` with connection pooling (ControlMaster).
4. Implement `SSHSessionManager` using the SSH executor.
5. Refactor server to manage multiple `SessionManager` instances keyed by host.
6. Add host context to all API endpoints.
7. Refactor frontend for multi-host gallery and host-scoped session views.

**Risks:**
- This is a near-rewrite of the core loop. Every function in `SessionManager` needs a host-aware variant.
- SSH reliability in a long-running daemon is tricky. ControlMaster sockets can go stale, SSH agents can lose keys, network partitions happen. Robust error handling and reconnection is essential and easy to underestimate.
- Testing is harder — you need real remote hosts or a mock SSH environment.
- Security implications are substantial (item 7). The dashboard becomes a gateway to remote systems.

**Recommendation:** This is the highest-effort item. Approach (B) (local ttyd over SSH) is simpler to implement and avoids remote host dependencies. Start with a single additional SSH host before building the full multi-gallery UI. Requires item 8b (settings file) for host configuration. Do not attempt without item 7 (security) at least scoped.

---

## 7. Security and Authentication

Remote host support makes security a first-class concern. Currently, network security is delegated entirely to Tailscale with no application-level auth.

- Conduct a security review once remote SSH support is in scope.
- Add an authentication layer to the frontend and API (at minimum as an opt-in feature).
- Evaluate the threat model for proxying commands to external systems.

### Feasibility Assessment

**Effort: Medium | Risk: Medium | Dependencies: None (but triggered by 3 and 6)**

**Current security posture:**

- Network access: Tailscale-only. All ports (7680 + ttyd 7681-7699) are reachable only within the tailnet. This is a strong network-level control.
- Application auth: None. Anyone on the tailnet can access all endpoints, including the send-keys API (if item 3 is implemented) and session creation/deletion (already implemented).
- TLS: Optional, via `TLS_CERT`/`TLS_KEY` env vars. Already implemented in `server.py` with `_build_ssl_context()`.
- The reverse proxy (`handle_terminal`) forwards arbitrary WebSocket and HTTP traffic to ttyd. No per-session access control.

**When auth becomes necessary:**

- **Item 3 (External API):** The send-keys endpoint is remote code execution. If your tailnet includes shared devices or other users, this is a significant exposure.
- **Item 6 (SSH):** The dashboard becomes an SSH jump box. A compromised browser session could reach remote hosts.
- **Multi-user scenarios:** Even on a personal tailnet, if you share tailnet access with a family member or a second device that could be compromised, application auth adds defense in depth.

**What needs to be built:**

1. **API key authentication (simplest, highest priority).** A static API key stored in config (env var or settings file). API requests include it as a `Bearer` token or `X-API-Key` header. Frontend stores it in `localStorage` after a one-time login prompt. Implementation: a single `aiohttp` middleware that checks the header on all `/api/*` routes. ~30 lines of middleware + a login page/modal.

2. **Session token with expiry.** More robust: the API key authenticates once, server issues a session token (JWT or opaque) with a TTL. Frontend stores the token and refreshes it. This prevents a stolen API key from being usable indefinitely. Implementation: ~100 lines of token management plus cookie/header handling.

3. **Frontend login gate.** A simple full-page login form that appears before the dashboard loads. Submits the API key, receives a session token, stores it, and redirects to the dashboard. The `client_tracking_middleware` would need to enforce authentication before allowing any request through.

4. **Per-endpoint authorization (optional, for item 6).** If multiple hosts have different trust levels, certain endpoints (e.g., send-keys on production hosts) could require elevated authorization. This is overkill for a personal tool but worth considering in the design.

5. **Security audit of existing code.** Review areas of concern:
   - `handle_terminal` reverse proxy: does it sanitize the session name from the URL? Currently it URL-decodes and passes it through. A path-traversal attack is unlikely since it resolves against the session registry, not the filesystem — but worth verifying.
   - `create_session` validates the name against `SESSION_NAME_RE` (`^[A-Za-z0-9_-]+$`) — good.
   - `list_directories` for path autocomplete exposes the filesystem. It skips symlinks and non-directories, but does not restrict the search root. An attacker could enumerate directory names across the entire filesystem. Consider restricting to `$HOME` and below.
   - `_capture_pane` output is rendered into SVG with `html.escape()` — safe.

**Implementation sequence:**
1. Add API key middleware (opt-in via env var `TMUX_DASH_API_KEY`).
2. Add login page/modal to frontend.
3. Review `list_directories` path restriction.
4. Review reverse proxy session name handling.
5. (If item 6) Add per-host authorization scopes.

**Risks:**
- Implementing auth badly is worse than no auth (false sense of security). Keep it simple: static API key + HTTPS (already supported) is sufficient for a personal tool.
- Auth adds friction to every API call and the frontend. Make it opt-in (disabled by default when no API key is configured).

**Recommendation:** Implement the API key middleware now, even before items 3 and 6, as low-effort defense in depth. It's ~30 lines of middleware and a login modal. Make it opt-in. The `list_directories` path restriction should be done regardless — it's a quick fix.

---

## 8. Configuration Overhaul

Three layers of work, in order of priority:

### 8a. Environment Variable Coverage

Every internal configurable value (currently hardcoded constants in `config.py`) should be overridable via environment variable. The `.env` / `.env.example` pattern is already in place but coverage is incomplete.

#### Feasibility Assessment

**Effort: Low | Risk: Very Low | Dependencies: None**

**Current state:**

`config.py` has 13 configuration values. Of these, only 4 are currently overridable via environment variable:
- `BEAMUX_BINARY` — `os.getenv("BEAMUX_BINARY", ...)`
- `TTYD_FONT_FAMILY` — `os.getenv("TTYD_FONT_FAMILY", ...)`
- `TLS_CERT` — `os.getenv("TLS_CERT", ...)`
- `TLS_KEY` — `os.getenv("TLS_KEY", ...)`

Not overridable (hardcoded):
- `DASHBOARD_HOST`, `DASHBOARD_PORT`
- `TTYD_PORT_RANGE_START`, `TTYD_PORT_RANGE_END`
- `TTYD_BIND_HOST`, `TTYD_BINARY`, `TMUX_BINARY`
- `POLL_INTERVAL_ACTIVE`, `POLL_INTERVAL_IDLE`
- `SESSION_PAGE_SIZE`, `LOG_LEVEL`

**What needs to be done:**

Wrap each constant in `os.getenv()` with the current value as default. Add a type-conversion helper for int values:

```python
def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

DASHBOARD_PORT = _env_int("TMUX_DASH_PORT", 7680)
```

Then update `.env.example` to document all available variables.

**Implementation: ~30 minutes.** This is a single-file, mechanical change to `config.py` plus updating `.env.example`.

**Recommendation:** Do this immediately. It's the lowest-effort, lowest-risk item on the entire roadmap and unblocks nothing but improves operational flexibility.

### 8b. Settings File

Introduce a persistent configuration file (e.g., `settings.json`) for values that go beyond simple constants: SSH host definitions, session profiles, toolbar commands, extension config.

#### Feasibility Assessment

**Effort: Medium-Low | Risk: Low | Dependencies: None (but blocks 2, 4, 6)**

**What needs to be built:**

1. **Settings file format and location.** JSON is the pragmatic choice — Python's `json` module is in the standard library, no dependency needed. YAML would require `pyyaml`. TOML is an option (stdlib in Python 3.11+) but less natural for deeply nested structures like host definitions. File location: `settings.json` in the project root, or `~/.config/tmux-dash/settings.json` for XDG compliance.

2. **Settings loader module.** A `settings.py` module that: (a) loads the JSON file at startup, (b) validates the structure against a schema (or at minimum, provides typed accessor methods), (c) provides a `reload()` method for live updates, (d) provides a `save()` method for UI-driven changes (item 8c). The initial version can be ~50-80 lines.

3. **Schema definition.** Start with top-level sections matching planned features:
   ```json
   {
     "hosts": [],
     "profiles": [],
     "toolbar_commands": [],
     "extensions": {}
   }
   ```
   Each section is consumed by its respective feature. Unknown sections are preserved (forward-compatible).

4. **Integration with config.py.** Settings file values should take precedence over env vars, which take precedence over hardcoded defaults. The config loading order: hardcoded default < env var < settings file. This is a design decision — the alternative (env var overrides settings file) is also valid. Pick one and document it.

5. **Write-back support.** If item 8c (UI config) is planned, the settings module needs atomic write-back (write to temp file, rename). This prevents corruption if the server crashes mid-write.

**Implementation sequence:**
1. Create `settings.py` with load/save/get helpers.
2. Define initial schema (can be empty sections).
3. Wire `settings.py` into server startup (load settings, pass to SessionManager).
4. Add `GET /api/settings` and `PUT /api/settings` endpoints (for item 8c).

**Risks:**
- File format lock-in. Changing from JSON to YAML later means migrating all users' settings files. JSON is fine — commit to it.
- Concurrent modification: if the UI and an external editor both modify the file, last-write-wins. Acceptable for a single-user tool.

**Recommendation:** Build this early. It's a small module that unblocks items 2, 4, and 6. The initial version without UI write-back (item 8c) is very simple.

### 8c. UI-Based Configuration

Expose configuration editing through the dashboard UI itself, reading from and writing to the settings file. This is the final step -- it depends on the settings file format being stable.

#### Feasibility Assessment

**Effort: High | Risk: Medium | Dependencies: 8b (settings file), stable feature set**

**What needs to be built:**

1. **Settings page in the frontend.** A new top-level view (or modal) with form controls for each settings section. The existing modal pattern (new session, delete confirmation) provides a starting point, but a full settings UI is significantly more complex — it needs sections/tabs, different input types (text, number, toggles, lists of objects), and validation.

2. **Backend settings API.** `GET /api/settings` returns the current settings. `PUT /api/settings` writes them. Validation on the backend must reject invalid structures and provide useful error messages. Partial updates (`PATCH`) would be more ergonomic but harder to implement correctly.

3. **Settings schema for the frontend.** The frontend needs to know what fields exist, their types, and their constraints. Options: (a) hardcode the form structure in `app.js` (simple but brittle), (b) serve a schema from the API and render forms dynamically (more work but future-proof). For a personal tool, (a) is fine.

4. **Live reload.** When settings change via the UI, affected components need to pick up the changes without a server restart. For example, changing the poll interval should take effect immediately. This means the settings module needs change notification (callbacks or a simple "reload and diff" mechanism).

5. **CSS for the settings UI.** Forms, tabs, nested lists — this is a non-trivial amount of new styling. The existing form styles (from the new session modal) cover basic inputs but not tabbed layouts or complex nested forms.

**Implementation sequence:**
1. Add `GET/PUT /api/settings` endpoints.
2. Build settings page HTML structure and CSS.
3. Wire form controls to API (fetch on load, submit on save).
4. Add validation and error display.
5. Add live reload for changed values.

**Risks:**
- UI configuration is a rabbit hole. Every configurable value you expose needs a form control, validation, help text, and error handling. Scope carefully — start with the most-needed sections only (e.g., host definitions for item 6).
- If the settings schema is still evolving (because items 2, 4, 6 are in progress), the UI will need constant updates. This is why it's correctly positioned as the final step.

**Recommendation:** Defer until items 2, 4, and 6 are stable and the settings schema is settled. Editing `settings.json` in a text editor is adequate for a single-user tool in the interim. The UI is polish, not infrastructure.

---

## Priority and Dependency Map

```
                              ┌─────────────────────┐
                              │  8a. Env Var         │  Effort: Low
                              │  Coverage            │  No dependencies
                              └─────────────────────┘

                              ┌─────────────────────┐
                              │  8b. Settings File   │  Effort: Medium-Low
                              │                      │  No dependencies
                              └──────────┬──────────┘
                                         │ unlocks
            ┌────────────────────────────┼────────────────────────────┐
            │                            │                            │
   ┌────────▼──────────┐    ┌───────────▼──────────┐    ┌───────────▼──────────┐
   │  2. Session        │    │  6. Remote Host       │    │  8c. UI Config       │
   │  Profiles          │    │  Support (SSH)        │    │                      │
   │  Effort: Medium    │    │  Effort: Very High    │    │  Effort: High        │
   └────────┬──────────┘    └───────────┬──────────┘    └──────────────────────┘
            │                            │
            │               ┌───────────▼──────────┐
            │               │  7. Security &        │
            │               │  Authentication       │
            │               │  Effort: Medium       │
            │               └──────────────────────┘
            │
   ┌────────▼──────────┐
   │  3. External API   │  Effort: Medium
   │  (send-keys, etc.) │  No hard dependencies
   └────────┬──────────┘
            │
   ┌────────▼──────────┐
   │  4. Custom Toolbar │  Effort: Medium
   │  Commands          │  Depends on 3
   └───────────────────┘

   ┌──────────────────────┐
   │  1. Extension         │  Effort: High
   │  Architecture         │  No dependencies, but shapes all above
   └──────────────────────┘

   ┌──────────────────────┐
   │  5. Mobile Input      │  Effort: Low (web-only) to Medium (Tauri)
   │  (extra-keys bar)     │  No hard dependencies for web approach
   └──────────────────────┘
```

**Suggested priority ordering (highest value per effort first):**

1. **8a** -- Env var coverage. Trivial effort, immediate operational value.
2. **8b** -- Settings file. Low effort, unblocks multiple future items.
3. **3** -- External API. Medium effort, high value for OMP integration use case.
4. **2** -- Session profiles. Medium effort, high usability value, can start without 8b using file-per-profile approach.
5. **5 (Approach A)** -- Web-only extra-keys toolbar. 1-3 days, solves mobile input with no new dependencies. Moves up significantly from original assessment.
6. **7** -- Security. Medium effort, should be done before or alongside item 3 (send-keys is RCE).
7. **4** -- Toolbar commands. Medium effort, depends on 3.
8. **1** -- Extension architecture. High effort, deferred until concrete extension needs are clear.
9. **6** -- Remote host support. Very high effort, major architectural change.
10. **8c** -- UI configuration. High effort, deferred until schema is stable.
11. **5 (Approach B)** -- Tauri native keyboard accessory. Only if Approach A proves insufficient after real use.