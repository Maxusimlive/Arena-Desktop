## Arena Desktop v6.0.0

Major release rolling up everything since 5.0 (full changelog below). Arena Desktop is an unofficial MIT-licensed fork; Arena controls model availability, quotas and the behaviour of its online services.

### New in 6.0.0

- **MCP signature on agent tool cards.** When the Arena Agent uses an MCP tool through the Gateway from inside a bash call, the card now says so — e.g. `MCP: agent-desktop · desktop_launch, desktop_screenshot` — so you can see at a glance which local tools a given step used. Cards that use no MCP stay unlabelled.
- **Sub-agent chats can be deleted.** The delete button on a nested sub-agent chat silently did nothing: the confirmation dialog only searched the top-level chat list. It now finds chats nested under their parent, removes them immediately from the sidebar, deletes locally and archives on Arena like any chat — and if that sub-agent is still running, deleting its chat stops it first so it cannot keep writing into a deleted conversation.
- Carries all 5.8.x Gateway hardening (below), confirmed working in the field with native sub-agents.

### Everything added since 5.0

**v5.0.0 — speed, safety, chats, models, desktop**
- Agent file checkpoints with Undo, diffs in permission cards, parallel research sub-agents with time budgets, batched/deduplicated file reads, 90 s stream watchdog, idle-timeout shell with interactive commands blocked, MCP call timeouts, soft anti-loop.
- Full-text search across local chats, chat export to Markdown, fork a chat from any message, workspace folder/file snapshot as an attachment for tool-less modes, anti-autoscroll while streaming, previous/next message jumps.
- Favorite/hidden models with Vision/PDF capability badges, manual account-switch hint on 429, "Ask 3 models" parallel fan-out with summary, optional Elo badges (daily-cached), beta update channel.
- Windows completion toasts, Ctrl+Alt+A quick-ask window, scheduled automations.

**v5.1.x — resilience and rate-limit programme**
- Automatic continuation of a turn after network/5xx/empty failures (max 2 retries, 429-aware), soft anti-loop warnings instead of hard aborts, a prominent scroll-to-bottom button, a `delete_subagent` tool for the parent model, the cookie form available while signed in, and no send lockout during rate limits (hint only).
- Adaptive send pacing per account (token bucket; 429 waits in-queue), usage telemetry in Settings → Usage, agent context diet (read caching, earlier Compact warnings), light-model defaults for research sub-agents, opt-in daily-limit account failover.
- QoL: human-check window raises immediately, message queueing while generating, per-chat composer drafts, context-weight indicator (click = Compact), recent models group, Ctrl+F in-chat search, drag files from Workspace into the composer, completion toast/sound toggle.

**v5.5.x — Agent Gateway and components**
- Agent Gateway: selected local MCP servers exposed to the genuine Arena Agent through a temporary Cloudflare Quick Tunnel with pairing code, per-call approvals, session TTL/revoke, Settings → Gateway; Workspace Sync auto-start.
- Settings → Components: guided, checksum-verified installs (cloudflared, Node.js, Python, WebView2 detection).
- Rich Workspace file previews (sandboxed HTML/SVG, rendered Markdown, inline media/PDF).

**v5.6.x — native Agent protocol**
- Refresh of imported chats continued on arena.ai, native `ask_user` questions rendered as interactive cards, Agent image choices with full-size view and both-model reveal after picking.
- Fixes: stream cursor never moves backwards, continued Agent chats receive a fresh gateway block when the tunnel session changes, the built-in `workspace` MCP server (list/read/search/write/edit/run inside the picked folder) over the Gateway.

**v5.7.0 — voice, rendering, uploads, desktop integration**
- Native voice choice for Agent `add_voice` with previews and statistics.
- Streaming render keeps Reflections/tool cards/scroll state intact across tokens; Summary moved to the very end of a turn.
- Screenshot/upload pipeline: format by content (not extension), safe normalization, capability gating, upload diagnostics without private data, Retry/Remove without losing the draft.
- Ctrl+K command palette, Workspace Git panel (status/diff/stage/commit), styled HTML chat export, heavy-send badge, opt-in multi-account distribution for child chats, `arenadesktop://` deep links (no auto-send), DPAPI migration of stored credentials, Windows ARM64 CI builds.

**v5.8.x — native sub-agents and the Gateway hardening series**
- Up to six native Arena Agent sub-agents in parallel, each with its own Gateway, coordinating only through Workspace files; parent keeps working; children inherit permissions and can be stopped.
- Gateway shows `on` only when verified; component detection finds system-wide installs and "Check again" truly re-detects; cloudflared output is captured into Settings → Gateway and Copy diagnostics; QUIC tunnels that die fall back to HTTP/2 and the working protocol is remembered; tunnel verification depends on cloudflared's registration plus the loopback origin, not the local DNS resolver; synchronous `?wait=` jobs (one request = finished result), up to 40 steps per job.
- Per-instance local API token, scriptless Workspace HTML previews, plan-only unattended automations, code-mode deployment links verified against Arena's webdev metadata endpoint, input-only native `ask_user` events.

### Limits and verification

- Local validation at 6.0.0: non-browser and browser test suites pass on Linux (see the repository history for exact counts per patch), the x64 EXE passes PE architecture and embedded-content checks, and a real cloudflared tunnel was verified end-to-end. The maintainer's Windows machine confirms Gateway + sub-agents live against Arena; other live Arena behaviours can still change server-side.
- Windows builds are unsigned and can trigger SmartScreen. ARM64 is configured in CI but not locally tested. cloudflared's Windows x64 build requires emulation on Windows ARM64.
- The app does not solve human checks. Complete them in the Arena sign-in window.

### Download

Extract the source ZIP and run `Arena-Desktop.exe` from its top-level folder. The EXE is a GitHub release asset, not a committed source file.

- Windows x64 EXE: `Arena-Desktop.exe` — 30,168,056 bytes; SHA-256 `37a03077069989965e97b8f5929db37c1462fdb601ca2a9ecd7fe07ee8487266`.

Arena Desktop is not affiliated with or endorsed by Arena AI Inc. Original upstream: [PlayzAe/arena.ai-desktop](https://github.com/PlayzAe/arena.ai-desktop). License: MIT.
