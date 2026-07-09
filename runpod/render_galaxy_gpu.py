import os
import sys
import time
import struct
import argparse
import numpy as np

try:
    import cupy as cp
    import cudf
    import datashader as ds
    import datashader.transfer_functions as tf
    from datashader.utils import export_image
except ImportError as e:
    print("Error: Missing required GPU libraries (cupy, cudf, datashader).")
    print(f"Details: {e}")
    print("Please install them or use a RAPIDS-equipped container.")
    sys.exit(1)

def get_next_output_path(base_name="massive_galaxy_gpu"):
    i = 1
    while True:
        candidate = f"{base_name}{i}.png"
        if not os.path.exists(candidate):
            return candidate
        i += 1

def dim_hex_color(hex_color, factor=0.25):
    hex_color = hex_color.lstrip('#')
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    r = max(15, int(r * factor))
    g = max(15, int(g * factor))
    b = max(15, int(b * factor))
    return f"#{r:02x}{g:02x}{b:02x}"

def render_gpu(bin_path, edges_csv, output_name, width, height, edge_sample,
               edge_curve=0.0, edge_alpha=90, edges_off=False, cat_weights=None):
    print("--- GPU-Accelerated Datashader Renderer ---")
    print(f"  Edge settings: {'OFF' if edges_off else 'ON'} curve={edge_curve} alpha={edge_alpha}")
    print(f"  Category dominance weights: {cat_weights or 'all 1.0'}")
    start_total = time.time()
    
    # 1. Read Binary Coordinates
    print(f"Step 1: Loading raw enriched coordinates from {bin_path}...")
    start_load = time.time()
    with open(bin_path, "rb") as f:
        num_nodes = struct.unpack("I", f.read(4))[0]
        # Load raw float array using cupy directly to GPU VRAM
        raw_cp = cp.fromfile(f, dtype=cp.float32)
        
    total_floats = len(raw_cp)
    cols = 5
    if total_floats % 5 == 0:
        cols = 5
    elif total_floats % 4 == 0:
        cols = 4
        print("  Warning: Coordinates file has 4 columns (no category data). Coloring will be default.")
    else:
        cols = total_floats // num_nodes
        print(f"  Detected columns per node: {cols}")
        
    raw_cp = raw_cp[:num_nodes * cols].reshape(num_nodes, cols)
    
    # Move x, y to cuDF
    df_nodes = cudf.DataFrame({
        'x': raw_cp[:, 0],
        'y': raw_cp[:, 1]
    })
    
    if cols >= 5:
        # Views is column 2, Category ID is column 4
        df_nodes['views'] = raw_cp[:, 2]
        df_nodes['category'] = raw_cp[:, 4].astype(np.int32)
    else:
        # Default fallback values
        df_nodes['views'] = cp.zeros(num_nodes, dtype=cp.float32)
        df_nodes['category'] = cp.zeros(num_nodes, dtype=cp.int32)

    # Assign vertex IDs BEFORE any row filtering, so edge merges stay aligned
    df_nodes['vertex'] = cp.arange(num_nodes, dtype=np.int32)

    # Drop nodes with NaN/null coordinates (smoke-test bins mark unsimulated nodes
    # as NaN; cuDF stores them as nulls which CuPy/Datashader cannot consume)
    n_before = len(df_nodes)
    df_nodes = df_nodes.dropna(subset=['x', 'y']).reset_index(drop=True)
    if len(df_nodes) < n_before:
        print(f"  Dropped {n_before - len(df_nodes):,} NaN-coordinate nodes (smoke-mode bin).")

    print(f"  Loaded coordinates for {num_nodes:,} nodes in {time.time() - start_load:.2f}s.")
    
    # Distribute the orphan cluster at (0, 0) if any
    orphan_mask = (df_nodes['x'] == 0.0) & (df_nodes['y'] == 0.0)
    num_orphans = orphan_mask.sum()
    if num_orphans > 0:
        print(f"  Distributing {num_orphans:,} orphans located at (0, 0)...")
        min_x = float(df_nodes.loc[~orphan_mask, 'x'].min())
        max_x = float(df_nodes.loc[~orphan_mask, 'x'].max())
        min_y = float(df_nodes.loc[~orphan_mask, 'y'].min())
        max_y = float(df_nodes.loc[~orphan_mask, 'y'].max())
        
        # Generate random values on GPU
        df_nodes.loc[orphan_mask, 'x'] = cp.random.uniform(min_x, max_x, int(num_orphans))
        df_nodes.loc[orphan_mask, 'y'] = cp.random.uniform(min_y, max_y, int(num_orphans))

    # 2. Load Edge List and Map Coordinates
    print(f"Step 2: Loading edge list from {edges_csv}...")
    start_edges = time.time()
    
    # Check if edges file exists / are disabled
    if edges_off:
        print("  Edges disabled (--edges-off) — rendering nodes only.")
        df_lines = None
    elif not os.path.exists(edges_csv):
        print(f"Error: Edges file {edges_csv} not found. Nodes will be rendered without filaments.")
        df_lines = None
    else:
        # cuDF read CSV is extremely fast on GPU
        df_edges = cudf.read_csv(edges_csv, compression='gzip', dtype={'source': np.int32, 'target': np.int32})
        
        # Limit edges if requested
        if edge_sample > 0 and len(df_edges) > edge_sample:
            print(f"  Sampling first {edge_sample:,} edges for filaments (out of {len(df_edges):,} total)...")
            df_edges = df_edges.head(edge_sample)
            
        num_edges = len(df_edges)
        
        # Map source and target coordinates using GPU merges
        if 'vertex' not in df_nodes.columns:
            df_nodes['vertex'] = cp.arange(num_nodes, dtype=np.int32)
        
        edges_coords = df_edges.merge(df_nodes[['vertex', 'x', 'y']], left_on='source', right_on='vertex')
        edges_coords = edges_coords.rename(columns={'x': 'x_src', 'y': 'y_src'}).drop(columns=['vertex'])
        
        # Merge target coordinate columns
        edges_coords = edges_coords.merge(df_nodes[['vertex', 'x', 'y']], left_on='target', right_on='vertex')
        edges_coords = edges_coords.rename(columns={'x': 'x_tgt', 'y': 'y_tgt'}).drop(columns=['vertex'])

        # Recompute edge count: the inner merges drop edges whose endpoints were
        # filtered out (NaN-coordinate nodes in smoke bins) — stale num_edges
        # breaks the strided Bezier array assignments below
        if len(edges_coords) != num_edges:
            print(f"  {num_edges - len(edges_coords):,} edges dropped (filtered endpoints); rendering {len(edges_coords):,}.")
            num_edges = len(edges_coords)

        # Convert columns to CuPy arrays for vectorized GPU calculation
        x_src = cp.asarray(edges_coords['x_src'])
        y_src = cp.asarray(edges_coords['y_src'])
        x_tgt = cp.asarray(edges_coords['x_tgt'])
        y_tgt = cp.asarray(edges_coords['y_tgt'])

        # Straight edges: endpoint -> endpoint, NaN separator (3 points per edge).
        # NOTE: a previous version pulled every long edge's control point 45%
        # toward the global origin (0,0) "to bundle them" — that funnels all
        # filaments through the center and creates the washed-out white core.
        # True bundling attracts edges to each other, not to the origin; straight
        # lines are the honest default. (edge_curve>0 re-enables a mild outward
        # arc that never passes through the center.)
        if edge_curve > 0.0:
            # Bow each edge sideways (perpendicular to itself), away from center,
            # by a fraction of its own length — organic look, no origin funnel.
            mid_x = (x_src + x_tgt) / 2.0
            mid_y = (y_src + y_tgt) / 2.0
            dx = x_tgt - x_src
            dy = y_tgt - y_src
            # perpendicular unit vector, oriented outward from origin
            px = -dy
            py = dx
            plen = cp.sqrt(px * px + py * py) + 1e-6
            outward = cp.sign(px * mid_x + py * mid_y)
            outward = cp.where(outward == 0, 1.0, outward)
            seglen = cp.sqrt(dx * dx + dy * dy)
            p1_x = mid_x + (px / plen) * outward * seglen * edge_curve
            p1_y = mid_y + (py / plen) * outward * seglen * edge_curve
            lines_x = cp.empty(num_edges * 6, dtype=cp.float32)
            lines_y = cp.empty(num_edges * 6, dtype=cp.float32)
            for s_idx, t in enumerate([0.0, 0.25, 0.5, 0.75, 1.0]):
                lines_x[s_idx::6] = (1 - t) ** 2 * x_src + 2 * (1 - t) * t * p1_x + t ** 2 * x_tgt
                lines_y[s_idx::6] = (1 - t) ** 2 * y_src + 2 * (1 - t) * t * p1_y + t ** 2 * y_tgt
            lines_x[5::6] = cp.nan
            lines_y[5::6] = cp.nan
        else:
            lines_x = cp.empty(num_edges * 3, dtype=cp.float32)
            lines_y = cp.empty(num_edges * 3, dtype=cp.float32)
            lines_x[0::3] = x_src; lines_y[0::3] = y_src
            lines_x[1::3] = x_tgt; lines_y[1::3] = y_tgt
            lines_x[2::3] = cp.nan; lines_y[2::3] = cp.nan

        df_lines = cudf.DataFrame({
            'x': lines_x,
            'y': lines_y
        })
        
        # Clean up intermediate DataFrames
        del df_edges, edges_coords, lines_x, lines_y, x_src, y_src, x_tgt, y_tgt
        cp.get_default_memory_pool().free_all_blocks()
        
        print(f"  Mapped curves for {num_edges:,} edges in {time.time() - start_edges:.2f}s.")

    # 3. Setup Datashader Canvas
    print(f"Step 3: Creating canvas of size {width}x{height}...")
    x_min = float(df_nodes['x'].min())
    x_max = float(df_nodes['x'].max())
    y_min = float(df_nodes['y'].min())
    y_max = float(df_nodes['y'].max())
    
    # Add a small padding to prevent clipping at bounds
    x_pad = (x_max - x_min) * 0.01 if x_max > x_min else 1.0
    y_pad = (y_max - y_min) * 0.01 if y_max > y_min else 1.0
    x_range = (x_min - x_pad, x_max + x_pad)
    y_range = (y_min - y_pad, y_max + y_pad)
    
    print(f"  Using ranges x: {x_range}, y: {y_range}")
    cvs = ds.Canvas(plot_width=width, plot_height=height, x_range=x_range, y_range=y_range)
    
    # 4. Render Edges (Filaments)
    img_edges = None
    if df_lines is not None:
        print("Step 4: Rendering filaments (line aggregation)...")
        start_render_edges = time.time()
        agg_edges = cvs.line(df_lines, 'x', 'y', ds.count())
        
        # Pure white edges: single-color shading varies alpha by density (denser = more opaque).
        # edge_alpha caps the filament layer's opacity so dense bundles don't white out the map.
        img_edges = tf.shade(agg_edges, cmap='#ffffff', how='eq_hist', alpha=edge_alpha, min_alpha=6)
        print(f"  Filaments rendered in {time.time() - start_render_edges:.2f}s.")
        del df_lines
        cp.get_default_memory_pool().free_all_blocks()
        
    # 5. Render Nodes (Points) Category-by-Category to conserve memory
    print("Step 5: Rendering nodes (point aggregation category-by-category)...")
    start_render_nodes = time.time()
    
    # Hues spread maximally around the color wheel. The 4 dominant categories
    # (Biography/Other/Art/Geography = 88% of nodes) get red/yellow/blue/green —
    # ~90 deg apart, instantly distinguishable — instead of four warm reds.
    color_key = {
        0: '#ff2020',  # Biography & People (27.6%) - Red        (hue 0)
        1: '#00e5ff',  # Science & Technology (2.8%) - Cyan      (hue 190)
        2: '#ff8a00',  # History & Society (7.7%) - Orange       (hue 33)
        3: '#2a6bff',  # Art & Culture (19.4%) - Blue            (hue 220)
        4: '#c04dff',  # Philosophy & Religion (1.3%) - Violet   (hue 275)
        5: '#2bd94b',  # Geography & Places (18.3%) - Green      (hue 133)
        6: '#ffe000',  # Other & General (22.9%) - Yellow        (hue 53)
        7: '#ff2e8b',  # Sports (unpopulated) - Pink             (hue 330)
        8: '#00d9a6'   # Business (unpopulated) - Teal           (hue 166)
    }
    
    px_dust = max(1, int(width / 16000))
    px_stars = max(2, int(width / 8000))
    px_supernova = max(3, int(width / 5000))
    print(f"  Astronomical spread widths: dust={px_dust}px, stars={px_stars}px, supernova={px_supernova}px")
    
    import xarray as xr

    def hex_to_rgb(h):
        h = h.lstrip('#')
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    # Categories actually present in the data (skip empty ones cheaply)
    cats_present = [c for c in color_key if int((df_nodes['category'] == c).sum()) > 0]

    # Per-category dominance weights (parsed from --cat-weights "0:0.4,3:1.2" etc.)
    cat_weight = dict(cat_weights or {})

    # Color lookup table indexed by category id; row (maxc+1) is the black
    # "no winner" slot for empty pixels.
    maxc = max(cats_present) if cats_present else 0
    lut = cp.zeros((maxc + 2, 3), dtype=cp.float32)
    for c in cats_present:
        lut[c] = cp.asarray(hex_to_rgb(color_key[c]), dtype=cp.float32)

    from cupyx.scipy.ndimage import maximum_filter

    def wta_tier(df_tier, spread_px, floor, gamma=0.5):
        """Winner-take-all RGBA image (numpy-backed) for a node subset.

        For each pixel, pick the category with the most nodes there and color
        by it — so no category is privileged by draw order (the old bug where
        higher-index green/gold were stacked over red/magenta). Uses only
        ds.count() per category (guaranteed on the GPU path), incrementally
        tracking the per-pixel argmax to avoid materializing an (H,W,C) cube.
        Node "size" (spread) is applied in pure CuPy via maximum_filter on each
        category's counts BEFORE the argmax — this keeps everything GPU-native
        and avoids feeding a hand-built Image to tf.spread (whose CUDA path only
        triggers for tf.shade output). The result is copied to host as a numpy
        Image so the final composite uses Datashader's (validated) CPU tf.stack.
        """
        if len(df_tier) == 0:
            return None
        ksize = 2 * spread_px + 1
        total = best = winner = coords_ref = None
        for c in cats_present:
            df_c = df_tier[df_tier['category'] == c]
            if len(df_c) == 0:
                continue
            agg_c = cvs.points(df_c, 'x', 'y', ds.count())
            coords_ref = agg_c
            cnt = agg_c.data.astype(cp.float32)
            if ksize > 1:
                cnt = maximum_filter(cnt, size=ksize)  # spread = dilate counts
            if total is None:
                total = cp.zeros(cnt.shape, dtype=cp.float32)
                best = cp.zeros(cnt.shape, dtype=cp.float32)
                winner = cp.full(cnt.shape, -1, dtype=cp.int32)
            total += cnt  # brightness uses raw counts
            # Dominance uses WEIGHTED counts: demoting a category (weight < 1) lets
            # it win only where it truly dominates, so e.g. Biography stops painting
            # every Art/Science region red just by being the global plurality.
            score = cnt * cat_weight.get(c, 1.0)
            take = score > best
            winner = cp.where(take, cp.int32(c), winner)
            best = cp.where(take, score, best)
            del cnt, score, take, df_c, agg_c
            cp.get_default_memory_pool().free_all_blocks()
        if total is None:
            return None

        # Rank-based histogram equalization of total count over non-empty pixels
        # (matches Datashader eq_hist: uniform brightness distribution across ranks)
        mask = total > 0
        intensity = cp.zeros(total.shape, dtype=cp.float32)
        n = int(mask.sum())
        if n > 0:
            vals = total[mask]
            ranks = cp.argsort(cp.argsort(vals)).astype(cp.float32) / max(n - 1, 1)
            intensity[mask] = floor + (1.0 - floor) * (ranks ** gamma)

        widx = cp.where(winner < 0, maxc + 1, winner)  # empty pixels -> black row
        r = (lut[:, 0][widx] * intensity).astype(cp.uint32)
        g = (lut[:, 1][widx] * intensity).astype(cp.uint32)
        b = (lut[:, 2][widx] * intensity).astype(cp.uint32)
        a = mask.astype(cp.uint32) * 255
        packed = (r | (g << 8) | (b << 16) | (a << 24)).get()  # -> host numpy uint32

        # numpy coords so the DataArray is fully host-side (CPU tf.stack path)
        coords = {k: np.asarray(v) for k, v in coords_ref.coords.items()}
        img = tf.Image(xr.DataArray(packed, coords=coords, dims=coords_ref.dims))
        del total, best, winner, intensity, widx, r, g, b, a, mask
        cp.get_default_memory_pool().free_all_blocks()
        return img

    # Split ALL nodes into three view-based size tiers, then WTA-composite each.
    # Tiers stack by SIZE (dust bottom, supernova top) — a legitimate ordering
    # (more-viewed nodes drawn larger and on top), not a per-category bias.
    if len(df_nodes) > 100:
        q995 = float(df_nodes['views'].quantile(0.995))
        q95 = float(df_nodes['views'].quantile(0.95))
        df_dust = df_nodes[df_nodes['views'] < q95]
        df_stars = df_nodes[(df_nodes['views'] >= q95) & (df_nodes['views'] < q995)]
        df_supernova = df_nodes[df_nodes['views'] >= q995]
    else:
        df_dust, df_stars, df_supernova = df_nodes, df_nodes.head(0), df_nodes.head(0)

    print(f"  Winner-take-all compositing: dust={len(df_dust):,} "
          f"stars={len(df_stars):,} supernova={len(df_supernova):,}")

    img_nodes = None
    for df_tier, spx, fl in [(df_dust, px_dust, 0.12),
                             (df_stars, px_stars, 0.45),
                             (df_supernova, px_supernova, 0.75)]:
        tier_img = wta_tier(df_tier, spx, floor=fl)
        if tier_img is not None:
            img_nodes = tier_img if img_nodes is None else tf.stack(img_nodes, tier_img, how='over')
        del df_tier
        cp.get_default_memory_pool().free_all_blocks()

    if img_nodes is None:
        print("  Warning: No category nodes found. Rendering fallback default aggregation...")
        agg_nodes = cvs.points(df_nodes, 'x', 'y', ds.count())
        fb = tf.shade(agg_nodes, cmap=['#333333', '#ffffff'], how='eq_hist')
        fb = tf.spread(fb, px=px_dust)
        img_nodes = tf.Image(xr.DataArray(fb.data.get(), coords={k: np.asarray(v) for k, v in fb.coords.items()}, dims=fb.dims))

    print(f"  Nodes rendered in {time.time() - start_render_nodes:.2f}s.")

    # 6. Blending and Saving
    print("Step 6: Blending layers and exporting static PNG image...")
    start_blend = time.time()

    # img_nodes is host/numpy (from wta_tier). Bring edges to host too so the
    # final tf.stack runs on Datashader's validated CPU path with no device mix.
    if img_edges is not None:
        if hasattr(img_edges.data, 'get'):
            img_edges = tf.Image(xr.DataArray(
                img_edges.data.get(),
                coords={k: np.asarray(v) for k, v in img_edges.coords.items()},
                dims=img_edges.dims))
        final_img = tf.stack(img_edges, img_nodes, how='over')
    else:
        final_img = img_nodes

    # export_image (set_background) requires a datashader Image, not a bare
    # DataArray — keep it as tf.Image and ensure it is host-backed.
    if hasattr(final_img.data, 'get'):
        final_img = tf.Image(xr.DataArray(final_img.data.get(), coords=final_img.coords, dims=final_img.dims))

    # Remove file extension from output name since export_image appends .png
    base_output = os.path.splitext(output_name)[0]
    export_image(final_img, base_output, background="black", export_path=".")
    
    print(f"  Image exported successfully as {base_output}.png in {time.time() - start_blend:.2f}s.")
    print(f"=== Total Rendering Time: {time.time() - start_total:.2f} seconds ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPU-Accelerated Wikipedia Galaxy Renderer (Datashader)")
    parser.add_argument("--bin", type=str, default="coordinates_rapids.bin", help="Path to coordinates binary file")
    parser.add_argument("--edges", type=str, default="edges_weighted.csv.gz", help="Path to gzipped edges CSV file")
    parser.add_argument("--output", type=str, default="", help="Output image filename (default: auto-increment)")
    parser.add_argument("--width", type=int, default=16000, help="Width of the output image in pixels")
    parser.add_argument("--height", type=int, default=16000, help="Height of the output image in pixels")
    parser.add_argument("--edge_sample", type=int, default=15000000, help="Number of edges to render for filaments (0 for all)")
    parser.add_argument("--edges-off", action="store_true", help="Render nodes only, no filaments")
    parser.add_argument("--edge-curve", type=float, default=0.0, help="Sideways edge bow as fraction of length (0=straight, never toward center)")
    parser.add_argument("--edge-alpha", type=int, default=90, help="Max opacity of the filament layer (0-255); lower = less core washout")
    parser.add_argument("--cat-weights", type=str, default="", help="Per-category dominance multipliers, e.g. '0:0.4' to demote Biography")

    args = parser.parse_args()

    cat_weights = {}
    if args.cat_weights:
        for pair in args.cat_weights.split(","):
            k, v = pair.split(":")
            cat_weights[int(k)] = float(v)
    
    # Fallback paths check
    bin_file = args.bin
    if not os.path.exists(bin_file):
        if os.path.exists("coordinates.bin"):
            bin_file = "coordinates.bin"
        else:
            print("Error: Could not find coordinates_rapids.bin or coordinates.bin.")
            sys.exit(1)
            
    edges_file = args.edges
    if not os.path.exists(edges_file):
        if os.path.exists("edges.csv.gz"):
            edges_file = "edges.csv.gz"
            
    output_img = args.output
    if not output_img:
        output_img = get_next_output_path("massive_galaxy_gpu")
        
    render_gpu(bin_file, edges_file, output_img, args.width, args.height, args.edge_sample,
               edge_curve=args.edge_curve, edge_alpha=args.edge_alpha, edges_off=args.edges_off,
               cat_weights=cat_weights)
