import { useState, useEffect } from "react";
import "./App.css";

// Backend URL. Set VITE_API_URL in .env when the FastAPI backend is deployed.
let API = "http://localhost:8000";
try {
  API = import.meta.env?.VITE_API_URL || API;
} catch {
  /* preview environments without import.meta.env */
}

// ---------- Offline fallback (used only when the backend is not reachable) ----------
const LOCAL_KB = [
  { title: "Reset your password", snippet: "Open the self-service password portal, click 'Forgot password' and verify with OTP." },
  { title: "Fix VPN not connecting", snippet: "Check internet, restart the VPN client, re-login with your SSO account, then retry." },
  { title: "Outlook not syncing", snippet: "Restart Outlook, remove and re-add the account, then clear the OST cache." },
  { title: "Request new software", snippet: "Use the Software Request tab. Standard tools are approved automatically." },
];

function localClassify(text) {
  const t = text.toLowerCase();
  const has = (...w) => w.some((x) => t.includes(x));
  if (has("vpn")) return { intent: "Incident", category: "Network / VPN", priority: "P2", team: "Network Support", self_heal: true };
  if (has("password")) return { intent: "Automatable Issue", category: "Account / Password", priority: "P2", team: "Service Desk", self_heal: true };
  if (has("outlook", "email", "mail")) return { intent: "Knowledge Question", category: "Email / Outlook", priority: "P3", team: "Service Desk", self_heal: false };
  if (has("vscode", "visual studio", "software", "install")) return { intent: "Service Request", category: "Software Installation", priority: "P3", team: "Software Support", self_heal: false };
  if (has("computer", "laptop", "system")) return { intent: "Incident", category: "Hardware / Device", priority: "P2", team: "IT Support", self_heal: false };
  return { intent: "Escalation", category: "Unknown", priority: "P3", team: "Helpdesk", self_heal: false };
}

const localKbSearch = (q) => {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return LOCAL_KB.filter((a) => words.some((w) => (a.title + a.snippet).toLowerCase().includes(w)));
};

let localCounter = 1000;
const localRef = (p) => `${p}${String(++localCounter).padStart(6, "0")}`;

// ---------- API helper ----------
async function call(path, options, fallback) {
  try {
    const res = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return { data: await res.json(), offline: false };
  } catch {
    return { data: fallback(), offline: true };
  }
}

// ---------- Styles ----------
const card = { background: "white", padding: 25, borderRadius: 15, boxShadow: "0 4px 15px rgba(0,0,0,0.08)", marginBottom: 25 };
const primaryBtn = { padding: "12px 20px", border: "none", borderRadius: 8, background: "#2563eb", color: "white", fontSize: 16, cursor: "pointer" };
const ghostBtn = { padding: "12px 20px", border: "1px solid #ccc", borderRadius: 8, background: "white", color: "#333", fontSize: 16, cursor: "pointer" };
const input = { width: "100%", padding: 14, marginTop: 10, borderRadius: 10, border: "1px solid #ccc", fontSize: 16, boxSizing: "border-box" };

const TABS = [
  ["intake", "Ticket intake"],
  ["chat", "Help assistant"],
  ["kb", "Knowledge base"],
  ["software", "Software requests"],
  ["tickets", "My tickets"],
];

function Field({ label, value, bg }) {
  return (
    <div style={{ padding: 18, background: bg, borderRadius: 10 }}>
      <small>{label}</small>
      <h3 style={{ margin: "6px 0 0" }}>{value}</h3>
    </div>
  );
}

// ---------- 1 + 2. Ticket intake and auto resolution ----------
function Intake({ onTicket, setOffline }) {
  const [problem, setProblem] = useState("");
  const [result, setResult] = useState(null);
  const [heal, setHeal] = useState("");
  const [busy, setBusy] = useState(false);

  async function analyze() {
    if (!problem.trim()) {
      setResult({ error: "Please describe your problem first." });
      return;
    }
    setBusy(true);
    setHeal("");
    const { data, offline } = await call(
      "/api/tickets/analyze",
      { method: "POST", body: JSON.stringify({ text: problem }) },
      () => ({ ...localClassify(problem), reference: localRef("INC"), description: problem })
    );
    setOffline(offline);
    setResult(data);
    onTicket({ ...data, description: problem });
    setBusy(false);
  }

  async function selfHeal() {
    setBusy(true);
    const { data, offline } = await call(
      "/api/self-heal",
      { method: "POST", body: JSON.stringify({ category: result.category, reference: result.reference }) },
      () => ({ message: `Self-heal agent ran for "${result.category}". Please retry now. (demo mode)` })
    );
    setOffline(offline);
    setHeal(data.message);
    setBusy(false);
  }

  const examples = ["My VPN is not connecting", "I forgot my password", "My Outlook email is not working", "I need Visual Studio software"];

  return (
    <section className="panel" style={card}>
      <h2>🎫 Intelligent Ticket Intake</h2>
      <p style={{ color: "#666" }}>Describe your IT problem. AI will classify it and create a ticket.</p>
      <textarea
        value={problem}
        onChange={(e) => setProblem(e.target.value)}
        rows="5"
        placeholder="Example: My VPN is not connecting..."
        style={{ ...input, resize: "vertical" }}
      />
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 15 }}>
        <button type="button" style={primaryBtn} onClick={analyze} disabled={busy}>
          {busy ? "Analyzing..." : "🔍 Analyze Request"}
        </button>
        <button type="button" style={ghostBtn} onClick={() => { setProblem(""); setResult(null); setHeal(""); }}>
          🗑️ Clear
        </button>
      </div>
      <p style={{ fontWeight: "bold", marginTop: 20 }}>Quick Examples:</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {examples.map((e) => (
          <button key={e} type="button" onClick={() => setProblem(e)}>{e}</button>
        ))}
      </div>

      {result?.error && <p style={{ color: "#b91c1c", marginTop: 20 }}>{result.error}</p>}

      {result && !result.error && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 15, marginTop: 25 }}>
            <Field label="Intent" value={result.intent} bg="#eff6ff" />
            <Field label="Category" value={result.category} bg="#f0fdf4" />
            <Field label="Priority" value={result.priority} bg="#fff7ed" />
            <Field label="Assigned Team" value={result.team} bg="#faf5ff" />
            <Field label="Reference" value={result.reference} bg="#f8fafc" />
          </div>
          {result.self_heal && (
            <div style={{ marginTop: 20, padding: 18, background: "#ecfdf5", borderRadius: 10 }}>
              <b>🛠️ Auto Resolution available</b>
              <p style={{ margin: "6px 0 12px" }}>The self-heal agent can try to fix this now.</p>
              <button type="button" style={primaryBtn} onClick={selfHeal} disabled={busy}>Run self-heal</button>
              {heal && <p style={{ marginTop: 12 }}>✅ {heal}</p>}
            </div>
          )}
        </>
      )}
    </section>
  );
}

// Polls the backend while a software install runs; calls onUpdate({state, message, steps}).
function watchInstall(ref, onUpdate) {
  let stopped = false;
  const tick = async (n) => {
    if (stopped || n > 400) return;
    try {
      const res = await fetch(`${API}/api/software/status/${ref}`);
      const st = await res.json();
      onUpdate(st);
      if (st.state === "installed" || st.state === "failed" || st.state === "rejected") return;
    } catch {
      /* backend busy, try again */
    }
    setTimeout(() => tick(n + 1), 3000);
  };
  tick(0);
  return () => {
    stopped = true;
  };
}

// ---------- 4. Chatbot ----------
function Chatbot({ setOffline }) {
  const [msgs, setMsgs] = useState([{ role: "bot", text: "Hi! Ask me any IT question, like VPN, password or Outlook." }]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  // password flow: { ref, step: "wait" | "email" | "otp" | "newpass" }
  const [flow, setFlow] = useState(null);

  const say = (t) => setMsgs((m) => [...m, { role: "bot", text: t }]);

  // while waiting for the manager, check the approval every 3 seconds
  useEffect(() => {
    if (!flow || flow.step !== "wait") return;
    const id = setInterval(async () => {
      try {
        const res = await fetch(`${API}/api/password/status/${flow.ref}`);
        const st = (await res.json()).state;
        if (st === "approved") {
          say("Your manager approved the request. Please type your Gmail address to get an OTP.");
          setFlow({ ref: flow.ref, step: "email" });
        } else if (st === "rejected") {
          say("Your manager rejected the password change request. The ticket is closed.");
          setFlow(null);
        }
      } catch {
        /* backend busy */
      }
    }, 3000);
    return () => clearInterval(id);
  }, [flow]);

  async function flowStep(q) {
    const post = async (path, body) => {
      const res = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      return res.json();
    };
    if (flow.step === "email") {
      const r = await post("/api/password/email", { reference: flow.ref, email: q });
      say(r.message);
      if (r.ok) setFlow({ ref: flow.ref, step: "otp" });
    } else if (flow.step === "otp") {
      const r = await post("/api/password/otp", { reference: flow.ref, otp: q });
      say(r.message);
      if (r.ok) setFlow({ ref: flow.ref, step: "newpass" });
      else if (r.restart) setFlow({ ref: flow.ref, step: "email" });
    } else if (flow.step === "newpass") {
      const r = await post("/api/password/reset", { reference: flow.ref, password: q });
      say(r.message);
      if (r.ok) setFlow(null);
    }
  }

  async function send() {
    const q = text.trim();
    if (!q) return;
    setText("");
    const shown = flow?.step === "newpass" ? "••••••••" : q;
    setMsgs((m) => [...m, { role: "user", text: shown }]);
    setBusy(true);
    if (flow && flow.step !== "wait") {
      try {
        await flowStep(q);
      } catch {
        say("Could not reach the backend. Please try again.");
      }
      setBusy(false);
      return;
    }
    const { data, offline } = await call(
      "/api/chat",
      { method: "POST", body: JSON.stringify({ message: q }) },
      () => {
        const hit = localKbSearch(q)[0];
        return { reply: hit ? hit.snippet : "I could not find an answer. Please raise a ticket in Ticket Intake.", sources: hit ? [hit.title] : [] };
      }
    );
    setOffline(offline);
    setMsgs((m) => [...m, { role: "bot", text: data.reply, sources: data.sources }]);
    if (data.flow === "waiting_approval") setFlow({ ref: data.reference, step: "wait" });
    if (data.install_started) {
      watchInstall(data.reference, (st) => {
        if (st.state === "installed" || st.state === "failed" || st.state === "rejected") {
          setMsgs((m) => [...m, { role: "bot", text: `Install update for ${data.reference}: ${st.message}` }]);
        }
      });
    }
    setBusy(false);
  }

  return (
    <section className="panel" style={card}>
      <h2>💬 AI Chatbot</h2>
      <div className="chat-window" style={{ minHeight: 200, maxHeight: 360, overflowY: "auto", marginTop: 10 }}>
        {msgs.map((m, i) => (
          <div className="chat-line" key={i} style={{ textAlign: m.role === "user" ? "right" : "left", margin: "8px 0" }}>
            <span className={m.role === "user" ? "chat-bubble-user" : "chat-bubble-bot"} style={{ display: "inline-block", padding: "10px 14px", borderRadius: 12, maxWidth: "85%", background: m.role === "user" ? "#2563eb" : "#f1f5f9", color: m.role === "user" ? "white" : "#111" }}>
              {m.text}
            </span>
            {m.sources?.length > 0 && <div style={{ fontSize: 12, color: "#666", marginTop: 3 }}>Source: {m.sources.join(", ")}</div>}
          </div>
        ))}
        {busy && <p style={{ color: "#666" }}>Thinking...</p>}
      </div>
      <div className="chat-compose" style={{ display: "flex", gap: 8 }}>
        <input style={input} type={flow?.step === "newpass" ? "password" : "text"} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} placeholder={flow?.step === "wait" ? "Waiting for manager approval..." : flow?.step === "email" ? "Type your Gmail..." : flow?.step === "otp" ? "Type the OTP..." : flow?.step === "newpass" ? "Type your new password..." : "Type your question..."} />
        <button type="button" style={{ ...primaryBtn, marginTop: 10 }} onClick={send}>Send</button>
      </div>
    </section>
  );
}

// ---------- 3. Knowledge base (RAG search) ----------
function KnowledgeBase({ setOffline }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState(null);

  async function search() {
    if (!q.trim()) return;
    const { data, offline } = await call(
      "/api/kb/search",
      { method: "POST", body: JSON.stringify({ query: q }) },
      () => localKbSearch(q)
    );
    setOffline(offline);
    setItems(data);
  }

  return (
    <section className="panel" style={card}>
      <h2>📚 Knowledge Base</h2>
      <p style={{ color: "#666" }}>Search articles using AI semantic search.</p>
      <div style={{ display: "flex", gap: 8 }}>
        <input style={input} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="Search: vpn, password, outlook..." />
        <button type="button" style={{ ...primaryBtn, marginTop: 10 }} onClick={search}>Search</button>
      </div>
      {items && items.length === 0 && <p>No articles found. Try a different word.</p>}
      {items?.map((a, i) => (
        <div key={i} style={{ padding: 16, background: "#f8fafc", borderRadius: 10, marginTop: 12 }}>
          <b>{a.title}</b>
          <p style={{ margin: "6px 0 0" }}>{a.snippet}</p>
        </div>
      ))}
    </section>
  );
}

// ---------- 5. Software provisioning ----------
function Software({ onTicket, setOffline }) {
  const [name, setName] = useState("Visual Studio Code");
  const [reason, setReason] = useState("");
  const [done, setDone] = useState(null);
  const [live, setLive] = useState(null);
  const options = ["Visual Studio Code", "Visual Studio", "Python", "Git", "GitHub Desktop", "Docker Desktop", "Postman", "Node.js", "Java JDK", "Google Chrome", "Mozilla Firefox", "7-Zip", "Notepad++", "Slack", "Zoom", "VLC Media Player"];

  async function submit() {
    const { data, offline } = await call(
      "/api/software/request",
      { method: "POST", body: JSON.stringify({ software: name, justification: reason }) },
      () => ({ reference: localRef("REQ"), status: "Approved", message: `${name} will be installed automatically. (demo mode)` })
    );
    setOffline(offline);
    setDone(data);
    setLive(null);
    if (data.install_started) watchInstall(data.reference, setLive);
    onTicket({ intent: "Service Request", category: `Software: ${name}`, priority: "P3", team: "Software Support", reference: data.reference, description: reason || name });
  }

  return (
    <section className="panel" style={card}>
      <h2>💻 Software Provisioning</h2>
      <select style={input} value={name} onChange={(e) => setName(e.target.value)}>
        {options.map((o) => <option key={o}>{o}</option>)}
      </select>
      <textarea style={{ ...input, resize: "vertical" }} rows="3" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why do you need it?" />
      <button type="button" style={{ ...primaryBtn, marginTop: 15 }} onClick={submit}>Submit request</button>
      {done && (
        <div style={{ marginTop: 18, padding: 16, background: "#ecfdf5", borderRadius: 10 }}>
          <b>{done.reference}</b>: {done.status}
          <p style={{ margin: "6px 0 0" }}>{done.message}</p>
          {live && (
            <p style={{ margin: "6px 0 0" }}>
              Install status: <b>{live.state}</b>. {live.message}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

// ---------- Tickets list ----------
function Tickets({ local, setOffline }) {
  const [list, setList] = useState(local);

  useEffect(() => {
    call("/api/tickets", {}, () => local).then(({ data, offline }) => {
      setOffline(offline);
      setList(data);
    });
  }, []); // eslint-disable-line

  return (
    <section className="panel" style={card}>
      <h2>📋 My Tickets</h2>
      {list.length === 0 && <p>No tickets yet. Create one in Ticket Intake.</p>}
      {list.map((t, i) => (
        <div key={i} style={{ padding: 14, background: "#f8fafc", borderRadius: 10, marginTop: 10 }}>
          <b>{t.reference}</b> · {t.category} · {t.priority}
          <div style={{ color: "#666", fontSize: 14 }}>{t.team}{t.description ? `: ${t.description}` : ""}</div>
        </div>
      ))}
    </section>
  );
}

// ---------- App ----------
export default function App() {
  const [tab, setTab] = useState("intake");
  const [tickets, setTickets] = useState([]);
  const [offline, setOffline] = useState(false);
  const addTicket = (t) => setTickets((p) => [t, ...p]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-inner">
          <div className="brand"><div className="brand-mark" aria-hidden="true">IT</div><div><h1>AI IT Helpdesk</h1><p>Employee support, made simple</p></div></div>
          <div className={`connection${offline ? " offline" : ""}`}><span className="connection-dot" />{offline ? "Demo mode · backend offline" : "Helpdesk system online"}</div>
        </div>
      </header>

      <main className="workspace">
        <div className="page-intro"><h2>How can we help?</h2><p>Find an answer, submit an IT issue, or manage your requests.</p></div>

      <nav className="tab-nav" aria-label="Helpdesk sections">
        {TABS.map(([id, label]) => (
          <button key={id} type="button" aria-current={tab === id ? "page" : undefined} onClick={() => setTab(id)}
            style={{ ...ghostBtn, whiteSpace: "nowrap", padding: "10px 16px", background: tab === id ? "#2563eb" : "white", color: tab === id ? "white" : "#333" }}>
            {label}
          </button>
        ))}
      </nav>

        {tab === "intake" && <Intake onTicket={addTicket} setOffline={setOffline} />}
        {tab === "chat" && <Chatbot setOffline={setOffline} />}
        {tab === "kb" && <KnowledgeBase setOffline={setOffline} />}
        {tab === "software" && <Software onTicket={addTicket} setOffline={setOffline} />}
        {tab === "tickets" && <Tickets local={tickets} setOffline={setOffline} />}
      </main>

      <footer className="app-footer">
        <p style={{ margin: 0 }}>AI IT Helpdesk © 2026 | Intelligent IT Automation</p>
      </footer>
    </div>
  );
}
