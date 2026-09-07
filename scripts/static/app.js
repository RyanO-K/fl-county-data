"use strict";

/* ---------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------- */
const state = {
  county: "",
  dataset_type: "",
  zoning_code: "",
  land_use_code: "",
  min_acreage: "",
  max_acreage: "",
  q: "",
  page: 1,
  per_page: 50,
  view: "map",
  mainTab: "browse",
};

let lastFeaturesTotal = null;
let featureRowIndex = new Map(); // id -> {el, values}
let statusRowIndex = new Map(); // county -> {el, cells} on the Pipeline Status table
let leafletMap = null;
let geoLayer = null;
let colorCache = new Map();
let combosCache = []; // from /api/counties
let mapLoadedKey = null; // filter params the map currently shows

/* Parcel Values tab state. */
const vstate = {
  county: "",
  use_code: "",
  min_jv: "",
  max_jv: "",
  min_sale: "",
  max_sale: "",
  q: "",
  sort: "just_value",
  dir: "desc",
  page: 1,
  per_page: 50,
};
let valuesRowIndex = new Map(); // "county|parcel_id" -> {el, values}
let valuesCountiesCache = []; // from /api/values/counties
let valuesLoaded = false;

/* ---------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------- */
async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> HTTP ${resp.status}`);
  return resp.json();
}

function qs(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v !== null && v !== undefined) usp.set(k, v);
  }
  return usp.toString();
}

function currentFilterParams(extra) {
  return Object.assign({
    county: state.county,
    dataset_type: state.dataset_type,
    zoning_code: state.zoning_code,
    land_use_code: state.land_use_code,
    min_acreage: state.min_acreage,
    max_acreage: state.max_acreage,
    q: state.q,
  }, extra || {});
}

function fmtNum(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtMoney(n) {
  if (n === null || n === undefined) return "—";
  return "$" + Math.round(n).toLocaleString();
}

/* Human-readable labels: county/dataset keys are snake_case in the DB. */
const COUNTY_LABELS = {
  miami_dade: "Miami-Dade", st_johns: "St. Johns", st_lucie: "St. Lucie",
  desoto: "DeSoto", palm_beach: "Palm Beach", indian_river: "Indian River",
  santa_rosa: "Santa Rosa",
};
function pretty(s) {
  if (s === null || s === undefined || s === "") return "—";
  if (COUNTY_LABELS[s]) return COUNTY_LABELS[s];
  return String(s).split("_").filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}
function prettyKey(k) {
  // Raw source attribute names: OBJECTID, ZONE_NAME, ZoningNm ...
  if (/^objectid(_\d+)?$/i.test(k)) return "Object ID";
  return String(k).split("_").filter(Boolean).map((w) =>
    w === w.toUpperCase() || w === w.toLowerCase()
      ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
      : w).join(" ");
}
function esc(v) {
  if (v === null || v === undefined) return "—";
  return String(v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------------------------------------------------------------
 * Detail-row labels with hover explanations (glossary.js)
 *
 * Abbreviated tax-roll field names (JV, DOR_UC, AV_NSD, TOT_LVG_AR ...) mean
 * nothing on sight, so every label we can explain is wrapped in an <abbr> with
 * a dotted underline and a help cursor telling the reader a tooltip is there.
 * Labels we cannot explain are left plain.
 * ------------------------------------------------------------------- */
function glossaryTh(label, desc, rawKey) {
  const text = esc(label);
  if (desc) {
    return `<th><abbr class="gloss" title="${esc(rawKey ? `${rawKey} — ${desc}` : desc)}">${text}</abbr></th>`;
  }
  if (rawKey) {
    const generic = (window.FLGlossary && FLGlossary.generic) ||
      "Raw attribute from the source layer.";
    return `<th title="${esc(`${rawKey} — ${generic}`)}">${text}</th>`;
  }
  return `<th>${text}</th>`;
}
/* Label for one of our normalized columns ("Just value", "Acreage", ...). */
function fieldTh(label) {
  return glossaryTh(label, window.FLGlossary ? FLGlossary.field(label) : null, null);
}
/* Label for a raw source-layer attribute key (DOR_UC, PHY_ADDR1, ...). */
function attrTh(key) {
  return glossaryTh(prettyKey(key), window.FLGlossary ? FLGlossary.attr(key) : null, key);
}

/* ---------------------------------------------------------------------
 * Detail modal open/close
 *
 * Both details share #detail-modal. Opening locks the page behind it so the
 * only scrollbar on screen belongs to the modal's own body; the padding
 * compensates for the page scrollbar we just removed so nothing shifts.
 * ------------------------------------------------------------------- */
function openDetailModal(title) {
  const modal = document.getElementById("detail-modal");
  document.getElementById("detail-title").textContent = title;
  // Measure the page scrollbar before hiding it, so re-opening an already open
  // modal does not measure zero and undo the compensation.
  if (modal.hidden) {
    const gutter = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.paddingRight = gutter > 0 ? `${gutter}px` : "";
  }
  document.body.classList.add("modal-open");
  modal.hidden = false;
}

function closeDetailModal() {
  document.getElementById("detail-modal").hidden = true;
  document.body.classList.remove("modal-open");
  document.body.style.paddingRight = "";
}

function fmtSaleDate(year, month) {
  if (!year) return "—";
  return month ? `${year}-${String(month).padStart(2, "0")}` : String(year);
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch (e) {
    return iso;
  }
}

function flashCell(el) {
  el.classList.remove("flash");
  // force reflow so the animation restarts
  void el.offsetWidth;
  el.classList.add("flash");
}

function setTextIfChanged(el, text) {
  if (el.textContent !== text) {
    el.textContent = text;
    flashCell(el);
    return true;
  }
  return false;
}

/* ---------------------------------------------------------------------
 * Dropdowns: counties / datasets / facets
 * ------------------------------------------------------------------- */
async function loadCombos() {
  combosCache = await fetchJSON("/api/counties");
  const countySel = document.getElementById("f-county");
  const datasetSel = document.getElementById("f-dataset");

  const counties = [...new Set(combosCache.map((c) => c.county))].sort();
  const prevCounty = countySel.value;
  countySel.innerHTML = '<option value="">All</option>' +
    counties.map((c) => `<option value="${c}">${pretty(c)}</option>`).join("");
  countySel.value = counties.includes(prevCounty) ? prevCounty : "";

  refreshDatasetOptions();
}

function refreshDatasetOptions() {
  const datasetSel = document.getElementById("f-dataset");
  const county = document.getElementById("f-county").value;
  const relevant = county
    ? combosCache.filter((c) => c.county === county)
    : combosCache;
  const datasets = [...new Set(relevant.map((c) => c.dataset_type))].sort();
  const prev = datasetSel.value;
  datasetSel.innerHTML = '<option value="">All</option>' +
    datasets.map((d) => `<option value="${d}">${pretty(d)}</option>`).join("");
  datasetSel.value = datasets.includes(prev) ? prev : "";
}

async function loadFacets() {
  const zoningSel = document.getElementById("f-zoning");
  const landuseSel = document.getElementById("f-landuse");
  const params = qs({ county: state.county, dataset_type: state.dataset_type });
  const data = await fetchJSON(`/api/facets?${params}`);

  const prevZ = zoningSel.value;
  zoningSel.innerHTML = '<option value="">All</option>' +
    data.zoning_codes
      .map((z) => `<option value="${z.zoning_code}">${z.zoning_code}${z.zoning_desc ? " - " + truncate(z.zoning_desc, 40) : ""}</option>`)
      .join("");
  zoningSel.value = data.zoning_codes.some((z) => z.zoning_code === prevZ) ? prevZ : "";

  const prevL = landuseSel.value;
  landuseSel.innerHTML = '<option value="">All</option>' +
    data.land_use_codes
      .map((l) => `<option value="${l.land_use_code}">${l.land_use_code}${l.land_use_desc ? " - " + truncate(l.land_use_desc, 40) : ""}</option>`)
      .join("");
  landuseSel.value = data.land_use_codes.some((l) => l.land_use_code === prevL) ? prevL : "";
}

function truncate(s, n) {
  return s && s.length > n ? s.slice(0, n) + "…" : s;
}

/* ---------------------------------------------------------------------
 * Features table
 * ------------------------------------------------------------------- */
function rowValues(row) {
  return {
    county: row.county,
    dataset_type: row.dataset_type,
    feature_key: row.feature_key,
    city: row.city,
    acreage: row.acreage,
    zoning_code: row.zoning_code,
    zoning_desc: row.zoning_desc,
    land_use_code: row.land_use_code,
    land_use_desc: row.land_use_desc,
    land_value: row.land_value,
    building_value: row.building_value,
    total_value: row.total_value,
    just_value: row.just_value,
    sale_price: row.sale_price,
    sale_date: row.sale_date,
    last_synced_at: row.last_synced_at,
  };
}

function lastSaleCell(price, date) {
  if (price === null || price === undefined) return "—";
  return fmtMoney(price) + (date ? ` (${esc(date)})` : "");
}

/* Like lastSaleCell, but for parcel_values rows which carry the sale date
 * as separate year/month columns rather than a pre-formatted string. */
function saleCell(price, year, month) {
  if (price === null || price === undefined) return "—";
  const date = fmtSaleDate(year, month);
  return fmtMoney(price) + (date !== "—" ? ` (${date})` : "");
}

/* ---------------------------------------------------------------------
 * Acreage provenance (cited on hover)
 * ------------------------------------------------------------------- */
let sourcesCache = null;   // from /api/sources
async function loadSources() {
  try { sourcesCache = await fetchJSON("/api/sources"); } catch (e) { sourcesCache = null; }
}

function acreageCitation(row) {
  if (row.acreage === null || row.acreage === undefined) return null;
  const src = row.acreage_source || "county";
  const cty = pretty(row.county);
  const ds = pretty(row.dataset_type);
  const cfg = sourcesCache && sourcesCache.counties && sourcesCache.counties[row.county]
    ? sourcesCache.counties[row.county][row.dataset_type] : null;
  if (src === "county") {
    const field = cfg && cfg.acreage_field ? `field "${cfg.acreage_field}"` : "acreage field";
    const url = cfg && cfg.url ? cfg.url : "county GIS service";
    return { label: `${cty} ${ds} layer`, text: `Source: ${cty} County ${ds} GIS layer, ${field}. ${url}` };
  }
  if (src === "dor") {
    const url = sourcesCache && sourcesCache.dor_values ? sourcesCache.dor_values.url : "";
    return { label: "FL Dept. of Revenue tax roll",
      text: `Source: Florida Dept. of Revenue tax roll (Florida Statewide Cadastral layer, LND_SQFOOT field) divided by 43,560 sq ft per acre. ${url}` };
  }
  if (src === "geometry") {
    const url = cfg && cfg.url ? cfg.url : "county GIS service";
    return { label: "computed from boundary geometry",
      text: `Source: computed from the polygon boundary published by ${cty} County (${ds} layer, no acreage field). Spherical area of the WGS84 geometry; typically within 0.2% of the county's own projected area. ${url}` };
  }
  return { label: src, text: `Source: ${src}` };
}

function acreageCell(row) {
  const c = acreageCitation(row);
  if (!c) return fmtNum(row.acreage);
  return `<span class="cited" title="${esc(c.text)}">${fmtNum(row.acreage)}</span>`;
}

function buildRowCells(row) {
  return [
    pretty(row.county),
    pretty(row.dataset_type),
    esc(row.feature_key),
    esc(row.city || "—"),
    acreageCell(row),
    esc([row.zoning_code, row.zoning_desc].filter(Boolean).join(" - ") || "—"),
    esc([row.land_use_code, row.land_use_desc].filter(Boolean).join(" - ") || "—"),
    fmtMoney(row.land_value),
    fmtMoney(row.building_value),
    fmtMoney(row.total_value),
    fmtMoney(row.just_value),
    lastSaleCell(row.sale_price, row.sale_date),
    fmtTime(row.last_synced_at),
  ];
}

function renderFeaturesTable(data, isPoll) {
  const tbody = document.getElementById("features-tbody");
  const incomingIds = new Set(data.rows.map((r) => r.id));
  const existingIds = new Set(featureRowIndex.keys());
  const sameIdSet = incomingIds.size === existingIds.size &&
    [...incomingIds].every((id) => existingIds.has(id));

  if (!isPoll || !sameIdSet || tbody.children.length === 0) {
    // Full (re)render - either a real navigation/filter change, or the
    // row set itself changed (rows added/removed by a concurrent sync).
    tbody.innerHTML = "";
    featureRowIndex.clear();
    for (const row of data.rows) {
      const tr = document.createElement("tr");
      tr.dataset.id = row.id;
      tr.innerHTML = buildRowCells(row).map((c) => `<td>${c}</td>`).join("");
      tr.addEventListener("click", () => showDetail(row.id));
      tbody.appendChild(tr);
      featureRowIndex.set(row.id, { el: tr, values: rowValues(row) });
    }
  } else {
    // Same rows, same order (id-sorted) - patch only changed cells so the
    // update doesn't visually jar the user while they're browsing.
    for (const row of data.rows) {
      const entry = featureRowIndex.get(row.id);
      if (!entry) continue;
      const newValues = rowValues(row);
      const cells = entry.el.children;
      const newCells = buildRowCells(row);
      let changed = false;
      for (const key of Object.keys(newValues)) {
        if (entry.values[key] !== newValues[key]) { changed = true; break; }
      }
      if (changed) {
        newCells.forEach((text, i) => setTextIfChanged(cells[i], text));
        entry.values = newValues;
      }
    }
  }

  const hint = document.getElementById("results-hint");
  hint.textContent = `${data.total.toLocaleString()} matching record${data.total === 1 ? "" : "s"}`;

  const pageInfo = document.getElementById("page-info");
  pageInfo.textContent = `Page ${data.page} of ${data.total_pages}`;
  document.getElementById("page-prev").disabled = data.page <= 1;
  document.getElementById("page-next").disabled = data.page >= data.total_pages;

  lastFeaturesTotal = data.total;
}

async function loadFeatures(isPoll) {
  const params = currentFilterParams({ page: state.page, per_page: state.per_page });
  try {
    const data = await fetchJSON(`/api/features?${qs(params)}`);
    renderFeaturesTable(data, isPoll);
    setConn(true);
  } catch (e) {
    setConn(false);
    if (!isPoll) console.error(e);
  }
}


/* ---------------------------------------------------------------------
 * Mini map inside the detail popup
 * ------------------------------------------------------------------- */
let detailMap = null;
let detailLayer = null;

function showDetailMap(geometryJson, note) {
  const el = document.getElementById("detail-map");
  const noteEl = document.getElementById("detail-map-note");
  let geom = null;
  try { geom = geometryJson ? JSON.parse(geometryJson) : null; } catch (e) { geom = null; }
  if (!geom) {
    el.hidden = true;
    noteEl.hidden = !note;
    noteEl.textContent = note || "";
    if (detailLayer) { detailLayer.remove(); detailLayer = null; }
    return;
  }
  noteEl.hidden = true;
  el.hidden = false;
  if (!detailMap) {
    detailMap = L.map(el, { zoomControl: true, attributionControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors | County lines: U.S. Census',
    }).addTo(detailMap);
    watchMapResize(el, () => detailMap);
    loadCountyBoundaries().then(() => {
      if (countiesGeo && !detailCountyLayer) {
        detailCountyLayer = makeCountyLayer(detailMap, { selectedKey: null, labels: false });
      }
    });
  }
  if (detailLayer) detailLayer.remove();
  detailLayer = L.geoJSON({ type: "Feature", geometry: geom, properties: {} }, {
    style: { color: "#dc2626", weight: 2, fillOpacity: 0.25 },
  }).addTo(detailMap);
  // The modal was hidden when the map was created, so Leaflet has no size yet.
  setTimeout(() => {
    detailMap.invalidateSize();
    try { detailMap.fitBounds(detailLayer.getBounds(), { padding: [16, 16], maxZoom: 18 }); } catch (e) { /* empty geometry */ }
  }, 50);
}

/* ---------------------------------------------------------------------
 * Feature detail modal
 * ------------------------------------------------------------------- */
async function showDetail(id) {
  const body = document.getElementById("detail-body");
  body.textContent = "Loading...";
  showDetailMap(null, null);
  openDetailModal("Feature detail");
  try {
    const data = await fetchJSON(`/api/feature/${id}`);
    showDetailMap(data.geometry_geojson, "No boundary geometry stored for this feature.");
    let attrs = {};
    try { attrs = JSON.parse(data.attributes_json) || {}; } catch (e) { /* leave empty */ }
    const summary = [
      ["County", pretty(data.county)],
      ["Dataset", pretty(data.dataset_type)],
      ["Feature key", esc(data.feature_key)],
      ["City", esc(data.city || "—")],
      ["Acreage", (() => {
        const c = acreageCitation(data);
        return c ? `${acreageCell(data)} <span class="source-note">${esc(c.label)}</span>` : fmtNum(data.acreage);
      })()],
      ["Zoning", esc([data.zoning_code, data.zoning_desc].filter(Boolean).join(" - ") || "—")],
      ["Land use", esc([data.land_use_code, data.land_use_desc].filter(Boolean).join(" - ") || "—")],
      ["Land value", fmtMoney(data.land_value)],
      ["Building value", fmtMoney(data.building_value)],
      ["Total value", fmtMoney(data.total_value)],
      ["Just value", fmtMoney(data.just_value)],
      ["Last sale", lastSaleCell(data.sale_price, data.sale_date)],
      ["Last synced", fmtTime(data.last_synced_at)],
    ];
    const summaryHtml = summary.map(([k, v]) =>
      `<tr>${fieldTh(k)}<td>${v}</td></tr>`).join("");
    const attrRows = Object.entries(attrs)
      .filter(([k]) => !/^(shape[._]|st_)/i.test(k));
    const attrHtml = attrRows.map(([k, v]) =>
      `<tr>${attrTh(k)}<td>${esc(v)}</td></tr>`).join("");
    body.innerHTML =
      `<table class="detail-table">${summaryHtml}</table>` +
      `<h4>Source attributes</h4>` +
      (attrRows.length
        ? `<table class="detail-table">${attrHtml}</table>`
        : `<p class="hint">${window.DEMO_MODE && data.dataset_type === "parcels"
            ? "Raw source attributes are omitted for parcels in the public demo to fit the free host; the full build keeps them."
            : "No additional attributes."}</p>`);
  } catch (e) {
    body.textContent = "Failed to load detail: " + e.message;
  }
}

/* ---------------------------------------------------------------------
 * Status panel: one row per county, one cell per dataset
 *
 * Fed by /api/status/counties, which pivots sync_log into a cell per
 * (county, dataset) carrying the last successful time, the live row count and
 * the current state (ok / failed / running / manual / not loaded).
 * ------------------------------------------------------------------- */
const STATUS_COLUMNS = ["dor_values", "parcels", "zoning", "land_use", "future_land_use"];

/* "2 h ago" for the cell, exact timestamp on hover. */
function fmtAgo(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return String(iso);
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 45) return "just now";
  if (secs < 90) return "1 min ago";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(secs / 3600);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(secs / 86400)} d ago`;
}

function absTime(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : `${iso}\n(${d.toLocaleString()} local)`;
}

function agoSpan(iso, prefix) {
  return `<span class="ds-time" title="${esc(absTime(iso))}">${prefix || ""}${fmtAgo(iso)}</span>`;
}

function countText(n, noun) {
  const word = noun || "row";
  return `${Number(n).toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

/* "98% valued" - the share of a county's parcel features that carry a DOR
 * value. Computed on the server by a background scan, so it can be null on
 * the first poll after the page loads. */
function joinRateText(cell) {
  if (!cell.row_count) return "";
  if (cell.join_rate === null || cell.join_rate === undefined) {
    return ` <span class="ds-pending" title="Counting joined parcels...">· join rate…</span>`;
  }
  const pct = cell.join_rate >= 0.995 && cell.join_rate < 1
    ? "99" : Math.round(cell.join_rate * 100);
  const cls = cell.join_rate >= 0.9 ? "" : " ds-warn";
  return ` · <span class="${cls.trim()}" title="${esc(`${Number(cell.joined_count).toLocaleString()} of ${Number(cell.row_count).toLocaleString()} parcel features carry a DOR value`)}">${pct}% valued</span>`;
}

/* The identity of a cell for change detection: relative times tick over on
 * their own, and re-flashing a cell for that alone would be noise. */
function statusCellKey(cell) {
  if (!cell) return "";
  return [cell.state, cell.last_success_at, cell.last_attempt_at, cell.row_count,
    cell.join_rate === null || cell.join_rate === undefined ? "" : cell.join_rate.toFixed(4)].join("|");
}

function statusCellHtml(cell, dataset) {
  if (!cell) return '<span class="ds-none">—</span>';
  const sub = (html) => `<span class="ds-sub">${html}</span>`;
  // The DOR values column counts tax-roll parcels for the county, not rows of
  // a county layer, so it gets its own noun.
  const noun = dataset === "dor_values" ? "parcel" : "row";
  const rows = cell.row_count ? countText(cell.row_count, noun) : `no ${noun}s`;
  const rate = dataset === "parcels" ? joinRateText(cell) : "";

  if (cell.state === "manual") {
    return `<span class="ds-muted" title="${esc(cell.note || "No public REST layer; sources.json marks this dataset as manual.")}">manual</span>` +
      (cell.row_count ? sub(rows) : "");
  }
  if (cell.state === "not_configured") {
    return `<span class="ds-none" title="No source configured for this county/dataset in sources.json.">—</span>`;
  }
  if (cell.state === "not_loaded") {
    return `<span class="ds-muted" title="${esc(`Configured in sources.json (${cell.source_type || "rest"}) but never synced.`)}">not loaded</span>`;
  }
  if (cell.state === "running") {
    return `<span class="status-tag status-running" title="${esc(`Started ${absTime(cell.last_attempt_at)}`)}">syncing…</span>` +
      sub(`started ${fmtAgo(cell.last_attempt_at)}`);
  }
  if (cell.state === "failed") {
    return `<span class="status-tag status-failed" title="${esc(cell.error || "Sync failed.")}">failed</span>` +
      sub((cell.last_success_at ? `last ok ${fmtAgo(cell.last_success_at)}` : "never succeeded") +
        (cell.row_count ? ` · ${rows}` : ""));
  }
  // ok
  return agoSpan(cell.last_success_at) + sub(rows + rate);
}

/* Sort state for the county table. Dataset columns sort by last successful
 * time (most recent first), counties with nothing loaded last. */
const statusSort = { key: "county", dir: "asc" };

function statusSortValue(row, key) {
  if (key === "county") return pretty(row.county).toLowerCase();
  const cell = row.datasets[key];
  const iso = cell && (cell.last_success_at || cell.last_attempt_at);
  const t = iso ? Date.parse(iso) : NaN;
  return isNaN(t) ? null : t;
}

function sortStatusRows(rows) {
  const dir = statusSort.dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = statusSortValue(a, statusSort.key);
    const vb = statusSortValue(b, statusSort.key);
    if (va === null && vb === null) return pretty(a.county).localeCompare(pretty(b.county));
    if (va === null) return 1;   // never-loaded rows sink, in both directions
    if (vb === null) return -1;
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return pretty(a.county).localeCompare(pretty(b.county));
  });
}

function renderStatusSummary(summary) {
  const el = document.getElementById("status-summary");
  if (!el.children.length) {
    el.innerHTML = `
      <div class="stat"><span class="num" id="sum-rows"></span><span class="label">Total features</span></div>
      <div class="stat"><span class="num" id="sum-counties"></span><span class="label">Counties with parcels</span></div>
      <div class="stat"><span class="num" id="sum-values"></span><span class="label">Valued parcels</span></div>
      <div class="stat"><span class="num" id="sum-fail"></span><span class="label">Failing sources</span></div>
      <div class="stat"><span class="num" id="sum-last"></span><span class="label">Last successful sync</span></div>
    `;
  }
  setTextIfChanged(document.getElementById("sum-rows"), summary.total_features.toLocaleString());
  setTextIfChanged(document.getElementById("sum-counties"),
    `${summary.counties_with_parcels} / ${summary.county_count}`);
  setTextIfChanged(document.getElementById("sum-values"), summary.valued_parcels.toLocaleString());
  setTextIfChanged(document.getElementById("sum-fail"), String(summary.failing_count));
  const last = document.getElementById("sum-last");
  last.title = absTime(summary.last_success_at);
  setTextIfChanged(last, fmtAgo(summary.last_success_at));

  document.getElementById("conn-indicator").className =
    summary.failing_count > 0 ? "pill pill-bad" : "pill pill-ok";
}

function buildStatusRowCells(row) {
  const badge = row.priority
    ? ' <span class="metro-badge" title="Priority metro county (etl.PRIORITY_COUNTIES)">metro</span>'
    : "";
  return [
    { html: `<span class="county-name">${esc(pretty(row.county))}</span>${badge}`, key: row.county },
  ].concat(STATUS_COLUMNS.map((d) => ({
    html: statusCellHtml(row.datasets[d], d),
    key: statusCellKey(row.datasets[d]),
  })));
}

function renderStatusCounties(counties) {
  const tbody = document.getElementById("status-tbody");
  const rows = sortStatusRows(counties);
  const seen = new Set();

  rows.forEach((row, idx) => {
    const cells = buildStatusRowCells(row);
    let entry = statusRowIndex.get(row.county);
    if (!entry) {
      const tr = document.createElement("tr");
      tr.dataset.county = row.county;
      tr.innerHTML = cells.map((c) => `<td>${c.html}</td>`).join("");
      entry = { el: tr, cells };
      statusRowIndex.set(row.county, entry);
    } else {
      cells.forEach((c, i) => {
        const td = entry.el.children[i];
        if (entry.cells[i].html !== c.html) td.innerHTML = c.html;
        if (entry.cells[i].key !== c.key) flashCell(td);   // real change, not a ticking clock
      });
      entry.cells = cells;
    }
    if (tbody.children[idx] !== entry.el) {
      tbody.insertBefore(entry.el, tbody.children[idx] || null);
    }
    seen.add(row.county);
  });

  for (const [county, entry] of statusRowIndex) {
    if (!seen.has(county)) { entry.el.remove(); statusRowIndex.delete(county); }
  }
}

function markStatusSortHeader() {
  document.querySelectorAll("#status-counties-table th.sortable").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sortKey === statusSort.key);
    th.classList.toggle("sorted-asc", th.dataset.sortKey === statusSort.key && statusSort.dir === "asc");
    th.classList.toggle("sorted-desc", th.dataset.sortKey === statusSort.key && statusSort.dir === "desc");
  });
}

let lastStatusCounties = [];

async function loadStatus() {
  try {
    const data = await fetchJSON("/api/status/counties");
    lastStatusCounties = data.counties;
    renderStatusSummary(data.summary);
    renderStatusCounties(data.counties);
    markStatusSortHeader();
    setConn(true);
  } catch (e) {
    setConn(false);
  }
}

function initStatusEvents() {
  document.querySelectorAll("#status-counties-table th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (statusSort.key === key) {
        statusSort.dir = statusSort.dir === "asc" ? "desc" : "asc";
      } else {
        statusSort.key = key;
        // Names read best A-Z; times read best newest-first.
        statusSort.dir = key === "county" ? "asc" : "desc";
      }
      renderStatusCounties(lastStatusCounties);
      markStatusSortHeader();
    });
  });
}

function setConn(ok) {
  const el = document.getElementById("conn-indicator");
  if (!ok) {
    el.textContent = "offline";
    el.className = "pill pill-bad";
  } else if (el.textContent === "offline") {
    el.textContent = "live";
    el.className = "pill pill-ok";
  }
}

/* ---------------------------------------------------------------------
 * Map view
 * ------------------------------------------------------------------- */
function colorFor(code) {
  const key = code || "(none)";
  if (colorCache.has(key)) return colorCache.get(key);
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  const hue = hash % 360;
  const color = `hsl(${hue}, 65%, 45%)`;
  colorCache.set(key, color);
  return color;
}


/* ---------------------------------------------------------------------
 * County outlines (U.S. Census TIGERweb boundaries, static/fl_counties.geojson)
 * ------------------------------------------------------------------- */
let countiesGeo = null;
let countyLayer = null;        // on the main map
let detailCountyLayer = null;  // on the mini map

async function loadCountyBoundaries() {
  if (countiesGeo) return countiesGeo;
  try { countiesGeo = await fetchJSON("/static/fl_counties.geojson"); } catch (e) { countiesGeo = null; }
  return countiesGeo;
}

function countyStyle(feature, selectedKey) {
  const selected = selectedKey && feature.properties.key === selectedKey;
  return selected
    ? { color: "#1d4ed8", weight: 3, opacity: 1, dashArray: null, fillColor: "#3b82f6", fillOpacity: 0.06 }
    : { color: "#334155", weight: 1.5, opacity: 0.9, dashArray: "6 4", fillOpacity: 0 };
}

function ensureCountyPane(map) {
  // Outlines live below the feature layers (overlayPane is z 400) so a click
  // on a lot always reaches the lot, never the county polygon under it.
  if (!map.getPane("counties")) {
    const pane = map.createPane("counties");
    pane.style.zIndex = 350;
    pane.style.pointerEvents = "none";
  }
}

function makeCountyLayer(map, opts) {
  const { selectedKey, labels } = opts;
  ensureCountyPane(map);
  const layer = L.geoJSON(countiesGeo, {
    pane: "counties",
    style: (f) => countyStyle(f, selectedKey),
    interactive: false,
    onEachFeature: (feature, lyr) => {
      if (labels) {
        // Short permanent label (name only) so labels don't collide at state zoom.
        lyr.bindTooltip(feature.properties.name, { permanent: true, direction: "center", className: "county-label", pane: "counties" });
      }
    },
  }).addTo(map);
  return layer;
}

/* Point-in-polygon (ray casting) against the generalized county outlines. */
function ringContains(ring, lng, lat) {
  let inside = false;
  for (let i = 0, k = ring.length - 1; i < ring.length; k = i++) {
    const [xi, yi] = ring[i], [xk, yk] = ring[k];
    if ((yi > lat) !== (yk > lat) && lng < ((xk - xi) * (lat - yi)) / (yk - yi) + xi) inside = !inside;
  }
  return inside;
}

function countyAt(latlng) {
  if (!countiesGeo) return null;
  const { lng, lat } = latlng;
  for (const f of countiesGeo.features) {
    const g = f.geometry;
    const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
    for (const poly of polys) {
      if (ringContains(poly[0], lng, lat) && !poly.slice(1).some((hole) => ringContains(hole, lng, lat))) return f;
    }
  }
  return null;
}

function onMapMouseMove(e) {
  // Hover name for the county under the cursor (replaces per-polygon tooltips).
  // Naming only - clicking empty ground never changes the county filter.
  const f = countyAt(e.latlng);
  const el = leafletMap.getContainer();
  let tip = el.querySelector(".county-hover");
  if (!tip) { tip = document.createElement("div"); tip.className = "county-hover"; el.appendChild(tip); }
  if (f) {
    tip.textContent = f.properties.name + " County";
    tip.hidden = false;
  } else {
    tip.hidden = true;
  }
}

function refreshCountyOutlines(selectedKey) {
  if (!leafletMap || !countiesGeo) return;
  if (countyLayer) leafletMap.removeLayer(countyLayer);
  countyLayer = makeCountyLayer(leafletMap, { selectedKey, labels: true });
  updateCountyLabelVisibility();
}

function updateCountyLabelVisibility() {
  if (!leafletMap) return;
  const el = leafletMap.getContainer();
  el.classList.toggle("show-county-labels", leafletMap.getZoom() <= 9);
}

function countyBounds(key) {
  if (!countiesGeo) return null;
  const f = countiesGeo.features.find((x) => x.properties.key === key);
  return f ? L.geoJSON(f).getBounds() : null;
}

/* Keep Leaflet in sync when the user drags a map's resize handle. */
function watchMapResize(el, getMap) {
  if (!("ResizeObserver" in window)) return;
  let pending = null;
  new ResizeObserver(() => {
    const m = getMap();
    if (!m) return;
    clearTimeout(pending);
    pending = setTimeout(() => m.invalidateSize(), 60);
  }).observe(el);
}

function ensureMap() {
  if (leafletMap) return leafletMap;
  leafletMap = L.map("map").setView([27.8, -81.5], 7); // roughly centered on FL
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors | County lines: U.S. Census TIGERweb",
  }).addTo(leafletMap);
  leafletMap.on("zoomend", updateCountyLabelVisibility);
  leafletMap.on("mousemove", onMapMouseMove);
  // Any pan/zoom the user starts themselves cancels the one automatic fit for
  // the current load, so a streaming render never yanks the view back.
  leafletMap.on("movestart zoomstart", () => { if (!mapProgrammaticMove) mapUserMoved = true; });
  leafletMap.on("mouseout", () => { const t = leafletMap.getContainer().querySelector(".county-hover"); if (t) t.hidden = true; });
  watchMapResize(document.getElementById("map"), () => leafletMap);
  loadCountyBoundaries().then(() => refreshCountyOutlines(state.county || null));
  return leafletMap;
}

function clearMap() {
  mapRun++;
  if (geoLayer && leafletMap) leafletMap.removeLayer(geoLayer);
  geoLayer = null;
  if (leafletMap) { setMapProgress(false); }
  refreshCountyOutlines(null);
  mapLoadedKey = null;
  document.getElementById("map-legend").innerHTML = "";
  document.getElementById("map-hint").textContent =
    'Select a county and dataset above and click "Apply filters" to render matching polygons (capped for performance).';
}

/* Map rendering is uncapped: every matching feature is drawn. To keep the
 * browser usable on big result sets we (a) draw on a shared canvas renderer,
 * (b) stream rows from the server in keyset-paged chunks, and (c) render in
 * short slices with a pause after each - a duty cycle that caps the render
 * at roughly half of one CPU core. When the cap engages we tell the user. */
const MAP_CHUNK = 2000;      // rows per request
const RENDER_WORK_MS = 40;   // render budget per slice
const RENDER_REST_MS = 40;   // pause after each slice (=> ~50% duty cycle)
let mapRun = 0;              // generation token; bumping it cancels a run
let mapRenderer = null;
let mapNoticeTimer = null;
let mapMeterTimer = null;
let mapUserMoved = false;      // did the user pan/zoom since this load started?
let mapProgrammaticMove = 0;   // >0 while we move the map ourselves

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* Move the map without it counting as a user pan/zoom. */
function programmaticMapMove(fn) {
  mapProgrammaticMove++;
  try { fn(); } finally { setTimeout(() => { mapProgrammaticMove = Math.max(0, mapProgrammaticMove - 1); }, 0); }
}

/* Both map chrome elements are small cards pinned inside the map container:
 * they never cover the map, and they swallow their own mouse/scroll events so
 * clicking or scrolling on them does not pan or zoom the map underneath. */
function mapOverlayEls() {
  const container = leafletMap.getContainer();
  let meter = container.querySelector(".map-meter");
  if (!meter) {
    meter = document.createElement("div");
    meter.className = "map-meter";
    meter.hidden = true;
    meter.innerHTML =
      '<div class="map-meter-head">' +
      '<span class="map-meter-text">Loading features...</span>' +
      '<button type="button" class="map-meter-cancel map-cancel" aria-label="Cancel loading" title="Cancel loading">&times;</button>' +
      "</div>" +
      '<div class="map-meter-bar"><div class="map-meter-fill"></div></div>';
    meter.querySelector(".map-cancel").addEventListener("click", cancelMapRender);
    L.DomEvent.disableClickPropagation(meter);
    L.DomEvent.disableScrollPropagation(meter);
    container.appendChild(meter);
  }
  let notice = container.querySelector(".map-notice");
  if (!notice) {
    notice = document.createElement("div");
    notice.className = "map-notice";
    notice.hidden = true;
    L.DomEvent.disableClickPropagation(notice);
    L.DomEvent.disableScrollPropagation(notice);
    container.appendChild(notice);
  }
  return { meter, notice };
}

function cancelMapRender() {
  mapRun++;                    // generation token: the running loop bails out
  setMapProgress(false);
  showMapNotice("Render cancelled. Narrow the filters or apply again to restart.", 6000);
  document.getElementById("map-hint").textContent = "Render cancelled.";
}

/* Compact top-right progress meter. `total` null => indeterminate bar (we do
 * not know the feature count until the first chunk comes back). */
function setMapProgress(on, shown, total) {
  if (!leafletMap) return;
  const { meter } = mapOverlayEls();
  clearTimeout(mapMeterTimer);
  meter.classList.remove("map-meter-done");
  if (!on) { meter.hidden = true; return; }
  meter.hidden = false;
  const bar = meter.querySelector(".map-meter-bar");
  const fill = meter.querySelector(".map-meter-fill");
  const n = shown || 0;
  if (total == null) {
    meter.querySelector(".map-meter-text").textContent = `Loading ${n.toLocaleString()} features`;
    bar.classList.add("indeterminate");
    fill.style.width = "";
  } else {
    meter.querySelector(".map-meter-text").textContent =
      `Loading ${n.toLocaleString()} / ${total.toLocaleString()} features`;
    bar.classList.remove("indeterminate");
    fill.style.width = `${Math.round(Math.min(1, total ? n / total : 0) * 100)}%`;
  }
}

/* Load finished: show the final count for a beat, then fade the meter out. */
function finishMapProgress(shown) {
  if (!leafletMap) return;
  const { meter } = mapOverlayEls();
  if (meter.hidden) return;
  meter.querySelector(".map-meter-text").textContent = `Rendered ${(shown || 0).toLocaleString()} features`;
  meter.querySelector(".map-meter-bar").classList.remove("indeterminate");
  meter.querySelector(".map-meter-fill").style.width = "100%";
  meter.classList.add("map-meter-done");
  clearTimeout(mapMeterTimer);
  mapMeterTimer = setTimeout(() => {
    meter.hidden = true;
    meter.classList.remove("map-meter-done");
  }, 1500);
}

function showMapNotice(html, autoHideMs) {
  if (!leafletMap) return;
  const { notice } = mapOverlayEls();
  notice.innerHTML = html + '<button type="button" class="map-notice-close" aria-label="Dismiss">&times;</button>';
  notice.querySelector(".map-notice-close").addEventListener("click", () => { notice.hidden = true; });
  notice.hidden = false;
  clearTimeout(mapNoticeTimer);
  if (autoHideMs) mapNoticeTimer = setTimeout(() => { notice.hidden = true; }, autoHideMs);
}

function hideMapNotice() {
  if (!leafletMap) return;
  mapOverlayEls().notice.hidden = true;
}

function renderLegend(legendCodes) {
  const legendEl = document.getElementById("map-legend");
  legendEl.innerHTML = [...legendCodes.entries()]
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])))
    .slice(0, 60)
    .map(([code, color]) => `<span><span class="swatch" style="background:${color}"></span>${code}</span>`)
    .join("") + (legendCodes.size > 60 ? `<span class="hint">+${legendCodes.size - 60} more codes</span>` : "");
}

async function loadMap() {
  const hint = document.getElementById("map-hint");
  const run = ++mapRun;
  if (!state.county) {
    ensureMap();
    await loadCountyBoundaries();
    clearMap();
    setMapProgress(false);
    hideMapNotice();
    hint.textContent = "Pick a county and dataset above, then click \"Apply filters\" - features render per county to keep it fast.";
    return;
  }
  hint.textContent = "Loading...";
  const map = ensureMap();
  await loadCountyBoundaries();
  if (run !== mapRun) return;
  refreshCountyOutlines(state.county);
  const params = currentFilterParams();
  mapLoadedKey = JSON.stringify(params);

  if (geoLayer) { map.removeLayer(geoLayer); geoLayer = null; }
  if (!mapRenderer) mapRenderer = L.canvas({ padding: 0.5 });
  const legendCodes = new Map();
  const styleField = state.dataset_type === "land_use" || state.dataset_type === "future_land_use"
    ? "land_use_code" : "zoning_code";

  geoLayer = L.geoJSON(null, {
    renderer: mapRenderer,
    style: (feature) => {
      const code = feature.properties[styleField] || "(none)";
      legendCodes.set(code, colorFor(code));
      return { color: colorFor(code), weight: 1, fillOpacity: 0.45 };
    },
    onEachFeature: (feature, layer) => {
      const p = feature.properties;
      layer.on("click", (e) => {
        L.DomEvent.stop(e);
        showDetail(p.id);
      });
      layer.on("mouseover", () => layer.setStyle({ weight: 2.5 }));
      layer.on("mouseout", () => layer.setStyle({ weight: 1 }));
    },
  }).addTo(map);
  if (countyLayer) countyLayer.bringToBack();

  // Frame the county at most once per load - immediately, so features stream
  // in on top of the right view. Later calls are no-ops, and once the user has
  // panned or zoomed we never move the map again for this load.
  mapUserMoved = false;
  let fittedOnce = false;
  const fitToData = () => {
    if (fittedOnce || mapUserMoved) return;
    const b = countyBounds(state.county);
    if (!b) return;
    fittedOnce = true;
    programmaticMapMove(() => map.fitBounds(b, { padding: [10, 10] }));
  };
  fitToData();

  hideMapNotice();
  setMapProgress(true, 0, null);
  const t0 = performance.now();
  let after = 0, shown = 0, total = null, throttled = false, chunks = 0;
  try {
    for (;;) {
      const data = await fetchJSON(`/api/features/geometry?${qs(Object.assign({}, params, { after_id: after, limit: MAP_CHUNK }))}`);
      if (run !== mapRun) return;
      if (data.total != null) total = data.total;
      const rows = data.rows;
      if (!rows.length) break;
      chunks++;

      let i = 0;
      while (i < rows.length) {
        const sliceStart = performance.now();
        while (i < rows.length && performance.now() - sliceStart < RENDER_WORK_MS) {
          const r = rows[i++];
          if (!r.geometry_geojson) continue;
          let geom;
          try { geom = JSON.parse(r.geometry_geojson); } catch (e) { continue; }
          r.geometry_geojson = null; // drop the string once parsed
          geoLayer.addData({ type: "Feature", geometry: geom, properties: r });
          shown++;
        }
        setMapProgress(true, shown, total);
        if (i < rows.length || data.has_more) {
          // Budget exhausted before the chunk finished: the CPU cap is engaged.
          if (!throttled) {
            throttled = true;
            showMapNotice(`<b>Large render</b> - ${total != null ? total.toLocaleString() : "many"} features. ` +
              "Drawing is throttled to about half of one CPU core so the page stays responsive; this may take a while. " +
              "You can keep panning and zooming, or cancel the render (x) and narrow the filters.", 12000);
          }
          await sleep(RENDER_REST_MS);
          if (run !== mapRun) return;
        }
      }
      if (chunks % 3 === 0) renderLegend(legendCodes);
      after = data.next_after;
      if (!data.has_more) break;
    }
    if (run !== mapRun) return;
    fitToData();
    const secs = (performance.now() - t0) / 1000;
    hint.textContent = `Rendered on map (${shown.toLocaleString()} features` +
      (total != null && total !== shown ? ` of ${total.toLocaleString()} matching; the rest have no boundary geometry` : "") +
      `, ${secs.toFixed(1)}s).`;
    renderLegend(legendCodes);
    finishMapProgress(shown);
    if (throttled) {
      showMapNotice(`Done: ${shown.toLocaleString()} features rendered in ${secs.toFixed(0)}s (throttled).`, 8000);
    }
  } catch (e) {
    if (run !== mapRun) return;
    mapLoadedKey = null;
    hint.textContent = "Failed to load map data: " + e.message;
    showMapNotice("Map load failed: " + esc(e.message), 10000);
    setMapProgress(false);
  }
}

/* ---------------------------------------------------------------------
 * Parcel Values tab
 * ------------------------------------------------------------------- */
function valuesFilterParams(extra) {
  return Object.assign({
    county: vstate.county,
    use_code: vstate.use_code,
    min_jv: vstate.min_jv,
    max_jv: vstate.max_jv,
    min_sale: vstate.min_sale,
    max_sale: vstate.max_sale,
    q: vstate.q,
    sort: vstate.sort,
    dir: vstate.dir,
  }, extra || {});
}

async function loadValuesCounties() {
  valuesCountiesCache = await fetchJSON("/api/values/counties");
  const sel = document.getElementById("v-county");
  const prev = sel.value;
  sel.innerHTML = '<option value="">All</option>' +
    valuesCountiesCache
      .map((c) => `<option value="${c.county}">${pretty(c.county)} (${c.row_count.toLocaleString()})</option>`)
      .join("");
  sel.value = valuesCountiesCache.some((c) => c.county === prev) ? prev : "";
}

async function loadValuesUseCodes() {
  const sel = document.getElementById("v-usecode");
  const county = document.getElementById("v-county").value;
  const params = qs({ county });
  const data = await fetchJSON(`/api/values/use_codes?${params}`);
  const prev = sel.value;
  sel.innerHTML = '<option value="">All</option>' +
    data
      .map((u) => `<option value="${esc(u.dor_use_code)}">${esc(u.dor_use_code)} (${u.row_count.toLocaleString()})</option>`)
      .join("");
  sel.value = data.some((u) => u.dor_use_code === prev) ? prev : "";
}

function valueRowKey(row) {
  return `${row.county}|${row.parcel_id}`;
}

function valueRowValues(row) {
  return {
    county: row.county,
    parcel_id: row.parcel_id,
    site_address: row.site_address,
    site_city: row.site_city,
    dor_use_code: row.dor_use_code,
    just_value: row.just_value,
    assessed_value: row.assessed_value,
    taxable_value: row.taxable_value,
    land_value: row.land_value,
    land_sqft: row.land_sqft,
    sale_price: row.sale_price,
    sale_year: row.sale_year,
    sale_month: row.sale_month,
    sale_qual: row.sale_qual,
    year_built: row.year_built,
  };
}

function buildValueRowCells(row) {
  return [
    pretty(row.county),
    esc(row.parcel_id),
    esc(row.site_address || "—"),
    esc(row.site_city || "—"),
    esc(row.dor_use_code),
    fmtMoney(row.just_value),
    fmtMoney(row.assessed_value),
    fmtMoney(row.taxable_value),
    fmtMoney(row.land_value),
    fmtNum(row.land_sqft),
    saleCell(row.sale_price, row.sale_year, row.sale_month),
    esc(row.sale_qual),
    row.year_built ? String(row.year_built) : "—",
  ];
}

function renderValuesTable(data, isPoll) {
  const tbody = document.getElementById("values-tbody");
  const table = document.getElementById("values-table");
  const emptyEl = document.getElementById("values-empty");

  if (data.rows.length === 0) {
    tbody.innerHTML = "";
    valuesRowIndex.clear();
    table.hidden = true;
    emptyEl.hidden = false;
  } else {
    table.hidden = false;
    emptyEl.hidden = true;

    const incomingKeys = new Set(data.rows.map(valueRowKey));
    const existingKeys = new Set(valuesRowIndex.keys());
    const sameKeySet = incomingKeys.size === existingKeys.size &&
      [...incomingKeys].every((k) => existingKeys.has(k));

    if (!isPoll || !sameKeySet || tbody.children.length === 0) {
      tbody.innerHTML = "";
      valuesRowIndex.clear();
      for (const row of data.rows) {
        const tr = document.createElement("tr");
        const key = valueRowKey(row);
        tr.innerHTML = buildValueRowCells(row).map((c) => `<td>${c}</td>`).join("");
        tr.addEventListener("click", () => showValueDetail(row.county, row.parcel_id));
        tbody.appendChild(tr);
        valuesRowIndex.set(key, { el: tr, values: valueRowValues(row) });
      }
    } else {
      for (const row of data.rows) {
        const key = valueRowKey(row);
        const entry = valuesRowIndex.get(key);
        if (!entry) continue;
        const newValues = valueRowValues(row);
        const cells = entry.el.children;
        const newCells = buildValueRowCells(row);
        let changed = false;
        for (const k of Object.keys(newValues)) {
          if (entry.values[k] !== newValues[k]) { changed = true; break; }
        }
        if (changed) {
          newCells.forEach((text, i) => setTextIfChanged(cells[i], text));
          entry.values = newValues;
        }
      }
    }
  }

  const hint = document.getElementById("values-hint");
  hint.textContent = `${data.total.toLocaleString()} matching parcel${data.total === 1 ? "" : "s"}`;

  const pageInfo = document.getElementById("v-page-info");
  pageInfo.textContent = `Page ${data.page} of ${data.total_pages}`;
  document.getElementById("v-page-prev").disabled = data.page <= 1;
  document.getElementById("v-page-next").disabled = data.page >= data.total_pages;
}

async function loadValuesTable(isPoll) {
  const params = valuesFilterParams({ page: vstate.page, per_page: vstate.per_page });
  try {
    const data = await fetchJSON(`/api/values?${qs(params)}`);
    renderValuesTable(data, isPoll);
    setConn(true);
  } catch (e) {
    setConn(false);
    if (!isPoll) console.error(e);
  }
}

async function showValueDetail(county, parcelId) {
  const body = document.getElementById("detail-body");
  body.textContent = "Loading...";
  showDetailMap(null, null);
  openDetailModal("Parcel value detail");
  try {
    const data = await fetchJSON(`/api/value/${encodeURIComponent(county)}/${encodeURIComponent(parcelId)}`);
    showDetailMap(data.geometry_geojson,
      `Boundary map not available yet: ${pretty(data.county)} County's parcel layer has not been loaded (see Pipeline Status).`);
    const rows = [
      ["County", pretty(data.county)],
      ["Parcel ID", esc(data.parcel_id)],
      ["Assessment year", data.assessment_year ? String(data.assessment_year) : "—"],
      ["Use code", esc(data.dor_use_code)],
      ["Site address", esc(data.site_address || "—")],
      ["City", esc([data.site_city, data.site_zip].filter(Boolean).join(" ") || "—")],
      ["Just value", fmtMoney(data.just_value)],
      ["Assessed value", fmtMoney(data.assessed_value)],
      ["Taxable value", fmtMoney(data.taxable_value)],
      ["Land value", fmtMoney(data.land_value)],
      ["Land sq ft", fmtNum(data.land_sqft)],
      ["Building count", data.building_count === null ? "—" : String(data.building_count)],
      ["Year built", data.year_built ? String(data.year_built) : "—"],
      ["Living area (sq ft)", fmtNum(data.living_area)],
      ["Last sale", saleCell(data.sale_price, data.sale_year, data.sale_month)],
      ["Last sale qualified", esc(data.sale_qual)],
      ["Prior sale", saleCell(data.sale2_price, data.sale2_year, data.sale2_month)],
      ["Prior sale qualified", esc(data.sale2_qual)],
      ["Last synced", fmtTime(data.last_synced_at)],
    ];
    const rowsHtml = rows.map(([k, v]) => `<tr>${fieldTh(k)}<td>${v}</td></tr>`).join("");
    body.innerHTML = `<table class="detail-table">${rowsHtml}</table>`;
  } catch (e) {
    body.textContent = "Failed to load detail: " + e.message;
  }
}

function readValuesFiltersFromForm() {
  vstate.county = document.getElementById("v-county").value;
  vstate.use_code = document.getElementById("v-usecode").value;
  vstate.min_jv = document.getElementById("v-min-jv").value;
  vstate.max_jv = document.getElementById("v-max-jv").value;
  vstate.min_sale = document.getElementById("v-min-sale").value;
  vstate.max_sale = document.getElementById("v-max-sale").value;
  vstate.q = document.getElementById("v-q").value;
  vstate.sort = document.getElementById("v-sort").value;
  vstate.dir = document.getElementById("v-dir").value;
  vstate.page = 1;
}

function initValuesEvents() {
  document.getElementById("v-county").addEventListener("change", async () => {
    await loadValuesUseCodes();
  });

  document.getElementById("values-filters").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    readValuesFiltersFromForm();
    await loadValuesTable(false);
  });

  document.getElementById("v-page-prev").addEventListener("click", async () => {
    if (vstate.page > 1) { vstate.page -= 1; await loadValuesTable(false); }
  });
  document.getElementById("v-page-next").addEventListener("click", async () => {
    vstate.page += 1; await loadValuesTable(false);
  });
  document.getElementById("v-per-page").addEventListener("change", async (ev) => {
    vstate.per_page = parseInt(ev.target.value, 10);
    vstate.page = 1;
    await loadValuesTable(false);
  });
}

async function ensureValuesLoaded() {
  if (valuesLoaded) return;
  valuesLoaded = true;
  await loadValuesCounties();
  await loadValuesUseCodes();
  await loadValuesTable(false);
}

/* ---------------------------------------------------------------------
 * Wiring
 * ------------------------------------------------------------------- */
function readFiltersFromForm() {
  state.county = document.getElementById("f-county").value;
  state.dataset_type = document.getElementById("f-dataset").value;
  state.zoning_code = document.getElementById("f-zoning").value;
  state.land_use_code = document.getElementById("f-landuse").value;
  state.min_acreage = document.getElementById("f-min-acre").value;
  state.max_acreage = document.getElementById("f-max-acre").value;
  state.q = document.getElementById("f-q").value;
  state.page = 1;
}

/* Switch the Browse panel between the table and map views. Used by the
 * Table/Map toggle and by init() to open on the default view. */
function setView(view) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.view === view);
  });
  state.view = view;
  document.getElementById("view-table").hidden = state.view !== "table";
  document.getElementById("view-map").hidden = state.view !== "map";
  if (state.view === "map") {
    if (leafletMap) setTimeout(() => leafletMap.invalidateSize(), 50);
    // Render with the current filters unless the map already shows them.
    if (mapLoadedKey !== JSON.stringify(currentFilterParams())) loadMap();
  }
}

function initEvents() {
  document.getElementById("f-county").addEventListener("change", () => {
    refreshDatasetOptions();
  });

  document.getElementById("filters").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    readFiltersFromForm();
    await loadFacets();
    await loadFeatures(false);
    if (state.view === "map") await loadMap();
  });

  document.getElementById("f-reset").addEventListener("click", async () => {
    document.getElementById("filters").reset();
    refreshDatasetOptions();
    readFiltersFromForm();
    await loadFacets();
    await loadFeatures(false);
    clearMap();
  });

  document.getElementById("page-prev").addEventListener("click", async () => {
    if (state.page > 1) { state.page -= 1; await loadFeatures(false); }
  });
  document.getElementById("page-next").addEventListener("click", async () => {
    state.page += 1; await loadFeatures(false);
  });
  document.getElementById("per-page").addEventListener("change", async (ev) => {
    state.per_page = parseInt(ev.target.value, 10);
    state.page = 1;
    await loadFeatures(false);
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  });

  document.querySelectorAll(".main-tab").forEach((tab) => {
    tab.addEventListener("click", () => setMainTab(tab.dataset.mainTab));
  });

  initValuesEvents();
  initStatusEvents();

  document.querySelectorAll(".fullscreen-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleFullscreen(btn.dataset.fullscreenTarget));
  });
  document.addEventListener("fullscreenchange", onFullscreenChange);

  document.getElementById("detail-close").addEventListener("click", closeDetailModal);
  document.getElementById("detail-modal").addEventListener("click", (ev) => {
    if (ev.target.id === "detail-modal") closeDetailModal();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !document.getElementById("detail-modal").hidden) {
      closeDetailModal();
    }
  });
}

async function init() {
  initEvents();
  // The status page has no dependency on the browse bootstrap below, and that
  // bootstrap can take a while when a sync is hammering the database, so start
  // its fetch now rather than after it.
  const statusReady = loadStatus();
  await loadSources();
  await loadCombos();
  await loadFacets();
  await loadFeatures(false);
  // Open on the default view (map) now that the filter state is ready, using
  // the same path a click on the toggle takes so Leaflet sizes itself.
  setView(state.view);
  await statusReady;

  // Live updates: poll status every 20s, and silently refresh the current
  // table page every 25s so new/changed rows and updated sync timestamps
  // show up without a manual reload. Polling never disturbs an open
  // filter edit or the map view.
  setInterval(loadStatus, 20000);
  setInterval(() => {
    if (state.mainTab === "browse" && state.view === "table" &&
        document.getElementById("detail-modal").hidden) {
      loadFeatures(true);
    }
  }, 25000);
  setInterval(() => {
    if (state.mainTab === "values" && valuesLoaded &&
        document.getElementById("detail-modal").hidden) {
      loadValuesTable(true);
    }
  }, 20000);
}

/* ---------------------------------------------------------------------
 * Main tabs + fullscreen
 * ------------------------------------------------------------------- */
function setMainTab(name) {
  state.mainTab = name;
  document.querySelectorAll(".main-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.mainTab === name);
  });
  document.querySelectorAll("[data-main-panel]").forEach((p) => {
    p.hidden = p.dataset.mainPanel !== name;
  });
  if (name === "browse" && state.view === "map" && leafletMap) {
    setTimeout(() => leafletMap.invalidateSize(), 50);
  }
  if (name === "values") {
    ensureValuesLoaded();
  }
}

function toggleFullscreen(targetId) {
  const el = document.getElementById(targetId);
  if (document.fullscreenElement === el) {
    document.exitFullscreen();
  } else if (document.fullscreenElement) {
    document.exitFullscreen().then(() => el.requestFullscreen());
  } else {
    el.requestFullscreen();
  }
}

function onFullscreenChange() {
  const active = document.fullscreenElement;
  document.querySelectorAll(".fullscreen-btn").forEach((btn) => {
    const isThis = active && active.id === btn.dataset.fullscreenTarget;
    btn.innerHTML = isThis ? "&#x2716; Exit fullscreen" : "&#x26F6; Fullscreen";
  });
  // The map container's box changes size going in/out of fullscreen, and
  // Leaflet only redraws its tiles once told the size changed.
  if (leafletMap) setTimeout(() => leafletMap.invalidateSize(), 100);
}

document.addEventListener("DOMContentLoaded", init);
