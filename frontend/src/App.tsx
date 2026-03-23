import { useState } from 'react';
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
  
  const [highlightNodes, setHighlightNodes] = useState<GraphNode[]>([]);
  const [highlightEdges, setHighlightEdges] = useState<GraphEdge[]>([]);

  const handleSendMessage = async (query: string) => {
    // 1. Add user message
    const userMsg: Message = {
      id: Date.now().toString() + '-user',
      role: 'user',
      content: query
    };
    
    // Extract history before adding new query
    const history = messages.map(m => m.content);
    
    setMessages(prev => [...prev, userMsg]);
    setIsLoading(true);

    try {
      const response = await apiClient.querySync({
        query,
        conversation_history: history
      });

      // Update graph highlights
      if (response.highlight_nodes) setHighlightNodes(response.highlight_nodes);
      if (response.highlight_edges) setHighlightEdges(response.highlight_edges);

      // Add assistant message
      const asstMsg: Message = {
        id: Date.now().toString() + '-asst',
        role: 'assistant',
        content: response.answer,
        metadata: response
      };
      setMessages(prev => [...prev, asstMsg]);

    } catch (error: any) {
      console.error("API Error:", error);
      
      // Fallback error message
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

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas text-gray-200">
      
      {/* 60% Graph Panel */}
      <div className="w-[60%] h-full relative">
        <GraphCanvas 
          highlightNodes={highlightNodes} 
          highlightEdges={highlightEdges} 
        />
        
        {/* Absolute header over graph */}
        <div className="absolute top-0 left-0 p-6 pointer-events-none">
          <h1 className="text-2xl font-bold text-white flex items-center gap-2 tracking-tight">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center shadow-lg shadow-blue-900/50">
               <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-white"><path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            </div>
            DodgeAI 
            <span className="font-light opacity-50">FDE</span>
          </h1>
        </div>
      </div>

      {/* 40% Chat Panel */}
      <div className="w-[40%] h-full flex flex-col shadow-2xl relative z-10">
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
