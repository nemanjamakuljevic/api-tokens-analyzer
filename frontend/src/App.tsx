import { useState, FormEvent, ReactNode } from "react";
import TokensTable from "./components/TokensTable";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

type State =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; tokens: Token[] }
  | { status: "error"; message: string };

export default function App() {
  const [storeId, setStoreId] = useState("");
  const [state, setState] = useState<State>({ status: "idle" });

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setState({ status: "loading" });
    try {
      const res = await fetch("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: parseInt(storeId, 10) }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Unknown error" }));
        setState({ status: "error", message: err.detail ?? "Request failed" });
        return;
      }
      const data = await res.json();
      setState({ status: "success", tokens: data.tokens });
    } catch {
      setState({ status: "error", message: "Could not reach the backend. Is it running?" });
    }
  }

  const formSlot: ReactNode = (
    <form
      onSubmit={handleSubmit}
      className="bg-white rounded-xl shadow-sm border border-gray-200 py-3 px-5"
    >
      <div className="flex flex-row gap-4 items-center">
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
          disabled={state.status === "loading"}
          className="px-6 py-2.5 bg-blue-600 text-white font-semibold rounded-lg hover:bg-blue-700 active:bg-blue-800 disabled:opacity-50 disabled:cursor-not-allowed transition whitespace-nowrap focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 mt-5"
        >
          {state.status === "loading" ? "Searching…" : "Search Tokens"}
        </button>
      </div>
    </form>
  );

  const statusSlot: ReactNode = (
    <>
      {state.status === "loading" && (
        <div className="flex items-center gap-2 text-gray-500 py-2">
          <svg className="animate-spin h-4 w-4 text-blue-500 shrink-0" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
          </svg>
          <span className="text-sm">Querying Snowflake…</span>
        </div>
      )}
      {state.status === "error" && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm">
          <span className="font-semibold">Error:</span> {state.message}
        </div>
      )}
    </>
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50">
      <div className="max-w-[1400px] mx-auto px-4 py-10">
        <div className="mb-6">
          <h1 className="text-3xl font-bold text-gray-900 tracking-tight">API Token Analyzer</h1>
          <p className="mt-1 text-gray-500 text-sm">
            Query tokens from Snowflake — the AI agent picks the observation window and analyzes each token autonomously.
          </p>
        </div>

        <TokensTable
          tokens={state.status === "success" ? state.tokens : []}
          storeId={parseInt(storeId, 10) || 0}
          formSlot={formSlot}
          statusSlot={statusSlot}
        />
      </div>
    </div>
  );
}
