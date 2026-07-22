const ITEMS = [
  {
    dot: "bg-amber-400",
    label: "Token Rotation",
    desc: "Active token needing credential refresh — high traffic, stalled migration, or load imbalance.",
  },
  {
    dot: "bg-blue-400",
    label: "Token Cleanup",
    desc: "Definitively idle over the full observation window — safe to revoke.",
  },
  {
    dot: "bg-red-400",
    label: "Security Audit",
    desc: "Unusual pattern, very old credentials, or unexplained endpoint activity.",
  },
  {
    dot: "bg-orange-400",
    label: "Insufficient Data",
    desc: "Observation window too short — retry Splunk query with ≥30 days to confirm.",
  },
  {
    dot: "bg-gray-400",
    label: "No Action",
    desc: "Token health acceptable — no immediate action required.",
  },
];

export default function Legend() {
  return (
    <div className="w-full bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      <div className="flex divide-x divide-gray-100">
        {ITEMS.map((item) => (
          <div key={item.label} className="flex-1 flex items-center gap-2.5 px-4 py-2.5">
            <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${item.dot}`} />
            <div className="min-w-0">
              <span className="text-xs font-semibold text-gray-700 whitespace-nowrap">{item.label}</span>
              <span className="text-[11px] text-gray-400 ml-1.5 hidden xl:inline">{item.desc}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
