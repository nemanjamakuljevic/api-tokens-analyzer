import { useState, FormEvent } from "react";
import TokensTable from "./components/TokensTable";
import AgentPanel from "./components/AgentPanel";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

type AppMode = "free_form" | "store_id";

type StoreIdState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; tokens: Token[] }
  | { status: "error"; message: string };

type FreeFormState =
  | { status: "idle" }
  | { status: "analyzing"; message: string };

export default function App() {
  const [mode, setMode] = useState<AppMode>("free_form");

  // Free-form state
  const [freeFormInput, setFreeFormInput] = useState("");
  const [freeFormState, setFreeFormState] = useState<FreeFormState>({ status: "idle" });
  const [freeFormKey, setFreeFormKey] = useState(0);

  // Store-ID state
  const [storeId, setStoreId] = useState("");
  const [storeIdState, setStoreIdState] = useState<StoreIdState>({ status: "idle" });

  function submitFreeForm(msg: string) {
    const trimmed = msg.trim();
    if (!trimmed) return;
    setFreeFormState({ status: "analyzing", message: trimmed });
    setFreeFormKey((k) => k + 1);
  }

  function resetFreeForm() {
    setFreeFormState({ status: "idle" });
    setFreeFormInput("");
  }

  async function handleStoreIdSubmit(e: FormEvent) {
    e.preventDefault();
    setStoreIdState({ status: "loading" });
    try {
      const res = await fetch("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: parseInt(storeId, 10) }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Unknown error" }));
        setStoreIdState({ status: "error", message: err.detail ?? "Request failed" });
        return;
      }
      const data = await res.json();
      setStoreIdState({ status: "success", tokens: data.tokens });
    } catch {
      setStoreIdState({ status: "error", message: "Could not reach the backend. Is it running?" });
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50">
      <div className="max-w-[1400px] mx-auto px-4 py-10">
        <div className="mb-6">
          <h1 className="text-3xl font-bold text-gray-900 tracking-tight">API Token Analyzer</h1>
          <p className="mt-1 text-gray-500 text-sm">
            Ask anything about your API tokens — the agent decides what to fetch and how to answer.
          </p>
        </div>

        {/* Free-form mode */}
        {mode === "free_form" && (
          <>
            {freeFormState.status === "idle" && (
              <div className="max-w-2xl">
                <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-5">
                  <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                    What do you want to know?
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={freeFormInput}
                      onChange={(e) => setFreeFormInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey && freeFormInput.trim()) {
                          e.preventDefault();
                          submitFreeForm(freeFormInput);
                        }
                      }}
                      placeholder="e.g. Why is token 1152471 getting rate limited?"
                      autoFocus
                      className="flex-1 px-3.5 py-2.5 border border-gray-300 rounded-lg text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent transition"
                    />
                    <button
                      onClick={() => submitFreeForm(freeFormInput)}
                      disabled={!freeFormInput.trim()}
                      className="px-5 py-2.5 bg-violet-600 text-white font-semibold rounded-lg hover:bg-violet-700 disabled:opacity-40 disabled:cursor-not-allowed transition whitespace-nowrap"
                    >
                      Ask
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5 mt-3">
                    {[
                      "audit store 20116",
                      "why is token 1152471 in store 20116 getting rate limited?",
                      "clean up unused tokens in store 20116",
                    ].map((s) => (
                      <button
                        key={s}
                        onClick={() => submitFreeForm(s)}
                        className="text-[11px] px-2.5 py-1 rounded-full border border-gray-200 text-gray-500 hover:border-violet-300 hover:text-violet-600 transition"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
                <p className="mt-3 text-xs text-gray-400">
                  Or{" "}
                  <button
                    onClick={() => setMode("store_id")}
                    className="text-violet-500 hover:text-violet-700 underline"
                  >
                    run a full audit by store ID
                  </button>
                </p>
              </div>
            )}

            {freeFormState.status === "analyzing" && (
              <div className="max-w-[900px]">
                <div className="mb-3 flex items-center gap-3">
                  <span className="text-sm text-gray-500 italic">"{freeFormState.message}"</span>
                  <button
                    onClick={resetFreeForm}
                    className="text-xs text-gray-400 hover:text-gray-600 transition underline"
                  >
                    ← New question
                  </button>
                </div>
                <AgentPanel
                  key={freeFormKey}
                  storeId={0}
                  tokens={[]}
                  freeForm={true}
                  freeFormMessage={freeFormState.message}
                  chatFirst={false}
                  onClose={resetFreeForm}
                  onReanalyze={() => {
                    setFreeFormKey((k) => k + 1);
                  }}
                />
              </div>
            )}
          </>
        )}

        {/* Store-ID mode */}
        {mode === "store_id" && (
          <>
            <div className="mb-3">
              <button
                onClick={() => {
                  setMode("free_form");
                  setStoreIdState({ status: "idle" });
                  setStoreId("");
                }}
                className="text-xs text-violet-500 hover:text-violet-700 underline"
              >
                ← Back to free-form
              </button>
            </div>
            <div>
              {/* Store-ID form slot */}
              <form
                onSubmit={handleStoreIdSubmit}
                className="bg-white rounded-xl shadow-sm border border-gray-200 py-3 px-5 inline-flex flex-row gap-4 items-center mb-4"
              >
                <div className="w-44">
                  <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                    Store ID
                  </label>
                  <input
                    type="number"
                    min="1"
                    required
                    placeholder="e.g. 20116"
                    value={storeId}
                    onChange={(e) => setStoreId(e.target.value)}
                    className="w-full px-3.5 py-2.5 border border-gray-300 rounded-lg text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
                  />
                </div>
                <button
                  type="submit"
                  disabled={storeIdState.status === "loading"}
                  className="px-6 py-2.5 bg-blue-600 text-white font-semibold rounded-lg hover:bg-blue-700 active:bg-blue-800 disabled:opacity-50 disabled:cursor-not-allowed transition whitespace-nowrap focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 mt-5"
                >
                  {storeIdState.status === "loading" ? "Searching…" : "Search Tokens"}
                </button>
              </form>

              {storeIdState.status === "loading" && (
                <div className="flex items-center gap-2 text-gray-500 py-2 mb-4">
                  <svg className="animate-spin h-4 w-4 text-blue-500 shrink-0" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                  <span className="text-sm">Querying Snowflake…</span>
                </div>
              )}
              {storeIdState.status === "error" && (
                <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm mb-4">
                  <span className="font-semibold">Error:</span> {storeIdState.message}
                </div>
              )}

              <TokensTable
                tokens={storeIdState.status === "success" ? storeIdState.tokens : []}
                storeId={parseInt(storeId, 10) || 0}
                formSlot={null}
                statusSlot={null}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
