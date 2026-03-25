import { useState, useEffect } from 'react';
import { GraphCanvas } from './components/GraphCanvas';
import { ChatPanel } from './components/ChatPanel';
import { apiClient } from './api/client';
import type { GraphNode, GraphEdge, SyncQueryResponse } from './api/client';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  metadata?: SyncQueryResponse;
}

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  
  // Background graph (always shown)
  const [backgroundNodes, setBackgroundNodes] = useState<GraphNode[]>([]);
  const [backgroundEdges, setBackgroundEdges] = useState<GraphEdge[]>([]);
  
  // Query highlights (on top of background)
  const [highlightNodes, setHighlightNodes] = useState<GraphNode[]>([]);
  const [highlightEdges, setHighlightEdges] = useState<GraphEdge[]>([]);
  
  // Expanded node graph state
  const [expandedNodes, setExpandedNodes] = useState<GraphNode[]>([]);
  const [expandedEdges, setExpandedEdges] = useState<GraphEdge[]>([]);
  const [isExpandingNode, setIsExpandingNode] = useState(false);

  // Load sample graph on page load
  useEffect(() => {
    const loadSampleGraph = async () => {
      try {
        console.log('Loading sample graph for background...');
        const data = await apiClient.getGraphSample(50);
        setBackgroundNodes(data.nodes);
        setBackgroundEdges(data.edges);
        console.log('Background graph loaded:', data.nodes.length, 'nodes,', data.edges.length, 'edges');
      } catch (error) {
        console.error('Failed to load sample graph:', error);
      }
    };
    loadSampleGraph();
  }, []);

  const handleSendMessage = async (query: string) => {
    const userMsg: Message = {
      id: Date.now().toString() + '-user',
      role: 'user',
      content: query
    };
    
    const history = messages.map(m => m.content);
    
    setMessages(prev => [...prev, userMsg]);
    setIsLoading(true);

    try {
      const response = await apiClient.querySync({
        query,
        conversation_history: history
      });

      if (response.highlight_nodes) setHighlightNodes(response.highlight_nodes);
      if (response.highlight_edges) setHighlightEdges(response.highlight_edges);

      const asstMsg: Message = {
        id: Date.now().toString() + '-asst',
        role: 'assistant',
        content: response.answer,
        metadata: response
      };
      setMessages(prev => [...prev, asstMsg]);

    } catch (error: any) {
      console.error("API Error:", error);
      
      const errorMsg: Message = {
        id: Date.now().toString() + '-err',
        role: 'assistant',
        content: error.response?.data?.detail 
            || 'Sorry, there was an error connecting to the DodgeAI backend.',
        metadata: {
            answer: '',
            error: error.message,
            latency_ms: 0,
            retrieval_mode: 'unknown',
            highlight_edges: [],
            highlight_nodes: [],
            query_plan: null,
            sql_query: null
        }
      };
      setMessages(prev => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleNodeClick = async (node: any) => {
    console.log('=== handleNodeClick START ===');
    console.log('Node ID:', node.id, 'Type:', node.type);
    console.log('isExpandingNode state:', isExpandingNode);
    
    if (isExpandingNode) {
      console.log('Already expanding, returning early');
      return;
    }
    
    // Metric nodes (aggregations) cannot be expanded
    const metricTypes = ['count', 'revenue', 'amount', 'metric'];
    if (metricTypes.includes(node.type)) {
      console.log(`Cannot expand metric node of type '${node.type}'`);
      return;
    }
    
    console.log('Setting isExpandingNode to true');
    setIsExpandingNode(true);
    try {
      console.log('Making expand request for:', node.id);
      const response = await apiClient.expandNode(node.id, node.type || 'unknown');
      console.log('Expansion response received:', response.nodes.length, 'nodes,', response.edges.length, 'edges');
      
      setExpandedNodes(prev => {
        const existing = new Map(prev.map(n => [n.id, n]));
        response.nodes.forEach(n => {
          if (!existing.has(n.id)) {
            existing.set(n.id, n);
          }
        });
        const result = Array.from(existing.values());
        console.log('Updated expandedNodes total:', result.length);
        return result;
      });
      
      setExpandedEdges(prev => {
        const existing = new Set(prev.map(e => `${e.source}-${e.target}`));
        const newEdges = response.edges.filter(e => !existing.has(`${e.source}-${e.target}`));
        const result = [...prev, ...newEdges];
        console.log('Updated expandedEdges total:', result.length);
        return result;
      });
    } catch (error) {
      console.error("Error expanding node:", error);
    } finally {
      console.log('Setting isExpandingNode to false (finally block)');
      setIsExpandingNode(false);
      console.log('=== handleNodeClick END ===');
    }
  };

  const handleResetGraph = () => {
    console.log('=== RESET GRAPH ===');
    console.log('Before reset - expandedNodes:', expandedNodes.length, 'expandedEdges:', expandedEdges.length, 'isExpandingNode:', isExpandingNode);
    setExpandedNodes([]);
    setExpandedEdges([]);
    setIsExpandingNode(false);  // CRITICAL FIX: Reset expansion state
    console.log('Reset complete - expansion state cleared');
  };

  return (
    <div className="h-screen w-screen flex overflow-hidden bg-gray-50">
      
      {/* Left Panel — Graph (~60%) */}
      <div className="flex-[3] relative bg-white border-r border-gray-200">
        <GraphCanvas 
          backgroundNodes={backgroundNodes}
          backgroundEdges={backgroundEdges}
          highlightNodes={highlightNodes} 
          highlightEdges={highlightEdges}
          expandedNodes={expandedNodes}
          expandedEdges={expandedEdges}
          onNodeClick={handleNodeClick}
          onResetClick={handleResetGraph}
          isExpanding={isExpandingNode}
        />
        
        {/* Branded logo overlay */}
        <div className="absolute top-0 left-0 p-5 pointer-events-none z-20">
          <h1 className="text-xl font-bold text-gray-900 flex items-center gap-2.5 tracking-tight">
            <div className="w-8 h-8 bg-gradient-to-br from-blue-600 to-violet-600 rounded-lg flex items-center justify-center shadow-lg shadow-blue-200">
               <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-white"><path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            </div>
            DodgeAI 
            <span className="font-light opacity-50 text-base">FDE</span>
          </h1>
        </div>
      </div>

      {/* Right Panel — Chat (~40%) */}
      <div className="flex-[2] flex flex-col border-l border-gray-200 bg-white">
        <ChatPanel 
          messages={messages} 
          isLoading={isLoading} 
          onSendMessage={handleSendMessage} 
        />
      </div>

    </div>
  );
}

export default App;
