# Quick Reference: Node Expansion Features

## 🎯 One-Minute Overview

**What's New?**
1. **Node Expansion**: Click any result node to see related entities
2. **Edge Tooltips**: Hover edges to see relationship types  
3. **Reset Button**: Clear all expansions with one click

**Where's the Code?**
- Backend: `backend/main.py` (lines ~310-400) - `/graph/expand` endpoint
- Frontend State: `frontend/src/App.tsx` - `handleNodeClick()` function
- Frontend UI: `frontend/src/components/GraphCanvas.tsx` - new props
- API Client: `frontend/src/api/client.ts` - `expandNode()` method

---

## 🔧 Developer Quick Start

### Test the Backend Endpoint
```bash
# Terminal 1: Start backend
cd backend && source venv/bin/activate
uvicorn main:app --reload --port 8000

# Terminal 2: Test endpoint
curl -X POST "http://127.0.0.1:8000/graph/expand?node_id=C001&node_type=customer" \
  -H "Content-Type: application/json" -d "{}"
```

### Test the Frontend
```bash
# Terminal 3: Start frontend
cd frontend && npm run dev
# Open http://localhost:5173
# Ask a query, then click a result node to expand
```

---

## 📋 Data Structures

### ExpandNodeResponse (Backend Returns This)
```typescript
{
  nodes: [
    { id: "SO123", type: "sales_order", label: "Sales Order" }
  ],
  edges: [
    {
      source: "CUST001",
      target: "SO123",
      source_type: "customer",
      target_type: "sales_order",
      label: "placed"
    }
  ],
  source_node_id: "CUST001",
  source_node_type: "customer"
}
```

### GraphCanvas Props Signature
```typescript
interface GraphCanvasProps {
    highlightNodes: GraphNode[];          // From query
    highlightEdges: GraphEdge[];          // From query
    expandedNodes?: GraphNode[];          // From expansions
    expandedEdges?: GraphEdge[];          // From expansions
    onNodeClick?: (node: any) => void;    // Handle clicks
    onResetClick?: () => void;            // Reset expansions
    isExpanding?: boolean;                // Loading state
}
```

---

## 🔌 API Endpoints Reference

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/graph/expand` | Expand a node, get related entities |
| GET | `/graph/nodes` | Get base entity types (schema) |
| GET | `/graph/edges` | Get base relationships (schema) |
| POST | `/query/sync` | Main query endpoint |

### `/graph/expand` Details
```
POST /graph/expand
Query Parameters:
  - node_id: string (e.g., "CUST001")
  - node_type: string (e.g., "customer")

Response:
  200 OK: ExpandNodeResponse
  400: Invalid node_type
  503: Database not initialized
```

---

## 🎨 Visual Styling Guide

```css
/* Base schema entities */
.baseNode {
  size: val: 10;        /* Large */
  opacity: full;
  color: #predefined;
}

.baseEdge {
  width: 1px;
  opacity: 0.1;         /* Very faint */
  color: rgba(255,255,255,0.1);
}

/* Query result nodes/edges */
.resultNode {
  size: val: 5;         /* Medium */
  color: #inherited-from-type;
}

.resultEdge {
  width: 2px;
  opacity: 0.4;         /* Visible */
  color: rgba(255,255,255,0.4);
}

/* Expanded nodes/edges - NEW */
.expandedNode {
  size: val: 5;         /* Same as results */
  color: #inherited-from-type;
  data-expanded: true;  /* Tracked in state */
}

.expandedEdge {
  width: 2px;
  opacity: 0.3;         /* Distinct */
  color: rgba(100,200,255,0.3);  /* Light blue */
}
```

---

## 🔄 State Flow in React

```
App.tsx State:
├── expandedNodes: GraphNode[] (initially [])
├── expandedEdges: GraphEdge[] (initially [])
└── isExpandingNode: boolean (loading state)

Event: User clicks node
  ↓
App.handleNodeClick(node)
  ├─ Set isExpandingNode = true
  ├─ Call apiClient.expandNode()
  ├─ When response arrives:
  │  ├─ Merge with expandedNodes (deduped by ID)
  │  ├─ Merge with expandedEdges (deduped by source-target)
  │  └─ Set isExpandingNode = false
  └─ Pass all state to GraphCanvas

Event: User clicks Reset
  ↓
App.handleResetGraph()
  ├─ Set expandedNodes = []
  └─ Set expandedEdges = []
```

---

## 🐛 Debug Tips

### In Browser Console
```javascript
// Check expansion state
console.log("Expanded nodes:", expandedNodes.length);
console.log("Expanded edges:", expandedEdges.length);

// Check if deduplication worked
const nodeIds = expandedNodes.map(n => n.id);
console.log("Duplicates:", nodeIds.filter((id, i) => nodeIds.indexOf(id) !== i));

// Mock expand for testing (if backend is down)
const mockResponse = {
  nodes: [{id: "X1", type: "customer"}],
  edges: [],
  source_node_id: "C001",
  source_node_type: "customer"
};
```

### In Browser DevTools
```
Performance Tab:
- Record → Make expansion → Stop
- Check: Main thread max ~10ms
- Look for: Long tasks that block interaction

Network Tab:
- Filter: XHR/Fetch
- Look for: POST /graph/expand
- Status should be: 200
- Time: typically 100-500ms
```

### Backend Logs
```
When expansion called:
2026-03-24 12:34:56 INFO 📥 expand_node: node_id=CUST001 type=customer
2026-03-24 12:34:56 INFO ↳ Found 3 related sales_orders
2026-03-24 12:34:56 INFO 📤 expand response: 3 nodes, 3 edges
```

---

## ✋ Don't Touch (Existing Code)

These files have dependencies and should NOT be modified without care:
- ✋ `/backend/graph/` - LangGraph orchestration
- ✋ `/backend/llm/` - AI/prompt logic  
- ✋ `frontend/src/components/ChatPanel.tsx` - Chat UI
- ✋ Database schema files

---

## ✅ Safety Checklist Before Deploy

- [ ] Backend syntax: `python3 -m py_compile backend/main.py`
- [ ] Frontend build: `cd frontend && npm run build`
- [ ] No console errors when running
- [ ] Expansion works with at least one node click
- [ ] Reset button clears graph
- [ ] Edge tooltips appear on hover
- [ ] No memory leaks (dev tools over 5 min)
- [ ] CORS configured for production domain
- [ ] Database file `o2c.db` exists
- [ ] Rate limiting considered for production

---

## 📞 Quick Troubleshooting

| Problem | Solution |
|---------|----------|
| Expansion doesn't work | 1. Check backend running 2. Check DevTools Network tab 3. Try curl test |
| No results found | Might be node type not in expansion_paths - add it |
| Graph too crowded | Click Reset to clear expansions |
| Edge tooltips don't show | Hover more slowly, they may be subtle |
| Memory keeps growing | Likely a ref/listener leak - check React hooks (useEffect cleanup) |
| CORS errors | Ensure frontend URL in backend CORS whitelist |

---

## 🎓 Key Files to Review

**For node expansion logic**:
- `backend/main.py` - `expand_node()` function (search for `@app.post("/graph/expand")`)

**For frontend state management**:
- `frontend/src/App.tsx` - `handleNodeClick()` and `handleResetGraph()`

**For graph rendering**:
- `frontend/src/components/GraphCanvas.tsx` - Graph props and useMemo calculation

**For API calls**:
- `frontend/src/api/client.ts` - `expandNode()` method

---

**Last Updated**: 2026-03-24  
**Recommended Reading Order**:  
1. This file (you are here)
2. IMPLEMENTATION_SUMMARY.md (detailed overview)
3. TESTING_GUIDE.md (how to verify everything works)
4. Code files (when you need to debug/modify)
