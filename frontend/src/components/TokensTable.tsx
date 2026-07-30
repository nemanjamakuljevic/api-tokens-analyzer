import { useState, ReactNode } from "react";
import AgentPanel from "./AgentPanel";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

interface Props {
  tokens: Token[];
  storeId: number;
  demo?: boolean;
  formSlot?: ReactNode;
  statusSlot?: ReactNode;
}

export default function TokensTable({ tokens, storeId, demo, formSlot, statusSlot }: Props) {
  const [analyzeKey, setAnalyzeKey] = useState(0);

  function handleReanalyze() {
    setAnalyzeKey((k) => k + 1);
  }

  return (
    <div className="flex gap-6 items-start">
      {/* Left: form + token list */}
      <div className="flex-1 min-w-0 space-y-5">
        {formSlot}
        {statusSlot}

        {tokens.length > 0 && (
          <details className="rounded-xl border border-gray-200 shadow-sm overflow-hidden group">
            <summary className="cursor-pointer select-none px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between text-sm font-medium text-gray-600 hover:bg-gray-100 transition-colors list-none">
              <span>
                {tokens.length} token{tokens.length !== 1 ? "s" : ""} found
              </span>
              <span className="text-xs text-gray-400 group-open:hidden">▾ expand</span>
              <span className="text-xs text-gray-400 hidden group-open:inline">▴ collapse</span>
            </summary>
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="px-4 py-3 font-semibold text-gray-600 uppercase tracking-wide text-xs">ID</th>
                    <th className="px-4 py-3 font-semibold text-gray-600 uppercase tracking-wide text-xs">Name</th>
                    <th className="px-4 py-3 font-semibold text-gray-600 uppercase tracking-wide text-xs">Created At</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 bg-white">
                  {tokens.map((token) => (
                    <tr key={token.id} className="hover:bg-gray-50 transition-colors">
                      <td className="px-4 py-3 font-mono text-gray-500">{token.id}</td>
                      <td className="px-4 py-3 font-medium text-gray-800">{token.name}</td>
                      <td className="px-4 py-3 text-gray-500">{token.created_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}
      </div>

      {/* Right sidebar: AI agent panel */}
      <div className="w-[680px] shrink-0 sticky top-4">
        {tokens.length > 0 && (
          <AgentPanel
            key={analyzeKey}
            storeId={storeId}
            tokens={tokens}
            demo={demo}
            chatFirst={true}
            onReanalyze={handleReanalyze}
            onClose={() => {}}
          />
        )}
      </div>
    </div>
  );
}
