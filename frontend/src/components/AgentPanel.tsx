import { useEffect, useRef, useState } from "react";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

interface AgentPanelProps {
  storeId: number;
  tokens: Token[];
  timeWindowSeconds: number;
  demo?: boolean;
  onClose: () => void;
}

interface Step {
  label: string;
  detail: string;
  ts: string;
  kind: "setup" | "thinking" | "tool:fetch" | "tool:skill" | "tool:score" | "tool:verify" | "tool:emit" | "error";
  tool?: string;
  call?: string;      // concrete tool-call signature, e.g. load_skill('token_cleanup')
  narration?: string; // agent's decision rationale, written before the tool call
}

// The agent's one-sentence "why" written before each tool call — shown inline.
function DecisionNote({ text }: { text?: string }) {
  if (!text) return null;
  return (
    <p className="mt-0.5 text-[10px] text-gray-500 italic leading-snug break-words whitespace-pre-wrap">
      {text}
    </p>
  );
}

// Monospace badge showing the exact tool call the agent decided to make.
function CallChip({ call }: { call?: string }) {
  if (!call) return null;
  return (
    <code className="mr-1.5 px-1.5 py-0.5 rounded bg-gray-900 text-gray-100 text-[10px] font-mono align-middle">
      {call}
    </code>
  );
}

interface TokenScore {
  token_id: number;
  token_name: string;
  rotation_score: number;
  rotation_reasoning: string;
  cleanup_score: number;
  cleanup_reasoning: string;
  security_audit_score: number;
  security_audit_reasoning: string;
  recommended_action: "token_rotation" | "token_cleanup" | "security_audit" | "no_action" | "insufficient_data";
  recommendation: string;
  verification_approved?: boolean;
  verification_reasoning?: string;
  // usage carried in the done event (agent fetched it mid-loop)
  splunk_count?: number | null;
  calls_per_second?: number | null;
  rate_429?: number | null;
  window_days?: number | null;
}

interface DoneResult {
  token_scores: TokenScore[];
  store_summary: string;
  iterations: number;
  approved: boolean;
  verification_reasoning: string;
  objections?: string[];
}

const SKILL_LABELS: Record<string, string> = {
  token_rotation: "Token Rotation",
  token_cleanup: "Token Cleanup",
  security_audit: "Security Audit",
  no_action: "No Action",
  insufficient_data: "Insufficient Data",
};

const SKILL_COLORS: Record<string, { badge: string; score: string }> = {
  token_rotation: {
    badge: "text-amber-700 bg-amber-50 border-amber-200",
    score: "text-amber-700",
  },
  token_cleanup: {
    badge: "text-blue-700 bg-blue-50 border-blue-200",
    score: "text-blue-700",
  },
  security_audit: {
    badge: "text-red-700 bg-red-50 border-red-200",
    score: "text-red-700",
  },
  no_action: {
    badge: "text-gray-500 bg-gray-50 border-gray-200",
    score: "text-gray-400",
  },
  insufficient_data: {
    badge: "text-orange-700 bg-orange-50 border-orange-300",
    score: "text-orange-500",
  },
};

function BoldText({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("**") && part.endsWith("**")
          ? <strong key={i}>{part.slice(2, -2)}</strong>
          : part
      )}
    </>
  );
}

function ExpandableText({ text, maxChars = 280 }: { text: string; maxChars?: number }) {
  const [expanded, setExpanded] = useState(false);
  if (text.length <= maxChars) {
    return <p className="text-sm text-gray-800 leading-relaxed"><BoldText text={text} /></p>;
  }
  const short = text.slice(0, maxChars).replace(/\s+\S*$/, "");
  return (
    <div>
      <p className="text-sm text-gray-800 leading-relaxed">
        <BoldText text={expanded ? text : short + "…"} />
      </p>
      <button
        onClick={() => setExpanded(!expanded)}
        className="mt-1 text-xs text-violet-600 hover:text-violet-800 font-medium"
      >
        {expanded ? "Show less" : "Read full recommendation"}
      </button>
    </div>
  );
}

// ── Step row components ────────────────────────────────────────────────────────

function ThinkingStep({ step }: { step: Step }) {
  const [expanded, setExpanded] = useState(false);
  const full = step.detail ?? "";
  const preview = full.length > 180 ? full.slice(0, 180).replace(/\s+\S*$/, "") + "…" : full;
  return (
    <div className="flex items-start gap-2 px-2 py-1.5 rounded border border-violet-200 bg-violet-50">
      <span className="text-violet-500 text-xs shrink-0 mt-0.5">✦</span>
      <div className="min-w-0 flex-1">
        <div className="text-[11px] text-violet-500 font-semibold">Extended Thinking</div>
        <p className="mt-0.5 text-[10px] text-violet-700/80 leading-snug break-words whitespace-pre-wrap">
          {expanded ? full : preview}
        </p>
        {full.length > 180 && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="mt-0.5 text-[10px] text-violet-500 font-medium hover:text-violet-700 transition"
          >
            {expanded ? "▴ less" : "▾ more"}
          </button>
        )}
      </div>
    </div>
  );
}

function SetupStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-gray-300 shrink-0 mt-1.5" />
      <div className="min-w-0">
        <span className="text-[11px] text-gray-500 font-medium">{step.label}</span>
        {step.detail && (
          <span className="text-[10px] text-gray-400 ml-1 break-words">— {step.detail}</span>
        )}
      </div>
    </div>
  );
}

function FetchStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="text-cyan-500 text-xs shrink-0">⤓</span>
      <div className="min-w-0">
        <CallChip call={step.call} />
        <span className="text-[11px] text-cyan-600 font-medium">{step.label}</span>
        {step.detail && (
          <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>
        )}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function SkillStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="text-indigo-500 text-xs shrink-0">📘</span>
      <div className="min-w-0">
        <CallChip call={step.call} />
        <span className="text-[11px] text-indigo-600 font-medium">{step.label}</span>
        {step.detail && (
          <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>
        )}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function ScoreStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="text-blue-400 text-xs shrink-0">⚡</span>
      <div className="min-w-0">
        <CallChip call={step.call} />
        <span className="text-[11px] text-blue-500 font-medium">{step.label}</span>
        {step.detail && (
          <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>
        )}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function VerifyStep({ step }: { step: Step }) {
  const approved = step.label.toUpperCase().includes("APPROVED");
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className={`text-xs shrink-0 font-bold ${approved ? "text-green-500" : "text-red-500"}`}>
        {approved ? "✓" : "✗"}
      </span>
      <div className="min-w-0">
        <CallChip call={step.call} />
        <span className={`text-[11px] font-medium ${approved ? "text-green-600" : "text-red-600"}`}>
          {step.label}
        </span>
        {step.detail && (
          <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>
        )}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function EmitStep({ step }: { step: Step }) {
  const arrowIdx = step.label.indexOf("→");
  const beforeArrow = arrowIdx >= 0 ? step.label.slice(0, arrowIdx).trim() : step.label;
  const actionStr = arrowIdx >= 0 ? step.label.slice(arrowIdx + 1).trim() : "";
  const colors = SKILL_COLORS[actionStr] ?? SKILL_COLORS.no_action;

  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="text-violet-500 text-xs shrink-0">→</span>
      <div className="min-w-0 flex-1">
        <CallChip call={step.call} />
        <span className="text-[11px] text-violet-500 font-medium">{beforeArrow}</span>
        {actionStr && (
          <span className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded-full border font-semibold ${colors.badge}`}>
            {SKILL_LABELS[actionStr] ?? actionStr}
          </span>
        )}
        {step.detail && (
          <span className="ml-1 text-[10px] text-gray-400">— {step.detail}</span>
        )}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function ErrorStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5 px-2 rounded bg-red-50 border border-red-200">
      <span className="text-red-500 text-xs shrink-0">✗</span>
      <div className="min-w-0">
        <span className="text-[11px] text-red-600 font-medium">{step.label}</span>
        {step.detail && (
          <span className="text-[10px] text-red-500 ml-1">{step.detail}</span>
        )}
      </div>
    </div>
  );
}

function StepRow({ step }: { step: Step }) {
  switch (step.kind) {
    case "thinking":   return <ThinkingStep step={step} />;
    case "tool:fetch": return <FetchStep step={step} />;
    case "tool:skill": return <SkillStep step={step} />;
    case "tool:score": return <ScoreStep step={step} />;
    case "tool:verify":return <VerifyStep step={step} />;
    case "tool:emit":  return <EmitStep step={step} />;
    case "error":      return <ErrorStep step={step} />;
    default:           return <SetupStep step={step} />;
  }
}

// ── Result card components ─────────────────────────────────────────────────────

function VerificationBlock({ reasoning, approved }: { reasoning: string; approved: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const maxChars = 220;
  const short = reasoning.length > maxChars
    ? reasoning.slice(0, maxChars).replace(/\s+\S*$/, "") + "…"
    : reasoning;
  const colors = approved
    ? "bg-green-50 border-green-100 text-green-800"
    : "bg-amber-50 border-amber-100 text-amber-800";

  return (
    <div className={`text-xs px-3 py-2.5 rounded-lg border ${colors}`}>
      <div className="font-semibold mb-1">{approved ? "Verification passed" : "Unverified"}</div>
      <p className="leading-relaxed">{expanded ? reasoning : short}</p>
      {reasoning.length > maxChars && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1 font-medium opacity-70 hover:opacity-100"
        >
          {expanded ? "Show less" : "Show full reasoning"}
        </button>
      )}
    </div>
  );
}

function TokenScoreCard({ ts }: { ts: TokenScore }) {
  const [open, setOpen] = useState(false);
  const colors = SKILL_COLORS[ts.recommended_action] ?? SKILL_COLORS.no_action;
  const calls = ts.splunk_count;
  const errors429 = ts.rate_429;
  const windowDays = ts.window_days ?? null;

  const scoreRows: [string, number, string, string][] = [
    ["Token Rotation", ts.rotation_score, ts.rotation_reasoning, "token_rotation"],
    ["Token Cleanup", ts.cleanup_score, ts.cleanup_reasoning, "token_cleanup"],
    ["Security Audit", ts.security_audit_score, ts.security_audit_reasoning, "security_audit"],
  ];

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <div className="px-4 py-3 bg-white">
        {/* Header row */}
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <span className="font-mono text-gray-400 text-xs mr-2">{ts.token_id}</span>
            <span className="font-semibold text-gray-800 text-sm">{ts.token_name}</span>
          </div>
          <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${colors.badge}`}>
            {SKILL_LABELS[ts.recommended_action] ?? ts.recommended_action}
          </span>
        </div>

        {/* Mini score chips + verification */}
        <div className="flex gap-1.5 mt-2 flex-wrap">
          {scoreRows.map(([label, score, , skill]) => (
            <span
              key={skill}
              className={`text-xs px-2 py-0.5 rounded-full border font-medium ${
                skill === ts.recommended_action && ts.recommended_action !== "no_action"
                  ? SKILL_COLORS[skill]?.badge ?? "text-gray-700 bg-gray-50 border-gray-200"
                  : "text-gray-400 bg-gray-50 border-gray-100"
              }`}
            >
              {label.replace("Token ", "")}: {score}
            </span>
          ))}
          {calls != null && (
            <span className="text-xs px-2 py-0.5 rounded-full border border-gray-100 text-gray-400 bg-gray-50">
              {calls} calls{windowDays != null ? ` / ${windowDays}d` : ""}
            </span>
          )}
          {errors429 != null && errors429 > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full border border-red-200 text-red-600 bg-red-50 font-semibold">
              {errors429} × 429
            </span>
          )}
          {ts.verification_approved != null && (
            <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${
              ts.verification_approved
                ? "text-green-700 bg-green-50 border-green-200"
                : "text-amber-700 bg-amber-50 border-amber-200"
            }`}>
              {ts.verification_approved ? "✓ verified" : "⚠ unverified"}
            </span>
          )}
        </div>

        {/* Insufficient data notice */}
        {ts.recommended_action === "insufficient_data" && (
          <div className="mt-2 px-2.5 py-1.5 rounded-lg bg-orange-50 border border-orange-200 text-[11px] text-orange-800 leading-snug">
            <span className="font-semibold">Insufficient data.</span> Retry the Splunk query with a minimum <strong>30-day</strong> window to confirm token inactivity before taking any action.
          </div>
        )}

        {/* Recommendation */}
        <div className="mt-2.5">
          <ExpandableText text={ts.recommendation} maxChars={200} />
        </div>

        {/* Toggle score details */}
        <button
          onClick={() => setOpen(!open)}
          className="mt-1.5 text-[11px] text-violet-500 hover:text-violet-700 font-medium flex items-center gap-1"
        >
          {open ? "▴" : "▾"} Score details
        </button>
      </div>

      {open && (
        <div className="border-t border-gray-100 divide-y divide-gray-50">
          {scoreRows.map(([label, score, reasoning, skill]) => (
            <div key={skill} className="px-4 py-2.5 bg-gray-50">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-[11px] font-semibold text-gray-500">{label}</span>
                <span className={`text-xs font-bold ${
                  skill === ts.recommended_action && ts.recommended_action !== "no_action"
                    ? SKILL_COLORS[skill]?.score ?? "text-gray-600"
                    : "text-gray-400"
                }`}>{score}</span>
              </div>
              <p className="text-[11px] text-gray-600 leading-snug">{reasoning || "—"}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export default function AgentPanel({
  storeId,
  tokens,
  timeWindowSeconds,
  demo,
  onClose,
}: AgentPanelProps) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [done, setDone] = useState<DoneResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(true);
  const [expanded, setExpanded] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;

    async function stream() {
      try {
        const res = await fetch("/api/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            store_id: storeId,
            tokens,
            time_window_seconds: timeWindowSeconds,
            demo: !!demo,
          }),
        });

        if (!res.ok || !res.body) {
          const err = await res.json().catch(() => ({ detail: "Analyze request failed" }));
          setError(err.detail ?? "Analyze request failed");
          setRunning(false);
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (!cancelled) {
          const { done: streamDone, value } = await reader.read();
          if (streamDone) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            try {
              const event = JSON.parse(line.slice(6));

              if (event.type === "thinking") {
                setSteps((prev) => [
                  ...prev,
                  {
                    label: "Thinking",
                    detail: event.content ?? "",
                    ts: event.ts ?? "",
                    kind: "thinking",
                  },
                ]);
              } else if (event.type === "step") {
                let kind: Step["kind"] = "setup";
                if (event.tool === "fetch_token_usage") kind = "tool:fetch";
                else if (event.tool === "load_skill") kind = "tool:skill";
                else if (event.tool === "score_single_token") kind = "tool:score";
                else if (event.tool === "verify_single_token_score") kind = "tool:verify";
                else if (event.tool === "emit_recommendation") kind = "tool:emit";

                setSteps((prev) => [
                  ...prev,
                  {
                    label: event.label ?? "",
                    detail: event.detail ?? "",
                    ts: event.ts ?? "",
                    kind,
                    tool: event.tool,
                    call: event.call,
                    narration: event.narration,
                  },
                ]);
              } else if (event.type === "done") {
                setDone(event as DoneResult);
                setRunning(false);
              } else if (event.type === "error") {
                setSteps((prev) => [
                  ...prev,
                  {
                    label: "Error",
                    detail: event.message ?? "",
                    ts: event.ts ?? "",
                    kind: "error",
                  },
                ]);
                setError(event.message);
                setRunning(false);
              }
            } catch {
              // malformed SSE line — skip
            }
          }
        }
      } catch (e: unknown) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Stream error");
          setRunning(false);
        }
      }
    }

    stream();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [steps]);

  // Sort token scores: highest-priority actions first
  const ACTION_ORDER: Record<string, number> = {
    security_audit: 0,
    token_rotation: 1,
    token_cleanup: 2,
    insufficient_data: 3,
    no_action: 4,
  };
  const sortedScores = done
    ? [...done.token_scores].sort(
        (a, b) =>
          (ACTION_ORDER[a.recommended_action] ?? 4) - (ACTION_ORDER[b.recommended_action] ?? 4)
      )
    : [];

  return (
    <div className="rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 bg-gradient-to-r from-violet-50 to-blue-50 border-b border-gray-200 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <svg className="h-4 w-4 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
          </svg>
          <span className="text-sm font-semibold text-gray-700">
            AI Token Analyzer
            {running && <span className="ml-2 inline-block animate-pulse text-violet-500">•</span>}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-gray-400 hover:text-gray-600 font-mono transition px-1"
            title={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? "[−]" : "[+]"}
          </button>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition" title="Close">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {expanded && (
        <>
          {/* Analysis feed */}
          <div className="bg-white border-b border-gray-100 p-3">
            <div className="max-h-72 overflow-y-auto space-y-1">
              {steps.map((step, i) => (
                <StepRow key={i} step={step} />
              ))}
              {running && (
                <div className="flex items-center gap-2 py-1 text-gray-400 text-xs">
                  <svg className="animate-spin h-3 w-3 text-violet-400 shrink-0" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                  Processing…
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          </div>

          {/* Error block */}
          {error && (
            <div className="px-4 py-3 bg-red-50 border-t border-red-200 text-sm text-red-700">
              <span className="font-semibold">Error:</span> {error}
            </div>
          )}

          {/* Per-token result cards */}
          {done && (
            <div className="bg-white p-4 space-y-4">
              <div>
                <div className="text-[10px] uppercase tracking-widest text-gray-400 font-semibold mb-2">
                  Token Recommendations
                </div>
                <div className="space-y-2">
                  {sortedScores.map((ts) => (
                    <TokenScoreCard key={ts.token_id} ts={ts} />
                  ))}
                </div>
              </div>


              {/* Store-level summary */}
              {done.store_summary && (
                <div className="px-3 py-3 rounded-lg bg-gray-50 border border-gray-100">
                  <div className="text-[10px] uppercase tracking-widest text-gray-400 font-semibold mb-1.5">
                    Store Summary
                  </div>
                  <p className="text-sm text-gray-700 leading-relaxed">
                    <BoldText text={done.store_summary} />
                  </p>
                </div>
              )}

              {/* Combined verification block */}
              {done.verification_reasoning && (
                <VerificationBlock
                  reasoning={done.verification_reasoning}
                  approved={done.approved}
                />
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
