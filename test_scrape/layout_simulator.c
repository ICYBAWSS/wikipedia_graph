#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <math.h>
#include <string.h>
#include <time.h>
#include <sqlite3.h>
#include <pthread.h>
#include <sys/sysctl.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// We'll determine NUM_NODES dynamically
uint32_t num_nodes = 0;
uint64_t num_links = 0;

// Simulation parameters
const float damping = 0.60f; // matches D3 default velocityDecay of 0.6
const float max_speed = 50.0f;

// Node collision radius scaling matching D3 scalePow exponent 0.25 (views 0 to max_views mapped to radius 2 to 5)
float get_node_radius(uint32_t views, float current_collision, float max_views) {
    float base_min = 2.0f;
    float base_max = 5.0f;
    float norm = (float)views / max_views;
    if (norm > 1.0f) norm = 1.0f;
    if (norm < 0.0f) norm = 0.0f;
    
    // ScalePow exponent 0.25
    float r_scale = base_min + (base_max - base_min) * powf(norm, 0.25f);
    return r_scale + current_collision - 16.0f;
}

// Spatial Hashing structs
typedef struct {
    uint64_t cell_key;
    uint32_t node_index;
} NodeCell;

typedef struct {
    uint64_t key;
    uint32_t start;
    uint32_t count;
} CellInfo;

// Comparison function for qsort
int compare_node_cells(const void *a, const void *b) {
    uint64_t ka = ((NodeCell*)a)->cell_key;
    uint64_t kb = ((NodeCell*)b)->cell_key;
    if (ka < kb) return -1;
    if (ka > kb) return 1;
    return 0;
}

// Fast hash function for 64-bit keys
inline uint32_t hash_key(uint64_t key, uint32_t mask) {
    uint64_t h = key;
    h ^= h >> 23;
    h *= 0x2127599bf4325c37ULL;
    h ^= h >> 47;
    return (uint32_t)(h & mask);
}

// Thread-safe, deterministic LCG random number generator
inline float lcg_rand(uint64_t *state) {
    *state = (*state * 6364136223846793005ULL) + 1442695040888963407ULL;
    return (float)(*state >> 33) / 8589934592.0f; // float between 0.0 and 1.0
}

// Dynamic CPU core detection
int get_num_cores() {
    int count;
    size_t size = sizeof(count);
    if (sysctlbyname("hw.ncpu", &count, &size, NULL, 0) == 0) {
        return count;
    }
    return 8; // default fallback
}

// Global shared variables
float *posX = NULL;
float *posY = NULL;
float *global_velX = NULL;
float *global_velY = NULL;
uint32_t *sources = NULL;
uint32_t *targets = NULL;
uint32_t *inDegrees = NULL;
uint32_t *views = NULL;
uint32_t *deg = NULL;
NodeCell *node_cells = NULL;
CellInfo *hash_table = NULL;
uint32_t hash_mask = 0;
float max_views = 10000000.0f;

// Thread local storage for velocities
float **thread_velX = NULL;
float **thread_velY = NULL;

// Link worker thread struct
typedef struct {
    int thread_id;
    int num_threads;
    float spring_constant;
    float gravity_constant;
    float rest_distance;
    float alpha;
    int iter;
} LinkThreadData;

void *link_force_worker(void *arg) {
    LinkThreadData *data = (LinkThreadData *)arg;
    uint64_t start = (num_links * data->thread_id) / data->num_threads;
    uint64_t end = (num_links * (data->thread_id + 1)) / data->num_threads;
    
    float *t_velX = thread_velX[data->thread_id];
    float *t_velY = thread_velY[data->thread_id];
    
    // Clear thread-local velocity accumulation
    memset(t_velX, 0, num_nodes * sizeof(float));
    memset(t_velY, 0, num_nodes * sizeof(float));
    
    float spring_constant = data->spring_constant;
    float gravity_constant = data->gravity_constant;
    float rest_distance = data->rest_distance;
    float alpha = data->alpha;
    int iter = data->iter;
    
    for (uint64_t i = start; i < end; i++) {
        uint32_t s = sources[i];
        uint32_t t = targets[i];

        // 1. D3-style link (spring) force (uses velocity Verlet look-ahead)
        float dx = (posX[t] + global_velX[t]) - (posX[s] + global_velX[s]);
        float dy = (posY[t] + global_velY[t]) - (posY[s] + global_velY[s]);
        if (dx == 0.0f) {
            uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_links) + 10101ULL;
            dx = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
        }
        if (dy == 0.0f) {
            uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_links) + 20202ULL;
            dy = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
        }
        float r = sqrtf(dx*dx + dy*dy);

        float spring_factor = (r - rest_distance) / r * alpha * spring_constant;
        float s_fx = dx * spring_factor;
        float s_fy = dy * spring_factor;

        // Bias based on node degrees
        float bias = 0.5f;
        if (deg[s] + deg[t] > 0) {
            bias = (float)deg[s] / (float)(deg[s] + deg[t]);
        }

        // 2. JS-style gravity force (attractive force along links)
        float g_dx = posX[t] - posX[s];
        float g_dy = posY[t] - posY[s];
        if (g_dx == 0.0f) {
            uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_links) + 30303ULL;
            g_dx = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
        }
        if (g_dy == 0.0f) {
            uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_links) + 40404ULL;
            g_dy = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
        }
        float g_r2 = g_dx*g_dx + g_dy*g_dy;
        float g_r = sqrtf(g_r2);

        float massS = (float)(inDegrees[s] > 0 ? inDegrees[s] : 1);
        float massT = (float)(inDegrees[t] > 0 ? inDegrees[t] : 1);

        float minDistance2 = 100.0f;
        float forceMagnitude = gravity_constant * (massS * massT) / (g_r2 > minDistance2 ? g_r2 : minDistance2);

        float g_fx = forceMagnitude * (g_dx / g_r) * alpha;
        float g_fy = forceMagnitude * (g_dy / g_r) * alpha;

        // Apply forces to node velocities
        t_velX[s] += s_fx * (1.0f - bias) + g_fx;
        t_velY[s] += s_fy * (1.0f - bias) + g_fy;
        t_velX[t] -= (s_fx * bias + g_fx);
        t_velY[t] -= (s_fy * bias + g_fy);
    }
    return NULL;
}

// Spatial grid worker thread struct
typedef struct {
    int thread_id;
    int num_threads;
    float current_charge;
    float current_collision;
    float cell_size;
    float alpha;
    int iter;
} GridThreadData;

void *grid_force_worker(void *arg) {
    GridThreadData *data = (GridThreadData *)arg;
    uint32_t start = (num_nodes * data->thread_id) / data->num_threads;
    uint32_t end = (num_nodes * (data->thread_id + 1)) / data->num_threads;
    
    float *t_velX = thread_velX[data->thread_id];
    float *t_velY = thread_velY[data->thread_id];
    
    float current_charge = data->current_charge;
    float current_collision = data->current_collision;
    float cell_size = data->cell_size;
    float alpha = data->alpha;
    int iter = data->iter;
    
    for (uint32_t i = start; i < end; i++) {
        int cx = (int)floorf(posX[i] / cell_size);
        int cy = (int)floorf(posY[i] / cell_size);

        float r_i = get_node_radius(views[i], current_collision, max_views);
        float r_i2 = r_i * r_i;

        int charge_count = 0;
        int collision_count = 0;
        const int max_charge_count = 100;
        const int max_collision_count = 50;

        // Check cells in range [-2, 2] covering distance up to 20.0f
        for (int ny = cy - 2; ny <= cy + 2; ny++) {
            if (charge_count >= max_charge_count && collision_count >= max_collision_count) break;
            for (int nx = cx - 2; nx <= cx + 2; nx++) {
                if (charge_count >= max_charge_count && collision_count >= max_collision_count) break;

                uint64_t neighbor_key = ((uint64_t)nx << 32) | (uint32_t)ny;
                
                uint32_t idx = hash_key(neighbor_key, hash_mask);
                uint32_t cell_start = 0;
                uint32_t cell_count = 0;
                while (hash_table[idx].key != 0xFFFFFFFFFFFFFFFFULL) {
                    if (hash_table[idx].key == neighbor_key) {
                        cell_start = hash_table[idx].start;
                        cell_count = hash_table[idx].count;
                        break;
                    }
                    idx = (idx + 1) & hash_mask;
                }

                if (cell_count == 0) continue;

                for (uint32_t k = 0; k < cell_count; k++) {
                    // Critical Early Exit: stop checking nodes inside the cell if limits are already satisfied
                    if (charge_count >= max_charge_count && collision_count >= max_collision_count) break;
                    
                    uint32_t j = node_cells[cell_start + k].node_index;
                    if (j > i) { // Process each pair once
                        float dx = posX[j] - posX[i];
                        float dy = posY[j] - posY[i];
                        float r2 = dx*dx + dy*dy;

                        if (r2 < 20.0f * 20.0f) {
                            // 1. Charge repulsion force (D3 ManyBody)
                            if (charge_count < max_charge_count) {
                                if (dx == 0.0f) {
                                    uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_nodes) + 50505ULL;
                                    dx = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
                                    r2 += dx * dx;
                                }
                                if (dy == 0.0f) {
                                    uint64_t rng_state = (uint64_t)j + ((uint64_t)iter * num_nodes) + 60606ULL;
                                    dy = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
                                    r2 += dy * dy;
                                }

                                float r2_clamped = r2;
                                if (r2_clamped < 1.0f) {
                                    r2_clamped = sqrtf(r2_clamped);
                                }

                                float charge_force = (current_charge * alpha) / r2_clamped;
                                float c_fx = dx * charge_force;
                                float c_fy = dy * charge_force;

                                t_velX[i] += c_fx;
                                t_velY[i] += c_fy;
                                t_velX[j] -= c_fx;
                                t_velY[j] -= c_fy;

                                charge_count++;
                            }

                            // 2. Collision force (uses lookahead positions)
                            if (collision_count < max_collision_count) {
                                float xi = posX[i] + global_velX[i];
                                float yi = posY[i] + global_velY[i];
                                float xj = posX[j] + global_velX[j];
                                float yj = posY[j] + global_velY[j];

                                float col_dx = xi - xj;
                                float col_dy = yi - yj;
                                float col_r2 = col_dx*col_dx + col_dy*col_dy;

                                float r_j = get_node_radius(views[j], current_collision, max_views);
                                float R = r_i + r_j;

                                if (col_r2 < R * R) {
                                    if (col_dx == 0.0f) {
                                        uint64_t rng_state = (uint64_t)i + ((uint64_t)iter * num_nodes) + 70707ULL;
                                        col_dx = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
                                        col_r2 += col_dx * col_dx;
                                    }
                                    if (col_dy == 0.0f) {
                                        uint64_t rng_state = (uint64_t)j + ((uint64_t)iter * num_nodes) + 80808ULL;
                                        col_dy = 1e-6f * (lcg_rand(&rng_state) - 0.5f);
                                        col_r2 += col_dy * col_dy;
                                    }

                                    float col_r = sqrtf(col_r2);
                                    float disp = (R - col_r) / col_r;

                                    float r_j2 = r_j * r_j;
                                    float r2_sum = r_i2 + r_j2;
                                    float ratio_i = r_j2 / r2_sum;
                                    float ratio_j = 1.0f - ratio_i;

                                    float col_fx = col_dx * disp;
                                    float col_fy = col_dy * disp;

                                    t_velX[i] += col_fx * ratio_i;
                                    t_velY[i] += col_fy * ratio_i;
                                    t_velX[j] -= col_fx * ratio_j;
                                    t_velY[j] -= col_fy * ratio_j;

                                    collision_count++;
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    return NULL;
}

// Integration and position update worker struct
typedef struct {
    int thread_id;
    int num_threads;
    float gravity_constant;
    float alpha;
} UpdateThreadData;

void *update_worker(void *arg) {
    UpdateThreadData *data = (UpdateThreadData *)arg;
    uint32_t start = (num_nodes * data->thread_id) / data->num_threads;
    uint32_t end = (num_nodes * (data->thread_id + 1)) / data->num_threads;
    int num_threads = data->num_threads;

    for (uint32_t i = start; i < end; i++) {
        // Merge velocities from all thread-local storage
        float vx = 0.0f;
        float vy = 0.0f;
        for (int t = 0; t < num_threads; t++) {
            vx += thread_velX[t][i];
            vy += thread_velY[t][i];
        }
        
        // Add to global velocity
        global_velX[i] += vx;
        global_velY[i] += vy;

        // Apply damping
        global_velX[i] *= damping;
        global_velY[i] *= damping;

        // Clamp velocity
        float speed = sqrtf(global_velX[i]*global_velX[i] + global_velY[i]*global_velY[i]);
        if (speed > max_speed) {
            global_velX[i] = (global_velX[i] / speed) * max_speed;
            global_velY[i] = (global_velY[i] / speed) * max_speed;
        }

        // Apply true central gravity force drawing node to (0,0)
        float gx = posX[i];
        float gy = posY[i];
        float gr = sqrtf(gx*gx + gy*gy) + 0.0001f;
        // Linear gravity drawing node to 0,0
        float g_force = gr * 0.005f * data->gravity_constant * data->alpha;
        global_velX[i] -= (gx / gr) * g_force;
        global_velY[i] -= (gy / gr) * g_force;

        // Update positions
        posX[i] += global_velX[i];
        posY[i] += global_velY[i];

        // Check for NaNs and fix them
        if (isnan(posX[i]) || isnan(posY[i])) {
            posX[i] = 0.0f;
            posY[i] = 0.0f;
        }
    }
    return NULL;
}

int main(int argc, char *argv[]) {
    setbuf(stdout, NULL);
    if (argc < 5) {
        printf("Usage: %s <spring_constant> <gravity_constant> <output_bin_name> <update_db_flag> [num_iterations]\n", argv[0]);
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

    // Get total node count
    rc = sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM nodes", -1, &stmt, NULL);
    if (rc == SQLITE_OK && sqlite3_step(stmt) == SQLITE_ROW) {
        num_nodes = sqlite3_column_int(stmt, 0);
    }
    sqlite3_finalize(stmt);
    printf("Found %u nodes in database.\n", num_nodes);

    // Get total link count
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

    // Get max views dynamically from nodes table
    rc = sqlite3_prepare_v2(db, "SELECT MAX(views) FROM nodes", -1, &stmt, NULL);
    if (rc == SQLITE_OK && sqlite3_step(stmt) == SQLITE_ROW) {
        double mv = sqlite3_column_double(stmt, 0);
        if (mv > 0.0) max_views = (float)mv;
    }
    sqlite3_finalize(stmt);
    printf("Dynamic max views in DB: %f\n", max_views);

    // Allocate memory
    posX = malloc(num_nodes * sizeof(float));
    posY = malloc(num_nodes * sizeof(float));
    global_velX = calloc(num_nodes, sizeof(float));
    global_velY = calloc(num_nodes, sizeof(float));
    sources = malloc(num_links * sizeof(uint32_t));
    targets = malloc(num_links * sizeof(uint32_t));
    inDegrees = malloc(num_nodes * sizeof(uint32_t));
    views = malloc(num_nodes * sizeof(uint32_t));

    if (!posX || !posY || !global_velX || !global_velY || !sources || !targets || !inDegrees || !views) {
        fprintf(stderr, "Failed to allocate memory.\n");
        return 1;
    }

    // Load baseline coordinates or initialize
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

    // Load inDegrees
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

    // Load views
    printf("Step 2.6: Loading views...\n");
    rc = sqlite3_prepare_v2(db, "SELECT views FROM nodes ORDER BY id", -1, &stmt, NULL);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "Failed to prepare views: %s\n", sqlite3_errmsg(db));
        return 1;
    }
    node_idx = 0;
    while (sqlite3_step(stmt) == SQLITE_ROW && node_idx < num_nodes) {
        views[node_idx] = sqlite3_column_int(stmt, 0);
        node_idx++;
    }
    sqlite3_finalize(stmt);
    printf("Loaded %u node views.\n", node_idx);

    // Load links
    printf("Step 3: Loading links...\n");
    FILE *flink = fopen("links.bin", "rb");
    uint64_t loaded_links_count = 0;
    if (flink) {
        fread(&loaded_links_count, sizeof(uint64_t), 1, flink);
        if (loaded_links_count == num_links) {
            size_t read_s = fread(sources, sizeof(uint32_t), num_links, flink);
            size_t read_t = fread(targets, sizeof(uint32_t), num_links, flink);
            if (read_s == num_links && read_t == num_links) {
                printf("Loaded %llu links from links.bin (cache hit).\n", (unsigned long long)num_links);
                loaded_links_count = num_links;
            } else {
                loaded_links_count = 0;
            }
        }
        fclose(flink);
    }

    if (loaded_links_count != num_links) {
        rc = sqlite3_prepare_v2(db, "SELECT source_idx, target_idx FROM links", -1, &stmt, NULL);
        uint64_t link_idx = 0;
        while (sqlite3_step(stmt) == SQLITE_ROW && link_idx < num_links) {
            sources[link_idx] = sqlite3_column_int(stmt, 0);
            targets[link_idx] = sqlite3_column_int(stmt, 1);
            link_idx++;
        }
        sqlite3_finalize(stmt);
        printf("Loaded %llu links from database.\n", (unsigned long long)link_idx);

        flink = fopen("links.bin", "wb");
        if (flink) {
            fwrite(&num_links, sizeof(uint64_t), 1, flink);
            fwrite(sources, sizeof(uint32_t), num_links, flink);
            fwrite(targets, sizeof(uint32_t), num_links, flink);
            fclose(flink);
            printf("Saved links cache to links.bin.\n");
        }
    }

    // Pre-calculate degrees
    deg = calloc(num_nodes, sizeof(uint32_t));
    if (!deg) {
        fprintf(stderr, "Failed to allocate memory for degrees.\n");
        return 1;
    }
    for (uint64_t i = 0; i < num_links; i++) {
        uint32_t s = sources[i];
        uint32_t t = targets[i];
        if (s < num_nodes) deg[s]++;
        if (t < num_nodes) deg[t]++;
    }

    // Setup threads and allocate thread-local storage
    int num_threads = get_num_cores();
    printf("Detected %d CPU cores. Spawning %d threads...\n", num_threads, num_threads);
    
    thread_velX = malloc(num_threads * sizeof(float*));
    thread_velY = malloc(num_threads * sizeof(float*));
    for (int t = 0; t < num_threads; t++) {
        thread_velX[t] = malloc(num_nodes * sizeof(float));
        thread_velY[t] = malloc(num_nodes * sizeof(float));
    }

    pthread_t *threads = malloc(num_threads * sizeof(pthread_t));
    LinkThreadData *link_data = malloc(num_threads * sizeof(LinkThreadData));
    GridThreadData *grid_data = malloc(num_threads * sizeof(GridThreadData));
    UpdateThreadData *update_data = malloc(num_threads * sizeof(UpdateThreadData));

    int num_iterations_to_run = 80;
    if (argc >= 6) {
        num_iterations_to_run = atoi(argv[5]);
    }
    printf("Step 4: Running %d iterations...\n", num_iterations_to_run);

    float rest_distance = 120.0f;
    if (argc >= 7) {
        rest_distance = atof(argv[6]);
    }
    float current_charge = -200.0f;
    if (argc >= 8) {
        current_charge = atof(argv[7]);
    }
    float current_collision = 20.0f;
    if (argc >= 9) {
        current_collision = atof(argv[8]);
    }
    float cell_size = current_collision > 10.0f ? current_collision : 10.0f;

    for (int iter = 0; iter < num_iterations_to_run; iter++) {
        float alpha = 1.0f - 0.95f * ((float)iter / (float)num_iterations_to_run);

        // --- A. Link Forces (Parallelized) ---
        for (int t = 0; t < num_threads; t++) {
            link_data[t].thread_id = t;
            link_data[t].num_threads = num_threads;
            link_data[t].spring_constant = spring_constant;
            link_data[t].gravity_constant = gravity_constant;
            link_data[t].rest_distance = rest_distance;
            link_data[t].alpha = alpha;
            link_data[t].iter = iter;
            pthread_create(&threads[t], NULL, link_force_worker, &link_data[t]);
        }
        for (int t = 0; t < num_threads; t++) {
            pthread_join(threads[t], NULL);
        }

        // --- B. Spatial Hashing Setup (Single-threaded, fast) ---
        node_cells = malloc(num_nodes * sizeof(NodeCell));
        for (uint32_t i = 0; i < num_nodes; i++) {
            int cx = (int)floorf(posX[i] / cell_size);
            int cy = (int)floorf(posY[i] / cell_size);
            node_cells[i].cell_key = ((uint64_t)cx << 32) | (uint32_t)cy;
            node_cells[i].node_index = i;
        }

        qsort(node_cells, num_nodes, sizeof(NodeCell), compare_node_cells);

        uint32_t num_unique_cells = 0;
        if (num_nodes > 0) {
            num_unique_cells = 1;
            for (uint32_t i = 1; i < num_nodes; i++) {
                if (node_cells[i].cell_key != node_cells[i-1].cell_key) {
                    num_unique_cells++;
                }
            }
        }

        uint32_t hash_size = num_unique_cells * 2;
        uint32_t mask = 1;
        while (mask < hash_size) mask <<= 1;
        mask -= 1;
        hash_mask = mask;

        hash_table = malloc((hash_mask + 1) * sizeof(CellInfo));
        for (uint32_t h = 0; h <= hash_mask; h++) {
            hash_table[h].key = 0xFFFFFFFFFFFFFFFFULL;
        }

        uint32_t start_idx = 0;
        for (uint32_t i = 0; i <= num_nodes; i++) {
            if (i == num_nodes || (i > 0 && node_cells[i].cell_key != node_cells[i-1].cell_key)) {
                uint64_t key = node_cells[start_idx].cell_key;
                uint32_t count = i - start_idx;

                uint32_t idx = hash_key(key, hash_mask);
                while (hash_table[idx].key != 0xFFFFFFFFFFFFFFFFULL) {
                    idx = (idx + 1) & hash_mask;
                }
                hash_table[idx].key = key;
                hash_table[idx].start = start_idx;
                hash_table[idx].count = count;

                start_idx = i;
            }
        }

        // --- C. Spatial Grid Search for Charge and Collision (Parallelized) ---
        for (int t = 0; t < num_threads; t++) {
            grid_data[t].thread_id = t;
            grid_data[t].num_threads = num_threads;
            grid_data[t].current_charge = current_charge;
            grid_data[t].current_collision = current_collision;
            grid_data[t].cell_size = cell_size;
            grid_data[t].alpha = alpha;
            grid_data[t].iter = iter;
            pthread_create(&threads[t], NULL, grid_force_worker, &grid_data[t]);
        }
        for (int t = 0; t < num_threads; t++) {
            pthread_join(threads[t], NULL);
        }

        // Free spatial grid allocations for this iteration
        free(node_cells);
        free(hash_table);

        // --- D. Integration and Position Updates (Parallelized) ---
        for (int t = 0; t < num_threads; t++) {
            update_data[t].thread_id = t;
            update_data[t].num_threads = num_threads;
            update_data[t].gravity_constant = gravity_constant;
            update_data[t].alpha = alpha;
            pthread_create(&threads[t], NULL, update_worker, &update_data[t]);
        }
        for (int t = 0; t < num_threads; t++) {
            pthread_join(threads[t], NULL);
        }

        // --- E. Center Force (Translates center of mass to exactly 0,0) ---
        double current_center_x = 0.0;
        double current_center_y = 0.0;
        for (uint32_t i = 0; i < num_nodes; i++) {
            current_center_x += posX[i];
            current_center_y += posY[i];
        }
        current_center_x /= num_nodes;
        current_center_y /= num_nodes;

        for (uint32_t i = 0; i < num_nodes; i++) {
            posX[i] -= (float)current_center_x;
            posY[i] -= (float)current_center_y;
        }

        printf("  Iteration %d complete.\r", iter + 1);
        fflush(stdout);
    }
    printf("\nSimulation complete.\n");

    // Clean up threads memory
    for (int t = 0; t < num_threads; t++) {
        free(thread_velX[t]);
        free(thread_velY[t]);
    }
    free(thread_velX);
    free(thread_velY);
    free(threads);
    free(link_data);
    free(grid_data);
    free(update_data);

    // Export results
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
            sqlite3_bind_int(stmt, 3, i + 1);
            sqlite3_step(stmt);
            sqlite3_reset(stmt);
        }
        sqlite3_finalize(stmt);
        sqlite3_exec(db, "COMMIT", NULL, NULL, NULL);
    }

    free(posX);
    free(posY);
    global_velX = global_velX; // unused reference silencer
    free(global_velX);
    free(global_velY);
    free(sources);
    free(targets);
    free(inDegrees);
    free(views);
    free(deg);

    sqlite3_close(db);
    printf("All done!\n");
    return 0;
}
