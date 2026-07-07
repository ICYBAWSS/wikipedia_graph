# RunPod GPU Pipeline

Everything needed to run the layout compile + render on a RAPIDS cloud GPU.
Input data (edges/metadata) is fetched from Hugging Face automatically — nothing
else from this repo is required on the pod.

## Fast clone (this folder only, ~50 KB instead of the full 125 MB repo)

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/ICYBAWSS/wikipedia_graph.git
cd wikipedia_graph
git sparse-checkout set runpod
cd runpod
```

## Usage

Use a `rapidsai/*` docker image (RAPIDS >= 24.x for `prevent_overlapping`/`vertex_radius` FA2 support).

```bash
# 1. Smoke test (~15-20 min): validates env + APIs, runs the pipeline on a 3%
#    edge sample twice (Phase 3 edge_weight_influence 0.4 vs 0.0), renders both.
./runpod_smoke_test.sh

# 2. Eyeball smoke_render_ewi04.png vs smoke_render_ewi00.png, pick the winner.

# 3. Full 8M-node run (+ optional artifact upload to HF):
EWI3=0.4 ./runpod_full_run.sh
# or:
UPLOAD=1 HF_TOKEN=hf_xxx EWI3=0.0 ./runpod_full_run.sh
```

## Files

| File | Purpose |
| :--- | :--- |
| `runpod_check_env.py` | 1-min preflight: RAPIDS versions, FA2 signature, Louvain/Datashader smoke on a toy graph. Fails fast before any money is spent. |
| `compile_galaxy_multistage.py` | Multi-stage FA2 layout compiler (Louvain-seeded). Flags: `--sample-frac --iters-scale --ewi3 --out --diag` |
| `render_galaxy_gpu.py` | GPU Datashader renderer. Flags: `--bin --width --height --edge_sample --output` |
| `runpod_smoke_test.sh` | Orchestrates the cheap validation run + ewi A/B |
| `runpod_full_run.sh` | Orchestrates the production run + optional HF upload |

## Outputs

- `coordinates_rapids.bin` — layout binary consumed by the WebGL visualizer
- `diagnostic_layout.png` — 50k-node scatter sanity check
- `massive_galaxy_full.png` — 16k final render
- `smoke_test.log` / `full_run.log` — complete logs
