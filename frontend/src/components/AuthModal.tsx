interface AuthModalProps {
  onSelect: (mode: "api_key" | "claude_cli") => void;
}

export default function AuthModal({ onSelect }: AuthModalProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-gradient-to-br from-slate-100 to-blue-50">
      <div className="bg-white rounded-2xl shadow-xl border border-gray-200 max-w-md w-full mx-4 overflow-hidden">
        <div className="px-6 pt-6 pb-4 border-b border-gray-100">
          <h1 className="text-xl font-bold text-gray-900">API Token Analyzer</h1>
          <p className="mt-1 text-sm text-gray-500">Choose how to authenticate with Claude</p>
        </div>

        <div className="p-6 space-y-3">
          <button
            onClick={() => onSelect("api_key")}
            className="w-full text-left p-4 rounded-xl border-2 border-gray-200 hover:border-violet-400 hover:bg-violet-50 transition group"
          >
            <div className="flex items-start gap-3">
              <div className="mt-0.5 w-8 h-8 rounded-lg bg-violet-100 flex items-center justify-center shrink-0 group-hover:bg-violet-200 transition">
                <svg className="w-4 h-4 text-violet-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                </svg>
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-gray-900 text-sm">Anthropic API Key</div>
                <p className="text-xs text-gray-500 mt-0.5 leading-relaxed">
                  Uses <code className="bg-gray-100 px-1 rounded text-gray-700">ANTHROPIC_API_KEY</code> from your backend <code className="bg-gray-100 px-1 rounded text-gray-700">.env</code>. Full streaming and extended thinking.
                </p>
              </div>
            </div>
          </button>

          <button
            onClick={() => onSelect("claude_cli")}
            className="w-full text-left p-4 rounded-xl border-2 border-gray-200 hover:border-emerald-400 hover:bg-emerald-50 transition group"
          >
            <div className="flex items-start gap-3">
              <div className="mt-0.5 w-8 h-8 rounded-lg bg-emerald-100 flex items-center justify-center shrink-0 group-hover:bg-emerald-200 transition">
                <svg className="w-4 h-4 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
              </div>
              <div className="min-w-0">
                <div className="font-semibold text-gray-900 text-sm">Claude CLI Session</div>
                <p className="text-xs text-gray-500 mt-0.5 leading-relaxed">
                  Uses your logged-in <code className="bg-gray-100 px-1 rounded text-gray-700">claude</code> CLI session — no API key needed. Requires Claude Code installed and <code className="bg-gray-100 px-1 rounded text-gray-700">claude login</code>.
                </p>
              </div>
            </div>
          </button>
        </div>

        <div className="px-6 pb-5 text-center">
          <p className="text-[11px] text-gray-400">
            Your choice is saved in this browser. You can reset it by clearing localStorage.
          </p>
        </div>
      </div>
    </div>
  );
}
