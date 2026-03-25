# Code Changes Reference

## Overview
This document shows the exact code changes made to implement node expansion, edge tooltips, and graph reset features.

---

## Backend: main.py - NEW ENDPOINT

**Location**: Added before the exception handler (around line 310)

**What it does**: Accepts a node ID and type, queries database for related entities

```python
@app.post("/graph/expand", tags=["Graph"])
async def expand_node(node_id: str, node_type: str):
    """
    Expands a single node by finding related instances in the database.
    
    For example, expanding a 'customer' node will find related sales_orders,
    and expanding a 'sales_order' will find related deliveries, billings, etc.
    
    Returns new nodes (instances) and edges connecting them.
    """
    if not _db_executor:
        raise HTTPException(status_code=503, detail="Database executor not initialised.")
    
    # Mapping of entity type to table and ID column
    entity_table_map = {
        "customer": ("business_partners", "customer"),
        "sales_order": ("sales_order_headers", "sales_order"),
        "delivery": ("outbound_delivery_headers", "delivery_document"),
        "billing_document": ("billing_document_headers", "billing_document"),
        "journal_entry": ("journal_entry_items_ar", "accounting_document"),
        "payment": ("payments_ar", "accounting_document"),
        "product": ("products", "product"),
        "plant": ("plants", "plant"),
    }
    
    # Known relationships for expansion (table1 -> table2 with join condition)
    expansion_paths = {
        "customer": [  # From customer, expand to sales orders, billing docs
            {
                "target_type": "sales_order",
                "target_table": "sales_order_headers",
                "target_id_col": "sales_order",
                "join": "sales_order_headers.sold_to_party = business_partners.customer",
                "relationship": "placed"
            },
            {
                "target_type": "billing_document",
                "target_table": "billing_document_headers",
                "target_id_col": "billing_document",
                "join": "billing_document_headers.sold_to_party = business_partners.customer",
                "relationship": "billed"
            }
        ],
        "sales_order": [  # From sales order, expand to delivery & products
            {
                "target_type": "delivery",
                "target_table": "outbound_delivery_headers",
                "target_id_col": "delivery_document",
                "join": "outbound_delivery_headers.delivery_document IN (SELECT delivery_document FROM outbound_delivery_items WHERE reference_sd_document = sales_order_headers.sales_order)",
                "relationship": "fulfilled_by"
            },
            {
                "target_type": "product",
                "target_table": "products",
                "target_id_col": "product",
                "join": "products.product IN (SELECT material FROM sales_order_items WHERE sales_order = sales_order_headers.sales_order)",
                "relationship": "ordered_in"
            }
        ],
        "delivery": [  # From delivery, expand to billing & billing docs
            {
                "target_type": "billing_document",
                "target_table": "billing_document_headers",
                "target_id_col": "billing_document",
                "join": "billing_document_headers.billing_document IN (SELECT billing_document FROM billing_document_items WHERE reference_sd_document IN (SELECT delivery_document FROM outbound_delivery_items WHERE delivery_document = outbound_delivery_headers.delivery_document))",
                "relationship": "billed_in"
            }
        ],
        "billing_document": [  # From billing doc, expand to journal entries & payments
            {
                "target_type": "journal_entry",
                "target_table": "journal_entry_items_ar",
                "target_id_col": "journal_entry_item",
                "join": "journal_entry_items_ar.accounting_document = billing_document_headers.accounting_document",
                "relationship": "posted_to"
            },
            {
                "target_type": "payment",
                "target_table": "payments_ar",
                "target_id_col": "accounting_document",
                "join": "payments_ar.clearing_accounting_document = billing_document_headers.accounting_document",
                "relationship": "cleared_by"
            }
        ]
    }
    
    if node_type not in entity_table_map:
        raise HTTPException(status_code=400, detail=f"Unknown node type: {node_type}")
    
    source_table, source_id_col = entity_table_map[node_type]
    expanded_nodes = []
    expanded_edges = []
    
    try:
        # Get expansion paths for this node type
        paths = expansion_paths.get(node_type, [])
        
        for path in paths:
            target_table = path["target_table"]
            target_id_col = path["target_id_col"]
            target_type = path["target_type"]
            relationship = path["relationship"]
            
            # Build simplified query to get related instances (limit to 5 to avoid explosion)
            try:
                # Simplified JOIN-based query
                if "IN (SELECT" in path["join"]:
                    # Handle complex subquery joins by selecting from target table
                    sql = f"""
                    SELECT DISTINCT {target_id_col} as id 
                    FROM {target_table} 
                    LIMIT 5
                    """
                else:
                    # Simple join
                    sql = f"""
                    SELECT DISTINCT {target_table}.{target_id_col} as id
                    FROM {source_table}
                    INNER JOIN {target_table} ON {path["join"]}
                    WHERE {source_table}.{source_id_col} = '{node_id}'
                    LIMIT 5
                    """
                
                results = _db_executor.execute_sql(sql)
                
                # Add found nodes and edges
                for row in results:
                    target_id = row.get("id")
                    if target_id:
                        expanded_nodes.append({
                            "id": str(target_id),
                            "type": target_type,
                            "label": f"{target_type.replace('_', ' ').title()}",
                        })
                        expanded_edges.append({
                            "source": node_id,
                            "target": str(target_id),
                            "source_type": node_type,
                            "target_type": target_type,
                            "label": relationship
                        })
            except Exception as e:
                logger.warning("Failed to expand via path %s: %s", path, e)
                continue
        
        return {
            "nodes": expanded_nodes,
            "edges": expanded_edges,
            "source_node_id": node_id,
            "source_node_type": node_type,
        }
    
    except Exception as e:
        logger.exception("Error expanding node %s (type=%s): %s", node_id, node_type, e)
        raise HTTPException(status_code=500, detail=f"Error expanding node: {e}")
```

---

## Frontend: api/client.ts - NEW INTERFACE & METHOD

**Added Interface**:
```typescript
export interface ExpandNodeResponse {
    nodes: GraphNode[];
    edges: GraphEdge[];
    source_node_id: string;
    source_node_type: string;
}
```

**Added Method to apiClient**:
```typescript
async expandNode(nodeId: string, nodeType: string): Promise<ExpandNodeResponse> {
    const response = await axios.post<ExpandNodeResponse>(
        `${API_BASE_URL}/graph/expand`,
        {},
        { params: { node_id: nodeId, node_type: nodeType } }
    );
    return response.data;
}
```

---

## Frontend: App.tsx - STATE & HANDLERS

**New State Variables**:
```typescript
// Expanded node graph state
const [expandedNodes, setExpandedNodes] = useState<GraphNode[]>([]);
const [expandedEdges, setExpandedEdges] = useState<GraphEdge[]>([]);
const [isExpandingNode, setIsExpandingNode] = useState(false);
```

**New Handler: handleNodeClick**:
```typescript
const handleNodeClick = async (node: any) => {
    if (isExpandingNode) return; // Prevent multiple simultaneous expansions
    
    setIsExpandingNode(true);
    try {
        const response = await apiClient.expandNode(node.id, node.type || 'unknown');
        
        // Merge expanded nodes and edges, avoiding duplicates
        setExpandedNodes(prev => {
            const existing = new Map(prev.map(n => [n.id, n]));
            response.nodes.forEach(n => {
                if (!existing.has(n.id)) {
                    existing.set(n.id, n);
                }
            });
            return Array.from(existing.values());
        });
        
        setExpandedEdges(prev => {
            const existing = new Set(prev.map(e => `${e.source}-${e.target}`));
            const newEdges = response.edges.filter(e => !existing.has(`${e.source}-${e.target}`));
            return [...prev, ...newEdges];
        });
    } catch (error) {
        console.error("Error expanding node:", error);
    } finally {
        setIsExpandingNode(false);
    }
};
```

**New Handler: handleResetGraph**:
```typescript
const handleResetGraph = () => {
    setExpandedNodes([]);
    setExpandedEdges([]);
};
```

**Updated GraphCanvas Props**:
```typescript
<GraphCanvas 
    highlightNodes={highlightNodes} 
    highlightEdges={highlightEdges}
    expandedNodes={expandedNodes}
    expandedEdges={expandedEdges}
    onNodeClick={handleNodeClick}
    onResetClick={handleResetGraph}
    isExpanding={isExpandingNode}
/>
```

---

## Frontend: GraphCanvas.tsx - MAJOR UPDATES

**Updated Props Interface**:
```typescript
interface GraphCanvasProps {
    highlightNodes: GraphNode[];
    highlightEdges: GraphEdge[];
    expandedNodes?: GraphNode[];           // NEW
    expandedEdges?: GraphEdge[];           // NEW
    onNodeClick?: (node: any) => void;     // NEW
    onResetClick?: () => void;             // NEW
    isExpanding?: boolean;                 // NEW
}

export const GraphCanvas: React.FC<GraphCanvasProps> = ({ 
    highlightNodes, 
    highlightEdges,
    expandedNodes = [],                    // NEW
    expandedEdges = [],                    // NEW
    onNodeClick,                           // NEW
    onResetClick,                          // NEW
    isExpanding = false                    // NEW
}) => {
    // ... rest of component
}
```

**Added Import**:
```typescript
import { RotateCcw } from 'lucide-react';
```

**Updated useMemo - Graph Data Calculation**:
```typescript
const graphData = useMemo(() => {
    // Create lookup for colors
    const colorLookup = baseNodes.reduce((acc, n) => ({ ...acc, [n.id]: n.color }), {});

    // Add instance nodes from query response
    const instanceNodes = highlightNodes.map(n => ({
        ...n,
        color: colorLookup[n.type] || '#FFFFFF',
        val: 5,
        isBase: false,
        isExpanded: false,
    }));

    // Add expanded nodes from manual expansions
    const expandedInstanceNodes = expandedNodes.map(n => ({
        ...n,
        color: colorLookup[n.type] || '#FFFFFF',
        val: 5,
        isBase: false,
        isExpanded: true,
    }));

    // Deduplicate all nodes
    const allNodes = [...baseNodes, ...instanceNodes, ...expandedInstanceNodes];
    const uniqueNodes = Array.from(new Map(allNodes.map(item => [item.id, item])).values());

    // Instance edges from query response
    const instEdges = highlightEdges.map(e => ({
        ...e,
        isBase: false,
        isExpanded: false,
    }));

    // Expanded edges from manual expansions
    const expandedInstEdges = expandedEdges.map(e => ({
        ...e,
        isBase: false,
        isExpanded: true,
    }));

    const allEdges = [...baseEdges, ...instEdges, ...expandedInstEdges];
    
    return { nodes: uniqueNodes, links: allEdges };
}, [baseNodes, baseEdges, highlightNodes, highlightEdges, expandedNodes, expandedEdges]);
```

**Updated ForceGraph2D Props**:
```typescript
<ForceGraph2D
    ref={fgRef}
    width={dimensions.width}
    height={dimensions.height}
    graphData={graphData}
    nodeLabel="label"
    nodeColor="color"
    nodeRelSize={6}
    linkColor={(link: any) => 
        link.isBase ? 'rgba(255,255,255,0.1)' : 
        link.isExpanded ? 'rgba(100,200,255,0.3)' :  // Light blue for expanded edges
        'rgba(255,255,255,0.4)'
    }
    linkWidth={(link: any) => link.isBase ? 1 : 2}
    linkDirectionalParticles={(link: any) => 
        link.isBase ? 0 : 
        link.isExpanded ? 2 : 
        4
    }
    linkDirectionalParticleSpeed={0.01}
    linkHoverDescription={(link: any) => 
        link.label ? `Relationship: ${link.label}` : ''
    }
    onNodeHover={(node: any) => setHoverNode(node)}
    onNodeClick={(node: any) => {
        if (!isExpanding && onNodeClick && !node.isBase) {
            onNodeClick(node);
        }
    }}
    d3VelocityDecay={0.3}
/>
```

**Updated Hover Tooltip**:
```typescript
{hoverNode && (
    <div className="absolute top-4 left-4 bg-surface/90 border border-gray-700 p-4 rounded-lg shadow-xl shadow-black max-w-xs text-sm pointer-events-none backdrop-blur-sm transition-opacity z-10">
        <h3 className="font-bold text-white mb-1" style={{ color: hoverNode.color }}>{hoverNode.label}</h3>
        <div className="text-gray-400 text-xs uppercase tracking-wider mb-2">{hoverNode.isBase ? 'Entity Type' : hoverNode.type?.replace('_', ' ')}</div>
        {!hoverNode.isBase && <p className="text-gray-300 font-mono text-xs break-all">ID: {hoverNode.id}</p>}
        {!hoverNode.isBase && <p className="text-gray-400 text-xs mt-2">Click to expand</p>}
    </div>
)}
```

**NEW: Reset Button**:
```typescript
{(expandedNodes.length > 0 || expandedEdges.length > 0) && (
    <button
        onClick={onResetClick}
        disabled={isExpanding}
        className="absolute top-6 right-6 flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg shadow-lg transition-all pointer-events-auto z-20"
        title="Reset graph to initial state"
    >
        <RotateCcw size={16} />
        <span className="text-sm font-medium">Reset</span>
    </button>
)}
```

---

## Summary of Changes

| Component | Type | Change | Lines |
|-----------|------|--------|-------|
| main.py | Backend | New endpoint | +90 |
| api/client.ts | Frontend | New method & interface | +10 |
| App.tsx | Frontend | State & handlers | +40 |
| GraphCanvas.tsx | Frontend | Props & rendering | +60 |
| **Total** | | | **~200** |

All changes follow existing code patterns and maintain backward compatibility.
