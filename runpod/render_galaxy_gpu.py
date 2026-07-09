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

def render_gpu(bin_path, edges_csv, output_name, width, height, edge_sample):
    print("--- GPU-Accelerated Datashader Renderer ---")
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
    
    # Check if edges file exists
    if not os.path.exists(edges_csv):
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

        # Compute edge distances on the GPU using pure CuPy to separate short-range and long-range edges
        dx = x_tgt - x_src
        dy = y_tgt - y_src
        dist = cp.sqrt(dx**2 + dy**2)
        
        # Bends edges that span > 12% of the maximum edge distance in the graph
        max_dist = float(dist.max())
        threshold = max_dist * 0.12
        
        # Calculate midpoints (baseline control points)
        mid_x = (x_src + x_tgt) / 2.0
        mid_y = (y_src + y_tgt) / 2.0
        
        # Apply bending shift toward the coordinate center (0, 0)
        bend_mask_cp = dist > threshold
        p1_x = mid_x.copy()
        p1_y = mid_y.copy()
        
        # Pull long-range edges 45% of the way toward the center to bundle them
        p1_x[bend_mask_cp] = mid_x[bend_mask_cp] * 0.55
        p1_y[bend_mask_cp] = mid_y[bend_mask_cp] * 0.55
        
        # Evaluate Quadratic Bezier curve at 5 points (t = 0.0, 0.25, 0.5, 0.75, 1.0)
        # Plus 1 NaN point to separate the segment rendering
        total_points = num_edges * 6
        
        lines_x = cp.empty(total_points, dtype=cp.float32)
        lines_y = cp.empty(total_points, dtype=cp.float32)
        
        # evaluate formula: B(t) = (1-t)^2 * P0 + 2*(1-t)*t * P1 + t^2 * P2
        for s_idx, t in enumerate([0.0, 0.25, 0.5, 0.75, 1.0]):
            b_x = (1.0 - t)**2 * x_src + 2.0 * (1.0 - t) * t * p1_x + t**2 * x_tgt
            b_y = (1.0 - t)**2 * y_src + 2.0 * (1.0 - t) * t * p1_y + t**2 * y_tgt
            
            lines_x[s_idx::6] = b_x
            lines_y[s_idx::6] = b_y
            
        # Add NaNs to separate lines in Datashader
        lines_x[5::6] = cp.nan
        lines_y[5::6] = cp.nan
        
        df_lines = cudf.DataFrame({
            'x': lines_x,
            'y': lines_y
        })
        
        # Clean up intermediate DataFrames
        del df_edges, edges_coords, lines_x, lines_y, dx, dy, dist, x_src, y_src, x_tgt, y_tgt, mid_x, mid_y, p1_x, p1_y, bend_mask_cp
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
        # Alpha is capped at 150 so the filament layer stays translucent — the previous render
        # showed unbounded white edges completely whiting out the periphery and burying node dust.
        img_edges = tf.shade(agg_edges, cmap='#ffffff', how='eq_hist', alpha=150, min_alpha=8)
        print(f"  Filaments rendered in {time.time() - start_render_edges:.2f}s.")
        del df_lines
        cp.get_default_memory_pool().free_all_blocks()
        
    # 5. Render Nodes (Points) Category-by-Category to conserve memory
    print("Step 5: Rendering nodes (point aggregation category-by-category)...")
    start_render_nodes = time.time()
    
    color_key = {
        # Hues assigned by population: the 4 dominant categories (89% of nodes)
        # get maximally separated colors; rare categories take the in-between hues.
        0: '#ff1a1a',  # Biography & People (27.6%) - Red
        1: '#00ffff',  # Science & Technology (2.8%) - Cyan
        2: '#ff8000',  # History & Society (7.7%) - Orange
        3: '#ff00ff',  # Art & Culture (19.4%) - Magenta
        4: '#ff3399',  # Philosophy & Religion (1.3%) - Hot Pink
        5: '#33ff33',  # Geography & Places (18.3%) - Lime Green
        6: '#ffcc00',  # Other & General (22.9%) - Gold
        7: '#50c878',  # Sports (unpopulated) - Emerald
        8: '#3399ff'   # Business (unpopulated) - Blue
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

    # Color lookup table indexed by category id; row (maxc+1) is the black
    # "no winner" slot for empty pixels.
    maxc = max(cats_present) if cats_present else 0
    lut = cp.zeros((maxc + 2, 3), dtype=cp.float32)
    for c in cats_present:
        lut[c] = cp.asarray(hex_to_rgb(color_key[c]), dtype=cp.float32)

    def wta_tier(df_tier, spread_px, floor, gamma=0.5):
        """Winner-take-all RGBA image for a node subset.

        For each pixel, pick the category with the most nodes there and color
        by it — so no category is privileged by draw order (the old bug where
        higher-index green/gold were stacked over red/magenta). Uses only
        ds.count() per category (guaranteed on the GPU path), incrementally
        tracking the per-pixel argmax to avoid materializing an (H,W,C) cube.
        """
        if len(df_tier) == 0:
            return None
        total = best = winner = coords_ref = None
        for c in cats_present:
            df_c = df_tier[df_tier['category'] == c]
            if len(df_c) == 0:
                continue
            agg_c = cvs.points(df_c, 'x', 'y', ds.count())
            coords_ref = agg_c
            cnt = agg_c.data.astype(cp.float32)
            if total is None:
                total = cp.zeros(cnt.shape, dtype=cp.float32)
                best = cp.zeros(cnt.shape, dtype=cp.float32)
                winner = cp.full(cnt.shape, -1, dtype=cp.int32)
            total += cnt
            take = cnt > best
            winner = cp.where(take, cp.int32(c), winner)
            best = cp.where(take, cnt, best)
            del cnt, take, df_c
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
        packed = r | (g << 8) | (b << 16) | (a << 24)  # datashader RGBA uint32

        img = tf.Image(xr.DataArray(packed, coords=coords_ref.coords, dims=coords_ref.dims))
        img = tf.spread(img, px=spread_px)
        del total, best, winner, intensity, widx, r, g, b, a, mask, packed
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
        img_nodes = tf.shade(agg_nodes, cmap=['#333333', '#ffffff'], how='eq_hist')
        img_nodes = tf.spread(img_nodes, px=px_dust)
        
    print(f"  Nodes rendered in {time.time() - start_render_nodes:.2f}s.")
    
    # 6. Blending and Saving
    print("Step 6: Blending layers and exporting static PNG image...")
    start_blend = time.time()
    
    if img_edges is not None:
        final_img = tf.stack(img_edges, img_nodes, how='over')
    else:
        final_img = img_nodes
        
    # Convert final stacked image to CPU right before saving to prevent device/type mismatches in export_image
    if hasattr(final_img.data, 'get'):
        import xarray as xr
        final_img = xr.DataArray(final_img.data.get(), coords=final_img.coords, dims=final_img.dims)

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
    
    args = parser.parse_args()
    
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
        
    render_gpu(bin_file, edges_file, output_img, args.width, args.height, args.edge_sample)
