import subprocess
import time
import shutil
import os

def main():
    # Springs and gravities match the slider defaults and ranges of index.html
    # k1_g1 is the exact default layout setting (spring=0.15, gravity=0.05)
    springs = [0.05, 0.15, 0.30]
    gravities = [0.01, 0.05, 0.15]
    
    start_total = time.time()
    
    for ki, spring in enumerate(springs):
        for gi, gravity in enumerate(gravities):
            out_name = f"coords_k{ki}_g{gi}.bin"
            print(f"\n==================================================")
            print(f"Generating {out_name} (spring={spring}, gravity={gravity})")
            print(f"==================================================")
            
            # 1. Reset baseline coordinates.bin and database to clean nebula layout
            print("Resetting baseline coordinates...")
            subprocess.run(["python3", "update_coordinates_nebula.py"])
            
            # 2. Run the C simulator to relax coordinates
            if ki == 1 and gi == 1:
                # For the default layout (k1_g1), write to coordinates.bin and update the SQLite DB (1)
                print(f"Running layout_simulator for default layout (coordinates.bin) and updating DB...")
                subprocess.run(["./layout_simulator", str(spring), str(gravity), "coordinates.bin", "1", "80"])
                # Also copy coordinates.bin to coords_k1_g1.bin
                shutil.copyfile("coordinates.bin", "coords_k1_g1.bin")
            else:
                # For other layouts, write to out_name and skip DB update (0)
                print(f"Running layout_simulator for {out_name}...")
                subprocess.run(["./layout_simulator", str(spring), str(gravity), out_name, "0", "80"])
            
    print(f"\nAll 9 grid layouts generated successfully in {time.time() - start_total:.2f}s!")

if __name__ == "__main__":
    main()
