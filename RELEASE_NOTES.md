## Arena Desktop v5.8.1

Patch release on top of v5.8.0 (built on our 5.7.0). Arena Desktop is an unofficial MIT-licensed fork; Arena controls model availability, quotas and the behaviour of its online services.

### Component detection fix (this release)

- Component detection now also finds cloudflared, Node.js and Python installed system-wide (for example via winget or an official installer), not only the copies installed into the app's data folder. Previously a working system-wide cloudflared was reported as missing — and the Agent Gateway refused to start with "cloudflared is not installed".
- "Check again" in Settings → Components now performs a real re-detection: cached version strings are dropped and, on Windows, the current registry PATH is consulted, so components installed while the app is already running appear without restarting Arena Desktop.
- The first-run components prompt no longer appears when everything required is already installed, and optional components (Node.js, Python) no longer trigger it on their own; cloudflared — required for the Gateway — is the only component that prompts at startup.

### Carried over from v5.8.0

- A parent native Arena Agent can launch up to six child Agent sessions concurrently and continue its own work. Each child gets a separate temporary Gateway; tasks and reports pass through files in the selected Workspace. Children inherit the parent's permission mode, can be stopped, and cannot delegate again. They share the same Workspace: separate gateways do **not** isolate files. Split edits into non-overlapping tasks and review the results. Arena chooses the model for native Agent sessions and Arena rate limits still apply.
- Agent Workspace Sync can automatically start a Cloudflare Quick Tunnel and pass its address and pairing code to the agent. Existing explicit Gateway opt-outs are respected. Gateway is shown as on only after a successful health response; if it becomes unavailable during a turn, start a new session and resend — automatic reconnection during that turn is not implemented.
- Native Agent questions handle Arena's input-only question events. Pending questions, approvals and child controls remain visible even when the chat has no saved messages yet.
- Local API requests use a per-instance token. Workspace HTML previews are scriptless and network-restricted. Windows DPAPI continues to protect stored cookies, including legacy formats. Existing unattended automations run in read-only Plan mode. Code-mode deployment links are checked against Arena's webdev metadata endpoint.
- All 5.7 features remain: voice choices, streaming state, Workspace Git, Compact, HTML export and deep links. Update checks point to `Maxusimlive/Arena-Desktop`, independently of upstream.

### Limits and verification

- Local validation: 381 non-browser tests passed on Linux; 27 browser tests passed on Linux; the new component-detection tests also pass under Windows Python (Wine). The x64 EXE passed PE architecture checks and 72 embedded-content checks. These checks do not replace live account testing.
- Live native Agent child sessions, Cloudflare tunnels and the webdev metadata endpoint have not been verified against a signed-in Arena account in Windows. Local tests use simulated responses; Arena can change its protocol.
- Windows builds are unsigned and can trigger SmartScreen. ARM64 is configured in CI but was not locally built or tested. cloudflared's Windows x64 build requires emulation on Windows ARM64.
- The app does not solve human checks. Complete them in the Arena sign-in window.

### Download

Extract the source ZIP and run `Arena-Desktop.exe` from its top-level folder. The EXE is a GitHub release asset, not a committed source file.

- Windows x64 EXE: `Arena-Desktop.exe` — 30,138,273 bytes; SHA-256 `d8df8aa94a21bd73a5cb58eb13ef1f73cdb8732c12e748b7796f10b224871a81`.

Arena Desktop is not affiliated with or endorsed by Arena AI Inc. Original upstream: [PlayzAe/arena.ai-desktop](https://github.com/PlayzAe/arena.ai-desktop). License: MIT.
