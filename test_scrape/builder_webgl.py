import os

def build():
    template_path = "template.html"
    output_path = "8m_optimized.html"
    
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
        
    # Remove inlined placeholders & D3 script
    d3_start = html.find('<script src="https://d3js.org/d3.v7.min.js"')
    placeholder_end = html.find('</script>', html.find('COMPRESSED_SQLITE_DB_PLACEHOLDER')) + 9
    
    head_prefix = html[:d3_start]
    body_suffix = html[placeholder_end:]
    
    # Inject sql-httpvfs.js
    script_inject = '<script src="libs/sql-httpvfs.js"></script>'
    assembled_html = head_prefix + script_inject + body_suffix
    
    # We want to replace the visualizer script section (from <!-- Visualizer Script --> to the end)
    script_marker = "<!-- Visualizer Script -->"
    marker_pos = assembled_html.find(script_marker)
    if marker_pos == -1:
        raise Exception("Could not find visualizer script marker in template.")
        
    base_html = assembled_html[:marker_pos + len(script_marker)]
    
    webgl_script = """
    <script type="module">
        const categoryColors = {
            "Biography & People": "#a8788a",
            "Science & Technology": "#588fa8",
            "History & Society": "#d35849",
            "Art & Culture": "#e8b840",
            "Philosophy & Religion": "#748c69",
            "Geography & Places": "#c58c64",
            "Other & General": "#c3aed6"
        };

        const topics = [
            "Biography & People", "Science & Technology", "Art & Culture", 
            "History & Society", "Geography & Places", "Philosophy & Religion", 
            "Other & General"
        ];

        // Global State & WebGL Configuration
        const { createDbWorker } = window;
        let db = null;
        let coordsArray = null; // Float32Array of interleaved x, y coordinates
        let colorData = null; // Uint8Array of RGB colors
        let renderLimit = 0;

        let selectedNodeIdx = -1;
        let hoveredNodeIdx = -1;

        const camera = {
            x: 0,
            y: 0,
            zoom: 0.12 // matches D3 starting zoom
        };

        const baseNodeSize = 3.5;
        let nodeSizeMultiplier = 1.0;
        let currentDensity = 10;
        const categoryVisibility = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0];

        // Background links (top_links.bin)
        let isTopLinksLoaded = false;
        let numTopLinks = 0;
        let topLinksIndexBuffer = null;
        let rawLinkIndices = null;
        let nodeDegrees = null;
        let cellRepresentatives = null;
        let cellLinks = null;
        let dynamicLinksCount = 0;
        let dynamicLinksIndexBuffer = null;

        // Selected node links
        let selectedLinksCount = 0;
        let selectedLinksIndexBuffer = null;
        const layoutBuffers = [];
        let showLinksDuringInteraction = false;

        // Grid parameters
        const springs = [0.005, 0.020, 0.070];
        const gravities = [0.001, 0.005, 0.025];
        const layoutUrls = [
            "/test_scrape/coords_k0_g0.bin",
            "/test_scrape/coords_k0_g1.bin",
            "/test_scrape/coords_k0_g2.bin",
            "/test_scrape/coords_k1_g0.bin",
            "/test_scrape/coordinates.bin", // coords_k1_g1
            "/test_scrape/coords_k1_g2.bin",
            "/test_scrape/coords_k2_g0.bin",
            "/test_scrape/coords_k2_g1.bin",
            "/test_scrape/coords_k2_g2.bin"
        ];
        const isLayoutLoaded = new Array(9).fill(false);
        const layoutDataArrays = new Array(9).fill(null);
        let coordsScaleFactor = 1.0;

        // Physics simulation state variables
        let currentLinkDistanceScale = 1.0;
        let currentChargeScale = 1.0;
        let currentGravityScale = 0.0;
        const galaxyCenters = new Float32Array(7 * 2); // Centers of mass

        function getPhysicalSpring() {
            const slider = document.getElementById("slider-distance");
            if (!slider) return 0.020;
            const d = parseFloat(slider.value);
            if (d <= 120) {
                return 0.005 + (d - 10) / (120 - 10) * (0.020 - 0.005);
            } else {
                return 0.020 + (d - 120) / (200 - 120) * (0.070 - 0.020);
            }
        }

        function getPhysicalGravity() {
            const slider = document.getElementById("slider-gravity");
            if (!slider) return 0.005;
            const gr = parseFloat(slider.value);
            if (gr <= 0.05) {
                return 0.001 + (gr - 0.0) / (0.05 - 0.0) * (0.005 - 0.001);
            } else {
                return 0.005 + (gr - 0.05) / (0.1 - 0.05) * (0.025 - 0.005);
            }
        }

        function getActiveGridCell(k, g) {
            let col = 0;
            if (k >= springs[1]) {
                col = 1;
            }
            let row = 0;
            if (g >= gravities[1]) {
                row = 1;
            }
            const k0 = springs[col];
            const k1 = springs[col + 1];
            const g0 = gravities[row];
            const g1 = gravities[row + 1];
            
            const tx = Math.max(0.0, Math.min(1.0, (k - k0) / (k1 - k0)));
            const ty = Math.max(0.0, Math.min(1.0, (g - g0) / (g1 - g0)));
            
            const idx00 = col * 3 + row;
            const idx10 = (col + 1) * 3 + row;
            const idx01 = col * 3 + (row + 1);
            const idx11 = (col + 1) * 3 + (row + 1);
            
            return { idx00, idx10, idx01, idx11, tx, ty };
        }

        function getTransformedCoords(nodeIdx) {
            if (!coordsArray || nodeIdx < 0 || nodeIdx * 2 >= coordsArray.length) {
                return [0, 0];
            }
            
            const k = getPhysicalSpring();
            const g = getPhysicalGravity();
            const cell = getActiveGridCell(k, g);
            
            function getLayoutCoords(layoutIdx, idx) {
                const arr = layoutDataArrays[layoutIdx];
                if (arr) {
                    return [arr[idx * 2], arr[idx * 2 + 1]];
                }
                const def = layoutDataArrays[4];
                if (def) {
                    return [def[idx * 2], def[idx * 2 + 1]];
                }
                return [0, 0];
            }
            
            const [x00, y00] = getLayoutCoords(cell.idx00, nodeIdx);
            const [x10, y10] = getLayoutCoords(cell.idx10, nodeIdx);
            const [x01, y01] = getLayoutCoords(cell.idx01, nodeIdx);
            const [x11, y11] = getLayoutCoords(cell.idx11, nodeIdx);
            
            const tx = cell.tx;
            const ty = cell.ty;
            
            const x = (1 - ty) * ((1 - tx) * x00 + tx * x10) + ty * ((1 - tx) * x01 + tx * x11);
            const y = (1 - ty) * ((1 - tx) * y00 + tx * y10) + ty * ((1 - tx) * y01 + tx * y11);
            
            return [x, y];
        }

        function getRawCoords(gx, gy) {
            if (isLayoutLoaded[4]) {
                return [gx, gy];
            }
            let rx = gx / (1.0 - currentGravityScale);
            let ry = gy / (1.0 - currentGravityScale);
            rx /= currentChargeScale;
            ry /= currentChargeScale;
            
            let nearestCx = 0, nearestCy = 0;
            let minDist2 = Infinity;
            for (let i = 0; i < 7; i++) {
                const cx = galaxyCenters[i * 2];
                const cy = galaxyCenters[i * 2 + 1];
                const d2 = (rx - cx) * (rx - cx) + (ry - cy) * (ry - cy);
                if (d2 < minDist2) {
                    minDist2 = d2;
                    nearestCx = cx;
                    nearestCy = cy;
                }
            }
            rx = nearestCx + (rx - nearestCx) / currentLinkDistanceScale;
            ry = nearestCy + (ry - nearestCy) / currentLinkDistanceScale;
            return [rx, ry];
        }

        let renderPending = false;
        function requestRender() {
            if (renderPending) return;
            renderPending = true;
            requestAnimationFrame(() => {
                render();
                renderPending = false;
            });
        }

        // Initialize Canvas & WebGL Context
        const canvas = document.getElementById("graph-canvas");
        const gl = canvas.getContext("webgl", { antialias: true, depth: false }) || 
                   canvas.getContext("experimental-webgl", { antialias: true, depth: false });

        if (!gl) {
            alert("WebGL is not supported in this browser.");
        }

        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

        // Initialize layout and index buffers after gl
        topLinksIndexBuffer = gl.createBuffer();
        selectedLinksIndexBuffer = gl.createBuffer();
        for (let i = 0; i < 9; i++) {
            layoutBuffers.push(gl.createBuffer());
        }

        // WebGL Point Shaders
        const vsPoint = `
            attribute vec2 a_pos00;
            attribute vec2 a_pos10;
            attribute vec2 a_pos01;
            attribute vec2 a_pos11;
            attribute vec3 a_color;
            attribute float a_categoryIdx;
            
            varying vec3 v_color;
            
            uniform vec2 u_camera;
            uniform float u_zoom;
            uniform vec2 u_resolution;
            uniform float u_pointSize;
            uniform float u_categoryVisibility[7];
            
            // Grid mix weights
            uniform float u_tx;
            uniform float u_ty;
            
            // Fallback physics uniforms
            uniform float u_useGrid;
            uniform vec2 u_galaxyCenters[7];
            uniform float u_linkDistanceScale;
            uniform float u_chargeScale;
            uniform float u_gravityScale;

            void main() {
                float visible = 1.0;
                for (int j = 0; j < 7; j++) {
                    if (float(j) == a_categoryIdx) {
                        visible = u_categoryVisibility[j];
                    }
                }
                if (visible < 0.5) {
                    gl_Position = vec4(9999.0, 9999.0, 9999.0, 1.0);
                    gl_PointSize = 0.0;
                } else {
                    vec2 pos = vec2(0.0);
                    if (u_useGrid > 0.5) {
                        pos = mix(mix(a_pos00, a_pos10, u_tx), mix(a_pos01, a_pos11, u_tx), u_ty);
                    } else {
                        pos = a_pos00;
                        vec2 center = vec2(0.0);
                        int catIdx = int(a_categoryIdx);
                        for (int j = 0; j < 7; j++) {
                            if (j == catIdx) {
                                center = u_galaxyCenters[j];
                            }
                        }
                        vec2 offset = pos - center;
                        pos = center + offset * u_linkDistanceScale;
                        pos = pos * u_chargeScale;
                        pos = pos * (1.0 - u_gravityScale);
                    }

                    vec2 screenPos = (pos - u_camera) * u_zoom;
                    vec2 clip = screenPos / (u_resolution * 0.5);
                    
                    // GPU-level viewport culling: Discard point if completely outside NDC space (+ margin)
                    if (abs(clip.x) > 1.05 || abs(clip.y) > 1.05) {
                        gl_Position = vec4(9999.0, 9999.0, 9999.0, 1.0);
                        gl_PointSize = 0.0;
                    } else {
                        gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
                        gl_PointSize = clamp(u_pointSize * sqrt(u_zoom), 1.0, 16.0);
                    }
                }
                v_color = a_color;
            }
        `;
        const fsPoint = `
            precision mediump float;
            varying vec3 v_color;
            void main() {
                vec2 circCoord = 2.0 * gl_PointCoord - 1.0;
                if (dot(circCoord, circCoord) > 1.0) {
                    discard;
                }
                gl_FragColor = vec4(v_color, 0.85);
            }
        `;
        const pointProgram = createProgram(gl, vsPoint, fsPoint);
        const aPos00Loc = gl.getAttribLocation(pointProgram, "a_pos00");
        const aPos10Loc = gl.getAttribLocation(pointProgram, "a_pos10");
        const aPos01Loc = gl.getAttribLocation(pointProgram, "a_pos01");
        const aPos11Loc = gl.getAttribLocation(pointProgram, "a_pos11");
        const aColorLoc = gl.getAttribLocation(pointProgram, "a_color");
        const aCategoryIdxLoc = gl.getAttribLocation(pointProgram, "a_categoryIdx");
        const uCameraLoc = gl.getUniformLocation(pointProgram, "u_camera");
        const uZoomLoc = gl.getUniformLocation(pointProgram, "u_zoom");
        const uResolutionLoc = gl.getUniformLocation(pointProgram, "u_resolution");
        const uPointSizeLoc = gl.getUniformLocation(pointProgram, "u_pointSize");
        const uCategoryVisibilityLoc = gl.getUniformLocation(pointProgram, "u_categoryVisibility");

        // Physics simulation locations
        const uUseGridLoc = gl.getUniformLocation(pointProgram, "u_useGrid");
        const uTxLoc = gl.getUniformLocation(pointProgram, "u_tx");
        const uTyLoc = gl.getUniformLocation(pointProgram, "u_ty");
        const uGalaxyCentersLoc = gl.getUniformLocation(pointProgram, "u_galaxyCenters");
        const uLinkDistanceScaleLoc = gl.getUniformLocation(pointProgram, "u_linkDistanceScale");
        const uChargeScaleLoc = gl.getUniformLocation(pointProgram, "u_chargeScale");
        const uGravityScaleLoc = gl.getUniformLocation(pointProgram, "u_gravityScale");

        const colorBuffer = gl.createBuffer();
        const categoryIdxBuffer = gl.createBuffer();

        // WebGL Line Shaders
        const vsLine = `
            attribute vec2 a_pos00;
            attribute vec2 a_pos10;
            attribute vec2 a_pos01;
            attribute vec2 a_pos11;
            attribute vec3 a_color;
            attribute float a_categoryIdx;
            
            varying vec3 v_color;
            
            uniform vec2 u_camera;
            uniform float u_zoom;
            uniform vec2 u_resolution;
            uniform float u_categoryVisibility[7];
            
            // Grid mix weights
            uniform float u_tx;
            uniform float u_ty;
            
            // Fallback physics uniforms
            uniform float u_useGrid;
            uniform vec2 u_galaxyCenters[7];
            uniform float u_linkDistanceScale;
            uniform float u_chargeScale;
            uniform float u_gravityScale;

            void main() {
                float visible = 1.0;
                for (int j = 0; j < 7; j++) {
                    if (float(j) == a_categoryIdx) {
                        visible = u_categoryVisibility[j];
                    }
                }
                
                if (visible < 0.5) {
                    gl_Position = vec4(9999.0, 9999.0, 9999.0, 1.0);
                } else {
                    vec2 pos = vec2(0.0);
                    if (u_useGrid > 0.5) {
                        pos = mix(mix(a_pos00, a_pos10, u_tx), mix(a_pos01, a_pos11, u_tx), u_ty);
                    } else {
                        pos = a_pos00;
                        vec2 center = vec2(0.0);
                        int catIdx = int(a_categoryIdx);
                        for (int j = 0; j < 7; j++) {
                            if (j == catIdx) {
                                center = u_galaxyCenters[j];
                            }
                        }
                        vec2 offset = pos - center;
                        pos = center + offset * u_linkDistanceScale;
                        pos = pos * u_chargeScale;
                        pos = pos * (1.0 - u_gravityScale);
                    }
                    
                    vec2 screenPos = (pos - u_camera) * u_zoom;
                    vec2 clip = screenPos / (u_resolution * 0.5);
                    gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
                }
                v_color = a_color;
            }
        `;
        const fsLine = `
            precision mediump float;
            varying vec3 v_color;
            uniform float u_opacity;
            void main() {
                gl_FragColor = vec4(v_color, u_opacity);
            }
        `;
        const lineProgram = createProgram(gl, vsLine, fsLine);
        const aLinePos00Loc = gl.getAttribLocation(lineProgram, "a_pos00");
        const aLinePos10Loc = gl.getAttribLocation(lineProgram, "a_pos10");
        const aLinePos01Loc = gl.getAttribLocation(lineProgram, "a_pos01");
        const aLinePos11Loc = gl.getAttribLocation(lineProgram, "a_pos11");
        const aLineColorLoc = gl.getAttribLocation(lineProgram, "a_color");
        const aLineCategoryIdxLoc = gl.getAttribLocation(lineProgram, "a_categoryIdx");
        
        const uLineCameraLoc = gl.getUniformLocation(lineProgram, "u_camera");
        const uLineZoomLoc = gl.getUniformLocation(lineProgram, "u_zoom");
        const uLineResolutionLoc = gl.getUniformLocation(lineProgram, "u_resolution");
        const uLineCategoryVisibilityLoc = gl.getUniformLocation(lineProgram, "u_categoryVisibility");
        
        const uLineUseGridLoc = gl.getUniformLocation(lineProgram, "u_useGrid");
        const uLineTxLoc = gl.getUniformLocation(lineProgram, "u_tx");
        const uLineTyLoc = gl.getUniformLocation(lineProgram, "u_ty");
        const uLineOpacityLoc = gl.getUniformLocation(lineProgram, "u_opacity");
        
        const uLineGalaxyCentersLoc = gl.getUniformLocation(lineProgram, "u_galaxyCenters");
        const uLineLinkDistanceScaleLoc = gl.getUniformLocation(lineProgram, "u_linkDistanceScale");
        const uLineChargeScaleLoc = gl.getUniformLocation(lineProgram, "u_chargeScale");
        const uLineGravityScaleLoc = gl.getUniformLocation(lineProgram, "u_gravityScale");

        const highlightBuffer = gl.createBuffer();

        function createProgram(gl, vsSource, fsSource) {
            const vs = gl.createShader(gl.VERTEX_SHADER);
            gl.shaderSource(vs, vsSource);
            gl.compileShader(vs);
            if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
                console.error("VS error:", gl.getShaderInfoLog(vs));
            }
            const fs = gl.createShader(gl.FRAGMENT_SHADER);
            gl.shaderSource(fs, fsSource);
            gl.compileShader(fs);
            if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
                console.error("FS error:", gl.getShaderInfoLog(fs));
            }
            const prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
                console.error("Link error:", gl.getProgramInfoLog(prog));
            }
            return prog;
        }

        // Camera Map coordinates translation
        function graphToScreen(gx, gy) {
            const halfW = canvas.clientWidth / 2;
            const halfH = canvas.clientHeight / 2;
            const px = halfW + (gx - camera.x) * camera.zoom;
            const py = halfH + (gy - camera.y) * camera.zoom;
            return [px, py];
        }

        function screenToGraph(px, py) {
            const halfW = canvas.clientWidth / 2;
            const halfH = canvas.clientHeight / 2;
            const gx = camera.x + (px - halfW) / camera.zoom;
            const gy = camera.y + (py - halfH) / camera.zoom;
            return [gx, gy];
        }

        // Uniform Spatial Grid for selection search
        const K = 500;
        const cellSize = 4000 / K; // Expanded size slightly to handle the 1900 coordinate bounds
        const grid = new Array(K * K);
        let nodeCellIdx = null;

        function buildSpatialGrid() {
            console.log("Building spatial lookup grid...");
            nodeCellIdx = new Int32Array(renderLimit);
            nodeCellIdx.fill(-1);
            for (let i = 0; i < K * K; i++) {
                grid[i] = [];
            }
            for (let i = 0; i < renderLimit; i++) {
                const x = coordsArray[i * 2];
                const y = coordsArray[i * 2 + 1];
                
                // Add boundary checks and safe fallbacks
                if (isNaN(x) || isNaN(y)) continue;
                
                const cx = Math.floor((x + 2000) / cellSize);
                const cy = Math.floor((y + 2000) / cellSize);
                
                const clampedX = Math.max(0, Math.min(cx, K - 1));
                const clampedY = Math.max(0, Math.min(cy, K - 1));
                
                const cellIdx = clampedY * K + clampedX;
                if (!grid[cellIdx]) grid[cellIdx] = [];
                grid[cellIdx].push(i);
                nodeCellIdx[i] = cellIdx;
            }
            console.log("Spatial lookup grid built successfully.");
        }

        function initLinkRenderingStructures() {
            console.log("Initializing link rendering structures (degrees, representatives, spatial indexes)...");
            
            // 1. Compute node degrees in the top links subgraph
            nodeDegrees = new Int32Array(renderLimit);
            for (let i = 0; i < numTopLinks; i++) {
                const src = rawLinkIndices[i * 2];
                const tgt = rawLinkIndices[i * 2 + 1];
                if (src < renderLimit && tgt < renderLimit) {
                    nodeDegrees[src]++;
                    nodeDegrees[tgt]++;
                }
            }
            
            // 2. Determine cell representative node (node in that cell with highest degree)
            cellRepresentatives = new Int32Array(K * K);
            cellRepresentatives.fill(-1);
            for (let c = 0; c < K * K; c++) {
                const indices = grid[c];
                if (indices && indices.length > 0) {
                    let maxDeg = -1;
                    let repIdx = indices[0];
                    for (let i = 0; i < indices.length; i++) {
                        const idx = indices[i];
                        const deg = nodeDegrees[idx];
                        if (deg > maxDeg) {
                            maxDeg = deg;
                            repIdx = idx;
                        }
                    }
                    cellRepresentatives[c] = repIdx;
                }
            }
            
            // 3. Map cell index to the list of top links touching it
            cellLinks = [];
            for (let i = 0; i < K * K; i++) {
                cellLinks[i] = [];
            }
            for (let i = 0; i < numTopLinks; i++) {
                const src = rawLinkIndices[i * 2];
                const tgt = rawLinkIndices[i * 2 + 1];
                if (src >= renderLimit || tgt >= renderLimit) continue;
                const cellSrc = nodeCellIdx[src];
                const cellTgt = nodeCellIdx[tgt];
                if (cellSrc !== -1) cellLinks[cellSrc].push(i);
                if (cellTgt !== -1 && cellTgt !== cellSrc) {
                    cellLinks[cellTgt].push(i);
                }
            }
            console.log("Link rendering structures successfully initialized.");
        }

        function updateDynamicLinks() {
            if (!isTopLinksLoaded || !rawLinkIndices || !nodeCellIdx || !cellLinks) return;
            
            // 1. Calculate camera bounds in graph units
            const halfW = canvas.width / 2;
            const halfH = canvas.height / 2;
            const xMin = camera.x - halfW / camera.zoom;
            const xMax = camera.x + halfW / camera.zoom;
            const yMin = camera.y - halfH / camera.zoom;
            const yMax = camera.y + halfH / camera.zoom;
            
            // 2. Decide bundling scale S based on zoom level
            let S = 1;
            if (camera.zoom < 0.015) {
                S = 25;
            } else if (camera.zoom < 0.05) {
                S = 10;
            } else if (camera.zoom < 0.15) {
                S = 4;
            } else if (camera.zoom < 0.40) {
                S = 2;
            } else {
                S = 1;
            }

            // Extract layout weights & arrays for fast interpolation
            const k = getPhysicalSpring();
            const g = getPhysicalGravity();
            const cell = getActiveGridCell(k, g);
            const tx = cell.tx;
            const ty = cell.ty;
            const w00 = (1 - ty) * (1 - tx);
            const w10 = (1 - ty) * tx;
            const w01 = ty * (1 - tx);
            const w11 = ty * tx;
            
            const defArr = layoutDataArrays[4];
            const arr00 = layoutDataArrays[cell.idx00] || defArr;
            const arr10 = layoutDataArrays[cell.idx10] || defArr;
            const arr01 = layoutDataArrays[cell.idx01] || defArr;
            const arr11 = layoutDataArrays[cell.idx11] || defArr;

            if (!defArr) return; // not loaded yet

            const tempIndices = [];
            
            if (S === 1) {
                // Fully zoomed in: Direct viewport culling via AABB
                for (let i = 0; i < numTopLinks; i++) {
                    const src = rawLinkIndices[i * 2];
                    const tgt = rawLinkIndices[i * 2 + 1];
                    if (src >= renderLimit || tgt >= renderLimit) continue;
                    
                    const src2 = src * 2;
                    const tgt2 = tgt * 2;
                    
                    // Fast interpolation
                    const xSrc = w00 * arr00[src2] + w10 * arr10[src2] + w01 * arr01[src2] + w11 * arr11[src2];
                    const ySrc = w00 * arr00[src2 + 1] + w10 * arr10[src2 + 1] + w01 * arr01[src2 + 1] + w11 * arr11[src2 + 1];
                    
                    const xTgt = w00 * arr00[tgt2] + w10 * arr10[tgt2] + w01 * arr01[tgt2] + w11 * arr11[tgt2];
                    const yTgt = w00 * arr00[tgt2 + 1] + w10 * arr10[tgt2 + 1] + w01 * arr01[tgt2 + 1] + w11 * arr11[tgt2 + 1];
                    
                    const minX = xSrc < xTgt ? xSrc : xTgt;
                    const maxX = xSrc > xTgt ? xSrc : xTgt;
                    const minY = ySrc < yTgt ? ySrc : yTgt;
                    const maxY = ySrc > yTgt ? ySrc : yTgt;
                    
                    if (minX <= xMax && maxX >= xMin && minY <= yMax && maxY >= yMin) {
                        tempIndices.push(src, tgt);
                    }
                }
            } else {
                // Zoomed out: AABB viewport check first, then bundle those links
                const coarsePairs = new Map();
                const numCoarseCols = Math.ceil(K / S);
                
                for (let i = 0; i < numTopLinks; i++) {
                    const src = rawLinkIndices[i * 2];
                    const tgt = rawLinkIndices[i * 2 + 1];
                    if (src >= renderLimit || tgt >= renderLimit) continue;
                    
                    const src2 = src * 2;
                    const tgt2 = tgt * 2;
                    
                    // Fast interpolation
                    const xSrc = w00 * arr00[src2] + w10 * arr10[src2] + w01 * arr01[src2] + w11 * arr11[src2];
                    const ySrc = w00 * arr00[src2 + 1] + w10 * arr10[src2 + 1] + w01 * arr01[src2 + 1] + w11 * arr11[src2 + 1];
                    
                    const xTgt = w00 * arr00[tgt2] + w10 * arr10[tgt2] + w01 * arr01[tgt2] + w11 * arr11[tgt2];
                    const yTgt = w00 * arr00[tgt2 + 1] + w10 * arr10[tgt2 + 1] + w01 * arr01[tgt2 + 1] + w11 * arr11[tgt2 + 1];
                    
                    const minX = xSrc < xTgt ? xSrc : xTgt;
                    const maxX = xSrc > xTgt ? xSrc : xTgt;
                    const minY = ySrc < yTgt ? ySrc : yTgt;
                    const maxY = ySrc > yTgt ? ySrc : yTgt;
                    
                    // Viewport check
                    if (minX <= xMax && maxX >= xMin && minY <= yMax && maxY >= yMin) {
                        const cellSrc = nodeCellIdx[src];
                        const cellTgt = nodeCellIdx[tgt];
                        if (cellSrc === -1 || cellTgt === -1) continue;
                        
                        const cx_src = cellSrc % K;
                        const cy_src = Math.floor(cellSrc / K);
                        const cx_tgt = cellTgt % K;
                        const cy_tgt = Math.floor(cellTgt / K);
                        
                        const ccx_src = Math.floor(cx_src / S);
                        const ccy_src = Math.floor(cy_src / S);
                        const ccx_tgt = Math.floor(cx_tgt / S);
                        const ccy_tgt = Math.floor(cy_tgt / S);
                        
                        if (ccx_src === ccx_tgt && ccy_src === ccy_tgt) {
                            continue; // ignore intra-cell links
                        }
                        
                        const id_src = ccy_src * numCoarseCols + ccx_src;
                        const id_tgt = ccy_tgt * numCoarseCols + ccx_tgt;
                        
                        const key = id_src < id_tgt ? (id_src << 16) | id_tgt : (id_tgt << 16) | id_src;
                        
                        if (!coarsePairs.has(key)) {
                            coarsePairs.set(key, {
                                ccx_src, ccy_src,
                                ccx_tgt, ccy_tgt
                            });
                        }
                    }
                }
                
                // Get coarse representatives and add to indices
                coarsePairs.forEach(pair => {
                    const repSrc = getCoarseRepresentative(pair.ccx_src, pair.ccy_src, S);
                    const repTgt = getCoarseRepresentative(pair.ccx_tgt, pair.ccy_tgt, S);
                    if (repSrc !== -1 && repTgt !== -1 && repSrc !== repTgt) {
                        tempIndices.push(repSrc, repTgt);
                    }
                });
            }
            
            // 4. Upload indices to the GPU
            dynamicLinksCount = tempIndices.length / 2;
            
            // Lazy initialization of dynamicLinksIndexBuffer
            if (!dynamicLinksIndexBuffer) {
                dynamicLinksIndexBuffer = gl.createBuffer();
            }
            
            if (dynamicLinksCount > 0) {
                gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, dynamicLinksIndexBuffer);
                gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(tempIndices), gl.STREAM_DRAW);
            }
        }
        
        function getCoarseRepresentative(ccx, ccy, S) {
            let bestNode = -1;
            let maxDeg = -1;
            const cxStart = ccx * S;
            const cxEnd = Math.min(K - 1, cxStart + S - 1);
            const cyStart = ccy * S;
            const cyEnd = Math.min(K - 1, cyStart + S - 1);
            
            for (let cy = cyStart; cy <= cyEnd; cy++) {
                for (let cx = cxStart; cx <= cxEnd; cx++) {
                    const cellIdx = cy * K + cx;
                    const rep = cellRepresentatives[cellIdx];
                    if (rep !== -1) {
                        const deg = nodeDegrees[rep];
                        if (deg > maxDeg) {
                            maxDeg = deg;
                            bestNode = rep;
                        }
                    }
                }
            }
            return bestNode;
        }

        function findNearestNode(gx, gy, maxDistanceGraphUnits) {
            // gx, gy are transformed graph coordinates. Convert them back to raw to search in spatial grid.
            const [rx, ry] = getRawCoords(gx, gy);
            const cx = Math.floor((rx + 2000) / cellSize);
            const cy = Math.floor((ry + 2000) / cellSize);
            
            let nearestIdx = -1;
            let minD2 = maxDistanceGraphUnits * maxDistanceGraphUnits;
            
            for (let dy = -1; dy <= 1; dy++) {
                for (let dx = -1; dx <= 1; dx++) {
                    const curCx = cx + dx;
                    const curCy = cy + dy;
                    if (curCx < 0 || curCx >= K || curCy < 0 || curCy >= K) continue;
                    
                    const cellIdx = curCy * K + curCx;
                    const nodeIndices = grid[cellIdx];
                    if (!nodeIndices) continue;
                    
                    for (let i = 0; i < nodeIndices.length; i++) {
                        const idx = nodeIndices[i];
                        const catIdx = idx % topics.length;
                        if (categoryVisibility[catIdx] < 0.5) continue;
                        
                        const [nx, ny] = getTransformedCoords(idx);
                        const dist2 = (nx - gx) * (nx - gx) + (ny - gy) * (ny - gy);
                        if (dist2 < minD2) {
                            minD2 = dist2;
                            nearestIdx = idx;
                        }
                    }
                }
            }
            return nearestIdx;
        }

        function getVisibleNodeIds(xMin, xMax, yMin, yMax, maxCount = 300) {
            if (!coordsArray) return [];
            
            const [rxMin, ryMin] = getRawCoords(xMin, yMin);
            const [rxMax, ryMax] = getRawCoords(xMax, yMax);
            
            const rMinX = Math.min(rxMin, rxMax);
            const rMaxX = Math.max(rxMin, rxMax);
            const rMinY = Math.min(ryMin, ryMax);
            const rMaxY = Math.max(ryMin, ryMax);

            const cxMin = Math.max(0, Math.min(K - 1, Math.floor((rMinX + 2000) / cellSize)));
            const cxMax = Math.max(0, Math.min(K - 1, Math.floor((rMaxX + 2000) / cellSize)));
            const cyMin = Math.max(0, Math.min(K - 1, Math.floor((rMinY + 2000) / cellSize)));
            const cyMax = Math.max(0, Math.min(K - 1, Math.floor((rMaxY + 2000) / cellSize)));
            
            const visibleIds = [];
            for (let cy = cyMin; cy <= cyMax; cy++) {
                for (let cx = cxMin; cx <= cxMax; cx++) {
                    const cellIdx = cy * K + cx;
                    const indices = grid[cellIdx];
                    if (!indices) continue;
                    for (let i = 0; i < indices.length; i++) {
                        const idx = indices[i];
                        const catIdx = idx % topics.length;
                        if (categoryVisibility[catIdx] < 0.5) continue;

                        const [x, y] = getTransformedCoords(idx);
                        if (x >= xMin && x <= xMax && y >= yMin && y <= yMax) {
                            visibleIds.push('Node_' + String(idx + 1).padStart(7, '0'));
                            if (visibleIds.length >= maxCount) {
                                return visibleIds;
                            }
                        }
                    }
                }
            }
            return visibleIds;
        }

        // Background link queries are replaced by top_links.bin loaded once at startup.
        // We keep helper functions and background grid layout loader here.
        async function fetchTopLinks() {
            try {
                const loadingText = getOrCreateLoadingText();
                if (loadingText) loadingText.textContent = "Loading 128,000 top link connections...";
                console.log("Fetching top_links.bin...");
                const res = await fetch("/test_scrape/top_links.bin");
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const buf = await res.arrayBuffer();
                
                const view = new DataView(buf);
                numTopLinks = view.getUint32(0, true);
                console.log(`Loaded ${numTopLinks} top links from binary.`);
                
                rawLinkIndices = new Uint32Array(buf, 4, numTopLinks * 2);
                
                // Initialize the node degrees, cell representatives, and cellLinks index on the CPU
                initLinkRenderingStructures();
                
                isTopLinksLoaded = true;
                requestRender();
            } catch (err) {
                console.error("Failed to fetch top_links.bin:", err);
            }
        }

        async function loadBackgroundLayouts() {
            console.log("Background layout loading bypassed (single coordinate set mode).");
        }

        // Remote Logging Bridge
        (function() {
            const originalLog = console.log;
            const originalError = console.error;
            const originalWarn = console.warn;

            async function sendToRelay(type, args) {
                const message = Array.from(args).map(arg => {
                    if (arg instanceof Error) {
                        return `${arg.message}\n${arg.stack}`;
                    }
                    if (arg && typeof arg === 'object') {
                        if (arg.message && arg.stack) {
                            return `${arg.message}\n${arg.stack}`;
                        }
                        try {
                            return JSON.stringify(arg, null, 2);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                
                try {
                    await fetch('http://localhost:8000/log', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ type, message })
                    });
                } catch (e) {}
            }

            console.log = function() {
                originalLog.apply(console, arguments);
                sendToRelay('log', arguments);
            };
            console.error = function() {
                originalError.apply(console, arguments);
                sendToRelay('error', arguments);
            };
            console.warn = function() {
                originalWarn.apply(console, arguments);
                sendToRelay('warn', arguments);
            };

            window.addEventListener('error', function(event) {
                sendToRelay('exception', [event.message, event.filename, event.lineno]);
            });

            window.addEventListener('unhandledrejection', function(event) {
                sendToRelay('unhandledrejection', [event.reason ? (event.reason.message || String(event.reason)) : 'Unhandled rejection']);
            });
        })();

        // WebGL Render Loop
        function render() {
            // Background color #2c2720
            gl.clearColor(44/255, 39/255, 32/255, 1.0);
            gl.clear(gl.COLOR_BUFFER_BIT);

            if (!coordsArray || renderLimit === 0) return;

            gl.useProgram(pointProgram);

            // Set Point Uniforms
            gl.uniform2f(uCameraLoc, camera.x, camera.y);
            gl.uniform1f(uZoomLoc, camera.zoom);
            gl.uniform2f(uResolutionLoc, canvas.width, canvas.height);
            gl.uniform1f(uPointSizeLoc, baseNodeSize * nodeSizeMultiplier);
            gl.uniform1fv(uCategoryVisibilityLoc, new Float32Array(categoryVisibility));

            // Set Grid/Physics parameters
            const k = getPhysicalSpring();
            const g = getPhysicalGravity();
            const cell = getActiveGridCell(k, g);
            
            const buf00 = isLayoutLoaded[cell.idx00] ? layoutBuffers[cell.idx00] : layoutBuffers[4];
            const buf10 = isLayoutLoaded[cell.idx10] ? layoutBuffers[cell.idx10] : layoutBuffers[4];
            const buf01 = isLayoutLoaded[cell.idx01] ? layoutBuffers[cell.idx01] : layoutBuffers[4];
            const buf11 = isLayoutLoaded[cell.idx11] ? layoutBuffers[cell.idx11] : layoutBuffers[4];
            
            const useGrid = (isLayoutLoaded[cell.idx00] && isLayoutLoaded[cell.idx10] && 
                             isLayoutLoaded[cell.idx01] && isLayoutLoaded[cell.idx11]) ? 1.0 : 0.0;

            gl.uniform1f(uUseGridLoc, useGrid);
            gl.uniform1f(uTxLoc, cell.tx);
            gl.uniform1f(uTyLoc, cell.ty);

            gl.uniform2fv(uGalaxyCentersLoc, galaxyCenters);
            gl.uniform1f(uLinkDistanceScaleLoc, currentLinkDistanceScale);
            gl.uniform1f(uChargeScaleLoc, currentChargeScale);
            gl.uniform1f(uGravityScaleLoc, currentGravityScale);

            // Bind Node Positions
            gl.bindBuffer(gl.ARRAY_BUFFER, buf00);
            gl.enableVertexAttribArray(aPos00Loc);
            gl.vertexAttribPointer(aPos00Loc, 2, gl.FLOAT, false, 0, 0);

            gl.bindBuffer(gl.ARRAY_BUFFER, buf10);
            gl.enableVertexAttribArray(aPos10Loc);
            gl.vertexAttribPointer(aPos10Loc, 2, gl.FLOAT, false, 0, 0);

            gl.bindBuffer(gl.ARRAY_BUFFER, buf01);
            gl.enableVertexAttribArray(aPos01Loc);
            gl.vertexAttribPointer(aPos01Loc, 2, gl.FLOAT, false, 0, 0);

            gl.bindBuffer(gl.ARRAY_BUFFER, buf11);
            gl.enableVertexAttribArray(aPos11Loc);
            gl.vertexAttribPointer(aPos11Loc, 2, gl.FLOAT, false, 0, 0);

            // Bind Node Colors
            gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
            gl.enableVertexAttribArray(aColorLoc);
            gl.vertexAttribPointer(aColorLoc, 3, gl.UNSIGNED_BYTE, true, 0, 0);

            // Bind Category Indices
            gl.bindBuffer(gl.ARRAY_BUFFER, categoryIdxBuffer);
            gl.enableVertexAttribArray(aCategoryIdxLoc);
            gl.vertexAttribPointer(aCategoryIdxLoc, 1, gl.UNSIGNED_BYTE, false, 0, 0);

            // Draw Nodes
            gl.drawArrays(gl.POINTS, 0, renderLimit);

            // Draw background link connections (top links) as thin, faint category-colored lines
            const isMoving = isDragging || cameraAnimation;
            if (isTopLinksLoaded && numTopLinks > 0 && (!isMoving || showLinksDuringInteraction)) {
                // Perform dynamic CPU-side viewport culling and edge bundling
                updateDynamicLinks();
                
                if (dynamicLinksCount > 0) {
                    gl.useProgram(lineProgram);
                    gl.uniform2f(uLineCameraLoc, camera.x, camera.y);
                    gl.uniform1f(uLineZoomLoc, camera.zoom);
                    gl.uniform2f(uLineResolutionLoc, canvas.width, canvas.height);
                    gl.uniform1fv(uLineCategoryVisibilityLoc, new Float32Array(categoryVisibility));
                    
                    const opacity = isMoving ? 0.02 : 0.08;
                    gl.uniform1f(uLineOpacityLoc, opacity);

                    gl.uniform1f(uLineUseGridLoc, useGrid);
                    gl.uniform1f(uLineTxLoc, cell.tx);
                    gl.uniform1f(uLineTyLoc, cell.ty);
                    gl.uniform2fv(uLineGalaxyCentersLoc, galaxyCenters);
                    gl.uniform1f(uLineLinkDistanceScaleLoc, currentLinkDistanceScale);
                    gl.uniform1f(uLineChargeScaleLoc, currentChargeScale);
                    gl.uniform1f(uLineGravityScaleLoc, currentGravityScale);

                    gl.bindBuffer(gl.ARRAY_BUFFER, buf00);
                    gl.enableVertexAttribArray(aLinePos00Loc);
                    gl.vertexAttribPointer(aLinePos00Loc, 2, gl.FLOAT, false, 0, 0);

                    gl.bindBuffer(gl.ARRAY_BUFFER, buf10);
                    gl.enableVertexAttribArray(aLinePos10Loc);
                    gl.vertexAttribPointer(aLinePos10Loc, 2, gl.FLOAT, false, 0, 0);

                    gl.bindBuffer(gl.ARRAY_BUFFER, buf01);
                    gl.enableVertexAttribArray(aLinePos01Loc);
                    gl.vertexAttribPointer(aLinePos01Loc, 2, gl.FLOAT, false, 0, 0);

                    gl.bindBuffer(gl.ARRAY_BUFFER, buf11);
                    gl.enableVertexAttribArray(aLinePos11Loc);
                    gl.vertexAttribPointer(aLinePos11Loc, 2, gl.FLOAT, false, 0, 0);

                    // Disable color array and use constant warm gray color #9d9894
                    gl.disableVertexAttribArray(aLineColorLoc);
                    gl.vertexAttrib3f(aLineColorLoc, 157/255, 152/255, 148/255);

                    gl.bindBuffer(gl.ARRAY_BUFFER, categoryIdxBuffer);
                    gl.enableVertexAttribArray(aLineCategoryIdxLoc);
                    gl.vertexAttribPointer(aLineCategoryIdxLoc, 1, gl.UNSIGNED_BYTE, false, 0, 0);

                    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, dynamicLinksIndexBuffer);
                    gl.drawElements(gl.LINES, dynamicLinksCount * 2, gl.UNSIGNED_INT, 0);
                }
            }

            // Draw selected node/path link connections as solid lines
            if (selectedNodeIdx !== -1 && selectedLinksCount > 0) {
                gl.useProgram(lineProgram);
                gl.uniform2f(uLineCameraLoc, camera.x, camera.y);
                gl.uniform1f(uLineZoomLoc, camera.zoom);
                gl.uniform2f(uLineResolutionLoc, canvas.width, canvas.height);
                gl.uniform1fv(uLineCategoryVisibilityLoc, new Float32Array(categoryVisibility));
                gl.uniform1f(uLineOpacityLoc, 0.45);

                gl.uniform1f(uLineUseGridLoc, useGrid);
                gl.uniform1f(uLineTxLoc, cell.tx);
                gl.uniform1f(uLineTyLoc, cell.ty);
                gl.uniform2fv(uLineGalaxyCentersLoc, galaxyCenters);
                gl.uniform1f(uLineLinkDistanceScaleLoc, currentLinkDistanceScale);
                gl.uniform1f(uLineChargeScaleLoc, currentChargeScale);
                gl.uniform1f(uLineGravityScaleLoc, currentGravityScale);

                gl.bindBuffer(gl.ARRAY_BUFFER, buf00);
                gl.enableVertexAttribArray(aLinePos00Loc);
                gl.vertexAttribPointer(aLinePos00Loc, 2, gl.FLOAT, false, 0, 0);

                gl.bindBuffer(gl.ARRAY_BUFFER, buf10);
                gl.enableVertexAttribArray(aLinePos10Loc);
                gl.vertexAttribPointer(aLinePos10Loc, 2, gl.FLOAT, false, 0, 0);

                gl.bindBuffer(gl.ARRAY_BUFFER, buf01);
                gl.enableVertexAttribArray(aLinePos01Loc);
                gl.vertexAttribPointer(aLinePos01Loc, 2, gl.FLOAT, false, 0, 0);

                gl.bindBuffer(gl.ARRAY_BUFFER, buf11);
                gl.enableVertexAttribArray(aLinePos11Loc);
                gl.vertexAttribPointer(aLinePos11Loc, 2, gl.FLOAT, false, 0, 0);

                // Disable color array and use constant warm color #d6d2ca
                gl.disableVertexAttribArray(aLineColorLoc);
                gl.vertexAttrib3f(aLineColorLoc, 214/255, 210/255, 202/255);

                gl.bindBuffer(gl.ARRAY_BUFFER, categoryIdxBuffer);
                gl.enableVertexAttribArray(aLineCategoryIdxLoc);
                gl.vertexAttribPointer(aLineCategoryIdxLoc, 1, gl.UNSIGNED_BYTE, false, 0, 0);

                gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, selectedLinksIndexBuffer);
                gl.drawElements(gl.LINES, selectedLinksCount, gl.UNSIGNED_INT, 0);
            }

            // Draw hovered point highlight
            if (hoveredNodeIdx !== -1) {
                const [hx, hy] = getTransformedCoords(hoveredNodeIdx);
                drawHighlightPoint(hx, hy, [255/255, 255/255, 255/255], 10.0);
            }

            // Draw selected point highlight
            if (selectedNodeIdx !== -1) {
                const [sx, sy] = getTransformedCoords(selectedNodeIdx);
                drawHighlightPoint(sx, sy, [232/255, 184/255, 64/255], 14.0);
            }
        }

        function drawHighlightPoint(gx, gy, color, size) {
            gl.useProgram(pointProgram);

            gl.uniform2f(uCameraLoc, camera.x, camera.y);
            gl.uniform1f(uZoomLoc, camera.zoom);
            gl.uniform2f(uResolutionLoc, canvas.width, canvas.height);
            gl.uniform1f(uPointSizeLoc, size);

            gl.uniform1f(uUseGridLoc, 1.0);
            gl.uniform1f(uTxLoc, 0.0);
            gl.uniform1f(uTyLoc, 0.0);

            gl.bindBuffer(gl.ARRAY_BUFFER, highlightBuffer);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([gx, gy]), gl.DYNAMIC_DRAW);
            
            gl.enableVertexAttribArray(aPos00Loc);
            gl.vertexAttribPointer(aPos00Loc, 2, gl.FLOAT, false, 0, 0);
            gl.enableVertexAttribArray(aPos10Loc);
            gl.vertexAttribPointer(aPos10Loc, 2, gl.FLOAT, false, 0, 0);
            gl.enableVertexAttribArray(aPos01Loc);
            gl.vertexAttribPointer(aPos01Loc, 2, gl.FLOAT, false, 0, 0);
            gl.enableVertexAttribArray(aPos11Loc);
            gl.vertexAttribPointer(aPos11Loc, 2, gl.FLOAT, false, 0, 0);

            gl.disableVertexAttribArray(aColorLoc);
            gl.vertexAttrib3f(aColorLoc, color[0], color[1], color[2]);

            gl.disableVertexAttribArray(aCategoryIdxLoc);
            gl.vertexAttrib1f(aCategoryIdxLoc, 999.0);

            gl.drawArrays(gl.POINTS, 0, 1);
        }

        function resizeCanvas() {
            const dpr = window.devicePixelRatio || 1;
            canvas.width = canvas.clientWidth * dpr;
            canvas.height = canvas.clientHeight * dpr;
            gl.viewport(0, 0, canvas.width, canvas.height);
            requestRender();
        }

        window.addEventListener("resize", resizeCanvas);

        // Interaction event handlers (Drag and Center-Zoom)
        let isDragging = false;
        let startMouseX = 0;
        let startMouseY = 0;
        let startCamX = 0;
        let startCamY = 0;

        canvas.addEventListener("mousedown", (e) => {
            if (e.button !== 0) return;
            isDragging = true;
            startMouseX = e.clientX;
            startMouseY = e.clientY;
            startCamX = camera.x;
            startCamY = camera.y;
            if (cameraAnimation) {
                cancelAnimationFrame(cameraAnimation.rafId);
                cameraAnimation = null;
            }
        });

        window.addEventListener("mousemove", (e) => {
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            if (isDragging) {
                const dx = e.clientX - startMouseX;
                const dy = e.clientY - startMouseY;
                camera.x = startCamX - dx / camera.zoom;
                camera.y = startCamY - dy / camera.zoom;
                requestRender();
            } else {
                // Hover detection
                const [gx, gy] = screenToGraph(mx, my);
                const hoverRadius = 15 / camera.zoom;
                const idx = findNearestNode(gx, gy, hoverRadius);

                const tooltip = document.getElementById("tooltip");
                if (idx !== -1) {
                    hoveredNodeIdx = idx;
                    const idStr = 'Node_' + String(idx + 1).padStart(7, '0');
                    const cat = topics[idx % topics.length];
                    
                    tooltip.style.display = "block";
                    document.getElementById("tt-title").textContent = idStr;
                    document.getElementById("tt-cat").textContent = cat + " (Click to load details)";
                    tooltip.style.left = (e.clientX + 15) + "px";
                    tooltip.style.top = (e.clientY + 15) + "px";
                    requestRender();
                } else {
                    if (hoveredNodeIdx !== -1) {
                        hoveredNodeIdx = -1;
                        tooltip.style.display = "none";
                        requestRender();
                    }
                }
            }
        });

        window.addEventListener("mouseup", () => {
            isDragging = false;
        });

        canvas.addEventListener("wheel", (e) => {
            e.preventDefault();
            if (cameraAnimation) {
                cancelAnimationFrame(cameraAnimation.rafId);
                cameraAnimation = null;
            }
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            const [gx, gy] = screenToGraph(mx, my);

            const zoomFactor = e.deltaY < 0 ? 1.15 : 1.0 / 1.15;
            camera.zoom = Math.max(0.005, Math.min(camera.zoom * zoomFactor, 100.0));

            const halfW = canvas.clientWidth / 2;
            const halfH = canvas.clientHeight / 2;
            camera.x = gx - (mx - halfW) / camera.zoom;
            camera.y = gy - (my - halfH) / camera.zoom;

            requestRender();
        }, { passive: false });

        canvas.addEventListener("click", async (e) => {
            if (isDragging && (Math.abs(e.clientX - startMouseX) > 3 || Math.abs(e.clientY - startMouseY) > 3)) {
                return;
            }
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            const [gx, gy] = screenToGraph(mx, my);
            const clickRadius = 15 / camera.zoom;
            const idx = findNearestNode(gx, gy, clickRadius);

            if (idx !== -1) {
                await selectNode(idx);
            } else {
                selectedNodeIdx = -1;
                selectedLinksCount = 0;
                hideSidebar();
                requestRender();
            }
        });

        // Camera Animation
        let cameraAnimation = null;
        function animateCameraTo(targetX, targetY, targetZoom, durationMs = 750) {
            if (cameraAnimation) {
                cancelAnimationFrame(cameraAnimation.rafId);
            }
            const startTime = performance.now();
            const startX = camera.x;
            const startY = camera.y;
            const startZoom = camera.zoom;

            function step(now) {
                const elapsed = now - startTime;
                const progress = Math.min(elapsed / durationMs, 1.0);
                const t = 1 - Math.pow(1 - progress, 3); // easeOutCubic
                
                camera.x = startX + (targetX - startX) * t;
                camera.y = startY + (targetY - startY) * t;
                camera.zoom = startZoom + (targetZoom - startZoom) * t;
                
                render();
                
                if (progress < 1.0) {
                    cameraAnimation.rafId = requestAnimationFrame(step);
                } else {
                    cameraAnimation = null;
                }
            }
            cameraAnimation = {
                rafId: requestAnimationFrame(step)
            };
        }

        async function selectNode(nodeIndex) {
            selectedNodeIdx = nodeIndex;
            const idStr = 'Node_' + String(nodeIndex + 1).padStart(7, '0');
            
            const [sx, sy] = getTransformedCoords(nodeIndex);
            
            animateCameraTo(sx, sy, Math.max(camera.zoom, 1.2));
            
            await showNodeDetails(idStr);
        }

        // Initialize SQLite HTTP VFS
        function getOrCreateLoadingText() {
            const loadingScreen = document.getElementById("loading-screen");
            if (!loadingScreen) return null;
            let loadingText = document.getElementById("loading-text");
            if (!loadingText) {
                for (let i = 0; i < loadingScreen.children.length; i++) {
                    const child = loadingScreen.children[i];
                    if (child.tagName === "DIV" && !child.classList.contains("loader-ring") && !child.classList.contains("eyebrow")) {
                        child.id = "loading-text";
                        loadingText = child;
                        break;
                    }
                }
                if (!loadingText) {
                    loadingText = document.createElement("div");
                    loadingText.id = "loading-text";
                    loadingText.style.fontSize = "11px";
                    loadingText.style.color = "var(--color-ash)";
                    loadingText.style.marginTop = "8px";
                    loadingText.style.textAlign = "center";
                    loadingScreen.appendChild(loadingText);
                }
            }
            return loadingText;
        }

        async function initSQLiteVFS() {
            const loadingText = getOrCreateLoadingText();
            if (loadingText) loadingText.textContent = "Connecting to 35GB SQLite VFS...";
            try {
                const worker = await createDbWorker(
                    [{
                        from: "inline",
                        config: {
                            serverMode: "full",
                            url: "/test_scrape/wiki_simulation.db",
                            requestChunkSize: 4096
                        }
                    }],
                    "/test_scrape/libs/sqlite.worker.js",
                    "/test_scrape/libs/sql-wasm.wasm"
                );
                db = worker.db;
                console.log("SQLite HTTP VFS connected successfully.");
            } catch (err) {
                console.error("VFS Connection Failed:", err);
                if (loadingText) loadingText.innerHTML = `<span style="color:#d35849">VFS Error: ${err.message}. Ensure log_server.py is running.</span>`;
                throw err;
            }
        }

        // Initialize Coordinate Loader and WebGL Engine
        async function initGraphEngine() {
            const loadingText = getOrCreateLoadingText();
            if (loadingText) loadingText.textContent = "Spawning coordinates loader worker...";
            console.log("initGraphEngine: Spawning coordinates loader worker.");

            // Zero-copy Web Worker to load 64MB coordinates.bin in-place
            const workerCode = `
                self.onmessage = async function(e) {
                    const { binUrl } = e.data;
                    console.log("Worker: received msg. binUrl =", binUrl);
                    try {
                        self.postMessage({ type: 'progress', msg: 'Fetching 64MB binary positions...' });
                        const response = await fetch(binUrl);
                        console.log("Worker: fetch returned status =", response.status);
                        if (!response.ok) throw new Error('HTTP status ' + response.status);
                        
                        const buffer = await response.arrayBuffer();
                        const view = new DataView(buffer);
                        const count = view.getUint32(0, true);
                        console.log("Worker: Parse done. count =", count);
                        
                        self.postMessage({ type: 'progress', msg: 'Parsing ' + count.toLocaleString() + ' coordinate positions...' });
                        
                        const coords = new Float32Array(buffer, 4, count * 2);
                        
                        // Dynamic normalization to fit within [-1800, 1800]
                        let maxVal = 0;
                        for (let i = 0; i < count * 2; i++) {
                            const val = Math.abs(coords[i]);
                            if (val > maxVal) maxVal = val;
                        }
                        const scale = maxVal > 0 ? 1800.0 / maxVal : 1.0;
                        
                        for (let i = 0; i < count * 2; i++) {
                            coords[i] *= scale;
                        }
                        
                        self.postMessage({ 
                            type: 'done', 
                            count: count,
                            coords: coords.buffer,
                            scale: scale
                        }, [coords.buffer]);
                    } catch (err) {
                        console.error("Worker Error:", err);
                        self.postMessage({ type: 'error', msg: err.message });
                    }
                };
            `;
            const workerBlob = new Blob([workerCode], { type: 'application/javascript' });
            const worker = new Worker(URL.createObjectURL(workerBlob));
            console.log("initGraphEngine: Worker spawned successfully.");

            worker.onmessage = function(e) {
                console.log("initGraphEngine: Main thread received message:", e.data.type);
                if (e.data.type === 'progress') {
                    if (loadingText) loadingText.textContent = e.data.msg;
                } else if (e.data.type === 'error') {
                    if (loadingText) loadingText.innerHTML = `<span style="color:#d35849">Worker Error: ${e.data.msg}</span>`;
                } else if (e.data.type === 'done') {
                    if (loadingText) loadingText.textContent = "Initializing WebGL Engine...";
                    
                    renderLimit = e.data.count;
                    coordsArray = new Float32Array(e.data.coords);
                    coordsScaleFactor = e.data.scale;

                    // Compute centers of gravity for the 7 category galaxies
                    const counts = new Float32Array(7);
                    galaxyCenters.fill(0);
                    for (let i = 0; i < renderLimit; i++) {
                        const catIdx = i % 7;
                        galaxyCenters[catIdx * 2] += coordsArray[i * 2];
                        galaxyCenters[catIdx * 2 + 1] += coordsArray[i * 2 + 1];
                        counts[catIdx]++;
                    }
                    for (let i = 0; i < 7; i++) {
                        if (counts[i] > 0) {
                            galaxyCenters[i * 2] /= counts[i];
                            galaxyCenters[i * 2 + 1] /= counts[i];
                        }
                    }

                    // Generate colors deterministically
                    console.log("Generating RGB colors for 8,000,000 nodes...");
                    colorData = new Uint8Array(renderLimit * 3);
                    for (let i = 0; i < renderLimit; i++) {
                        const cat = topics[i % topics.length];
                        const hex = categoryColors[cat] || "#c3aed6";
                        colorData[i * 3] = parseInt(hex.slice(1, 3), 16);
                        colorData[i * 3 + 1] = parseInt(hex.slice(3, 5), 16);
                        colorData[i * 3 + 2] = parseInt(hex.slice(5, 7), 16);
                    }

                    // Generate category indices
                    console.log("Generating category indices for 8,000,000 nodes...");
                    const categoryIndices = new Uint8Array(renderLimit);
                    for (let i = 0; i < renderLimit; i++) {
                        categoryIndices[i] = i % topics.length;
                    }

                    // Populate WebGL Buffers
                    gl.bindBuffer(gl.ARRAY_BUFFER, layoutBuffers[4]);
                    gl.bufferData(gl.ARRAY_BUFFER, coordsArray, gl.STATIC_DRAW);

                    for (let i = 0; i < 9; i++) {
                        layoutDataArrays[i] = coordsArray;
                        isLayoutLoaded[i] = true;
                        layoutBuffers[i] = layoutBuffers[4];
                    }

                    gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
                    gl.bufferData(gl.ARRAY_BUFFER, colorData, gl.STATIC_DRAW);

                    gl.bindBuffer(gl.ARRAY_BUFFER, categoryIdxBuffer);
                    gl.bufferData(gl.ARRAY_BUFFER, categoryIndices, gl.STATIC_DRAW);

                    // Build Spatial Grid
                    buildSpatialGrid();

                    // Map stats
                    document.getElementById("stat-nodes").textContent = renderLimit.toLocaleString();
                    document.getElementById("stat-links").textContent = "186,481,888";
                    document.getElementById("stat-hub").textContent = "Node_0000001";

                    // Disable physics sliders because settings are baked in coordinates.bin
                    ["slider-distance", "slider-strength", "slider-charge", "slider-collision", "slider-gravity"].forEach(id => {
                        const slider = document.getElementById(id);
                        if (slider) {
                            slider.disabled = true;
                            const group = slider.closest(".control-group");
                            if (group) {
                                group.style.opacity = "0.4";
                                group.style.pointerEvents = "none";
                                // Add baked label indicator next to label
                                const label = group.querySelector(".control-label");
                                if (label && !label.innerHTML.includes("(Baked)")) {
                                    label.innerHTML += ' <span style="font-size:10px; color:var(--color-yellow);">(Baked)</span>';
                                }
                            }
                        }
                    });
                    const hpToggle = document.getElementById("high-perf-toggle");
                    if (hpToggle) {
                        hpToggle.checked = true;
                        hpToggle.disabled = true;
                        const group = hpToggle.closest(".control-group");
                        if (group) {
                            group.style.opacity = "0.6";
                            group.style.pointerEvents = "none";
                        }
                    }

                    // Fetch top links and layouts
                    fetchTopLinks();
                    loadBackgroundLayouts();

                    // Hide loading overlay
                    setTimeout(() => {
                        const loaderScr = document.getElementById("loading-screen");
                        if (loaderScr) {
                            loaderScr.style.opacity = '0';
                            setTimeout(() => {
                                loaderScr.style.display = 'none';
                            }, 500);
                        }
                    }, 500);

                    resizeCanvas();
                }
            };

            worker.postMessage({ binUrl: new URL('/test_scrape/coordinates.bin', window.location.href).href });
        }

        let activeQueryNodeId = null;

        // VFS details query and Sidebar population
        async function showNodeDetails(nodeId) {
            activeQueryNodeId = nodeId;
            const sidebar = document.getElementById("detail-sidebar");
            
            // Query details from nodes table
            const rows = await db.query('SELECT category, views, inDegree, outDegree, snippet FROM nodes WHERE id = ?', [nodeId]);
            if (activeQueryNodeId !== nodeId) return;
            if (!rows || rows.length === 0) return;

            const n = rows[0];
            document.getElementById("sidebar-tag").textContent = n.category;
            document.getElementById("sidebar-tag").style.color = categoryColors[n.category] || "#9d9894";
            document.getElementById("sidebar-title").textContent = nodeId;
            document.getElementById("sidebar-views").textContent = parseInt(n.views).toLocaleString();
            document.getElementById("sidebar-inbound").textContent = n.inDegree;
            document.getElementById("sidebar-outbound").textContent = n.outDegree;
            document.getElementById("sidebar-desc").textContent = n.snippet || "No excerpt.";
            document.getElementById("sidebar-wiki-link").href = `https://en.wikipedia.org/wiki/${encodeURIComponent(nodeId)}`;

            // Populate connections
            const connList = document.getElementById("sidebar-connections");
            connList.innerHTML = "";
            
            const selectedLinksIndices = [];

            const linksRows = await db.query(`
                SELECT target as id, context, 'out' as type FROM links WHERE source = ? 
                UNION ALL 
                SELECT source as id, context, 'in' as type FROM links WHERE target = ? 
                ORDER BY type DESC LIMIT ?
            `, [nodeId, nodeId, currentDensity]) || [];
            if (activeQueryNodeId !== nodeId) return;

            const nodeIndex = parseInt(nodeId.split('_')[1]) - 1;

            linksRows.forEach(row => {
                const item = document.createElement("li");
                item.className = "connection-item";
                
                const main = document.createElement("div");
                main.className = "connection-main";
                main.innerHTML = `<span>${row.id}</span><span class="connection-tag">${row.type}</span>`;
                main.onclick = async (e) => {
                    e.stopPropagation();
                    const targetIndex = parseInt(row.id.split('_')[1]) - 1;
                    await selectNode(targetIndex);
                };
                item.appendChild(main);

                if (row.context) {
                    const ctxDiv = document.createElement("div");
                    ctxDiv.className = "connection-context";
                    ctxDiv.textContent = `"${row.context}"`;
                    ctxDiv.onclick = (e) => {
                        e.stopPropagation();
                        ctxDiv.classList.toggle("expanded");
                    };
                    item.appendChild(ctxDiv);
                }

                connList.appendChild(item);

                const targetIndex = parseInt(row.id.split('_')[1]) - 1;
                if (!isNaN(targetIndex) && targetIndex >= 0 && targetIndex < renderLimit) {
                    selectedLinksIndices.push(nodeIndex, targetIndex);
                }
            });

            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, selectedLinksIndexBuffer);
            gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(selectedLinksIndices), gl.DYNAMIC_DRAW);
            selectedLinksCount = selectedLinksIndices.length;

            // Open sidebar
            sidebar.classList.add("active");
            
            // Adjust other panels for desktop viewport
            if (window.innerWidth > 1024) {
                ["controls-panel", "legend-panel", "controls-toggle", "legend-toggle"].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.style.transform = "translateX(-450px)";
                });
            }
            requestRender();
        }

        function hideSidebar() {
            const sidebar = document.getElementById("detail-sidebar");
            if (sidebar) sidebar.classList.remove("active");
            ["controls-panel", "legend-panel", "controls-toggle", "legend-toggle"].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.style.transform = "translateX(0)";
            });
        }

        document.getElementById("sidebar-close").addEventListener("click", () => {
            selectedNodeIdx = -1;
            selectedLinksCount = 0;
            hideSidebar();
            requestRender();
        });

        // Autocomplete Search matching logic (FTS5 based)
        const searchBox = document.getElementById("search-box");
        let searchTimeout = null;
        searchBox.addEventListener("input", (e) => {
            clearTimeout(searchTimeout);
            const query = e.target.value.trim();
            if (query.length < 2) {
                return;
            }

            searchTimeout = setTimeout(async () => {
                const rows = await db.query(`SELECT id, category FROM nodes_fts WHERE id MATCH ? LIMIT 10`, [query + '*']) || [];
                const datalist = document.getElementById("article-list");
                datalist.innerHTML = "";
                rows.forEach(row => {
                    const opt = document.createElement("option");
                    opt.value = row.id;
                    datalist.appendChild(opt);
                });
            }, 100);
        });

        searchBox.addEventListener("change", async (e) => {
            const val = e.target.value.trim();
            if (!val) return;
            const idx = parseInt(val.split('_')[1]) - 1;
            if (isNaN(idx) || idx < 0 || idx >= renderLimit) return;

            await selectNode(idx);
        });

        // Collapsible Panel Toggles
        function setupCollapsiblePanel(panelId, toggleId) {
            const panel = document.getElementById(panelId);
            const toggleBtn = document.getElementById(toggleId);
            if (!panel || !toggleBtn) return;
            toggleBtn.addEventListener("click", () => {
                const isCollapsed = panel.classList.toggle("collapsed");
                toggleBtn.classList.toggle("active", !isCollapsed);
                if (!isCollapsed && window.innerWidth <= 768) {
                    document.querySelectorAll('div.panel:not(#' + panelId + ')').forEach(p => { if (p.id !== 'detail-sidebar') p.classList.add('collapsed'); });
                    document.querySelectorAll('.toggle-btn:not(#' + toggleId + ')').forEach(b => b.classList.remove('active'));
                }
            });
        }
        setupCollapsiblePanel("header-panel", "header-toggle");
        setupCollapsiblePanel("stats-panel", "stats-toggle");
        setupCollapsiblePanel("controls-panel", "controls-toggle");
        setupCollapsiblePanel("legend-panel", "legend-toggle");

        // Pathfinder Results
        const findRouteBtn = document.getElementById("find-route-btn");
        const routeStart = document.getElementById("route-start");
        const routeEnd = document.getElementById("route-end");
        const routeContainer = document.getElementById("route-result-container");
        const routePath = document.getElementById("route-text-path");

        findRouteBtn.addEventListener("click", async () => {
            const startId = routeStart.value.trim();
            const endId = routeEnd.value.trim();

            if (!startId || !endId) {
                alert("Please enter both Start and End Article IDs.");
                return;
            }

            findRouteBtn.disabled = true;
            routeContainer.style.display = "none";

            try {
                const path = await runBidirectionalBFS(startId, endId);
                if (path) {
                    routePath.innerHTML = "";
                    path.forEach((nodeId, idx) => {
                        const step = document.createElement("div");
                        step.className = "route-step";
                        step.style.cursor = "pointer";
                        step.innerHTML = `<span style="opacity:0.5; font-family:var(--font-geist);">${idx+1}.</span> <strong>${nodeId}</strong>`;
                        step.onclick = async () => {
                            const nodeIndex = parseInt(nodeId.split('_')[1]) - 1;
                            await selectNode(nodeIndex);
                        };
                        routePath.appendChild(step);
                    });
                    routeContainer.style.display = "block";

                    // Highlight the full path dynamically on WebGL!
                    const routeIndices = [];
                    for (let i = 0; i < path.length - 1; i++) {
                        const idx = parseInt(path[i].split('_')[1]) - 1;
                        const nextIdx = parseInt(path[i+1].split('_')[1]) - 1;
                        if (!isNaN(idx) && idx >= 0 && idx < renderLimit && !isNaN(nextIdx) && nextIdx >= 0 && nextIdx < renderLimit) {
                            routeIndices.push(idx, nextIdx);
                        }
                    }
                    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, selectedLinksIndexBuffer);
                    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(routeIndices), gl.DYNAMIC_DRAW);
                    selectedLinksCount = routeIndices.length;
                    
                    // Center camera on start node
                    const startIdx = parseInt(path[0].split('_')[1]) - 1;
                    await selectNode(startIdx);
                } else {
                    alert("No link path found between these articles.");
                }
            } catch (err) {
                console.error("BFS Error:", err);
                alert("Error during pathfinding: " + err.message);
            } finally {
                findRouteBtn.disabled = false;
            }
        });

        // Bidirectional BFS implementation
        async function runBidirectionalBFS(startId, endId) {
            if (startId === endId) return [startId];

            const startPreds = new Map([[startId, null]]);
            const endPreds = new Map([[endId, null]]);

            let startFrontier = [startId];
            let endFrontier = [endId];

            const maxDepth = 6;
            let depth = 0;

            while (startFrontier.length > 0 && endFrontier.length > 0 && depth < maxDepth) {
                const nextStartFrontier = [];
                const queryNodes = startFrontier;
                const placeholders = queryNodes.map(() => '?').join(',');
                
                const result = await db.exec(`SELECT source, target FROM links WHERE source IN (${placeholders}) OR target IN (${placeholders})`, [...queryNodes, ...queryNodes]);
                const rows = (result && result[0] && result[0].values) || [];
                for (const [src, tgt] of rows) {
                    for (const [curr, neigh] of [[src, tgt], [tgt, src]]) {
                        if (queryNodes.includes(curr)) {
                            if (!startPreds.has(neigh)) {
                                startPreds.set(neigh, curr);
                                nextStartFrontier.push(neigh);
                                if (endPreds.has(neigh)) {
                                    return buildPath(startPreds, endPreds, neigh);
                                }
                            }
                        }
                    }
                }
                startFrontier = nextStartFrontier;
                depth++;

                if (startFrontier.length === 0 || depth >= maxDepth) break;

                const nextEndFrontier = [];
                const queryNodesEnd = endFrontier;
                const placeholdersEnd = queryNodesEnd.map(() => '?').join(',');

                const resultEnd = await db.exec(`SELECT source, target FROM links WHERE source IN (${placeholdersEnd}) OR target IN (${placeholdersEnd})`, [...queryNodesEnd, ...queryNodesEnd]);
                const rowsEnd = (resultEnd && resultEnd[0] && resultEnd[0].values) || [];
                for (const [src, tgt] of rowsEnd) {
                    for (const [curr, neigh] of [[src, tgt], [tgt, src]]) {
                        if (queryNodesEnd.includes(curr)) {
                            if (!endPreds.has(neigh)) {
                                endPreds.set(neigh, curr);
                                nextEndFrontier.push(neigh);
                                if (startPreds.has(neigh)) {
                                    return buildPath(startPreds, endPreds, neigh);
                                }
                            }
                        }
                    }
                }
                endFrontier = nextEndFrontier;
                depth++;
            }
            return null;
        }

        function buildPath(startPreds, endPreds, intersect) {
            const pathStart = [];
            let curr = intersect;
            while (curr !== null) {
                pathStart.push(curr);
                curr = startPreds.get(curr);
            }
            pathStart.reverse();

            const pathEnd = [];
            curr = endPreds.get(intersect);
            while (curr !== null) {
                pathEnd.push(curr);
                curr = endPreds.get(curr);
            }
            return [...pathStart, ...pathEnd];
        }

        // Legend Category Toggling Setup
        const legendList = document.getElementById("legend-list");
        const EYE_ON_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px; height:14px; display:block;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
        const EYE_OFF_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px; height:14px; display:block;"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;

        topics.forEach((topic, idx) => {
            const color = categoryColors[topic] || "#c3aed6";
            const item = document.createElement("div");
            item.className = "legend-item";
            item.innerHTML = `
                <div class="legend-left" style="display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none;">
                    <div class="legend-color" style="width: 10px; height: 10px; border-radius: 50%; background-color: ${color}"></div>
                    <span>${topic}</span>
                </div>
                <button class="legend-toggle-btn" style="background: none; border: none; color: var(--color-ash); cursor: pointer; display: flex; align-items: center; padding: 4px;">${EYE_ON_SVG}</button>
            `;
            const btn = item.querySelector(".legend-toggle-btn");
            const left = item.querySelector(".legend-left");
            
            const toggle = () => {
                const isHidden = categoryVisibility[idx] < 0.5;
                if (isHidden) {
                    categoryVisibility[idx] = 1.0;
                    btn.innerHTML = EYE_ON_SVG;
                    btn.style.color = "var(--color-ash)";
                    left.style.opacity = "1.0";
                } else {
                    categoryVisibility[idx] = 0.0;
                    btn.innerHTML = EYE_OFF_SVG;
                    btn.style.color = "rgba(255,255,255,0.15)";
                    left.style.opacity = "0.35";
                }
                requestRender();
            };
            btn.onclick = toggle;
            left.onclick = toggle;
            legendList.appendChild(item);
        });

        // Interaction links toggle handler
        const interactionToggle = document.getElementById("interaction-links-toggle");
        if (interactionToggle) {
            interactionToggle.addEventListener("change", (e) => {
                showLinksDuringInteraction = e.target.checked;
                requestRender();
            });
        }

        // Slider control event handlers
        const sizeSlider = document.getElementById("slider-size");
        const sizeVal = document.getElementById("size-val");
        if (sizeSlider && sizeVal) {
            sizeSlider.addEventListener("input", (e) => {
                nodeSizeMultiplier = parseFloat(e.target.value);
                sizeVal.textContent = nodeSizeMultiplier.toFixed(1);
                requestRender();
            });
        }

        const densitySlider = document.getElementById("slider-density");
        const densityVal = document.getElementById("density-val");
        if (densitySlider && densityVal) {
            densitySlider.addEventListener("input", (e) => {
                currentDensity = parseInt(e.target.value);
                densityVal.textContent = currentDensity;
                if (selectedNodeIdx !== -1) {
                    const idStr = 'Node_' + String(selectedNodeIdx + 1).padStart(7, '0');
                    showNodeDetails(idStr);
                }
            });
        }

        const distSlider = document.getElementById("slider-distance");
        const distVal = document.getElementById("dist-val");
        if (distSlider && distVal) {
            distSlider.addEventListener("input", (e) => {
                const val = parseFloat(e.target.value);
                distVal.textContent = val;
                currentLinkDistanceScale = val / 120.0;
                requestRender();
            });
        }

        const chargeSlider = document.getElementById("slider-charge");
        const chargeVal = document.getElementById("charge-val");
        if (chargeSlider && chargeVal) {
            chargeSlider.addEventListener("input", (e) => {
                const val = parseFloat(e.target.value);
                chargeVal.textContent = val;
                currentChargeScale = -val / 200.0;
                requestRender();
            });
        }

        const gravSlider = document.getElementById("slider-gravity");
        const gravVal = document.getElementById("gravity-val");
        if (gravSlider && gravVal) {
            gravSlider.addEventListener("input", (e) => {
                const val = parseFloat(e.target.value);
                gravVal.textContent = val.toFixed(2);
                currentGravityScale = (val - 0.05) * 2.0;
                requestRender();
            });
        }

        // Initialize App
        async function run() {
            console.log("run: Starting app initialization.");
            try {
                await initSQLiteVFS();
                console.log("run: initSQLiteVFS completed. Calling initGraphEngine.");
                await initGraphEngine();
                console.log("run: initGraphEngine completed.");
            } catch (err) {
                console.error("run: Init Error:", err);
            }
        }

        run();
    </script>
    </body>
    </html>
    """
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(base_html + webgl_script)
    print(f"Successfully compiled {output_path}")

if __name__ == "__main__":
    build()
