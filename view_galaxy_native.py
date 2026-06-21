import sys
import os
import sqlite3
import struct
import numpy as np

# Add graph-tool to path
gt_site_packages = "/opt/homebrew/opt/graph-tool/libexec/lib/python3.14/site-packages"
if gt_site_packages not in sys.path:
    sys.path.append(gt_site_packages)

try:
    from graph_tool.all import Graph, graph_draw, GraphView
except ImportError:
    print(f"Error: graph-tool not found in {gt_site_packages}")
    sys.exit(1)

def launch_native_viewer():
    structure_db = "wiki_graph_structure.db"
    bin_path = "coordinates.bin"

    if not os.path.exists(bin_path):
        print(f"Error: {bin_path} not found. Simulation must be finished.")
        return

    print("Step 1: Loading Graph Structure into graph-tool...")
    g = Graph(directed=False)
    conn = sqlite3.connect(structure_db)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM nodes")
    num_nodes = cursor.fetchone()[0]
    g.add_vertex(num_nodes)
    
    # To prevent OOM, we only load a subset of edges for the interactive window
    # graph-tool's Cairo backend cannot handle 100M edges interactively.
    # We will load the first 500,000 edges as a representative sample.
    print("  Loading interactive edge sample (500k edges)...")
    cursor.execute("SELECT source_idx, target_idx FROM links LIMIT 500000")
    g.add_edge_list(cursor.fetchall())
    conn.close()

    print("Step 2: Loading Coordinates into Property Map...")
    with open(bin_path, "rb") as f:
        num_nodes_bin = struct.unpack("I", f.read(4))[0]
        data = np.frombuffer(f.read(), dtype=np.float32)
        coords = data.reshape(-1, 2)
    
    # Create the property map graph-tool expects
    pos = g.new_vertex_property("vector<double>")
    for i in range(num_nodes):
        pos[g.vertex(i)] = coords[i]

    print("\nStep 3: Launching graph-tool Interactive Window...")
    print("Controls:")
    print("  - Left Mouse: Drag nodes")
    print("  - Mouse Wheel: Zoom")
    print("  - Right Click: Context Menu")
    print("\nNote: Close the window to exit the script.")
    
    # We use GraphView to only show nodes that have coordinates (all of them in this case)
    # and to potentially filter further if it's slow.
    graph_draw(
        g, 
        pos=pos, 
        interactive=True,
        window_title="Wikipedia Galaxy - Native graph-tool",
        vertex_size=2,
        vertex_fill_color=[0.2, 0.6, 1.0, 0.8], # Blue galaxy nodes
        edge_pen_width=0.5,
        bg_color=[0, 0, 0, 1] # Black background
    )

if __name__ == "__main__":
    launch_native_viewer()
