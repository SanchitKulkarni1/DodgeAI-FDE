import React, { useEffect, useRef, useState, useMemo } from 'react';
import { apiClient } from '../api/client';
import ForceGraph2D from 'react-force-graph-2d';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import type { GraphNode, GraphEdge } from '../api/client';

interface GraphCanvasProps {
    highlightNodes: GraphNode[];
    highlightEdges: GraphEdge[];
    onNodeClick?: (node: any) => void;
}

export const GraphCanvas: React.FC<GraphCanvasProps> = ({ highlightNodes, highlightEdges, onNodeClick }) => {
    const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
    const [baseNodes, setBaseNodes] = useState<any[]>([]);
    const [baseEdges, setBaseEdges] = useState<any[]>([]);
    const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
    const containerRef = useRef<HTMLDivElement>(null);
    const [hoverNode, setHoverNode] = useState<any | null>(null);

    // Initial load of schema nodes & edges
    useEffect(() => {
        const fetchSchema = async () => {
            try {
                const [nodesData, edgesData] = await Promise.all([
                    apiClient.getGraphNodes(),
                    apiClient.getGraphEdges()
                ]);
                
                const bNodes = nodesData.entity_types.map(t => ({
                    id: t.type,
                    label: t.label,
                    color: t.color,
                    isBase: true,
                    val: 10,
                }));
                
                const bEdges = edgesData.edges.map(e => ({
                    source: e.source_type,
                    target: e.target_type,
                    label: e.label,
                    isBase: true
                }));
                
                setBaseNodes(bNodes);
                setBaseEdges(bEdges);
            } catch (err) {
                console.error("Failed to fetch graph schema:", err);
            }
        };
        fetchSchema();
    }, []);

    // Resize observer
    useEffect(() => {
        if (!containerRef.current) return;
        const observer = new ResizeObserver(entries => {
            if (entries[0]) {
                setDimensions({
                    width: entries[0].contentRect.width,
                    height: entries[0].contentRect.height
                });
            }
        });
        observer.observe(containerRef.current);
        return () => observer.disconnect();
    }, []);

    const graphData = useMemo(() => {
        // Create lookup for colors
        const colorLookup = baseNodes.reduce((acc, n) => ({ ...acc, [n.id]: n.color }), {});

        // Add instance nodes
        const instanceNodes = highlightNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || '#FFFFFF',
            val: 5,
            isBase: false,
        }));

        // Deduplicate nodes
        const allNodes = [...baseNodes, ...instanceNodes];
        const uniqueNodes = Array.from(new Map(allNodes.map(item => [item.id, item])).values());

        // Instance edges
        const instEdges = highlightEdges.map(e => ({
            ...e,
            isBase: false,
        }));

        const allEdges = [...baseEdges, ...instEdges];
        
        return { nodes: uniqueNodes, links: allEdges };
    }, [baseNodes, baseEdges, highlightNodes, highlightEdges]);

    return (
        <div ref={containerRef} className="w-full h-full relative bg-canvas">
            {dimensions.width > 0 && (
                <ForceGraph2D
                    ref={fgRef}
                    width={dimensions.width}
                    height={dimensions.height}
                    graphData={graphData}
                    nodeLabel="label"
                    nodeColor="color"
                    nodeRelSize={6}
                    linkColor={(link: any) => link.isBase ? 'rgba(255,255,255,0.1)' : 'rgba(255,255,255,0.4)'}
                    linkWidth={(link: any) => link.isBase ? 1 : 2}
                    linkDirectionalParticles={(link: any) => link.isBase ? 0 : 4}
                    linkDirectionalParticleSpeed={0.01}
                    onNodeHover={(node: any) => setHoverNode(node)}
                    onNodeClick={(node: any) => onNodeClick && onNodeClick(node)}
                    d3VelocityDecay={0.3}
                />
            )}
            
            {/* Sidebar Tooltip for Metadata if clicked, or just float for hover */}
            {hoverNode && (
                <div className="absolute top-4 left-4 bg-surface/90 border border-gray-700 p-4 rounded-lg shadow-xl shadow-black max-w-xs text-sm pointer-events-none backdrop-blur-sm transition-opacity z-10">
                    <h3 className="font-bold text-white mb-1" style={{ color: hoverNode.color }}>{hoverNode.label}</h3>
                    <div className="text-gray-400 text-xs uppercase tracking-wider mb-2">{hoverNode.isBase ? 'Entity Type' : hoverNode.type?.replace('_', ' ')}</div>
                    {!hoverNode.isBase && <p className="text-gray-300 font-mono text-xs break-all">ID: {hoverNode.id}</p>}
                </div>
            )}
        </div>
    );
};
