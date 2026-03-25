# Testing Guide: Node Expansion & Graph Features

## Overview
This guide covers testing the new node expansion, edge tooltips, and graph reset features that have been added to DodgeAI-FDE.

## Features Implemented

### 1. **Node Expansion** ✨
- Click on any instance node in the graph (not base entity types)
- Backend queries database to find related entities
- Related nodes and edges are merged into the graph
- Duplicates are automatically avoided (Map-based deduplication)

### 2. **Edge Tooltips** 🔗
- Hover over any edge to see the relationship type
- Example: "Relationship: placed", "Relationship: fulfilled_by"
- Works on all edges (base, query result, and expanded)

### 3. **Graph Reset Button** 🔄
- Appears in top-right corner when expanded nodes exist
- Click to clear all expanded nodes/edges
- Returns graph to initial state with query results only

## End-to-End Testing Plan

### Prerequisites
1. Backend running on `http://127.0.0.1:8000`
2. Frontend running on `http://localhost:5173` (or similar)
3. Database (`o2c.db`) accessible in backend directory

### Test Scenario 1: Basic Query & Highlight
**Goal**: Verify that basic query functionality still works

**Steps**:
1. Open the DodgeAI FDE interface
2. Ask a query: "Show me top customers"
3. **Expected**: 
   - Chat response appears in right panel
   - Base entity types appear in graph (Customer, Sales Order, etc.)
   - Some nodes are highlighted (query results)

**Verify**:
- ✓ Graph loads with base schema
- ✓ Query results highlight relevant nodes/edges
- ✓ No errors in browser console



### Test Scenario 2: Node Expansion
**Goal**: Verify that clicking a node expands related entities

**Steps**:
1. Complete Test Scenario 1
2. Hover over a **result node** (not a base entity type)
   - Should show tooltip with node type and ID
   - Should say "Click to expand"
3. Click on the node
4. **Expected**:
   - Loading state briefly appears
   - New nodes and edges appear in light blue
   - Reset button becomes visible in top-right

**Verify**:
- ✓ Node click is registered (no console errors)
- ✓ Backend `/graph/expand` endpoint is called
- ✓ New nodes inserted with different visual style
- ✓ No duplicate nodes added
- ✓ Edges correctly connect expanded nodes

**Browser Console Checks**:
```
// Should see these API calls:
POST http://127.0.0.1:8000/graph/expand
// Response should be valid ExpandNodeResponse
```



### Test Scenario 3: Edge Tooltips
**Goal**: Verify that edges show relationship types on hover

**Steps**:
1. Move mouse over any edge in the graph
2. Hover briefly
3. **Expected**:
   - A small tooltip appears near cursor
   - Shows format: "Relationship: [type]"
   - Example: "Relationship: placed"

**Verify**:
- ✓ Tooltip appears on hover (not always visible)
- ✓ Correct relationship type shown
- ✓ Works on base edges, query edges, and expanded edges



### Test Scenario 4: Reset Graph
**Goal**: Verify that reset clears all expanded data

**Steps**:
1. Complete Test Scenario 2 (have expanded nodes visible)
2. Note the number of total nodes/edges visible
3. Click the "Reset" button (top-right)
4. **Expected**:
   - All expanded nodes disappear
   - Reset button disappears
   - Graph returns to state after initial query
   - Only base schema and query result nodes remain

**Verify**:
- ✓ Button click event is registered
- ✓ Graph state is properly reset
- ✓ No nodes/edges are orphaned
- ✓ Visual styling reverts to normal



### Test Scenario 5: End-to-End Flow
**Goal**: Complete realistic usage scenario

**Steps**:
1. Start fresh (empty chat)
2. Ask: "Show me all customers and their orders"
3. Expand a Customer node → Should show related Sales Orders
4. Expand a Sales Order node → Should show related Deliveries
5. Expand a Delivery node → Should show related Billing Documents
6. Click Reset → Everything clears

**Verify**:
- ✓ Graph progressively expands
- ✓ No duplicates accumulate
- ✓ Visual hierarchy is maintained
- ✓ Reset works after multiple expansions
- ✓ No memory leaks (reopen & restart successfully)



### Test Scenario 6: Error Handling
**Goal**: Verify graceful error handling

**Steps**:
1. Try to expand a base entity type (should be ignored)
2. Stop backend server and try to expand
3. Try to expand with invalid node ID
4. **Expected**:
   - Invalid expansions are silently ignored
   - Network errors show console warning
   - UI remains responsive

**Verify**:
- ✓ No crash on invalid input
- ✓ Error messages logged to console
- ✓ UI remains interactive



### Test Scenario 7: Performance
**Goal**: Verify that expansion doesn't cause lag

**Steps**:
1. Execute 5+ expansions in sequence
2. Monitor browser DevTools → Performance tab
3. **Expected**:
   - Frame rate remains above 30 FPS
   - Memory usage doesn't spike dramatically
   - Response time under 2 seconds per expansion

**Verify**:
- ✓ Graph remains interactive
- ✓ No jank or stuttering
- ✓ Smooth animation transitions



## API Testing

### Test the `/graph/expand` Endpoint Directly

**Using curl**:
```bash
curl -X POST "http://127.0.0.1:8000/graph/expand?node_id=CUST123&node_type=customer" \
  -H "Content-Type: application/json" \
  -d "{}"
```

**Expected Response**:
```json
{
  "nodes": [
    {
      "id": "SO12345",
      "type": "sales_order",
      "label": "Sales Order"
    }
  ],
  "edges": [
    {
      "source": "CUST123",
      "target": "SO12345",
      "source_type": "customer",
      "target_type": "sales_order",
      "label": "placed"
    }
  ],
  "source_node_id": "CUST123",
  "source_node_type": "customer"
}
```

### Test All Graph Endpoints

```bash
# Get base nodes
curl http://127.0.0.1:8000/graph/nodes | jq .

# Get base edges
curl http://127.0.0.1:8000/graph/edges | jq .

# Expand a node
curl -X POST "http://127.0.0.1:8000/graph/expand?node_id=test&node_type=customer" -d "{}"
```



## Browser DevTools Checks

### Console Errors to Watch For
- ❌ `Cannot read property 'onNodeClick' of undefined`
- ❌ `GraphNode type issue`
- ❌ Failed to parse Expand response
- ✓ Expected: Clean on successful flow

### Network Tab
- ✓ `/graph/expand` requests should be `POST` with status `200`
- ✓ Response time typically 100-500ms
- ✓ Payload size under 1KB for most queries

### Performance DevTools
- ✓ Main thread blocked < 16ms per frame
- ✓ Memory steady (~50-100MB base)
- ✓ No continuous memory growth



## Deduplication Verification

To verify that deduplication is working:

1. Expand the same node **twice**
2. Check graph node count
3. **Expected**: Node count should NOT double

**How to check**:
```javascript
// In browser console:
console.log("Total nodes:", graphData.nodes.length);
// Should not increase after second expansion
```



## Common Issues & Troubleshooting

### Issue: "Node expansion doesn't work"
- **Check**: Is the node you clicking actually a result node (not base)?
- **Check**: Is backend `/graph/expand` endpoint responding?
- **Check**: Console for error messages
- **Verify**: `POST http://127.0.0.1:8000/graph/expand` returns 200

### Issue: "Graph becomes too crowded"
- **Solution**: Click Reset button to clear expansions
- **Noted**: Limited to 5 related nodes per relation type (prevents explosion)

### Issue: "Edge tooltips not showing"
- **Note**: Tooltips are from react-force-graph library
- **Try**: Hover more slowly, may take moment to appear
- **Check**: Ensure hovering directly over edge line

### Issue: "Reset button doesn't appear"
- **Check**: Have you actually expanded any nodes?
- **Verify**: `expandedNodes.length > 0` in dev console

### Issue: "Backend 503 error"
- **Check**: Is database file present at `backend/o2c.db`?
- **Check**: Backend startup logs for errors
- **Try**: Restart backend with `uvicorn main:app --reload --port 8000`



## Success Criteria

✅ **All scenarios pass when**:
1. Base graph schema loads correctly
2. Query results highlight relevant nodes/edges
3. Node expansion works and finds related entities  
4. Edge tooltips display relationship types
5. Reset button clears expanded data
6. No duplicate nodes accumulate
7. No console errors when following test flow
8. Performance remains smooth (30+ FPS)
9. API endpoints respond correctly
10. Error cases are handled gracefully



## Next Steps for Enhancement

1. Implement infinite scrolling / pagination for large expansions (currently limited to 5)
2. Add animation when expanding nodes
3. Add breadcrumb trail showing expansion path
4. Implement node clustering for dense graphs
5. Add "expand all related" option for power users
6. Persist expansion state to URL for sharing

---

**Last Updated**: 2026-03-24  
**Version**: 1.0  
**Status**: Ready for Testing
