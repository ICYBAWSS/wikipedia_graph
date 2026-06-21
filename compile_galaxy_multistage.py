import os
import sys
import struct
import time
import numpy as np
import cupy as cp

try:
    import cudf
    import cugraph
    import pandas as pd
except ImportError:
    print("Error: RAPIDS (cudf, cugraph, pandas) is not installed.")
    sys.exit(1)

def compile_galaxy_multistage():
    edges_csv = "edges_weighted.csv.gz"
    meta_csv = "metadata.csv"
    out_bin = "coordinates_rapids.bin"

    print("--- Wikipedia Galaxy Compiler (Multi-Stage Layout Mode) ---")

    if not os.path.exists(edges_csv) or not os.path.exists(meta_csv):
        print(f"Error: {edges_csv} or {meta_csv} not found.")
        print("Make sure both metadata.csv and edges_weighted.csv.gz are present.")
        return

    # 1. Loading Metadata
    print("Step 1: Loading node metadata from CSV to GPU...")
    start_meta = time.time()
    gdf_meta = cudf.read_csv(meta_csv)
    num_nodes = len(gdf_meta)
    
    # Ensure they are sorted by ID to align with outputs
    gdf_meta = gdf_meta.sort_values('id').reset_index(drop=True)
    node_views = gdf_meta['views'].to_pandas().values.astype(np.float32)
    node_cats = gdf_meta['category_id'].to_pandas().values.astype(np.float32)
    print(f"  Loaded metadata for {num_nodes:,} nodes in {time.time() - start_meta:.2f} seconds.")
    
    # 2. Loading Edges natively into GPU
    print("Step 2: Loading edges to GPU (cuDF CSV Reader)...")
    start_load = time.time()
    
    # cuDF natively reads gzipped CSVs extremely fast
    gdf_edges = cudf.read_csv(
        edges_csv, 
        compression='gzip', 
        dtype={'source': np.int32, 'target': np.int32, 'weight': np.float32}
    )
    # Rename columns to what cugraph expects
    gdf_edges = gdf_edges.rename(columns={"target": "destination"})
    print(f"  GPU Dataframe Ready with {len(gdf_edges):,} edges. ({time.time() - start_load:.2f} seconds)")

    # 3. Construct Graph & Calculate Degrees on GPU
    print("Step 3: Constructing cuGraph & calculating degrees...")
    start_graph = time.time()
    G = cugraph.Graph(directed=False)
    # Pass weight column for weighted ForceAtlas2
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
    radius_gdf = cudf.DataFrame({
        'vertex': cp.arange(num_nodes, dtype=np.int32), 
        'radius': node_radii.astype(np.float32)
    })

    # --- MULTI-STAGE STEP 1: Core Backbone Extraction (k_core, k=3) ---
    print("Step 4: Running k-core decomposition (k=3) to extract structural backbone...")
    start_kcore = time.time()
    core_df = cugraph.core_number(G)
    core_col = 'core_number' if 'core_number' in core_df.columns else 'values'
    
    # Identify vertices belonging to core (degree >= 3 within core)
    backbone_vertices = core_df[core_df[core_col] >= 3]['vertex']
    num_backbone_nodes = len(backbone_vertices)
    print(f"  Backbone extracted with {num_backbone_nodes:,} core nodes ({num_backbone_nodes/num_nodes*100:.2f}% of total). ({time.time() - start_kcore:.2f} seconds)")

    # Slice edges that exist only within the backbone
    print("  Slicing backbone edges...")
    gdf_edges_backbone = gdf_edges[
        gdf_edges['source'].isin(backbone_vertices) & 
        gdf_edges['destination'].isin(backbone_vertices)
    ]
    
    G_backbone = cugraph.Graph(directed=False)
    G_backbone.from_cudf_edgelist(
        gdf_edges_backbone, 
        source='source', 
        destination='destination', 
        edge_attr='weight'
    )
    print(f"  Backbone graph constructed with {len(gdf_edges_backbone):,} edges.")

    # --- PHASE 1: Simulate Backbone layout (ForceAtlas2) ---
    print("Step 5 [Phase 1]: Simulating Backbone layout (ForceAtlas2) for global continents...")
    start_backbone_sim = time.time()
    
    # Filter radius DataFrame for backbone nodes
    radius_gdf_backbone = radius_gdf[radius_gdf['vertex'].isin(backbone_vertices)]
    
    pos_backbone = cugraph.force_atlas2(
        G_backbone,
        max_iter=600,  # 600 iterations for deep macro-continent relaxation
        lin_log_mode=True, 
        outbound_attraction_distribution=True, 
        scaling_ratio=80.0, # wide separation for macro-continents
        strong_gravity_mode=False,
        gravity=0.2, # relatively high gravity to prevent drifting core components
        edge_weight_influence=0.4, # balanced edge weight influence
        prevent_overlapping=False, # keep it fast
        vertex_radius=radius_gdf_backbone,
        verbose=True
    )
    print(f"  Backbone simulation complete. ({time.time() - start_backbone_sim:.2f} seconds)")

    # --- PHASE 2 PREPARATION: Gateway Ancestor Mapping ---
    print("Step 6: Running Multi-Source Label Propagation to map peripheral nodes to closest core gateways...")
    start_prop = time.time()
    
    # Create bidirectional edge list for mapping neighbors
    gdf_edges_rev = gdf_edges.rename(columns={"source": "destination", "destination": "source"})
    gdf_undir_edges = cudf.concat([gdf_edges, gdf_edges_rev]).drop_duplicates(subset=['source', 'destination'])
    
    # Initialize closest_core array
    closest_core = cudf.DataFrame({
        'vertex': cp.arange(num_nodes, dtype=np.int32),
        'gateway': cp.full(num_nodes, -1, dtype=np.int32)
    })
    # Set backbone nodes' gateway to themselves
    mask_backbone = closest_core['vertex'].isin(backbone_vertices)
    closest_core.loc[mask_backbone, 'gateway'] = closest_core.loc[mask_backbone, 'vertex']
    
    # Run iterative BFS propagation on GPU
    step = 0
    while True:
        unvisited = closest_core[closest_core['gateway'] == -1]['vertex']
        num_unvisited = len(unvisited)
        print(f"  Propagation step {step}: {num_unvisited:,} peripheral nodes remaining without a gateway.")
        if num_unvisited == 0 or step >= 10:  # 10 hops is plenty for Wikipedia graph diameter
            break
            
        # Get edges connected to unvisited destinations
        edges_to_unvisited = gdf_undir_edges[gdf_undir_edges['destination'].isin(unvisited)]
        
        # Merge with closest_core on source to get neighbors' gateway information
        joined = edges_to_unvisited.merge(
            closest_core.rename(columns={'vertex': 'source', 'gateway': 'neighbor_gateway'}),
            on='source',
            how='inner'
        )
        
        # Filter for active neighbor gateways (those != -1)
        active_joined = joined[joined['neighbor_gateway'] != -1]
        if len(active_joined) == 0:
            print("  No more connections to core. Stopping propagation.")
            break
            
        # Group by destination and select the first neighbor gateway
        resolved = active_joined.groupby('destination').agg({'neighbor_gateway': 'min'}).reset_index()
        resolved = resolved.rename(columns={'destination': 'vertex', 'neighbor_gateway': 'gateway'})
        
        # Update closest_core with the newly resolved gateways
        closest_core = closest_core.merge(resolved, on='vertex', how='left', suffixes=('', '_new'))
        mask = closest_core['gateway_new'].notnull()
        closest_core.loc[mask, 'gateway'] = closest_core.loc[mask, 'gateway_new'].astype(np.int32)
        closest_core = closest_core.drop(columns=['gateway_new'])
        
        step += 1
        
    print(f"  Propagation finished in {time.time() - start_prop:.2f} seconds.")

    # --- PHASE 2 PREPARATION: Vector-Offset Initialization ---
    print("Step 7: Initializing peripheral positions near core gateways with radial jitter...")
    # Merge closest_core with pos_backbone to align coordinates
    backbone_coords = pos_backbone.rename(columns={'vertex': 'gateway', 'x': 'gx', 'y': 'gy'})
    gateway_coords = closest_core.merge(backbone_coords, on='gateway', how='left')
    
    # Generate deterministic radial offsets based on vertex ID using Cupy
    vertex_ids_cp = cp.array(gateway_coords['vertex'].values)
    seed_cp = (vertex_ids_cp * 1103515245 + 12345) & 0x7fffffff
    theta_cp = (seed_cp % 1000) / 1000.0 * 2.0 * np.pi
    r_cp = 2.0 + (seed_cp // 1000 % 1000) / 1000.0 * 13.0
    
    theta = cp.asnumpy(theta_cp)
    r = cp.asnumpy(r_cp)
    
    # Calculate starting coordinates
    init_pos = gateway_coords.copy()
    init_pos['x'] = init_pos['gx'] + r * np.cos(theta)
    init_pos['y'] = init_pos['gy'] + r * np.sin(theta)
    
    # For backbone vertices, they should keep their original backbone positions (no jitter)
    is_backbone = init_pos['vertex'].isin(backbone_vertices)
    init_pos.loc[is_backbone, 'x'] = init_pos.loc[is_backbone, 'gx']
    init_pos.loc[is_backbone, 'y'] = init_pos.loc[is_backbone, 'gy']
    
    # For unplaced nodes (isolated components, gateway == -1), scatter randomly
    unplaced_mask = init_pos['gateway'] == -1
    num_unplaced = unplaced_mask.sum()
    if num_unplaced > 0:
        std_val = float(pos_backbone['x'].std()) if len(pos_backbone) > 0 else 1000.0
        init_pos.loc[unplaced_mask, 'x'] = np.random.uniform(-std_val * 3, std_val * 3, num_unplaced)
        init_pos.loc[unplaced_mask, 'y'] = np.random.uniform(-std_val * 3, std_val * 3, num_unplaced)
        
    print(f"  Initialized coordinates for {len(init_pos):,} nodes.")
    
    # Clean up temp dataframes to conserve memory
    del gateway_coords, backbone_coords, gdf_edges_rev, gdf_undir_edges
    cp.get_default_memory_pool().free_all_blocks()

    # --- PHASE 2: Pinned Ingestion Simulation ---
    print("Step 8 [Phase 2]: Running Pinned Ingestion Simulation (250 iterations total)...")
    start_phase2 = time.time()
    
    # Format starting positions DataFrame
    current_pos = init_pos[['vertex', 'x', 'y']].astype(np.float32)
    current_pos['vertex'] = current_pos['vertex'].astype(np.int32)
    
    # Extract pinned core coordinates DataFrame to reuse for resets
    pinned_core_pos = pos_backbone[['vertex', 'x', 'y']].astype(np.float32)
    pinned_core_pos['vertex'] = pinned_core_pos['vertex'].astype(np.int32)
    
    num_steps = 10
    iters_per_step = 25
    
    for step_idx in range(num_steps):
        step_start = time.time()
        print(f"  Step {step_idx + 1}/{num_steps}: Simulating {iters_per_step} iterations...")
        
        # Run ForceAtlas2 on the full graph
        current_pos = cugraph.force_atlas2(
            G,
            max_iter=iters_per_step,
            pos_list=current_pos,
            lin_log_mode=True,
            outbound_attraction_distribution=True,
            scaling_ratio=80.0,
            strong_gravity_mode=False,
            gravity=0.05,  # lower gravity to allow peripheral trees to expand
            edge_weight_influence=0.4,
            prevent_overlapping=False,
            verbose=False
        )
        
        # Pin backbone vertices: reset their coordinates to pinned_core_pos
        periph_pos = current_pos[~current_pos['vertex'].isin(backbone_vertices)]
        current_pos = cudf.concat([pinned_core_pos, periph_pos]).reset_index(drop=True)
        
        # Explicit clean-up to prevent VRAM memory fragmentation/drift
        del periph_pos
        cp.get_default_memory_pool().free_all_blocks()
        
        print(f"    Completed step in {time.time() - step_start:.2f} seconds.")
        
    print(f"  Phase 2 complete in {time.time() - start_phase2:.2f} seconds.")

    # --- PHASE 3: The Global Polish & Fine Settle ---
    print("Step 9 [Phase 3]: Running Global Polish Simulation...")
    start_phase3 = time.time()
    
    print("  Sub-phase 3A: Simulating 80 iterations without overlap prevention...")
    current_pos = cugraph.force_atlas2(
        G,
        max_iter=80,
        pos_list=current_pos,
        lin_log_mode=True,
        outbound_attraction_distribution=True,
        scaling_ratio=60.0, # turned down slightly from 80.0
        strong_gravity_mode=False,
        gravity=0.15,       # increased slightly from 0.05 to settle components
        edge_weight_influence=0.4,
        prevent_overlapping=False,
        verbose=True
    )
    
    print("  Sub-phase 3B: Simulating final 40 iterations with overlap prevention enabled...")
    final_pos_gdf = cugraph.force_atlas2(
        G,
        max_iter=40,
        pos_list=current_pos,
        lin_log_mode=True,
        outbound_attraction_distribution=True,
        scaling_ratio=60.0,
        strong_gravity_mode=False,
        gravity=0.15,
        edge_weight_influence=0.4,
        prevent_overlapping=True,
        vertex_radius=radius_gdf,
        verbose=True
    )
    print(f"  Phase 3 complete in {time.time() - start_phase3:.2f} seconds.")

    # --- EXPORT BINARY ---
    print("Step 10: Exporting coordinate layout + metadata to binary...")
    start_export = time.time()
    final_pos_gdf = final_pos_gdf.sort_values('vertex')

    print("  Transferring coordinates from GPU to CPU RAM...")
    xs = final_pos_gdf['x'].to_pandas().to_numpy(dtype=np.float32)
    ys = final_pos_gdf['y'].to_pandas().to_numpy(dtype=np.float32)
    valid_vertices = final_pos_gdf['vertex'].to_pandas().to_numpy(dtype=np.int32)

    final_coords = np.zeros((num_nodes, 2), dtype=np.float32)
    final_coords[valid_vertices, 0] = xs
    final_coords[valid_vertices, 1] = ys

    # Scatter degree-0 orphans that never entered the ForceAtlas2 simulation
    mask = np.ones(num_nodes, dtype=bool)
    mask[valid_vertices] = False
    orphans = np.where(mask)[0]

    if len(orphans) > 0:
        print(f"  Scattering {len(orphans):,} degree-0 orphans as background dust...")
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

if __name__ == '__main__':
    compile_galaxy_multistage()
