# Desktop Engineering Guide

How to build Hermes Desktop well. This is a judgment guide, not an inventory —
it teaches the invariants and the reasoning behind them so a change fits the app
even as files move. Read it with the repository `AGENTS.md` (root rules still
apply) and [`DESIGN.md`](./DESIGN.md) for the visual and interaction contract.

When a rule here and the code disagree, trust the code and fix whichever is
wrong — but never break an invariant to make a change easier.

## What this app is

Desktop is its own native chat surface. It is not the browser dashboard and it
does not embed the TUI. Three parties, each authoritative for one thing:

- **Electron** owns the machine: process lifecycle, native filesystem/git/
  windows, install/update, and a narrow, typed capability bridge.
- **The renderer** owns the experience: navigation, presentation, and ephemeral
  interaction state.
- **The agent backend** owns the work: sessions, tools, model calls, streaming.

Keep the seams clean. The renderer never reaches for Node or Electron directly;
native power arrives through a deliberate capability, not a general escape hatch.
Agent behavior lives behind the gateway, never reimplemented in React. When a
change blurs a seam, that is the smell — fix the seam, don't widen it.

## Decide state by authority

The first question for any piece of state is *who is allowed to be right about
it*, not where it is convenient to store it. Put state with its authority:

- The **backend** is authoritative for anything another Hermes surface can also
  change. Treat the renderer's copy as a cache of that truth.
- **Electron** is authoritative for machine and runtime facts.
- The **renderer** owns only what is purely about this window's presentation.

From that, everything else follows: shared renderer state lives in small stores
owned by the feature that owns the concern; request-shaped server data that wants
invalidation lives in the query layer; short-lived interaction detail stays in
the component; hot coordination that must not paint stays in a ref. Reach for the
narrowest home that still lets the state be correct. A new global store is a
claim that many distant surfaces need it — earn that claim.

Persisted state must declare its scope in its own key: is this global, or does it
belong to a connection, a profile, a stored session, a project, or a window?
Getting the scope wrong is how one profile's setting bleeds into another.

## Identity is not incidental

Sessions have more than one identity, and conflating them is a recurring source
of "session not found" and vanishing history. Reason about which identity a
surface needs: durable navigation and anything the user pins or persists key off
the stable/durable identity; live streaming keys off the runtime identity; state
that must outlive compression keys off the lineage root. Keep the mapping between
them explicit and translate at the boundary rather than passing the wrong id
inward.

## Server truth is cached, not owned

The renderer paints from a cache of backend truth, so it must reconcile, not
assume:

- **Merge, don't clobber.** A refresh is new information layered over what you
  already know, not a replacement that can drop live or pinned rows.
- **Be optimistic, then honest.** Direct manipulation should paint immediately
  from a snapshot; a failed write rolls back visibly and an authoritative
  refresh gets the last word.
- **Guard against the past.** Async results can arrive out of order; a stale
  response must never overwrite newer intent. Generation counters and request
  tokens exist for this.
- **Isolate the foreground.** Only the surface the user is looking at may publish
  into the shared view; background work updates its own cache quietly.
- **Coalesce noise, flush signal.** Batch high-frequency cosmetic updates, but
  let terminal transitions (a turn finishing, needing input, failing) reach the
  user immediately.
- **Preserve reference identity on no-ops.** Handing React a fresh array that
  contains the same data re-renders expensive trees for nothing.

## Switching context is a re-home, not a reboot

Changing profile, connection, or mode is a workspace switch, not a cold start.
The shell and whatever the user was doing stay put; only the gateway-bound view
is cleared and repopulated, and the previous context must not leak into the next
one. Reserve the full-screen boot/connecting experience for a genuinely unusable
backend.

There are three distinct switch shapes, and conflating them is the classic bug:

- A **connection/mode apply** (local ↔ remote ↔ cloud) is the soft re-home:
  shell mounted, gateway-bound stores explicitly wiped, then reconnect. Query
  invalidation alone cannot evict live session stores — wipe them.
- A **runtime home change** (switching the underlying `HERMES_HOME` profile) is
  a hard re-home: the window legitimately reloads and state resets by remount.
- A **live profile swap** in the same window activates another profile's socket
  while background profiles keep streaming; lists merge rather than wipe, and
  only an explicit user selection starts a fresh foreground draft.

Treating a soft switch as hard flickers the app; treating a hard one as soft
strands stale rows. After any swap, the active socket, active profile, and
connection atoms must agree, or REST and filesystem calls route to the wrong
backend.

## Cross everything as an observable ladder

Desktop lives at the seams: versions, profiles, local vs remote vs cloud,
partially installed runtimes, stale caches, older backends. The durable technique
for all of it is the same — an ordered ladder of candidates:

1. Precedence is written down, in one place, as data or a pure function.
2. A candidate is trusted only after it is validated at the right boundary.
   Existence is not proof; probe what you're about to rely on.
3. A failed *read* falls to the next rung; a failed *authoritative write*
   surfaces or rolls back rather than silently retargeting.
4. A missing capability and a transient failure are different: the first may
   enable a compatibility path or a disabled state; the second should retry.
5. Retries are bounded and end in a real recovery affordance — never an infinite
   spinner or a hot loop.
6. One resolver owns each policy so every caller gets the same answer. Scatter is
   how two call sites drift apart.

This is the shape of backend discovery, command/version fallbacks, connection and
auth resolution, workspace-cwd selection, capability detection, and preview
normalization alike. Learn the shape, not a snapshot of the current rungs.

Two auth-flavored corollaries worth naming because they are easy to get wrong:

- **One-time credentials are never reused.** An OAuth gateway connection mints a
  fresh WebSocket ticket on every dial and never falls back to the cached URL.
  Only a confirmed 401/403 (or an explicitly tagged auth rejection) means
  reauthentication; timeout, network, malformed-response, and server failures
  remain connectivity errors. Only long-lived token/local auth may reuse a
  cached URL as a lower rung.
- **A connection test must exercise the leg you'll actually use.** An HTTP
  status probe passing while the WebSocket/auth leg fails is a false positive
  that ships as "it said connected but nothing works."

## Compatibility without carrying the past forever

Desktop and its runtime update on separate clocks, so a change can meet an older
backend. Keep those users working: preserve the current feature, keep the
fallback narrow and tied to an identified older runtime, and cover it with a
test. A fallback that quietly degrades the feature it's meant to protect is worse
than the crash it replaced.

## Keep the waist narrow, grow at the edges

The root contribution rubric governs here too. New capability should arrive at
the smallest surface that solves it: extend what exists, add a feature locally,
lean on an existing seam — before you invent a framework. The shell's internal
registries are composition seams, not a public plugin ABI; do not build a
universal extension system, a manifest, or a plugin adapter for a single
consumer. Design a shared contract only once more than one real consumer proves
its shape. "Plugin" means several unrelated things across Hermes — do not assume
one surface's extension model runs in another.

## Respect the person using it

Design and engineering meet at intent. The user's attention and context are
sacred:

- Never navigate, move focus, or open a surface because something *happened* in
  the background. Offer; don't hijack.
- The states around loading are distinct experiences — empty, loading,
  reconnecting, degraded/stale, and exhausted-recovery each deserve their own
  honest copy and their own way out.
- Keyboard ownership follows focus. The focused surface wins its keys; one
  cancel gesture does exactly one thing.
- Expensive, stateful surfaces (terminals, live tools) stay alive when hidden.
  Visibility is not lifecycle.

## Make it feel instant

Performance is a feature the user feels, especially in drag, resize, scroll,
typing, streaming, and terminals. The principles are timeless even as the code
changes: keep hot-path state local or narrowly derived; don't subscribe heavy
trees to per-frame updates; coalesce pointer work; avoid reading layout right
after writing style; and don't mount expensive content mid-gesture. Prove speed
against realistic content — a fast empty demo proves nothing about a long
transcript. If motion is masking latency, remove the motion, don't tune it.

## Testing as a habit of proof

Test the behavior that would actually break a user, not a snapshot of today's
data. Favor invariants over frozen values. Exercise the real path for anything
at a seam — resolver precedence and its failure rungs, identity and scope
boundaries, optimistic rollback and stale-response ordering, and both sides of a
local/remote adapter with its profile routing intact. Match how the suite is
actually run rather than inventing a command; when in doubt, read the scripts.

## The taste test before you hand off

- Does every piece of state live with its authority, at the narrowest scope?
- Would a background event ever steal the foreground or the user's focus?
- Does each resolver have one home, a validated ladder, and a bounded, recoverable
  end?
- Do local, remote, and profile routing still agree?
- Does async failure leave a usable UI and a way forward?
- Do hot interactions stay cheap under realistic load?
- Does the change pass the [`DESIGN.md`](./DESIGN.md) checklist and update all
  locales?

If any answer is "not sure," that's the part to go verify.

<!-- Moved word for word from the repository root AGENTS.md on 2026-11-17 (a root instruction file is paid on every session; this loads when work touches this folder). Source: Brian, 2026-11-17: "go" (instruction-file cleanup). -->

### Electron Desktop Chat App (`apps/desktop/`)

A **separate** chat surface from both the classic CLI and the dashboard's embedded TUI. It is an Electron + React + nanostore renderer (`@assistant-ui/react`) that talks to a `tui_gateway` backend over JSON-RPC (`requestGateway(method, params)`). The WebSocket/JSON-RPC transport lives in the framework-agnostic `apps/shared` package (`@hermes/shared` — `JsonRpcGatewayClient` + WS URL helpers), which the web dashboard (`web/`) also consumes; **desktop has no build/runtime dependency on the dashboard frontend** — it spawns a headless `hermes serve` backend server (the same gateway `dashboard` serves, minus the browser UI entirely: `serve` sets `headless_backend=True`, so `cmd_dashboard` skips `_build_web_ui` AND exports `HERMES_SERVE_HEADLESS=1` so `mount_spa()` disables the SPA even if a stray `web_dist/` exists — only the JSON-RPC/WS/API surface is reachable). `dashboard` and `serve` share `cmd_dashboard`/`start_server` but are independent surfaces — neither launches the other. The one exception is a backward-compat *fallback*: `serve` is newer, so the desktop spawn (`electron/backend-command.ts` + `backendSupportsServe()` in `electron/main.ts`) detects whether the resolved runtime registers `serve` and, only when it does not (an older managed install / PATH `hermes` the app hasn't updated yet), rewrites the argv to the legacy `dashboard --no-open`. Without that, a new app against an un-upgraded runtime would crash on an unknown subcommand and brick every mid-upgrade user. It does NOT embed `hermes --tui` — it has its own composer, transcript, and slash-command pipeline. For scoped Desktop architecture, state, resolver, transport, and testing rules, read `apps/desktop/AGENTS.md`.

**Slash commands in the desktop app are curated client-side, then dispatched to the backend.** The pipeline:

- **Backend already provides everything.** `tui_gateway/server.py` `commands.catalog` (empty-query list) and `complete.slash` (typed-query completions) both include built-in commands, user `quick_commands`, AND skill-derived commands (`scan_skill_commands()` / `get_skill_commands()`). The desktop app does not need a new RPC to see skills.
- **The renderer curates via `apps/desktop/src/lib/desktop-slash-commands.ts`.** This is the load-bearing file. It holds `DESKTOP_COMMAND_SPECS` (the built-ins and their Desktop surfaces) plus `NO_DESKTOP_SURFACE` block-lists for terminal-only / messaging-only / picker-owned / settings-owned / advanced commands that should NOT clutter the desktop popover.
  - `isDesktopSlashCommand(name)` — gates **execution**. Returns true for built-ins AND for any non-built-in (skill / quick command), so typed extension commands run.
  - `isDesktopSlashSuggestion(name)` — gates **discovery/completion**. Used by BOTH completion paths in `app/chat/composer/hooks/use-slash-completions.ts` (empty-query catalog filter + typed-query `complete.slash` filter) and by `filterDesktopCommandsCatalog`.
  - `isDesktopSlashExtensionCommand(name)` — true when the command is NOT a known Hermes built-in (i.e. a skill or user quick command). Both suggestion and catalog-filter paths allow extensions through so skill commands surface in the palette. (Added when fixing "skill commands missing from the desktop slash palette" — the curated allow-list was silently dropping every skill/quick command from completions even though they executed fine when typed.)
- **Dispatch** lives in `app/session/hooks/use-prompt-actions/slash.ts` (`runSlash`): built-ins that the desktop owns (`/skin`, `/help`, `/new`, …) are handled locally or via `commands.catalog`; everything else goes to `slash.exec`, falling back to `command.dispatch` (which the gateway resolves into skill / alias / exec directives). A skill command resolves to `{type: "skill", message}` and is submitted as a normal prompt.

**Rule:** the desktop slash palette's curation is about hiding noise (terminal-only / messaging-only built-ins), NOT about hiding user-activated extensions. Skill commands and `quick_commands` are extensions the backend surfaces — they belong in completions. If you tighten `desktop-slash-commands.ts`, keep `isDesktopSlashExtensionCommand` flowing into both the suggestion and catalog-filter paths. Tests: from `apps/desktop`, run `npx vitest run src/lib/desktop-slash-commands.test.ts` (workspace dependencies are installed at the repo root).

---
