#!/usr/bin/env python
"""
RunPod RAPIDS environment preflight check.

Validates every API assumption the layout/render pipeline makes, on a tiny
throwaway graph, BEFORE any expensive data download or simulation starts.
Exits 0 on PASS, 1 on FAIL with a clear reason.

Usage: python runpod_check_env.py
"""
import sys
import inspect

FAILURES = []


def check(name, fn):
    try:
        result = fn()
        print(f"  [PASS] {name}" + (f" -> {result}" if result else ""))
        return True
    except Exception as ex:
        print(f"  [FAIL] {name}: {type(ex).__name__}: {ex}")
        FAILURES.append(name)
        return False


def main():
    print("=== RunPod RAPIDS Preflight Check ===\n")

    # --- 1. Imports & versions ---
    print("1. Imports & versions:")
    try:
        import numpy as np
        import cupy as cp
        import cudf
        import cugraph
        print(f"  [PASS] numpy {np.__version__}, cupy {cp.__version__}, "
              f"cudf {cudf.__version__}, cugraph {cugraph.__version__}")
    except ImportError as ex:
        print(f"  [FAIL] RAPIDS import: {ex}")
        print("\nRESULT: FAIL (RAPIDS not installed — use a rapidsai/* docker image)")
        sys.exit(1)

    check("datashader import", lambda: __import__("datashader") and None)
    check("matplotlib import", lambda: __import__("matplotlib") and None)

    # --- 2. GPU visibility ---
    print("\n2. GPU:")
    def gpu_info():
        dev = cp.cuda.Device(0)
        props = cp.cuda.runtime.getDeviceProperties(0)
        free_b, total_b = dev.mem_info
        return (f"{props['name'].decode()} | VRAM {total_b/1e9:.1f} GB total, "
                f"{free_b/1e9:.1f} GB free")
    check("CUDA device", gpu_info)

    # --- 3. force_atlas2 signature: params the compiler passes ---
    print("\n3. cugraph.force_atlas2 signature:")
    sig = inspect.signature(cugraph.force_atlas2)
    needed = ["pos_list", "prevent_overlapping", "vertex_radius",
              "edge_weight_influence", "lin_log_mode",
              "outbound_attraction_distribution", "scaling_ratio", "gravity"]
    for p in needed:
        if p in sig.parameters:
            print(f"  [PASS] param '{p}'")
        else:
            print(f"  [FAIL] param '{p}' MISSING — this cugraph version cannot run the compiler as written")
            FAILURES.append(f"force_atlas2 param {p}")

    # --- 4. Functional smoke on a tiny graph (two cliques + bridge) ---
    print("\n4. Functional smoke test (12-node graph, exact pipeline params):")
    import numpy as np
    src, dst = [], []
    for base in (0, 6):  # two 6-cliques
        for i in range(6):
            for j in range(i + 1, 6):
                src.append(base + i); dst.append(base + j)
    src.append(0); dst.append(6)  # bridge
    gdf = cudf.DataFrame({
        "source": cudf.Series(src, dtype=np.int32),
        "destination": cudf.Series(dst, dtype=np.int32),
        "weight": cudf.Series([1.5] * len(src), dtype=np.float32),
    })
    G = cugraph.Graph(directed=False)
    G.from_cudf_edgelist(gdf, source="source", destination="destination", edge_attr="weight")

    # 4a. Louvain: return shape + column names the compiler depends on
    def louvain_check():
        result = cugraph.louvain(G)
        parts, mod = result if isinstance(result, tuple) else (result, -1.0)
        cols = list(parts.columns)
        assert "vertex" in cols and "partition" in cols, f"unexpected columns: {cols}"
        n = int(parts["partition"].nunique())
        assert n >= 2, f"expected >=2 communities on two cliques, got {n}"
        return f"{n} communities, modularity {float(mod):.3f}, columns {cols}"
    check("cugraph.louvain", louvain_check)

    # 4b. FA2 with the exact kwargs used in every compiler phase
    def fa2_check():
        pos0 = cudf.DataFrame({
            "vertex": cudf.Series(range(12), dtype=np.int32),
            "x": cudf.Series(np.random.rand(12).astype(np.float32)),
            "y": cudf.Series(np.random.rand(12).astype(np.float32)),
        })
        radius = cudf.DataFrame({
            "vertex": cudf.Series(range(12), dtype=np.int32),
            "radius": cudf.Series([1.0] * 12, dtype=np.float32),
        })
        pos = cugraph.force_atlas2(
            G, max_iter=20, pos_list=pos0,
            lin_log_mode=True, outbound_attraction_distribution=False,
            scaling_ratio=240.0, strong_gravity_mode=False, gravity=0.05,
            edge_weight_influence=0.4, prevent_overlapping=True,
            vertex_radius=radius, verbose=False,
        )
        cols = list(pos.columns)
        assert {"vertex", "x", "y"}.issubset(set(cols)), f"unexpected columns: {cols}"
        spread = float(pos["x"].std())
        assert np.isfinite(spread) and spread > 0, f"degenerate layout, std(x)={spread}"
        return f"ok, output columns {cols}, std(x)={spread:.3f}"
    check("cugraph.force_atlas2 (pinning/overlap/radius kwargs)", fa2_check)

    # 4c. cuDF ops the compiler relies on
    def cudf_ops_check():
        s = gdf.sample(frac=0.5, random_state=42)
        assert len(s) > 0
        g = gdf.groupby("source").size().reset_index()
        _ = cp.clip(cp.log1p(cp.asarray(gdf["weight"])), 0.0, 2.0)
        return "sample/groupby/asarray ok"
    check("cuDF ops (sample, groupby, cp.asarray)", cudf_ops_check)

    # 4d. Datashader GPU path used by the renderer
    def datashader_check():
        import datashader as ds
        import datashader.transfer_functions as tf
        pts = cudf.DataFrame({
            "x": cudf.Series(np.random.rand(1000).astype(np.float32)),
            "y": cudf.Series(np.random.rand(1000).astype(np.float32)),
        })
        cvs = ds.Canvas(plot_width=100, plot_height=100, x_range=(0, 1), y_range=(0, 1))
        agg = cvs.points(pts, "x", "y", ds.count())
        img = tf.shade(agg, cmap="#ffffff", how="eq_hist", alpha=150, min_alpha=8)
        img = tf.spread(img, px=1)
        return "cudf points -> shade(eq_hist, alpha) -> spread ok"
    check("datashader GPU aggregation", datashader_check)

    # 4e. Winner-take-all render path (mirrors render_galaxy_gpu.py exactly):
    #     maximum_filter spread -> argsort eq_hist -> packed RGBA -> host Image -> CPU tf.stack
    def wta_check():
        import datashader as ds
        import datashader.transfer_functions as tf
        import xarray as xr
        from cupyx.scipy.ndimage import maximum_filter
        pts = cudf.DataFrame({
            "x": cudf.Series(np.random.rand(500).astype(np.float32)),
            "y": cudf.Series(np.random.rand(500).astype(np.float32)),
        })
        cvs = ds.Canvas(plot_width=64, plot_height=64, x_range=(0, 1), y_range=(0, 1))
        agg = cvs.points(pts, "x", "y", ds.count())
        total = maximum_filter(agg.data.astype(cp.float32), size=3)  # GPU spread
        mask = total > 0
        vals = total[mask]
        ranks = cp.argsort(cp.argsort(vals)).astype(cp.float32) / max(int(mask.sum()) - 1, 1)
        inten = cp.zeros(total.shape, dtype=cp.float32)
        inten[mask] = ranks
        packed = ((inten * 255).astype(cp.uint32)
                  | ((inten * 255).astype(cp.uint32) << 8)
                  | (mask.astype(cp.uint32) * 255 << 24)).get()  # -> host numpy
        coords = {k: np.asarray(v) for k, v in agg.coords.items()}
        img_a = tf.Image(xr.DataArray(packed, coords=coords, dims=agg.dims))
        # host edges image + CPU stack (the real final-composite path).
        # tf.shade may return a host- or device-backed image depending on the
        # datashader build; guard the .get() exactly as the renderer does.
        edge = tf.shade(cvs.points(pts, "x", "y", ds.count()), cmap="#ffffff", how="eq_hist")
        edge_data = edge.data.get() if hasattr(edge.data, "get") else edge.data
        edge = tf.Image(xr.DataArray(edge_data, coords=coords, dims=edge.dims))
        stacked = tf.stack(edge, img_a, how="over")
        assert not hasattr(stacked.data, "get"), "final composite must be host-side"
        return "maximum_filter spread -> packed RGBA -> host Image -> CPU tf.stack ok"
    check("winner-take-all render path", wta_check)

    # --- Result ---
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("RESULT: PASS — environment is safe for the full pipeline.")
    sys.exit(0)


if __name__ == "__main__":
    main()
