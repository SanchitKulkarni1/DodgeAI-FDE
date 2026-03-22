from typing import TypedDict, List, Optional

class GraphState(TypedDict):
    user_query: str
    
    # memory
    conversation_history: List[str]
    resolved_query: Optional[str]
    
    # intent + routing
    intent: Optional[str]
    retrieval_mode: Optional[str]  # "sql" | "semantic" | "hybrid"
    
    # SQL path
    query_plan: Optional[str]
    sql_query: Optional[str]
    query_result: Optional[List[dict]]
    
    # semantic path
    semantic_results: Optional[List[dict]]
    
    # final
    final_answer: Optional[str]
    
    # UI support
    highlight_nodes: Optional[List[dict]]
    highlight_edges: Optional[List[dict]]
    
    error: Optional[str]