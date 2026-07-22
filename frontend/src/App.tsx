import { useState, FormEvent, ReactNode } from "react";
import TokensTable from "./components/TokensTable";
import Legend from "./components/Legend";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

type State =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; tokens: Token[]; demo: boolean }
  | { status: "error"; message: string };

type TimeUnit = "minutes" | "hours" | "days";

export default function App() {
  const [storeId, setStoreId] = useState("");
  const [timeValue, setTimeValue] = useState("");
  const [timeUnit, setTimeUnit] = useState<TimeUnit>("days");
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
      setState({ status: "success", tokens: data.tokens, demo: !!data.demo });
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
        <div className="w-36">
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

        <div>
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
            Time Window <span className="normal-case font-normal text-gray-400">(default 7 days)</span>
          </label>
          <div className="flex">
            <input
              type="number"
              min="1"
              placeholder="7"
              value={timeValue}
              onChange={(e) => setTimeValue(e.target.value)}
              className="w-20 px-3 py-2.5 border border-gray-300 rounded-l-lg text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
            />
            <select
              value={timeUnit}
              onChange={(e) => setTimeUnit(e.target.value as TimeUnit)}
              className="px-3 py-2.5 border border-l-0 border-gray-300 rounded-r-lg text-gray-700 bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition text-sm"
            >
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </select>
          </div>
        </div>

        <button
          type="submit"
          disabled={state.status === "loading"}
          className="px-6 py-2.5 bg-blue-600 text-white font-semibold rounded-lg hover:bg-blue-700 active:bg-blue-800 disabled:opacity-50 disabled:cursor-not-allowed transition whitespace-nowrap focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 mt-5"
        >
          {state.status === "loading" ? "Searching..." : "Search Tokens"}
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
          <span className="text-sm">Querying Snowflake...</span>
        </div>
      )}
      {state.status === "error" && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm">
          <span className="font-semibold">Error:</span> {state.message}
        </div>
      )}
      {state.status === "success" && state.demo && (
        <div className="px-4 py-2.5 bg-amber-50 border border-amber-200 rounded-lg text-amber-700 text-xs font-medium">
          Demo mode — showing cached data. Fill in{" "}
          <code className="font-mono bg-amber-100 px-1 rounded">backend/.env</code> with your
          Snowflake credentials for live queries.
        </div>
      )}
    </>
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50">
      <div className="max-w-[1400px] mx-auto px-4 py-10">
        <div className="mb-4">
          <h1 className="text-3xl font-bold text-gray-900 tracking-tight">API Token Analyzer</h1>
          <p className="mt-1 text-gray-500 text-sm">Query API tokens from Snowflake by store</p>
        </div>

        <Legend />

        <div className="mt-4">
        <TokensTable
          tokens={state.status === "success" ? state.tokens : []}
          storeId={parseInt(storeId, 10) || 0}
          timeValue={timeValue}
          timeUnit={timeUnit}
          demo={state.status === "success" ? state.demo : false}
          formSlot={formSlot}
          statusSlot={statusSlot}
        />
        </div>
      </div>
    </div>
  );
}
