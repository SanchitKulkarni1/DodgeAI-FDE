import React, { useState, useRef, useEffect } from 'react';
import { Send, Clock, Search, AlertTriangle } from 'lucide-react';
import type { SyncQueryResponse } from '../api/client';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';
import ReactMarkdown from 'react-markdown';

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

const SUGGESTED_PROMPTS = [
    "Show me the top 5 customers by revenue",
    "What are the delivery delays this month?",
    "Analyze billing patterns",
];

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
        <div className="flex flex-col h-full">
            {/* Header */}
            <div className="px-5 py-4 border-b border-gray-200">
                <h2 className="text-gray-900 font-semibold text-sm tracking-wide">Chat with Graph</h2>
                <p className="text-gray-500 text-xs mt-0.5">Order to Cash</p>
            </div>

            {/* Messages Area */}
            <div className="flex-1 overflow-y-auto p-5 space-y-1 chat-messages">
                {messages.length === 0 ? (
                    <div className="flex flex-col h-full justify-center items-center">
                        {/* Agent identity */}
                        <div className="flex items-center gap-3 mb-6">
                            <div className="w-10 h-10 rounded-full bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white text-sm font-bold shadow-lg shadow-blue-200">
                                D
                            </div>
                            <div>
                                <p className="text-gray-900 text-xs font-semibold">Dodge AI</p>
                                <p className="text-gray-500 text-[10px]">Graph Agent</p>
                            </div>
                        </div>

                        {/* Welcome message */}
                        <p className="text-gray-700 text-sm text-center mb-5 max-w-xs leading-relaxed">
                            Hi! I can help you analyze the Order-to-Cash dataset. Ask me about orders, deliveries, invoices, payments, customers, products, or addresses.
                        </p>

                        {/* Status indicator */}
                        <div className="flex items-center gap-2 text-xs text-green-600 mb-6">
                            <span className="w-1.5 h-1.5 rounded-full bg-green-600 animate-pulse"/>
                            Dodge AI is awaiting instructions
                        </div>

                        {/* Suggested prompts */}
                        <div className="flex flex-col gap-2 w-full max-w-xs">
                            {SUGGESTED_PROMPTS.map((prompt, i) => (
                                <button
                                    key={i}
                                    onClick={() => onSendMessage(prompt)}
                                    className="text-xs text-gray-600 hover:text-gray-900 transition-colors border border-gray-300 hover:border-gray-400 rounded-lg px-3 py-2 text-left hover:bg-gray-50"
                                >
                                    {prompt}
                                </button>
                            ))}
                        </div>
                    </div>
                ) : (
                    <>
                        {/* Agent header at top of conversation */}
                        <div className="flex items-center gap-3 mb-4 pb-3 border-b border-gray-200">
                            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white text-xs font-bold">
                                D
                            </div>
                            <div>
                                <p className="text-gray-900 text-xs font-semibold">Dodge AI</p>
                                <p className="text-gray-500 text-[10px]">Graph Agent</p>
                            </div>
                        </div>

                        {messages.map((msg) => (
                            <MessageBubble key={msg.id} msg={msg} />
                        ))}

                        {/* Loading indicator */}
                        {isLoading && (
                            <div className="flex gap-3 mb-4 message-enter">
                                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-600 to-violet-600 flex-shrink-0 flex items-center justify-center text-white text-[10px] font-bold">
                                    D
                                </div>
                                <div className="flex items-center gap-1.5 px-4 py-2">
                                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-bounce" style={{animationDelay: '0ms'}}/>
                                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-bounce" style={{animationDelay: '150ms'}}/>
                                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-bounce" style={{animationDelay: '300ms'}}/>
                                </div>
                            </div>
                        )}
                    </>
                )}
                <div ref={endOfMessagesRef} />
            </div>

            {/* Input Bar */}
            <div className="p-4 border-t border-gray-200">
                <form onSubmit={handleSubmit}>
                    <div className="flex items-center gap-2 bg-white border border-gray-300 rounded-xl px-4 py-2.5 shadow-sm">
                        <input
                            type="text"
                            value={inputValue}
                            onChange={(e) => setInputValue(e.target.value)}
                            placeholder="Ask a question..."
                            className="flex-1 bg-transparent text-gray-800 text-xs placeholder-gray-400 outline-none font-sans"
                            disabled={isLoading}
                        />
                        <button
                            type="submit"
                            disabled={!inputValue.trim() || isLoading}
                            className="w-7 h-7 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:hover:bg-blue-600 flex items-center justify-center transition-colors flex-shrink-0"
                        >
                            <Send size={12} className="text-white" />
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
};

// ——— Message Bubble Component ———

const MessageBubble: React.FC<{ msg: Message }> = ({ msg }) => {
    const isUser = msg.role === 'user';
    const isOffTopic = msg.metadata?.retrieval_mode === 'off_topic';

    if (isUser) {
        return (
            <div className="flex justify-end mb-3 message-enter">
                <span className="bg-blue-600 text-white text-xs px-4 py-2 rounded-full shadow-sm max-w-[80%] inline-block">
                    {msg.content}
                </span>
            </div>
        );
    }

    // Assistant message
    return (
        <div className="flex gap-3 mb-4 message-enter">
            <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-600 to-violet-600 flex-shrink-0 flex items-center justify-center text-white text-[10px] font-bold mt-0.5">
                D
            </div>
            <div className="flex-1 min-w-0">
                {/* Markdown content */}
                <div className="text-gray-800 text-xs leading-relaxed prose prose-xs max-w-none
                    prose-headings:text-gray-900 prose-headings:text-xs prose-headings:font-semibold prose-headings:mb-1 prose-headings:mt-2
                    prose-p:mb-1.5 prose-p:mt-0
                    prose-li:text-gray-700 prose-li:my-0.5
                    prose-strong:text-gray-900 prose-strong:font-semibold
                    prose-code:text-blue-600 prose-code:bg-blue-50 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-[11px] prose-code:font-mono
                    prose-pre:bg-gray-50 prose-pre:border prose-pre:border-gray-200 prose-pre:rounded-lg prose-pre:text-[11px]
                ">
                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                </div>

                {/* Metadata tags */}
                {msg.metadata && (
                    <div className="mt-2.5 flex flex-col gap-2">
                        <div className="flex items-center gap-2 flex-wrap">
                            {isOffTopic ? (
                                <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-amber-100 text-amber-700 border border-amber-300">
                                    <AlertTriangle size={10} /> Off-topic
                                </span>
                            ) : (
                                <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-blue-100 text-blue-700 border border-blue-300">
                                    <Search size={10} /> {msg.metadata.retrieval_mode}
                                </span>
                            )}
                            <span className="flex items-center gap-1 ml-auto text-[10px] text-gray-500 font-mono">
                                <Clock size={10} /> {msg.metadata.latency_ms}ms
                            </span>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};
