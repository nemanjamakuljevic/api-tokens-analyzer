import { useEffect, useRef, useState, KeyboardEvent } from "react";
import ClarificationBox from "./ClarificationBox";

interface Token {
  id: number;
  name: string;
  created_at: string;
}

interface AgentPanelProps {
  storeId: number;
  tokens: Token[];
  chatFirst?: boolean;
  userMessage?: string;
  freeForm?: boolean;
  freeFormMessage?: string;
  authMode?: string | null;
  onClose: () => void;
  onReanalyze?: () => void;
}

interface SplunkRow {
  id?: number;
  token_id?: string | number;
  name?: string;
  token_name?: string;
  calls?: number;
  cps?: number;
  rate_429?: number;
  fill_top?: number;
  count_429?: number;
}

interface Step {
  label: string;
  detail: string;
  ts: string;
  kind: "setup" | "thinking" | "intent" | "tool:fetch" | "tool:skill" | "tool:score" | "tool:verify" | "tool:emit" | "loop:rejection" | "error";
  tool?: string;
  call?: string;
  narration?: string;
  splunk_rows?: SplunkRow[];
  splunk_429_rows?: SplunkRow[];
  re_query?: boolean;
  prev_window_days?: number;
  loop_event?: boolean;
  rescore?: boolean;
  // intent-specific
  request_type?: string;
  requires_recommendation?: boolean;
  open_questions?: string[];
}

interface ClarificationEvent {
  question: string;
  context?: string;
}

interface StatusCode {
  code: number;
  name: string;
  description: string;
}

function DecisionNote({ text }: { text?: string }) {
  if (!text) return null;
  return (
    <p className="mt-0.5 text-[10px] text-gray-500 italic leading-snug break-words whitespace-pre-wrap">
      {text}
    </p>
  );
}

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
  splunk_count?: number | null;
  calls_per_second?: number | null;
  rate_429?: number | null;
  window_days?: number | null;
}

interface DoneResult {
  session_id: string | null;
  token_scores: TokenScore[];
  store_summary: string;
  iterations: number;
  approved: boolean;
  verification_reasoning: string;
}

const SKILL_LABELS: Record<string, string> = {
  token_rotation: "Token Rotation",
  token_cleanup: "Token Cleanup",
  security_audit: "Security Audit",
  no_action: "No Action",
  insufficient_data: "Insufficient Data",
};

const SKILL_COLORS: Record<string, { badge: string; score: string }> = {
  token_rotation: { badge: "text-amber-700 bg-amber-50 border-amber-200", score: "text-amber-700" },
  token_cleanup:  { badge: "text-blue-700 bg-blue-50 border-blue-200",   score: "text-blue-700"  },
  security_audit: { badge: "text-red-700 bg-red-50 border-red-200",      score: "text-red-700"   },
  no_action:      { badge: "text-gray-500 bg-gray-50 border-gray-200",   score: "text-gray-400"  },
  insufficient_data: { badge: "text-orange-700 bg-orange-50 border-orange-300", score: "text-orange-500" },
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

// ── Splunk data collapsible table ──────────────────────────────────────────────

function SplunkTable({ rows, is429 }: { rows: SplunkRow[]; is429?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!rows || rows.length === 0) return null;
  return (
    <div className="mt-1">
      <button
        onClick={() => setOpen(!open)}
        className="text-[10px] text-cyan-500 hover:text-cyan-700 font-medium flex items-center gap-0.5"
      >
        {open ? "▴" : "▾"} {is429 ? "429 breakdown" : "Splunk data"}
      </button>
      {open && (
        <div className="mt-1 overflow-x-auto rounded border border-gray-100">
          <table className="w-full text-[10px]">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-2 py-1 text-left text-gray-500 font-semibold">Token</th>
                {is429 ? (
                  <th className="px-2 py-1 text-right text-red-500 font-semibold">429s</th>
                ) : (
                  <>
                    <th className="px-2 py-1 text-right text-gray-500 font-semibold">Calls</th>
                    <th className="px-2 py-1 text-right text-gray-500 font-semibold">calls/s</th>
                    <th className="px-2 py-1 text-right text-red-400 font-semibold">429</th>
                    <th className="px-2 py-1 text-right text-amber-500 font-semibold">Fill%</th>
                  </>
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {rows.map((r, i) => {
                const label = r.name || r.token_name || String(r.token_id || r.id || "");
                return (
                  <tr key={i} className="hover:bg-gray-50">
                    <td className="px-2 py-1 text-gray-600 font-medium">{label}</td>
                    {is429 ? (
                      <td className="px-2 py-1 text-right font-mono text-red-600">{r.count_429 ?? 0}</td>
                    ) : (
                      <>
                        <td className="px-2 py-1 text-right font-mono text-gray-500">{r.calls ?? 0}</td>
                        <td className="px-2 py-1 text-right font-mono text-gray-500">{r.cps ?? 0}</td>
                        <td className="px-2 py-1 text-right font-mono text-red-400">{r.rate_429 ?? 0}</td>
                        <td className="px-2 py-1 text-right font-mono text-amber-600">{r.fill_top ?? 0}%</td>
                      </>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Status codes modal ─────────────────────────────────────────────────────────

function StatusCodesModal({ codes, source, onClose }: { codes: StatusCode[]; source: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl max-w-lg w-full max-h-[80vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-sm font-semibold text-gray-800">ReCharge API Status Codes</h2>
            <a
              href={source}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-violet-500 hover:text-violet-700"
            >
              {source}
            </a>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="overflow-y-auto flex-1 divide-y divide-gray-50">
          {codes.map((c) => (
            <div key={c.code} className="px-5 py-3">
              <div className="flex items-center gap-2 mb-0.5">
                <span className={`text-xs font-bold px-1.5 py-0.5 rounded font-mono ${
                  c.code >= 500 ? "bg-red-100 text-red-700"
                  : c.code === 429 ? "bg-amber-100 text-amber-700"
                  : c.code >= 400 ? "bg-orange-100 text-orange-700"
                  : "bg-green-100 text-green-700"
                }`}>{c.code}</span>
                <span className="text-sm font-semibold text-gray-700">{c.name}</span>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed">{c.description}</p>
            </div>
          ))}
        </div>
      </div>
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
        {step.detail && <span className="text-[10px] text-gray-400 ml-1 break-words">— {step.detail}</span>}
      </div>
    </div>
  );
}

function FetchStep({ step }: { step: Step }) {
  if (step.re_query) {
    return (
      <div className="flex items-start gap-2 py-0.5">
        <span className="text-amber-500 text-xs shrink-0 font-bold">⟳</span>
        <div className="min-w-0 flex-1">
          <CallChip call={step.call} />
          <span className="text-[11px] text-amber-600 font-medium">{step.label}</span>
          {step.prev_window_days != null && (
            <span className="text-[10px] text-amber-500 ml-1">— was {step.prev_window_days}d</span>
          )}
          {step.detail && <span className="text-[10px] text-gray-500 ml-1 break-words">· {step.detail}</span>}
          <DecisionNote text={step.narration} />
          {step.splunk_rows && step.splunk_rows.length > 0 && (
            <SplunkTable rows={step.splunk_rows} />
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className="text-cyan-500 text-xs shrink-0">⤓</span>
      <div className="min-w-0 flex-1">
        <CallChip call={step.call} />
        <span className="text-[11px] text-cyan-600 font-medium">{step.label}</span>
        {step.detail && <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>}
        <DecisionNote text={step.narration} />
        {step.splunk_rows && step.splunk_rows.length > 0 && (
          <SplunkTable rows={step.splunk_rows} />
        )}
        {step.splunk_429_rows && step.splunk_429_rows.length > 0 && (
          <SplunkTable rows={step.splunk_429_rows} is429 />
        )}
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
        {step.detail && <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function ScoreStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 py-0.5">
      <span className={`text-xs shrink-0 ${step.rescore ? "text-amber-500 font-bold" : "text-blue-400"}`}>
        {step.rescore ? "⟳" : "⚡"}
      </span>
      <div className="min-w-0">
        <CallChip call={step.call} />
        {step.rescore && (
          <span className="text-[10px] text-amber-600 font-semibold mr-1">Re-scoring:</span>
        )}
        <span className="text-[11px] text-blue-500 font-medium">{step.label}</span>
        {step.detail && <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>}
        <DecisionNote text={step.narration} />
      </div>
    </div>
  );
}

function LoopEventStep({ step }: { step: Step }) {
  return (
    <div className="flex items-start gap-2 px-2 py-1.5 rounded border border-amber-300 bg-amber-50">
      <span className="text-amber-600 text-xs font-bold shrink-0">⟳</span>
      <div className="min-w-0">
        <span className="text-[11px] text-amber-700 font-semibold">Judge rejected — agent re-scoring</span>
        {step.detail && (
          <p className="text-[10px] text-amber-600 mt-0.5 leading-snug break-words">{step.detail}</p>
        )}
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
        {step.detail && <span className="text-[10px] text-gray-500 ml-1 break-words">— {step.detail}</span>}
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
        {step.detail && <span className="ml-1 text-[10px] text-gray-400">— {step.detail}</span>}
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
        {step.detail && <span className="text-[10px] text-red-500 ml-1">{step.detail}</span>}
      </div>
    </div>
  );
}

function IntentStep({ step }: { step: Step }) {
  const label = (step.request_type ?? "").replace(/_/g, " ");
  const hasOpenQuestions = (step.open_questions ?? []).length > 0;
  return (
    <div className="flex items-start gap-2 px-2 py-1.5 rounded border border-indigo-200 bg-indigo-50">
      <span className="text-indigo-500 text-xs shrink-0 mt-0.5">◈</span>
      <div className="min-w-0 flex-1">
        <div className="text-[11px] text-indigo-600 font-semibold">Understood: {label}</div>
        {hasOpenQuestions ? (
          <p className="mt-0.5 text-[10px] text-amber-600 leading-snug">
            Open: {(step.open_questions ?? []).join(", ")}
          </p>
        ) : (
          <p className="mt-0.5 text-[10px] text-indigo-500 leading-snug">
            {step.requires_recommendation
              ? "Will produce recommendations."
              : "Informational — no recommendation pipeline."}
          </p>
        )}
      </div>
    </div>
  );
}

function StepRow({ step }: { step: Step }) {
  switch (step.kind) {
    case "thinking":       return <ThinkingStep step={step} />;
    case "intent":         return <IntentStep step={step} />;
    case "tool:fetch":     return <FetchStep step={step} />;
    case "tool:skill":     return <SkillStep step={step} />;
    case "tool:score":     return <ScoreStep step={step} />;
    case "tool:verify":    return <VerifyStep step={step} />;
    case "tool:emit":      return <EmitStep step={step} />;
    case "loop:rejection": return <LoopEventStep step={step} />;
    case "error":          return <ErrorStep step={step} />;
    default:               return <SetupStep step={step} />;
  }
}

// ── Result cards ──────────────────────────────────────────────────────────────

function TokenScoreCard({ ts }: { ts: TokenScore }) {
  const [open, setOpen] = useState(false);
  const colors = SKILL_COLORS[ts.recommended_action] ?? SKILL_COLORS.no_action;
  const scoreRows: [string, number, string, string][] = [
    ["Rotation",      ts.rotation_score,      ts.rotation_reasoning,      "token_rotation"],
    ["Cleanup",       ts.cleanup_score,        ts.cleanup_reasoning,       "token_cleanup"],
    ["Security",      ts.security_audit_score, ts.security_audit_reasoning,"security_audit"],
  ];

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <div className="px-4 py-3 bg-white">
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <span className="font-mono text-gray-400 text-xs mr-2">{ts.token_id}</span>
            <span className="font-semibold text-gray-800 text-sm">{ts.token_name}</span>
          </div>
          <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${colors.badge}`}>
            {SKILL_LABELS[ts.recommended_action] ?? ts.recommended_action}
          </span>
        </div>

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
              {label}: {score}
            </span>
          ))}
          {ts.splunk_count != null && (
            <span className="text-xs px-2 py-0.5 rounded-full border border-gray-100 text-gray-400 bg-gray-50">
              {ts.splunk_count} calls{ts.window_days != null ? ` / ${ts.window_days}d` : ""}
            </span>
          )}
          {ts.rate_429 != null && ts.rate_429 > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full border border-red-200 text-red-600 bg-red-50 font-semibold">
              {ts.rate_429} × 429
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

        {ts.recommended_action === "insufficient_data" && (
          <div className="mt-2 px-2.5 py-1.5 rounded-lg bg-orange-50 border border-orange-200 text-[11px] text-orange-800 leading-snug">
            <span className="font-semibold">Insufficient data.</span> Retry the Splunk query with a minimum <strong>30-day</strong> window.
          </div>
        )}

        <div className="mt-2.5">
          <ExpandableText text={ts.recommendation} maxChars={200} />
        </div>

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

// ── Chat message bubble ────────────────────────────────────────────────────────

function ChatBubble({ role, text }: { role: "user" | "agent"; text: string }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[90%] px-3 py-2 rounded-xl text-sm leading-relaxed ${
        isUser
          ? "bg-violet-600 text-white rounded-br-sm"
          : "bg-gray-100 text-gray-800 rounded-bl-sm"
      }`}>
        {isUser ? text : <BoldText text={text} />}
      </div>
    </div>
  );
}

// ── Streaming text display ─────────────────────────────────────────────────────

function StreamingText({ text, active }: { text: string; active: boolean }) {
  if (!text && !active) return null;
  return (
    <div className="px-3 py-2 bg-violet-50 rounded-xl border border-violet-100 text-sm text-gray-800 leading-relaxed">
      <BoldText text={text} />
      {active && <span className="inline-block w-1.5 h-3.5 ml-0.5 bg-violet-400 animate-pulse rounded-sm align-middle" />}
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export default function AgentPanel({
  storeId,
  tokens,
  chatFirst,
  userMessage,
  freeForm,
  freeFormMessage,
  authMode,
  onClose,
  onReanalyze,
}: AgentPanelProps) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [done, setDone] = useState<DoneResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [turnCount, setTurnCount] = useState(0);

  // Chat-first state; free-form with a message starts immediately
  const [started, setStarted] = useState(!chatFirst || (!!freeForm && !!freeFormMessage));
  const [initInput, setInitInput] = useState("");
  const pendingMsgRef = useRef<string>(freeFormMessage || "");

  // Streaming agent reply text
  const [streamingText, setStreamingText] = useState("");
  const [streamingActive, setStreamingActive] = useState(false);

  // HITL clarification
  const [clarification, setClarification] = useState<ClarificationEvent | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);

  // Follow-up chat
  const [chatMessages, setChatMessages] = useState<Array<{ role: "user" | "agent"; text: string }>>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatRunning, setChatRunning] = useState(false);
  const [chatStreamText, setChatStreamText] = useState("");

  // Status codes modal
  const [statusCodes, setStatusCodes] = useState<StatusCode[] | null>(null);
  const [statusCodesSource, setStatusCodesSource] = useState("");

  const bottomRef = useRef<HTMLDivElement>(null);
  const stepsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!started) return;

    let cancelled = false;

    async function stream() {
      setRunning(true);
      try {
        const url = freeForm ? "/api/chat/ask" : "/api/chat/start";
        const body = freeForm
          ? { message: pendingMsgRef.current || userMessage || "", auth_mode: authMode ?? "api_key" }
          : { store_id: storeId, tokens, user_message: pendingMsgRef.current || userMessage || "", auth_mode: authMode ?? "api_key" };
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
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

              if (event.type === "thought_chunk") {
                setSteps((prev) => {
                  const last = prev[prev.length - 1];
                  if (last?.kind === "thinking") {
                    const updated = [...prev];
                    updated[updated.length - 1] = { ...last, detail: (last.detail || "") + event.delta };
                    return updated;
                  }
                  setTurnCount((n) => n + 1);
                  return [...prev, { label: "Thinking", detail: event.delta ?? "", ts: event.ts ?? "", kind: "thinking" }];
                });

              } else if (event.type === "content_chunk") {
                setStreamingActive(true);
                setStreamingText((prev) => prev + (event.delta ?? ""));

              } else if (event.type === "thinking") {
                const snippet = (event.content ?? "").trim();
                if (snippet) {
                  setSteps((prev) => [
                    ...prev,
                    { label: "Thinking", detail: snippet, ts: event.ts ?? "", kind: "thinking" },
                  ]);
                }

              } else if (event.type === "intent") {
                setSteps((prev) => [
                  ...prev,
                  {
                    label: event.label ?? `Understood: ${(event.request_type ?? "").replace(/_/g, " ")}`,
                    detail: event.detail ?? "",
                    ts: event.ts ?? "",
                    kind: "intent",
                    request_type: event.request_type,
                    requires_recommendation: event.requires_recommendation,
                    open_questions: event.open_questions ?? [],
                  },
                ]);

              } else if (event.type === "step") {
                let kind: Step["kind"] = "setup";
                if (event.tool === "fetch_token_usage" || event.tool === "fetch_429_errors" || event.tool === "lookup_store_tokens" || event.tool === "lookup_token_store") kind = "tool:fetch";
                else if (event.tool === "load_skill" || event.tool === "load_recharge_status_codes") kind = "tool:skill";
                else if (event.tool === "score_single_token") kind = "tool:score";
                else if (event.tool === "verify_single_token_score") kind = "tool:verify";
                else if (event.tool === "emit_recommendation") kind = "tool:emit";
                const newStep: Step = {
                  label: event.label ?? "",
                  detail: event.detail ?? "",
                  ts: event.ts ?? "",
                  kind,
                  tool: event.tool,
                  call: event.call,
                  narration: event.narration,
                  splunk_rows: event.splunk_rows,
                  splunk_429_rows: event.splunk_429_rows,
                  re_query: event.re_query ?? false,
                  prev_window_days: event.prev_window_days,
                  loop_event: event.loop_event ?? false,
                  rescore: event.rescore ?? false,
                };
                setSteps((prev) => {
                  const next = [...prev, newStep];
                  if (event.loop_event) {
                    next.push({
                      label: "Judge rejected — agent re-scoring",
                      detail: event.detail ?? "",
                      ts: event.ts ?? "",
                      kind: "loop:rejection",
                    });
                  }
                  return next;
                });

              } else if (event.type === "status_codes") {
                setStatusCodes(event.codes ?? []);
                setStatusCodesSource(event.source ?? "");

              } else if (event.type === "session_started") {
                setSessionId(event.session_id ?? null);

              } else if (event.type === "clarification_request") {
                setClarification({ question: event.question, context: event.context });

              } else if (event.type === "done") {
                setDone(event as DoneResult);
                setSessionId(event.session_id ?? null);
                setStreamingActive(false);
                setRunning(false);

              } else if (event.type === "error") {
                setSteps((prev) => [
                  ...prev,
                  { label: "Error", detail: event.message ?? "", ts: event.ts ?? "", kind: "error" },
                ]);
                setError(event.message);
                setStreamingActive(false);
                setRunning(false);
              }
            } catch {
              // malformed SSE line
            }
          }
        }
      } catch (e: unknown) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Stream error");
          setStreamingActive(false);
          setRunning(false);
        }
      }
    }

    stream();
    return () => { cancelled = true; };
  }, [started]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [steps, streamingText, chatMessages, chatStreamText]);

  function startAnalysis(msg: string) {
    pendingMsgRef.current = msg.trim();
    setStarted(true);
  }

  function handleInitKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey && initInput.trim()) {
      e.preventDefault();
      startAnalysis(initInput);
    }
  }

  async function sendChat() {
    if (!chatInput.trim() || chatRunning || !sessionId) return;
    const msg = chatInput.trim();
    setChatInput("");
    setChatMessages((prev) => [...prev, { role: "user", text: msg }]);
    setChatRunning(true);
    setChatStreamText("");

    let cancelled = false;
    let accumulated = "";

    try {
      const res = await fetch(`/api/chat/${sessionId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });

      if (!res.ok || !res.body) {
        setChatMessages((prev) => [...prev, { role: "agent", text: "Error — could not reach backend." }]);
        setChatRunning(false);
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
            if (event.type === "content_chunk") {
              accumulated += event.delta ?? "";
              setChatStreamText(accumulated);
            } else if (event.type === "chat_done") {
              setChatMessages((prev) => [...prev, { role: "agent", text: accumulated }]);
              setChatStreamText("");
              setChatRunning(false);
            } else if (event.type === "error") {
              setChatMessages((prev) => [...prev, { role: "agent", text: `Error: ${event.message}` }]);
              setChatStreamText("");
              setChatRunning(false);
            }
          } catch {
            // skip
          }
        }
      }
    } catch {
      if (!cancelled) {
        setChatMessages((prev) => [...prev, { role: "agent", text: "Connection error." }]);
        setChatStreamText("");
        setChatRunning(false);
      }
    }
  }

  function handleChatKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  }

  const ACTION_ORDER: Record<string, number> = {
    security_audit: 0, token_rotation: 1, token_cleanup: 2, insufficient_data: 3, no_action: 4,
  };
  const sortedScores = done
    ? [...done.token_scores].sort(
        (a, b) => (ACTION_ORDER[a.recommended_action] ?? 4) - (ACTION_ORDER[b.recommended_action] ?? 4)
      )
    : [];

  return (
    <>
      {/* Status codes modal */}
      {statusCodes && (
        <StatusCodesModal
          codes={statusCodes}
          source={statusCodesSource}
          onClose={() => setStatusCodes(null)}
        />
      )}

      <div className="rounded-xl border border-gray-200 shadow-sm overflow-hidden flex flex-col">
        {/* Header */}
        <div className="px-4 py-3 bg-gradient-to-r from-violet-50 to-blue-50 border-b border-gray-200 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2">
            <svg className="h-4 w-4 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
            </svg>
            <span className="text-sm font-semibold text-gray-700">
              AI Token Analyzer
              {running && <span className="ml-2 inline-block animate-pulse text-violet-500">•</span>}
            </span>
            {running && turnCount > 0 && (
              <span className="text-[10px] text-violet-400 font-mono bg-violet-50 px-1.5 py-0.5 rounded">
                turn {turnCount}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {done && onReanalyze && (
              <button
                onClick={onReanalyze}
                className="text-xs text-violet-500 hover:text-violet-700 font-medium transition"
              >
                Re-analyze
              </button>
            )}
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
            {/* Chat-first idle state */}
            {!started && (
              <div className="px-4 py-6 flex flex-col gap-3 bg-white">
                <div className="text-center">
                  <p className="text-sm text-gray-600 font-medium">What would you like to analyze?</p>
                  <p className="text-xs text-gray-400 mt-1">
                    {tokens.length} token{tokens.length !== 1 ? "s" : ""} loaded for store {storeId}
                  </p>
                </div>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={initInput}
                    onChange={(e) => setInitInput(e.target.value)}
                    onKeyDown={handleInitKey}
                    placeholder="e.g. Analyze token 1234567, or check REVIEWS.io for rate limiting…"
                    autoFocus
                    className="flex-1 px-3 py-2.5 text-sm border border-gray-300 rounded-lg text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-violet-400 transition"
                  />
                  <button
                    onClick={() => initInput.trim() && startAnalysis(initInput)}
                    disabled={!initInput.trim()}
                    className="px-4 py-2.5 bg-violet-600 text-white text-xs font-semibold rounded-lg hover:bg-violet-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
                  >
                    Analyze
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5 justify-center">
                  {[
                    "Analyze all tokens",
                    "Check rate limiting",
                    "Find idle tokens",
                    "Security check",
                  ].map((suggestion) => (
                    <button
                      key={suggestion}
                      onClick={() => startAnalysis(suggestion)}
                      className="text-[11px] px-2.5 py-1 rounded-full border border-gray-200 text-gray-500 hover:border-violet-300 hover:text-violet-600 transition"
                    >
                      {suggestion}
                    </button>
                  ))}
                  <button
                    onClick={() => setInitInput("Focus only on token ")}
                    className="text-[11px] px-2.5 py-1 rounded-full border border-gray-200 text-gray-500 hover:border-violet-300 hover:text-violet-600 transition"
                  >
                    Focus on token ID or name…
                  </button>
                </div>
              </div>
            )}

            {/* Analysis steps feed */}
            {started && (
              <div className="bg-white border-b border-gray-100 p-3" ref={stepsRef}>
                <div className="max-h-64 overflow-y-auto space-y-1">
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
                </div>
              </div>
            )}

            {/* Streaming agent text */}
            {started && !done && (streamingText || streamingActive) && (
              <div className="px-4 py-3 border-b border-gray-100">
                <StreamingText text={streamingText} active={streamingActive} />
              </div>
            )}

            {/* HITL clarification */}
            {clarification && (
              <div className="px-4 py-3 border-b border-gray-100">
                <ClarificationBox
                  question={clarification.question}
                  context={clarification.context}
                  sessionId={sessionId}
                  onAnswered={(answer) => {
                    setClarification(null);
                    setChatMessages((prev) => [...prev, { role: "user", text: answer }]);
                  }}
                />
              </div>
            )}

            {/* Error */}
            {error && (
              <div className="px-4 py-3 bg-red-50 border-t border-red-200 text-sm text-red-700">
                <span className="font-semibold">Error:</span> {error}
              </div>
            )}

            {/* Results */}
            {done && (
              <div className="bg-white p-4 space-y-4">
                {done.token_scores.length > 0 ? (
                  <>
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
                  </>
                ) : (
                  <div className="px-3 py-3 rounded-lg bg-violet-50 border border-violet-100">
                    <div className="text-[10px] uppercase tracking-widest text-violet-400 font-semibold mb-1.5">
                      Analysis Complete
                    </div>
                    <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap">
                      <BoldText text={done.store_summary || "Analysis complete."} />
                    </p>
                  </div>
                )}
                <p className="text-[10px] text-gray-400 text-center">
                  {done.iterations} turn{done.iterations !== 1 ? "s" : ""} · {done.approved ? "all verified" : "partial verification"}
                </p>
              </div>
            )}

            {/* Follow-up chat */}
            {done && sessionId && (
              <div className="border-t border-gray-100 bg-white">
                {(chatMessages.length > 0 || chatStreamText) && (
                  <div className="px-4 pt-3 space-y-2 max-h-48 overflow-y-auto">
                    {chatMessages.map((m, i) => (
                      <ChatBubble key={i} role={m.role} text={m.text} />
                    ))}
                    {chatStreamText && (
                      <StreamingText text={chatStreamText} active={chatRunning} />
                    )}
                  </div>
                )}
                <div className="px-4 py-3 flex gap-2 items-center">
                  <input
                    type="text"
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={handleChatKey}
                    placeholder="Ask a follow-up question…"
                    disabled={chatRunning}
                    className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-lg text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-violet-400 disabled:opacity-50"
                  />
                  <button
                    onClick={sendChat}
                    disabled={!chatInput.trim() || chatRunning}
                    className="px-3 py-2 bg-violet-600 text-white text-xs font-semibold rounded-lg hover:bg-violet-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
                  >
                    {chatRunning ? "…" : "Send"}
                  </button>
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </>
        )}
      </div>
    </>
  );
}
