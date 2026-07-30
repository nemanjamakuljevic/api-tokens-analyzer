import { useState, KeyboardEvent } from "react";

interface ClarificationBoxProps {
  question: string;
  context?: string;
  sessionId: string | null;
  onAnswered: (answer: string) => void;
}

export default function ClarificationBox({ question, context, sessionId, onAnswered }: ClarificationBoxProps) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  async function submit() {
    if (!answer.trim() || submitting || !sessionId) return;
    setSubmitting(true);
    try {
      await fetch(`/api/chat/${sessionId}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: answer.trim() }),
      });
      setSubmitted(true);
      onAnswered(answer.trim());
    } catch {
      setSubmitting(false);
    }
  }

  function handleKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  if (submitted) {
    return (
      <div className="px-3 py-2.5 rounded-lg bg-green-50 border border-green-200 text-xs text-green-800">
        <span className="font-semibold">You:</span> {answer}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 overflow-hidden">
      <div className="px-3 py-2 border-b border-amber-200">
        <div className="flex items-center gap-1.5 mb-0.5">
          <span className="text-amber-600 text-sm">?</span>
          <span className="text-xs font-semibold text-amber-800">Agent needs your input</span>
        </div>
        <p className="text-sm text-amber-900 font-medium">{question}</p>
        {context && (
          <p className="mt-0.5 text-[11px] text-amber-700 italic">{context}</p>
        )}
      </div>
      <div className="px-3 py-2 flex gap-2">
        <input
          type="text"
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={handleKey}
          placeholder="Type your answer…"
          disabled={submitting || !sessionId}
          autoFocus
          className="flex-1 px-3 py-1.5 text-sm border border-amber-300 rounded-lg bg-white text-gray-800 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-amber-400 disabled:opacity-50"
        />
        <button
          onClick={submit}
          disabled={!answer.trim() || submitting || !sessionId}
          className="px-3 py-1.5 text-xs font-semibold bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          {submitting ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
