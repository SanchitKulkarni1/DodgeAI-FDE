import React, { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { apiClient } from '../api/client';
import ForceGraph2D from 'react-force-graph-2d';
import type { ForceGraphMethods } from 'react-force-graph-2d';
import type { GraphNode, GraphEdge } from '../api/client';
import { RotateCcw, Eye, EyeOff, X } from 'lucide-react';

// Consistent entity color map
export const ENTITY_COLORS: Record<string, string> = {
  Order:        '#f97316',
  SalesOrder:   '#f97316',
  Delivery:     '#22c55e',
  Invoice:      '#3b82f6',
  Billing:      '#3b82f6',
  Payment:      '#a855f7',
  Customer:     '#06b6d4',
  Product:      '#eab308',
  Material:     '#eab308',
  Address:      '#6b7280',
  JournalEntry: '#ec4899',
  // Metric node types for aggregations
  count:        '#f59e0b',  // Amber for counts
  revenue:      '#10b981',  // Emerald for revenue
  amount:       '#06b6d4',  // Cyan for amounts
  metric:       '#64748b',  // Gray for other metrics
  Default:      '#94a3b8',
};

function getEntityColor(type: string): string {
  return ENTITY_COLORS[type] || ENTITY_COLORS.Default;
}

interface GraphCanvasProps {
    highlightNodes: GraphNode[];
    highlightEdges: GraphEdge[];
    expandedNodes?: GraphNode[];
    expandedEdges?: GraphEdge[];
    onNodeClick?: (node: any) => void;
    onResetClick?: () => void;
    isExpanding?: boolean;
}

export const GraphCanvas: React.FC<GraphCanvasProps> = ({ 
    highlightNodes, 
    highlightEdges,
    expandedNodes = [],
    expandedEdges = [],
    onNodeClick,
    onResetClick,
    isExpanding = false
}) => {
    const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
    const [baseNodes, setBaseNodes] = useState<any[]>([]);
    const [baseEdges, setBaseEdges] = useState<any[]>([]);
    const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
    const containerRef = useRef<HTMLDivElement>(null);
    const [hoverNode, setHoverNode] = useState<any | null>(null);
    const [selectedNode, setSelectedNode] = useState<any | null>(null);
    const [showOverlay, setShowOverlay] = useState(true);

    // Set of highlighted node IDs for fast lookup
    const highlightNodeIds = useMemo(() => {
        const ids = new Set<string>();
        highlightNodes.forEach(n => ids.add(n.id));
        expandedNodes.forEach(n => ids.add(n.id));
        return ids;
    }, [highlightNodes, expandedNodes]);



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
                    color: t.color || getEntityColor(t.type),
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
        const colorLookup: Record<string, string> = baseNodes.reduce((acc: Record<string, string>, n: any) => ({ ...acc, [n.id]: n.color }), {});

        // Instance nodes from query highlights
        const instanceNodes = highlightNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || getEntityColor(n.type),
            val: 5,
            isBase: false,
            isExpanded: false,
            isHighlighted: true,
        }));

        // Instance nodes from expansion
        const expandedInstanceNodes = expandedNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || getEntityColor(n.type),
            val: 5,
            isBase: false,
            isExpanded: true,
            isHighlighted: true,
        }));

        // Combine nodes based on showOverlay state:
        // - Always show base schema nodes
        // - Only show instance nodes if showOverlay is true
        const allNodes = showOverlay 
            ? [...baseNodes, ...instanceNodes, ...expandedInstanceNodes]
            : [...baseNodes];
        
        const uniqueNodes = Array.from(new Map(allNodes.map(item => [item.id, item])).values());

        // Edge highlights from query
        const instEdges = highlightEdges.map(e => ({
            ...e,
            isBase: false,
            isExpanded: false,
            isHighlighted: true,
        }));

        // Edge highlights from expansion
        const expandedInstEdges = expandedEdges.map(e => ({
            ...e,
            isBase: false,
            isExpanded: true,
            isHighlighted: true,
        }));

        // Only include instance edges if overlay is shown
        const allEdges = showOverlay
            ? [...baseEdges, ...instEdges, ...expandedInstEdges]
            : [...baseEdges];
        
        return { nodes: uniqueNodes, links: allEdges };
    }, [baseNodes, baseEdges, highlightNodes, highlightEdges, expandedNodes, expandedEdges, showOverlay]);

    // Custom node canvas painter
    const nodeCanvasObject = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
        const label = node.label || node.id;
        const isHL = node.isHighlighted || highlightNodeIds.has(node.id);
        const radius = isHL ? 10 : node.isBase ? 5 : 7;
        const alpha = node.isBase && !isHL ? 0.35 : 1.0;

        ctx.save();
        ctx.globalAlpha = alpha;

        // Glow for highlighted nodes
        if (isHL) {
            ctx.shadowColor = node.color || '#60a5fa';
            ctx.shadowBlur = 15;
        }

        ctx.beginPath();
        ctx.arc(node.x!, node.y!, radius, 0, 2 * Math.PI);
        ctx.fillStyle = node.color || '#60a5fa';
        ctx.fill();

        // Reset shadow
        ctx.shadowBlur = 0;

        // Label rendering
        if (globalScale > 1.2 || isHL) {
            const fontSize = Math.max(10 / globalScale, 6);
            ctx.font = `${fontSize}px "DM Mono", monospace`;
            ctx.fillStyle = 'rgba(255,255,255,0.85)';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, node.x!, node.y! + radius + 10 / globalScale);
        }

        ctx.restore();
    }, [highlightNodeIds]);

    const handleCanvasNodeClick = useCallback((node: any) => {
        if (node.isBase) return;
        setSelectedNode(node);
        if (!isExpanding && onNodeClick) {
            onNodeClick(node);
        }
    }, [isExpanding, onNodeClick]);

    // Count connections for a node
    const getConnectionCount = useCallback((nodeId: string) => {
        return graphData.links.filter((l: any) => {
            const src = typeof l.source === 'object' ? l.source.id : l.source;
            const tgt = typeof l.target === 'object' ? l.target.id : l.target;
            return src === nodeId || tgt === nodeId;
        }).length;
    }, [graphData.links]);

    // Helper to format metadata field values
    const formatFieldValue = (value: any): string => {
        if (value === null || value === undefined) return '—';
        if (typeof value === 'boolean') return value ? 'Yes' : 'No';
        if (typeof value === 'number') {
            if (Number.isInteger(value)) return value.toString();
            return value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
        }
        return String(value);
    };

    // Helper to get displayable metadata from node
    const getNodeMetadata = (node: any) => {
        const fields: Array<[string, any]> = [];
        
        // Always show ID and Type
        if (node.id) fields.push(['ID', node.id]);
        if (node.type && !node.isBase) fields.push(['Type', node.type.replace('_', ' ').toUpperCase()]);
        
        // Entity-specific common fields from O2C schema
        const commonMetaFields = [
            'customer', 'sales_order', 'delivery_document', 'billing_document', 
            'accounting_document', 'material', 'product', 'plant',
            'sold_to_party', 'company_code', 'fiscal_year', 'gl_account',
            'reference_document', 'cost_center', 'profit_center', 'transaction_currency',
            'amount_in_transaction_currency', 'net_amount_in_doc_currency',
            'posting_date', 'document_date', 'accounting_document_type',
            'accounting_document_item', 'business_partner_full_name',
            'net_amount', 'total_amount', 'quantity', 'delivery_status'
        ];
        
        // Add any other custom properties from the node
        Object.entries(node).forEach(([key, value]) => {
            if (![' id', 'type', 'label', 'color', 'val', 'isBase', 'isExpanded', 'isHighlighted', 'x', 'y', 'vx', 'vy', 'fx', 'fy', '__threeObj', 'group'].includes(key) && value !== undefined && value !== null) {
                fields.push([key, value]);
            }
        });
        
        return fields.slice(0, 12); // Limit to 12 fields shown
    };

    return (
        <div ref={containerRef} className="w-full h-full relative" style={{ background: '#0d0f14' }}>
            {dimensions.width > 0 && (
                <ForceGraph2D
                    ref={fgRef}
                    width={dimensions.width}
                    height={dimensions.height}
                    graphData={graphData}
                    backgroundColor="#0d0f14"
                    nodeCanvasObject={nodeCanvasObject}
                    nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
                        const radius = node.isHighlighted ? 10 : node.isBase ? 5 : 7;
                        ctx.beginPath();
                        ctx.arc(node.x!, node.y!, radius + 2, 0, 2 * Math.PI);
                        ctx.fillStyle = color;
                        ctx.fill();
                    }}
                    linkColor={(link: any) => link.isHighlighted ? 'rgba(96,165,250,0.7)' : 'rgba(147,197,253,0.08)'}
                    linkWidth={(link: any) => link.isHighlighted ? 2 : 0.5}
                    linkDirectionalParticles={(link: any) => link.isHighlighted ? 4 : 0}
                    linkDirectionalParticleWidth={2}
                    linkDirectionalParticleSpeed={0.006}
                    onNodeHover={(node: any) => setHoverNode(node)}
                    onNodeClick={handleCanvasNodeClick}
                    onBackgroundClick={() => setSelectedNode(null)}
                    d3AlphaDecay={0.02}
                    d3VelocityDecay={0.3}
                    warmupTicks={100}
                    cooldownTicks={200}
                    nodeRelSize={5}
                    enableNodeDrag={true}
                    enableZoomInteraction={true}
                />
            )}
            
            {/* Floating Hover Tooltip */}
            {hoverNode && !selectedNode && (
                <div className="absolute top-4 left-4 bg-black/60 backdrop-blur-md border border-white/10 rounded-xl p-3 text-xs pointer-events-none z-10 max-w-xs">
                    <div className="flex items-center gap-2 mb-1.5">
                        <span 
                            className="px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider"
                            style={{ backgroundColor: (hoverNode.color || '#94a3b8') + '25', color: hoverNode.color || '#94a3b8' }}
                        >
                            {hoverNode.isBase ? 'Entity Type' : hoverNode.type?.replace('_', ' ') || 'Node'}
                        </span>
                    </div>
                    <h3 className="font-bold text-white text-sm mb-1">{hoverNode.label || hoverNode.id}</h3>
                    {!hoverNode.isBase && (
                        <>
                            <p className="text-white/50 font-mono text-[10px] break-all">ID: {hoverNode.id}</p>
                            <p className="text-white/30 text-[10px] mt-1.5 italic">Click to expand</p>
                        </>
                    )}
                </div>
            )}

            {/* Floating Metadata Card on Click */}
            {selectedNode && (
                <div className="absolute top-6 right-6 w-[340px] max-h-[85vh] bg-black/80 backdrop-blur-xl border border-white/15 rounded-xl p-4 text-xs z-30 shadow-2xl shadow-black/50 overflow-y-auto">
                    {/* Header with badge and close */}
                    <div className="flex items-center justify-between mb-4">
                        <span 
                            className="px-2.5 py-1 rounded-full text-[10px] font-semibold uppercase tracking-wider"
                            style={{ backgroundColor: (selectedNode.color || '#94a3b8') + '25', color: selectedNode.color || '#94a3b8' }}
                        >
                            {selectedNode.type?.replace('_', ' ') || (selectedNode.isBase ? 'Entity Type' : 'Entity')}
                        </span>
                        <button 
                            onClick={() => setSelectedNode(null)}
                            className="text-white/30 hover:text-white/70 transition-colors flex-shrink-0"
                        >
                            <X size={16} />
                        </button>
                    </div>

                    {/* Title */}
                    <h3 className="font-bold text-white text-sm mb-4 break-all">{selectedNode.label || selectedNode.id}</h3>

                    {/* Metadata fields */}
                    {selectedNode.isBase ? (
                        <div className="text-white/60 italic text-center py-4">
                            <p>Base schema node</p>
                            <p className="text-[10px] text-white/40 mt-1">Click instance nodes for details</p>
                        </div>
                    ) : (
                        <>
                            <div className="space-y-2 font-mono text-[11px] mb-4">
                                {getNodeMetadata(selectedNode).map(([key, val], idx) => (
                                    <div key={idx} className="flex justify-between gap-3 pb-2 border-b border-white/5 last:border-0">
                                        <span className="text-white/40 flex-shrink-0 capitalize">{key}</span>
                                        <span className="text-white/70 text-right break-all flex-1">{formatFieldValue(val)}</span>
                                    </div>
                                ))}
                            </div>

                            {/* Additional info footer */}
                            <div className="pt-3 border-t border-white/10">
                                <p className="text-white/30 italic text-[10px]">
                                    🔗 Connections: {getConnectionCount(selectedNode.id)}
                                </p>
                                {selectedNode.isExpanded && (
                                    <p className="text-white/30 italic text-[10px] mt-1">
                                        From expansion
                                    </p>
                                )}
                            </div>
                        </>
                    )}
                </div>
            )}

            {/* Overlay toggle button */}
            {showOverlay ? (
                <button 
                    onClick={() => setShowOverlay(false)}
                    className="absolute top-14 left-5 z-20 flex items-center gap-2 bg-white/10 hover:bg-white/20 border border-white/10 text-white text-xs px-3 py-2 rounded-lg backdrop-blur-sm transition-all"
                >
                    <Eye size={14} />
                    Hide Granular Overlay
                </button>
            ) : (
                <button 
                    onClick={() => setShowOverlay(true)}
                    className="absolute top-14 left-5 z-20 flex items-center gap-2 bg-white/10 hover:bg-white/20 border border-white/10 text-white text-xs px-3 py-2 rounded-lg backdrop-blur-sm transition-all"
                >
                    <EyeOff size={14} />
                    Show Granular Overlay
                </button>
            )}

            {/* Reset button overlay */}
            {(expandedNodes.length > 0 || expandedEdges.length > 0) && (
                <button
                    onClick={onResetClick}
                    disabled={isExpanding}
                    className="absolute top-6 right-6 flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg shadow-lg transition-all z-20"
                    title="Reset graph to initial state"
                >
                    <RotateCcw size={16} />
                    <span className="text-sm font-medium">Reset</span>
                </button>
            )}
        </div>
    );
};
