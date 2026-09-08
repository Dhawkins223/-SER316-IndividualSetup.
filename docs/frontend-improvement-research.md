# Frontend improvement research

Research-only review of the dashboard frontend served by
`src/kalshi_research_bot/paper_server.py` (the `/` dashboard, `/login`, and
`/ops` pages). No behavior was changed. Line references are against commit
`34df017`.

## Method

- Read the full frontend surface: the render functions, the 1,861-line `CSS`
  string, the 267-line `JS` string, and the `PaperHandler` routes they talk to.
- Cross-referenced every CSS class selector and JS element id against the HTML
  the render functions actually emit (script-assisted diff).
- Rendered the dashboard offline with a hand-built payload that passes the
  freshness and combo-evidence gates, then screenshotted it in Chromium at
  1440px, 820px, and 390px, in both fresh and blocked states, plus the login
  and operator pages.
- Verified each suspected bug against the server code (auth store, role gates,
  refresh flow) rather than the UI alone.

## How the frontend works today

The dashboard is a single server-rendered HTML page. Python f-string render
functions build the markup; one `CSS` constant and one `JS` constant are
inlined into every response; there are no static assets, no build step, and no
framework. Updates reach the user by full page reload: a `meta refresh` tag
(local mode only, default every 600s), a JS poller that reloads when
`generated_at` changes, and a manual Refresh button that POSTs `/refresh` and
reloads on completion. All state lives server-side; the only client state is a
CSRF token in `sessionStorage`.

This architecture is a genuine strength for this project: no supply chain, a
strict CSP, progressive enhancement (the page is fully readable with JS off),
and one process to deploy. The recommendations below deliberately stay inside
it — nothing here proposes a SPA, a bundler, or a framework.

### What is already good

Worth naming so it doesn't get refactored away:

- Every withheld-data state is explicit and honest (fresh/stale/blocked/empty
  render distinct, labeled panels) — the safety-first posture survives into
  the UI.
- Security headers, `SameSite=Strict` HttpOnly session cookie, CSRF on
  mutating routes, escaping is consistent across render functions, and the
  JSON bootstrap uses the `</` escape.
- Accessibility basics are present: skip link, `aria-live` status regions,
  `aria-current` section tracking, `:focus-visible` outlines,
  `prefers-reduced-motion`, and text contrast that measures AA or better
  almost everywhere.
- The blocked-state page (screenshot-verified) communicates clearly why slips
  are hidden and what will bring them back.

## Findings

### A. User-visible bugs (fix first)

**A1. The session CSRF token is unrecoverable, so Refresh and the operator
inbox silently die in any new tab.**
`/auth/login` returns the CSRF token once and the login page stores it in
`sessionStorage` (`paper_server.py:257`). `sessionStorage` is per-tab: open the
dashboard in a second tab, restore the browser, or follow a bookmark and the
session cookie still authenticates you, but the CSRF token is gone. The server
stores only a hash (`auth.py:247`, `resolve_session` returns
`csrf_token=None` at `auth.py:292`), and no endpoint re-issues it — `/auth/me`
omits it (`paper_server.py:2023-2032`). Every POST then 403s
(`csrf_validation_failed`) until the user logs out and back in, and the UI
shows only a terse failure line. Fix options: return the token from a
dedicated `/auth/csrf` (or include it in `/auth/me`) and have the JS fetch it
on load, rotate it per-session-resolve, or switch to a double-submit cookie
readable by JS. Any of these is a small, contained change.

**A2. Freshness polling and auto-refresh only work for admins; other roles
silently never update.**
`pollLiveDataFreshness` polls `/quality.json` every 60s and auto-POSTs
`/refresh` when stale (`paper_server.py:4575-4603`), but both routes require
the `admin` role (`paper_server.py:2103-2106`, `2133-2135`). For `researcher`
and `read_only` users the poll returns `{"error": "role_forbidden"}` forever:
no reload on new data, no staleness indication, a wasted request per minute,
and in hosted mode (`refresh_seconds=0`, so no meta refresh — `cli.py:351`)
the page never updates at all while claiming "Live". Fix: expose a
role-appropriate freshness endpoint (the payload's `generated_at` +
`data_age_seconds` are already public in `/data.json`), gate the poll and the
auto-refresh POST on the principal's role (embed the role in
`window.PAPER_DATA` or read `/auth/me` once), and show a visible "data updated
— reload" affordance for roles that cannot trigger a refresh.

**A3. The topbar "Ready" status floats outside the header.**
`.refresh-control #refresh-status` is `position: absolute; top: 49px`
(`paper_server.py:3543`) but `.refresh-control` (`paper_server.py:3528`) never
sets `position: relative`, so the label anchors to the sticky topbar and dangles
below its border (visible in the 1440px screenshot). One-line fix.

**A4. Header timestamps are in the server's timezone; event times are in the
user's.**
`display_timestamp` renders "Updated / Last build" via server-side
`.astimezone()` (`paper_server.py:692-701`) — UTC on Railway — while slip leg
times are `<time datetime>` elements localized client-side
(`paper_server.py:4500-4502`). One page shows two different clocks. Only two
call sites emit `<time>`; the fix is to emit `<time datetime>` for every
timestamp and let the existing JS localize all of them (with the server string
as no-JS fallback).

**A5. Failures are silent or leak internals.**
- The operator page's `loadQueue()` swallows failures — a dead endpoint shows
  an empty queue with no message, and a failed fetch is an unhandled rejection
  (`paper_server.py:339-344`; reproduced in Chromium as `pageerror Failed to
  fetch`).
- Copy buttons `await navigator.clipboard.writeText` with no catch
  (`paper_server.py:4654-4662`) — on any clipboard denial the button just does
  nothing.
- The sports panel prints raw internals like
  `sports_board_unavailable:RuntimeError` at 9px (`render_sports_section`,
  `paper_server.py:1003`; screenshot-verified). Map exception classes to
  operator-readable reasons ("The sports database was unreachable during this
  page load") and keep the raw string in a `title`/details affordance if
  needed.
- Login failures collapse everything to "Sign-in failed." — a locked-out
  account (`AUTH_MAX_FAILED_LOGINS`) is indistinguishable from a typo'd
  password, which reads as "the site is broken" after the lock engages.

**A6. No favicon.** Every load fires a `/favicon.ico` request that 404s
through the auth stack. An inline `<link rel="icon" href="data:image/svg+xml,…">`
(the brand hawk already exists as inline SVG) removes the noise and gives the
tab an identity.

### B. Dead code (~700 lines, zero-risk deletion)

Verified by call-site search and selector/id cross-reference:

| Dead code | Location | Size |
| --- | --- | --- |
| `render_pick_section`, `render_market_card`, `render_research_section`, `render_public_intel_section`, `render_failure_guardrails`, `render_slip_rationale_row` — never called | `paper_server.py:1397-1962` (interleaved) | ~340 lines |
| `render_leg_detail` — called only by two of the dead functions above | `paper_server.py:1965-1975` | ~10 lines |
| Leg-calculator JS: `addLeg`, `recalc`, `#add-leg`/`#clear-legs` listeners — references ids `legs`, `target`, `penalty`, `combined`, `adjusted`, `status`, `add-leg`, `clear-legs`; none exist in any rendered page | `paper_server.py:4457-4462`, `4605-4653`, `4692-4693` | ~70 lines |
| CSS classes `.hero`, `.hero-meta`, `.refresh-box`, `.cards`, `.ghost` — emitted by no render function (the old pre-"product shell" layout) | CSS block | ~16 rule blocks |
| CSS neutralizing markup that no longer exists: `body::before, .hero::after, .panel::after, .panel::before { display: none !important; }` | `paper_server.py:2643-2648` | — |

The dead JS is not inert: `recalc()` runs on every page load, and the
calculator's `row.innerHTML` interpolation is the one unescaped-injection
pattern in the file — deleting it removes both waste and a latent footgun.

### C. Architecture and maintainability

**C1. The CSS is a 43KB override layer fighting a design that is already
gone.** 77 `!important` declarations, 10 overlapping breakpoints (1450, 1180,
1100, 900, 820→, 760, 680, 640, 430, 410), and first-line comment "Production
product UI" over rules that neutralize the previous generation's decorations.
The login page then defines a *second* design system — different background,
accent (`#00e676` vs `#29b779`), radii, and a duplicated brand SVG — so brand
changes must be made twice. Recommendation: one token set shared by all three
pages, rebuild the sheet additively (target: near-zero `!important`, 3-4
breakpoints), and extract the brand into the existing `render_brand()` for the
login page too.

**C2. The typography stack promises a font that never loads.** `font-family:
Inter, …` everywhere, but Inter is never shipped and the CSP (`default-src
'self'`) correctly blocks any font CDN — every user sees system fallbacks, and
the non-standard weights (`720`, `820`, `750`) quietly round to 700/800.
Either self-host Inter (a `woff2` served from the process keeps the
no-external-dependency stance) or standardize on the system stack and normal
weights so what's written is what renders.

**C3. One 4,723-line file holds HTTP, auth, business gating, HTML, CSS, and
JS.** Editing CSS inside a Python raw string means no syntax highlighting, no
linting, no formatting, and the f-string/`{{` escaping tax on every page.
Lowest-disruption fix: move `CSS` and `JS` (and optionally each page's
template) into `src/kalshi_research_bot/dashboard_assets/…` files read at
import time — deployment stays single-process, tooling returns, and
`paper_server.py` drops to ~2,300 lines. A natural second step is splitting
render functions into a `dashboard_render.py` module.

**C4. Everything is re-sent and re-computed on every request.** Responses are
`Cache-Control: no-store` with all assets inlined, so each of the frequent
full-page reloads re-transfers ~85KB, and each `GET /` synchronously performs
the Postgres snapshot read plus three more DB-touching panels (research
record, sports board, CLV) and builds review packets five times (four tiers +
drawer duplicates primary). For a single-operator tool this works; it will
degrade with tabs × roles × 60s polling. Cheap wins, in order: serve the CSS/JS
as hash-named static routes with long-lived caching (HTML stays `no-store`;
this also enables dropping `'unsafe-inline'` from `script-src`), memoize the
per-`generated_at` render inputs for a few seconds, and build the primary
review packet once per request.

**C5. Full-page reload is the only update mechanism.** Reloads discard open
`<details>`, sidebar scroll, and any in-progress operator-inbox text (the meta
refresh in local mode fires on a timer regardless of what the user is doing).
Staying framework-free, the incremental path is: fetch `/data.json` (already
exists), re-render only the dynamic regions (`textContent`/`replaceChildren`
on the stat tiles, badges, and slip panels), and reserve full reloads for
structural changes. Even a first step of "suspend meta refresh while a
`<details>` is open or the ops form is dirty" removes most of the pain.

### D. UX and information architecture

**D1. Three parallel navigation systems.** Topbar quick-nav (6 anchors),
sidebar (9 anchors in two groups), and mobile bottom nav (4) — all pointing at
the same single page, all needing separate upkeep (the topbar's "Research"
link and the sidebar's "Research scout" already target the same section under
different names). Desktop needs one primary nav; the sidebar is the natural
keeper since it carries the live-status card and leg counts.

**D2. The sidebar is not sticky, so desktop scrolls beside a dead 210px
column.** The prediction drawer is `position: sticky` but `.app-sidebar` is
not; on a page ~4,200px tall, roughly 90% of the scroll has an empty left
gutter (screenshot-verified). Make the sidebar sticky like the drawer, and the
persistent nav also resolves most of D1.

**D3. Mobile opens on the drawer, not the builder.** At ≤1180px the drawer
moves to `grid-row: 1`, so a 390px phone shows ~1.5 screens of compact slip
before the hero headline arrives mid-viewport; the full page measures ~7,300px
(screenshot-verified). If slip-first is intentional, make it a collapsed
summary bar ("3 legs · 61c · Ready — expand") that expands into a bottom
sheet; either way the page needs progressive disclosure — the four tier
panels repeat full detail (and full-width Copy/TXT/JSON button stacks) that
could collapse to summaries with an expander.

**D4. Redundant status, prime space spent on marketing.** "Updated/Last
build" appears 5 times above the fold (topbar status, hero meta, builder
summary, map update-line, sidebar card); the hero spends ~200px of the first
viewport on a static tagline the operator reads twice a day. One canonical
freshness indicator (topbar) plus a compacted hero returns most of a viewport
to live content.

**D5. Stacked duplicate empty states.** The sports section renders the CLV
panel's "No closing lines recorded" card directly above the board's "No sports
rows uploaded yet" card — two big warning boxes for one underlying condition
(no sports data yet). Collapse to a single state card that mentions both, or
hide the CLV panel entirely until at least one row is graded.

**D6. Micro-typography.** 9px (`.sidebar-label`, `.section-label`,
`.sports-state-reason`, topbar status) and 10px uppercase labels are below
comfortable legibility even for labels; the practical floor is 11px. Worth one
pass over the type scale.

### E. Accessibility

Beyond the strong baseline noted above:

- The mobile menu toggles `aria-expanded` and closes on Escape, but focus is
  not moved into the opened sidebar, not trapped, and not returned to the
  hamburger on close.
- Icon glyphs in nav links are decorative unicode with `aria-hidden` and
  adjacent text — fine — but the drawer's expand link (`↗`) has an
  `aria-label` while several `.copy` buttons communicate success only by a
  900ms text swap; adding `aria-live="polite"` to the button (or a shared
  status region) would announce "Copied".
- `prefers-color-scheme` is unhandled; the UI is intentionally dark-only,
  which is fine, but `<meta name="theme-color">` is missing, so mobile browser
  chrome renders white against the dark app.

### F. Testing gap (and an easy way in)

Server tests cover auth, gating, and a few string-contains checks on rendered
sections; nothing renders the full dashboard with a realistic payload, and no
test executes the JS. Two observations from doing it by hand:

1. Building a payload that passes `slip_has_authoritative_combo_evidence` is
   genuinely hard (nine per-leg evidence fields plus a leg-signature hash that
   must match). That difficulty is why no realistic fixture exists — and a
   canonical `make_verified_fixture_payload()` helper in
   `browser_fixtures.py` would fix tests, local preview, and
   `scripts/browser_validation_server.py` (which today requires a real
   `data/today_paper_view.json` to exist) in one move.
2. With that helper, two cheap layers pay for themselves immediately:
   - Golden render tests: `render_dashboard(fixture)` for each state
     (fresh/blocked/empty) asserting on the load-bearing strings (gate labels,
     leg counts, absence of slip rows when blocked) — pure unittest, no
     browser.
   - One Playwright smoke test (Chromium, headless) that loads the rendered
     pages, fails on console errors and uncaught rejections, and screenshots
     at 1440/390px. The console-error assertion alone would have caught A5,
     and a DOM-id assertion would have caught the entire dead calculator (B).

## Prioritized plan

Status: P0, P1, and the non-browser half of P2 are implemented. P3-P5 remain.

| Priority | Work | Effort | Risk |
| --- | --- | --- | --- |
| P0 — done | A1 CSRF recovery; A2 role-aware freshness polling; A3 refresh-status positioning; A5 silent failures + human-readable errors; A6 favicon | 1-2 days total | Low; each independently shippable |
| P1 — done | B dead-code removal (~700 lines); A4 timestamp localization | ~½ day | Near-zero (deletions verified above) |
| P2 — partly done | F fixture helper + golden render tests (done); Playwright smoke gate (outstanding) | 1-2 days | None to runtime; protects everything after it |
| P3 | C3/C4 asset extraction to files, static routes with caching, CSP tightened to drop `'unsafe-inline'`; memoize per-snapshot render work | 2-3 days | Moderate (deployment-visible; needs the P2 tests first) |
| P4 | C1/C2 CSS consolidation (one token set incl. login, ≤4 breakpoints, `!important` burn-down, font decision); D1/D2 sticky single sidebar nav; D4 status dedupe | 3-5 days | Visual regressions — do after P2 screenshots exist |
| P5 | C5 partial updates instead of reloads; D3 mobile progressive disclosure + drawer bottom sheet; E focus management; theme-color/manifest | as capacity allows | Contained per-item |

Sequencing rationale: the P0 bugs change what users experience today; P1 is
free page-weight; P2 builds the safety net that every visual/structural change
(P3-P5) needs. Everything stays within the current no-framework, no-build,
CSP-strict architecture — the point is to make the existing approach solid,
not to replace it.

## Reproduction artifacts

The offline render + screenshot harness used for this review (fixture payload
builder, page renderer, Playwright screenshot script) was session-local and is
described in "Method"; the fixture-construction logic worth keeping is exactly
what the P2 `browser_fixtures.py` helper should absorb.

## Reader readability update

The dashboard label floor is now 11px, including narrow-screen status badges.
Reader navigation uses Review, and its summary retains contracts checked and
review tiers ready; operational counts remain on the operator page. Fragility
labels state what the badge describes and expose the inputs below the figures.
