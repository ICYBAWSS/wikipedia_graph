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
                              spacing=3000.0, seed_disk=2.0, min_ring=0.0,
                              seed_mode='communities', layout_mode='multistage',
                              fa2_scaling=2.0, fa2_gravity=1.0, fa2_iters=1000,
                              umap_neighbors=15, umap_min_dist=0.1, umap_metric='cosine',
                              umap_dims=128):
    # Scale iteration counts for smoke-test runs (--iters-scale 0.2 => 20% of iterations)
    def it(n, floor=10):
        return max(floor, int(round(n * iters_scale)))

    # Physics regime. 'communities' uses aggressive separation (high repulsion +
    # overlap prevention) which needs disk-seeding or it collapses into rings.
    # 'organic' uses standard force-directed physics (low repulsion, real gravity,
    # NO overlap prevention) so ForceAtlas2 forms the natural filament/spike web.
    WEB = (seed_mode == 'organic')
    P_SCALING = 30.0 if WEB else 240.0
    P_OVERLAP = (not WEB)
    # OAD must be FALSE for the web look: hubs pull their neighbors into tight
    # radial stars (the spikes in the reference). TRUE distributes the pull and
    # smears everything into a uniform blob (which is what the first attempt did).
    P_OAD = False
    def P_GRAV(community_value):
        return 1.0 if WEB else community_value
    def P_RADIUS(rgdf):
        return rgdf if P_OVERLAP else None

    print("--- Wikipedia Galaxy Compiler (Multi-Stage Layout Mode) ---")
    print(f"  Physics: seed_mode={seed_mode} scaling={P_SCALING} overlap={P_OVERLAP} oad={P_OAD}")
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

    # ===================================================================
    # SIMPLE LAYOUT: single-pass ForceAtlas2 on the full graph — replicates
    # the frontend's graphology force layout (which produces the reference web
    # for 1000 nodes) but precomputed on GPU for all 8M. No backbone/gateway/
    # blossom phases (that machinery is what collapses into rings/blobs).
    # ===================================================================
    if layout_mode == 'simple':
        print("=== SIMPLE LAYOUT: single-pass ForceAtlas2 (frontend-style web) ===")
        print("  Louvain on full graph for community coloring...")
        parts, mod = cugraph.louvain(G)
        print(f"  Louvain: {int(parts['partition'].nunique())} communities, modularity {mod:.4f}")
        node_community = np.full(num_nodes, -1, dtype=np.int32)
        pv = parts['vertex'].to_pandas().to_numpy(dtype=np.int32)
        pc = parts['partition'].to_pandas().to_numpy(dtype=np.int32)
        node_community[pv] = pc
        del parts
        cp.get_default_memory_pool().free_all_blocks()

        print(f"  Running single-pass ForceAtlas2: scaling={fa2_scaling} gravity={fa2_gravity} iters={it(fa2_iters)}...")
        t0 = time.time()
        pos = cugraph.force_atlas2(
            G,
            max_iter=it(fa2_iters),
            lin_log_mode=True,                        # clusters correspond to modularity
            outbound_attraction_distribution=False,   # hubs pull neighbors into radial spikes
            scaling_ratio=fa2_scaling,
            strong_gravity_mode=False,
            gravity=fa2_gravity,
            edge_weight_influence=1.0,
            prevent_overlapping=False,                # overlap prevention makes rings
            barnes_hut_optimize=True,
            verbose=True,
        )
        print(f"  FA2 complete in {time.time() - t0:.1f}s.")

        pos = pos.sort_values('vertex')
        xs = pos['x'].to_pandas().to_numpy(dtype=np.float32)
        ys = pos['y'].to_pandas().to_numpy(dtype=np.float32)
        vv = pos['vertex'].to_pandas().to_numpy(dtype=np.int32)
        final_coords = np.zeros((num_nodes, 2), dtype=np.float32)
        final_coords[vv, 0] = xs
        final_coords[vv, 1] = ys
        mask = np.ones(num_nodes, dtype=bool); mask[vv] = False
        orphans = np.where(mask)[0]
        if len(orphans) > 0:
            if sample_frac < 1.0:
                final_coords[orphans, :] = np.nan
            else:
                sv = float(np.nanstd(final_coords[vv]))
                final_coords[orphans, 0] = np.random.uniform(-sv * 3, sv * 3, len(orphans))
                final_coords[orphans, 1] = np.random.uniform(-sv * 3, sv * 3, len(orphans))

        with open(out_bin, "wb") as f:
            f.write(struct.pack("I", num_nodes))
            pd6 = np.zeros((num_nodes, 6), dtype=np.float32)
            pd6[:, 0] = final_coords[:, 0]; pd6[:, 1] = final_coords[:, 1]
            pd6[:, 2] = node_views; pd6[:, 3] = full_degrees
            pd6[:, 4] = node_cats; pd6[:, 5] = node_community
            f.write(pd6.tobytes())
        print(f"  Wrote {out_bin} (6-col with community).")

        x_std = float(np.nanstd(final_coords[:, 0])); y_std = float(np.nanstd(final_coords[:, 1]))
        print(f"  [Diagnostics] spread std(x)={x_std:.1f} std(y)={y_std:.1f}")
        try:
            import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
            idx = np.random.choice(vv, min(50000, len(vv)), replace=False)
            plt.figure(figsize=(10, 10))
            plt.scatter(final_coords[idx, 0], final_coords[idx, 1], s=0.4, alpha=0.5, c='red')
            plt.title(f"Simple FA2 | scaling={fa2_scaling} gravity={fa2_gravity} sample={sample_frac}")
            plt.savefig(diag_png, dpi=150); plt.close()
            print(f"  [Diagnostics] saved {diag_png}")
        except Exception as ex:
            print(f"  [Diagnostics] warn: {ex}")
        return

    # ===================================================================
    # UMAP LAYOUT: structural embedding instead of a force layout.
    # FA2 hairballs on scale-free graphs (hubs sink to the centroid) and the
    # community/disk-seed regime only escapes that by IMPOSING geometry
    # (golden-spiral rings + overlap-prevention annuli = the "artificial rings").
    # UMAP has neither failure mode: position emerges from neighbor similarity,
    # min_dist enforces a spacing floor so hubs can't collapse, and negative
    # sampling separates clusters — organic islands, no imposed rings/blob.
    # Embedding = each node's weighted adjacency row; cosine similarity = "share
    # neighbors". UMAP builds its own kNN over those rows (not the raw edges),
    # so it captures structural neighborhoods, not just direct links.
    # ===================================================================
    if layout_mode == 'umap':
        print("=== UMAP LAYOUT: structural embedding (no imposed geometry) ===")
        from cuml.manifold import UMAP
        import cupyx.scipy.sparse as cusp

        print("  Louvain on full graph for community coloring...")
        parts, mod = cugraph.louvain(G)
        print(f"  Louvain: {int(parts['partition'].nunique())} communities, modularity {mod:.4f}")
        node_community = np.full(num_nodes, -1, dtype=np.int32)
        pv = parts['vertex'].to_pandas().to_numpy(dtype=np.int32)
        pc = parts['partition'].to_pandas().to_numpy(dtype=np.int32)
        node_community[pv] = pc
        del parts
        cp.get_default_memory_pool().free_all_blocks()

        # Symmetric weighted adjacency, compacted to nodes that actually have edges
        # (degree-0 orphans have no structural signal — scattered as dust below).
        # weight is already log1p-clamped (line ~86), so it's a bounded similarity.
        src = gdf_edges['source'].values.astype(cp.int32)
        dst = gdf_edges['destination'].values.astype(cp.int32)
        w   = gdf_edges['weight'].values.astype(cp.float32)
        present = cp.unique(cp.concatenate([src, dst]))          # compact node set (degree>0)
        m = int(present.size)
        rows = cp.searchsorted(present, cp.concatenate([src, dst])).astype(cp.int32)
        cols = cp.searchsorted(present, cp.concatenate([dst, src])).astype(cp.int32)
        vals = cp.concatenate([w, w])
        A = cusp.coo_matrix((vals, (rows, cols)), shape=(m, m)).tocsr()
        del src, dst, w, rows, cols, vals
        cp.get_default_memory_pool().free_all_blocks()
        print(f"  Adjacency built: {m:,} nodes with edges, {A.nnz:,} nnz.")

        # cuml UMAP can't do sparse kNN on millions of rows (sparse nn_descent
        # supports no metrics; sparse brute-force needs a fixed ~16GB workspace and
        # OOMs). So first reduce each node to a dense low-dim structural signature
        # via a 2-step degree-normalized random-walk diffusion of a random matrix
        # (Johnson-Lindenstrauss): E = P·P·R with P = D^-1(A+I). Nodes with similar
        # neighborhoods get similar rows; SpMM is ~2s and O(m·D) memory. Then dense
        # nn_descent UMAP (fast, memory-safe, scales to 8M). Rows are L2-normalized
        # so euclidean distance ≡ cosine similarity of the diffusion signatures.
        D = umap_dims
        print(f"  Building {D}-dim diffusion embedding (2-hop random-walk projection)...")
        t0 = time.time()
        A = A + cusp.identity(m, dtype=cp.float32, format='csr')       # self-loops
        deg = cp.asarray(A.sum(axis=1)).ravel()
        P = cusp.diags((1.0 / cp.maximum(deg, 1e-6)).astype(cp.float32)) @ A   # row-stochastic
        del A; cp.get_default_memory_pool().free_all_blocks()
        rng = cp.random.RandomState(42)
        R = (rng.standard_normal((m, D)) / cp.sqrt(cp.float32(D))).astype(cp.float32)
        E = P @ (P @ R)                                               # 2-step diffusion
        nrm = cp.linalg.norm(E, axis=1, keepdims=True)
        E = (E / cp.maximum(nrm, 1e-6)).astype(cp.float32)
        del P, R; cp.get_default_memory_pool().free_all_blocks()
        print(f"  Embedding ready {E.shape} in {time.time() - t0:.1f}s.")

        print(f"  Running UMAP: n_neighbors={umap_neighbors} min_dist={umap_min_dist} metric=euclidean (nn_descent)...")
        t0 = time.time()
        emb = UMAP(n_neighbors=umap_neighbors, min_dist=umap_min_dist,
                   metric='euclidean', build_algo='nn_descent', verbose=True).fit_transform(E)
        emb = cp.asnumpy(emb)
        del E; cp.get_default_memory_pool().free_all_blocks()
        print(f"  UMAP complete in {time.time() - t0:.1f}s.")

        present_np = cp.asnumpy(present)
        final_coords = np.zeros((num_nodes, 2), dtype=np.float32)
        final_coords[present_np, 0] = emb[:, 0]
        final_coords[present_np, 1] = emb[:, 1]

        # Degree-0 orphans: NaN in smoke mode (excluded from render), dust otherwise
        mask = np.ones(num_nodes, dtype=bool); mask[present_np] = False
        orphans = np.where(mask)[0]
        if len(orphans) > 0:
            if sample_frac < 1.0:
                final_coords[orphans, :] = np.nan
            else:
                sv = float(np.nanstd(final_coords[present_np]))
                final_coords[orphans, 0] = np.random.uniform(-sv * 3, sv * 3, len(orphans))
                final_coords[orphans, 1] = np.random.uniform(-sv * 3, sv * 3, len(orphans))

        with open(out_bin, "wb") as f:
            f.write(struct.pack("I", num_nodes))
            pd6 = np.zeros((num_nodes, 6), dtype=np.float32)
            pd6[:, 0] = final_coords[:, 0]; pd6[:, 1] = final_coords[:, 1]
            pd6[:, 2] = node_views; pd6[:, 3] = full_degrees
            pd6[:, 4] = node_cats; pd6[:, 5] = node_community
            f.write(pd6.tobytes())
        print(f"  Wrote {out_bin} (6-col with community).")

        x_std = float(np.nanstd(final_coords[:, 0])); y_std = float(np.nanstd(final_coords[:, 1]))
        print(f"  [Diagnostics] spread std(x)={x_std:.1f} std(y)={y_std:.1f}")
        try:
            import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
            idx = np.random.choice(present_np, min(50000, m), replace=False)
            plt.figure(figsize=(10, 10))
            plt.scatter(final_coords[idx, 0], final_coords[idx, 1], s=0.4, alpha=0.5, c='red')
            plt.title(f"UMAP | n_neighbors={umap_neighbors} min_dist={umap_min_dist} sample={sample_frac}")
            plt.savefig(diag_png, dpi=150); plt.close()
            print(f"  [Diagnostics] saved {diag_png}")
        except Exception as ex:
            print(f"  [Diagnostics] warn: {ex}")
        return

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

    if seed_mode == 'organic':
        # Organic mode: skip community disk-seeding entirely. Let ForceAtlas2 find
        # the natural filament/spike structure from its own init (the classic
        # force-directed "web" look). Louvain is still computed for the community
        # column, just not used to pre-place nodes.
        print("  Seed mode: ORGANIC — FA2 finds natural web structure (no disk-seeding).")
        seed_pos = None
        del parts
        cp.get_default_memory_pool().free_all_blocks()
    else:
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
        pos_list=seed_pos,  # Louvain-seeded start (None in organic mode)
        lin_log_mode=True,
        outbound_attraction_distribution=P_OAD,
        scaling_ratio=P_SCALING,
        strong_gravity_mode=False,
        gravity=P_GRAV(0.2),
        edge_weight_influence=0.4, # balanced edge weight influence
        prevent_overlapping=P_OVERLAP,
        vertex_radius=P_RADIUS(radius_gdf_backbone),
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
            outbound_attraction_distribution=P_OAD,
            scaling_ratio=P_SCALING,
            strong_gravity_mode=False,
            gravity=P_GRAV(0.05),
            edge_weight_influence=0.4,
            prevent_overlapping=P_OVERLAP,
            vertex_radius=P_RADIUS(radius_gdf),
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
        outbound_attraction_distribution=P_OAD,
        scaling_ratio=P_SCALING,
        strong_gravity_mode=False,
        gravity=P_GRAV(0.01),
        edge_weight_influence=ewi3, # A/B-testable: 0.4 keeps mild weight signal, 0.0 = pure topology
        prevent_overlapping=P_OVERLAP,
        vertex_radius=P_RADIUS(radius_gdf),
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
        outbound_attraction_distribution=P_OAD,
        scaling_ratio=P_SCALING,
        strong_gravity_mode=False,
        gravity=P_GRAV(0.01),
        edge_weight_influence=ewi3,
        prevent_overlapping=P_OVERLAP,
        vertex_radius=P_RADIUS(radius_gdf),
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
    parser.add_argument("--seed-mode", type=str, default="communities", choices=["communities", "organic"], help="'communities' = disk-seed per Louvain community; 'organic' = let FA2 find natural web structure")
    parser.add_argument("--layout", type=str, default="multistage", choices=["multistage", "simple", "umap"], help="'umap' = structural embedding (no hairball/rings); 'simple' = single-pass FA2 web; 'multistage' = 4-phase community pipeline")
    parser.add_argument("--fa2-scaling", type=float, default=2.0, help="[simple] FA2 scaling_ratio (lower = tighter clusters)")
    parser.add_argument("--fa2-gravity", type=float, default=1.0, help="[simple] FA2 gravity (higher = tighter overall)")
    parser.add_argument("--fa2-iters", type=int, default=1000, help="[simple] FA2 iterations")
    parser.add_argument("--umap-neighbors", type=int, default=15, help="[umap] n_neighbors (lower = more local/fragmented, higher = smoother global)")
    parser.add_argument("--umap-min-dist", type=float, default=0.1, help="[umap] min_dist spacing floor (higher = more even spread, kills hub collapse)")
    parser.add_argument("--umap-metric", type=str, default="cosine", help="[umap] (unused: diffusion embedding uses euclidean on L2-normalized rows)")
    parser.add_argument("--umap-dims", type=int, default=128, help="[umap] diffusion embedding dimensionality fed to UMAP")
    args = parser.parse_args()

    compile_galaxy_multistage(
        edges_csv=args.edges, meta_csv=args.meta, out_bin=args.out,
        sample_frac=args.sample_frac, iters_scale=args.iters_scale,
        ewi3=args.ewi3, diag_png=args.diag,
        spacing=args.spacing, seed_disk=args.seed_disk, min_ring=args.min_ring,
        seed_mode=args.seed_mode, layout_mode=args.layout,
        fa2_scaling=args.fa2_scaling, fa2_gravity=args.fa2_gravity, fa2_iters=args.fa2_iters,
        umap_neighbors=args.umap_neighbors, umap_min_dist=args.umap_min_dist, umap_metric=args.umap_metric,
        umap_dims=args.umap_dims
    )
