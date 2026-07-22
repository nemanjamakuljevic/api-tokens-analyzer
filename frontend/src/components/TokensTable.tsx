import { useState, useEffect, ReactNode } from "react";
import AgentPanel from "./AgentPanel";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

type TimeUnit = "minutes" | "hours" | "days";

const UNIT_SECONDS: Record<TimeUnit, number> = {
  minutes: 60,
  hours: 3600,
  days: 86400,
};

interface Props {
  tokens: Token[];
  storeId: number;
  timeValue: string;
  timeUnit: TimeUnit;
  demo?: boolean;
  formSlot?: ReactNode;
  statusSlot?: ReactNode;
}

type UsageItem = { access_token_id: string; count: string };
type DetailItem = { access_token_id: string; method: string; full_path: string; status_code: string; count: string };

type SplunkState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; columns: string[]; rows: string[][]; splunkUrl: string; storeTotalUsage: UsageItem[]; storeDetailUsage: DetailItem[] }
  | { status: "redirect"; splunkUrl: string }
  | { status: "error"; message: string };

export default function TokensTable({ tokens, storeId, timeValue, timeUnit, demo, formSlot, statusSlot }: Props) {
  const [splunkState, setSplunkState] = useState<SplunkState>({ status: "idle" });
  const [analyzeOpen, setAnalyzeOpen] = useState(false);
  const [analyzeKey, setAnalyzeKey] = useState(0);

  const parsedTimeValue = parseInt(timeValue, 10);
  const hasValidTime = timeValue !== "" && parsedTimeValue > 0;
  const timeWindowSeconds = hasValidTime ? parsedTimeValue * UNIT_SECONDS[timeUnit] : 7 * 86400;

  async function runSplunkSearch(time_value: number, time_unit: TimeUnit) {
    setSplunkState({ status: "loading" });
    try {
      const res = await fetch("/api/splunk-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: storeId, token_ids: [], time_value, time_unit }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Unknown error" }));
        setSplunkState({ status: "error", message: err.detail ?? "Splunk request failed" });
        return;
      }

      const data = await res.json();
      if (data.redirect) {
        window.open(data.splunk_url, "_blank");
        setSplunkState({ status: "redirect", splunkUrl: data.splunk_url });
        return;
      }

      setSplunkState({
        status: "success",
        columns: data.columns,
        rows: data.rows,
        splunkUrl: data.splunk_url,
        storeTotalUsage: data.store_total_usage ?? [],
        storeDetailUsage: data.store_detail_usage ?? [],
      });
    } catch {
      setSplunkState({ status: "error", message: "Could not reach the backend. Is it running?" });
    }
  }

  function handleAnalyze() {
    setAnalyzeKey((k) => k + 1);
    setAnalyzeOpen(true);
  }

  // Auto-trigger Splunk when tokens load for a new store
  useEffect(() => {
    if (tokens.length === 0) return;
    setSplunkState({ status: "idle" });
    setAnalyzeOpen(false);
    const time_value = hasValidTime ? parsedTimeValue : 7;
    const time_unit = hasValidTime ? timeUnit : "days";
    runSplunkSearch(time_value, time_unit);
  }, [tokens]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-start analysis when Splunk data arrives
  useEffect(() => {
    if (splunkState.status === "success") {
      handleAnalyze();
    }
  }, [splunkState.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const retryTime = hasValidTime ? parsedTimeValue : 7;
  const retryUnit = hasValidTime ? timeUnit : "days";

  return (
    <div className="flex gap-6 items-start">
      {/* Left: main content */}
      <div className="flex-1 min-w-0 space-y-5">
        {formSlot}
        {statusSlot}
        {/* Tokens table — only when tokens are loaded */}
        {tokens.length === 0 ? null : <>
        {/* Tokens table */}
        <div className="rounded-xl border border-gray-200 shadow-sm overflow-hidden">
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
          <div className="px-4 py-2.5 bg-gray-50 border-t border-gray-200">
            <span className="text-xs text-gray-400">
              {tokens.length} token{tokens.length !== 1 ? "s" : ""} found
            </span>
          </div>
        </div>

        {/* Splunk loading */}
        {splunkState.status === "loading" && (
          <div className="flex items-center justify-between gap-3 px-5 py-4 bg-white rounded-xl border border-gray-200 shadow-sm">
            <div className="flex items-center gap-3">
              <svg className="animate-spin h-5 w-5 text-red-500 shrink-0" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              <span className="text-sm text-gray-600">Fetching Splunk usage data…</span>
            </div>
            <button
              onClick={() => runSplunkSearch(30, "minutes")}
              className="text-xs text-gray-400 hover:text-gray-700 underline underline-offset-2 transition whitespace-nowrap"
            >
              Taking too long? Try last 30 min
            </button>
          </div>
        )}

        {/* Splunk redirect — auth required */}
        {splunkState.status === "redirect" && (
          <div className="p-4 bg-amber-50 border border-amber-200 rounded-xl text-amber-800 text-sm flex items-start gap-3">
            <svg className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
            <div className="flex-1">
              <p className="font-semibold">Splunk authentication required</p>
              <p className="mt-0.5 text-amber-700">
                Authenticate in the{" "}
                <a href={splunkState.splunkUrl} target="_blank" rel="noopener noreferrer" className="underline hover:text-amber-900">
                  Splunk tab
                </a>{" "}
                that opened, then retry. Or add{" "}
                <code className="bg-amber-100 px-1 rounded font-mono text-xs">SPLUNK_TOKEN</code>{" "}
                to <code className="bg-amber-100 px-1 rounded font-mono text-xs">backend/.env</code> to skip this step.
              </p>
              <button
                onClick={() => runSplunkSearch(retryTime, retryUnit)}
                className="mt-2 px-3 py-1 text-xs font-semibold bg-amber-700 text-white rounded-lg hover:bg-amber-800 transition"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Splunk error */}
        {splunkState.status === "error" && (
          <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm flex items-start justify-between gap-3">
            <p><span className="font-semibold">Splunk error:</span> {splunkState.message}</p>
            <button
              onClick={() => runSplunkSearch(retryTime, retryUnit)}
              className="shrink-0 px-3 py-1 text-xs font-semibold bg-red-700 text-white rounded-lg hover:bg-red-800 transition"
            >
              Retry
            </button>
          </div>
        )}

        {/* Splunk results — grouped by token */}
        {splunkState.status === "success" && (
          <div className="rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div className="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-gray-700">Splunk Usage</h2>
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-400">{splunkState.rows.length} row{splunkState.rows.length !== 1 ? "s" : ""}</span>
                <a href={splunkState.splunkUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-500 hover:text-blue-700 underline">
                  View in Splunk
                </a>
              </div>
            </div>
            {splunkState.rows.length === 0 ? (
              <div className="px-4 py-8 text-center text-gray-500 text-sm bg-white space-y-2">
                <p>No events found for this store in the queried window.</p>
                <button
                  onClick={() => runSplunkSearch(30, "minutes")}
                  className="text-xs text-blue-500 hover:text-blue-700 underline underline-offset-2 transition"
                >
                  Try last 30 min instead
                </button>
              </div>
            ) : (() => {
              // group rows by access_token_id (col 0), preserve insertion order
              const colIdx = {
                tokenId: splunkState.columns.indexOf("access_token_id"),
                method: splunkState.columns.indexOf("method"),
                path: splunkState.columns.indexOf("full_path"),
                status: splunkState.columns.indexOf("status_code"),
                count: splunkState.columns.indexOf("count"),
              };
              const groups = new Map<string, string[][]>();
              splunkState.rows.forEach((row) => {
                const tid = row[colIdx.tokenId] ?? "unknown";
                if (!groups.has(tid)) groups.set(tid, []);
                groups.get(tid)!.push(row);
              });
              return (
                <div className="divide-y divide-gray-100 bg-white">
                  {Array.from(groups.entries()).map(([tokenId, rows]) => {
                    const tokenName = tokens.find((t) => String(t.id) === tokenId)?.name;
                    const total = rows.reduce((s, r) => s + (parseInt(r[colIdx.count], 10) || 0), 0);
                    const has429 = rows.some((r) => r[colIdx.status] === "429");
                    return (
                      <div key={tokenId}>
                        {/* Token group header */}
                        <div className="flex items-center gap-2 px-4 py-2 bg-gray-50 border-b border-gray-100">
                          <span className="font-mono text-xs text-gray-400">{tokenId}</span>
                          {tokenName && (
                            <span className="text-xs font-semibold text-gray-700">{tokenName}</span>
                          )}
                          <span className="ml-auto text-xs text-gray-400">{total.toLocaleString()} calls</span>
                          {has429 && (
                            <span className="text-xs font-semibold text-red-600 bg-red-50 border border-red-200 px-1.5 py-0.5 rounded-full">
                              429
                            </span>
                          )}
                        </div>
                        {/* Rows for this token */}
                        <table className="w-full text-xs text-left">
                          <thead className="bg-white border-b border-gray-50">
                            <tr>
                              <th className="px-4 py-1.5 font-medium text-gray-400 uppercase tracking-wide text-[10px] w-16">Method</th>
                              <th className="px-4 py-1.5 font-medium text-gray-400 uppercase tracking-wide text-[10px]">Path</th>
                              <th className="px-4 py-1.5 font-medium text-gray-400 uppercase tracking-wide text-[10px] w-20 text-right">Status</th>
                              <th className="px-4 py-1.5 font-medium text-gray-400 uppercase tracking-wide text-[10px] w-16 text-right">Count</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-gray-50">
                            {rows.map((row, i) => (
                              <tr key={i} className="hover:bg-gray-50 transition-colors">
                                <td className="px-4 py-2 font-mono text-gray-500">{row[colIdx.method]}</td>
                                <td className="px-4 py-2 font-mono text-gray-700 break-all">{row[colIdx.path]}</td>
                                <td className={`px-4 py-2 font-mono text-right font-semibold ${
                                  row[colIdx.status] === "429" ? "text-red-600" : "text-gray-500"
                                }`}>{row[colIdx.status]}</td>
                                <td className="px-4 py-2 font-mono text-gray-700 text-right">{row[colIdx.count]}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    );
                  })}
                </div>
              );
            })()}
          </div>
        )}
        </> /* end tokens.length > 0 */}
      </div>

      {/* Right sidebar: AI panel — sticky */}
      <div className="w-[650px] shrink-0 space-y-4 sticky top-4">
        {analyzeOpen && (
          <AgentPanel
            key={analyzeKey}
            storeId={storeId}
            tokens={tokens}
            timeWindowSeconds={timeWindowSeconds}
            demo={demo}
            onClose={() => setAnalyzeOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
