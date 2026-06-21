import subprocess
import os
import sys

def main():
    # User's preferred layout settings
    spring = 0.50
    gravity = 0.37
    iterations = 80
    distance = 253.0
    charge = -1878.0
    collision = 23.0
    
    print("\n==================================================")
    print("Wikipedia Graph Visualizer - Generate Single Layout")
    print("==================================================")
    print(f"Spring/Link Strength: {spring}")
    print(f"Gravity Constant:    {gravity}")
    print(f"Iterations:          {iterations}")
    print(f"Link Distance:       {distance}")
    print(f"Charge Strength:     {charge}")
    print(f"Collision Radius:    {collision}")
    print("==================================================\n")
    
    # 1. Compile layout_simulator.c
    print("Step 1: Compiling layout_simulator.c...")
    cmd_compile = ["gcc", "-O3", "-march=native", "layout_simulator.c", "-lsqlite3", "-lpthread", "-o", "layout_simulator"]
    res = subprocess.run(cmd_compile)
    if res.returncode != 0:
        print("Error compiling layout_simulator.c")
        sys.exit(1)
    print("Compilation successful.")
    
    # 2. Reset baseline coordinates
    print("\nStep 2: Resetting baseline coordinates via update_coordinates_nebula.py...")
    res = subprocess.run(["python3", "update_coordinates_nebula.py"])
    if res.returncode != 0:
        print("Error resetting baseline coordinates")
        sys.exit(1)
        
    # 3. Run layout simulation
    print("\nStep 3: Running layout simulator...")
    cmd_sim = [
        "./layout_simulator",
        str(spring),
        str(gravity),
        "coordinates.bin",
        "1", # update_db_flag
        str(iterations),
        str(distance),
        str(charge),
        str(collision)
    ]
    print(f"Command: {' '.join(cmd_sim)}")
    res = subprocess.run(cmd_sim)
    if res.returncode != 0:
        print("Error running layout simulator")
        sys.exit(1)
        
    print("\nLayout generation complete!")

if __name__ == "__main__":
    main()
