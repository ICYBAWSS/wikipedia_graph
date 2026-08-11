/* Browser test harness for the Wikipedia graph viewer.
 *
 * Runs inside the live page against the real deck.gl instance and the real
 * SQLite-over-HTTP database — there is no mock layer. Load it with:
 *
 *   await fetch('/tests/harness.js').then(r => r.text()).then(eval);
 *   await WGTest.run();                 // functional suite
 *   await WGTest.bench();               // pathfinder + query benchmarks
 *   await WGTest.run('sidebar');        // only tests whose name matches
 *
 * Every entry point resolves to a plain JSON-serializable object so it can be
 * driven from an automation tool that only gets a return value back.
 */
(function () {
  'use strict';

  const TESTS = [];
  const test = (name, fn, opts = {}) => TESTS.push({ name, fn, timeout: opts.timeout || 30000 });

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const $ = id => document.getElementById(id);

  function assert(cond, msg) {
    if (!cond) throw new Error('assertion failed: ' + msg);
  }
  function assertEq(actual, expected, msg) {
    if (actual !== expected) throw new Error(`${msg}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }

  // Poll until predicate is truthy or the budget runs out. Returns elapsed ms.
  async function waitFor(pred, { timeout = 15000, interval = 100, what = 'condition' } = {}) {
    const t0 = performance.now();
    while (performance.now() - t0 < timeout) {
      let ok = false;
      try { ok = await pred(); } catch (_) { ok = false; }
      if (ok) return performance.now() - t0;
      await sleep(interval);
    }
    throw new Error(`timed out after ${timeout}ms waiting for ${what}`);
  }

  const wg = () => {
    if (!window.__wg) throw new Error('__wg test surface missing — engine.js did not finish run()');
    return window.__wg;
  };

  // Console error capture, so a test can fail on errors it did not directly observe.
  const captured = [];
  const origError = console.error;
  console.error = function (...args) {
    captured.push(args.map(a => (a && a.stack) ? a.stack : String(a)).join(' '));
    return origError.apply(console, args);
  };

  // ------------------------------------------------------------------
  // Boot / render
  // ------------------------------------------------------------------

  test('boot: test surface is present', async () => {
    assert(wg().ready, 'deck.gl instance exists');
    assert(wg().N > 1e6, `node count looks sane (got ${wg().N})`);
  });

  test('boot: loading overlay is dismissed', async () => {
    const ls = $('loading-screen');
    assert(ls, 'loading-screen element exists');
    await waitFor(() => ls.style.display === 'none', { what: 'loading overlay to hide' });
  });

  test('render: canvas has a WebGL context with content', async () => {
    const cv = $('graph-canvas');
    assert(cv, 'canvas exists');
    assert(cv.width > 0 && cv.height > 0, `canvas is sized (${cv.width}x${cv.height})`);
    assert(wg().visibleCount > 1000, `cull() selected a real batch of nodes (got ${wg().visibleCount})`);
  });

  test('render: cull responds to zoom', async () => {
    const before = wg().visibleCount;
    const vs0 = wg().viewState;
    wg().setViewState({ target: [0, 0, 0], zoom: vs0.zoom + 3 });
    await sleep(150);
    const zoomed = wg().visibleCount;
    wg().setViewState(vs0); // restore
    await sleep(150);
    assert(zoomed !== before, `visible count changed on zoom (${before} -> ${zoomed})`);
  });

  // ------------------------------------------------------------------
  // Database
  // ------------------------------------------------------------------

  test('db: connection is live', async () => {
    assert(wg().db, 'db handle is set');
    const rows = await wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [1], 3);
    assert(rows && rows.length === 1, 'single-row lookup returns a row');
    assert(typeof rows[0].id === 'string' && rows[0].id.length > 0, 'row has a title');
  });

  test('db: links table is queryable by integer index', async () => {
    const rows = await wg().dbQuery(
      'SELECT source_idx, target_idx FROM links WHERE source_idx = ? LIMIT 5', [1000], 3);
    assert(Array.isArray(rows), 'links query returns an array');
  });

  test('db: evicted queries reject instead of hanging', async () => {
    // Three same-priority queries in flight: the middle one must be evicted by the
    // third and settle, rather than dangling forever.
    const p1 = wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [10], 2);
    const p2 = wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [11], 2);
    const p3 = wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [12], 2);
    const settled = await Promise.race([
      Promise.allSettled([p1, p2, p3]),
      sleep(20000).then(() => 'TIMEOUT')
    ]);
    assert(settled !== 'TIMEOUT', 'all three queries settled (none dangled)');
    assert(settled.some(s => s.status === 'fulfilled'), 'at least one query succeeded');
    for (const s of settled) {
      if (s.status === 'rejected') {
        assertEq(s.reason?.name, 'QueryEvicted', 'rejection reason is an explicit eviction');
      }
    }
  });

  // ------------------------------------------------------------------
  // Sidebar / selection
  // ------------------------------------------------------------------

  test('sidebar: selectNode populates details', async () => {
    const sb = $('detail-sidebar');
    await window.selectNode(1000);
    await waitFor(() => sb.classList.contains('active'), { what: 'sidebar to open' });
    await waitFor(() => ($('sidebar-title')?.textContent || '').trim().length > 0,
      { what: 'sidebar title to populate' });
    const title = $('sidebar-title').textContent.trim();
    assert(title && !/^loading/i.test(title), `title is real content (got "${title}")`);
    assertEq(wg().selectedNodeIdx, 1000, 'selected index is tracked');
    const link = $('sidebar-wiki-link');
    assert(link && /wikipedia\.org\/wiki\//.test(link.href), 'wikipedia link is built');
  });

  test('sidebar: connections list populates', async () => {
    await window.selectNode(1000);
    await waitFor(() => ($('sidebar-connections')?.children.length || 0) > 0,
      { timeout: 25000, what: 'connections list to fill' });
    assert($('sidebar-connections').children.length > 0, 'at least one connection rendered');
  });

  test('sidebar: selectNodeById resolves a title to a node', async () => {
    const row = await wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [2000], 3);
    const title = row[0].id;
    await window.selectNodeById(title);
    await waitFor(() => $('sidebar-title').textContent.trim() === title,
      { what: `sidebar to show "${title}"` });
    assertEq($('sidebar-title').textContent.trim(), title, 'sidebar shows requested article');
  });

  test('sidebar: close button clears selection', async () => {
    await window.selectNode(1000);
    await waitFor(() => $('detail-sidebar').classList.contains('active'), { what: 'sidebar open' });
    $('sidebar-close').click();
    await sleep(100);
    assert(!$('detail-sidebar').classList.contains('active'), 'sidebar closed');
    assertEq(wg().selectedNodeIdx, -1, 'selection cleared');
  });

  // ------------------------------------------------------------------
  // Search / autocomplete
  // ------------------------------------------------------------------

  test('search: autocomplete fills the datalist', async () => {
    const box = $('search-box');
    const list = $('article-list');
    list.innerHTML = '';
    box.value = 'Berlin';
    box.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => list.children.length > 0, { timeout: 25000, what: 'autocomplete options' });
    assert(list.children.length > 0, `got ${list.children.length} suggestions`);
  });

  test('search: autocomplete loader always clears', async () => {
    const box = $('search-box');
    box.value = 'Paris';
    box.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => $('search-loader').style.display === 'none',
      { timeout: 25000, what: 'search loader to clear' });
    assertEq($('search-loader').style.display, 'none', 'loader hidden after query settles');
  });

  // ------------------------------------------------------------------
  // Pathfinding
  // ------------------------------------------------------------------

  async function titleAt(rowid) {
    const r = await wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [rowid], 3);
    return r?.[0]?.id;
  }

  test('path: bidirectional BFS finds a route', async () => {
    const a = await titleAt(1), b = await titleAt(500);
    const t0 = performance.now();
    const path = await wg().pathfinders.bfs(a, b);
    const ms = performance.now() - t0;
    assert(Array.isArray(path) && path.length >= 2, `path returned (${ms | 0}ms): ${JSON.stringify(path)}`);
    assertEq(path[0], a, 'path starts at source');
    assertEq(path[path.length - 1], b, 'path ends at target');
  }, { timeout: 120000 });

  test('path: route UI renders discovered path', async () => {
    const a = await titleAt(1), b = await titleAt(500);
    $('route-start').value = a;
    $('route-end').value = b;
    $('find-any-route-btn').click();
    await waitFor(() => $('route-text-path')?.children.length > 0,
      { timeout: 120000, what: 'route list to render' });
    assert($('route-text-path').children.length >= 2, 'route list has hops');
  }, { timeout: 150000 });

  // ------------------------------------------------------------------
  // Runner
  // ------------------------------------------------------------------

  async function run(filter) {
    const results = [];
    // Anchor on a category prefix ("render:", "path:") so a filter like "render" can't
    // also pull in "...renders discovered path" from a different, far more expensive category.
    const selected = TESTS.filter(t => !filter || t.name === filter || t.name.startsWith(filter));
    // Live progress, readable from outside while the suite is still running —
    // an automation driver that can only hold a connection for a few seconds
    // polls WGTest.progress instead of awaiting run().
    run.progress = { total: selected.length, done: 0, current: null, results };
    window.WGTest && (window.WGTest.progress = run.progress);
    for (const t of selected) {
      run.progress.current = t.name;
      captured.length = 0;
      wg().resetStats?.();
      const t0 = performance.now();
      let status = 'pass', error = null;
      try {
        await Promise.race([
          t.fn(),
          sleep(t.timeout).then(() => { throw new Error(`test timeout after ${t.timeout}ms`); })
        ]);
      } catch (e) {
        status = 'FAIL';
        error = e.message;
      }
      const stats = wg().stats?.() || {};
      results.push({
        name: t.name,
        status,
        ms: Math.round(performance.now() - t0),
        queries: stats.count || 0,
        evicted: stats.evicted || 0,
        ...(error ? { error } : {}),
        ...(captured.length ? { consoleErrors: captured.slice(0, 3) } : {})
      });
      run.progress.done++;
    }
    run.progress.current = null;
    const failed = results.filter(r => r.status === 'FAIL');
    return {
      summary: `${results.length - failed.length}/${results.length} passed`,
      failed: failed.length,
      results
    };
  }

  // Benchmarks: report where the wall-clock time actually goes.
  async function bench(opts = {}) {
    const pairs = opts.pairs || [[1, 500], [1, 250000], [1000, 900000]];
    const out = { queryLatency: null, pathfinders: [] };

    // Baseline single-row round trip — this is the network floor for one query.
    const samples = [];
    for (let i = 0; i < 5; i++) {
      const t0 = performance.now();
      await wg().dbQuery('SELECT id FROM nodes WHERE rowid = ?', [1 + i * 7919], 3);
      samples.push(performance.now() - t0);
    }
    samples.sort((a, b) => a - b);
    out.queryLatency = {
      samples: samples.map(s => Math.round(s)),
      medianMs: Math.round(samples[2]),
      note: 'one indexed single-row lookup = this many ms of network round trips'
    };

    for (const [ra, rb] of pairs) {
      const a = await titleAt(ra), b = await titleAt(rb);
      if (!a || !b) continue;
      for (const algo of (opts.algos || ['bfs', 'simpleBfs', 'simpleDfs'])) {
        wg().resetStats();
        const t0 = performance.now();
        let path = null, error = null;
        try {
          path = await Promise.race([
            wg().pathfinders[algo](a, b),
            sleep(opts.budgetMs || 60000).then(() => { throw new Error('budget exceeded'); })
          ]);
        } catch (e) { error = e.message; }
        const ms = Math.round(performance.now() - t0);
        const st = wg().stats();
        out.pathfinders.push({
          algo, from: a, to: b, ms,
          hops: path ? path.length : null,
          queries: st.count,
          dbMs: Math.round(st.totalMs),
          slowestQueryMs: Math.round(st.maxMs),
          pctInDb: st.totalMs ? Math.round((st.totalMs / ms) * 100) + '%' : null,
          ...(error ? { error } : {})
        });
      }
    }
    return out;
  }

  window.WGTest = { run, bench, tests: () => TESTS.map(t => t.name), waitFor, assert };
  return { loaded: true, tests: TESTS.length };
})();
