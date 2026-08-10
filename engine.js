// ── Scalable engine (viewport-cull + degree-LOD) driving the original UI shell ──
const {Deck, OrthographicView, ScatterplotLayer, LineLayer, LinearInterpolator} = deck;
// Global error handler to surface silent failures
window.onerror = (msg, src, line, col, err) => console.error('GLOBAL ERROR:', msg, 'at', src, line, col, err);
window.onunhandledrejection = (e) => console.error('UNHANDLED REJECTION:', e.reason);

const CAT=[[168,120,138],[88,143,168],[211,88,73],[232,184,64],[116,140,105],[197,140,100],[195,174,214]];
const CATNAME=['Biography','Science','History','Art','Philosophy','Geography','Other'];
const clamp=(x,a,b)=>Math.min(b,Math.max(a,x));
const G=384, BUDGET=150000, V='e1'; // BUDGET is the preallocated buffer ceiling; nodeBudget (below) is the live-adjustable draw cap, never exceeding it
const $=id=>document.getElementById(id);

// The 5 layout binaries (viewer_full.bin, edgeTgt.bin, adjacency_csr[.rev].bin,
// titles.bin) are gitignored -- too large for the repo -- so the GitHub Pages
// deployment has no local copy. Try the local relative path first (local dev,
// serving from the directory where these files actually live), then fall back
// to the HF-hosted copy (same repo the SQLite DB is already served from).
const HF_ASSET_BASE = 'https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main';

// ── Loading-screen progress bar ─────────────────────────────────────────────
// Tracks real bytes received for whichever assets are currently blocking first
// paint (loadCoreAssets, below) -- a byte counter on the response stream plus
// Content-Length, not a fake animated fill. Keyed by asset name so multiple
// parallel fetches can each report in without clobbering each other; the
// displayed percentage is bytes-loaded / bytes-total summed across every key
// currently in the map.
const loadProgress = {};
// Clears only the given keys (or everything, if none given) -- the CSR fetches
// now run concurrently with loadCoreAssets(), reporting under their own keys,
// so a blanket clear here would wipe their in-flight progress too.
function resetLoadProgress(keys) {
  if (!keys) { for (const k in loadProgress) delete loadProgress[k]; return; }
  for (const k of keys) delete loadProgress[k];
}
function reportProgress(name, loaded, total) {
  loadProgress[name] = { loaded, total };
  let l = 0, t = 0;
  for (const k in loadProgress) { l += loadProgress[k].loaded; t += loadProgress[k].total; }
  const pct = t > 0 ? Math.min(100, Math.round((l / t) * 100)) : 0;
  const fill = document.getElementById('loading-bar-fill');
  const pctEl = document.getElementById('loading-pct');
  if (fill) fill.style.width = pct + '%';
  if (pctEl) pctEl.textContent = pct + '%';
}
// Wraps a response body stream with a passthrough that reports bytes as they
// arrive, without buffering or otherwise altering the stream itself.
function countingStream(readable, name, total) {
  let loaded = 0;
  return readable.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      loaded += chunk.byteLength;
      reportProgress(name, loaded, total);
      controller.enqueue(chunk);
    }
  }));
}

async function fetchAsset(name, optional, progressTag) {
  for (const url of [`${name}?v=${V}`, `${HF_ASSET_BASE}/${name}`]) {
    try {
      const r = await fetch(url);
      if (r.ok) {
        if (progressTag && r.body) {
          const total = Number(r.headers.get('content-length')) || 0;
          return await new Response(countingStream(r.body, progressTag, total)).arrayBuffer();
        }
        return r.arrayBuffer();
      }
    } catch (e) { /* try next source */ }
  }
  if (optional) return null;
  throw new Error(`Failed to fetch required asset ${name} from any source`);
}

// ── Compact v2 assets ───────────────────────────────────────────────────────
// viewer_v2.bin.gz / edgeTgt_v2.bin.gz / titles_v2.bin.gz carry the same data
// as the v1 float32 originals, just quantized (coordinates -> uint16 on a
// shared grid, degree -> a uint16 index into a ~4.4k-entry palette, title
// length -> uint8) and gzipped -- see build_web_assets.py for the exact math
// and why the precision loss is invisible (0.044-unit coordinate error, 0.06px
// at a 4K full-graph view). Serving these instead of the originals is a 3x+
// reduction in the ~330MB that used to block first paint. They're committed
// same-origin (not on HF) specifically so the browser gets a stable URL and a
// real Cache-Control/ETag instead of HF's no-store + freshly-signed CDN link
// on every visit -- that's what made every repeat load re-download ~1GB.
//
// Each inflate function rebuilds the exact v1 byte layout (u32 N header +
// float32 array) so nothing downstream of loadCoreAssets() needs to know or
// care which path a given load came from.
async function fetchGzipSameOrigin(name) {
  try {
    if (typeof DecompressionStream === 'undefined') return null; // old browser: caller falls back to v1
    const r = await fetch(`${name}?v=${V}`);
    if (!r.ok || !r.body) return null;
    // Progress is tracked on the compressed bytes actually crossing the network
    // (Content-Length here is the .gz size), before decompression -- that's the
    // number that reflects real download progress.
    const total = Number(r.headers.get('content-length')) || 0;
    const counted = countingStream(r.body, name, total);
    const stream = counted.pipeThrough(new DecompressionStream('gzip'));
    return await new Response(stream).arrayBuffer();
  } catch (e) { return null; }
}

// Same idea, but for the two adjacency CSR files, which stay on HF -- at
// ~250MB gzipped each they're too large for the same-origin git-hosted
// approach the other three assets use (see build_csr_gz in
// build_web_assets.py: plain gzip, ~1.4x, no quantization possible on
// already-minimal uint32 node indices).
//
// These used to load in the background after the loading screen dismissed,
// but that meant every interactive feature (search, node click, routing)
// either silently fell back to slow per-node DB queries or -- worse, on a
// cold load -- competed with the CSR download for the same bandwidth and
// didn't resolve at all for over a minute. Simplest fix: block launch on
// these too, same as the other three assets. Slower first paint, but
// everything actually works the moment the site says it's ready.
async function fetchGzipHF(name, progressTag) {
  try {
    if (typeof DecompressionStream === 'undefined') return null;
    const r = await fetch(`${HF_ASSET_BASE}/${name}.gz`);
    if (!r.ok || !r.body) return null;
    const body = progressTag
      ? countingStream(r.body, progressTag, Number(r.headers.get('content-length')) || 0)
      : r.body;
    const stream = body.pipeThrough(new DecompressionStream('gzip'));
    return await new Response(stream).arrayBuffer();
  } catch (e) { return null; }
}

// One CSR file: gzip'd from HF, falling back to the original uncompressed file
// (fetchAsset's existing local-then-HF behavior) if the gzip fetch fails or
// DecompressionStream is unavailable.
async function fetchCsr(name, progressTag) {
  const gz = await fetchGzipHF(name, progressTag);
  if (gz) return gz;
  return fetchAsset(name, true, progressTag);
}

function inflateViewerV2(buf) {
  if (new TextDecoder().decode(new Uint8Array(buf, 0, 4)) !== 'WGV2') throw new Error('bad viewer_v2 magic');
  const head = new DataView(buf, 4, 20);
  const n = head.getUint32(0, true), paletteLen = head.getUint32(4, true);
  const lo = head.getFloat32(8, true), scale = head.getFloat32(12, true);
  let off = 24;
  const palette = new Float32Array(buf, off, paletteLen); off += paletteLen * 4;
  const xq = new Uint16Array(buf, off, n); off += n * 2;
  const yq = new Uint16Array(buf, off, n); off += n * 2;
  const degIdx = new Uint16Array(buf, off, n); off += n * 2;
  const catq = new Uint8Array(buf, off, n);

  const out = new ArrayBuffer(4 + n * 4 * 4);
  new Uint32Array(out, 0, 1)[0] = n;
  const raw = new Float32Array(out, 4, n * 4);
  for (let i = 0; i < n; i++) {
    raw[i * 4] = xq[i] / scale + lo;
    raw[i * 4 + 1] = yq[i] / scale + lo;
    raw[i * 4 + 2] = palette[degIdx[i]];
    raw[i * 4 + 3] = catq[i];
  }
  return { buf: out, lo, scale };
}

function inflateEdgeV2(buf, lo, scale) {
  if (new TextDecoder().decode(new Uint8Array(buf, 0, 4)) !== 'WGE2') throw new Error('bad edgeTgt_v2 magic');
  const n = new DataView(buf, 4, 4).getUint32(0, true);
  let off = 8;
  const maskBytes = Math.ceil(n / 8);
  const mask = new Uint8Array(buf, off, maskBytes);
  // build_edge() pads the mask to an even byte count so txq/tyq land uint16-
  // aligned -- see its comment. Skip past that pad byte the same way.
  off += maskBytes + (maskBytes % 2);
  const txq = new Uint16Array(buf, off, n); off += n * 2;
  const tyq = new Uint16Array(buf, off, n);

  const out = new ArrayBuffer(4 + n * 2 * 4);
  new Uint32Array(out, 0, 1)[0] = n;
  const et = new Float32Array(out, 4, n * 2);
  for (let i = 0; i < n; i++) {
    // np.packbits defaults to MSB-first (element 0 -> bit 0x80, not 0x01) --
    // this has to read bits back in the same order it wrote them in.
    if ((mask[i >> 3] >> (7 - (i & 7))) & 1) { et[i * 2] = txq[i] / scale + lo; et[i * 2 + 1] = tyq[i] / scale + lo; }
    else { et[i * 2] = NaN; et[i * 2 + 1] = NaN; }
  }
  return out;
}

function inflateTitlesV2(buf) {
  if (new TextDecoder().decode(new Uint8Array(buf, 0, 4)) !== 'WGT2') throw new Error('bad titles_v2 magic');
  const n = new DataView(buf, 4, 4).getUint32(0, true);
  const lengths = new Uint8Array(buf, 8, n);
  const text = new Uint8Array(buf, 8 + n);

  const out = new ArrayBuffer(4 + (n + 1) * 4 + text.length);
  new Uint32Array(out, 0, 1)[0] = n;
  const offsets = new Uint32Array(out, 4, n + 1);
  let acc = 0;
  for (let i = 0; i < n; i++) { offsets[i] = acc; acc += lengths[i]; }
  offsets[n] = acc;
  new Uint8Array(out, 4 + (n + 1) * 4).set(text);
  return out;
}

// Returns {nbuf, ebuf, tbuf} in the same v1 byte layout startVisualization
// already expects, trying the compact same-origin assets first and falling
// back to the original uncompressed HF-hosted files (fetchAsset's existing
// behavior) if any of the three are missing, corrupt, or the browser lacks
// DecompressionStream. All-or-nothing on purpose: a mixed v2/v1 load isn't
// worth the added complexity when the fallback already works end to end.
async function loadCoreAssets() {
  const [v2n, v2e, v2t] = await Promise.all([
    fetchGzipSameOrigin('viewer_v2.bin.gz'),
    fetchGzipSameOrigin('edgeTgt_v2.bin.gz'),
    fetchGzipSameOrigin('titles_v2.bin.gz')
  ]);
  if (v2n && v2e && v2t) {
    try {
      const { buf: nbuf, lo, scale } = inflateViewerV2(v2n);
      const ebuf = inflateEdgeV2(v2e, lo, scale);
      const tbuf = inflateTitlesV2(v2t);
      console.log('Loaded compact v2 assets (quantized + gzip, same-origin)');
      return { nbuf, ebuf, tbuf };
    } catch (e) {
      console.warn('v2 asset inflate failed, falling back to original assets:', e);
    }
  }
  // Only this function's own keys -- CSR fetches run concurrently with this
  // one (see startVisualization) and report progress under their own names.
  resetLoadProgress(['viewer_v2.bin.gz', 'edgeTgt_v2.bin.gz', 'titles_v2.bin.gz']);
  const [nbuf, ebuf, tbuf] = await Promise.all([
    fetchAsset('viewer_full.bin', false, 'viewer_full.bin'),
    fetchAsset('edgeTgt.bin', false, 'edgeTgt.bin'),
    fetchAsset('titles.bin', false, 'titles.bin')
  ]);
  return { nbuf, ebuf, tbuf };
}

let db = null;
let currentCullQueryId = 0;
let currentRoutePath = null;
let routeRequestGen = 0; // bumped per route search so stale per-hop context fetches don't overwrite a newer route's list
let currentRouteIndices = new Set();
let selectedNodeIdx = -1;
let selectedConnections = new Set(); // Stores indices of neighbors for active node highlighting
let hoverCache = new Map(); // Cache nodeIndex -> title
let detailsCache = new Map(); // Cache nodeIndex -> compiled sidebar details
let activePrecachedIndices = new Set(); // Speculatively cached neighbor indices to evict on next click
let clickHistory = []; // Stack of visited nodeIndexes for back navigation
let lastHoveredIdx = -1;
let hoverTimeout = null;
let searchTimeout = null;
let cullTimeout = null;
let px = null, py = null;
let nodeDegrees = null; // Float32Array of degrees, resident from viewer_full.bin
let csrOffsets = null, csrNeighbors = null, csrN = 0; // OUT-edges (adjacency_csr.bin), see fetchNeighbours
let csrOffsetsRev = null, csrNeighborsRev = null; // IN-edges (adjacency_csr_rev.bin), same N as csrN

// Live-adjustable render settings — the actual controls-panel knobs, wired directly
// to what cull() reads every frame. (The previous panel's sliders — link
// density/distance/strength, charge, collision, gravity — were force-simulation
// parameters from a pre-rewrite version of this app; this engine has no physics
// simulation at all, it's a static precomputed layout, so none of them did anything.)
let nodeBudget = 90000;       // how many nodes cull() draws per frame, ≤ BUDGET
let nodeSizeScale = 1.0;      // multiplies radiusMinPixels/radiusMaxPixels
let edgeOpacity = 1.0;        // multiplies the background hairline edge layer's alpha
let dimAlpha = 22;            // alpha used for off-route / unconnected "dimmed" nodes
let hideAllNodes = false;     // if true, cull() draws zero nodes (edges unaffected)
let hiddenCategories = new Set(); // category ids toggled off via the legend's eye icon
let titleOffsets = null, titleBytes = null, titleDecoder = null; // in-memory title index (titles.bin)
function titleOf(idx) {
  if (!titleOffsets || idx < 0 || idx >= titleOffsets.length - 1) return null;
  return titleDecoder.decode(titleBytes.subarray(titleOffsets[idx], titleOffsets[idx + 1]));
}

// Uniformly random article title, for the search box's and route finder's dice
// buttons. Uniform means most picks are low-degree stubs (that's most of the
// 6.9M articles) rather than recognizable pages -- an explicit choice over
// biasing toward high-degree nodes, since a "random" button that only ever
// lands on well-known articles isn't actually random.
function randomTitle() {
  if (!titleOffsets) return null;
  const n = titleOffsets.length - 1;
  return titleOf(Math.floor(Math.random() * n));
}

// ms -> "42ms" under a second, "3.2s" at or above it -- used by the route-finder
// timing toast so both a near-instant CSR-backed search and a multi-second
// exhaustive one read naturally.
function formatDuration(ms) {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function findTitleIndexInTitlesBin(title) {
  if (!titleOffsets) return -1;
  let lo = 0, hi = (titleOffsets.length - 1);
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const midTitle = titleOf(mid) || "";
    if (midTitle < title) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (titleOf(lo) === title) return lo;

  // Case-insensitive fallback using the prebuilt autocomplete search index
  const localIdx = findTitleIndexLocal(title);
  if (localIdx !== undefined) return localIdx;

  return -1;
}

// Alphabetical search index over titles.bin, built once so autocomplete is a binary
// search (no network round trip) instead of a ~1s DB range query per keystroke. Built
// off the main thread's critical path — see buildTitleSearchIndex() — so it doesn't
// delay the initial page becoming interactive; search-box wiring falls back to the old
// DB query path until it's ready.
let titleSearchOrder = null;   // Uint32Array: node index, sorted by lowercase title
let titleSearchLower = null;   // string[]: lowercase title at the same sorted position

function buildTitleSearchIndex() {
  if (!titleOffsets || titleSearchOrder) return;
  const n = titleOffsets.length - 1;
  const order = new Array(n);
  const lower = new Array(n);
  for (let i = 0; i < n; i++) {
    order[i] = i;
    lower[i] = (titleOf(i) || '').toLowerCase();
  }
  order.sort((a, b) => { const la = lower[a], lb = lower[b]; return la < lb ? -1 : la > lb ? 1 : 0; });
  titleSearchLower = new Array(n);
  for (let i = 0; i < n; i++) titleSearchLower[i] = lower[order[i]];
  titleSearchOrder = Uint32Array.from(order);
  console.log(`Title search index built: ${n.toLocaleString()} entries`);
}

// Case-insensitive prefix search against the in-memory index. Returns node indices,
// or null if the index isn't built yet (caller should fall back to a DB query).
function searchTitlesLocal(prefix, limit = 10) {
  if (!titleSearchOrder) return null;
  const p = prefix.toLowerCase();
  let lo = 0, hi = titleSearchLower.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (titleSearchLower[mid] < p) lo = mid + 1; else hi = mid;
  }
  const out = [];
  for (let i = lo; i < titleSearchLower.length && out.length < limit; i++) {
    if (!titleSearchLower[i].startsWith(p)) break;
    out.push(titleSearchOrder[i]);
  }
  return out;
}

// Exact-title -> node index, via the same sorted array (binary search on equality
// instead of prefix). Returns undefined if the index isn't built or there's no match.
function findTitleIndexLocal(title) {
  if (!titleSearchOrder) return undefined;
  const p = title.toLowerCase();
  let lo = 0, hi = titleSearchLower.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (titleSearchLower[mid] < p) lo = mid + 1; else hi = mid;
  }
  // Lowercase match can collide across differently-cased titles; scan the equal-run
  // for one whose real (cased) title matches exactly.
  for (let i = lo; i < titleSearchLower.length && titleSearchLower[i] === p; i++) {
    const idx = titleSearchOrder[i];
    if (titleOf(idx) === title) return idx;
  }
  return undefined;
}
let routeAnimationInterval = null;
let lastVisibleCount = 0; // nodes drawn by the most recent cull() — surfaced to the test harness

// Visualization state for search algorithms
let searchVisitedNodes = new Set(); // node indices visited in order
let searchFrontierEdges = []; // [srcIdx, tgtIdx] pairs in traversal order
let searchActive = false;
let searchStartIdx = -1;
let searchEndIdx = -1;


// Full ordered hop list for the displayed route. animateRoute() reveals hops into
// currentRouteIndices one at a time, so this is the record of what the *complete*
// route is — needed to restore the whole path if the flight is interrupted partway.
let currentRouteOrdered = [];

// Stop the camera flight without discarding the route. Interrupting the animation
// (by clicking a node, say) should leave the route on screen and fully drawn, not
// frozen at whichever hop it had reached.
function stopRouteAnimation() {
  if (routeAnimationInterval) {
    clearInterval(routeAnimationInterval);
    routeAnimationInterval = null;
    if (currentRouteOrdered.length) currentRouteIndices = new Set(currentRouteOrdered);
  }
  const caption = $('route-caption');
  if (caption) { caption.style.opacity = '0'; caption.style.transform = 'translateX(-50%) translateY(-8px)'; }
}

// Discard the route entirely. Only for explicit dismissal (closing the sidebar) or
// starting a new search — NOT for selecting a node, which leaves the route standing.
function clearRoute() {
  stopRouteAnimation();
  currentRoutePath = null;
  currentRouteOrdered = [];
  currentRouteIndices.clear();
  const routeResult = $('route-result-container');
  if (routeResult) routeResult.style.display = 'none';
}

// Query Priority Scheduler definitions
const PRIORITY_CULL = 0;
const PRIORITY_PRECACHE = 0;
const PRIORITY_HOVER = 1;
const PRIORITY_SEARCH = 2;
const PRIORITY_CLICK = 3;

let queryQueue = [];
let workerExecuting = false;
let originalQuery = null;

// Query instrumentation — read/reset via window.__wg.stats() / window.__wg.resetStats()
const qStats = { count: 0, evicted: 0, failed: 0, totalMs: 0, maxMs: 0, byPriority: {}, log: [] };
const QLOG_MAX = 500;

class QueryEvicted extends Error {
  constructor() { super('query evicted by higher-priority request'); this.name = 'QueryEvicted'; }
}

// Eviction rules. The intent is "a newer user action supersedes stale background work",
// but evicting *every* pending query of equal-or-lower priority means two same-priority
// consumers (e.g. two in-flight pathfinder steps, or the two parallel autocomplete range
// queries) permanently cancel each other and neither ever runs — a livelock that presents
// as "search hangs forever". So:
//   - strictly lower priority is evicted (urgent work jumps the background queue)
//   - a query with the same `tag` is evicted (a new hover supersedes the previous hover)
//   - equal-priority, differently-tagged work is left alone and runs FIFO
function scheduleQuery(sql, params, priority = 0, tag = null) {
  return new Promise((resolve, reject) => {
    // Evicted callers MUST be settled, otherwise their promise dangles forever and the
    // awaiting code path hangs with no error at all.
    const kept = [];
    for (const q of queryQueue) {
      const supersededByTag = tag !== null && q.tag === tag;
      if (q.priority < priority || supersededByTag) {
        qStats.evicted++;
        q.reject(new QueryEvicted());
      } else {
        kept.push(q);
      }
    }
    queryQueue = kept;

    queryQueue.push({ sql, params, priority, tag, resolve, reject });
    processQueue();
  });
}

async function processQueue() {
  if (workerExecuting || queryQueue.length === 0) return;
  workerExecuting = true;
  const next = queryQueue.shift();
  const t0 = performance.now();
  try {
    const result = await originalQuery(next.sql, next.params);
    next.resolve(result);
  } catch (err) {
    qStats.failed++;
    next.reject(err);
  } finally {
    const ms = performance.now() - t0;
    qStats.count++;
    qStats.totalMs += ms;
    if (ms > qStats.maxMs) qStats.maxMs = ms;
    qStats.byPriority[next.priority] = (qStats.byPriority[next.priority] || 0) + 1;
    if (qStats.log.length < QLOG_MAX) {
      qStats.log.push({ ms: Math.round(ms), priority: next.priority, sql: next.sql.replace(/\s+/g, ' ').trim().slice(0, 120) });
    }
    workerExecuting = false;
    processQueue();
  }
}

function dbQuery(sql, params, priority = 0, tag = null) {
  if (!originalQuery) return Promise.resolve(null);
  return scheduleQuery(sql, params, priority, tag);
}

function cleanWikiText(text) {
  if (!text) return "";
  return text
    .replace(/'''''/g, '')
    .replace(/'''/g, '')
    .replace(/''/g, '')
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2')
    .replace(/\[\[([^\]]+)\]\]/g, '$1');
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function cleanAndHighlightWikiText(text, targetTitle, targetColor) {
  if (!text) return "";
  let cleaned = escapeHtml(text);
  cleaned = cleaned
    .replace(/'''''/g, '')
    .replace(/'''/g, '')
    .replace(/''/g, '');
  let highlighted = false;
  if (targetTitle && targetColor) {
    const escapedTarget = escapeHtml(targetTitle).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const rePipe = new RegExp(`\\[\\[${escapedTarget}\\|([^\\]]+)\\]\\]`, 'gi');
    if (rePipe.test(cleaned)) {
      cleaned = cleaned.replace(rePipe, `<span style="color: ${targetColor}; font-style: normal; font-weight: 600; opacity: 1;">$1</span>`);
      highlighted = true;
    }
    const reSimple = new RegExp(`\\[\\[(${escapedTarget})\\]\\]`, 'gi');
    if (reSimple.test(cleaned)) {
      cleaned = cleaned.replace(reSimple, `<span style="color: ${targetColor}; font-style: normal; font-weight: 600; opacity: 1;">$1</span>`);
      highlighted = true;
    }
  }
  // Clean all other links
  cleaned = cleaned
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2')
    .replace(/\[\[([^\]]+)\]\]/g, '$1');

  // Fallback if not highlighted in wikitext link
  if (!highlighted && targetTitle && targetColor) {
    const escapedTarget = escapeHtml(targetTitle).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const rePlain = new RegExp(`(${escapedTarget})`, 'gi');
    cleaned = cleaned.replace(rePlain, `<span style="color: ${targetColor}; font-style: normal; font-weight: 600; opacity: 1;">$1</span>`);
  }
  return cleaned;
}

async function fetchLiveWikiSnippet(title) {
  try {
    const url = `https://en.wikipedia.org/w/api.php?action=query&format=json&prop=extracts&exintro=1&explaintext=1&exsentences=3&titles=${encodeURIComponent(title)}&origin=*`;
    const response = await fetch(url);
    if (response.ok) {
      const data = await response.json();
      const pages = data?.query?.pages;
      if (pages) {
        const pageId = Object.keys(pages)[0];
        if (pageId !== "-1") {
          return pages[pageId].extract || null;
        }
      }
    }
  } catch (err) {
    console.warn("Failed to fetch live Wikipedia snippet for:", title, err);
  }
  return null;
}

// Init legend. Each row toggles that category's visibility on click -- the eye icon
// swaps open/closed and cull() (see its hiddenCategories check) skips those nodes
// (and their edges) on the next redraw. window.__wg isn't set up yet when this runs
// at load, but the click handler only calls it later, once a user actually clicks.
const EYE_OPEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;display:block;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
const EYE_CLOSED = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;display:block;"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a20.3 20.3 0 0 1 5.06-5.94M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a20.3 20.3 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;
(function(){ const el=$('legend-list'); if(!el) return;
  el.innerHTML = CAT.map((c,i) =>
    `<div class="legend-item" data-cat="${i}">
       <span class="legend-left">
         <span class="legend-color" style="background:rgb(${c.join(',')})"></span>
         ${CATNAME[i]}
       </span>
       <span class="legend-eye">${EYE_OPEN}</span>
     </div>`).join('');
  el.querySelectorAll('.legend-item').forEach(item => {
    item.onclick = () => {
      const catId = Number(item.dataset.cat);
      const eye = item.querySelector('.legend-eye');
      if (hiddenCategories.has(catId)) {
        hiddenCategories.delete(catId);
        item.style.opacity = '';
        eye.innerHTML = EYE_OPEN;
      } else {
        hiddenCategories.add(catId);
        item.style.opacity = '0.45';
        eye.innerHTML = EYE_CLOSED;
      }
      if (window.__wg) window.__wg.cull();
    };
  });
})();

// Setup panels collapsible toggles
[['header-toggle','header-panel'],['legend-toggle','legend-panel'],['controls-toggle','controls-panel'],['stats-toggle','stats-panel']]
  .forEach(([b,p])=>{ const bt=$(b),pn=$(p); if(bt&&pn) bt.onclick=()=>{ pn.classList.toggle('collapsed'); bt.classList.toggle('active'); }; });

// Close detail sidebar
// cull() is a closure local to run(), not in scope here — go through window.__wg
// (set at the end of run()) so this actually redraws instead of silently no-op'ing.
const sbc=$('sidebar-close'); if(sbc) sbc.onclick=()=>{ $('detail-sidebar').classList.remove('active'); selectedNodeIdx = -1; selectedConnections.clear(); clearRoute(); window.__wg && window.__wg.cull(); };

// Sidebar Back button execution
const sbb=$('sidebar-back');
if(sbb) {
  sbb.onclick=()=> {
    if (clickHistory.length > 0) {
      const prevIdx = clickHistory.pop();
      window.selectNode(prevIdx, true);
    }
  };
}

// Wire the controls panel to the live-adjustable render settings declared above.
// Each one updates its module-level variable and redraws through window.__wg.cull()
// (see the sidebar-close comment above for why it can't call cull() directly).
(function wireControlsPanel() {
  const redraw = () => window.__wg && window.__wg.cull();

  const hideToggle = $('hide-nodes-toggle');
  if (hideToggle) hideToggle.onchange = () => { hideAllNodes = hideToggle.checked; redraw(); };

  const sizeSlider = $('slider-node-size'), sizeVal = $('node-size-val');
  if (sizeSlider) sizeSlider.oninput = () => { nodeSizeScale = parseFloat(sizeSlider.value); if (sizeVal) sizeVal.textContent = nodeSizeScale.toFixed(1); redraw(); };

  const budgetSlider = $('slider-node-budget'), budgetVal = $('node-budget-val');
  if (budgetSlider) budgetSlider.oninput = () => { nodeBudget = Math.min(parseInt(budgetSlider.value, 10), BUDGET); if (budgetVal) budgetVal.textContent = nodeBudget.toLocaleString(); redraw(); };

  const edgeSlider = $('slider-edge-opacity'), edgeVal = $('edge-opacity-val');
  if (edgeSlider) edgeSlider.oninput = () => { edgeOpacity = parseFloat(edgeSlider.value); if (edgeVal) edgeVal.textContent = edgeOpacity.toFixed(1); redraw(); };

  const dimSlider = $('slider-dim-alpha'), dimVal = $('dim-alpha-val');
  if (dimSlider) dimSlider.oninput = () => { dimAlpha = parseInt(dimSlider.value, 10); if (dimVal) dimVal.textContent = dimAlpha; redraw(); };
})();

// Load Coordinate Binaries and Connect SQLite VFS
async function startVisualization() {
  if ($('loading-text')) $('loading-text').textContent = "Downloading node coordinates...";
  // Everything blocks launch: viewer/edge/titles (via loadCoreAssets, which
  // tries the compact quantized+gzipped same-origin versions first and falls
  // back to the originals) AND both adjacency CSRs. The CSRs used to load in
  // the background after the site said it was ready, but that meant every
  // interactive feature either silently fell back to slow per-node DB queries,
  // or -- worse, on a cold load -- competed with the CSR download for the same
  // bandwidth and didn't work at all for over a minute. Slower first paint,
  // but nothing is shown as ready until it actually is. Kicked off together,
  // not loadCoreAssets().then(...), so the CSR download overlaps with
  // viewer/edge/titles instead of queuing behind them.
  const [{ nbuf, ebuf, tbuf }, cbuf, cbufRev] = await Promise.all([
    loadCoreAssets(),
    fetchCsr('adjacency_csr.bin', 'adjacency_csr.bin'),
    fetchCsr('adjacency_csr_rev.bin', 'adjacency_csr_rev.bin')
  ]);

  const N=new Uint32Array(nbuf,0,1)[0]; const raw=new Float32Array(nbuf,4,N*4);
  const et=new Float32Array(ebuf,4,N*2);          // parallel: node i's strongest-neighbor pos (NaN if none)

  {
    const tN = new Uint32Array(tbuf, 0, 1)[0];
    if (tN === N) {
      titleOffsets = new Uint32Array(tbuf, 4, tN + 1);
      titleBytes = new Uint8Array(tbuf, 4 + (tN + 1) * 4);
      titleDecoder = new TextDecoder('utf-8');
      console.log(`Title index loaded: ${tN.toLocaleString()} titles`);
    } else {
      console.warn(`titles.bin node count (${tN}) doesn't match viewer_full.bin (${N}) — ignoring, falling back to per-node DB query for titles`);
    }
  }

  // OUT-edges: what the "instant connections" sidebar and every forward-
  // direction pathfinder need.
  if (cbuf) {
    const header = new Uint32Array(cbuf, 0, 2);
    const csrFileN = header[0], csrE = header[1];
    if (csrFileN === N) {
      csrOffsets = new Uint32Array(cbuf, 8, csrFileN + 1);
      csrNeighbors = new Uint32Array(cbuf, 8 + (csrFileN + 1) * 4, csrE);
      csrN = csrFileN;
      console.log(`In-memory adjacency (out) loaded: ${csrN.toLocaleString()} nodes, ${csrE.toLocaleString()} entries`);
    } else {
      console.warn(`adjacency_csr.bin node count (${csrFileN}) doesn't match viewer_full.bin (${N}) — ignoring, falling back to live DB for pathfinding`);
    }
  } else {
    console.warn('adjacency_csr.bin failed to load from any source — pathfinding and instant connections will fall back to live DB queries');
  }

  // IN-edges: only used by bidirectional search's backward half (growing "who
  // can reach the target" from the target side needs real in-links, not the
  // same out-link structure walked backward -- see runBidirectionalBFS).
  if (cbufRev) {
    const header = new Uint32Array(cbufRev, 0, 2);
    const csrFileN = header[0], csrE = header[1];
    if (csrFileN === N) {
      csrOffsetsRev = new Uint32Array(cbufRev, 8, csrFileN + 1);
      csrNeighborsRev = new Uint32Array(cbufRev, 8 + (csrFileN + 1) * 4, csrE);
      console.log(`In-memory adjacency (in) loaded: ${csrFileN.toLocaleString()} nodes, ${csrE.toLocaleString()} entries`);
    } else {
      console.warn(`adjacency_csr_rev.bin node count (${csrFileN}) doesn't match viewer_full.bin (${N}) — ignoring, falling back to live DB for reverse pathfinding`);
    }
  } else {
    console.warn('adjacency_csr_rev.bin failed to load from any source — bidirectional pathfinding will fall back to live DB queries');
  }

  px=new Float32Array(N); py=new Float32Array(N); const rad=new Float32Array(N), col=new Uint8Array(N*4);
  const deg=new Float32Array(N), cat=new Uint8Array(N);
  nodeDegrees = deg; // module-level pathfinders rank frontiers hub-first off this

  for(let i=0;i<N;i++){
    px[i]=raw[i*4]; py[i]=raw[i*4+1];
    deg[i]=raw[i*4+2]; cat[i]=raw[i*4+3]|0;
    rad[i]=0.35+Math.log(1+deg[i])*0.19;
    const c=CAT[cat[i]]||[150,150,150];
    col[i*4]=c[0];col[i*4+1]=c[1];col[i*4+2]=c[2];col[i*4+3]=255;
  }

  // A band of ~5,000 nodes between ~95% and ~98.5% of the layout's max radius sits at
  // 10-50x the local point density of its surroundings (radius histogram: ~10-90
  // nodes per 10-unit bucket through r=1800-2700, then a spike to 500-1150/bucket at
  // r=2740-2840) — a visibly crisp ring, unlike the rest of the boundary, which is
  // fuzzy. This reads as a normalization/clamp artifact in the layout pipeline rather
  // than real structure. Spread that band across the same radial range the organic
  // outlier population already occupies, keeping each node's original angle (the only
  // part of its position likely to carry real signal).
  // A uniform-random radius between two fixed bounds (the first version of this fix)
  // still put a hard wall at the outer bound — nothing could ever land beyond it, so
  // the redistributed band's outer edge traced its own crisp circle instead of the old
  // one. Organic radial falloff has no hard edge, just decreasing density, so sample
  // an exponential offset past spreadLo instead: unbounded tail, most mass still near
  // spreadLo, no radius any point literally cannot cross.
  { let maxR=0; for(let i=0;i<N;i++){ const r=Math.hypot(px[i],py[i]); if(r>maxR) maxR=r; }
    const ringLo=maxR*0.94, spreadLo=maxR*0.62, decayScale=(maxR-spreadLo)/2.5;
    for(let i=0;i<N;i++){ const r=Math.hypot(px[i],py[i]);
      if(r>ringLo){ const th=Math.atan2(py[i],px[i]); const nr=spreadLo-Math.log(Math.random())*decayScale;
        px[i]=Math.cos(th)*nr; py[i]=Math.sin(th)*nr; } } }

  // Grid bounds must cover the actual data range, not a guessed constant. The old
  // hardcoded ±1500 was stale (real positions reach ~±2885) — every node beyond it got
  // clamp()'d into the single outermost ring of grid cells, which then held far more
  // real nodes than any interior cell, so cull()'s per-cell draw budget (BUDGET/M)
  // starved almost all of them: edges are drawn from raw, unclamped positions and
  // don't go through this grid at all, so they rendered fine while their own source
  // dots mostly didn't — nodes near the edges seemingly missing despite their edges
  // clearly reaching out there.
  let maxAbsX=0, maxAbsY=0;
  for(let i=0;i<N;i++){ const ax=Math.abs(px[i]), ay=Math.abs(py[i]); if(ax>maxAbsX) maxAbsX=ax; if(ay>maxAbsY) maxAbsY=ay; }
  const bound=Math.max(maxAbsX,maxAbsY)*1.02; // small margin so nothing sits exactly on the edge
  const minx=-bound,maxx=bound,miny=-bound,maxy=bound, cw=(maxx-minx)/G, ch=(maxy-miny)/G, NC=G*G;
  const cell=new Int32Array(N), cnt=new Int32Array(NC);
  for(let i=0;i<N;i++){ const cx=clamp((px[i]-minx)/cw|0,0,G-1), cy=clamp((py[i]-miny)/ch|0,0,G-1); const c=cy*G+cx; cell[i]=c; cnt[c]++; }
  const start=new Int32Array(NC+1); for(let c=0;c<NC;c++) start[c+1]=start[c]+cnt[c];
  const order=new Int32Array(N), fp=start.slice(0,NC);
  for(let i=0;i<N;i++){ const c=cell[i]; order[fp[c]++]=i; }

  // Populate Stats Panel Default Count
  if($('stat-nodes')) $('stat-nodes').textContent = N.toLocaleString();
  if($('stat-links')) $('stat-links').textContent = (N * 25).toLocaleString();

  // Try to connect to SQLite worker
  try {
    const { createDbWorker } = window;
    const urls = [
      // wiki_simulation_ctxfix.db: same schema/data as wiki_simulation.db, with
      // links.context corrected (see scraper/xml_parser.py's rewritten
      // parse_wikitext_links + runpod/merge_contexts.py). The original file is
      // untouched on HF -- revert this one URL to roll back.
      "https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/test_scrape/wiki_simulation_ctxfix.db",
      "/test_scrape/wiki_simulation.db"
    ];
    for (const url of urls) {
      try {
        if ($('loading-text')) $('loading-text').textContent = "Connecting to database...";
        console.log("Connecting to SQLite VFS at:", url);
        const worker = await createDbWorker(
          [{
            from: "inline",
            config: {
              serverMode: "full",
              url: url,
              // Every read is an HTTPS range request against a 25GB file, so latency per
              // request (~50-150ms) dwarfs transfer time. 4KB chunks meant one round trip
              // per B-tree page; 64KB fetches ~16 pages per trip, which is close to free
              // at this latency and collapses index descents into far fewer requests.
              requestChunkSize: 65536
              // maxReadSpeed intentionally unset: the previous 64KB/s cap throttled every
              // read to roughly one chunk per second, which is what made traversals crawl.
            }
          }],
          // Fully-resolved absolute URLs, not bare relative paths: the worker
          // script resolves its OWN relative URLs (e.g. sql-wasm.wasm) against
          // its own location, not the page's -- "libs/sql-wasm.wasm" from inside
          // a worker already loaded from .../libs/sqlite.worker.js doubled up to
          // .../libs/libs/sql-wasm.wasm, 404ing (GH Pages' 404 HTML then got fed
          // to WebAssembly.instantiate as if it were the wasm binary, hence the
          // "expected magic word ... found 3c 21 44 4f" ("<!DO...") error).
          new URL('libs/sqlite.worker.js', location.href).href,
          new URL('libs/sql-wasm.wasm', location.href).href
        );
        db = worker.db;
        originalQuery = db.query.bind(db);
        console.log("SQLite HTTP VFS connected successfully:", url);
        break;
      } catch (err) {
        console.warn("Failed VFS connection for", url, err);
      }
    }
  } catch (err) {
    console.error("VFS initialization failed completely. Proceeding in offline mode:", err);
  }

  // Setup Autocomplete and Search
  setupSearch(N, px, py);

  // Run the Deck.gl engine
  try {
    run(N,px,py,rad,col,deg,cat,et,{minx,miny,cw,ch,start,order});
  } catch(e) {
    console.error('run() failed:', e);
  }

}

// Start visualizer setup
startVisualization().catch(e=>console.error("Visualizer initialization failed:", e));

function setupSearch(N, px, py) {
  const searchBox = $('search-box');
  const datalist = $('article-list');
  if (searchBox && datalist) {
    const escapeAttr = s => s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

    searchBox.oninput = (e) => {
      const target = e.target;
      const val = target.value.trim();
      if (val.length < 3) {
        datalist.innerHTML = "";
        return;
      }

      // Instant path: titles.bin + the sorted index built in buildTitleSearchIndex()
      // are both in memory, so this is a binary search — no network round trip, no
      // debounce needed since it's synchronous and effectively free.
      const localResults = searchTitlesLocal(val, 10);
      if (localResults !== null) {
        datalist.innerHTML = localResults.map(idx => `<option value="${escapeAttr(titleOf(idx))}">`).join('');
        const listId = target.getAttribute('list');
        if (listId) { target.removeAttribute('list'); setTimeout(() => target.setAttribute('list', listId), 1); }
        return;
      }

      // Fallback: DB range query, only reachable before the local index finishes
      // building (or if titles.bin failed to load).
      if (!db) {
        console.warn("Autocomplete skipped: DB connection is null");
        return;
      }
      const loaderId = target.id === 'search-box' ? 'search-loader' : (target.id + '-loader');
      const loader = $(loaderId);
      if (loader) loader.style.display = 'block';
      if (searchTimeout) clearTimeout(searchTimeout);
      searchTimeout = setTimeout(async () => {
        try {
          const terms = val.split(/\s+/).filter(Boolean);
          let result = [];
          if (terms.length > 0) {
            // Variant 1: Title Case (Capitalize first letter of every word)
            const titleCaseVal = terms.map(t => t.charAt(0).toUpperCase() + t.slice(1)).join(' ');
            const start1 = titleCaseVal;
            const end1 = start1.slice(0, -1) + String.fromCharCode(start1.charCodeAt(start1.length - 1) + 1);

            // Variant 2: First-Word Capitalized (Capitalize first letter of first word, rest as typed)
            const firstCapVal = terms[0].charAt(0).toUpperCase() + terms[0].slice(1) + (terms.length > 1 ? ' ' + terms.slice(1).join(' ') : '');
            const start2 = firstCapVal;
            const end2 = start2.slice(0, -1) + String.fromCharCode(start2.charCodeAt(start2.length - 1) + 1);

            console.log(`Running parallel autocomplete range queries: [${start1}] and [${start2}]`);
            const [res1, res2] = await Promise.all([
              dbQuery(`SELECT id FROM nodes WHERE id >= ? AND id < ? LIMIT 10`, [start1, end1], PRIORITY_SEARCH),
              dbQuery(`SELECT id FROM nodes WHERE id >= ? AND id < ? LIMIT 10`, [start2, end2], PRIORITY_SEARCH)
            ]);

            // Merge and deduplicate results
            const seen = new Set();
            const merged = [];
            for (const r of [...(res1 || []), ...(res2 || [])]) {
              if (!seen.has(r.id)) {
                seen.add(r.id);
                merged.push(r);
              }
            }
            result = merged.slice(0, 10);
          }
          console.log("Autocomplete query raw result rows:", result);
          datalist.innerHTML = result.map(r => `<option value="${r.id}">`).join('');
          console.log("Updated datalist options HTML:", datalist.innerHTML);
          
          // Force browser to refresh the autocomplete dropdown by toggling the list attribute
          const listId = target.getAttribute('list');
          if (listId) {
            target.removeAttribute('list');
            setTimeout(() => target.setAttribute('list', listId), 1);
          }
        } catch (e) {
          console.error("Autocomplete query failed:", e);
        } finally {
          if (loader) loader.style.display = 'none';
        }
      }, 250);
    };

    searchBox.onchange = async () => {
      const val = searchBox.value.trim();
      if (!val) return;

      // Instant path: the picked suggestion came from our own local index (or, if
      // typed by hand, might still happen to be an exact title), so try resolving it
      // there before touching the network at all.
      const localIdx = findTitleIndexLocal(val);
      if (localIdx !== undefined) {
        window.selectNode(localIdx);
        return;
      }

      try {
        const idx = findTitleIndexInTitlesBin(val);
        if (idx !== -1) {
          window.selectNode(idx);
        } else {
          console.warn(`Node "${val}" is not present in the layout subset.`);
        }
      } catch (e) {
        console.error("Search selection failed:", e);
      }
    };

    const searchRandomBtn = $('search-random-btn');
    if (searchRandomBtn) {
      searchRandomBtn.onclick = () => {
        if (!titleOffsets) return; // titles.bin hasn't loaded yet
        const idx = Math.floor(Math.random() * (titleOffsets.length - 1));
        searchBox.value = titleOf(idx) || '';
        window.selectNode(idx);
      };
    }
  }

  // Setup route search autocomplete sharing the same datalist
  const routeStart = $('route-start');
  const routeEnd = $('route-end');
  [routeStart, routeEnd].forEach(el => {
    if (el) {
      el.oninput = searchBox.oninput;
    }
  });
}

window.selectNodeById = async (nodeId) => {
  const idx = findTitleIndexInTitlesBin(nodeId);
  if (idx !== -1) {
    window.selectNode(idx);
  } else {
    console.warn(`Node "${nodeId}" is not present in the layout subset.`);
  }
};

// Returns { orderedIndices, titleToIdx }. orderedIndices drops any title that failed
// to resolve (used for rendering positions, where a gap is harmless); titleToIdx keeps
// the full title->index map so callers that need path[i] <-> index[i] alignment (e.g.
// per-hop category coloring) aren't thrown off if one title in the middle is missing —
// with the filtered array alone that shifts every index after the gap, silently
// mismatching titles to the wrong category.
async function updateRouteIndices(path) {
  if (!path || path.length === 0) {
    currentRouteIndices = new Set();
    return { orderedIndices: [], titleToIdx: new Map() };
  }
  const titleToIdx = new Map();
  const orderedIndices = [];
  for (const title of path) {
    const idx = findTitleIndexInTitlesBin(title);
    if (idx !== -1) {
      titleToIdx.set(title, idx);
      orderedIndices.push(idx);
    }
  }
  currentRouteIndices = new Set(orderedIndices);
  return { orderedIndices, titleToIdx };
}

// Fetch the link "context" excerpt (same field the sidebar connections list shows)
// for one hop of a route.
//
// NEVER query this table with `source_idx = ? AND target_idx = ?`. That looks like the
// most selective possible lookup, but EXPLAIN QUERY PLAN shows SQLite resolves it as:
//
//     SEARCH TABLE links USING INDEX idx_links_tgt_idx (target_idx=?)
//
// i.e. it picks the *target* index, walks every in-link of the destination, and
// random-fetches each candidate row from the main table to test source_idx. Under
// sql-httpvfs every one of those row lookups is a separate 64KB HTTP range request, so
// on any well-linked destination it never finishes — measured >3 minutes with no result
// on Physics→Elementary particle, a small, already-known-good edge.
//
// Anchoring on source_idx instead keeps it on the other index:
//
//     SEARCH TABLE links USING INDEX idx_links_src_idx (source_idx=?)
//
// which reads one contiguous index range for a single article. Same edge: 1.7s.
// A hop's edge may be stored in either direction, so we check the source's out-links
// first and fall back to the destination's out-links, rather than ever asking the
// database to match on target_idx.
const titleToDbIdxCache = new Map();
async function resolveDbIdx(title) {
  if (!title) return -1;
  if (titleToDbIdxCache.has(title)) {
    return titleToDbIdxCache.get(title);
  }
  try {
    const rows = await dbQuery(
      `SELECT rowid - 1 AS idx_db FROM nodes WHERE id = ?`,
      [title],
      PRIORITY_CLICK
    );
    if (rows && rows.length > 0) {
      const idx = rows[0].idx_db;
      titleToDbIdxCache.set(title, idx);
      return idx;
    }
  } catch (e) {
    console.error("Failed to resolve db index for", title, e);
  }
  return -1;
}

const ROUTE_CONTEXT_TIMEOUT_MS = 25000;

// One article's outgoing links that carry context, keyed by db idx. Cached because
// consecutive hops of a route share endpoints (A→B then B→C both want B's out-links),
// so an N-hop route costs ~N queries rather than 2N.
//
// No LIMIT here — this used to cap at 100, which silently missed real edges for any
// hub with more than 100 context-bearing out-links (confirmed on Physics: 219 such
// links post context-fix, with the specific route hop landing at position >100, so
// the route showed no context for a real, existing edge). A LIMIT here is a
// correctness bug, not a perf knob: this isn't a curated top-N for display (that's
// the sidebar's job, with its own deliberate LIMIT 20), it's "does this exact edge
// have context" — any cap can always be defeated by a big-enough hub, and the CSR's
// own max degree is 184,958. The query still only ever touches source_idx=? (the
// single-column index, same one this whole file's comments warn to never pair with
// target_idx in one WHERE), so it can't regress into the documented two-column scan
// hang — the only cost of removing the cap is more data for extreme hubs, which the
// existing ROUTE_CONTEXT_TIMEOUT_MS race below already bounds.
const outLinksCache = new Map();
async function fetchOutLinks(dbIdx) {
  if (dbIdx < 0) return [];
  if (outLinksCache.has(dbIdx)) return outLinksCache.get(dbIdx);

  const rows = await Promise.race([
    dbQuery(
      `SELECT target_idx AS nidx, context FROM links WHERE source_idx = ? AND context IS NOT NULL AND context != ""`,
      [dbIdx],
      PRIORITY_CLICK
    ),
    new Promise((_, reject) => setTimeout(() => reject(new Error('context fetch timed out')), ROUTE_CONTEXT_TIMEOUT_MS))
  ]) || [];

  outLinksCache.set(dbIdx, rows);
  return rows;
}

async function fetchSingleLinkContext(aTitle, bTitle) {
  if (!db || !aTitle || !bTitle) return null;

  try {
    // Sequential, not Promise.all: the query worker executes one statement at a time,
    // so firing these together only makes the second one's timeout start ticking while
    // it sits in the queue. Both are near-instant once cached.
    const aIdx = await resolveDbIdx(aTitle);
    const bIdx = await resolveDbIdx(bTitle);
    if (aIdx < 0 || bIdx < 0) return null;

    // Forward direction: does A link out to B?
    const fwd = await fetchOutLinks(aIdx);
    const hitFwd = fwd.find(r => r.nidx === bIdx && r.context);
    if (hitFwd) return hitFwd.context;

    // Otherwise the edge is stored the other way round: does B link out to A?
    const rev = await fetchOutLinks(bIdx);
    const hitRev = rev.find(r => r.nidx === aIdx && r.context);
    if (hitRev) return hitRev.context;

    return null;
  } catch (e) {
    if (e?.name !== 'QueryEvicted' && e?.message !== 'context fetch timed out') console.error("Failed to fetch link context:", e);
    return null;
  }
}

function run(N,px,py,rad,col,deg,cat,et,grid){
  const {minx,miny,cw,ch,start,order}=grid;
  const sPos=new Float32Array(BUDGET*2), sRad=new Float32Array(BUDGET), sCol=new Uint8Array(BUDGET*4), sIdx=new Int32Array(BUDGET);
  const eS=new Float32Array(BUDGET*2), eT=new Float32Array(BUDGET*2);
  let CV=null; const dims=()=>{ if(!CV)CV=$('graph-canvas'); const w=CV?CV.clientWidth:0,h=CV?CV.clientHeight:0; return [w||1200,h||800]; };
  let VS={target:[0,0,0],zoom:-1};
  const tt=$('tooltip');

  const deckgl=new Deck({
    canvas:'graph-canvas', views:[new OrthographicView({flipY:false})], viewState:VS,
    controller:{scrollZoom:{speed:0.045,smooth:false}, inertia:300, dragPan:true},
    // Default hit-testing only registers a click that lands on the rendered pixel(s)
    // of a node. At radiusMinPixels this small, that's a sub-pixel target — this adds
    // a forgiving invisible cushion around each point so clicking near a tiny dot
    // still hits it, without changing how big anything looks.
    pickingRadius: 8,
    onViewStateChange:({viewState})=>{ VS=viewState; deckgl.setProps({viewState:VS}); cull(VS); },
    onClick: ({index}) => {
      if (index >= 0) {
        const i = sIdx[index];
        // Directly clicking a node you can already see shouldn't yank the camera to
        // re-center it — that's what made this confusing. Camera panning is reserved
        // for search/route-list selection, where the target may be off-screen and
        // needs navigation to reach.
        window.selectNode(i, false, false);
      }
    },
    onHover:({index,x,y})=>{
      if(index>=0 && tt){
        const i=sIdx[index];
        const instantTitle = titleOf(i); // client-side lookup, no query
        if (instantTitle || hoverCache.has(i)) {
          if($('tt-loader')) $('tt-loader').style.display='none';
          const title = instantTitle || hoverCache.get(i);
          if($('tt-title')) $('tt-title').textContent=`${title} · degree ${deg[i]|0}`;
          if($('tt-cat')) $('tt-cat').textContent=CATNAME[cat[i]]||'';
          tt.style.left=(x+14)+'px'; tt.style.top=(y+14)+'px'; tt.style.opacity='1'; tt.classList.add('visible');
        } else {
          if($('tt-loader')) $('tt-loader').style.display='block';
          if($('tt-title')) $('tt-title').textContent=`Loading... · degree ${deg[i]|0}`;
          if($('tt-cat')) $('tt-cat').textContent=CATNAME[cat[i]]||'';
          tt.style.left=(x+14)+'px'; tt.style.top=(y+14)+'px'; tt.style.opacity='1'; tt.classList.add('visible');
          
          if (hoverTimeout) clearTimeout(hoverTimeout);
          if (db) {
            hoverTimeout = setTimeout(() => {
              if (lastHoveredIdx !== i) return; // Only query if the cursor is still resting on this node!
              console.log("Hover VFS query for index:", i, "rowid:", i + 1);
              dbQuery('SELECT id FROM nodes WHERE rowid = ?', [i + 1], PRIORITY_HOVER, 'hover').then(rows => {
                if (rows && rows.length > 0) {
                  const title = rows[0].id;
                  hoverCache.set(i, title);
                  if (lastHoveredIdx === i) {
                    if($('tt-title')) $('tt-title').textContent=`${title} · degree ${deg[i]|0}`;
                    if($('tt-loader')) $('tt-loader').style.display='none';
                  }
                }
              }).catch(err => {
                if (err?.name !== 'QueryEvicted') console.error("Hover query failed for rowid:", i + 1, err);
                if (lastHoveredIdx === i && $('tt-loader')) $('tt-loader').style.display='none';
              });
            }, 300);
          }
        }
        lastHoveredIdx = i;
      } else if(tt){ 
        tt.style.opacity='0'; 
        tt.classList.remove('visible'); 
        if (hoverTimeout) clearTimeout(hoverTimeout);
        if($('tt-loader')) $('tt-loader').style.display='none';
        lastHoveredIdx = -1;
      }
    }
  });

  window.selectNode = async function(nodeIndex, isBackNavigation = false, shouldPan = true) {
    if (!isBackNavigation && selectedNodeIdx !== -1 && selectedNodeIdx !== nodeIndex) {
      clickHistory.push(selectedNodeIdx);
    }
    // Halt the camera flight, but keep the route displayed — inspecting a node is how
    // you read a route, so selecting one must not throw the route away. (Clicking a hop
    // in the results list routes through here too, so clearing here previously deleted
    // the very route the user was clicking into, and left showNodeDetails' route
    // prev/next lookup reading an already-nulled currentRoutePath.)
    stopRouteAnimation();
    selectedNodeIdx = nodeIndex;
    selectedConnections = new Set([nodeIndex]); // Reset connection highlighting to selected node only initially

    if (shouldPan) {
      // Smooth target camera pan - Zoom in closer (3.2). Only for navigating to a node
      // that may be off-screen (search, route list); a direct canvas click already has
      // the node in view, so panning to it there is just disorienting.
      const sx = px[nodeIndex], sy = py[nodeIndex];
      VS = { ...VS, target: [sx, sy, 0], zoom: Math.max(VS.zoom, 3.2) };
      deckgl.setProps({ viewState: VS });
    }
    cull(VS);

    await showNodeDetails(nodeIndex);
  };

  async function showNodeDetails(nodeIndex) {
    const sidebar = $('detail-sidebar');
    if (!sidebar) return;
    sidebar.classList.add('active');

    const backBtn = $('sidebar-back');
    if (backBtn) {
      backBtn.style.display = clickHistory.length > 0 ? 'flex' : 'none';
    }

    // titleOf() is a client-side array lookup (titles.bin) — instant, no query.
    // hoverCache is a fallback for whenever that asset didn't load; "#idx" is the
    // last resort while the real title is in flight from the DB.
    let title = titleOf(nodeIndex) || hoverCache.get(nodeIndex);
    if (!title) {
      title = `#${nodeIndex + 1}`;
    }

    // Evict old speculative precached neighbors
    activePrecachedIndices.forEach(idx => {
      if (idx !== nodeIndex) {
        detailsCache.delete(idx);
      }
    });
    activePrecachedIndices.clear();

    // 1. Instant-render: Populate basic metadata immediately from memory (0.0ms)
    const colHex = CAT[cat[nodeIndex]] ? `rgb(${CAT[cat[nodeIndex]].join(',')})` : '#9d9894';
    $('sidebar-tag').textContent = CATNAME[cat[nodeIndex]] || 'Other';
    $('sidebar-tag').style.borderColor = colHex;
    $('sidebar-tag').style.color = colHex;
    $('sidebar-title').textContent = title;
    $('sidebar-inbound').textContent = deg[nodeIndex] | 0;
    $('sidebar-outbound').textContent = "Loading...";
    $('sidebar-desc').textContent = "Loading excerpt preview...";
    $('sidebar-wiki-link').href = `https://en.wikipedia.org/wiki/${encodeURIComponent(title)}`;

    const connList = $('sidebar-connections');
    if (connList) {
      connList.innerHTML = "<li style='padding:10px; display:flex; align-items:center; gap:8px; color:var(--color-ash)'><div class='mini-loader' style='display:block; width:12px; height:12px; border-color:rgba(255,255,255,0.1); border-top-color:var(--color-bone); border-width:1.5px;'></div>Loading connections...</li>";
    }

    // Instant connections: adjacency_csr.bin + titles.bin are both already resident in
    // memory, so the neighbor list itself needs no query at all — only the per-link
    // "context" quote and in/out direction label genuinely require the DB. Render this
    // immediately (hub-first, matching the DB version's degree-ish ordering intent),
    // then let the real network query below replace it with the enriched version
    // whenever it lands. This is what makes clicking an arbitrary node feel instant
    // instead of waiting on a network round trip just to see what it connects to.
    if (connList && csrOffsets && titleOffsets && !detailsCache.has(nodeIndex)) {
      const s = csrOffsets[nodeIndex], e = csrOffsets[nodeIndex + 1];
      const neighbors = Array.from(csrNeighbors.subarray(s, e))
        .sort((a, b) => (nodeDegrees[b] || 0) - (nodeDegrees[a] || 0))
        .slice(0, 40);
      if (neighbors.length > 0) {
        connList.innerHTML = neighbors.map(idx => {
          const t = titleOf(idx) || `#${idx + 1}`;
          const tagCol = CAT[cat[idx]] ? `rgb(${CAT[cat[idx]].join(',')})` : '#9d9894';
          return `<li class="connection-item">
              <div class="connection-main" onclick="window.selectNode(${idx})">
                <span style="color: ${tagCol}; font-weight:600">${t}</span>
              </div>
              <div style="padding: 4px 8px; display: flex; align-items: center; gap: 6px; margin-top: 4px;">
                <div class="mini-loader" style="display: block; width: 10px; height: 10px; border-color: rgba(255,255,255,0.1); border-top-color: var(--color-bone); border-width: 1.5px;"></div>
                <span style="color: var(--color-ash); font-size: 11px;">Loading link context…</span>
              </div>
            </li>`;
        }).join('');
        selectedConnections = new Set([nodeIndex, ...neighbors]);
        cull(VS);
      }
    }

    let realTitle = title;
    let snippetText = "Loading excerpt preview...";
    let inboundVal = deg[nodeIndex] | 0;
    let outboundVal = "Loading...";
    let isMetadataLoaded = false;

    // Check cache
    if (detailsCache.has(nodeIndex)) {
      const cached = detailsCache.get(nodeIndex);
      realTitle = cached.title;
      inboundVal = cached.inbound;
      outboundVal = cached.outbound;
      snippetText = cached.snippet;
      isMetadataLoaded = true;

      $('sidebar-tag').textContent = cached.category;
      $('sidebar-tag').style.borderColor = cached.tagCol;
      $('sidebar-tag').style.color = cached.tagCol;
      $('sidebar-title').textContent = realTitle;
      $('sidebar-inbound').textContent = inboundVal;
      $('sidebar-outbound').textContent = outboundVal;
      $('sidebar-desc').textContent = snippetText;
      $('sidebar-wiki-link').href = cached.wikiLink;

      // If connections are already cached, it's a full hit!
      if (cached.connectionsHTML !== null) {
        selectedConnections = new Set([nodeIndex, ...cached.neighborIndices]);
        cull(VS);
        if (connList) {
          connList.innerHTML = cached.connectionsHTML;
        }
        return;
      }
    }

    if (!db) {
      $('sidebar-outbound').textContent = "Unknown";
      $('sidebar-desc').textContent = "Offline mode - SQLite database not connected.";
      return;
    }

    try {
      // 2. Fetch metadata if not loaded from cache
      if (!isMetadataLoaded) {
        const title = titleOf(nodeIndex);
        let rows;
        if (title) {
          console.log("Sidebar query for title:", title);
          rows = await dbQuery('SELECT id, category, inDegree, outDegree, snippet FROM nodes WHERE id = ?', [title], PRIORITY_CLICK);
        } else {
          console.log("Sidebar query for nodeIndex:", nodeIndex, "rowid:", nodeIndex + 1);
          rows = await dbQuery('SELECT id, category, inDegree, outDegree, snippet FROM nodes WHERE rowid = ?', [nodeIndex + 1], PRIORITY_CLICK);
        }
        console.log("Sidebar query result:", rows);
        if (rows && rows.length > 0) {
          const n = rows[0];
          realTitle = n.id;
          hoverCache.set(nodeIndex, realTitle); // Keep hover cache updated

          snippetText = cleanWikiText(n.snippet || "No excerpt available.");
          inboundVal = n.inDegree;
          outboundVal = n.outDegree;

          // Update UI with fetched metadata
          $('sidebar-title').textContent = realTitle;
          $('sidebar-inbound').textContent = inboundVal;
          $('sidebar-outbound').textContent = outboundVal;
          $('sidebar-desc').textContent = snippetText;
          $('sidebar-wiki-link').href = `https://en.wikipedia.org/wiki/${encodeURIComponent(realTitle)}`;

          // If snippet is missing, attempt live fetch from Wikipedia API
          if (!snippetText || snippetText === "No excerpt available.") {
            const live = await fetchLiveWikiSnippet(realTitle);
            if (live) {
              snippetText = cleanWikiText(live);
              $('sidebar-desc').textContent = snippetText;
              // Update cached entry later (handled by detailsCache set below)
            }
          }
        }
      }

      let connectionsHTML = "";
      let neighborIndices = [];
      // Hoisted alongside the above, not declared where it's built (line ~1094):
      // it's read later at line ~1182 to fire off each "loading" context's
      // background fetch, well after the `if (anchorDbIdx !== -1)` block that
      // builds it has closed. A block-scoped `const` there threw
      // ReferenceError on every single sidebar open, silently aborting
      // everything after it -- caching (detailsCache.set) and speculative
      // neighbor pre-caching included, never running for any node.
      let mappedRows = [];

      // 3. Run connections query in the background
      let anchorDbIdx = -1;
      const title = titleOf(nodeIndex);
      if (title) {
        try {
          const nodeRows = await dbQuery(
            `SELECT rowid - 1 AS idx_db FROM nodes WHERE id = ?`,
            [title],
            PRIORITY_CLICK
          );
          if (nodeRows && nodeRows.length > 0) {
            anchorDbIdx = nodeRows[0].idx_db;
          }
        } catch (e) {
          console.error("Failed to map node Index for sidebar connections:", e);
        }
      }

      if (anchorDbIdx !== -1) {
        const linksRows = await dbQuery(`
          SELECT * FROM (SELECT target_idx AS neighbor_idx, target AS neighbor_id, context, 'out' AS type FROM links WHERE source_idx = ? AND context IS NOT NULL AND context != "" LIMIT 20)
          UNION ALL
          SELECT * FROM (SELECT source_idx AS neighbor_idx, source AS neighbor_id, context, 'in' AS type FROM links WHERE target_idx = ? AND context IS NOT NULL AND context != "" LIMIT 20)
        `, [anchorDbIdx, anchorDbIdx], PRIORITY_CLICK) || [];

        // Map neighbor titles back to their layout indices
        mappedRows = [];
        neighborIndices = [];
        const seenNeighborTitles = new Set();

        linksRows.forEach(row => {
          const tgtIdx = findTitleIndexInTitlesBin(row.neighbor_id);
          if (tgtIdx !== -1) {
            neighborIndices.push(tgtIdx);
            seenNeighborTitles.add(row.neighbor_id);
            mappedRows.push({
              neighbor_idx: tgtIdx,
              neighbor_id: row.neighbor_id,
              context: row.context,
              type: row.type
            });
          }
        });

        // Query and prepend route neighbors if they are not already in the mappedRows
        const routeNeighbors = [];
        if (currentRoutePath && currentRoutePath.length > 0) {
          const rIdx = currentRoutePath.indexOf(title);
          if (rIdx !== -1) {
            if (rIdx > 0) routeNeighbors.push({ title: currentRoutePath[rIdx - 1], type: 'prev' });
            if (rIdx < currentRoutePath.length - 1) routeNeighbors.push({ title: currentRoutePath[rIdx + 1], type: 'next' });
          }
        }

        for (const rn of routeNeighbors) {
          if (!seenNeighborTitles.has(rn.title)) {
            const rnIdx = findTitleIndexInTitlesBin(rn.title);
            if (rnIdx !== -1) {
              neighborIndices.push(rnIdx);
              const uniqueId = `sidebar-route-ctx-${rnIdx}`;
              mappedRows.unshift({
                neighbor_idx: rnIdx,
                neighbor_id: rn.title,
                context: "loading",
                unique_id: uniqueId,
                type: `route ${rn.type}`
              });
            }
          } else {
            const row = mappedRows.find(r => r.neighbor_id === rn.title);
            if (row) {
              row.type = `${row.type} (route ${rn.type})`;
            }
          }
        }

        // Update selected connections set and trigger redraw
        selectedConnections = new Set([nodeIndex, ...neighborIndices]);
        cull(VS);

        connectionsHTML = mappedRows.map(row => {
          const tgtIdx = row.neighbor_idx;
          const tagCol = tgtIdx >= 0 && CAT[cat[tgtIdx]] ? `rgb(${CAT[cat[tgtIdx]].join(',')})` : '#9d9894';
          const isRoute = row.type.includes('route');
          const borderStyle = isRoute ? `border: 1px solid ${tagCol}; background: rgba(${CAT[cat[tgtIdx]]?.join(',') || '150,150,150'}, 0.08); padding: 4px; border-radius: 4px; margin-bottom: 4px;` : '';
          
          let contextHtml = '';
          if (row.context === 'loading') {
            contextHtml = `
              <div id="${row.unique_id}" style="padding: 4px 8px; display: flex; align-items: center; gap: 6px;">
                <div class="mini-loader" style="display: block; width: 10px; height: 10px; border-color: rgba(255,255,255,0.1); border-top-color: var(--color-bone); border-width: 1.5px;"></div>
                <span style="color: var(--color-ash); font-size: 11px;">Loading link context…</span>
              </div>`;
          } else if (row.context) {
            contextHtml = `<div class="connection-context" onclick="this.classList.toggle('expanded')">${cleanAndHighlightWikiText(row.context, row.neighbor_id, tagCol)}</div>`;
          }

          return `
            <li class="connection-item" style="${borderStyle}">
              <div class="connection-main" onclick="window.selectNode(${tgtIdx})">
                <span style="color: ${tagCol}; font-weight:600">${row.neighbor_id}</span>
                <span class="connection-tag" style="background: rgba(255,255,255,0.05); color: var(--color-ash)">${row.type}</span>
              </div>
              ${contextHtml}
            </li>
          `;
        }).join('');
      }

      if (connList) {
        connList.innerHTML = connectionsHTML;
      }

      // Fetch the loading contexts in the background
      mappedRows.forEach(row => {
        if (row.context === 'loading') {
          (async () => {
            const context = await fetchSingleLinkContext(titleOf(nodeIndex), row.neighbor_id);
            const slot = document.getElementById(row.unique_id);
            if (slot) {
              if (context) {
                const tagCol = row.neighbor_idx >= 0 && CAT[cat[row.neighbor_idx]] ? `rgb(${CAT[cat[row.neighbor_idx]].join(',')})` : '#9d9894';
                const highlightedText = cleanAndHighlightWikiText(context, row.neighbor_id, tagCol);
                slot.outerHTML = `<div class="connection-context" onclick="this.classList.toggle('expanded')" style="padding: 4px 8px; cursor: pointer;">${highlightedText}</div>`;
              } else {
                slot.outerHTML = `<div class="connection-context" style="padding: 4px 8px; opacity: 0.6; font-style: italic;">Linked on route (no text context available)</div>`;
              }
            }
          })();
        }
      });

      // Store/upgrade fully cached details
      detailsCache.set(nodeIndex, {
        category: CATNAME[cat[nodeIndex]] || 'Other',
        tagCol: colHex,
        title: realTitle,
        inbound: inboundVal,
        outbound: outboundVal,
        snippet: snippetText,
        wikiLink: `https://en.wikipedia.org/wiki/${encodeURIComponent(realTitle)}`,
        connectionsHTML: connectionsHTML,
        neighborIndices: neighborIndices
      });

      // 4. Speculative Pre-caching of neighbors
      if (neighborIndices.length > 0) {
        setTimeout(async () => {
          try {
            const neighborTitles = neighborIndices.map(idx => titleOf(idx)).filter(t => t !== null);
            const placeholders = neighborTitles.map(() => '?').join(',');
            const neighborMetadata = await dbQuery(`
              SELECT id, category, inDegree, outDegree, snippet
              FROM nodes
              WHERE id IN (${placeholders})
            `, neighborTitles, PRIORITY_PRECACHE) || [];

            neighborMetadata.forEach(row => {
              const idx = findTitleIndexInTitlesBin(row.id);
              if (!detailsCache.has(idx)) {
                const tagCol = CAT[cat[idx]] ? `rgb(${CAT[cat[idx]].join(',')})` : '#9d9894';
                detailsCache.set(idx, {
                  category: row.category,
                  tagCol: tagCol,
                  title: row.id,
                  inbound: row.inDegree,
                  outbound: row.outDegree,
                  snippet: cleanWikiText(row.snippet || "No excerpt available."),
                  wikiLink: `https://en.wikipedia.org/wiki/${encodeURIComponent(row.id)}`,
                  connectionsHTML: null,
                  neighborIndices: null
                });
                activePrecachedIndices.add(idx);
              }
            });
            console.log("Speculatively pre-cached", activePrecachedIndices.size, "neighbors.");
          } catch (err) {
            console.error("Speculative pre-caching failed:", err);
          }
        }, 80);
      }
    } catch (e) {
      console.error('Sidebar load failed:', e);
    }
  } // close showNodeDetails

  // Is this position currently inside the visible viewport (no margin — this is
  // "would a human see it right now", not cull()'s prefetch-oversized bounds)?
  function isPointInView(x, y, vs) {
    const [W, H] = dims();
    const s = Math.pow(2, vs.zoom);
    const hw = (W / 2) / s, hh = (H / 2) / s;
    return Math.abs(x - vs.target[0]) <= hw && Math.abs(y - vs.target[1]) <= hh;
  }

  // Zoom out (target/pan never changes) just enough to keep a spreading search
  // visible, instead of it wandering off the edge of the screen. A fixed small
  // step per trigger, not a jump straight to "exact fit" — the point is watching
  // the view gradually pull back as the search grows, not snapping to a
  // pre-computed bounding box.
  const SEARCH_ZOOM_OUT_STEP = 0.35;
  const SEARCH_ZOOM_TRANSITION_MS = 500;
  let lastZoomOutAt = 0;
  function zoomOutIfNeeded(indices) {
    let offscreen = false;
    for (const idx of indices) {
      if (!isPointInView(px[idx], py[idx], VS)) { offscreen = true; break; }
    }
    if (!offscreen) return;
    const now = performance.now();
    if (now - lastZoomOutAt < SEARCH_ZOOM_TRANSITION_MS) return; // let one transition finish before starting the next
    lastZoomOutAt = now;
    VS = {
      ...VS,
      zoom: VS.zoom - SEARCH_ZOOM_OUT_STEP,
      transitionDuration: SEARCH_ZOOM_TRANSITION_MS,
      transitionInterpolator: new LinearInterpolator({ transitionProps: ['zoom'] }),
      transitionEasing: easeInOutQuad
    };
    deckgl.setProps({ viewState: VS });
  }

  // BFS/DFS report each newly-discovered batch here (a whole level for BFS, one
  // node for DFS — this function doesn't care which). cull() rebuilds every
  // visible node's position/colour buffer, so redraws are throttled to ~15fps;
  // the offscreen check itself is cheap and runs on every call so a batch never
  // silently drifts off-frame just because its redraw got throttled.
  //
  // Awaited by the caller, not fire-and-forget: pacing the traversal so it's
  // visible at all (an in-memory CSR search finishes in single-digit
  // milliseconds otherwise) is purely a presentation concern, so it lives here
  // in the UI-side reporting hook, not in the pathfinding algorithms themselves.
  // Only the first PACE_STEPS_MAX calls get slowed down, so a large/failing
  // search doesn't hang around waiting on artificial delay after the
  // interesting part of the animation is already over.
  let lastSearchPaint = 0;
  let paceStepsUsed = 0;
  // Total artificial delay actually applied so far this search -- executePathfinder
  // resets this before dispatch and subtracts it from the wall-clock time it reports,
  // so the timing toast shows how long the algorithm took, not how long the
  // watchable-animation pacing took.
  let pacedDelayMs = 0;
  const PACE_STEPS_MAX = 150;
  // Raised from 35ms: this used to pace once per EDGE examined, so even a modest
  // search meant hundreds of paced calls and 35ms each was already a long wait.
  // Now that DFS batches onProgress once per POPPED NODE (see runSimpleDFS), a
  // typical short search is only a handful of calls total -- at 35ms that's under
  // half a second, over before it's perceptible as edges "growing" rather than a
  // single flash. 90ms keeps the same PACE_STEPS_MAX-call ceiling (worst case
  // still bounded, ~13s) while actually being visible for the common case.
  const PACE_DELAY_MS = 90;
  // edges: array of [fromIdx, toIdx] pairs — every edge the algorithm actually
  // examined this step, not just the nodes it landed on. Both ends get marked
  // visited and pushed into searchFrontierEdges so cull()'s existing search-edges
  // LineLayer (already there, previously fed by nothing any button used) draws
  // the traversal as it spreads, not just the node dots.
  async function markSearchProgress(edges) {
    searchActive = true;
    const touchedNodes = [];
    for (const [from, to] of edges) {
      searchVisitedNodes.add(from);
      searchVisitedNodes.add(to);
      searchFrontierEdges.push(from, to);
      touchedNodes.push(to);
    }
    zoomOutIfNeeded(touchedNodes);
    const now = performance.now();
    if (now - lastSearchPaint > 66) {
      lastSearchPaint = now;
      cull(VS);
    }
    if (paceStepsUsed++ < PACE_STEPS_MAX) {
      pacedDelayMs += PACE_DELAY_MS;
      await new Promise(r => setTimeout(r, PACE_DELAY_MS));
    }
  }

  function renderSearchVisualization() {
    // All visual state is driven through cull() — just trigger a redraw
    cull(VS);
  }


  // Setup Route Finder execution
  const findRouteBtn = $('find-route-btn');
  const routeAlgoSelect = $('route-algo');
  const routeStart = $('route-start');
  const routeEnd = $('route-end');
  const routeLoader = $('route-loader');
  const routeResult = $('route-result-container');
  const routeTextPath = $('route-text-path');

  const easeInOutQuad = t => t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2, 2)/2;
  const ROUTE_STEP_MS = 700; // wall-clock time per hop, must exceed the transition below

  const ROUTE_ANIMATE_MAX_HOPS = 50; // DFS in particular can return paths in the hundreds/thousands of hops (no shortest-path preference) -- flying through those one at a time would take minutes

  function animateRoute(pathIndices) {
    if (routeAnimationInterval) clearInterval(routeAnimationInterval);

    // Remember the whole path before revealing it hop by hop, so an interrupted flight
    // can be completed rather than left half-drawn.
    currentRouteOrdered = Array.from(pathIndices);

    const caption = $('route-caption'), captionStep = $('route-caption-step'), captionTitle = $('route-caption-title');

    // Long routes: skip the node-by-node flight entirely and just show the whole
    // route at once, camera framed to fit it.
    if (pathIndices.length > ROUTE_ANIMATE_MAX_HOPS) {
      currentRouteIndices = new Set(pathIndices);
      if (caption) { caption.style.opacity = '0'; caption.style.transform = 'translateX(-50%) translateY(-8px)'; }

      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (const idx of pathIndices) {
        const x = px[idx], y = py[idx];
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
      const [w, h] = dims();
      const extent = Math.max(maxX - minX, maxY - minY, 50) * 1.3; // margin; floored so a tightly-clustered route doesn't zoom in absurdly far
      VS = {
        ...VS,
        target: [(minX + maxX) / 2, (minY + maxY) / 2, 0],
        zoom: Math.log2(Math.min(w, h) / extent),
        transitionDuration: 700,
        transitionInterpolator: new LinearInterpolator({ transitionProps: ['target', 'zoom'] }),
        transitionEasing: easeInOutQuad
      };
      deckgl.setProps({ viewState: VS });
      cull(VS);
      return;
    }

    currentRouteIndices = new Set();
    cull(VS);

    let step = 0;
    routeAnimationInterval = setInterval(() => {
      if (step >= pathIndices.length) {
        clearInterval(routeAnimationInterval);
        routeAnimationInterval = null;
        if (caption) { caption.style.opacity = '0'; caption.style.transform = 'translateX(-50%) translateY(-8px)'; }
        return;
      }

      const nodeIdx = pathIndices[step];
      currentRouteIndices.add(nodeIdx);

      // Fly the camera to the newly revealed hop instead of snapping to it — deck.gl
      // animates 'target'/'zoom' itself over transitionDuration once these props are
      // set; onViewStateChange (already wired for manual pan/zoom) fires each
      // intermediate frame and keeps VS/cull() in sync with it automatically.
      const sx = px[nodeIdx], sy = py[nodeIdx];
      VS = {
        ...VS,
        target: [sx, sy, 0],
        zoom: Math.max(VS.zoom, 2.5),
        transitionDuration: ROUTE_STEP_MS - 100,
        transitionInterpolator: new LinearInterpolator({ transitionProps: ['target', 'zoom'] }),
        transitionEasing: easeInOutQuad
      };
      deckgl.setProps({ viewState: VS });

      // Caption the article the camera is currently visiting — titleOf() is an
      // instant client-side lookup (titles.bin), so this never waits on a query.
      if (caption && captionTitle) {
        const title = titleOf(nodeIdx) || `#${nodeIdx + 1}`;
        captionStep.textContent = `Hop ${step + 1} of ${pathIndices.length}`;
        captionTitle.textContent = title;
        caption.style.opacity = '1';
        caption.style.transform = 'translateX(-50%) translateY(0)';
      }

      cull(VS);
      step++;
    }, ROUTE_STEP_MS);
  }

// mode -> {label, run}. label feeds both the failure alert and the timing toast;
// run(startVal, endVal, opts) is one of the module-level pathfinders, all sharing
// the same {onEndpoints, onProgress} contract.
const PATHFINDER_ALGOS = {
  bidirectional: { label: 'Bidirectional BFS', run: runBidirectionalBFS },
  bfs: { label: 'BFS', run: runSimpleBFS },
  dfs: { label: 'DFS', run: runSimpleDFS },
  astar: { label: 'A*', run: runAStarPathfinder },
  greedy: { label: 'Greedy Best-First', run: runGreedyBestFirst },
  dijkstra: { label: 'Dijkstra', run: runDijkstraWeighted },
  randomwalk: { label: 'Random Walk', run: runRandomWalk }
};

function showRouteTiming(text) {
  const toast = $('route-timing-toast');
  if (!toast) return;
  toast.textContent = text;
  toast.style.opacity = '1';
  toast.style.transform = 'translateX(-50%) translateY(0)';
  clearTimeout(showRouteTiming._t);
  showRouteTiming._t = setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(-50%) translateY(8px)';
  }, 4000);
}

async function executePathfinder(mode) {
  const startVal = routeStart.value.trim();
  const endVal = routeEnd.value.trim();
  if (!startVal || !endVal || !db) return;
  const algo = PATHFINDER_ALGOS[mode] || PATHFINDER_ALGOS.bidirectional;

  // Clear any running pathfinding animation or highlights and visualization state
  clearRoute();
  searchActive = false;
  searchVisitedNodes.clear();
  searchFrontierEdges = [];
  paceStepsUsed = 0; // fresh animation budget for this search
  pacedDelayMs = 0;  // fresh timing budget too -- see its declaration
  lastZoomOutAt = 0;

  findRouteBtn.disabled = true;
  if (routeLoader) routeLoader.style.display = 'block';

  const t0 = performance.now();
  try {
    // Bidirectional BFS: meets in the middle from both ends, unweighted-graph
    // optimal in hop count. Simple BFS/DFS: textbook single-direction traversals,
    // no shortest-path guarantee for DFS — see runSimpleBFS/runSimpleDFS. A*/greedy/
    // Dijkstra: heap-guided, see runHeapPathfinder. Random walk: see runRandomWalk.
    const onEndpoints = (s, e) => {
      searchStartIdx = s; searchEndIdx = e;
      // Fly to the start node so the search animation is actually legible — without
      // this the camera stays wherever it happened to be (often zoomed out enough
      // that highlighted nodes are sub-pixel and the traversal edges are invisible
      // among the background link haze, reading as "nothing is happening").
      VS = {
        ...VS,
        target: [px[s], py[s], 0],
        zoom: Math.max(VS.zoom, 2.5),
        transitionDuration: 600,
        transitionInterpolator: new LinearInterpolator({ transitionProps: ['target', 'zoom'] }),
        transitionEasing: easeInOutQuad
      };
      deckgl.setProps({ viewState: VS });
    };
    const path = await algo.run(startVal, endVal, { onEndpoints, onProgress: markSearchProgress });
    // Wall clock minus the artificial animation-pacing delay -- see pacedDelayMs --
    // is the actual algorithm time, which is what the timing toast reports.
    const elapsedMs = Math.max(0, performance.now() - t0 - pacedDelayMs);

    if (path && path.length > 0) {
      console.log("Path discovered:", path);
      currentRoutePath = path;
      const { orderedIndices, titleToIdx } = await updateRouteIndices(path);
      const hopIndices = path.map(t => titleToIdx.get(t) ?? -1);

      // Route generation guard: if the user fires another search before this one's
      // context fetches finish, those late results must not overwrite the newer
      // route's DOM. Each hop's async update checks this before touching anything.
      const myRouteGen = ++routeRequestGen;

      if (routeResult && routeTextPath) {
        routeResult.style.display = 'block';
        // Route (titles + category color, matching the graph's destination-category
        // coloring rule) renders immediately; each hop's link-context excerpt is
        // fetched one at a time afterward (see below) and streamed in as it arrives,
        // rather than blocking the whole list on every hop's query up front.
        routeTextPath.innerHTML = path.map((nodeId, idx) => {
          const nodeIdx = titleToIdx.get(nodeId);
          const hasCat = nodeIdx !== undefined && nodeIdx >= 0 && nodeIdx < N;
          const tagCol = hasCat && CAT[cat[nodeIdx]] ? `rgb(${CAT[cat[nodeIdx]].join(',')})` : '#9d9894';
          const catName = hasCat ? (CATNAME[cat[nodeIdx]] || 'Other') : '';
          const contextSlot = idx > 0
            ? `<div id="route-ctx-${idx}" style="padding-bottom:8px; display:flex; align-items:center; gap:6px;">
                 <div class="mini-loader" style="display:block; width:10px; height:10px; border-color:rgba(255,255,255,0.1); border-top-color:var(--color-bone); border-width:1.5px;"></div>
                 <span style="color:var(--color-ash); font-size:12px;">Searching context…</span>
               </div>`
            : '';
          return `<div style="border-bottom:1px solid var(--color-graphite); padding:6px 0;">
               <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                 <span style="cursor:pointer; color:${tagCol}; font-weight:600;" onclick="window.selectNodeById('${nodeId}')">${idx + 1}. ${nodeId}</span>
                 ${catName ? `<span style="flex-shrink:0; font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; border:1px solid ${tagCol}; color:${tagCol};">${catName}</span>` : ''}
               </div>
               ${contextSlot}
             </div>`;
        }).join('');
      }

      // Animate route discovery if we mapped the indices
      if (orderedIndices.length > 0) {
        animateRoute(orderedIndices);
      }

      // Fetch each hop's link-context excerpt one at a time, streaming each into the DOM
      // as it lands.
      //
      // This used to fan all hops out at once with Promise.all. That was the reason
      // context "loaded then gave up" on every hop but the first: the query worker runs
      // a single statement at a time, but each hop's timeout starts the moment its
      // promise is created. Hop 2 spent its whole budget queued behind hop 1 and was
      // reported as "no context" without the database ever having looked at it. Awaiting
      // in sequence means a hop's clock only starts when its query can actually run.
      (async () => {
        for (let hopIdx = 1; hopIdx < path.length; hopIdx++) {
          if (myRouteGen !== routeRequestGen) return; // superseded by a newer search
          const context = await fetchSingleLinkContext(path[hopIdx - 1], path[hopIdx]);
          if (myRouteGen !== routeRequestGen) return;
          const slot = document.getElementById(`route-ctx-${hopIdx}`);
          if (!slot) continue;
          if (context) {
            const tgtIdx = hopIndices[hopIdx];
            const tgtColor = tgtIdx >= 0 && CAT[cat[tgtIdx]] ? `rgb(${CAT[cat[tgtIdx]].join(',')})` : '#9d9894';
            const highlightedText = cleanAndHighlightWikiText(context, path[hopIdx], tgtColor);
            slot.outerHTML = `<div id="route-ctx-${hopIdx}" class="connection-context" onclick="this.classList.toggle('expanded')" style="padding-bottom:8px; cursor:pointer;">${highlightedText}</div>`;
          } else {
            slot.remove();
          }
        }
      })();

      showRouteTiming(`${algo.label} · found in ${formatDuration(elapsedMs)} · ${path.length} hop${path.length === 1 ? '' : 's'}`);
    } else if (mode === 'randomwalk') {
      // A random walk giving up is never evidence no path exists -- it only ever
      // sees one arbitrary stumble through the graph, unlike the other six modes,
      // which are exhaustive. Wording it the same as "no path found" would claim
      // something this algorithm can't actually back up.
      showRouteTiming(`Random walk gave up after ${formatDuration(elapsedMs)} — doesn't mean no path exists`);
    } else {
      showRouteTiming(`${algo.label} · no path found · searched for ${formatDuration(elapsedMs)}`);
      alert(`No link path found between these articles using ${algo.label}.`);
    }
  } catch (e) {
    if (e?.name === 'QueryEvicted') {
      console.warn("Pathfinder superseded by a newer request.");
    } else {
      console.error("Pathfinder failed:", e);
      alert("Pathfinder error: " + e.message);
    }
  } finally {
    searchActive = false;
    findRouteBtn.disabled = false;
    if (routeLoader) routeLoader.style.display = 'none';
  }
}

  if (findRouteBtn && routeStart && routeEnd) {
    findRouteBtn.onclick = () => executePathfinder(routeAlgoSelect ? routeAlgoSelect.value : 'bidirectional');
  }

  // Route finder's random-pick button: two distinct uniformly-random titles, dropped
  // into the inputs without auto-running the search (matching the search box's
  // random button, which also just fills the field rather than acting on it).
  const routeRandomBtn = $('route-random-btn');
  if (routeRandomBtn && routeStart && routeEnd) {
    routeRandomBtn.onclick = () => {
      if (!titleOffsets) return; // titles.bin hasn't loaded yet
      const a = randomTitle();
      let b = randomTitle();
      while (b === a) b = randomTitle();
      routeStart.value = a;
      routeEnd.value = b;
    };
  }

function cull(vs){
  const [W,H]=dims(); const s=Math.pow(2,vs.zoom); const hw=(W/2)/s*1.4, hh=(H/2)/s*1.4;
  const x0=vs.target[0]-hw, x1=vs.target[0]+hw, y0=vs.target[1]-hh, y1=vs.target[1]+hh;
  const cx0=clamp((x0-minx)/cw|0,0,G-1), cx1=clamp((x1-minx)/cw|0,0,G-1);
  const cy0=clamp((y0-miny)/ch|0,0,G-1), cy1=clamp((y1-miny)/ch|0,0,G-1);
  // hideAllNodes only suppresses the node dots at render time (see the ScatterplotLayer
  // below) — the loop itself always runs at full nodeBudget so the background edges,
  // which are collected in this same pass per node, aren't affected by it.
  const M=(cx1-cx0+1)*(cy1-cy0+1), perCap=Math.max(1,Math.ceil(nodeBudget/M));
  let v=0, ec=0;

  // Shared by both the per-cell budget loop below and the "must-render" pass after
  // it. Pulled out so a node's color/size logic lives in exactly one place, however
  // it ends up getting drawn.
  const colorAndPlace = (i) => {
    sPos[v*2]=px[i]; sPos[v*2+1]=py[i]; sIdx[v]=i;

    if (searchActive) {
      // The search-in-progress signal is the edges layer (searchFrontierEdges,
      // below) lighting up as they're examined, not the nodes -- every node here
      // is either an orientation marker (start/end) or dimmed out of the way so
      // the edge trail actually reads.
      if (i === searchStartIdx) {
        // Start node: bright lime green, enlarged
        sRad[v] = rad[i] * 4.0;
        sCol[v*4]=80; sCol[v*4+1]=255; sCol[v*4+2]=80; sCol[v*4+3]=255;
      } else if (i === searchEndIdx) {
        // End node: bright red, enlarged
        sRad[v] = rad[i] * 4.0;
        sCol[v*4]=255; sCol[v*4+1]=80; sCol[v*4+2]=80; sCol[v*4+3]=255;
      } else {
        // Everything else, visited or not: dimmed grey
        sRad[v] = rad[i] * 0.5;
        sCol[v*4]=70; sCol[v*4+1]=70; sCol[v*4+2]=70; sCol[v*4+3]=20;
      }
    // A route and a selected node are independent pieces of state and can both be
    // live at once, so this is one flat priority chain rather than a route branch
    // that swallows every other case. (It used to be the latter, which is why
    // clicking a node had to wipe the route to be visible at all.)
    } else if (i === selectedNodeIdx) {
      // Clicked node size and color (hot pink/lilac). Ranked above the route so a
      // node you click *on* the route still reads as the thing you just clicked.
      sRad[v] = rad[i] * 3.0;
      sCol[v*4]=245; sCol[v*4+1]=100; sCol[v*4+2]=150; sCol[v*4+3]=255;
    } else if (currentRouteIndices.has(i)) {
      // Path highlighted nodes size and color (yellow)
      sRad[v] = rad[i] * 3.5;
      sCol[v*4]=232; sCol[v*4+1]=184; sCol[v*4+2]=64; sCol[v*4+3]=255;
    } else if (selectedNodeIdx !== -1 && selectedConnections.has(i)) {
      // Connected neighbor! Bright and slightly enlarged
      sRad[v] = rad[i] * 1.3;
      sCol[v*4]=col[i*4]; sCol[v*4+1]=col[i*4+1]; sCol[v*4+2]=col[i*4+2]; sCol[v*4+3]=255;
    } else if (currentRouteIndices.size > 0 || selectedNodeIdx !== -1) {
      // Off-route / unconnected — dimmed much further than the old 70: barely-there
      // presence so the route and selection read as the clear focus, while still
      // keeping a faint trace of the rest of the galaxy (alpha 0 would be the
      // "everything else disappeared" problem from before).
      sRad[v] = rad[i] * 0.4;
      sCol[v*4]=col[i*4];sCol[v*4+1]=col[i*4+1];sCol[v*4+2]=col[i*4+2];sCol[v*4+3]=dimAlpha;
    } else {
      sRad[v]=rad[i];
      sCol[v*4]=col[i*4];sCol[v*4+1]=col[i*4+1];sCol[v*4+2]=col[i*4+2];sCol[v*4+3]=255;
    }

    const tx=et[i*2];
    if(tx===tx){
      eS[ec*2]=px[i];eS[ec*2+1]=py[i];
      eT[ec*2]=tx;eT[ec*2+1]=et[i*2+1];
      ec++;
    } // NaN check
    v++;
  };

  for(let cy=cy0;cy<=cy1&&v<nodeBudget;cy++){ const base=cy*G;
    for(let cx=cx0;cx<=cx1&&v<nodeBudget;cx++){ const c=base+cx,s0=start[c],e0=start[c+1],take=Math.min(e0-s0,perCap);
      for(let k=0;k<take&&v<nodeBudget;k++){ const i=order[s0+k];
        if (hiddenCategories.size > 0 && hiddenCategories.has(cat[i])) continue; // legend eye-toggle
        colorAndPlace(i);
      }
    }
  }

  // The per-cell budget above picks nodes by raw index order within each cell --
  // arbitrary with respect to importance, not sorted by degree or anything else.
  // An obscure low-degree node (exactly the kind a search or route is likely to
  // pass through) can lose that lottery entirely and never get drawn, which meant
  // its start/end/selected/route-node color logic above never ran either, no
  // matter how bright the color was supposed to be. These are orientation anchors,
  // not decoration, so guarantee them a slot regardless of what the grid pass did.
  const mustInclude = [];
  if (searchActive) {
    if (searchStartIdx !== -1) mustInclude.push(searchStartIdx);
    if (searchEndIdx !== -1) mustInclude.push(searchEndIdx);
  }
  if (selectedNodeIdx !== -1) mustInclude.push(selectedNodeIdx);
  if (currentRouteIndices.size > 0) for (const idx of currentRouteIndices) mustInclude.push(idx);

  if (mustInclude.length > 0 && v < BUDGET) {
    const included = new Set();
    for (let q = 0; q < v; q++) included.add(sIdx[q]);
    for (const idx of mustInclude) {
      if (v >= BUDGET) break;
      if (included.has(idx)) continue;
      included.add(idx);
      colorAndPlace(idx);
    }
  }

  // Render path lines, one color per hop keyed to the destination node's category —
  // same rule as the selected-node fan-out below, so the whole app colors edges
  // consistently by where they lead rather than a single flat highlight color.
  const routeIndices = Array.from(currentRouteIndices);
  const pathES = [];
  const pathET = [];
  const pathCol = [];
  for (let p = 0; p < routeIndices.length - 1; p++) {
    const srcIdx = routeIndices[p];
    const tgtIdx = routeIndices[p+1];
    pathES.push(px[srcIdx], py[srcIdx]);
    pathET.push(px[tgtIdx], py[tgtIdx]);
    const c = CAT[cat[tgtIdx]] || [232, 184, 64];
    pathCol.push(c[0], c[1], c[2], 255);
  }

  // Build selected node highlighted links, colored per-line by the destination
  // node's category so the fan-out reads as "what kinds of topics this connects
  // to" at a glance instead of one flat highlight color.
  const selectedLinksES = [];
  const selectedLinksET = [];
  const selectedLinksCol = [];
  if (selectedNodeIdx !== -1 && selectedConnections.size > 1) {
    const sx = px[selectedNodeIdx], sy = py[selectedNodeIdx];
    selectedConnections.forEach(tgtIdx => {
      if (tgtIdx !== selectedNodeIdx && tgtIdx >= 0 && tgtIdx < N) {
        selectedLinksES.push(sx, sy);
        selectedLinksET.push(px[tgtIdx], py[tgtIdx]);
        const c = CAT[cat[tgtIdx]] || [232, 184, 64];
        selectedLinksCol.push(c[0], c[1], c[2], 220);
      }
    });
  }

  lastVisibleCount = v;

  const staticLayers = [
    // hideAllNodes only zeroes this layer's data length — the edge layers below are
    // built from the same v/ec counts regardless, so the link structure stays visible.
    new ScatterplotLayer({ id:'nodes', data:{length:hideAllNodes?0:v,attributes:{getPosition:{value:sPos.subarray(0,v*2),size:2},getRadius:{value:sRad.subarray(0,v),size:1},getFillColor:{value:sCol.subarray(0,v*4),size:4,normalized:true}}},
        radiusUnits:'pixels', radiusMinPixels:2.5, radiusMaxPixels:12.0, radiusScale:nodeSizeScale, opacity:0.95, pickable:true, autoHighlight:true, highlightColor:[232,184,64,200] })
  ];

  if (selectedLinksES.length > 0) {
    staticLayers.push(
      new LineLayer({ id:'selected-links', data:{length:selectedLinksES.length/2,attributes:{
          getSourcePosition:{value:new Float32Array(selectedLinksES),size:2},
          getTargetPosition:{value:new Float32Array(selectedLinksET),size:2},
          getColor:{value:new Uint8Array(selectedLinksCol),size:4,normalized:true}
        }},
        widthUnits: 'pixels',
        getWidth: 1.8
      })
    );
  }

  if (routeIndices.length > 1) {
    staticLayers.unshift(
      new LineLayer({ id:'route-path', data:{length:routeIndices.length - 1,attributes:{
          getSourcePosition:{value:new Float32Array(pathES),size:2},
          getTargetPosition:{value:new Float32Array(pathET),size:2},
          getColor:{value:new Uint8Array(pathCol),size:4,normalized:true}
        }},
        widthUnits:'pixels', getWidth:2.5 })
    );
  }

  // Build search traversal edge layer
  let searchEdgeLayer = null;
  if (searchActive && searchFrontierEdges.length > 0) {
    const edgeCount = searchFrontierEdges.length / 2;
    const sePos = new Float32Array(edgeCount * 4);
    for (let e = 0; e < edgeCount; e++) {
      const a = searchFrontierEdges[e * 2], b = searchFrontierEdges[e * 2 + 1];
      sePos[e * 4] = px[a]; sePos[e * 4 + 1] = py[a];
      sePos[e * 4 + 2] = px[b]; sePos[e * 4 + 3] = py[b];
    }
    searchEdgeLayer = new LineLayer({
      id: 'search-edges',
      data: { length: edgeCount, attributes: {
        getSourcePosition: { value: sePos, size: 2, stride: 4, offset: 0 },
        getTargetPosition: { value: sePos, size: 2, stride: 4, offset: 2 }
      }},
      // Bright + fully opaque: this line IS the "every edge looked at" trail, so it
      // needs to read clearly against the (deliberately near-invisible, see the
      // background 'links' layer below) rest of the graph while a search is active.
      getColor: [80, 200, 255, 220],
      getWidth: 1.8,
      widthUnits: 'pixels'
    });
  }

  // Always render static layers and background links immediately first
    deckgl.setProps({
      layers: [
        new LineLayer({ id:'links', data:{length:ec,attributes:{getSourcePosition:{value:eS.subarray(0,ec*2),size:2},getTargetPosition:{value:eT.subarray(0,ec*2),size:2}}},
          getColor: (()=>{ const base=searchActive?[40,40,40,8]:(selectedNodeIdx!==-1?[157,152,148,8]:[157,152,148,30]); return [base[0],base[1],base[2],Math.round(base[3]*edgeOpacity)]; })(), widthUnits:'pixels', getWidth:0.6 }),
        ...(searchEdgeLayer ? [searchEdgeLayer] : []),
        ...staticLayers
      ]
    });

    // Dynamic max-zoom edge query
    // Dynamic max-zoom edge query
    const maxZoomThreshold = 2.0;

    if (cullTimeout) clearTimeout(cullTimeout);

    if (vs.zoom >= maxZoomThreshold && v <= 80 && db) {
      cullTimeout = setTimeout(async () => {
        const queryId = ++currentCullQueryId;
        const visibleIdxs = [];
        for (let k = 0; k < v; k++) {
          visibleIdxs.push(sIdx[k]);
        }
        if (visibleIdxs.length === 0) return;

        try {
          const placeholders = visibleIdxs.map(() => '?').join(',');
          const links = await dbQuery(
            `SELECT source_idx AS srcIdx, target_idx AS tgtIdx 
             FROM links 
             WHERE source_idx IN (${placeholders}) 
                OR target_idx IN (${placeholders})`,
            [...visibleIdxs, ...visibleIdxs],
            PRIORITY_CULL,
            'cull'
          ) || [];
          
          if (queryId !== currentCullQueryId) return; // ignore stale query callback
          
          const dynamicES = [];
          const dynamicET = [];
          const seenEdges = new Set();
          
          for (const l of links) {
            const srcIdx = l.srcIdx;
            const tgtIdx = l.tgtIdx;
            if (srcIdx === null || tgtIdx === null || srcIdx < 0 || srcIdx >= N || tgtIdx < 0 || tgtIdx >= N) continue;
            
            const edgeKey = srcIdx < tgtIdx ? `${srcIdx}_${tgtIdx}` : `${tgtIdx}_${srcIdx}`;
            if (seenEdges.has(edgeKey)) continue;
            seenEdges.add(edgeKey);
            
            dynamicES.push(px[srcIdx], py[srcIdx]);
            dynamicET.push(px[tgtIdx], py[tgtIdx]);
          }
          
          // Merge background single-strength links and new dynamic max-zoom links
          const combinedES = new Float32Array(ec * 2 + dynamicES.length);
          combinedES.set(eS.subarray(0, ec * 2));
          combinedES.set(dynamicES, ec * 2);
          
          const combinedET = new Float32Array(ec * 2 + dynamicET.length);
          combinedET.set(eT.subarray(0, ec * 2));
          combinedET.set(dynamicET, ec * 2);
          
          deckgl.setProps({
            layers: [
              new LineLayer({ id:'links', data:{length:ec + seenEdges.size,attributes:{getSourcePosition:{value:combinedES,size:2},getTargetPosition:{value:combinedET,size:2}}},
                getColor: selectedNodeIdx !== -1 ? [157,152,148,8] : [157,152,148,36], widthUnits:'pixels', getWidth:0.6 }),
              ...staticLayers
            ]
          });
        } catch (err) {
          console.error("Dynamic max zoom edges query failed:", err);
        }
      }, 800);
    }
  } // close cull

  // Fit-to-bounds, not a guessed constant: minx is -bound and the grid is square
  // and centered on the origin (see startVisualization's `bound=Math.max(maxAbsX,
  // maxAbsY)*1.02`), so -minx*2 is the true diameter of the actual laid-out graph.
  // Dividing by min(w,h) already makes this device/viewport-size aware; deriving
  // the diameter from the real data means the start zoom is always "every node
  // just fits," not a fixed number tuned for one screen size and wrong on others.
  (function fit(){ const [w,h]=dims(); if(w>10){
      const diameter = -minx * 2;
      const margin = 1.08; // small breathing room so the outermost nodes aren't flush against the edge
      VS={target:[0,0,0],zoom:Math.log2(Math.min(w,h)/(diameter*margin))}; deckgl.setProps({viewState:VS}); cull(VS);
      const ls=$('loading-screen'); if(ls){ ls.style.transition='opacity .5s'; ls.style.opacity='0'; setTimeout(()=>ls.style.display='none',500); }
    } else requestAnimationFrame(fit); })();

  // Build the title search index after the page is already interactive — sorting
  // 5.48M titles is a real chunk of work, and search falls back to the DB query path
  // until it's ready, so there's no reason to make the initial paint wait on it.
  setTimeout(buildTitleSearchIndex, 0);

  // ---------- Test / debug surface (see tests/harness.js) ----------
  // Everything the harness needs that would otherwise be trapped in run()'s closure.
  window.__wg = {
    get ready() { return !!deckgl; },
    get db() { return db; },
    get N() { return N; },
    px, py, deg, cat,
    deckgl,
    cull: (vs) => cull(vs || VS),
    get viewState() { return VS; },
    setViewState(vs) { VS = vs; deckgl.setProps({ viewState: VS }); cull(VS); },
    get visibleCount() { return lastVisibleCount; },
    get selectedNodeIdx() { return selectedNodeIdx; },
    get clickHistory() { return clickHistory.slice(); },
    dbQuery,
    pathfinders: {
      astar: (a, b, opts) => runAStarPathfinder(a, b, opts),
      bfs: (a, b, opts) => runBidirectionalBFS(a, b, opts),
      simpleBfs: (a, b, opts) => runSimpleBFS(a, b, opts),
      simpleDfs: (a, b, opts) => runSimpleDFS(a, b, opts),
      greedy: (a, b, opts) => runGreedyBestFirst(a, b, opts),
      dijkstra: (a, b, opts) => runDijkstraWeighted(a, b, opts),
      randomWalk: (a, b, opts) => runRandomWalk(a, b, opts)
    },
    stats: () => JSON.parse(JSON.stringify(qStats)),
    resetStats() {
      qStats.count = 0; qStats.evicted = 0; qStats.failed = 0;
      qStats.totalMs = 0; qStats.maxMs = 0; qStats.byPriority = {}; qStats.log.length = 0;
    }
  };
  console.log('__wg test surface ready');

} // close run()



// Bidirectional BFS pathfinding (Highly Optimized via Integer Indices)

// ---------------------------------------------------------------------------
// Neighbour access
//
// Every hop is an HTTPS range request into a 25GB remote SQLite file, so the
// only number that matters is "how many round trips", not "how many nodes".
// Two things follow: expand a whole BFS level in ONE query, and never fetch the
// same node's adjacency twice within a session.
// ---------------------------------------------------------------------------

const EMPTY_NEIGHBOURS = new Int32Array(0);
// Separate caches per direction — a node's out-neighbors and in-neighbors are
// different sets (see build_adjacency_csr.py), so they can't share one cache
// keyed only by node index without conflating "links to" with "linked from".
const neighbourCacheOut = new Map(); // nodeIdx -> Int32Array of nodes this one links TO
const neighbourCacheIn = new Map();  // nodeIdx -> Int32Array of nodes that link TO this one
const NEIGHBOUR_CACHE_MAX = 200000;

// Fetch adjacency for a batch of node indices, returning a Map idx -> (typed array).
// `direction` MUST match what the caller actually needs: 'out' for "articles this
// node links to" (real, clickable links FROM it), 'in' for "articles that link to
// this node" (real, clickable links TO it, i.e. this node's own page doesn't
// contain them). Bidirectional pathfinding needs both — see runBidirectionalBFS —
// but never the same direction for both search halves, and never a direction
// picked implicitly, which is what the single old undirected CSR forced.
//
// The in-memory CSR (adjacency_csr.bin / adjacency_csr_rev.bin) covers every node
// in the layout, so this is the hot path — a slice of a shared array, no network.
// The DB path below only ever runs for indices outside the CSR's range (shouldn't
// happen for real usage, since every renderable node has a position and thus a CSR
// entry) or when the relevant .bin failed to load, in which case pathfinding
// transparently degrades to the original query-per-frontier-level behavior.
async function fetchNeighbours(indices, priority = PRIORITY_CLICK, direction = 'out') {
  const offs = direction === 'in' ? csrOffsetsRev : csrOffsets;
  const nbrs = direction === 'in' ? csrNeighborsRev : csrNeighbors;
  if (offs) {
    const out = new Map();
    const dbNeeded = [];
    for (const i of indices) {
      if (i < csrN) {
        out.set(i, nbrs.subarray(offs[i], offs[i + 1]));
      } else {
        dbNeeded.push(i);
      }
    }
    if (dbNeeded.length === 0) return out;
    const dbResults = await fetchNeighboursFromDb(dbNeeded, priority, direction);
    for (const [i, arr] of dbResults) out.set(i, arr);
    return out;
  }
  return fetchNeighboursFromDb(indices, priority, direction);
}

async function fetchNeighboursFromDb(indices, priority = PRIORITY_CLICK, direction = 'out') {
  const neighbourCache = direction === 'in' ? neighbourCacheIn : neighbourCacheOut;
  const missing = indices.filter(i => !neighbourCache.has(i));

  if (missing.length > 0) {
    // 1. Map layout indices to titles
    const titleToLayoutIdx = new Map();
    const titles = [];
    for (const idx of missing) {
      const title = titleOf(idx);
      if (title) {
        titles.push(title);
        titleToLayoutIdx.set(title, idx);
      }
    }

    if (titles.length > 0) {
      const ph = titles.map(() => '?').join(',');
      try {
        // 2. Resolve database indices (rowid - 1) for these titles
        const nodeRows = await dbQuery(
          `SELECT id, rowid - 1 AS idx_db FROM nodes WHERE id IN (${ph})`,
          titles,
          priority
        ) || [];

        const dbIdxToLayoutIdx = new Map();
        const dbIndices = [];
        for (const row of nodeRows) {
          const lIdx = titleToLayoutIdx.get(row.id);
          if (lIdx !== undefined) {
            dbIdxToLayoutIdx.set(row.idx_db, lIdx);
            dbIndices.push(row.idx_db);
          }
        }

        if (dbIndices.length > 0) {
          const phDb = dbIndices.map(() => '?').join(',');
          // 3. Query links using database indices — single-column predicate only,
          // matching whichever direction was asked for. A two-column OR/UNION
          // across source_idx and target_idx here would be the same trap
          // documented elsewhere in this file (SQLite picks the wrong index over
          // httpvfs and the query never finishes) — direction is decided by which
          // single column we filter on, not by combining both.
          const col = direction === 'in' ? 'target_idx' : 'source_idx';
          const otherField = direction === 'in' ? 'source' : 'target';
          const linkRows = await dbQuery(
            `SELECT ${col} AS anchor_idx, ${otherField} AS neighbor_title FROM links
             WHERE ${col} IN (${phDb}) AND context IS NOT NULL AND context != ""`,
            dbIndices,
            priority
          ) || [];

          // Initialize adjacency list map for layout indices
          const acc = new Map();
          for (const lIdx of missing) acc.set(lIdx, []);

          // Group neighbors by layout index
          for (const r of linkRows) {
            const anchorLIdx = dbIdxToLayoutIdx.get(r.anchor_idx);
            if (anchorLIdx === undefined) continue;
            const neighborLIdx = findTitleIndexInTitlesBin(r.neighbor_title);
            if (neighborLIdx !== -1) acc.get(anchorLIdx).push(neighborLIdx);
          }

          // Cache the results
          if (neighbourCache.size > NEIGHBOUR_CACHE_MAX) neighbourCache.clear();
          for (const [lIdx, arr] of acc) {
            const uniqueNbrs = Array.from(new Set(arr));
            neighbourCache.set(lIdx, Int32Array.from(uniqueNbrs));
          }
        }
      } catch (err) {
        console.error("fetchNeighboursFromDb failed:", err);
      }
    }

    // Fill in default empty array for any missing indices that failed to resolve
    for (const i of missing) {
      if (!neighbourCache.has(i)) {
        neighbourCache.set(i, EMPTY_NEIGHBOURS);
      }
    }
  }

  const out = new Map();
  for (const i of indices) {
    out.set(i, neighbourCache.get(i) || EMPTY_NEIGHBOURS);
  }
  return out;
}

// Order a frontier hub-first. The link graph is scale-free with an effective
// diameter around 4, and high-degree articles are what make that true — expanding
// them first is what lets the two searches meet within a couple of levels instead
// of grinding through millions of leaf articles. `deg` is already resident in memory
// from viewer_full.bin, so this ranking costs nothing.
function orderByDegreeDesc(indices, degrees) {
  return indices.slice().sort((a, b) => (degrees[b] || 0) - (degrees[a] || 0));
}

// Binary min-heap ordered by `.f` -- shared by the three heap-guided pathfinders
// below (A*, greedy best-first, Dijkstra).
class MinHeap {
  constructor() { this.heap = []; }
  push(node) {
    this.heap.push(node);
    this._siftUp(this.heap.length - 1);
  }
  pop() {
    if (this.heap.length === 0) return null;
    const top = this.heap[0];
    const end = this.heap.pop();
    if (this.heap.length > 0) {
      this.heap[0] = end;
      this._siftDown(0);
    }
    return top;
  }
  _siftUp(idx) {
    let parent;
    while (idx > 0 && (parent = ((idx - 1) >> 1), this.heap[idx].f < this.heap[parent].f)) {
      [this.heap[idx], this.heap[parent]] = [this.heap[parent], this.heap[idx]];
      idx = parent;
    }
  }
  _siftDown(idx) {
    const length = this.heap.length;
    while (true) {
      let left = idx * 2 + 1;
      let right = left + 1;
      let smallest = idx;
      if (left < length && this.heap[left].f < this.heap[smallest].f) smallest = left;
      if (right < length && this.heap[right].f < this.heap[smallest].f) smallest = right;
      if (smallest === idx) break;
      [this.heap[idx], this.heap[smallest]] = [this.heap[smallest], this.heap[idx]];
      idx = smallest;
    }
  }
  size() { return this.heap.length; }
}

// Shared skeleton for the three heap-guided pathfinders (A*, greedy best-first,
// Dijkstra/degree-weighted): pop the lowest-f node, expand its real out-links,
// push each newly-improved neighbor. `stepCost(fromIdx, toIdx)` and
// `heuristic(idx, endIdx)` are what actually distinguish the three -- see their
// call sites below. Same onProgress/onEndpoints contract, and the same
// once-per-popped-node batching, as every other pathfinder here (see
// runSimpleDFS's comment on why per-edge batching is what made DFS hang).
async function runHeapPathfinder(startId, endId, stepCost, heuristic, opts = {}) {
  if (!db) return null;
  const onProgress = opts.onProgress;
  try {
    const startIdx = findTitleIndexInTitlesBin(startId);
    const endIdx = findTitleIndexInTitlesBin(endId);
    if (startIdx === -1 || endIdx === -1) return null;
    if (startIdx === endIdx) return [startId];
    opts.onEndpoints?.(startIdx, endIdx);

    const open = new MinHeap();
    const gScore = new Map([[startIdx, 0]]);
    const preds = new Map([[startIdx, null]]);
    const closed = new Set();
    open.push({ idx: startIdx, f: heuristic(startIdx, endIdx) });

    while (open.size() > 0) {
      const { idx: curr } = open.pop();
      if (closed.has(curr)) continue; // stale duplicate heap entry from a since-improved node
      closed.add(curr);

      if (curr === endIdx) {
        const pathIndices = [];
        let cur = curr;
        while (cur !== null && cur !== undefined) { pathIndices.push(cur); cur = preds.get(cur); }
        pathIndices.reverse();
        return await buildPathFromSimpleIndices(pathIndices);
      }

      const adjacency = await fetchNeighbours([curr], PRIORITY_CLICK, 'out');
      const touchedEdges = [];
      for (const neigh of adjacency.get(curr) || []) {
        if (closed.has(neigh)) continue;
        const tentativeG = gScore.get(curr) + stepCost(curr, neigh);
        if (tentativeG < (gScore.get(neigh) ?? Infinity)) {
          gScore.set(neigh, tentativeG);
          preds.set(neigh, curr);
          open.push({ idx: neigh, f: tentativeG + heuristic(neigh, endIdx) });
          touchedEdges.push([curr, neigh]);
        }
      }
      if (touchedEdges.length > 0) await onProgress?.(touchedEdges);
    }
  } catch (e) {
    if (e?.name !== 'QueryEvicted') console.error("Heap pathfinder failed:", e);
  }
  return null;
}

const layoutDistance = (a, b) => Math.hypot(px[a] - px[b], py[a] - py[b]);

// A*: real graph-hop cost (1 per edge) plus straight-line layout distance to
// the target as the heuristic. Layout distance is admissible in spirit (nodes
// close on screen tend to be few hops apart, since the force layout pulls
// linked nodes together) but not a strict lower bound, so this isn't provably
// optimal -- same caveat the original implementation this replaces carried.
async function runAStarPathfinder(startId, endId, opts = {}) {
  return runHeapPathfinder(startId, endId, () => 1, layoutDistance, opts);
}

// Greedy best-first: ignores accumulated path cost entirely (stepCost always
// 0), always expands whichever open node LOOKS closest to the target on
// screen. Fast, makes a visible beeline -- but unlike every other pathfinder
// here, gives no guarantee of a short path, or even noticing a shorter one
// existed.
async function runGreedyBestFirst(startId, endId, opts = {}) {
  return runHeapPathfinder(startId, endId, () => 0, layoutDistance, opts);
}

// Dijkstra with a degree-weighted step cost instead of a flat 1: entering a
// mega-hub like "United States" costs 1 + log1p(itsDegree)*0.5 instead of
// just 1, so the cheapest path tends to route through topically-related
// articles rather than the same handful of hub pages every other algorithm
// here funnels through. No heuristic (always 0), so this stays exhaustive and
// optimal for that cost function -- Dijkstra's algorithm proper.
async function runDijkstraWeighted(startId, endId, opts = {}) {
  const cost = (a, b) => 1 + Math.log1p(nodeDegrees ? (nodeDegrees[b] || 0) : 0) * 0.5;
  return runHeapPathfinder(startId, endId, cost, () => 0, opts);
}

// Random walk: no heuristic, no memory of a better path already found -- just
// pick a uniformly random real out-link and go, repeat. Genuinely bad as a
// pathfinder (can loop indefinitely, can dead-end in a sink with no
// out-links), which is the point: a foil that makes the other six look as
// deliberate as they are. Capped at maxSteps since, unlike the exhaustive
// algorithms above, a stuck walk has no natural termination -- giving up here
// is never evidence that no path exists, only that this particular random
// stumble didn't find one (executePathfinder words the failure accordingly).
async function runRandomWalk(startId, endId, opts = {}) {
  if (!db) return null;
  const maxSteps = opts.maxSteps ?? 20000;
  const onProgress = opts.onProgress;
  try {
    const startIdx = findTitleIndexInTitlesBin(startId);
    const endIdx = findTitleIndexInTitlesBin(endId);
    if (startIdx === -1 || endIdx === -1) return null;
    if (startIdx === endIdx) return [startId];
    opts.onEndpoints?.(startIdx, endIdx);

    const pathIndices = [startIdx];
    let curr = startIdx;
    for (let step = 0; step < maxSteps; step++) {
      const adjacency = await fetchNeighbours([curr], PRIORITY_CLICK, 'out');
      const neighbours = adjacency.get(curr) || [];
      if (neighbours.length === 0) break; // dead end -- a sink page with no out-links
      const next = neighbours[Math.floor(Math.random() * neighbours.length)];
      await onProgress?.([[curr, next]]);
      pathIndices.push(next);
      curr = next;
      if (curr === endIdx) return await buildPathFromSimpleIndices(pathIndices);
    }
  } catch (e) {
    if (e?.name !== 'QueryEvicted') console.error("Random walk failed:", e);
  }
  return null;
}

async function runBidirectionalBFS(startId, endId, opts = {}) {
  if (!db) return null;
  // No artificial depth cap: a search should only ever report "no path" because the
  // graph genuinely has none (both frontiers exhausted, see the break below), not
  // because an arbitrary round limit was hit while reachable graph still remained.
  const maxDepth = opts.maxDepth ?? Infinity;
  const frontierCap = opts.frontierCap ?? 220; // nodes expanded per level (1 query)
  const onProgress = opts.onProgress;

  try {
    const startIdx = findTitleIndexInTitlesBin(startId);
    const endIdx = findTitleIndexInTitlesBin(endId);
    if (startIdx === -1 || endIdx === -1) return null;
    if (startIdx === endIdx) return [startId];
    opts.onEndpoints?.(startIdx, endIdx);

    const startPreds = new Map([[startIdx, null]]);
    const endPreds = new Map([[endIdx, null]]);
    let startFrontier = [startIdx];
    let endFrontier = [endIdx];

    for (let depth = 0; depth < maxDepth; depth++) {
      if (startFrontier.length === 0 || endFrontier.length === 0) break;

      // Always expand whichever side is cheaper — this is what keeps the search
      // O(b^(d/2)) instead of O(b^d).
      const expandStart = startFrontier.length <= endFrontier.length;
      const frontier = expandStart ? startFrontier : endFrontier;
      const ownPreds = expandStart ? startPreds : endPreds;
      const otherPreds = expandStart ? endPreds : startPreds;

      const ordered = orderByDegreeDesc(frontier, nodeDegrees);
      const batch = ordered.slice(0, frontierCap);
      const rest = ordered.slice(frontierCap);

      // Direction is NOT optional here. The start-side search is walking forward
      // from the source, so it must only follow real out-links ('out') — that's
      // what makes the eventual path something you could actually click through.
      // The end-side search is walking backward from the target: it needs to find
      // "who has an out-link reaching a node already in my frontier", which is
      // exactly the in-link direction ('in'). Using 'out' for both (or a single
      // undirected graph, as this used to be) lets the backward half silently walk
      // in-links as if they were out-links, producing a hop no one could click.
      const adjacency = await fetchNeighbours(batch, PRIORITY_CLICK, expandStart ? 'out' : 'in'); // one round trip

      const next = [];
      const touchedEdges = [];
      let meetPoint = null;
      for (const curr of batch) {
        for (const neigh of adjacency.get(curr)) {
          if (ownPreds.has(neigh)) continue;
          ownPreds.set(neigh, curr);
          next.push(neigh);
          touchedEdges.push([curr, neigh]);
          // The two half-searches touched: startPreds walks back to the source and
          // endPreds walks forward to the target, whichever side just added `neigh`.
          if (otherPreds.has(neigh)) { meetPoint = neigh; break; }
        }
        if (meetPoint !== null) break;
      }

      if (touchedEdges.length > 0) await onProgress?.(touchedEdges);
      if (meetPoint !== null) return await buildPathFromIndices(startPreds, endPreds, meetPoint);

      if (expandStart) startFrontier = next.concat(rest);
      else endFrontier = next.concat(rest);
    }
  } catch (e) {
    if (e?.name !== 'QueryEvicted') console.error("BFS failed:", e);
  }
  return null;
}

// Convert a list of node indices back to string titles
async function buildPathFromSimpleIndices(indices) {
  return indices.map(idx => {
    const title = titleOf(idx);
    if (title) return title;
    return `#${idx + 1}`;
  });
}

// Convert the discovered integer path index list back to string titles in a single batched query
async function buildPathFromIndices(startPreds, endPreds, intersect) {
  const pathStart = [];
  let curr = intersect;
  while (curr !== null) {
    pathStart.push(curr);
    curr = startPreds.get(curr);
  }
  pathStart.reverse();

  const pathEnd = [];
  curr = endPreds.get(intersect);
  while (curr !== undefined && curr !== null) {
    pathEnd.push(curr);
    curr = endPreds.get(curr);
  }
  
  const combinedIndices = [...pathStart, ...pathEnd];
  return await buildPathFromSimpleIndices(combinedIndices);
}

// Textbook single-source, single-direction BFS: no bidirectional trick, no
// heuristic, no hub-first reordering — expand the frontier in discovery order,
// one level at a time, until the target turns up. Walks 'out' edges only, same
// reasoning as every other pathfinder in this file: the reconstructed path must
// only ever use real, clickable out-links.
//
// opts.onProgress(edges), if given, is called with each newly-examined level's
// [fromIdx, toIdx] edge pairs as they're discovered — a pure reporting hook,
// awaited so a caller can pace the traversal (e.g. for a visible search
// animation) without this function knowing or caring why. It does not change
// what gets visited or in what order.
async function runSimpleBFS(startId, endId, opts = {}) {
  if (!db) return null;
  // No artificial depth cap — see runBidirectionalBFS. The loop below already exits
  // on its own once frontier.length hits 0 (the whole reachable component's been
  // walked), which is the only condition that should ever mean "no path".
  const maxDepth = opts.maxDepth ?? Infinity;
  const onProgress = opts.onProgress;

  try {
    const startIdx = findTitleIndexInTitlesBin(startId);
    const endIdx = findTitleIndexInTitlesBin(endId);
    if (startIdx === -1 || endIdx === -1) return null;
    if (startIdx === endIdx) return [startId];
    opts.onEndpoints?.(startIdx, endIdx);

    const visited = new Set([startIdx]);
    const preds = new Map([[startIdx, null]]);
    let frontier = [startIdx];

    for (let depth = 0; depth < maxDepth && frontier.length > 0; depth++) {
      const adjacency = await fetchNeighbours(frontier, PRIORITY_CLICK, 'out');

      const next = [];
      const touchedEdges = [];
      let found = -1;
      for (const curr of frontier) {
        for (const neigh of adjacency.get(curr) || []) {
          if (visited.has(neigh)) continue;
          visited.add(neigh);
          preds.set(neigh, curr);
          next.push(neigh);
          touchedEdges.push([curr, neigh]);
          if (neigh === endIdx) found = neigh;
        }
      }

      if (touchedEdges.length > 0) await onProgress?.(touchedEdges);
      if (found !== -1) {
        const pathIndices = [];
        let cur = found;
        while (cur !== null && cur !== undefined) { pathIndices.push(cur); cur = preds.get(cur); }
        pathIndices.reverse();
        return await buildPathFromSimpleIndices(pathIndices);
      }

      frontier = next;
    }
  } catch (e) {
    if (e?.name !== 'QueryEvicted') console.error("Simple BFS failed:", e);
  }
  return null;
}

// Textbook iterative (stack-based, not recursive — the graph is far too deep for
// the call stack) single-direction DFS: pop a node, walk its real out-links,
// push whatever's new, repeat. No heuristic, no reordering of neighbors. Same
// direction guarantee as every other pathfinder here: 'out' edges only.
//
// DFS has no shortest-path guarantee, but it should still be exhaustive: no
// artificial visited cap, so "no path found" only ever means the whole forward-
// reachable component from startId was walked (stack ran empty) without hitting
// endId, not that a search budget ran out with graph still left unexplored.
// opts.onProgress works exactly as in runSimpleBFS, except it fires once per popped
// node (DFS's natural granularity) rather than once per level.
async function runSimpleDFS(startId, endId, opts = {}) {
  if (!db) return null;
  const maxVisited = opts.maxVisited ?? Infinity;
  const onProgress = opts.onProgress;

  try {
    const startIdx = findTitleIndexInTitlesBin(startId);
    const endIdx = findTitleIndexInTitlesBin(endId);
    if (startIdx === -1 || endIdx === -1) return null;
    if (startIdx === endIdx) return [startId];
    opts.onEndpoints?.(startIdx, endIdx);

    const visited = new Set([startIdx]);
    const preds = new Map([[startIdx, null]]);
    const stack = [startIdx];

    while (stack.length > 0 && visited.size < maxVisited) {
      const curr = stack.pop();
      const adjacency = await fetchNeighbours([curr], PRIORITY_CLICK, 'out');

      // Batch onProgress once per popped node (like runSimpleBFS batches once per
      // level), not once per neighbor. A hub node's out-degree can run into the
      // thousands, and awaiting onProgress per-edge turned every hub the traversal
      // popped into thousands of extra awaited round trips — that's what made this
      // effectively hang for minutes on any search that touched a popular page.
      const touchedEdges = [];
      let found = -1;
      for (const neigh of adjacency.get(curr) || []) {
        if (visited.has(neigh)) continue;
        visited.add(neigh);
        preds.set(neigh, curr);
        stack.push(neigh);
        touchedEdges.push([curr, neigh]);
        if (neigh === endIdx) found = neigh;
      }

      if (touchedEdges.length > 0) await onProgress?.(touchedEdges);
      if (found !== -1) {
        const pathIndices = [];
        let cur = found;
        while (cur !== null && cur !== undefined) { pathIndices.push(cur); cur = preds.get(cur); }
        pathIndices.reverse();
        return await buildPathFromSimpleIndices(pathIndices);
      }
    }
  } catch (e) {
    if (e?.name !== 'QueryEvicted') console.error("Simple DFS failed:", e);
  }
  return null;
}
