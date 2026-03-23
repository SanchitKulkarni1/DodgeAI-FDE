import React, { useState, useRef, useEffect } from 'react';
import { Send, Clock, Database, Search, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react';
import type { SyncQueryResponse } from '../api/client';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: any[]) {
    return twMerge(clsx(inputs));
}

interface Message {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    metadata?: SyncQueryResponse;
}

interface ChatPanelProps {
    messages: Message[];
    isLoading: boolean;
    onSendMessage: (msg: string) => void;
}

export const ChatPanel: React.FC<ChatPanelProps> = ({ messages, isLoading, onSendMessage }) => {
    const [inputValue, setInputValue] = useState('');
    const endOfMessagesRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        endOfMessagesRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isLoading]);

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!inputValue.trim() || isLoading) return;
        onSendMessage(inputValue);
        setInputValue('');
    };

    return (
        <div className="flex flex-col h-full bg-surface border-l border-gray-800">
            {/* Header */}
            <div className="p-4 border-b border-gray-800 bg-surfaceHighlight/50">
                <h2 className="text-lg font-semibold text-white tracking-tight">DodgeAI Assistant</h2>
                <p className="text-xs text-gray-400">Ask about your SAP Order-to-Cash data</p>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto p-4 space-y-6">
                {messages.length === 0 && (
                    <div className="flex flex-col items-center justify-center h-full text-center text-gray-500 space-y-3">
                        <Database size={32} className="opacity-50" />
                        <p>No messages yet. Try asking for the top customers or billing amounts.</p>
                    </div>
                )}
                {messages.map((msg) => (
                    <MessageBubble key={msg.id} msg={msg} />
                ))}
                {isLoading && (
                    <div className="flex justify-start">
                        <div className="bg-surfaceHighlight rounded-2xl p-4 shadow-sm border border-gray-800 flex items-center space-x-2">
                            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:-0.3s]"></div>
                            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:-0.15s]"></div>
                            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"></div>
                        </div>
                    </div>
                )}
                <div ref={endOfMessagesRef} />
            </div>

            {/* Input Form */}
            <div className="p-4 bg-surfaceHighlight/30 border-t border-gray-800">
                <form onSubmit={handleSubmit} className="relative flex items-center">
                    <input
                        type="text"
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        placeholder="Ask a question..."
                        className="w-full bg-canvas border border-gray-700 text-gray-100 rounded-full px-5 py-3 pr-12 focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500 transition-all placeholder:text-gray-500"
                        disabled={isLoading}
                    />
                    <button
                        type="submit"
                        disabled={!inputValue.trim() || isLoading}
                        className="absolute right-2 p-2 bg-blue-600 text-white rounded-full hover:bg-blue-500 disabled:opacity-50 disabled:hover:bg-blue-600 transition-colors"
                    >
                        <Send size={18} />
                    </button>
                </form>
            </div>
        </div>
    );
};

const MessageBubble: React.FC<{ msg: Message }> = ({ msg }) => {
    const isUser = msg.role === 'user';
    const isOffTopic = msg.metadata?.retrieval_mode === 'off_topic';
    const [planOpen, setPlanOpen] = useState(false);

    return (
        <div className={cn("flex w-full", isUser ? "justify-end" : "justify-start")}>
            <div
                className={cn(
                    "max-w-[85%] rounded-2xl p-4 shadow-sm flex flex-col gap-2",
                    isUser
                        ? "bg-blue-600 text-white rounded-tr-sm"
                        : cn(
                            "bg-surfaceHighlight border border-gray-800 text-gray-200 rounded-tl-sm",
                            isOffTopic && "border-amber-900/50 bg-amber-900/10"
                        )
                )}
            >
                {/* Main Content */}
                <div className="text-sm leading-relaxed whitespace-pre-wrap">
                    {msg.content}
                </div>

                {/* Metadata details for Assistant */}
                {!isUser && msg.metadata && (
                    <div className="mt-2 flex flex-col gap-2">
                        {/* Badges/Tags */}
                        <div className="flex items-center gap-2 flex-wrap">
                            {isOffTopic ? (
                                <span className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber-900/40 text-amber-500 border border-amber-800/50">
                                    <AlertTriangle size={12} /> Off-topic
                                </span>
                            ) : (
                                <span className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-blue-900/40 text-blue-400 border border-blue-800/50">
                                    <Search size={12} /> {msg.metadata.retrieval_mode}
                                </span>
                            )}
                            <span className="flex items-center gap-1 ml-auto text-xs text-gray-500 font-mono">
                                <Clock size={12} /> {msg.metadata.latency_ms} ms
                            </span>
                        </div>

                        {/* Query Details Toggle */}
                        {(msg.metadata.query_plan || msg.metadata.sql_query) && (
                            <div className="mt-2">
                                <button
                                    onClick={() => setPlanOpen(!planOpen)}
                                    className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-300 transition-colors"
                                >
                                    {planOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                                    Query Details
                                </button>
                                
                                {planOpen && (
                                    <div className="mt-2 space-y-3 p-3 bg-canvas/50 rounded-lg border border-gray-800">
                                        {msg.metadata.query_plan && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 mb-1">PLAN</h4>
                                                <pre className="text-xs text-gray-300 whitespace-pre-wrap font-mono">
                                                    {msg.metadata.query_plan}
                                                </pre>
                                            </div>
                                        )}
                                        {msg.metadata.sql_query && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 mb-1">SQL</h4>
                                                <div className="overflow-x-auto">
                                                    <pre className="text-xs text-green-400 font-mono">
                                                        {msg.metadata.sql_query}
                                                    </pre>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};
