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
        # Category ID is column 4
        df_nodes['category'] = raw_cp[:, 4].astype(np.int32)
    else:
        # Default all to category 0
        df_nodes['category'] = cp.zeros(num_nodes, dtype=cp.int32)
        
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
        df_nodes['vertex'] = cp.arange(num_nodes, dtype=np.int32)
        
        edges_coords = df_edges.merge(df_nodes[['vertex', 'x', 'y']], left_on='source', right_on='vertex')
        edges_coords = edges_coords.rename(columns={'x': 'x_src', 'y': 'y_src'}).drop(columns=['vertex'])
        
        # Merge target coordinate columns
        edges_coords = edges_coords.merge(df_nodes[['vertex', 'x', 'y']], left_on='target', right_on='vertex')
        edges_coords = edges_coords.rename(columns={'x': 'x_tgt', 'y': 'y_tgt'}).drop(columns=['vertex'])
        
        # Vectorized line coordinate construction using CuPy (0.1s on GPU)
        lines_x = cp.empty(num_edges * 3, dtype=cp.float32)
        lines_y = cp.empty(num_edges * 3, dtype=cp.float32)
        
        lines_x[0::3] = cp.array(edges_coords['x_src'].values)
        lines_y[0::3] = cp.array(edges_coords['y_src'].values)
        
        lines_x[1::3] = cp.array(edges_coords['x_tgt'].values)
        lines_y[1::3] = cp.array(edges_coords['y_tgt'].values)
        
        lines_x[2::3] = cp.nan
        lines_y[2::3] = cp.nan
        
        df_lines = cudf.DataFrame({
            'x': lines_x,
            'y': lines_y
        })
        
        # Clean up intermediate DataFrames
        del df_edges, edges_coords, lines_x, lines_y
        cp.get_default_memory_pool().free_all_blocks()
        
        print(f"  Mapped filaments for {num_edges:,} edges in {time.time() - start_edges:.2f}s.")

    # 3. Setup Datashader Canvas
    print(f"Step 3: Creating canvas of size {width}x{height}...")
    cvs = ds.Canvas(plot_width=width, plot_height=height)
    
    # 4. Render Edges (Filaments)
    img_edges = None
    if df_lines is not None:
        print("Step 4: Rendering filaments (line aggregation)...")
        start_render_edges = time.time()
        agg_edges = cvs.line(df_lines, 'x', 'y', ds.count())
        
        # Glowing blue-to-white edge palette
        img_edges = tf.shade(agg_edges, cmap=['#000000', '#0a1d35', '#246bcf', '#ffffff'], how='log')
        print(f"  Filaments rendered in {time.time() - start_render_edges:.2f}s.")
        del df_lines
        cp.get_default_memory_pool().free_all_blocks()
        
    # 5. Render Nodes (Points)
    print("Step 5: Rendering nodes (point aggregation)...")
    start_render_nodes = time.time()
    
    # Cast category to categoricals for color keying
    df_nodes['category'] = df_nodes['category'].astype('category')
    agg_nodes = cvs.points(df_nodes, 'x', 'y', ds.by('category', ds.count()))
    
    # Dynamic category coloring
    color_key = {
        0: '#e62727',  # Biography & People - Red
        1: '#1ea2d9',  # Science & Technology - Blue/Cyan
        2: '#d91e82',  # History & Society - Pink
        3: '#a81ed9',  # Art & Culture - Purple
        4: '#d97f1e',  # Philosophy & Religion - Orange
        5: '#1ed93d',  # Geography & Places - Green
        6: '#757575',  # Other & General - Grey
        7: '#d9d21e',  # Category 7 fallback (e.g. Sports - Yellow)
        8: '#5dbdcf'   # Category 8 fallback (e.g. Business - Cyan)
    }
    
    img_nodes = tf.shade(agg_nodes, color_key=color_key, how='log')
    
    # Spread nodes slightly to make them visible at high resolutions
    spread_px = max(1, int(width / 8000))
    print(f"  Spreading node pixels by px={spread_px} to improve visibility...")
    img_nodes = tf.spread(img_nodes, px=spread_px)
    print(f"  Nodes rendered in {time.time() - start_render_nodes:.2f}s.")
    
    # 6. Blending and Saving
    print("Step 6: Blending layers and exporting static PNG image...")
    start_blend = time.time()
    
    if img_edges is not None:
        final_img = tf.stack(img_edges, img_nodes, how='over')
    else:
        final_img = img_nodes
        
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
