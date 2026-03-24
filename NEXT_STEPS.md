# Next Steps

Roadmap for evolving tmux-dash from a single-host session monitor into an extensible agent coordination platform.

---

## 1. Extensible Core Architecture

The overarching goal: build tmux-dash as a lean core, then layer agent coordination and monitoring on top as extensions rather than embedding that logic directly into the core system.

- Define a plugin/extension interface that external modules can hook into.
- Keep session discovery, ttyd lifecycle, and the REST API as core concerns.
- Agent-specific workflows (OMP coordination, test orchestration) belong in the extension layer.

## 2. Session Profiles

Add the ability to save and load session profiles. A profile is a declarative definition (script or config) that specifies:

- Session name.
- Window/pane layout and parameters.
- Commands to inject into specific panes at creation time.

This replaces the current model where sessions must already exist in tmux before the dashboard can see them. Profiles let the dashboard itself create fully configured sessions.

## 3. External API for Session Interaction

Expose a backend API that allows external callers to communicate with running sessions:

- Query session state (beyond what `/api/sessions` currently returns).
- Post commands or input into a running session from outside the browser UI.
- Primary use case: an external agent or script talking to a running OMP session without needing the dashboard open.

## 4. Custom Toolbar Commands

Allow users to register bash commands that appear as clickable buttons in a toolbar above each tmux pane.

- Example: a "Git Pull" button that runs `git pull` in the pane's working directory.
- Commands should be configurable per-profile or globally.
- Each registered command should also be exposed as an API route, so external callers can trigger the same actions programmatically.

## 5. Native and Mobile App via Tauri

Wrap the current frontend in a Tauri application to support desktop, tablet, and mobile platforms.

**Motivation:** The web-based xterm.js terminal is difficult to use on mobile/tablet devices. There is no access to arrow keys, modifier keys, or other terminal-specific input without a native keyboard layer. Editing input requires deleting everything and retyping. A Tauri wrapper can provide:

- A proper on-screen keyboard with terminal-aware keys.
- Native input handling for arrow keys, Ctrl sequences, etc.
- Installable app for iOS/Android/desktop.

This is a longer-term goal that builds on the other work being stable first.

## 6. Remote Host Support (SSH)

Decouple the system from localhost-only tmux sessions. The dashboard should support multiple configurable SSH hosts:

- Multiple top-level "gallery" views, each corresponding to a different remote host.
- Automatic session discovery across all configured hosts.
- Configurable SSH connection parameters per host.

This is a significant architectural shift from the current single-host design.

## 7. Security and Authentication

Remote host support makes security a first-class concern. Currently, network security is delegated entirely to Tailscale with no application-level auth.

- Conduct a security review once remote SSH support is in scope.
- Add an authentication layer to the frontend and API (at minimum as an opt-in feature).
- Evaluate the threat model for proxying commands to external systems.

## 8. Configuration Overhaul

Three layers of work, in order of priority:

### 8a. Environment Variable Coverage

Every internal configurable value (currently hardcoded constants in `config.py`) should be overridable via environment variable. The `.env` / `.env.example` pattern is already in place but coverage is incomplete.

### 8b. Settings File

Introduce a persistent configuration file (e.g., `settings.json`) for values that go beyond simple constants: SSH host definitions, session profiles, toolbar commands, extension config.

### 8c. UI-Based Configuration

Expose configuration editing through the dashboard UI itself, reading from and writing to the settings file. This is the final step -- it depends on the settings file format being stable.
