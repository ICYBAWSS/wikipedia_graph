#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <math.h>
#include <time.h>
#include <sqlite3.h>

// We'll determine NUM_NODES dynamically
uint32_t num_nodes = 0;
uint64_t num_links = 0;

// Simulation parameters
const int num_iterations = 20;
const float damping = 0.85f;
const float max_speed = 50.0f;

int main(int argc, char *argv[]) {
    if (argc < 5) {
        printf("Usage: %s <spring_constant> <gravity_constant> <output_bin_name> <update_db_flag>\n", argv[0]);
        return 1;
    }

    float spring_constant = atof(argv[1]);
    float gravity_constant = atof(argv[2]);
    const char *output_bin_name = argv[3];
    int update_db_flag = atoi(argv[4]);

    sqlite3 *db;
    sqlite3_stmt *stmt;
    int rc;

    printf("Step 1: Opening database wiki_simulation.db...\n");
    rc = sqlite3_open("wiki_simulation.db", &db);
    if (rc) {
        fprintf(stderr, "Can't open database: %s\n", sqlite3_errmsg(db));
        return 1;
    }

    // 1. Get total node count
    rc = sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM nodes", -1, &stmt, NULL);
    if (rc == SQLITE_OK && sqlite3_step(stmt) == SQLITE_ROW) {
        num_nodes = sqlite3_column_int(stmt, 0);
    }
    sqlite3_finalize(stmt);
    printf("Found %u nodes in database.\n", num_nodes);

    // 2. Get total link count
    rc = sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM links", -1, &stmt, NULL);
    if (rc == SQLITE_OK && sqlite3_step(stmt) == SQLITE_ROW) {
        num_links = sqlite3_column_int64(stmt, 0);
    }
    sqlite3_finalize(stmt);
    printf("Found %llu links in database.\n", (unsigned long long)num_links);

    if (num_nodes == 0) {
        printf("Error: No nodes found.\n");
        sqlite3_close(db);
        return 1;
    }

    // 3. Allocate memory
    float *posX = malloc(num_nodes * sizeof(float));
    float *posY = malloc(num_nodes * sizeof(float));
    float *velX = calloc(num_nodes, sizeof(float));
    float *velY = calloc(num_nodes, sizeof(float));
    uint32_t *sources = malloc(num_links * sizeof(uint32_t));
    uint32_t *targets = malloc(num_links * sizeof(uint32_t));
    uint32_t *inDegrees = malloc(num_nodes * sizeof(uint32_t));

    if (!posX || !posY || !velX || !velY || !sources || !targets || !inDegrees) {
        fprintf(stderr, "Failed to allocate memory.\n");
        if (posX) free(posX);
        if (posY) free(posY);
        if (velX) free(velX);
        if (velY) free(velY);
        if (sources) free(sources);
        if (targets) free(targets);
        if (inDegrees) free(inDegrees);
        return 1;
    }

    // 4. Load baseline coordinates or initialize
    printf("Step 2: Initializing coordinates...\n");
    FILE *fbin = fopen("coordinates.bin", "rb");
    uint32_t bin_count = 0;
    if (fbin) {
        fread(&bin_count, sizeof(uint32_t), 1, fbin);
        if (bin_count == num_nodes) {
            for (uint32_t i = 0; i < num_nodes; i++) {
                fread(&posX[i], sizeof(float), 1, fbin);
                fread(&posY[i], sizeof(float), 1, fbin);
            }
            printf("Loaded coordinates from coordinates.bin.\n");
        }
        fclose(fbin);
    }
    
    if (bin_count != num_nodes) {
        printf("Initializing to spiral layout...\n");
        for (uint32_t i = 0; i < num_nodes; i++) {
            float phi = (1.0f + sqrtf(5.0f)) / 2.0f;
            float angle = 2.0f * M_PI * phi * i;
            float r = 5.0f * sqrtf(i);
            posX[i] = r * cosf(angle);
            posY[i] = r * sinf(angle);
        }
    }

    // Load inDegrees from nodes table in alphabetical order of id/title
    printf("Step 2.5: Loading in-degrees...\n");
    rc = sqlite3_prepare_v2(db, "SELECT inDegree FROM nodes ORDER BY id", -1, &stmt, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Failed to prepare in-degrees: %s\n", sqlite3_errmsg(db));
        return 1;
    }
    uint32_t node_idx = 0;
    while (sqlite3_step(stmt) == SQLITE_ROW && node_idx < num_nodes) {
        inDegrees[node_idx] = sqlite3_column_int(stmt, 0);
        node_idx++;
    }
    sqlite3_finalize(stmt);
    printf("Loaded %u node in-degrees.\n", node_idx);

    // 5. Load links using the new source_idx/target_idx columns
    printf("Step 3: Loading links...\n");
    rc = sqlite3_prepare_v2(db, "SELECT source_idx, target_idx FROM links", -1, &stmt, NULL);
    uint64_t link_idx = 0;
    while (sqlite3_step(stmt) == SQLITE_ROW && link_idx < num_links) {
        sources[link_idx] = sqlite3_column_int(stmt, 0);
        targets[link_idx] = sqlite3_column_int(stmt, 1);
        link_idx++;
    }
    sqlite3_finalize(stmt);
    printf("Loaded %llu links.\n", (unsigned long long)link_idx);

    // Pre-calculate node degrees for D3-style forceLink bias
    uint32_t *deg = calloc(num_nodes, sizeof(uint32_t));
    if (!deg) {
        fprintf(stderr, "Failed to allocate memory for node degrees.\n");
        return 1;
    }
    for (uint64_t i = 0; i < num_links; i++) {
        uint32_t s = sources[i];
        uint32_t t = targets[i];
        if (s < num_nodes) deg[s]++;
        if (t < num_nodes) deg[t]++;
    }

    // Pre-calculate initial center of mass to align positions later
    double init_center_x = 0.0;
    double init_center_y = 0.0;
    for (uint32_t i = 0; i < num_nodes; i++) {
        init_center_x += posX[i];
        init_center_y += posY[i];
    }
    init_center_x /= num_nodes;
    init_center_y /= num_nodes;

    // 6. Run Simulation (D3-style Link & JS-style Gravity Force)
    int num_iterations_to_run = 80; // Default to 80
    if (argc >= 6) {
        num_iterations_to_run = atoi(argv[5]);
    } else {
        num_iterations_to_run = 80;
    }
    printf("Step 4: Running %d iterations...\n", num_iterations_to_run);

    const float rest_distance = 30.0f; // Target link distance
    for (int iter = 0; iter < num_iterations_to_run; iter++) {
        // Cooling factor alpha decreases linearly from 1.0 down to 0.05
        float alpha = 1.0f - 0.95f * ((float)iter / (float)num_iterations_to_run);

        // Combined forces loop over links
        for (uint64_t i = 0; i < num_links; i++) {
            uint32_t s = sources[i];
            uint32_t t = targets[i];

            // 1. D3-style link (spring) force
            // Uses future positions based on velocities (momentum peek)
            float dx = (posX[t] + velX[t]) - (posX[s] + velX[s]);
            float dy = (posY[t] + velY[t]) - (posY[s] + velY[s]);
            float r = sqrtf(dx*dx + dy*dy);
            if (r == 0.0f) {
                dx = 0.01f;
                dy = 0.01f;
                r = sqrtf(dx*dx + dy*dy);
            }

            // D3 force strength defaults/scaling matches original spring_constant parameter
            float spring_factor = (r - rest_distance) / r * alpha * spring_constant * 0.01f;
            float s_fx = dx * spring_factor;
            float s_fy = dy * spring_factor;

            // Bias based on degrees
            float bias = 0.5f;
            if (deg[s] + deg[t] > 0) {
                bias = (float)deg[s] / (float)(deg[s] + deg[t]);
            }

            // 2. JS-style gravity force (attractive force based on node mass/inDegree)
            float g_dx = posX[t] - posX[s];
            float g_dy = posY[t] - posY[s];
            float g_r2 = g_dx*g_dx + g_dy*g_dy;
            float g_r = sqrtf(g_r2) + 0.0001f;

            float massS = (float)(inDegrees[s] > 0 ? inDegrees[s] : 1);
            float massT = (float)(inDegrees[t] > 0 ? inDegrees[t] : 1);

            float minDistance2 = 100.0f; // matches minDistance2 = 100 in JS
            float forceMagnitude = gravity_constant * (massS * massT) / (g_r2 > minDistance2 ? g_r2 : minDistance2);

            // Safety clamp to prevent instability for extremely popular hubs
            float max_gravity_force = 10.0f;
            if (forceMagnitude > max_gravity_force) {
                forceMagnitude = max_gravity_force;
            }

            float g_fx = forceMagnitude * (g_dx / g_r) * alpha;
            float g_fy = forceMagnitude * (g_dy / g_r) * alpha;

            // Apply forces to node velocities
            velX[s] += s_fx * (1.0f - bias) + g_fx;
            velY[s] += s_fy * (1.0f - bias) + g_fy;
            velX[t] -= s_fx * bias + g_fx;
            velY[t] -= s_fy * bias + g_fy;
        }

        // Update positions, NaN correction, clamp velocities, and damping
        for (uint32_t i = 0; i < num_nodes; i++) {
            // Clamp velocity
            float speed = sqrtf(velX[i]*velX[i] + velY[i]*velY[i]);
            if (speed > max_speed) {
                velX[i] = (velX[i] / speed) * max_speed;
                velY[i] = (velY[i] / speed) * max_speed;
            }

            // Update positions
            posX[i] += velX[i];
            posY[i] += velY[i];

            // Check for NaNs and fix them
            if (isnan(posX[i]) || isnan(posY[i])) {
                posX[i] = 0.0f;
                posY[i] = 0.0f;
            }

            // Damping
            velX[i] *= damping;
            velY[i] *= damping;
        }

        // Align center of mass to prevent drift
        double current_center_x = 0.0;
        double current_center_y = 0.0;
        for (uint32_t i = 0; i < num_nodes; i++) {
            current_center_x += posX[i];
            current_center_y += posY[i];
        }
        current_center_y /= num_nodes;
        current_center_x /= num_nodes;

        float offset_x = (float)(init_center_x - current_center_x);
        float offset_y = (float)(init_center_y - current_center_y);
        for (uint32_t i = 0; i < num_nodes; i++) {
            posX[i] += offset_x;
            posY[i] += offset_y;
        }

        printf("  Iteration %d complete.\r", iter + 1);
        fflush(stdout);
    }
    printf("\nSimulation complete.\n");

    // 7. Export results
    printf("Step 5: Exporting results to %s...\n", output_bin_name);
    FILE *fout = fopen(output_bin_name, "wb");
    fwrite(&num_nodes, sizeof(uint32_t), 1, fout);
    for (uint32_t i = 0; i < num_nodes; i++) {
        fwrite(&posX[i], sizeof(float), 1, fout);
        fwrite(&posY[i], sizeof(float), 1, fout);
    }
    fclose(fout);

    if (update_db_flag) {
        printf("Step 6: Updating database coordinates...\n");
        sqlite3_exec(db, "BEGIN TRANSACTION", NULL, NULL, NULL);
        rc = sqlite3_prepare_v2(db, "UPDATE nodes SET x = ?, y = ? WHERE rowid = ?", -1, &stmt, NULL);
        for (uint32_t i = 0; i < num_nodes; i++) {
            sqlite3_bind_double(stmt, 1, posX[i]);
            sqlite3_bind_double(stmt, 2, posY[i]);
            sqlite3_bind_int(stmt, 3, i + 1); // rowid starts at 1
            sqlite3_step(stmt);
            sqlite3_reset(stmt);
        }
        sqlite3_finalize(stmt);
        sqlite3_exec(db, "COMMIT", NULL, NULL, NULL);
    }

    // Free memory
    free(posX);
    free(posY);
    free(velX);
    free(velY);
    free(sources);
    free(targets);
    free(inDegrees);
    free(deg);

    sqlite3_close(db);
    printf("All done!\n");
    return 0;
}
