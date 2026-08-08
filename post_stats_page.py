"""The post-stats page's markup -- a single self-contained HTML document served by
post_stats_web.handle_page with "__PREFIX__" swapped for the route prefix, exactly like
arena_web.py and vote_web.py already do for their own PAGE_HTML constants.

Unlike those two, this is NOT a Telegram Mini App: it is a plain bookmarkable URL opened
in an ordinary browser, gated by a `?token=` query parameter rather than Telegram
initData. There is therefore no telegram-web-app.js, no tg-theme-* CSS variables, and no
Telegram-specific chrome -- just a light/dark-aware page built from CSS custom properties,
since the visitor's browser (not Telegram) decides which theme is active.

Everything here is one triple-quoted string, deliberately not an f-string: the JS below
is full of literal `{`/`}` characters that an f-string would try to interpret.
"""

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Post Stats</title>
<style>
  :root {
    color-scheme: light;
    --page-bg: #f9f9f7;
    --surface-1: #fcfcfb;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --baseline: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --series-1-hover: #1c5cab;
    --danger: #d03b3b;
    --danger-bg: rgba(208,59,59,0.08);
    --highlight-bg: rgba(42,120,214,0.16);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page-bg: #0d0d0d;
      --surface-1: #1a1a19;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --gridline: #2c2c2a;
      --baseline: #383835;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
      --series-1-hover: #184f95;
      --danger: #e66767;
      --danger-bg: rgba(230,103,103,0.14);
      --highlight-bg: rgba(57,135,229,0.22);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page-bg: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --gridline: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-1-hover: #184f95;
    --danger: #e66767;
    --danger-bg: rgba(230,103,103,0.14);
    --highlight-bg: rgba(57,135,229,0.22);
  }

  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { max-width: 100%; overflow-x: hidden; }
  body {
    margin: 0;
    background: var(--page-bg);
    color: var(--text-primary);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  a { color: var(--series-1); }

  /* ---------- header / controls (always visible) ---------- */
  header.topbar {
    position: sticky; top: 0; z-index: 30;
    background: var(--page-bg);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
  }
  .topbar-row { display: flex; align-items: baseline; justify-content: space-between;
                flex-wrap: wrap; gap: 4px 12px; margin-bottom: 10px; }
  .topbar-row h1 { font-size: 16px; margin: 0; font-weight: 700; }
  .meta { color: var(--text-secondary); font-size: 12px; }
  .controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .controls input[type="text"],
  .controls input[type="date"],
  .controls select {
    font: inherit; color: var(--text-primary); background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px;
  }
  #chatInput { min-width: 220px; flex: 1 1 220px; }
  .controls button {
    font: inherit; font-weight: 600; cursor: pointer;
    background: var(--series-1); color: #fff; border: 0; border-radius: 6px;
    padding: 8px 16px;
  }
  .controls button:hover { background: var(--series-1-hover); }
  .controls button:disabled { opacity: .55; cursor: default; }
  .loading { color: var(--text-secondary); font-size: 13px; display: inline-flex;
             align-items: center; gap: 6px; }
  .spinner {
    width: 13px; height: 13px; border-radius: 50%;
    border: 2px solid var(--gridline); border-top-color: var(--series-1);
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .banner {
    margin-top: 10px; padding: 8px 12px; border-radius: 6px; font-size: 13px;
  }
  .banner.error { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger); }
  .banner.notice { background: var(--surface-1); color: var(--text-secondary); border: 1px solid var(--border); }

  /* ---------- content ---------- */
  main { padding: 16px; max-width: 1180px; margin: 0 auto; }
  .empty-state {
    text-align: center; color: var(--text-secondary); padding: 48px 16px;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  }

  .filters {
    display: flex; align-items: flex-end; flex-wrap: wrap; gap: 10px 16px;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; margin-bottom: 14px;
  }
  .filter-field { display: flex; flex-direction: column; gap: 4px; }
  .filter-field label { font-size: 11px; color: var(--text-muted); text-transform: uppercase;
                         letter-spacing: .03em; }
  .filter-field input, .filter-field select {
    font: inherit; color: var(--text-primary); background: var(--page-bg);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
  }
  #minValueInput { width: 90px; }
  #searchInput { width: 180px; }

  .card {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; margin-bottom: 16px;
  }
  .card h2 { font-size: 13px; margin: 0 0 10px; color: var(--text-secondary);
             font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }

  .chart-scroll { overflow-x: auto; }
  #chart { display: block; }
  .chart-gridline { stroke: var(--gridline); stroke-width: 1; }
  .chart-axis { stroke: var(--baseline); stroke-width: 1; }
  .chart-axis-label { fill: var(--text-muted); font-size: 11px; }
  .chart-label { fill: var(--text-secondary); font-size: 12px; }
  .chart-value { fill: var(--text-primary); font-size: 12px; font-variant-numeric: tabular-nums; }
  .chart-bar {
    fill: var(--series-1);
    transition: width .3s ease, x .3s ease, fill .15s ease, opacity .15s ease;
  }
  .chart-bar.hover { fill: var(--series-1-hover); }
  .chart-hit { fill: transparent; cursor: pointer; }
  .empty-inline { color: var(--text-secondary); font-size: 13px; padding: 24px 0; text-align: center; }

  /* ---------- tooltip ---------- */
  #tooltip {
    position: fixed; z-index: 60; max-width: 280px;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; font-size: 12px; line-height: 1.5;
    box-shadow: 0 6px 20px rgba(0,0,0,.18);
    pointer-events: none;
  }
  #tooltip .tt-preview { font-weight: 600; margin-bottom: 4px; word-break: break-word; }
  #tooltip .tt-date { color: var(--text-muted); margin-bottom: 6px; }
  #tooltip .tt-row { display: flex; justify-content: space-between; gap: 12px; }
  #tooltip .tt-row span { color: var(--text-secondary); }
  #tooltip .tt-row strong { font-variant-numeric: tabular-nums; }
  #tooltip .tt-breakdown { margin: 2px 0 4px; color: var(--text-secondary); }

  /* ---------- table ---------- */
  .table-scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; min-width: 720px; font-size: 13px; }
  thead th {
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .03em;
    color: var(--text-muted); border-bottom: 1px solid var(--border);
    padding: 8px 10px; white-space: nowrap;
  }
  thead th.sortable { cursor: pointer; user-select: none; }
  thead th.sortable:hover { color: var(--text-secondary); }
  thead th .arrow { margin-left: 3px; color: var(--series-1); }
  tbody td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tbody tr:hover { background: rgba(128,128,128,.06); }
  tbody tr.highlight { background: var(--highlight-bg); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.col-text { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.col-thumb { width: 44px; }
  td.col-thumb img, td.col-thumb svg { width: 40px; height: 40px; border-radius: 6px;
                                        object-fit: cover; display: block; }
  td.col-edited { text-align: center; color: var(--text-muted); }
  td.col-open { text-align: right; white-space: nowrap; }
  .muted { color: var(--text-muted); font-size: 11px; }
  .empty-row td { text-align: center; color: var(--text-secondary); padding: 24px 0; }
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-row">
    <h1>Post Stats</h1>
    <div class="meta" id="chatMeta"></div>
  </div>
  <div class="controls" id="controls">
    <input type="text" id="chatInput" placeholder="@username, ID, or title">
    <select id="rangeSelect">
      <option value="today">Today</option>
      <option value="last7days">Last 7 days</option>
      <option value="last30days">Last 30 days</option>
      <option value="custom">Custom range</option>
    </select>
    <input type="date" id="startDate" hidden disabled>
    <input type="date" id="endDate" hidden disabled>
    <button id="loadBtn" type="button">Load</button>
    <span class="loading" id="loadingIndicator" hidden><span class="spinner"></span>Loading&hellip;</span>
  </div>
  <div class="banner error" id="errorBanner" role="alert" hidden></div>
  <div class="banner notice" id="tokenMissing" hidden>
    Open this page via the link that includes your access token.
  </div>
</header>

<main>
  <div class="empty-state" id="emptyState" hidden>No posts found in this range.</div>

  <section id="resultsSection" hidden>
    <div class="filters" id="filters">
      <div class="filter-field">
        <label for="metricSelect">Metric</label>
        <select id="metricSelect">
          <option value="views">Views</option>
          <option value="forwards">Forwards</option>
          <option value="reactions">Reactions</option>
          <option value="comments">Comments</option>
          <option value="engagement">Engagement</option>
        </select>
      </div>
      <div class="filter-field">
        <label for="mediaFilter">Media type</label>
        <select id="mediaFilter">
          <option value="all">All</option>
          <option value="photo">Photo</option>
          <option value="video">Video</option>
          <option value="album">Album</option>
          <option value="other">Other</option>
          <option value="none">Text only</option>
        </select>
      </div>
      <div class="filter-field">
        <label id="minValueLabel" for="minValueInput">Min Views</label>
        <input type="number" id="minValueInput" min="0" placeholder="0">
      </div>
      <div class="filter-field">
        <label for="searchInput">Search text</label>
        <input type="text" id="searchInput" placeholder="Search post text&hellip;">
      </div>
      <div class="filter-field">
        <label for="topNSelect">Show top N in chart</label>
        <select id="topNSelect">
          <option value="10">Top 10</option>
          <option value="15">Top 15</option>
          <option value="25" selected>Top 25</option>
          <option value="50">Top 50</option>
          <option value="all">All</option>
        </select>
      </div>
    </div>

    <div class="card" id="chartCard">
      <h2>Chart</h2>
      <div class="chart-scroll">
        <svg id="chart" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div class="empty-inline" id="chartEmpty" hidden>No posts match the current filters.</div>
    </div>

    <div class="card" id="tableCard">
      <h2>Posts</h2>
      <div class="table-scroll">
        <table id="postsTable">
          <thead>
            <tr>
              <th class="col-thumb"></th>
              <th class="sortable" data-key="text">Post<span class="arrow"></span></th>
              <th class="sortable num" data-key="views">Views<span class="arrow"></span></th>
              <th class="sortable num" data-key="forwards">Forwards<span class="arrow"></span></th>
              <th class="sortable num" data-key="reactions">Reactions<span class="arrow"></span></th>
              <th class="sortable num" data-key="comments">Comments<span class="arrow"></span></th>
              <th class="sortable num" data-key="engagement">Engagement<span class="arrow"></span></th>
              <th class="sortable" data-key="edited">Edited<span class="arrow"></span></th>
              <th class="col-open"></th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>
  </section>
</main>

<div id="tooltip" hidden></div>

<script>
(function () {
  "use strict";

  var PREFIX = "__PREFIX__";

  var TOKEN = null;
  var allPosts = [];
  var chatTitleValue = "";
  var periodLabelValue = "";
  var sortState = { key: "views", dir: "desc" };
  var searchDebounceTimer = null;
  var resizeDebounceTimer = null;

  var METRIC_COLUMN = {
    views: "views", forwards: "forwards", reactions: "reactions",
    comments: "comments", engagement: "engagement"
  };
  var METRIC_LABEL = {
    views: "Views", forwards: "Forwards", reactions: "Reactions",
    comments: "Comments", engagement: "Engagement"
  };

  var PLACEHOLDER_THUMB =
    '<svg class="thumb-placeholder" width="40" height="40" viewBox="0 0 40 40" ' +
    'aria-hidden="true" focusable="false">' +
    '<rect width="40" height="40" rx="6" fill="var(--gridline)"></rect>' +
    '<path d="M8 29l7-9 5 5 6-8 6 8" fill="none" stroke="var(--text-muted)" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>' +
    '<circle cx="15" cy="14" r="2.5" fill="var(--text-muted)"></circle>' +
    '</svg>';

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmt(n) {
    n = n == null || isNaN(n) ? 0 : n;
    return n.toLocaleString();
  }

  function truncate(s, n) {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso || "");
    try {
      return d.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
      });
    } catch (e) {
      return d.toISOString();
    }
  }

  function engagementOf(p) {
    return (p.reactions_total || 0) + (p.forwards || 0) + (p.comments || 0);
  }

  function metricValue(p, metric) {
    if (metric === "views") return p.views || 0;
    if (metric === "forwards") return p.forwards || 0;
    if (metric === "reactions") return p.reactions_total || 0;
    if (metric === "comments") return p.comments || 0;
    if (metric === "engagement") return engagementOf(p);
    return 0;
  }

  function isSafeHttpUrl(u) {
    return typeof u === "string" &&
      (u.slice(0, 7) === "http://" || u.slice(0, 8) === "https://");
  }

  /* ---------------- loading / errors ---------------- */

  function setLoading(isLoading) {
    $("loadingIndicator").hidden = !isLoading;
    $("loadBtn").disabled = isLoading;
  }

  function showError(msg) {
    var banner = $("errorBanner");
    banner.textContent = msg;
    banner.hidden = false;
  }

  function hideError() {
    $("errorBanner").hidden = true;
  }

  /* ---------------- init / URL state ---------------- */

  function applyRangeVisibility() {
    var custom = $("rangeSelect").value === "custom";
    $("startDate").hidden = !custom;
    $("startDate").disabled = !custom;
    $("endDate").hidden = !custom;
    $("endDate").disabled = !custom;
  }

  function updateMinLabel() {
    var metric = $("metricSelect").value;
    $("minValueLabel").textContent = "Min " + (METRIC_LABEL[metric] || "value");
  }

  function reflectUrl(chat) {
    var params = new URLSearchParams(location.search);
    params.set("chat", chat);
    var range = $("rangeSelect").value;
    if (range === "custom") {
      params.delete("range");
      params.set("start", $("startDate").value);
      params.set("end", $("endDate").value);
    } else {
      params.set("range", range);
      params.delete("start");
      params.delete("end");
    }
    var newUrl = location.pathname + "?" + params.toString();
    history.replaceState(null, "", newUrl);
  }

  function init() {
    var params = new URLSearchParams(location.search);
    TOKEN = params.get("token");
    if (!TOKEN) {
      $("tokenMissing").hidden = false;
      $("controls").hidden = true;
      return;
    }

    var chat = params.get("chat") || "";
    $("chatInput").value = chat;

    var range = params.get("range");
    var start = params.get("start");
    var end = params.get("end");
    if (!range && (start || end)) {
      $("rangeSelect").value = "custom";
      $("startDate").value = start || "";
      $("endDate").value = end || "";
    } else if (range === "today" || range === "last7days" || range === "last30days") {
      $("rangeSelect").value = range;
    }
    applyRangeVisibility();
    updateMinLabel();

    wireEvents();

    if (chat) loadData();
  }

  function wireEvents() {
    $("rangeSelect").addEventListener("change", applyRangeVisibility);
    $("loadBtn").addEventListener("click", loadData);
    $("chatInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); loadData(); }
    });

    $("metricSelect").addEventListener("change", function () {
      sortState = { key: METRIC_COLUMN[$("metricSelect").value], dir: "desc" };
      updateMinLabel();
      renderAll();
    });
    $("mediaFilter").addEventListener("change", renderAll);
    $("minValueInput").addEventListener("input", renderAll);
    $("topNSelect").addEventListener("change", renderChart);
    $("searchInput").addEventListener("input", function () {
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(renderAll, 150);
    });

    var headers = document.querySelectorAll("#postsTable thead th.sortable");
    for (var i = 0; i < headers.length; i++) {
      headers[i].addEventListener("click", function () {
        onHeaderClick(this.getAttribute("data-key"));
      });
    }

    window.addEventListener("resize", function () {
      clearTimeout(resizeDebounceTimer);
      resizeDebounceTimer = setTimeout(function () {
        if (allPosts.length) renderChart();
      }, 150);
    });
  }

  /* ---------------- data loading ---------------- */

  function loadData() {
    hideError();
    if (!TOKEN) return;

    var chat = $("chatInput").value.trim();
    if (!chat) { showError("Enter a chat username, ID, or title."); return; }

    var range = $("rangeSelect").value;
    var params = new URLSearchParams();
    params.set("token", TOKEN);
    params.set("chat", chat);
    if (range === "custom") {
      var start = $("startDate").value;
      var end = $("endDate").value;
      if (!start || !end) {
        showError("Pick both a start and end date for the custom range.");
        return;
      }
      params.set("start", start);
      params.set("end", end);
    } else {
      params.set("range", range);
    }

    setLoading(true);
    fetch(PREFIX + "/api/data?" + params.toString())
      .then(function (resp) {
        return resp.json().catch(function () { return null; }).then(function (data) {
          if (!resp.ok || !data || data.error) {
            var message = (data && data.error) || ("Request failed (" + resp.status + ")");
            throw new Error(message);
          }
          return data;
        });
      })
      .then(function (data) {
        allPosts = Array.isArray(data.posts) ? data.posts : [];
        chatTitleValue = data.chat_title || chat;
        periodLabelValue = data.period_label || "";
        updateMeta();
        sortState = { key: METRIC_COLUMN[$("metricSelect").value] || "views", dir: "desc" };

        $("resultsSection").hidden = false;
        if (allPosts.length === 0) {
          $("emptyState").hidden = false;
          $("filters").hidden = true;
          $("chartCard").hidden = true;
          $("tableCard").hidden = true;
        } else {
          $("emptyState").hidden = true;
          $("filters").hidden = false;
          $("chartCard").hidden = false;
          $("tableCard").hidden = false;
          renderAll();
        }
        reflectUrl(chat);
      })
      .catch(function (err) {
        showError(err && err.message ? err.message : "Something went wrong.");
      })
      .then(function () { setLoading(false); }, function () { setLoading(false); });
  }

  function updateMeta() {
    var parts = [];
    if (chatTitleValue) parts.push(chatTitleValue);
    if (periodLabelValue) parts.push(periodLabelValue);
    $("chatMeta").textContent = parts.join(" — ");
  }

  /* ---------------- filtering ---------------- */

  function getFiltered() {
    var mediaFilter = $("mediaFilter").value;
    var minRaw = $("minValueInput").value;
    var minVal = minRaw === "" ? null : parseFloat(minRaw);
    var searchTerm = $("searchInput").value.trim().toLowerCase();
    var metric = $("metricSelect").value;

    return allPosts.filter(function (p) {
      if (mediaFilter !== "all" && (p.media_type || "none") !== mediaFilter) return false;
      if (minVal !== null && !isNaN(minVal) && metricValue(p, metric) < minVal) return false;
      if (searchTerm && (p.text_preview || "").toLowerCase().indexOf(searchTerm) === -1) return false;
      return true;
    });
  }

  function renderAll() {
    renderChart();
    renderTable();
  }

  /* ---------------- chart ---------------- */

  function niceNumber(range, round) {
    if (range <= 0) return 1;
    var exponent = Math.floor(Math.log(range) / Math.LN10);
    var fraction = range / Math.pow(10, exponent);
    var niceFraction;
    if (round) {
      if (fraction < 1.5) niceFraction = 1;
      else if (fraction < 3) niceFraction = 2;
      else if (fraction < 7) niceFraction = 5;
      else niceFraction = 10;
    } else {
      if (fraction <= 1) niceFraction = 1;
      else if (fraction <= 2) niceFraction = 2;
      else if (fraction <= 5) niceFraction = 5;
      else niceFraction = 10;
    }
    return niceFraction * Math.pow(10, exponent);
  }

  function niceTicks(max, tickCount) {
    if (max <= 0) max = 1;
    var spacing = niceNumber(max / (tickCount - 1), true);
    var niceMax = Math.ceil(max / spacing) * spacing;
    var ticks = [];
    for (var v = 0; v <= niceMax + 1e-9; v += spacing) {
      ticks.push(Math.round(v * 100) / 100);
    }
    return ticks;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function renderChart() {
    var svg = $("chart");
    var chartEmpty = $("chartEmpty");
    var filtered = getFiltered();
    var metric = $("metricSelect").value;
    var topN = $("topNSelect").value;

    var sorted = filtered.slice().sort(function (a, b) {
      return metricValue(b, metric) - metricValue(a, metric);
    });
    if (topN !== "all") sorted = sorted.slice(0, parseInt(topN, 10) || 25);

    while (svg.firstChild) svg.removeChild(svg.firstChild);

    if (sorted.length === 0) {
      svg.setAttribute("width", 0);
      svg.setAttribute("height", 0);
      chartEmpty.hidden = false;
      return;
    }
    chartEmpty.hidden = true;

    var containerWidth = svg.parentElement.clientWidth || 640;
    var W = Math.max(480, containerWidth);
    var rowHeight = 30;
    var barHeight = 16;
    var margin = { top: 8, right: 64, bottom: 30, left: 200 };
    var plotWidth = Math.max(60, W - margin.left - margin.right);
    var plotHeight = sorted.length * rowHeight;
    var H = margin.top + plotHeight + margin.bottom;

    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);

    var maxVal = 0;
    for (var i = 0; i < sorted.length; i++) {
      var v = metricValue(sorted[i], metric);
      if (v > maxVal) maxVal = v;
    }
    if (maxVal <= 0) maxVal = 1;

    var ticks = niceTicks(maxVal, 5);
    var tickMax = ticks[ticks.length - 1] || maxVal;

    var gridGroup = svgEl("g", {});
    for (var t = 0; t < ticks.length; t++) {
      var tx = margin.left + (ticks[t] / tickMax) * plotWidth;
      gridGroup.appendChild(svgEl("line", {
        class: "chart-gridline",
        x1: tx, x2: tx, y1: margin.top, y2: margin.top + plotHeight
      }));
      var tickLabel = svgEl("text", {
        class: "chart-axis-label", x: tx, y: margin.top + plotHeight + 18,
        "text-anchor": "middle"
      });
      tickLabel.textContent = fmt(ticks[t]);
      gridGroup.appendChild(tickLabel);
    }
    gridGroup.appendChild(svgEl("line", {
      class: "chart-axis", x1: margin.left, x2: margin.left,
      y1: margin.top, y2: margin.top + plotHeight
    }));
    svg.appendChild(gridGroup);

    for (var r = 0; r < sorted.length; r++) {
      var post = sorted[r];
      var rowY = margin.top + r * rowHeight;
      var value = metricValue(post, metric);
      var barWidth = Math.max(value > 0 ? 2 : 0, (value / tickMax) * plotWidth);
      var barY = rowY + (rowHeight - barHeight) / 2;

      var row = svgEl("g", { class: "chart-row" });

      var label = svgEl("text", {
        class: "chart-label", x: margin.left - 8, y: rowY + rowHeight / 2 + 4,
        "text-anchor": "end"
      });
      label.textContent = truncate(post.text_preview || "(no text)", 26);
      row.appendChild(label);

      var bar = svgEl("rect", {
        class: "chart-bar", x: margin.left, y: barY,
        width: barWidth, height: barHeight, rx: 4, ry: 4
      });
      row.appendChild(bar);

      var valueLabel = svgEl("text", {
        class: "chart-value", x: margin.left + barWidth + 6, y: rowY + rowHeight / 2 + 4,
        "text-anchor": "start"
      });
      valueLabel.textContent = fmt(value);
      row.appendChild(valueLabel);

      var hit = svgEl("rect", {
        class: "chart-hit", x: 0, y: rowY, width: W, height: rowHeight
      });
      (function (postRef, barRef) {
        hit.addEventListener("mouseenter", function (e) {
          barRef.classList.add("hover");
          showTooltip(postRef, e.clientX, e.clientY);
        });
        hit.addEventListener("mousemove", function (e) {
          positionTooltip(e.clientX, e.clientY);
        });
        hit.addEventListener("mouseleave", function () {
          barRef.classList.remove("hover");
          hideTooltip();
        });
        hit.addEventListener("click", function () {
          scrollToRow(postRef.message_id);
        });
      })(post, bar);
      row.appendChild(hit);

      svg.appendChild(row);
    }
  }

  /* ---------------- tooltip ---------------- */

  function tooltipHtml(p) {
    var eng = engagementOf(p);
    var breakdown = "";
    if (p.reactions_breakdown && typeof p.reactions_breakdown === "object") {
      var parts = [];
      for (var key in p.reactions_breakdown) {
        if (!Object.prototype.hasOwnProperty.call(p.reactions_breakdown, key)) continue;
        parts.push(esc(key) + " " + esc(fmt(p.reactions_breakdown[key])));
      }
      breakdown = parts.join(" · ");
    }
    var html =
      '<div class="tt-preview">' + esc(truncate(p.text_preview || "(no text)", 140)) + "</div>" +
      '<div class="tt-date">' + esc(formatDate(p.date)) + "</div>" +
      '<div class="tt-row"><span>Views</span><strong>' + esc(fmt(p.views)) + "</strong></div>" +
      '<div class="tt-row"><span>Forwards</span><strong>' + esc(fmt(p.forwards)) + "</strong></div>" +
      '<div class="tt-row"><span>Reactions</span><strong>' + esc(fmt(p.reactions_total)) + "</strong></div>";
    if (breakdown) {
      html += '<div class="tt-breakdown">' + breakdown + "</div>";
    }
    html +=
      '<div class="tt-row"><span>Comments</span><strong>' + esc(fmt(p.comments)) + "</strong></div>" +
      '<div class="tt-row"><span>Engagement</span><strong>' + esc(fmt(eng)) + "</strong></div>" +
      '<div class="tt-row"><span>Media</span><strong>' + esc(p.media_type || "none") + "</strong></div>" +
      '<div class="tt-row"><span>Edited</span><strong>' + (p.is_edited ? "yes" : "no") + "</strong></div>";
    return html;
  }

  function showTooltip(p, x, y) {
    var tip = $("tooltip");
    tip.innerHTML = tooltipHtml(p);
    tip.hidden = false;
    positionTooltip(x, y);
  }

  function positionTooltip(x, y) {
    var tip = $("tooltip");
    if (tip.hidden) return;
    var pad = 14;
    var rect = tip.getBoundingClientRect();
    var left = x + pad;
    var top = y + pad;
    if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
    if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
    if (left < 8) left = 8;
    if (top < 8) top = 8;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function hideTooltip() {
    $("tooltip").hidden = true;
  }

  function scrollToRow(messageId) {
    var row = document.getElementById("post-" + messageId);
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("highlight");
    setTimeout(function () { row.classList.remove("highlight"); }, 1500);
  }

  /* ---------------- table ---------------- */

  function sortValue(p, key) {
    if (key === "text") return (p.text_preview || "").toLowerCase();
    if (key === "views") return p.views || 0;
    if (key === "forwards") return p.forwards || 0;
    if (key === "reactions") return p.reactions_total || 0;
    if (key === "comments") return p.comments || 0;
    if (key === "engagement") return engagementOf(p);
    if (key === "edited") return p.is_edited ? 1 : 0;
    return 0;
  }

  function onHeaderClick(key) {
    if (!key) return;
    if (sortState.key === key) {
      sortState.dir = sortState.dir === "desc" ? "asc" : "desc";
    } else {
      sortState = { key: key, dir: "desc" };
    }
    renderTable();
  }

  function updateSortIndicators() {
    var headers = document.querySelectorAll("#postsTable thead th.sortable");
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var arrow = th.querySelector(".arrow");
      if (th.getAttribute("data-key") === sortState.key) {
        arrow.textContent = sortState.dir === "desc" ? "▼" : "▲";
      } else {
        arrow.textContent = "";
      }
    }
  }

  function tableRowHtml(p) {
    var eng = engagementOf(p);
    var pct = p.views ? ((100 * eng / p.views).toFixed(1) + "%") : null;

    var breakdownTitle = "";
    if (p.reactions_breakdown && typeof p.reactions_breakdown === "object") {
      var parts = [];
      for (var key in p.reactions_breakdown) {
        if (!Object.prototype.hasOwnProperty.call(p.reactions_breakdown, key)) continue;
        parts.push(key + " " + p.reactions_breakdown[key]);
      }
      breakdownTitle = parts.join(", ");
    }

    var thumb = p.thumbnail_url
      ? '<img loading="lazy" src="' + esc(p.thumbnail_url) + '" alt="">'
      : PLACEHOLDER_THUMB;

    var openCell = isSafeHttpUrl(p.link)
      ? '<a href="' + esc(p.link) + '" target="_blank" rel="noopener">Open ↗</a>'
      : "";

    return (
      '<tr id="post-' + esc(String(p.message_id)) + '">' +
        '<td class="col-thumb">' + thumb + "</td>" +
        '<td class="col-text" title="' + esc(p.text_preview || "") + '">' +
          esc(truncate(p.text_preview || "(no text)", 90)) +
        "</td>" +
        '<td class="num">' + esc(fmt(p.views)) + "</td>" +
        '<td class="num">' + esc(fmt(p.forwards)) + "</td>" +
        '<td class="num" title="' + esc(breakdownTitle) + '">' + esc(fmt(p.reactions_total)) + "</td>" +
        '<td class="num">' + esc(fmt(p.comments)) + "</td>" +
        '<td class="num">' + esc(fmt(eng)) +
          (pct ? ' <span class="muted">(' + esc(pct) + ")</span>" : "") +
        "</td>" +
        '<td class="col-edited">' + (p.is_edited ? "✓" : "—") + "</td>" +
        '<td class="col-open">' + openCell + "</td>" +
      "</tr>"
    );
  }

  function renderTable() {
    var body = $("tableBody");
    var filtered = getFiltered();

    var sorted = filtered.slice().sort(function (a, b) {
      var va = sortValue(a, sortState.key);
      var vb = sortValue(b, sortState.key);
      var cmp;
      if (typeof va === "string") cmp = va.localeCompare(vb);
      else cmp = va - vb;
      return sortState.dir === "asc" ? cmp : -cmp;
    });

    if (sorted.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="9">No posts match the current filters.</td></tr>';
    } else {
      body.innerHTML = sorted.map(tableRowHtml).join("");
    }
    updateSortIndicators();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
</script>
</body>
</html>
"""
