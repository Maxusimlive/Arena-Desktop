## Arena Desktop v5.7.0

<p align="right"><a href="https://github.com/Maxusimlive/Arena-Desktop">📂 Repository on GitHub</a></p>

Arena Desktop 5.7 focuses on reliable streaming, safer attachments and everyday workflow tools. It also completes Arena Agent voice choices and adds optional account-session distribution for sub-agents.

### Streaming answers no longer lose their place

- An opened **Reflections** block now stays open while new reasoning, tool calls and answer tokens arrive.
- The broader streaming renderer preserves expanded activity blocks, scroll positions, input focus/caret, text selection and media state instead of resetting the whole turn on every token.
- For Agent and tool turns, the final report now renders **once, below the work timeline**, from the first token through reloads. Long finished turns get a subtle **Summary ↓** jump — without moving a copy back to the top.

### Screenshots and attachments are more defensive

- File type is detected from content rather than trusting only a filename or declared MIME type.
- Images are cautiously normalized only when needed, preserving ordinary screenshots and animated GIFs where possible.
- Direct, Side by Side and fan-out sends stop before upload when a selected model is explicitly known not to accept the attached image/file kind. Unknown catalogue capabilities are not treated as a ban.
- Attachment chips show the detected format, image dimensions, resulting size and normalization state.
- If Arena rejects an upload, the draft and attachment stay in the composer with Retry/Remove instead of being silently lost.
- **Copy diagnostics** now includes the last 20 upload attempts with stage, source (`picker`, `paste`, `drop` or `workspace`), app mode, safe Arena/storage statuses, MIME types, byte counts and image dimensions. It deliberately excludes filenames, file contents/hashes, cookies, storage keys and signed URLs.

### Arena Agent can ask you to choose a voice

- Native Arena Agent `add_voice` auditions now appear as an interactive voice-choice card.
- Both candidates can be played in the app; the audition text, language and voice identity stay visible.
- Your selection, returned `voice_id`, play counts and listening time are sent back into the same Arena session.
- Completed voice choices survive reopening, refresh and Arena history import; unlike generated images, voices do not receive a model reveal.

### New everyday tools

- **Ctrl+K command palette:** jump to recent chats, search message text, start a chat, change mode/model where allowed, open Workspace Git, export, focus the composer or run **Compact**.
- **Workspace Git panel:** see branch/status, inspect staged and unstaged diffs, stage/unstage selected files or everything, and create a local commit using the repository's own identity/hooks. It never pushes, pulls or switches branches.
- **Heavy-send badge:** when a large conversation is about to go to a premium model, the composer warns that this send may use a larger part of Arena's daily balance. Clicking it opens Usage.
- **Styled HTML chat export:** beside Markdown, export a standalone HTML transcript with both lanes, reasoning, tools and attachment names — escaped, with locked-down CSP and safe link handling.

### Optional multi-session sub-agents

A new Settings option can distribute **child chats from delegation, fan-out and parallel research** across your saved Arena accounts using isolated HTTP sessions and per-account cookie updates. It is **off by default**, keeps parent chats and native Arena Agent on the current account, does not rotate around a daily limit, and records which account actually ran each child. The visible Arena browser window remains single-account.

### Deep links and DPAPI-protected sessions

- The packaged EXE registers `arenadesktop://` for the current Windows user (HKCU, no administrator rights).
- Supported links: `arenadesktop://open`, `arenadesktop://new`, `arenadesktop://chat?id=<chat-id>` and `arenadesktop://ask?q=<text>`.
- An `ask` link only creates a draft and focuses the composer — it **never sends automatically**. A second process hands the link to the already-running window through a per-launch local token and exits.
- `cookies.txt` and cookie values in `accounts.json` are now saved in a versioned Windows DPAPI form for the current user. Existing plaintext values are read safely and migrated on load.

### Agent Gateway auto-start fixed

- The gateway could silently never start in a fresh app run even with both Gateway opt-ins enabled: the UI only learned those settings after a Settings toggle changed, so the automatic warm-up never fired. Settings are now loaded at startup, and a failed warm-up retries after a cooldown instead of latching off for the whole run.
- A slow Cloudflare Quick Tunnel (DNS/edge propagation) no longer gets killed by the shorter auto-start wait while its health probe is still pending — a live tunnel that already has its URL stays up.
- Auto-start skips and failures are now written to the app log with the reason (Workspace Sync off, nothing to expose, tunnel state/error) instead of failing silently.

### Builds

- This release asset is the verified **Windows x64** one-file EXE.
- The repository now includes GitHub Actions for both Windows x64 and native Windows ARM64 builds (`windows-11-arm`, Python 3.12 arm64), including PE-architecture verification of the produced artifacts. Cloudflare does not publish a Windows ARM64 cloudflared build for the pinned release, so on ARM64 Windows that optional Gateway helper remains the official x64 file run by Windows emulation.

**Verification** — 349 automated tests pass (one pre-existing test that expects locally installed Windows browsers is deselected in the Linux sandbox), 23/23 browser UI tests pass, and 69/69 packaged-EXE integrity checks pass. The Gateway auto-start fix adds a slow-tunnel regression test (gateway group 9/9, engine JS 17/17, native agent 18/18 re-run against the rebuilt EXE). Live Arena/Windows acceptance for real screenshots, `add_voice`, registry links and DPAPI migration is still the most valuable feedback for this release.

📥 **Download `Arena-Desktop.exe`** (26,396,227 bytes) — no installation needed, just run it.
SHA-256: `2744809e2eda165629d820e63e37d4b5dc964455a74afcead947c71af3952e0e`
💬 Discord: `@.max_power._`
