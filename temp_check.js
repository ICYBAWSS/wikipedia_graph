
        let highPerfMode = true;
        let db = null;
        let graphData = { nodes: [], links: [] }; // currently loaded viewport data
        let selectedNode = null;
        let hoveredNode = null;
        let isInteracting = false;
        let transform = d3.zoomIdentity;

        // Topic Color mapping helper
        const categoryColors = {
            "Science & Technology": "#ffffff",
            "History & Society": "#e5e5e5",
            "Art & Culture": "#f4f0ff",
            "Philosophy & Religion": "#a1a1a1",
            "Geography & Places": "#b5b5b5",
            "Biography & People": "#dcdcdc",
            "Other & General": "#737373"
        };

        const categoryGlowColors = {
            "Science & Technology": "rgba(255, 255, 255, 0.1)",
            "History & Society": "rgba(229, 229, 229, 0.1)",
            "Art & Culture": "rgba(244, 240, 255, 0.1)",
            "Philosophy & Religion": "rgba(161, 161, 161, 0.1)",
            "Geography & Places": "rgba(181, 181, 181, 0.1)",
            "Biography & People": "rgba(220, 220, 220, 0.1)",
            "Other & General": "rgba(115, 115, 115, 0.1)"
        };

        // Render Legend
        const legendList = document.getElementById("legend-list");
        Object.entries(categoryColors).forEach(([topic, color]) => {
            const item = document.createElement("div");
            item.className = "legend-item";
            item.innerHTML = `
                <div class="legend-color" style="background-color: ${color}"></div>
                <span>${topic}</span>
            `;
            legendList.appendChild(item);
        });

        // Initialize Canvas Components
        const container = document.getElementById("graph-container");
        const canvas = document.getElementById("graph-canvas");
        const ctx = canvas.getContext("2d");
        const dpr = window.devicePixelRatio || 1;
        let width = container.clientWidth;
        let height = container.clientHeight;

        function resizeCanvas() {
            width = container.clientWidth;
            height = container.clientHeight;
            canvas.width = width * dpr;
            canvas.height = height * dpr;
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.scale(dpr, dpr);
            canvas.style.width = width + "px";
            canvas.style.height = height + "px";
        }
        resizeCanvas();

        const tooltip = document.getElementById("tooltip");
        const sidebar = document.getElementById("detail-sidebar");

        // Global scales and parameters
        let nodeRadiusScale = d3.scalePow().exponent(0.25).range([5, 26]);
        let nodeOpacityScale = d3.scaleLinear().range([0.35, 1.0]);
        let maxViews = 10000000;
        let maxInDegree = 1;

        // Force Simulation setup
        let currentDist = 120;
        let currentCharge = -200;
        let currentCollision = 20;
        let gravityStrength = 0.05;

        // Custom gravity force between connected nodes: F = G * m1 * m2 / r^2
        function gravityForce(alpha) {
            const minDistance2 = 100; // Cap distance at 10px (100 px^2) to prevent division by zero or infinite force
            graphData.links.forEach(l => {
                const source = l.source;
                const target = l.target;
                
                if (typeof source !== "object" || typeof target !== "object") return;
                
                const dx = target.x - source.x;
                const dy = target.y - source.y;
                const r2 = dx * dx + dy * dy;
                const r = Math.sqrt(r2);
                
                if (r === 0) return;
                
                const m1 = source.inDegree || 1;
                const m2 = target.inDegree || 1;
                
                const forceMagnitude = gravityStrength * (m1 * m2) / Math.max(r2, minDistance2);
                
                const fx = forceMagnitude * (dx / r) * alpha;
                const fy = forceMagnitude * (dy / r) * alpha;
                
                source.vx += fx;
                source.vy += fy;
                target.vx -= fx;
                target.vy -= fy;
            });
        }

        const simulation = d3.forceSimulation([])
            .force("link", d3.forceLink([]).id(d => d.id).distance(currentDist).strength(0.05))
            .force("charge", d3.forceManyBody().strength(currentCharge))
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("collision", d3.forceCollide().radius(d => nodeRadiusScale(d.views) + currentCollision - 16).iterations(1))
            .force("gravity", gravityForce);

        simulation.on("tick", () => {
            draw();
        });

        // Zoom Behavior
        const zoom = d3.zoom()
            .scaleExtent([0.02, 8]) // expanded limits for deep zooming in large networks
            .on("start", () => {
                isInteracting = true;
                draw();
            })
            .on("zoom", (event) => {
                transform = event.transform;
                // Query database dynamically based on new coordinates during zoom/pan!
                executeQuery();
            })
            .on("end", () => {
                isInteracting = false;
                executeQuery();
            });
            
        d3.select(canvas).call(zoom);

        // Initial zoom transform: center (0,0) on screen and zoom out way more (scale 0.12)
        transform = d3.zoomIdentity.translate(width / 2, height / 2).scale(0.12);
        d3.select(canvas).call(zoom.transform, transform);

        // Decompress embedded database bytes
        function decompressDatabase() {
            const binaryString = window.atob(COMPRESSED_SQLITE_DB);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            return pako.ungzip(bytes);
        }

        // Initialize SQL.js from base64 string
        function initDatabase() {
            const wasmBinaryString = window.atob(SQL_WASM_BASE64);
            const wasmBytes = new Uint8Array(wasmBinaryString.length);
            for (let i = 0; i < wasmBinaryString.length; i++) {
                wasmBytes[i] = wasmBinaryString.charCodeAt(i);
            }
            
            initSqlJs({ wasmBinary: wasmBytes }).then(SQL => {
                const dbData = decompressDatabase();
                db = new SQL.Database(dbData);
                console.log("SQLite WASM Database initialized successfully!");
                bootstrapVisualizer();
            }).catch(err => {
                console.error("Failed to initialize WebAssembly SQLite: ", err);
            });
        }

        // Fetch metadata to configure sliders, scales, and stats
        function bootstrapVisualizer() {
            // Get max values for ranges
            const metaStmt = db.prepare("SELECT MAX(views) as mv, MAX(inDegree) as md, COUNT(*) as cn FROM nodes");
            metaStmt.step();
            const meta = metaStmt.getAsObject();
            maxViews = meta.mv || 10000000;
            maxInDegree = meta.md || 1;
            const totalNodes = meta.cn;
            metaStmt.free();

            nodeRadiusScale.domain([0, maxViews]);
            nodeOpacityScale.domain([0, maxInDegree]);

            // Set slider limits dynamically
            filterViews.max = maxViews;
            filterViews.step = Math.round(maxViews / 100) || 100000;
            filterRef.max = maxInDegree;
            filterRef.step = 1;

            const linkMetaStmt = db.prepare("SELECT COUNT(*) as cl FROM links");
            linkMetaStmt.step();
            const totalLinks = linkMetaStmt.getAsObject().cl;
            linkMetaStmt.free();

            // Set global panel stats
            document.getElementById("stat-nodes").textContent = totalNodes.toLocaleString();
            document.getElementById("stat-links").textContent = totalLinks.toLocaleString();

            const hubStmt = db.prepare("SELECT id, inDegree FROM nodes ORDER BY inDegree DESC LIMIT 1");
            hubStmt.step();
            const hub = hubStmt.getAsObject();
            document.getElementById("stat-hub").textContent = hub.id + " (" + hub.inDegree + ")";
            hubStmt.free();

            // Perform initial queries to display nodes and set up forces
            updateHighPerfSettings();
        }

        // Perform spatial database queries on viewport boundaries and active filters
        function executeQuery() {
            if (!db) return;

            // Bounding box mapping in coordinate space
            const xMin = (-transform.x) / transform.k;
            const xMax = (width - transform.x) / transform.k;
            const yMin = (-transform.y) / transform.k;
            const yMax = (height - transform.y) / transform.k;

            const query = searchBox.value.toLowerCase().trim();
            const category = filterCategory.value;
            const minViews = +filterViews.value;
            const minRef = +filterRef.value;

            // Bounding box range query for nodes (indexed for instant lookup)
            let nodeSql = `
                SELECT id, category, views, inDegree, outDegree, x, y, snippet 
                FROM nodes 
                WHERE x BETWEEN :xMin AND :xMax 
                  AND y BETWEEN :yMin AND :yMax 
                  AND views >= :minViews
                  AND inDegree >= :minRef
            `;
            
            // Add padding so nodes entering viewport don't clip/pop in abruptly
            const padding = 200;
            const params = {
                ":xMin": xMin - padding,
                ":xMax": xMax + padding,
                ":yMin": yMin - padding,
                ":yMax": yMax + padding,
                ":minViews": minViews,
                ":minRef": minRef
            };

            if (category !== "all") {
                nodeSql += " AND category = :category";
                params[":category"] = category;
            }
            if (query !== "") {
                nodeSql += " AND id LIKE :query";
                params[":query"] = `%${query}%`;
            }

            const stmt = db.prepare(nodeSql);
            stmt.bind(params);

            const nodes = [];
            const nodeIds = new Set();
            while (stmt.step()) {
                const row = stmt.getAsObject();
                nodes.push({
                    id: row.id,
                    category: row.category,
                    views: row.views,
                    inDegree: row.inDegree,
                    outDegree: row.outDegree,
                    x: row.x,
                    y: row.y,
                    snippet: row.snippet
                });
                nodeIds.add(row.id);
            }
            stmt.free();

            // Bounding box range query for links (join nodes to check end-points coords)
            const links = [];
            if (nodes.length > 0) {
                // Always query all links in viewport bounds to keep physics stable
                const linkSql = `
                    SELECT l.source, l.target 
                    FROM links l
                    JOIN nodes n1 ON l.source = n1.id
                    JOIN nodes n2 ON l.target = n2.id
                    WHERE n1.x BETWEEN :xMin AND :xMax AND n1.y BETWEEN :yMin AND :yMax
                      AND n2.x BETWEEN :xMin AND :xMax AND n2.y BETWEEN :yMin AND :yMax
                `;
                const linkParams = {
                    ":xMin": xMin - padding,
                    ":xMax": xMax + padding,
                    ":yMin": yMin - padding,
                    ":yMax": yMax + padding
                };

                const linkStmt = db.prepare(linkSql);
                linkStmt.bind(linkParams);
                while (linkStmt.step()) {
                    const row = linkStmt.getAsObject();
                    links.push({
                        source: row.source,
                        target: row.target
                    });
                }
                linkStmt.free();
            }

            graphData = { nodes, links };

            // Always update simulation with visible nodes & links so that drag/sliders work
            updateSimulation(nodes, links);

            if (highPerfMode && isInteracting) {
                simulation.stop();
                draw();
            }
        }

        // Map visible nodes to simulation and preserve momentum coordinates
        function updateSimulation(visibleNodes, visibleLinks) {
            const nodeMap = new Map(simulation.nodes().map(n => [n.id, n]));
            visibleNodes.forEach(n => {
                const existing = nodeMap.get(n.id);
                if (existing) {
                    n.x = existing.x;
                    n.y = existing.y;
                    n.vx = existing.vx;
                    n.vy = existing.vy;
                }
            });

            simulation.nodes(visibleNodes);
            simulation.force("link").links(visibleLinks);
            
            // Re-tick layout gently to space out nodes
            simulation.alpha(0.2).restart();
        }

        // Draw HTML5 Canvas Elements
        function draw() {
            ctx.clearRect(0, 0, width, height);

            ctx.save();
            ctx.translate(transform.x, transform.y);
            ctx.scale(transform.k, transform.k);

            const xMin = (-transform.x) / transform.k;
            const xMax = (width - transform.x) / transform.k;
            const yMin = (-transform.y) / transform.k;
            const yMax = (height - transform.y) / transform.k;

            const nodeMap = new Map(graphData.nodes.map(n => [n.id, n]));

            const focusNode = selectedNode || (!isInteracting ? hoveredNode : null);
            const focusNeighbors = new Set();
            if (focusNode) {
                focusNeighbors.add(focusNode.id);
                graphData.links.forEach(l => {
                    const sId = typeof l.source === "object" ? l.source.id : l.source;
                    const tId = typeof l.target === "object" ? l.target.id : l.target;
                    if (sId === focusNode.id) focusNeighbors.add(tId);
                    if (tId === focusNode.id) focusNeighbors.add(sId);
                });
            }

            // 1. Draw Links
            if (!(highPerfMode && isInteracting)) {
                ctx.beginPath();
                graphData.links.forEach(l => {
                    const sourceNode = typeof l.source === "object" ? l.source : nodeMap.get(l.source);
                    const targetNode = typeof l.target === "object" ? l.target : nodeMap.get(l.target);

                    if (!sourceNode || !targetNode) return;

                    const sId = sourceNode.id;
                    const tId = targetNode.id;

                    if (focusNode && sId !== focusNode.id && tId !== focusNode.id) return;

                    // Viewport link culling
                    const inViewport = (sourceNode.x >= xMin && sourceNode.x <= xMax && sourceNode.y >= yMin && sourceNode.y <= yMax) ||
                                       (targetNode.x >= xMin && targetNode.x <= xMax && targetNode.y >= yMin && targetNode.y <= yMax);
                    if (!inViewport) return;

                    ctx.moveTo(sourceNode.x, sourceNode.y);
                    ctx.lineTo(targetNode.x, targetNode.y);
                });

                if (focusNode) {
                    ctx.strokeStyle = "#f4f0ff";
                    ctx.lineWidth = 1.5 / transform.k;
                } else {
                    ctx.strokeStyle = "rgba(255, 255, 255, 0.04)";
                    ctx.lineWidth = 0.6 / transform.k;
                }
                ctx.stroke();
            }

            // 2. Draw Nodes
            graphData.nodes.forEach(d => {
                const r = nodeRadiusScale(d.views);

                // Level-of-Detail (LOD) node density filter
                if (highPerfMode && isInteracting && transform.k < 0.25 && d.inDegree < 5) {
                    return;
                }

                // Viewport culling
                if (d.x + r < xMin || d.x - r > xMax || d.y + r < yMin || d.y - r > yMax) {
                    return;
                }

                ctx.beginPath();
                ctx.arc(d.x, d.y, r, 0, 2 * Math.PI);

                const isHovered = hoveredNode === d;
                const isSelected = selectedNode === d;
                let color = categoryColors[d.category] || "#ffffff";
                
                if (isHovered || isSelected) {
                    // holographic iridescent linear gradient fill for active nodes!
                    const gradient = ctx.createLinearGradient(d.x - r, d.y - r, d.x + r, d.y + r);
                    gradient.addColorStop(0, '#d1aad7');   // Iridescent Pink
                    gradient.addColorStop(0.5, '#bbdef2'); // Iridescent Blue
                    gradient.addColorStop(1, '#f4f0ff');   // Lilac Haze
                    ctx.fillStyle = gradient;
                } else {
                    ctx.fillStyle = color;
                }

                let opacity = 1.0;
                if (focusNode) {
                    opacity = focusNeighbors.has(d.id) ? 1.0 : 0.08;
                } else {
                    opacity = nodeOpacityScale(d.inDegree);
                }

                ctx.globalAlpha = opacity;
                ctx.fill();

                const isHovered = hoveredNode === d;
                const isSelected = selectedNode === d;

                ctx.globalAlpha = focusNode ? (focusNeighbors.has(d.id) ? 1.0 : 0.08) : 1.0;
                if (isHovered || isSelected) {
                    ctx.strokeStyle = "#ffffff";
                    ctx.lineWidth = 2.5 / transform.k;
                    ctx.stroke();

                    // Iridescent flat outline ring (no glow/blur per Scale spec)
                    ctx.beginPath();
                    ctx.arc(d.x, d.y, r + 2, 0, 2 * Math.PI);
                    ctx.strokeStyle = "#bbdef2";
                    ctx.lineWidth = 1 / transform.k;
                    ctx.stroke();
                } else if (d.inDegree > maxInDegree * 0.15) {
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1.2 / transform.k;
                    ctx.stroke();
                } else {
                    ctx.strokeStyle = "rgba(0,0,0,0.65)";
                    ctx.lineWidth = 1 / transform.k;
                    ctx.stroke();
                }
            });

            ctx.globalAlpha = 1.0;
            ctx.restore();
        }

        // Mouse move and hover listeners on Canvas
        d3.select(canvas).on("mousemove", (event) => {
            if (!db) return;
            const transformState = d3.zoomTransform(canvas);
            const mouseX = (event.offsetX - transformState.x) / transformState.k;
            const mouseY = (event.offsetY - transformState.y) / transformState.k;

            let found = null;
            // Iterate backwards to hover nodes on top first
            for (let i = graphData.nodes.length - 1; i >= 0; i--) {
                const d = graphData.nodes[i];
                const dx = d.x - mouseX;
                const dy = d.y - mouseY;
                const r = nodeRadiusScale(d.views);
                const hitRadius = Math.max(r, 6);
                if (dx * dx + dy * dy < hitRadius * hitRadius) {
                    if (highPerfMode && isInteracting && transformState.k < 0.25 && d.inDegree < 5) {
                        continue;
                    }
                    found = d;
                    break;
                }
            }

            if (found !== hoveredNode) {
                hoveredNode = found;
                draw(); // redraw to update link highlighting

                if (hoveredNode) {
                    tooltip.style.opacity = 1;
                    document.getElementById("tt-title").textContent = hoveredNode.id;
                    const catEl = document.getElementById("tt-cat");
                    catEl.textContent = hoveredNode.category;
                    catEl.style.color = categoryColors[hoveredNode.category] || "var(--color-other)";
                } else {
                    tooltip.style.opacity = 0;
                }
            }

            if (hoveredNode) {
                tooltip.style.left = (event.pageX + 15) + "px";
                tooltip.style.top = (event.pageY - 15) + "px";
            }
        });

        d3.select(canvas).on("mouseout", () => {
            if (hoveredNode) {
                hoveredNode = null;
                tooltip.style.opacity = 0;
                draw();
            }
        });

        d3.select(canvas).on("click", (event) => {
            if (!db) return;
            const transformState = d3.zoomTransform(canvas);
            const mouseX = (event.offsetX - transformState.x) / transformState.k;
            const mouseY = (event.offsetY - transformState.y) / transformState.k;

            let found = null;
            for (let i = graphData.nodes.length - 1; i >= 0; i--) {
                const d = graphData.nodes[i];
                const dx = d.x - mouseX;
                const dy = d.y - mouseY;
                const r = nodeRadiusScale(d.views);
                const hitRadius = Math.max(r, 6);
                if (dx * dx + dy * dy < hitRadius * hitRadius) {
                    found = d;
                    break;
                }
            }

            if (found) {
                event.stopPropagation();
                selectNode(found);
            } else {
                deselectNode();
            }
        });

        // Close sidebar
        document.getElementById("sidebar-close").addEventListener("click", () => {
            deselectNode();
        });

        // Collapsible UI Panels Logic
        function setupCollapsiblePanel(panelId, closeId, toggleId) {
            const panel = document.getElementById(panelId);
            const closeBtn = document.getElementById(closeId);
            const toggleBtn = document.getElementById(toggleId);

            closeBtn.addEventListener("click", () => {
                panel.style.opacity = "0";
                panel.style.pointerEvents = "none";
                toggleBtn.style.opacity = "1";
                toggleBtn.style.pointerEvents = "auto";
            });

            toggleBtn.addEventListener("click", () => {
                panel.style.opacity = "1";
                panel.style.pointerEvents = "auto";
                toggleBtn.style.opacity = "0";
                toggleBtn.style.pointerEvents = "none";
            });
        }

        setupCollapsiblePanel("header-panel", "header-close", "header-toggle");
        setupCollapsiblePanel("stats-panel", "stats-close", "stats-toggle");
        setupCollapsiblePanel("controls-panel", "controls-close", "controls-toggle");
        setupCollapsiblePanel("legend-panel", "legend-close", "legend-toggle");

        // Combined Filters and Search Logic
        const searchBox = document.getElementById("search-box");
        const filterCategory = document.getElementById("filter-category");
        const filterViews = document.getElementById("filter-views");
        const filterViewsVal = document.getElementById("filter-views-val");
        const filterRef = document.getElementById("filter-ref");
        const filterRefVal = document.getElementById("filter-ref-val");

        function formatViewsCount(num) {
            if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
            if (num >= 1000) return (num / 1000).toFixed(0) + 'k';
            return num;
        }

        function applyFilters() {
            const minViews = +filterViews.value;
            const minRef = +filterRef.value;
            
            filterViewsVal.textContent = formatViewsCount(minViews);
            filterRefVal.textContent = minRef;
            
            deselectNode();
            executeQuery();
        }

        searchBox.addEventListener("input", applyFilters);
        filterCategory.addEventListener("change", applyFilters);
        filterViews.addEventListener("input", applyFilters);
        filterRef.addEventListener("input", applyFilters);

        // Helper: Select node and update sidebar details panel
        function selectNode(d) {
            selectedNode = d;
            
            const neighbors = new Set([d.id]);
            const connectionListItems = [];
            
            // Query connections from database
            const stmt = db.prepare("SELECT source, target FROM links WHERE source = :focusId OR target = :focusId");
            stmt.bind({ ":focusId": d.id });
            
            while (stmt.step()) {
                const row = stmt.getAsObject();
                if (row.source === d.id) {
                    // outbound connection: query target details
                    const detailsStmt = db.prepare("SELECT category FROM nodes WHERE id = :id");
                    detailsStmt.bind({ ":id": row.target });
                    if (detailsStmt.step()) {
                        const rowDetail = detailsStmt.getAsObject();
                        neighbors.add(row.target);
                        connectionListItems.push({id: row.target, category: rowDetail.category, type: "out"});
                    }
                    detailsStmt.free();
                } else {
                    // inbound connection: query source details
                    const detailsStmt = db.prepare("SELECT category FROM nodes WHERE id = :id");
                    detailsStmt.bind({ ":id": row.source });
                    if (detailsStmt.step()) {
                        const rowDetail = detailsStmt.getAsObject();
                        neighbors.add(row.source);
                        connectionListItems.push({id: row.source, category: rowDetail.category, type: "in"});
                    }
                    detailsStmt.free();
                }
            }
            stmt.free();

            // Redraw to focus clicked node in Canvas
            executeQuery();

            // Pan to center node smoothly
            const scale = Math.max(transform.k, 0.4);
            const x = width / 2 - d.x * scale;
            const y = height / 2 - d.y * scale;
            d3.select(canvas).transition().duration(750).call(
                zoom.transform,
                d3.zoomIdentity.translate(x, y).scale(scale)
            );

            // Populate sidebar DOM elements
            const tagEl = document.getElementById("sidebar-tag");
            tagEl.textContent = d.category;
            // tagEl.style.backgroundColor = categoryColors[d.category] || "var(--color-other)";
            
            document.getElementById("sidebar-title").textContent = d.id;
            document.getElementById("sidebar-views").textContent = d.views.toLocaleString();
            document.getElementById("sidebar-inbound").textContent = d.inDegree;
            document.getElementById("sidebar-outbound").textContent = d.outDegree;
            document.getElementById("sidebar-desc").innerHTML = d.snippet || "No excerpt details available for this Wikipedia article.";
            
            // Populate related articles list
            const connListContainer = document.getElementById("sidebar-connections");
            connListContainer.innerHTML = "";
            
            if (connectionListItems.length === 0) {
                connListContainer.innerHTML = "<li style='padding:12px; font-size:12px; color:var(--text-muted)'>No connections in crawled set.</li>";
            } else {
                connectionListItems.sort((a,b) => a.id.localeCompare(b.id)).forEach(conn => {
                    const item = document.createElement("li");
                    item.className = "connection-item";
                    const icon = conn.type === "out" ? "➔" : "➔";
                    const color = categoryColors[conn.category] || "var(--color-other)";
                    item.innerHTML = `
                        <div>
                            <span style="font-weight:normal; margin-right:8px; color:var(--color-bone)">${icon}</span>
                            <span>${conn.id}</span>
                        </div>
                        <span class="conn-cat">${conn.category}</span>
                    `;
                    item.addEventListener("click", () => {
                        // Query node details for selection
                        const nodeQuery = db.prepare("SELECT * FROM nodes WHERE id = :id");
                        nodeQuery.bind({ ":id": conn.id });
                        if (nodeQuery.step()) {
                            const nodeRow = nodeQuery.getAsObject();
                            selectNode({
                                id: nodeRow.id,
                                category: nodeRow.category,
                                views: nodeRow.views,
                                inDegree: nodeRow.inDegree,
                                outDegree: nodeRow.outDegree,
                                x: nodeRow.x,
                                y: nodeRow.y,
                                snippet: nodeRow.snippet
                            });
                        }
                        nodeQuery.free();
                    });
                    connListContainer.appendChild(item);
                });
            }

            // External Wikipedia URL link
            document.getElementById("sidebar-wiki-link").href = `https://en.wikipedia.org/wiki/${encodeURIComponent(d.id.replace(/ /g, '_'))}`;

            // Open sidebar Panel
            sidebar.classList.add("active");
            
            // Adjust overlays for layout shift
            document.getElementById("controls-panel").style.transform = "translateX(-450px)";
            document.getElementById("legend-panel").style.transform = "translateX(-450px)";
            document.getElementById("controls-toggle").style.transform = "translateX(-450px)";
            document.getElementById("legend-toggle").style.transform = "translateX(-450px)";
        }

        // Helper: Deselect node
        function deselectNode() {
            if (selectedNode === null) return;
            selectedNode = null;
            executeQuery();
                
            sidebar.classList.remove("active");
            document.getElementById("controls-panel").style.transform = "translateX(0)";
            document.getElementById("legend-panel").style.transform = "translateX(0)";
            document.getElementById("controls-toggle").style.transform = "translateX(0)";
            document.getElementById("legend-toggle").style.transform = "translateX(0)";
        }

        // Drag handlers (attached to canvas)
        d3.select(canvas).call(d3.drag()
            .container(canvas)
            .subject((event) => {
                if (!db) return null;
                const transformState = d3.zoomTransform(canvas);
                const mouseX = (event.sourceEvent.offsetX - transformState.x) / transformState.k;
                const mouseY = (event.sourceEvent.offsetY - transformState.y) / transformState.k;
                let found = null;
                for (let i = graphData.nodes.length - 1; i >= 0; i--) {
                    const d = graphData.nodes[i];
                    const dx = d.x - mouseX;
                    const dy = d.y - mouseY;
                    const r = nodeRadiusScale(d.views);
                    if (dx * dx + dy * dy < Math.max(r, 8) * Math.max(r, 8)) {
                        found = d;
                        break;
                    }
                }
                return found;
            })
            .on("start", dragstarted)
            .on("drag", dragged)
            .on("end", dragended));

        function dragstarted(event) {
            isInteracting = true;
            if (!event.active) simulation.alphaTarget(0.3).restart();
            event.subject.fx = event.subject.x;
            event.subject.fy = event.subject.y;
        }

        function dragged(event) {
            event.subject.fx = event.x;
            event.subject.fy = event.y;
        }

        function dragended(event) {
            isInteracting = false;
            if (!event.active) simulation.alphaTarget(0);
            event.subject.fx = null;
            event.subject.fy = null;
            executeQuery();
        }

        // Force Adjustments Event Handlers
        const sliderDistance = document.getElementById("slider-distance");
        sliderDistance.addEventListener("input", () => {
            currentDist = +sliderDistance.value;
            document.getElementById("dist-val").textContent = currentDist;
            simulation.force("link").distance(currentDist);
            simulation.alpha(0.3).restart();
        });

        const sliderCharge = document.getElementById("slider-charge");
        sliderCharge.addEventListener("input", () => {
            currentCharge = +sliderCharge.value;
            document.getElementById("charge-val").textContent = currentCharge;
            simulation.force("charge").strength(currentCharge);
            simulation.alpha(0.3).restart();
        });

        const sliderCollision = document.getElementById("slider-collision");
        sliderCollision.addEventListener("input", () => {
            currentCollision = +sliderCollision.value;
            document.getElementById("col-val").textContent = currentCollision;
            if (!highPerfMode) {
                simulation.force("collision").radius(d => nodeRadiusScale(d.views) + currentCollision - 16);
                simulation.alpha(0.3).restart();
            }
        });

        const sliderGravity = document.getElementById("slider-gravity");
        sliderGravity.addEventListener("input", () => {
            gravityStrength = +sliderGravity.value;
            document.getElementById("gravity-val").textContent = gravityStrength.toFixed(2);
            simulation.alpha(0.3).restart();
        });

        // High Performance Mode Settings Logic
        const highPerfToggle = document.getElementById("high-perf-toggle");
        
        function updateHighPerfSettings() {
            highPerfMode = highPerfToggle.checked;
            if (highPerfMode) {
                simulation.force("collision", null);
                simulation.alphaDecay(0.08);
            } else {
                simulation.force("collision", d3.forceCollide().radius(d => nodeRadiusScale(d.views) + currentCollision - 16).iterations(1));
                simulation.alphaDecay(0.0228);
            }
            executeQuery();
        }
        
        highPerfToggle.addEventListener("change", updateHighPerfSettings);
        
        // Resize handler
        window.addEventListener("resize", () => {
            resizeCanvas();
            simulation.force("center", d3.forceCenter(width / 2, height / 2));
            simulation.alpha(0.1).restart();
            draw();
        });

        // Initial boot trigger
        initDatabase();

        // Helper helper to strip string query
        String.prototype.strip = function() {
            return this.replace(/^\s+|\s+$/g, '');
        };
    