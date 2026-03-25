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
    backgroundNodes?: GraphNode[];
    backgroundEdges?: GraphEdge[];
    highlightNodes: GraphNode[];
    highlightEdges: GraphEdge[];
    expandedNodes?: GraphNode[];
    expandedEdges?: GraphEdge[];
    onNodeClick?: (node: any) => void;
    onResetClick?: () => void;
    isExpanding?: boolean;
}

export const GraphCanvas: React.FC<GraphCanvasProps> = ({ 
    backgroundNodes = [],
    backgroundEdges = [],
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
    const [tooltipPos, setTooltipPos] = useState({ x: 0, y: 0 });

    // Set of highlighted node IDs for fast lookup
    const highlightNodeIds = useMemo(() => {
        const ids = new Set<string>();
        highlightNodes.forEach(n => ids.add(n.id));
        expandedNodes.forEach(n => ids.add(n.id));
        return ids;
    }, [highlightNodes, expandedNodes]);



    // Semantic layout for base schema nodes - shows O2C flow
    const getSemanticPosition = (nodeType: string): { x: number; y: number } => {
        // Positions nodes to show the natural Order-to-Cash flow:
        // Left: Customer → Center: Sales Order/Delivery/Product → Right: Billing → Payment/Journal Entry
        const positions: Record<string, { x: number; y: number }> = {
            // O2C Flow: Start
            customer:          { x: -400, y: 0 },
            
            // O2C Flow: Order Phase
            sales_order:       { x: -150, y: 0 },
            
            // O2C Flow: Fulfillment Phase (parallel)
            delivery:          { x: 100, y: -100 },
            product:           { x: 100, y: 100 },
            plant:             { x: 100, y: 50 },
            
            // O2C Flow: Billing Phase
            billing_document:  { x: 300, y: 0 },
            
            // O2C Flow: Settlement Phase (parallel)
            journal_entry:     { x: 500, y: -80 },
            payment:           { x: 500, y: 80 },
            
            // Supporting entities
            address:           { x: -400, y: 250 },
        };
        
        return positions[nodeType] || { x: 0, y: 0 };
    };

    // Initial load of schema nodes & edges
    useEffect(() => {
        const fetchSchema = async () => {
            try {
                const [nodesData, edgesData] = await Promise.all([
                    apiClient.getGraphNodes(),
                    apiClient.getGraphEdges()
                ]);
                
                // Apply semantic positions to base nodes for meaningful layout
                const bNodes = nodesData.entity_types.map(t => {
                    const pos = getSemanticPosition(t.type);
                    return {
                        id: t.type,
                        label: t.label,
                        color: t.color || getEntityColor(t.type),
                        isBase: true,
                        val: 10,
                        // Pin nodes to their semantic positions (fx, fy disable physics forces)
                        fx: pos.x,
                        fy: pos.y,
                    };
                });
                
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

        // Background instance nodes (always dimmed)
        const backgroundInstanceNodes = backgroundNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || getEntityColor(n.type),
            val: 3,
            isBase: false,
            isExpanded: false,
            isHighlighted: false,
            isBackground: true,
        }));

        // Instance nodes from query highlights
        const instanceNodes = highlightNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || getEntityColor(n.type),
            val: 5,
            isBase: false,
            isExpanded: false,
            isHighlighted: true,
            isBackground: false,
        }));

        // Instance nodes from expansion
        const expandedInstanceNodes = expandedNodes.map(n => ({
            ...n,
            color: colorLookup[n.type] || getEntityColor(n.type),
            val: 5,
            isBase: false,
            isExpanded: true,
            isHighlighted: true,
            isBackground: false,
        }));

        // Combine nodes: base + background + highlights + expanded
        // Background always shown, highlights layered on top
        const allNodes = [
            ...baseNodes,
            ...backgroundInstanceNodes,
            ...(showOverlay ? [...instanceNodes, ...expandedInstanceNodes] : [])
        ];
        
        const uniqueNodes = Array.from(new Map(allNodes.map(item => [item.id, item])).values());
        
        // Build set of valid node IDs for edge validation
        const validNodeIds = new Set(uniqueNodes.map(n => String(n.id)));

        // Background edges (dimmed)
        const backgroundEdgeSet = new Set(backgroundEdges.map(e => `${e.source}-${e.target}`));
        const backgroundEdgeObjs = Array.from(backgroundEdgeSet).map(key => {
            const [source, target] = key.split('-');
            return {
                source,
                target,
                isHighlighted: false,
                isBackground: true,
            };
        });

        // Edge highlights from query
        const instEdges = highlightEdges.map(e => ({
            ...e,
            isBase: false,
            isExpanded: false,
            isHighlighted: true,
            isBackground: false,
        }));

        // Edge highlights from expansion
        const expandedInstEdges = expandedEdges.map(e => ({
            ...e,
            isBase: false,
            isExpanded: true,
            isHighlighted: true,
            isBackground: false,
        }));

        // Combine all edges: base + background + highlights + expanded
        // FILTER: only include edges where both source and target nodes exist
        const allEdges = [
            ...baseEdges,
            ...backgroundEdgeObjs,
            ...(showOverlay ? [...instEdges, ...expandedInstEdges] : [])
        ].filter(edge => validNodeIds.has(String(edge.source)) && validNodeIds.has(String(edge.target)));
        
        return { nodes: uniqueNodes, links: allEdges };
    }, [baseNodes, baseEdges, backgroundNodes, backgroundEdges, highlightNodes, highlightEdges, expandedNodes, expandedEdges, showOverlay]);

    // Custom node canvas painter
    const nodeCanvasObject = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
        const label = node.label || node.id;
        const isHL = node.isHighlighted || highlightNodeIds.has(node.id);
        const radius = isHL ? 10 : node.isBase ? 5 : 7;
        const alpha = node.isBase && !isHL ? 0.35 : 1.0;

        ctx.save();
        ctx.globalAlpha = alpha;

        // Glow for highlighted nodes
        if (isHL && !node.isBackground) {
            ctx.shadowColor = node.color || '#60a5fa';
            ctx.shadowBlur = 20;  // Stronger glow for highlights
        }

        ctx.beginPath();
        ctx.arc(node.x!, node.y!, radius, 0, 2 * Math.PI);
        // Background nodes: darker (50% opacity), highlighted nodes: bright, others: medium
        if (node.isBackground) {
            ctx.fillStyle = `${node.color || '#60a5fa'}80`;  // 50% opacity for background
        } else {
            ctx.fillStyle = node.color || '#60a5fa';
        }
        ctx.fill();

        // Reset shadow
        ctx.shadowBlur = 0;

        // Label rendering: show labels for all instance nodes, base nodes only when zoomed, background hidden
        const shouldShowLabel = !node.isBase && !node.isBackground || globalScale > 1.2;
        if (shouldShowLabel && !node.isBackground) {
            const fontSize = Math.max(10 / globalScale, 6);
            ctx.font = `${fontSize}px "DM Mono", monospace`;
            // Labels: dark text for highlights, dimmer for others, hidden for background
            ctx.fillStyle = isHL ? 'rgba(0,0,0,0.95)' : node.isBase ? 'rgba(100,100,100,0.4)' : 'rgba(60,66,72,0.75)';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, node.x!, node.y! + radius + 10 / globalScale);
        }

        ctx.restore();
    }, [highlightNodeIds]);

    const handleCanvasNodeClick = useCallback((node: any) => {
        if (node.isBase) return;
        console.log('Node clicked:', node.id, 'isExpanding:', isExpanding);
        setSelectedNode(node);
        if (!isExpanding && onNodeClick) {
            console.log('Triggering expansion for:', node.id);
            onNodeClick(node);
        } else if (isExpanding) {
            console.log('Expansion already in progress, blocking click');
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
        <div ref={containerRef} className="w-full h-full relative" style={{ background: '#ffffff' }}>
            {dimensions.width > 0 && (
                <ForceGraph2D
                    ref={fgRef}
                    width={dimensions.width}
                    height={dimensions.height}
                    graphData={graphData}
                    backgroundColor="#ffffff"
                    nodeCanvasObject={nodeCanvasObject}
                    nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
                        const radius = node.isHighlighted ? 10 : node.isBase ? 5 : 7;
                        ctx.beginPath();
                        ctx.arc(node.x!, node.y!, radius + 2, 0, 2 * Math.PI);
                        ctx.fillStyle = color;
                        ctx.fill();
                    }}
                    linkColor={(link: any) => {
                        if (link.isHighlighted) return 'rgba(59,130,246,1)';  // Bold bright blue for highlights
                        if (link.isBackground) return 'rgba(150,150,150,0.3)';  // Darker for background (0.3 opacity)
                        return 'rgba(200,200,200,0.15)';  // Regular faint for base/other
                    }}
                    linkWidth={(link: any) => {
                        if (link.isHighlighted) return 3.5;  // Bolder highlights
                        if (link.isBackground) return 0.8;  // More visible background edges
                        return 0.8;
                    }}
                    linkDirectionalParticles={(link: any) => link.isHighlighted ? 6 : 0}  // More particles for highlights
                    linkDirectionalParticleWidth={2}
                    linkDirectionalParticleSpeed={0.006}
                    onNodeHover={(node: any) => {
                        setHoverNode(node);
                        if (node && containerRef.current) {
                            const rect = containerRef.current.getBoundingClientRect();
                            const nodeScreenX = node.x ? (node.x + dimensions.width / 2) : dimensions.width / 2;
                            const nodeScreenY = node.y ? (node.y + dimensions.height / 2) : dimensions.height / 2;
                            const x = nodeScreenX < 190 ? nodeScreenX + 150 : nodeScreenX - 350;
                            const y = nodeScreenY < 100 ? nodeScreenY + 80 : nodeScreenY - 100;
                            setTooltipPos({ x: Math.max(0, Math.min(x, dimensions.width - 280)), y: Math.max(0, Math.min(y, dimensions.height - 150)) });
                        }
                    }}
                    onNodeClick={handleCanvasNodeClick}
                    onBackgroundClick={() => setSelectedNode(null)}
                    d3AlphaDecay={0.025}
                    d3VelocityDecay={0.45}
                    warmupTicks={100}
                    cooldownTicks={200}
                    nodeRelSize={5}
                    enableNodeDrag={true}
                    enableZoomInteraction={true}
                    d3Force="charge" d3ForceStrength={-120}
                    linkDistance={200}
                    distanceMax={500}
                />
            )}
            
            {/* Floating Hover Tooltip */}
            {hoverNode && !selectedNode && (
                <div 
                    className="absolute bg-white border border-gray-200 rounded-lg p-3 text-xs pointer-events-none z-20 max-w-xs shadow-lg"
                    style={{
                        left: `${tooltipPos.x}px`,
                        top: `${tooltipPos.y}px`,
                    }}
                >
                    <div className="flex items-center gap-2 mb-1.5">
                        <span 
                            className="px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider"
                            style={{ backgroundColor: (hoverNode.color || '#94a3b8') + '20', color: hoverNode.color || '#94a3b8' }}
                        >
                            {hoverNode.isBase ? 'Entity Type' : hoverNode.type?.replace('_', ' ') || 'Node'}
                        </span>
                    </div>
                    <h3 className="font-bold text-gray-900 text-sm mb-1">{hoverNode.label || hoverNode.id}</h3>
                    {!hoverNode.isBase && (
                        <>
                            <p className="text-gray-600 font-mono text-[10px] break-all">ID: {hoverNode.id}</p>
                            <p className="text-gray-500 text-[10px] mt-1.5 italic">Click to expand</p>
                        </>
                    )}
                </div>
            )}

            {/* Floating Metadata Card on Click */}
            {selectedNode && (
                <div className="absolute top-6 right-6 w-[340px] max-h-[85vh] bg-white border border-gray-200 rounded-lg p-4 text-xs z-30 shadow-lg overflow-y-auto">
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
                            className="text-gray-400 hover:text-gray-600 transition-colors flex-shrink-0"
                        >
                            <X size={16} />
                        </button>
                    </div>

                    {/* Title */}
                    <h3 className="font-bold text-gray-900 text-sm mb-4 break-all">{selectedNode.label || selectedNode.id}</h3>

                    {/* Metadata fields */}
                    {selectedNode.isBase ? (
                        <div className="text-gray-600 italic text-center py-4">
                            <p>Base schema node</p>
                            <p className="text-[10px] text-gray-500 mt-1">Click instance nodes for details</p>
                        </div>
                    ) : (
                        <>
                            <div className="space-y-2 font-mono text-[11px] mb-4">
                                {getNodeMetadata(selectedNode).map(([key, val], idx) => (
                                    <div key={idx} className="flex justify-between gap-3 pb-2 border-b border-gray-100 last:border-0">
                                        <span className="text-gray-500 flex-shrink-0 capitalize">{key}</span>
                                        <span className="text-gray-700 text-right break-all flex-1 font-semibold">{formatFieldValue(val)}</span>
                                    </div>
                                ))}
                            </div>

                            {/* Additional info footer */}
                            <div className="pt-3 border-t border-gray-100">
                                <p className="text-gray-500 italic text-[10px]">
                                    🔗 Connections: {getConnectionCount(selectedNode.id)}
                                </p>
                                {selectedNode.isExpanded && (
                                    <p className="text-gray-500 italic text-[10px] mt-1">
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
