from typing import TypedDict, List, Optional


class GraphState(TypedDict, total=False):
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

    # hybrid path — failure tracking
    hybrid_sql_failed: bool          # True if scoped SQL errored
    sql_error: Optional[str]         # error message from SQL execution

    # final
    final_answer: Optional[str]

    # UI support
    highlight_nodes: Optional[List[dict]]
    highlight_edges: Optional[List[dict]]

    error: Optional[str]