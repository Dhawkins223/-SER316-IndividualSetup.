/* Research dashboard behaviour. Progressive enhancement only: every value on
   the page is server-rendered first, and this layer refines it. */

const paperData = (() => {
  try {
    return JSON.parse(document.body.dataset.paper || "{}");
  } catch (error) {
    return {};
  }
})();

const refreshButton = document.querySelector("#refresh-slip");
const refreshStatus = document.querySelector("#refresh-status");
const liveDataGeneratedAt = paperData.generated_at || "";
const canRefresh = paperData.can_refresh === true;
const LIVE_DATA_POLL_SECONDS = 60;
const LIVE_DATA_STALE_SECONDS = 300;
let refreshPollTimer = null;
let liveDataPollTimer = null;

function csrfToken() {
  // The cookie outlives this tab, so a second window or a restarted browser
  // still posts with a token the server recognises.
  const fromCookie = document.cookie
    .split(";")
    .map(part => part.trim())
    .find(part => part.startsWith("hawknetic_research_csrf="));
  if (fromCookie) return decodeURIComponent(fromCookie.split("=").slice(1).join("="));
  return sessionStorage.getItem("research_csrf_token") || "";
}

function researchActionHeaders(action = "refresh-dashboard") {
  const token = csrfToken();
  const headers = { "X-Research-Action": action };
  if (token) headers["X-CSRF-Token"] = token;
  return headers;
}

/* ------------------------------------------------------------ timestamps */

function formatTimestamp(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatEventTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time TBD";
  const now = new Date();
  const dateKey = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const todayKey = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dayDelta = Math.round((dateKey - todayKey) / 86400000);
  const dayText = dayDelta === 0
    ? "Today"
    : dayDelta === 1
      ? "Tomorrow"
      : date.toLocaleDateString([], { month: "short", day: "numeric" });
  const timeText = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return `${dayText} · ${timeText}`;
}

document.querySelectorAll("time[datetime]").forEach(element => {
  element.textContent = element.dataset.format === "timestamp"
    ? formatTimestamp(element.dateTime)
    : formatEventTime(element.dateTime);
});

/* --------------------------------------------------------------- refresh */

function setRefreshStatus(status) {
  if (!refreshStatus) return;
  const state = status?.state || "idle";
  const refreshLabel = refreshButton?.querySelector(".refresh-label");
  refreshStatus.className = "";
  if (state === "running") {
    if (refreshButton) refreshButton.disabled = true;
    if (refreshLabel) refreshLabel.textContent = "Refreshing…";
    refreshStatus.classList.add("warning");
    refreshStatus.textContent = "Updating";
    return;
  }
  if (refreshButton) refreshButton.disabled = false;
  if (refreshLabel) refreshLabel.textContent = "Refresh";
  if (state === "complete") {
    refreshStatus.classList.add("good");
    refreshStatus.textContent = `Live · ${formatTimestamp(status.generated_at)}`;
    return;
  }
  if (state === "error") {
    refreshStatus.classList.add("bad");
    refreshStatus.textContent = status.error || status.message || "Refresh failed.";
    return;
  }
  refreshStatus.textContent = canRefresh ? "Ready" : "View only";
}

async function fetchRefreshStatus() {
  const response = await fetch("/refresh-status", { cache: "no-store" });
  return response.json();
}

async function pollRefreshStatus() {
  try {
    const status = await fetchRefreshStatus();
    setRefreshStatus(status);
    if (status.state === "running") {
      refreshPollTimer = setTimeout(pollRefreshStatus, 2000);
      return;
    }
    if (status.state === "complete") {
      setTimeout(() => window.location.reload(), 900);
    }
  } catch (error) {
    setRefreshStatus({ state: "error", error: `Refresh status check failed: ${error.message}` });
  }
}

async function triggerSlipRefresh() {
  if (!refreshButton) return;
  clearTimeout(refreshPollTimer);
  setRefreshStatus({ state: "running" });
  try {
    const response = await fetch("/refresh", {
      method: "POST",
      cache: "no-store",
      headers: researchActionHeaders(),
    });
    const status = await response.json();
    setRefreshStatus(status);
    if (status.state === "running") {
      refreshPollTimer = setTimeout(pollRefreshStatus, 2000);
    }
    if (!response.ok && status.state !== "running") {
      setRefreshStatus({ state: "error", error: status.message || "Refresh request was rejected." });
    }
  } catch (error) {
    setRefreshStatus({ state: "error", error: `Refresh request failed: ${error.message}` });
  }
}

// Whether the reader is looking at data the server has withheld slips over.
//
// The gate has already decided this, and re-deriving it here got it wrong. The
// poller used to ask `Number(freshness.data_age_seconds || 0) <= LIVE_DATA_
// STALE_SECONDS`, which reads an absent age as an age of zero. Five of the six
// outcomes `slip_payload_gate` produces block, and four of those five carry no
// age that comparison can use:
//
//   blocked_refresh_failed         null     the latest refresh failed
//   blocked_stale_source           null     serving cached rows
//   blocked_missing_generated_at   null     timestamp missing or unparseable
//   blocked_invalid_generated_at   -7200    timestamp in the future
//   blocked_stale_payload          10800    ordinary stale -- the only one that worked
//   fresh_data_ready               240      not blocked
//
// So in every case but ordinary staleness the reader was left on a page that
// looks live, which is the outcome the branch below exists to prevent.
// `status` sits in the same payload, already decided by `slip_payload_gate`.
//
// A response carrying neither a status nor a usable age is treated as blocked,
// not as fine. Silence about freshness is not evidence of freshness, and the
// first version of this function said otherwise -- which was the original
// defect surviving in the fallback.
function liveDataIsBlocked(freshness) {
  if (freshness && freshness.status) return freshness.status !== "ready";
  // One rule: an age is usable only if it is a real, finite, non-negative
  // number of seconds. Anything else is unknown, and unknown is blocked.
  //
  // Each earlier version delegated part of that judgement to `Number()`, which
  // answers with a finite number for things that are not numbers, so each one
  // let a different set through. Written as `Number.isFinite(Number(raw))` the
  // survivors were `""`, `"   "`, `true`, `[]` and any negative age; `null` was
  // not among them -- that version rejected it separately, and the version
  // before *that* is where `Number(null)` being 0 read as an age of zero.
  //
  // The shapes are enumerated in `tests/test_live_freshness_client.py`, which
  // runs this function rather than describing it. Kept there rather than
  // listed here, because a comment cannot be wrong in a way the suite notices.
  //
  // Negative is rejected rather than clamped: an age below zero means the
  // payload is stamped in the future, which is `blocked_invalid_generated_at`,
  // not freshness. `-7200 > 300` is false, so it read as live.
  const raw = freshness ? freshness.data_age_seconds : null;
  const age = typeof raw === "string" && raw.trim() !== "" ? Number(raw) : raw;
  if (typeof age !== "number" || !Number.isFinite(age) || age < 0) return true;
  return age > LIVE_DATA_STALE_SECONDS;
}

async function pollLiveDataFreshness() {
  try {
    const response = await fetch("/freshness.json", { cache: "no-store" });
    if (!response.ok) return;
    const freshness = await response.json();
    if (freshness.generated_at && liveDataGeneratedAt && freshness.generated_at !== liveDataGeneratedAt) {
      window.location.reload();
      return;
    }
    if (!liveDataIsBlocked(freshness)) return;
    if (!canRefresh) {
      // A reader without refresh rights would otherwise sit on stale data
      // that still looks live, so say so instead of polling silently.
      setRefreshStatus({ state: "error", error: freshness.message || "Data is stale. Ask an admin to refresh." });
      return;
    }
    const status = await fetchRefreshStatus().catch(() => ({}));
    if (status.state === "running") return;
    const refreshResponse = await fetch("/refresh", {
      method: "POST",
      cache: "no-store",
      headers: researchActionHeaders(),
    });
    const refreshPayload = await refreshResponse.json().catch(() => ({}));
    setRefreshStatus(refreshPayload);
    if (refreshPayload.state === "running") {
      refreshPollTimer = setTimeout(pollRefreshStatus, 2000);
    }
  } catch (error) {
    setRefreshStatus({ state: "error", error: `Live freshness check failed: ${error.message}` });
  } finally {
    liveDataPollTimer = setTimeout(pollLiveDataFreshness, LIVE_DATA_POLL_SECONDS * 1000);
  }
}

if (refreshButton) {
  refreshButton.addEventListener("click", triggerSlipRefresh);
  fetchRefreshStatus().then(status => {
    setRefreshStatus(status);
    if (status.state === "running") {
      refreshPollTimer = setTimeout(pollRefreshStatus, 2000);
    }
  }).catch(() => {});
}

/* ------------------------------------------------------ connected sources */

const sourceRefreshButton = document.querySelector("#refresh-source-data");
const sourceDataStatus = document.querySelector("#source-data-status");

function setSourceDataStatus(message, state = "") {
  if (!sourceDataStatus) return;
  sourceDataStatus.textContent = message;
  sourceDataStatus.className = state;
}

async function pollSourceRefresh(requestId, attempts = 0) {
  try {
    const response = await fetch(`/api/v1/source-data/refresh/${encodeURIComponent(requestId)}`, {
      cache: "no-store",
    });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || "Refresh status unavailable");
    if (["completed", "failed", "blocked"].includes(status.status)) {
      if (sourceRefreshButton) sourceRefreshButton.disabled = false;
      if (status.status === "completed") {
        setSourceDataStatus("Cloud data updated. Reloading…", "good");
        setTimeout(() => window.location.reload(), 700);
      } else {
        setSourceDataStatus(`Cloud refresh ${status.status}. Check source status.`, "bad");
      }
      return;
    }
    if (attempts >= 100) {
      if (sourceRefreshButton) sourceRefreshButton.disabled = false;
      setSourceDataStatus("Refresh is still queued in Railway.", "warning");
      return;
    }
    setTimeout(() => pollSourceRefresh(requestId, attempts + 1), 3000);
  } catch (error) {
    if (sourceRefreshButton) sourceRefreshButton.disabled = false;
    setSourceDataStatus(`Refresh check failed: ${error.message}`, "bad");
  }
}

async function triggerSourceRefresh() {
  if (!sourceRefreshButton) return;
  sourceRefreshButton.disabled = true;
  setSourceDataStatus("Cloud refresh queued…", "warning");
  try {
    const response = await fetch("/api/v1/source-data/refresh", {
      method: "POST",
      cache: "no-store",
      headers: {
        ...researchActionHeaders("queue-source-refresh"),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        sources: ["kalshi_current", "sports_current", "polymarket", "kalshi_reference"],
        scope: { requested_from: "dashboard" },
      }),
    });
    const queued = await response.json();
    if (!response.ok || !queued.request_id) {
      throw new Error(queued.error || "Refresh request was rejected");
    }
    setSourceDataStatus("Waiting for the cloud collector…", "warning");
    pollSourceRefresh(queued.request_id);
  } catch (error) {
    sourceRefreshButton.disabled = false;
    setSourceDataStatus(`Refresh request failed: ${error.message}`, "bad");
  }
}

sourceRefreshButton?.addEventListener("click", triggerSourceRefresh);

/* ----------------------------------------------------------------- copy */

async function copyText(text) {
  // Clipboard access is refused outside a secure context and can be denied by
  // permission; fall back so the operator can still get the packet out.
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const carrier = document.createElement("textarea");
    carrier.value = text;
    carrier.setAttribute("readonly", "");
    carrier.style.position = "fixed";
    carrier.style.opacity = "0";
    document.body.append(carrier);
    carrier.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (fallbackError) {
      copied = false;
    }
    carrier.remove();
    return copied;
  }
}

document.querySelectorAll(".copy").forEach(button => {
  const originalText = button.textContent;
  button.addEventListener("click", async () => {
    const text = button.dataset.copy || button.dataset.title || "";
    const copied = await copyText(text);
    button.textContent = copied ? "Copied" : "Copy failed — select manually";
    setTimeout(() => { button.textContent = originalText; }, copied ? 900 : 2600);
  });
});

/* ------------------------------------------------------ section tracking */

const sectionLinks = [...document.querySelectorAll(
  '.top-navigation a[href^="#"], .mobile-bottom-nav a[href^="#"], .side-navigation a[href^="#"]'
)];
const linkedSections = [...new Set(sectionLinks
  .map(link => document.querySelector(link.getAttribute("href")))
  .filter(Boolean))];

function setCurrentSection(sectionId) {
  sectionLinks.forEach(link => {
    if (link.getAttribute("href") === `#${sectionId}`) {
      link.setAttribute("aria-current", "location");
    } else {
      link.removeAttribute("aria-current");
    }
  });
}

if (sectionLinks.length) {
  const initialSectionId = window.location.hash.slice(1) || linkedSections[0]?.id;
  if (initialSectionId) setCurrentSection(initialSectionId);
  sectionLinks.forEach(link => link.addEventListener("click", () => {
    setCurrentSection(link.getAttribute("href").slice(1));
  }));
}

if ("IntersectionObserver" in window && linkedSections.length) {
  const sectionObserver = new IntersectionObserver(entries => {
    const visibleSection = entries
      .filter(entry => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (visibleSection) setCurrentSection(visibleSection.target.id);
  }, { rootMargin: "-20% 0px -65% 0px", threshold: [0.01, 0.25, 0.6] });
  linkedSections.forEach(section => sectionObserver.observe(section));
}

/* ---------------------------------------------------------- mobile drawer */

const mobileMenuToggle = document.querySelector("#mobile-menu-toggle");
const appSidebar = document.querySelector("#app-sidebar");
const sidebarScrim = document.querySelector("#sidebar-scrim");
const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';
let lastFocusedBeforeMenu = null;

function setMobileMenu(open) {
  if (!mobileMenuToggle || !appSidebar) return;
  appSidebar.classList.toggle("open", open);
  sidebarScrim?.classList.toggle("open", open);
  if (sidebarScrim) sidebarScrim.hidden = !open;
  mobileMenuToggle.setAttribute("aria-expanded", String(open));
  // Without the scroll lock the page behind kept scrolling under the drawer,
  // which read as the content bleeding through it.
  document.body.classList.toggle("nav-open", open);
  if (open) {
    lastFocusedBeforeMenu = document.activeElement;
    appSidebar.querySelector(FOCUSABLE)?.focus();
  } else if (lastFocusedBeforeMenu instanceof HTMLElement) {
    lastFocusedBeforeMenu.focus();
    lastFocusedBeforeMenu = null;
  }
}

function menuIsOpen() {
  return Boolean(appSidebar?.classList.contains("open"));
}

if (mobileMenuToggle && appSidebar) {
  mobileMenuToggle.addEventListener("click", () => setMobileMenu(!menuIsOpen()));
  sidebarScrim?.addEventListener("click", () => setMobileMenu(false));
  appSidebar.querySelectorAll('a[href^="#"]').forEach(link => {
    link.addEventListener("click", () => setMobileMenu(false));
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && menuIsOpen()) {
      setMobileMenu(false);
      return;
    }
    if (event.key !== "Tab" || !menuIsOpen()) return;
    // Keep tabbing inside the drawer while it covers the page.
    const focusable = [...appSidebar.querySelectorAll(FOCUSABLE)].filter(node => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  // Leaving the drawer breakpoint must not strand the page in a locked state.
  window.matchMedia("(min-width: 1181px)").addEventListener("change", event => {
    if (event.matches) setMobileMenu(false);
  });
}

/* --------------------------------------------------- mobile current slip */

const predictionDrawer = document.querySelector("#prediction-drawer");
const predictionDrawerTitle = document.querySelector("#prediction-drawer-title");
const mobileSlipToggle = document.querySelector("#mobile-slip-toggle");
const closePredictionDrawer = document.querySelector("#close-prediction-drawer");
const mobileSlipMedia = window.matchMedia("(max-width: 900px)");
const slipBackground = [...document.querySelectorAll(
  ".app-topbar, .app-sidebar, .workspace, .mobile-bottom-nav, #mobile-slip-toggle, .skip-link"
)];
let lastFocusedBeforeSlip = null;

function setMobileSlip(open, restoreFocus = true) {
  if (!predictionDrawer || !mobileSlipToggle) return;
  const mobile = mobileSlipMedia.matches;
  const expanded = mobile && open;
  if (expanded && !document.body.classList.contains("slip-open")) {
    setMobileMenu(false);
    lastFocusedBeforeSlip = document.activeElement;
  }
  document.body.classList.toggle("slip-open", expanded);
  predictionDrawer.hidden = mobile && !expanded;
  mobileSlipToggle.setAttribute("aria-expanded", String(expanded));
  predictionDrawer.toggleAttribute("aria-modal", expanded);
  if (expanded) {
    predictionDrawer.setAttribute("aria-modal", "true");
    predictionDrawer.setAttribute("role", "dialog");
  } else {
    predictionDrawer.removeAttribute("role");
  }
  slipBackground.forEach(node => { node.inert = expanded; });
  if (expanded) {
    predictionDrawerTitle?.focus();
  } else if (restoreFocus && lastFocusedBeforeSlip instanceof HTMLElement) {
    const target = mobile ? lastFocusedBeforeSlip : predictionDrawerTitle;
    target?.focus();
    lastFocusedBeforeSlip = null;
  }
}

if (predictionDrawer && mobileSlipToggle && closePredictionDrawer) {
  function syncMobileSlip() {
    const mobile = mobileSlipMedia.matches;
    const drawerHadFocus = predictionDrawer.contains(document.activeElement);
    document.body.classList.toggle("slip-enhanced", mobile);
    mobileSlipToggle.hidden = !mobile;
    closePredictionDrawer.hidden = !mobile;
    setMobileSlip(false);
    if (mobile && drawerHadFocus) mobileSlipToggle.focus();
  }
  syncMobileSlip();
  mobileSlipMedia.addEventListener("change", syncMobileSlip);
  mobileSlipToggle.addEventListener("click", () => setMobileSlip(true));
  closePredictionDrawer.addEventListener("click", () => setMobileSlip(false));
  document.querySelectorAll('a[href="#prediction-drawer"]').forEach(link => {
    link.addEventListener("click", event => {
      if (!mobileSlipMedia.matches) return;
      event.preventDefault();
      setMobileSlip(true);
    });
  });
  predictionDrawer.querySelectorAll('a[href^="#"]').forEach(link => {
    link.addEventListener("click", () => {
      if (!mobileSlipMedia.matches) return;
      setMobileSlip(false, false);
      lastFocusedBeforeSlip = null;
      const destination = document.querySelector(link.getAttribute("href"));
      if (destination) {
        destination.setAttribute("tabindex", "-1");
        destination.focus();
      }
    });
  });
  document.addEventListener("keydown", event => {
    if (!document.body.classList.contains("slip-open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setMobileSlip(false);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...predictionDrawer.querySelectorAll(FOCUSABLE)]
      .filter(node => node.offsetParent !== null);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === predictionDrawerTitle)) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  });
  if (window.location.hash === "#prediction-drawer" && mobileSlipMedia.matches) {
    setMobileSlip(true);
  }
}

liveDataPollTimer = setTimeout(pollLiveDataFreshness, LIVE_DATA_POLL_SECONDS * 1000);
