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

def compile_galaxy_multistage(edges_csv="edges_weighted.csv.gz", meta_csv="metadata.csv",
                              out_bin="coordinates_rapids.bin", sample_frac=1.0,
                              iters_scale=1.0, ewi3=0.4, diag_png="diagnostic_layout.png",
                              spacing=3000.0, seed_disk=2.0, min_ring=0.0):
    # Scale iteration counts for smoke-test runs (--iters-scale 0.2 => 20% of iterations)
    def it(n, floor=10):
        return max(floor, int(round(n * iters_scale)))

    print("--- Wikipedia Galaxy Compiler (Multi-Stage Layout Mode) ---")
    print(f"  Config: edges={edges_csv} meta={meta_csv} out={out_bin}")
    print(f"  Config: sample_frac={sample_frac} iters_scale={iters_scale} phase3_ewi={ewi3}")

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

    # Smoke-test mode: randomly subsample edges to validate the full pipeline cheaply
    if sample_frac < 1.0:
        gdf_edges = gdf_edges.sample(frac=sample_frac, random_state=42).reset_index(drop=True)
        print(f"  [SMOKE TEST] Subsampled to {len(gdf_edges):,} edges ({sample_frac:.0%}).")

    # --- ADVANCED STRUCTURAL STABILITY & ANTI-HAIRBALL FEATURES ---
    print("  Normalizing and clamping edge weights to prevent gravitational collapse (Attraction Floor)...")
    # Apply log1p normalization and clamp to 2.0 max weight using positional CuPy clip bounds
    gdf_edges['weight'] = cp.clip(cp.log1p(cp.asarray(gdf_edges['weight'])), 0.0, 2.0)

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

    # Calculate radii for collision prevention, normalized to the seeded layout scale:
    # map the 99.9th-percentile hub radius to ~half the Louvain community spacing (3000.0),
    # so even supernova hubs claim at most a neighborhood, never the whole map.
    # (The old flat *5000.0 gave top hubs ~195k-unit radii — larger than the entire layout.)
    raw_radii = np.log1p(node_views) * 2.0 + 1.0
    radius_scale = 1500.0 / float(np.percentile(raw_radii, 99.9))
    node_radii = raw_radii * radius_scale
    radius_gdf = cudf.DataFrame({
        'vertex': cp.arange(num_nodes, dtype=np.int32), 
        'radius': node_radii.astype(np.float32)
    })

    # --- MULTI-STAGE STEP 1: Core Backbone Extraction ---
    print("Step 4: Running k-core decomposition to extract structural backbone...")
    start_kcore = time.time()
    core_df = cugraph.core_number(G)
    core_col = 'core_number' if 'core_number' in core_df.columns else 'values'
    
    # Calculate optimal k-core threshold dynamically (targeting top ~10% of nodes for a tighter backbone skeleton)
    core_vals = core_df[core_col].to_pandas()
    k_threshold = int(core_vals.quantile(0.90))
    if k_threshold < 3:
        k_threshold = 3  # fallback to minimum 3
        
    backbone_vertices = core_df[core_df[core_col] >= k_threshold]['vertex']
    num_backbone_nodes = len(backbone_vertices)
    print(f"  Dynamic threshold chosen: k={k_threshold}")
    print(f"  Backbone extracted with {num_backbone_nodes:,} core nodes ({num_backbone_nodes/num_nodes*100:.2f}% of total). ({time.time() - start_kcore:.2f} seconds)")

    # Slice edges that exist only within the backbone
    print("  Slicing backbone edges...")
    gdf_edges_backbone = gdf_edges[
        gdf_edges['source'].isin(backbone_vertices) & 
        gdf_edges['destination'].isin(backbone_vertices)
    ]
    
    # Free the full graph G to save VRAM before constructing G_backbone
    print("  Freeing full graph G to conserve VRAM...")
    del G
    cp.get_default_memory_pool().free_all_blocks()
    
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

    # --- LOUVAIN COMMUNITY SEEDING ---
    # FA2 from random init collapses into a mixed-density disk local minimum (see
    # massive_galaxy_static3.png: uniformly interleaved core, no continent separation).
    # 600 iterations cannot "unmix" 690k interleaved nodes. Instead, detect communities
    # on the GPU and pre-place each one in its own region so FA2 only refines boundaries.
    print("  Computing Louvain communities to seed continental positions...")
    start_louvain = time.time()
    parts, modularity = cugraph.louvain(G_backbone)
    num_comms = int(parts['partition'].nunique())
    print(f"  Louvain found {num_comms:,} communities (modularity {modularity:.4f}) in {time.time() - start_louvain:.2f}s.")

    # Persist backbone vertex -> community for community-coloring export.
    # (Peripheral nodes inherit their gateway's community after label propagation.)
    comm_backbone = parts[['vertex', 'partition']].rename(columns={'partition': 'community'}).copy()

    # Community centroids on a golden-angle spiral: uniform 2D packing, largest at center
    sizes = parts.groupby('partition').size().reset_index()
    sizes = sizes.rename(columns={sizes.columns[-1]: 'n'})
    sizes = sizes.sort_values('n', ascending=False).reset_index(drop=True)
    rank = cp.arange(len(sizes), dtype=cp.float64)
    golden_angle = np.pi * (3.0 - np.sqrt(5.0))
    # min_ring pushes the innermost (biggest) communities off dead-center, opening
    # a central void so they don't pile and overlap into a muddy core.
    ring = cp.sqrt(rank + min_ring)
    sizes['cx'] = spacing * ring * cp.cos(rank * golden_angle)
    sizes['cy'] = spacing * ring * cp.sin(rank * golden_angle)

    # Scatter members in a disk around their community centroid (deterministic hash jitter,
    # disk radius ~ sqrt(community size) for uniform node density)
    parts = parts.merge(sizes[['partition', 'n', 'cx', 'cy']], on='partition')
    n_max = float(sizes['n'].max())
    seed_cp = (cp.asarray(parts['vertex']).astype(cp.int64) * 1103515245 + 12345) & 0x7fffffff
    theta_cp = (seed_cp % 100000) / 100000.0 * 2.0 * np.pi
    disk_r = seed_disk * spacing * cp.sqrt(cp.asarray(parts['n']).astype(cp.float64) / n_max)
    r_cp = cp.sqrt((seed_cp // 100000 % 100000) / 100000.0) * disk_r
    parts['x'] = cudf.Series(cp.asarray(parts['cx']) + r_cp * cp.cos(theta_cp), index=parts.index)
    parts['y'] = cudf.Series(cp.asarray(parts['cy']) + r_cp * cp.sin(theta_cp), index=parts.index)

    seed_pos = parts[['vertex', 'x', 'y']].astype(np.float32)
    seed_pos['vertex'] = seed_pos['vertex'].astype(np.int32)
    del parts, sizes, seed_cp, theta_cp, r_cp, disk_r
    cp.get_default_memory_pool().free_all_blocks()

    pos_backbone = cugraph.force_atlas2(
        G_backbone,
        max_iter=it(600),  # 600 iterations for deep macro-continent relaxation
        pos_list=seed_pos,  # Louvain-seeded start: refine continents, don't unmix a disk
        lin_log_mode=True, 
        outbound_attraction_distribution=False, # hubs pull neighbors closer
        scaling_ratio=240.0, # wide separation for macro-continents (tripled from 80.0)
        strong_gravity_mode=False,
        gravity=0.2, # relatively high gravity to prevent drifting core components
        edge_weight_influence=0.4, # balanced edge weight influence
        prevent_overlapping=True, # prevent backbone overlap from the start
        vertex_radius=radius_gdf_backbone,
        verbose=True
    )
    print(f"  Backbone simulation complete. ({time.time() - start_backbone_sim:.2f} seconds)")
    
    # Clean up backbone graph memory immediately
    del G_backbone, radius_gdf_backbone, gdf_edges_backbone
    cp.get_default_memory_pool().free_all_blocks()

    # --- PHASE 2 PREPARATION: Gateway Ancestor Mapping ---
    print("Step 6: Running Multi-Source Label Propagation to map peripheral nodes to closest core gateways...")
    start_prop = time.time()
    
    # Filter out core-to-core edges to save VRAM.
    # We only need edges that have at least one peripheral node endpoint for core-periphery mapping.
    is_core_src = gdf_edges['source'].isin(backbone_vertices)
    is_core_dst = gdf_edges['destination'].isin(backbone_vertices)
    
    gdf_periph_edges = gdf_edges[~is_core_src | ~is_core_dst]
    gdf_periph_edges = gdf_periph_edges[['source', 'destination']] # Discard weights to save memory
    
    # Create bidirectional edge list for mapping neighbors
    gdf_edges_rev = gdf_periph_edges.rename(columns={"source": "destination", "destination": "source"})
    gdf_undir_edges = cudf.concat([gdf_periph_edges, gdf_edges_rev]).drop_duplicates(subset=['source', 'destination'])
    
    # Clean up intermediate dataframes to free VRAM immediately
    del gdf_periph_edges, gdf_edges_rev
    cp.get_default_memory_pool().free_all_blocks()
    
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

    # Assign every node the community of its backbone gateway (backbone nodes are
    # their own gateway). Build a dense num_nodes array of community ids (-1 = none).
    node_community = np.full(num_nodes, -1, dtype=np.int32)
    comm_join = closest_core[['vertex', 'gateway']].merge(
        comm_backbone.rename(columns={'vertex': 'gateway'}), on='gateway', how='left')
    cj_v = comm_join['vertex'].to_pandas().to_numpy(dtype=np.int32)
    cj_c = comm_join['community'].fillna(-1).to_pandas().to_numpy(dtype=np.int32)
    node_community[cj_v] = cj_c
    print(f"  Assigned communities to {int((node_community >= 0).sum()):,} nodes.")
    del comm_join

    # --- PHASE 2 PREPARATION: Vector-Offset Initialization ---
    print("Step 7: Initializing peripheral positions near core gateways with radial jitter...")
    # Merge closest_core with pos_backbone to align coordinates
    backbone_coords = pos_backbone.rename(columns={'vertex': 'gateway', 'x': 'gx', 'y': 'gy'})
    gateway_coords = closest_core.merge(backbone_coords, on='gateway', how='left')
    
    # Generate deterministic radial offsets based on vertex ID using Cupy
    vertex_ids_cp = cp.asarray(gateway_coords['vertex'])
    seed_cp = (vertex_ids_cp * 1103515245 + 12345) & 0x7fffffff
    theta_cp = (seed_cp % 1000) / 1000.0 * 2.0 * np.pi
    r_cp = 6.0 + (seed_cp // 1000 % 1000) / 1000.0 * 39.0 # tripled jitter to match 3x layout scale
    
    offset_x = r_cp * cp.cos(theta_cp)
    offset_y = r_cp * cp.sin(theta_cp)
    
    # Calculate starting coordinates
    init_pos = gateway_coords.copy()
    init_pos['x'] = init_pos['gx'] + cudf.Series(offset_x, index=init_pos.index)
    init_pos['y'] = init_pos['gy'] + cudf.Series(offset_y, index=init_pos.index)
    
    # For backbone vertices, they should keep their original backbone positions (no jitter)
    is_backbone = init_pos['vertex'].isin(backbone_vertices)
    init_pos.loc[is_backbone, 'x'] = init_pos.loc[is_backbone, 'gx']
    init_pos.loc[is_backbone, 'y'] = init_pos.loc[is_backbone, 'gy']
    
    # For unplaced nodes (isolated components, gateway == -1), scatter randomly
    unplaced_mask = init_pos['gateway'] == -1
    num_unplaced = unplaced_mask.sum()
    if num_unplaced > 0:
        std_val = float(pos_backbone['x'].std()) if len(pos_backbone) > 0 else 1000.0
        # Generate random coordinates using CuPy directly on the GPU
        rand_x = cp.random.uniform(-std_val * 3, std_val * 3, int(num_unplaced))
        rand_y = cp.random.uniform(-std_val * 3, std_val * 3, int(num_unplaced))
        init_pos.loc[unplaced_mask, 'x'] = rand_x
        init_pos.loc[unplaced_mask, 'y'] = rand_y
        
    print(f"  Initialized coordinates for {len(init_pos):,} nodes.")
    
    # Clean dataframe columns to just vertex, x, y
    init_pos = init_pos[['vertex', 'x', 'y']]
    
    # Clean up temp dataframes to conserve memory
    del gateway_coords, backbone_coords, gdf_undir_edges
    cp.get_default_memory_pool().free_all_blocks()

    # --- PHASE 2: Pinned Ingestion Simulation ---
    print("Step 8 [Phase 2]: Reconstructing full cuGraph...")
    G = cugraph.Graph(directed=False)
    G.from_cudf_edgelist(gdf_edges, source='source', destination='destination', edge_attr='weight')

    print("Step 8 [Phase 2]: Running Pinned Ingestion Simulation (250 iterations total)...")
    start_phase2 = time.time()
    
    # Format starting positions DataFrame
    current_pos = init_pos[['vertex', 'x', 'y']].astype(np.float32)
    current_pos['vertex'] = current_pos['vertex'].astype(np.int32)
    
    # Extract pinned core coordinates DataFrame to reuse for resets
    pinned_core_pos = pos_backbone[['vertex', 'x', 'y']].astype(np.float32)
    pinned_core_pos['vertex'] = pinned_core_pos['vertex'].astype(np.int32)
    
    num_steps = 10
    iters_per_step = it(25, floor=5)
    
    for step_idx in range(num_steps):
        step_start = time.time()
        print(f"  Step {step_idx + 1}/{num_steps}: Simulating {iters_per_step} iterations...")
        
        # Run ForceAtlas2 on the full graph with overlap prevention enabled
        current_pos = cugraph.force_atlas2(
            G,
            max_iter=iters_per_step,
            pos_list=current_pos,
            lin_log_mode=True,
            outbound_attraction_distribution=False,
            scaling_ratio=240.0, # tripled from 80.0
            strong_gravity_mode=False,
            gravity=0.05,  # lower gravity to allow peripheral trees to expand
            edge_weight_influence=0.4,
            prevent_overlapping=True,
            vertex_radius=radius_gdf,
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
    
    # Print diagnostics before Phase 3
    x_std = float(current_pos['x'].std())
    y_std = float(current_pos['y'].std())
    print(f"  [Diagnostics] Pre-Phase 3 coordinate spread - std(x): {x_std:.2f}, std(y): {y_std:.2f}")

    print("  Sub-phase 3A: Simulating 80 iterations with overlap prevention...")
    current_pos = cugraph.force_atlas2(
        G,
        max_iter=it(80),
        pos_list=current_pos,
        lin_log_mode=True,
        outbound_attraction_distribution=False,
        scaling_ratio=240.0,  # Keep high to maintain strong repulsion
        strong_gravity_mode=False,
        gravity=0.01,         # Low gravity to allow expansion
        edge_weight_influence=ewi3, # A/B-testable: 0.4 keeps mild weight signal, 0.0 = pure topology
        prevent_overlapping=True,  # Prevent overlap earlier
        vertex_radius=radius_gdf,
        verbose=True
    )
    
    # Print diagnostics after Phase 3A
    x_std = float(current_pos['x'].std())
    y_std = float(current_pos['y'].std())
    print(f"  [Diagnostics] Post-Phase 3A coordinate spread - std(x): {x_std:.2f}, std(y): {y_std:.2f}")

    print("  Sub-phase 3B: Simulating final 40 iterations with overlap prevention enabled...")
    final_pos_gdf = cugraph.force_atlas2(
        G,
        max_iter=it(40),
        pos_list=current_pos,
        lin_log_mode=True,
        outbound_attraction_distribution=False,
        scaling_ratio=240.0,
        strong_gravity_mode=False,
        gravity=0.01,
        edge_weight_influence=ewi3,
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
        if sample_frac < 1.0:
            # Smoke test: mark unsimulated nodes NaN so renders show only real layout output
            print(f"  [SMOKE TEST] Marking {len(orphans):,} unsimulated nodes as NaN (excluded from render)...")
            final_coords[orphans, :] = np.nan
        else:
            print(f"  Scattering {len(orphans):,} degree-0 orphans as background dust...")
            std_val = np.std(final_coords[valid_vertices])
            final_coords[orphans, 0] = np.random.uniform(-std_val * 3, std_val * 3, len(orphans))
            final_coords[orphans, 1] = np.random.uniform(-std_val * 3, std_val * 3, len(orphans))

    # Format: [uint32 N][(float x, float y, float views, float degree, float cat_id, float community) * N]
    # Column 5 (community) is new; readers that expect 5 columns still work via
    # auto-detected column count. -1 = no community (orphans / unmapped).
    with open(out_bin, "wb") as f:
        f.write(struct.pack("I", num_nodes))
        packed_data = np.zeros((num_nodes, 6), dtype=np.float32)
        packed_data[:, 0] = final_coords[:, 0]
        packed_data[:, 1] = final_coords[:, 1]
        packed_data[:, 2] = node_views
        packed_data[:, 3] = full_degrees
        packed_data[:, 4] = node_cats
        packed_data[:, 5] = node_community
        f.write(packed_data.tobytes())

    print(f"Done! Saved enriched coordinates to {out_bin} in {time.time() - start_export:.2f} seconds.")

    # Print final coordinates spread diagnostics (nanstd: smoke bins contain NaN rows)
    x_std = float(np.nanstd(final_coords[:, 0]))
    y_std = float(np.nanstd(final_coords[:, 1]))
    print(f"  [Diagnostics] Final coordinate spread (with orphans) - std(x): {x_std:.2f}, std(y): {y_std:.2f}")

    # Generate Matplotlib raw scatter plot diagnostic
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        print("  [Diagnostics] Plotting raw coordinate scatter (50k sample)...")
        sample_size = min(50000, len(valid_vertices))
        sample_idx = np.random.choice(valid_vertices, sample_size, replace=False)
        plt.figure(figsize=(10, 10))
        plt.scatter(final_coords[sample_idx, 0], final_coords[sample_idx, 1], s=0.5, alpha=0.5, c='red')
        plt.title(f"Layout Scatter (50k sample) | ewi3={ewi3} sample_frac={sample_frac} iters_scale={iters_scale}")
        plt.grid(True, alpha=0.3)
        plt.savefig(diag_png, dpi=150)
        plt.close()
        print(f"  [Diagnostics] Saved raw coordinates scatter plot to {diag_png}.")
    except Exception as ex:
        print(f"  [Diagnostics] Warning: Could not generate diagnostic plot: {ex}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Stage GPU Galaxy Layout Compiler (RAPIDS cuGraph)")
    parser.add_argument("--edges", type=str, default="edges_weighted.csv.gz", help="Gzipped edges CSV")
    parser.add_argument("--meta", type=str, default="metadata.csv", help="Node metadata CSV")
    parser.add_argument("--out", type=str, default="coordinates_rapids.bin", help="Output coordinates binary")
    parser.add_argument("--sample-frac", type=float, default=1.0, help="Edge subsample fraction for smoke tests (e.g. 0.03)")
    parser.add_argument("--iters-scale", type=float, default=1.0, help="Scale all FA2 iteration counts (e.g. 0.2 for smoke tests)")
    parser.add_argument("--ewi3", type=float, default=0.4, help="Phase 3 edge_weight_influence (A/B: 0.0 vs 0.4)")
    parser.add_argument("--diag", type=str, default="diagnostic_layout.png", help="Diagnostic scatter plot filename")
    parser.add_argument("--spacing", type=float, default=3000.0, help="Golden-spiral community spacing (lower = communities closer/more connected)")
    parser.add_argument("--seed-disk", type=float, default=2.0, help="Seed disk radius scale (lower = tighter communities, less overlap)")
    parser.add_argument("--min-ring", type=float, default=0.0, help="Central-void offset added to spiral rank (higher = bigger empty center)")
    args = parser.parse_args()

    compile_galaxy_multistage(
        edges_csv=args.edges, meta_csv=args.meta, out_bin=args.out,
        sample_frac=args.sample_frac, iters_scale=args.iters_scale,
        ewi3=args.ewi3, diag_png=args.diag,
        spacing=args.spacing, seed_disk=args.seed_disk, min_ring=args.min_ring
    )
