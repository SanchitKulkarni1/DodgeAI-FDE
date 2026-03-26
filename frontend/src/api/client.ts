import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'https://dodgeai-fde.onrender.com';

export interface GraphNode {
    id: string;
    type: string;
    label: string;
    color?: string;
    group?: number;
}

export interface GraphEdge {
    source: string;
    target: string;
    source_type?: string;
    target_type?: string;
    label?: string;
}

export interface ExpandNodeResponse {
    nodes: GraphNode[];
    edges: GraphEdge[];
    source_node_id: string;
    source_node_type: string;
}

export interface SyncQueryRequest {
    query: string;
    conversation_history: string[];
}

export interface SyncQueryResponse {
    answer: string;
    retrieval_mode: "sql" | "semantic" | "hybrid" | "off_topic" | "unknown";
    query_plan: string | null;
    sql_query: string | null;
    highlight_nodes: GraphNode[];
    highlight_edges: GraphEdge[];
    latency_ms: number;
    error: string | null;
}

export const apiClient = {
    async querySync(request: SyncQueryRequest): Promise<SyncQueryResponse> {
        const response = await axios.post<SyncQueryResponse>(`${API_BASE_URL}/query/sync`, request);
        return response.data;
    },
    
    async getGraphNodes(): Promise<{ entity_types: { type: string, label: string, color: string }[] }> {
        const response = await axios.get(`${API_BASE_URL}/graph/nodes`);
        return response.data;
    },
    
    async getGraphEdges(): Promise<{ edges: GraphEdge[], edge_count: number }> {
        const response = await axios.get(`${API_BASE_URL}/graph/edges`);
        return response.data;
    },
    
    async getGraphSample(limit: number = 50): Promise<{ nodes: GraphNode[], edges: GraphEdge[] }> {
        const response = await axios.get(`${API_BASE_URL}/graph/sample`, { params: { limit } });
        return response.data;
    },
    
    async expandNode(nodeId: string, nodeType: string): Promise<ExpandNodeResponse> {
        const response = await axios.post<ExpandNodeResponse>(
            `${API_BASE_URL}/graph/expand`,
            {},
            { params: { node_id: nodeId, node_type: nodeType } }
        );
        return response.data;
    }
};
