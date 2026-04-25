import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000/api/v1";

// ─── API helpers ───────────────────────────────────────────────────────────
const api = {
  get: (path) => fetch(`${API_BASE}${path}`).then((r) => r.json()),
  post: (path, body, idempotencyKey) =>
    fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(body),
    }),
};

function generateUUID() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function paise2inr(paise) {
  return (paise / 100).toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
  });
}

function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ─── Status badge ──────────────────────────────────────────────────────────
const STATUS_STYLES = {
  pending:    "bg-amber-500/20 text-amber-300 border border-amber-500/30",
  processing: "bg-blue-500/20 text-blue-300 border border-blue-500/30",
  completed:  "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30",
  failed:     "bg-red-500/20 text-red-300 border border-red-500/30",
};

function StatusBadge({ status }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-mono font-semibold uppercase tracking-wider ${STATUS_STYLES[status] || "bg-gray-500/20 text-gray-400"}`}>
      {status === "processing" && (
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-blue-400 mr-1.5 animate-pulse" />
      )}
      {status}
    </span>
  );
}

// ─── Payout form ───────────────────────────────────────────────────────────
function PayoutForm({ merchant, onSuccess }) {
  const [amountInr, setAmountInr] = useState("");
  const [bankAccountId, setBankAccountId] = useState(
    merchant.bank_accounts?.[0]?.id || ""
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [lastKey, setLastKey] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    const paise = Math.round(parseFloat(amountInr) * 100);
    if (!paise || paise < 100) {
      setError("Minimum payout is ₹1.00");
      return;
    }
    const available = merchant.balance.available_paise;
    if (paise > available) {
      setError(`Insufficient funds. Available: ${paise2inr(available)}`);
      return;
    }

    setLoading(true);
    const key = generateUUID();
    setLastKey(key);

    try {
      const res = await api.post(
        `/merchants/${merchant.id}/payouts/create/`,
        { amount_paise: paise, bank_account_id: bankAccountId },
        key
      );
      const data = await res.json();
      if (res.ok) {
        setAmountInr("");
        onSuccess(data);
      } else {
        setError(data.error || "Failed to create payout.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  const available = merchant.balance?.available_paise || 0;

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="block text-xs font-semibold text-gray-400 uppercase tracking-wider mb-1.5">
          Amount (INR)
        </label>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 font-bold">₹</span>
          <input
            type="number"
            step="0.01"
            min="1"
            value={amountInr}
            onChange={(e) => setAmountInr(e.target.value)}
            placeholder="0.00"
            className="w-full bg-gray-900 border border-gray-700 rounded-lg pl-7 pr-4 py-3 text-white text-lg font-mono focus:outline-none focus:border-indigo-500 transition-colors"
          />
        </div>
        <p className="text-xs text-gray-500 mt-1">
          Available: <span className="text-emerald-400 font-mono">{paise2inr(available)}</span>
        </p>
      </div>

      {merchant.bank_accounts?.length > 1 && (
        <div>
          <label className="block text-xs font-semibold text-gray-400 uppercase tracking-wider mb-1.5">
            Bank Account
          </label>
          <select
            value={bankAccountId}
            onChange={(e) => setBankAccountId(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:border-indigo-500"
          >
            {merchant.bank_accounts.map((acc) => (
              <option key={acc.id} value={acc.id}>
                {acc.account_holder_name} — {acc.account_number_masked}
              </option>
            ))}
          </select>
        </div>
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2 text-red-400 text-sm">
          {error}
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !amountInr}
        className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-semibold py-3 rounded-lg transition-colors flex items-center justify-center gap-2"
      >
        {loading ? (
          <>
            <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            Processing…
          </>
        ) : (
          "Request Payout →"
        )}
      </button>
    </form>
  );
}

// ─── Payout row ────────────────────────────────────────────────────────────
function PayoutRow({ payout, onRefresh }) {
  const [refreshing, setRefreshing] = useState(false);

  // Auto-poll for non-terminal payouts
  useEffect(() => {
    if (payout.status === "completed" || payout.status === "failed") return;
    const interval = setInterval(async () => {
      await onRefresh(payout.id);
    }, 3000);
    return () => clearInterval(interval);
  }, [payout.id, payout.status]);

  return (
    <tr className="border-t border-gray-800 hover:bg-gray-800/30 transition-colors">
      <td className="px-4 py-3">
        <span className="font-mono text-xs text-gray-500">{payout.id.slice(0, 8)}…</span>
      </td>
      <td className="px-4 py-3 text-right font-mono text-white font-semibold">
        {paise2inr(payout.amount_paise)}
      </td>
      <td className="px-4 py-3">
        <StatusBadge status={payout.status} />
      </td>
      <td className="px-4 py-3 text-xs text-gray-500">
        {timeAgo(payout.created_at)}
      </td>
      {payout.failure_reason && (
        <td className="px-4 py-3 text-xs text-red-400 max-w-xs truncate">
          {payout.failure_reason}
        </td>
      )}
    </tr>
  );
}

// ─── Ledger row ────────────────────────────────────────────────────────────
function LedgerRow({ entry }) {
  const isCredit = entry.entry_type === "credit";
  return (
    <tr className="border-t border-gray-800 hover:bg-gray-800/20 transition-colors">
      <td className="px-4 py-2.5 text-xs text-gray-400">{timeAgo(entry.created_at)}</td>
      <td className="px-4 py-2.5 text-xs text-gray-300 max-w-xs truncate">{entry.description}</td>
      <td className={`px-4 py-2.5 text-right font-mono text-sm font-semibold ${isCredit ? "text-emerald-400" : "text-red-400"}`}>
        {isCredit ? "+" : "−"}{paise2inr(entry.amount_paise)}
      </td>
    </tr>
  );
}

// ─── Main App ──────────────────────────────────────────────────────────────
export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [merchant, setMerchant] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState("payouts");

  useEffect(() => {
    api.get("/merchants/").then((data) => {
      setMerchants(data);
      if (data.length > 0) setSelectedId(data[0].id);
    });
  }, []);

  const fetchMerchant = useCallback(async (id) => {
    if (!id) return;
    const [m, p, l] = await Promise.all([
      api.get(`/merchants/${id}/`),
      api.get(`/merchants/${id}/payouts/`),
      api.get(`/merchants/${id}/ledger/`),
    ]);
    setMerchant(m);
    setPayouts(p);
    setLedger(l);
  }, []);

  useEffect(() => {
    if (selectedId) {
      setLoading(true);
      fetchMerchant(selectedId).finally(() => setLoading(false));
    }
  }, [selectedId]);

  // Auto-refresh every 5s for live status
  useEffect(() => {
    if (!selectedId) return;
    const interval = setInterval(() => fetchMerchant(selectedId), 5000);
    return () => clearInterval(interval);
  }, [selectedId, fetchMerchant]);

  async function handlePayoutRefresh(payoutId) {
    try {
      const updated = await api.get(`/merchants/${selectedId}/payouts/${payoutId}/`);
      setPayouts((prev) => prev.map((p) => (p.id === payoutId ? updated : p)));
      // Also refresh merchant balance
      const m = await api.get(`/merchants/${selectedId}/`);
      setMerchant(m);
    } catch {}
  }

  function handlePayoutCreated(newPayout) {
    setPayouts((prev) => [newPayout, ...prev]);
    fetchMerchant(selectedId);
    setTab("payouts");
  }

  const balance = merchant?.balance;

  return (
    <div className="min-h-screen bg-gray-950 text-white font-sans">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/50 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-md bg-indigo-600 flex items-center justify-center">
              <span className="text-xs font-black">P</span>
            </div>
            <span className="font-bold text-sm tracking-tight">Playto Pay</span>
            <span className="text-gray-600 text-sm">/ Payout Engine</span>
          </div>
          <select
            value={selectedId || ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-indigo-500"
          >
            {merchants.map((m) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        {loading && !merchant ? (
          <div className="flex items-center justify-center h-64 text-gray-500">
            <span className="w-6 h-6 border-2 border-gray-600 border-t-indigo-500 rounded-full animate-spin mr-3" />
            Loading…
          </div>
        ) : merchant ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Left: Balance + Form */}
            <div className="space-y-4">
              {/* Balance card */}
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">
                  Available Balance
                </p>
                <p className="text-3xl font-black font-mono text-white">
                  {paise2inr(balance?.available_paise || 0)}
                </p>
                {balance?.held_paise > 0 && (
                  <p className="text-xs text-amber-400 mt-1 font-mono">
                    {paise2inr(balance.held_paise)} held in payouts
                  </p>
                )}
                <div className="mt-4 pt-4 border-t border-gray-800">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>Total credited</span>
                    <span className="text-emerald-400 font-mono">
                      {paise2inr(balance?.total_credited_paise || 0)}
                    </span>
                  </div>
                </div>
              </div>

              {/* Payout form */}
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <h2 className="font-bold text-sm mb-4">Request Payout</h2>
                <PayoutForm
                  merchant={merchant}
                  onSuccess={handlePayoutCreated}
                />
              </div>

              {/* Bank accounts */}
              {merchant.bank_accounts?.length > 0 && (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">
                    Bank Accounts
                  </h3>
                  {merchant.bank_accounts.map((acc) => (
                    <div key={acc.id} className="flex items-center gap-2 text-sm">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                      <span className="text-gray-300">{acc.account_number_masked}</span>
                      <span className="text-gray-600 text-xs">{acc.ifsc_code}</span>
                      {acc.is_primary && (
                        <span className="text-xs text-indigo-400 ml-auto">Primary</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Right: History */}
            <div className="lg:col-span-2">
              {/* Tabs */}
              <div className="flex gap-1 mb-4 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
                {["payouts", "ledger"].map((t) => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={`px-4 py-1.5 rounded-md text-sm font-semibold transition-colors capitalize ${
                      tab === t
                        ? "bg-indigo-600 text-white"
                        : "text-gray-400 hover:text-white"
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>

              <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
                {tab === "payouts" ? (
                  payouts.length === 0 ? (
                    <div className="py-16 text-center text-gray-600 text-sm">
                      No payouts yet. Request your first payout →
                    </div>
                  ) : (
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-xs text-gray-500 uppercase tracking-wider">
                          <th className="px-4 py-3 text-left font-semibold">ID</th>
                          <th className="px-4 py-3 text-right font-semibold">Amount</th>
                          <th className="px-4 py-3 text-left font-semibold">Status</th>
                          <th className="px-4 py-3 text-left font-semibold">When</th>
                        </tr>
                      </thead>
                      <tbody>
                        {payouts.map((p) => (
                          <PayoutRow
                            key={p.id}
                            payout={p}
                            onRefresh={handlePayoutRefresh}
                          />
                        ))}
                      </tbody>
                    </table>
                  )
                ) : (
                  ledger.length === 0 ? (
                    <div className="py-16 text-center text-gray-600 text-sm">
                      No ledger entries yet.
                    </div>
                  ) : (
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-xs text-gray-500 uppercase tracking-wider">
                          <th className="px-4 py-3 text-left font-semibold">When</th>
                          <th className="px-4 py-3 text-left font-semibold">Description</th>
                          <th className="px-4 py-3 text-right font-semibold">Amount</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ledger.map((e) => (
                          <LedgerRow key={e.id} entry={e} />
                        ))}
                      </tbody>
                    </table>
                  )
                )}
              </div>
            </div>
          </div>
        ) : null}
      </main>
    </div>
  );
}
