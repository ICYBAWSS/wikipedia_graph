// Graphology Web Worker - Graphology state manager
importScripts('https://unpkg.com/graphology@latest/dist/graphology.umd.js');

let graph = null;

self.onmessage = function(e) {
    const { type, data } = e.data;

    switch (type) {
        case 'init':
            // Instantiate graphology Graph
            graph = new graphology.Graph({ type: 'directed', allowSelfLoops: false });
            console.log("Graphology Worker: Graph initialized.");
            self.postMessage({ type: 'status', msg: 'Graphology initialized.' });
            break;

        case 'clear':
            if (graph) {
                graph.clear();
            }
            self.postMessage({ type: 'status', msg: 'Graph cleared.' });
            break;

        case 'load_graph':
            if (!graph) {
                graph = new graphology.Graph({ type: 'directed', allowSelfLoops: false });
            }
            const { nodes, links } = data;
            
            console.log(`Graphology Worker: Loading ${nodes.length} nodes and ${links.length} links...`);
            
            // Add nodes
            nodes.forEach(node => {
                graph.mergeNode(node.id, {
                    category: node.category,
                    views: node.views || 0,
                    inDegree: node.inDegree || 0,
                    outDegree: node.outDegree || 0,
                    snippet: node.snippet || ''
                });
            });

            // Add links
            links.forEach(link => {
                if (graph.hasNode(link.source) && graph.hasNode(link.target)) {
                    graph.mergeEdge(link.source, link.target);
                }
            });

            console.log(`Graphology Worker: Graph loaded. Nodes: ${graph.order}, Edges: ${graph.size}`);
            self.postMessage({ type: 'loaded', count: graph.order, edges: graph.size });
            break;

        case 'add_nodes_edges':
            if (!graph) {
                graph = new graphology.Graph({ type: 'directed', allowSelfLoops: false });
            }
            const newNodes = data.nodes || [];
            const newLinks = data.links || [];

            newNodes.forEach(node => {
                graph.mergeNode(node.id, {
                    category: node.category,
                    views: node.views || 0,
                    inDegree: node.inDegree || 0,
                    outDegree: node.outDegree || 0,
                    snippet: node.snippet || ''
                });
            });

            newLinks.forEach(link => {
                // Ensure nodes exist first
                if (!graph.hasNode(link.source)) graph.addNode(link.source);
                if (!graph.hasNode(link.target)) graph.addNode(link.target);
                graph.mergeEdge(link.source, link.target);
            });

            self.postMessage({ type: 'added', count: graph.order, edges: graph.size });
            break;

        case 'find_path':
            const { startId, endId } = data;
            console.log(`Graphology Worker: Running BFS between ${startId} and ${endId}`);
            
            const path = runBFS(startId, endId);
            self.postMessage({ type: 'path_result', startId, endId, path });
            break;

        case 'get_neighbors':
            const { nodeId } = data;
            if (!graph || !graph.hasNode(nodeId)) {
                self.postMessage({ type: 'neighbors_result', nodeId, neighbors: [] });
                return;
            }

            const neighbors = [];
            // Retrieve out-neighbors
            graph.outNeighbors(nodeId).forEach(neigh => {
                neighbors.push({
                    id: neigh,
                    attributes: graph.getNodeAttributes(neigh),
                    type: 'out'
                });
            });
            // Retrieve in-neighbors
            graph.inNeighbors(nodeId).forEach(neigh => {
                neighbors.push({
                    id: neigh,
                    attributes: graph.getNodeAttributes(neigh),
                    type: 'in'
                });
            });

            self.postMessage({ type: 'neighbors_result', nodeId, neighbors });
            break;

        default:
            console.warn("Graphology Worker: Unknown message type:", type);
    }
};

// Bidirectional BFS implementation using Graphology structure
function runBFS(startId, endId) {
    if (!graph || !graph.hasNode(startId) || !graph.hasNode(endId)) {
        console.error("BFS Error: Start or end node not found in Graphology graph.");
        return null;
    }
    if (startId === endId) return [startId];

    const startPreds = new Map([[startId, null]]);
    const endPreds = new Map([[endId, null]]);

    let startFrontier = [startId];
    let endFrontier = [endId];

    const maxDepth = 6;
    let depth = 0;

    while (startFrontier.length > 0 && endFrontier.length > 0 && depth < maxDepth) {
        // Expand forward from start
        const nextStartFrontier = [];
        for (const curr of startFrontier) {
            if (!graph.hasNode(curr)) continue;
            
            // Query out-neighbors
            const neighbors = graph.outNeighbors(curr);
            for (const neigh of neighbors) {
                if (!startPreds.has(neigh)) {
                    startPreds.set(neigh, curr);
                    nextStartFrontier.push(neigh);
                    if (endPreds.has(neigh)) {
                        return assembleBFSPath(startPreds, endPreds, neigh);
                    }
                }
            }
        }
        startFrontier = nextStartFrontier;
        depth++;

        if (startFrontier.length === 0 || depth >= maxDepth) break;

        // Expand backward from end
        const nextEndFrontier = [];
        for (const curr of endFrontier) {
            if (!graph.hasNode(curr)) continue;

            // Query in-neighbors (incoming edges)
            const neighbors = graph.inNeighbors(curr);
            for (const neigh of neighbors) {
                if (!endPreds.has(neigh)) {
                    endPreds.set(neigh, curr);
                    nextEndFrontier.push(neigh);
                    if (startPreds.has(neigh)) {
                        return assembleBFSPath(startPreds, endPreds, neigh);
                    }
                }
            }
        }
        endFrontier = nextEndFrontier;
        depth++;
    }

    return null;
}

function assembleBFSPath(startPreds, endPreds, meetingPoint) {
    const path = [];
    
    // Trace back from meeting point to start node
    let curr = meetingPoint;
    while (curr !== null) {
        path.unshift(curr);
        curr = startPreds.get(curr);
    }
    
    // Trace forward from meeting point to end node
    curr = endPreds.get(meetingPoint);
    while (curr !== null) {
        path.push(curr);
        curr = endPreds.get(curr);
    }
    
    return path;
}
