# Next Steps

Planned features to implement when directed. Not yet in progress.

---

## 1. Spawn New tmux Sessions from the Dashboard

Add the ability to create new tmux sessions directly from the browser UI, with a configurable starting directory.

### Requirements

- **New session API endpoint** (e.g. `POST /api/sessions`) accepting a session name and an optional working directory path.
- **Directory autocompletion in the UI:** As the user types a path in the working directory field, the frontend should offer completions. This requires a server-side endpoint (e.g. `GET /api/completions/path?prefix=...`) that lists matching directories on the host filesystem.
- **Session name validation:** Apply the same constraints as `beamux` (`[A-Za-z0-9_-]+`), with inline validation feedback in the UI before submission.
- **Server-side:** `session_manager.py` gains a `create_session(name, cwd=None)` method that runs `tmux new-session -d -s {name} -c {cwd}`, then immediately enters the normal discovery/ttyd-spawn flow.
- **Frontend:** A "New Session" affordance on the dashboard (button or card) that opens a form with name input and directory input (with autocompletion).

### Notes

- The directory autocompletion endpoint must restrict traversal to real directories (not symlink loops) and should have a result limit to avoid returning thousands of entries.
- If `cwd` is provided but does not exist, the server should reject the request with a clear error rather than letting tmux fall back silently.

---

## 2. Configurable Pane Layouts for New Sessions

When creating a new session, allow the user to specify an initial pane layout.

### Requirements

- **Layout specification format:** Use the same colon-separated format as `beamux`:
  - Row layout (`-rl`): e.g. `2:1` means row 0 has 2 side-by-side panes, row 1 has 1 full-width pane.
  - Column layout (`-cl`): e.g. `2:1` means col 0 has 2 stacked panes, col 1 has 1 full-height pane.
- **Integration with `beamux`:** The `beamux` command at `~/AgentFiles/projects/bea-sh/tools/beamux/beamux` already implements session creation with layout application (`apply_row_layout`, `apply_col_layout`). Two viable approaches:
  1. **Shell out to `beamux`:** The server calls `beamux <name> -rl <spec>` (or `-cl`) directly. Simpler, reuses proven logic, but adds a dependency on the external script path and its conventions.
  2. **Port the layout logic to Python:** Translate the `apply_row_layout` / `apply_col_layout` functions into `session_manager.py` using the same sequence of `tmux split-window` and `tmux select-layout` calls. More self-contained, but duplicates logic.
  - **Recommendation:** Shell out to `beamux` for session creation when a layout is requested. It already validates the spec, creates the session, and applies the layout atomically. Configure its path via `config.py` (e.g. `BEAMUX_BINARY`). Fall back to plain `tmux new-session` if `beamux` is not found and no layout was requested; error if layout was requested but `beamux` is unavailable.
- **Frontend:** The session creation form (from feature 1) gains a layout section with:
  - A toggle or dropdown for layout type (none / row / column).
  - A text input for the layout spec (e.g. `2:1:3`).
  - A visual preview of the resulting pane grid (CSS grid matching the spec) before submission, so the user sees what they'll get.
- **API extension:** The `POST /api/sessions` endpoint accepts optional `layout_type` (`"row"` | `"col"`) and `layout_spec` (string) fields alongside `name` and `cwd`.

### `beamux` Reference

Located at `~/AgentFiles/projects/bea-sh/tools/beamux/beamux`. Key behaviors:

- `beamux <name>` -- attach or create a bare session.
- `beamux <name> -rl <spec>` -- create with row layout (colon-separated pane counts per row).
- `beamux <name> -cl <spec>` -- create with column layout (colon-separated pane counts per column).
- Layout flags are ignored if the session already exists.
- Session names must match `[A-Za-z0-9_-]+`.
- Layout spec is validated: must be colon-separated positive integers.
- Layout application uses `tmux split-window` + `tmux select-layout even-vertical` / `even-horizontal`, then splits each row/column anchor pane into sub-panes.
- Has a zsh completion function (`_beamux`) that completes session names from `tmux list-sessions`.

### Notes

- `beamux` currently has no `-c` / `--cwd` flag. If the shell-out approach is chosen and directory support is needed at layout-creation time, either extend `beamux` to accept a `-c` flag, or create the session with `tmux new-session -d -s <name> -c <cwd>` first and then call `beamux <name> -rl <spec>` (which will detect the existing session and skip creation but also skip layout -- so this won't work as-is). This gap needs resolution at implementation time: the simplest fix is to add `-c <dir>` support to `beamux`.
