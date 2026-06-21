import os
import sys
import struct
import time
import numpy as np

try:
    import cudf
    import cugraph
    import pandas as pd
except ImportError:
    print("Error: RAPIDS (cudf, cugraph, pandas) is not installed.")
    sys.exit(1)

def compile_galaxy_rapids():
    edges_csv = "edges_weighted.csv.gz"
    meta_csv = "metadata.csv"
    out_bin = "coordinates_rapids.bin"

    print("--- Wikipedia Galaxy Compiler (ICYBAWSS WEIGHTED Mode) ---")

    if not os.path.exists(edges_csv) or not os.path.exists(meta_csv):
        print(f"Error: {edges_csv} or {meta_csv} not found.")
        print("Make sure you uploaded BOTH files.")
        return

    # 1. Loading Metadata
    print("Step 1: Loading node metadata from CSV...")
    df_meta = pd.read_csv(meta_csv)
    num_nodes = len(df_meta)
    
    node_views = df_meta['views'].values.astype(np.float32)
    node_cats = df_meta['category_id'].values.astype(np.float32)
    print(f"  Loaded metadata for {num_nodes:,} nodes.")
    
    # 2. Loading Edges natively into GPU
    print("Step 2: Loading 100M WEIGHTED edges to GPU (cuDF CSV Reader)...")
    start_load = time.time()
    
    # cuDF natively reads gzipped CSVs extremely fast
    gdf_edges = cudf.read_csv(
        edges_csv, 
        compression='gzip', 
        dtype={'source': np.int32, 'target': np.int32, 'weight': np.float32}
    )
    # Rename columns to what cugraph expects
    gdf_edges = gdf_edges.rename(columns={"target": "destination"})
    print(f"  GPU Dataframe Ready. ({time.time() - start_load:.2f} seconds)")

    # 3. Construct Graph & Calculate Degrees on GPU
    print("Step 3: Constructing cuGraph & calculating degrees...")
    start_graph = time.time()
    G = cugraph.Graph(directed=False)
    # We now pass the weight column!
    G.from_cudf_edgelist(gdf_edges, source='source', destination='destination', edge_attr='weight')
    print(f"  Graph construction complete. ({time.time() - start_graph:.2f} seconds)")
    
    degree_gdf = G.degree()
    full_degrees = np.zeros(num_nodes, dtype=np.float32)
    deg_v = degree_gdf['vertex'].to_pandas().values
    deg_c = degree_gdf['degree'].to_pandas().values
    full_degrees[deg_v] = deg_c
    print("  Degrees calculated.")

    # Calculate radii for collision prevention
    node_radii = np.log1p(node_views) * 2.0 + 1.0 
    radius_gdf = cudf.DataFrame({'vertex': np.arange(num_nodes, dtype=np.int32), 'radius': node_radii.astype(np.float32)})

    # 4. Running ForceAtlas2 (GOLDEN RATIO SETTINGS)
    print("Step 4: Running ForceAtlas2 Simulation (STABLE & EXPLODED)...")
    start_fa2 = time.time()

    # These settings are the industry standard for "Beautiful Mess" graphs.
    # They prevent hairballs while maintaining a centered, expansive galaxy.
    pos_gdf = cugraph.force_atlas2(
        G,
        max_iter=1000,
        lin_log_mode=True, 
        outbound_attraction_distribution=True, 
        scaling_ratio=5.0, # Increased slightly to accommodate weights
        strong_gravity_mode=False,
        gravity=1.0,
        edge_weight_influence=1.5, # Strong influence so lead-links act as primary anchors
        prevent_overlapping=True,
        vertex_radius=radius_gdf,
        verbose=True
    )
    print(f"  Simulation Complete! ({time.time() - start_fa2:.2f} seconds)")

    # 5. Exporting Master Binary
    print("Step 5: Exporting layout + metadata to binary...")
    pos_gdf = pos_gdf.sort_values('vertex')

    print("  Transferring coordinates from GPU to CPU RAM...")
    xs = pos_gdf['x'].to_pandas().to_numpy(dtype=np.float32)
    ys = pos_gdf['y'].to_pandas().to_numpy(dtype=np.float32)
    valid_vertices = pos_gdf['vertex'].to_pandas().to_numpy(dtype=np.int32)

    final_coords = np.zeros((num_nodes, 2), dtype=np.float32)
    final_coords[valid_vertices, 0] = xs
    final_coords[valid_vertices, 1] = ys

    # --- BETTER ORPHAN SCATTERING (Background Dust) ---
    mask = np.ones(num_nodes, dtype=bool)
    mask[valid_vertices] = False
    orphans = np.where(mask)[0]

    if len(orphans) > 0:
        print(f"  Scattering {len(orphans):,} degree-0 orphans as background dust...")
        # We scatter them in a very wide, sparse uniform box rather than a ring.
        # This makes them look like background stars rather than an artifact.
        std_val = np.std(final_coords[valid_vertices])
        final_coords[orphans, 0] = np.random.uniform(-std_val * 3, std_val * 3, len(orphans))
        final_coords[orphans, 1] = np.random.uniform(-std_val * 3, std_val * 3, len(orphans))


    # Format: [uint32 N][(float x, float y, float views, float degree, float cat_id) * N]
    with open(out_bin, "wb") as f:
        f.write(struct.pack("I", num_nodes))
        packed_data = np.zeros((num_nodes, 5), dtype=np.float32)
        packed_data[:, 0] = final_coords[:, 0]
        packed_data[:, 1] = final_coords[:, 1]
        packed_data[:, 2] = node_views
        packed_data[:, 3] = full_degrees
        packed_data[:, 4] = node_cats
        f.write(packed_data.tobytes())

    print(f"Done! Saved enriched coordinates to {out_bin} in {time.time() - start_export:.2f} seconds.")

if __name__ == "__main__":
    compile_galaxy_rapids()
