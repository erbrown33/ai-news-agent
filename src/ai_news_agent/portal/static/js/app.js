/**
 * app.js — AI News Portal client-side JavaScript.
 *
 * Modules:
 *   - switchAgent()      Agent switcher dropdown navigation (SRC-134)
 *   - initTagCloud()     Theme word cloud with frequency-weighted sizing (SRC-134)
 *   - initImpactFilter() Impact category filter bar for article cards (SRC-134)
 *   - initCadenceTabs()  Cadence tab switcher on index page (SRC-133)
 *   - triggerJob()       POST /api/trigger dispatcher (SRC-147)
 *   - initDownloadTracking() Download analytics stub (SRC-136)
 *
 * Traces: SRC-004 (portal), SRC-029 (daily article cards),
 *         SRC-030 (weekly themes), SRC-031 (monthly themes),
 *         SRC-032 (annual view), SRC-133 (cadence views),
 *         SRC-134 (theme filter, word cloud, agent switcher, model-provider filter),
 *         SRC-136 (download triggers), SRC-147 (trigger API)
 */

"use strict";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

/** Min/max font-size for theme word cloud (em relative to parent). */
const CLOUD_FONT_MIN = 0.78;
const CLOUD_FONT_MAX = 1.55;

/** Transition applied to article cards during filter operations. */
const CARD_TRANSITION = "opacity 0.2s ease, transform 0.2s ease";


// ─────────────────────────────────────────────────────────────────────────────
// Agent Switcher (SRC-134)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Navigate to the same cadence/date for a different agent ID.
 * Reads the current URL to infer cadence and date, then navigates to
 * the equivalent digest for the selected agent, or home if on index.
 *
 * @param {string} agentId - The selected agent_id from the dropdown.
 */
function switchAgent(agentId) {
  const path = window.location.pathname;
  const match = path.match(/^\/digest\/([^/]+)\/([^/]+)\/([^/]+)$/);
  if (match) {
    const [, , dateStr, cadence] = match;
    window.location.href = `/digest/${encodeURIComponent(agentId)}/${dateStr}/${cadence}`;
  } else {
    window.location.href = "/";
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// Theme Word Cloud (SRC-134 — visual weight by frequency)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Apply frequency-based font size scaling to theme tags so that more prominent
 * themes appear visually larger — a proper "word cloud" effect.
 *
 * Each .theme-tag element should carry a ``data-weight`` attribute (integer ≥ 1)
 * set by the Jinja2 template from the server-computed theme_weights dict.
 *
 * Traces: SRC-134 (theme word cloud)
 */
function applyWordCloudSizes() {
  const tags = Array.from(document.querySelectorAll(".theme-tag[data-weight]"));
  if (tags.length === 0) return;

  const weights = tags.map(t => parseInt(t.dataset.weight || "1", 10));
  const minW = Math.min(...weights);
  const maxW = Math.max(...weights);
  const range = maxW - minW || 1;

  tags.forEach(tag => {
    const w = parseInt(tag.dataset.weight || "1", 10);
    const normalized = (w - minW) / range;   // 0.0 … 1.0
    const size = CLOUD_FONT_MIN + normalized * (CLOUD_FONT_MAX - CLOUD_FONT_MIN);
    tag.style.fontSize = `${size.toFixed(2)}em`;
    // Slightly adjust padding for smaller tags
    const pad = 4 + Math.round(normalized * 4);
    tag.style.padding = `${pad}px ${10 + Math.round(normalized * 6)}px`;
  });
}


/**
 * Initialise the theme tag cloud on weekly/monthly/annual pages.
 *
 * Clicking a tag toggles it as a filter — when one or more tags are active,
 * article cards whose text does NOT contain any active theme keyword are dimmed.
 * Clicking the same tag again deactivates it and restores all cards.
 *
 * Multiple tags use OR logic (article matches if it contains ANY active theme).
 *
 * Traces: SRC-134 (theme filter)
 */
function initTagCloud() {
  applyWordCloudSizes();

  const tags = document.querySelectorAll(".theme-tag");
  const articles = document.querySelectorAll(".article-card");
  const hint = document.querySelector(".theme-filter-hint");

  if (tags.length === 0) return;

  /** Update article card visibility based on current active tags. */
  function updateFilter() {
    const activeTags = Array.from(document.querySelectorAll(".theme-tag.active"))
      .map(t => t.dataset.theme.toLowerCase().trim());

    if (hint) {
      hint.style.display = activeTags.length > 0 ? "block" : "none";
    }

    articles.forEach(card => {
      if (activeTags.length === 0) {
        card.classList.remove("dimmed");
      } else {
        const text = (
          (card.querySelector(".article-headline")?.textContent || "") +
          " " +
          (card.querySelector(".why-text")?.textContent || "")
        ).toLowerCase();
        const matches = activeTags.some(theme => text.includes(theme));
        card.classList.toggle("dimmed", !matches);
      }
    });
  }

  tags.forEach(tag => {
    tag.addEventListener("click", () => {
      tag.classList.toggle("active");
      updateFilter();
    });
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Impact Category Filter Bar (SRC-134)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialise the impact-category filter pills on daily/weekly/monthly views.
 *
 * Pills: All | 💼 Business | 👥 Workforce | ⚖️ Policy
 *
 * Clicking a category shows only articles tagged with that impact type.
 * "All" resets to show everything.
 *
 * Traces: SRC-134 (model-provider / category filter)
 */
function initImpactFilter() {
  const filterContainer = document.getElementById("impact-filter");
  if (!filterContainer) return;

  const articles = document.querySelectorAll(".article-card");
  const pills = filterContainer.querySelectorAll("[data-filter]");

  pills.forEach(pill => {
    pill.addEventListener("click", () => {
      const filter = pill.dataset.filter;

      // Toggle: clicking the active filter again → reset to all
      const wasActive = pill.classList.contains("active") && filter !== "all";
      pills.forEach(p => p.classList.remove("active"));

      if (wasActive) {
        filterContainer.querySelector("[data-filter='all']")?.classList.add("active");
      } else {
        pill.classList.add("active");
      }

      const activeFilter = wasActive ? "all" : filter;

      articles.forEach(card => {
        if (activeFilter === "all") {
          card.style.display = "";
          card.classList.remove("dimmed");
        } else {
          const hasTag = card.querySelector(`.impact-badge.impact-${activeFilter}`);
          card.style.display = hasTag ? "" : "none";
        }
      });
    });
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Cadence Tab Switcher — Index Page (SRC-133)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Initialise cadence tab switching on the index page.
 *
 * Each agent section has a row of cadence tabs (Daily/Weekly/Monthly/Annual).
 * Clicking a tab shows the corresponding .digest-panel for that cadence.
 * Tab state is stored in sessionStorage so the last-viewed cadence persists
 * within a browser session.
 *
 * Traces: SRC-133 (cadence-specific views), SRC-134 (agent switcher context)
 */
function initCadenceTabs() {
  document.querySelectorAll(".cadence-tabs").forEach(tabBar => {
    const agentId = tabBar.dataset.agent;
    const panelContainer = document.querySelector(`.digest-panels[data-agent="${agentId}"]`);
    if (!panelContainer) return;

    const tabs = tabBar.querySelectorAll(".cadence-tab");
    const panels = panelContainer.querySelectorAll(".digest-panel");

    /**
     * Activate a specific tab by its cadence value.
     * @param {string} cadence - "daily" | "weekly" | "monthly" | "annual"
     */
    function activateTab(cadence) {
      tabs.forEach(t => {
        const isActive = t.dataset.cadence === cadence;
        t.classList.toggle("active", isActive);
      });
      panels.forEach(p => {
        const isActive = p.dataset.cadence === cadence;
        p.classList.toggle("active", isActive);
      });
      try {
        sessionStorage.setItem(`cadence-tab-${agentId}`, cadence);
      } catch (_) { /* ignore sessionStorage errors in strict sandboxes */ }
    }

    // Restore previously selected tab, or activate first non-empty cadence
    let restoredCadence = null;
    try {
      restoredCadence = sessionStorage.getItem(`cadence-tab-${agentId}`);
    } catch (_) { /* ignore */ }

    // Find first tab that has at least one digest
    const firstAvailable = Array.from(tabs).find(t => {
      const panel = panelContainer.querySelector(`.digest-panel[data-cadence="${t.dataset.cadence}"]`);
      return panel && panel.querySelectorAll(".digest-card").length > 0;
    });

    const initialCadence = (
      restoredCadence &&
      panelContainer.querySelector(`.digest-panel[data-cadence="${restoredCadence}"]`)
        ?.querySelectorAll(".digest-card").length > 0
        ? restoredCadence
        : firstAvailable?.dataset.cadence
    ) || tabs[0]?.dataset.cadence;

    if (initialCadence) activateTab(initialCadence);

    tabs.forEach(tab => {
      tab.addEventListener("click", () => activateTab(tab.dataset.cadence));
    });
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Download Trigger (SRC-136)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Track download button clicks for telemetry (future enhancement).
 *
 * Traces: SRC-136 (export downloads from portal)
 */
function initDownloadTracking() {
  document.querySelectorAll(".export-btn, .download-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const href = btn.getAttribute("href") || "";
      // Stub — replace with analytics call in production
      if (typeof console !== "undefined") {
        console.debug("[portal] download triggered:", href);
      }
    });
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Manual Trigger (SRC-147 — POST /api/trigger)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Programmatically trigger a sourcing or curation job via the API.
 *
 * Callers should wrap with try/catch to handle HTTP errors gracefully.
 *
 * @param {string} agentId   - Agent configuration ID.
 * @param {string} jobType   - "sourcing" | "curation"
 * @param {string|null} cadence - Cadence string (required for curation jobs).
 * @param {string|null} apiKey  - Optional bearer token for SCHEDULER_API_KEY auth.
 * @returns {Promise<object>} - API response JSON.
 *
 * Traces: SRC-028 (re-runnable on demand), SRC-147 (manual override)
 */
async function triggerJob(agentId, jobType, cadence = null, apiKey = null) {
  const body = { agent_id: agentId, job_type: jobType };
  if (cadence) body.cadence = cadence;

  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;

  const response = await fetch("/api/trigger", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const errBody = await response.text();
    throw new Error(`Trigger failed (${response.status}): ${errBody}`);
  }

  return response.json();
}


// ─────────────────────────────────────────────────────────────────────────────
// Keyboard shortcut: Escape deactivates all active theme tags
// ─────────────────────────────────────────────────────────────────────────────

function initKeyboardShortcuts() {
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      // Clear all theme filters
      document.querySelectorAll(".theme-tag.active").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".article-card.dimmed").forEach(c => c.classList.remove("dimmed"));
      // Reset impact filter to "all"
      document.querySelectorAll("[data-filter]").forEach(p => p.classList.remove("active"));
      document.querySelector("[data-filter='all']")?.classList.add("active");
      document.querySelectorAll(".article-card").forEach(c => {
        c.style.display = "";
        c.classList.remove("dimmed");
      });
    }
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Smooth scroll for in-page anchor links
// ─────────────────────────────────────────────────────────────────────────────

function initSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener("click", e => {
      const target = document.querySelector(anchor.getAttribute("href") || "");
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
}


// ─────────────────────────────────────────────────────────────────────────────
// Initialisation — runs after DOM is ready
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initTagCloud();
  initImpactFilter();
  initCadenceTabs();
  initDownloadTracking();
  initKeyboardShortcuts();
  initSmoothScroll();
});
