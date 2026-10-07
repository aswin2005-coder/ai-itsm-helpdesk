import json
import os
import re
import uuid
from datetime import datetime, timezone

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_CHAT_MODEL = os.getenv("HF_CHAT_MODEL", "meta-llama/Llama-3.2-3B-Instruct")
HF_EMBED_MODEL = os.getenv("HF_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
SN_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "")
SN_USER = os.getenv("SERVICENOW_USER", "")
SN_PASS = os.getenv("SERVICENOW_PASSWORD", "")

app = FastAPI(title="AI ITSM Helpdesk")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- MongoDB Atlas (falls back to memory) ----------------
tickets_col = None
kb_col = None
MEM_TICKETS = []

if MONGODB_URI:
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client["itsm_helpdesk"]
        tickets_col = db["tickets"]
        kb_col = db["kb_articles"]
        print("MongoDB Atlas connected")
    except Exception as e:
        print("MongoDB not available, using memory:", e)


def now():
    return datetime.now(timezone.utc).isoformat()


def local_ref(prefix):
    return f"{prefix}{str(uuid.uuid4().int)[:7]}"


def save_ticket(t):
    if tickets_col is not None:
        tickets_col.insert_one(dict(t))
    else:
        MEM_TICKETS.append(dict(t))


def list_tickets():
    if tickets_col is not None:
        return list(tickets_col.find({}, {"_id": 0}).sort("created_at", -1).limit(50))
    return list(reversed(MEM_TICKETS))


def find_ticket(ref):
    if tickets_col is not None:
        return tickets_col.find_one({"reference": ref}, {"_id": 0})
    return next((t for t in MEM_TICKETS if t["reference"] == ref), None)


def update_ticket(ref, fields):
    if tickets_col is not None:
        tickets_col.update_one({"reference": ref}, {"$set": fields})
    else:
        for t in MEM_TICKETS:
            if t["reference"] == ref:
                t.update(fields)


# ---------------- Hugging Face LLM ----------------
def llm(messages, max_tokens=300):
    if not HF_TOKEN:
        return None
    try:
        from huggingface_hub import InferenceClient

        r = InferenceClient(token=HF_TOKEN).chat_completion(
            messages=messages, model=HF_CHAT_MODEL, max_tokens=max_tokens, temperature=0.2
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print("LLM error:", e)
        return None


# ---------------- Knowledge base + vector index (RAG) ----------------
SEED_KB = [
    {"title": "Reset your password", "content": "Open the self-service password portal, click Forgot password, verify with the OTP sent to your phone, then set a new password."},
    {"title": "Fix VPN not connecting", "content": "Check your internet, close and restart the VPN client, sign in again with your SSO account, then reconnect. If it still fails, raise a ticket."},
    {"title": "Outlook not syncing", "content": "Restart Outlook, remove and re-add your email account, then clear the OST cache and restart again."},
    {"title": "Request new software", "content": "Use the Software tab to request tools. Standard tools like VS Code, Git, Python and Postman are approved automatically. Others need manager approval."},
    {"title": "Wi-Fi keeps disconnecting", "content": "Forget the office Wi-Fi network, reconnect with your company login, and update the wireless driver from Device Manager."},
    {"title": "Printer not working", "content": "Check the printer is online, remove the printer from Settings, add it again using the printer name shown on the device, and print a test page."},
    {"title": "Laptop running slow", "content": "Restart your laptop, close unused apps, check Task Manager for high CPU usage, and run Windows Update. Raise a ticket if it stays slow."},
    {"title": "Set up multi-factor authentication", "content": "Install the authenticator app, scan the QR code from the security portal, and save your backup codes in a safe place."},
]


def load_kb():
    if kb_col is not None:
        if kb_col.count_documents({}) == 0:
            kb_col.insert_many([dict(d) for d in SEED_KB])
        return list(kb_col.find({}, {"_id": 0}))
    return [dict(d) for d in SEED_KB]


class VectorIndex:
    def __init__(self):
        self.docs = []
        self.mode = "tfidf"
        self.hf_matrix = None
        self.tfidf = None
        self.tfidf_matrix = None

    @staticmethod
    def _hf_embed(texts):
        from huggingface_hub import InferenceClient

        client = InferenceClient(token=HF_TOKEN)
        out = []
        for t in texts:
            v = np.array(client.feature_extraction(t, model=HF_EMBED_MODEL), dtype=float)
            v = v.reshape(-1, v.shape[-1]).mean(axis=0)
            out.append(v / (np.linalg.norm(v) + 1e-9))
        return np.vstack(out)

    def build(self, docs):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.docs = docs
        texts = [d["title"] + ". " + d["content"] for d in docs]
        self.tfidf = TfidfVectorizer(stop_words="english").fit(texts)
        self.tfidf_matrix = self.tfidf.transform(texts).toarray()
        self.mode = "tfidf"
        if HF_TOKEN:
            try:
                self.hf_matrix = self._hf_embed(texts)
                self.mode = "huggingface"
            except Exception as e:
                print("HF embeddings failed, using TF-IDF:", e)

    def search(self, query, k=3):
        if not self.docs:
            return []
        scores = None
        threshold = 0.05
        if self.mode == "huggingface":
            try:
                scores = self.hf_matrix @ self._hf_embed([query])[0]
                threshold = 0.3
            except Exception as e:
                print("HF query failed, using TF-IDF:", e)
        if scores is None:
            scores = self.tfidf_matrix @ self.tfidf.transform([query]).toarray()[0]
        order = np.argsort(-scores)[:k]
        return [
            {"title": self.docs[i]["title"], "snippet": self.docs[i]["content"], "score": round(float(scores[i]), 3)}
            for i in order
            if scores[i] > threshold
        ]

    def add(self, doc):
        self.build(self.docs + [doc])


index = VectorIndex()
index.build(load_kb())
print("Vector index mode:", index.mode)


# ---------------- ServiceNow ----------------
def servicenow_create(short, desc, priority="P3", table="incident"):
    if not (SN_INSTANCE and SN_USER and SN_PASS):
        return None
    try:
        urgency = {"P1": "1", "P2": "2"}.get(priority, "3")
        r = httpx.post(
            f"https://{SN_INSTANCE}.service-now.com/api/now/table/{table}",
            auth=(SN_USER, SN_PASS),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"short_description": short[:150], "description": desc, "urgency": urgency},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["result"]["number"]
    except Exception as e:
        print("ServiceNow error:", e)
        return None


# ---------------- Ticket classification ----------------
def rule_classify(text):
    t = text.lower()
    has = lambda *w: any(x in t for x in w)
    if has("vpn"):
        return {"intent": "Incident", "category": "Network / VPN", "priority": "P2", "team": "Network Support"}
    if has("password", "locked", "unlock"):
        return {"intent": "Automatable Issue", "category": "Account / Password", "priority": "P2", "team": "Service Desk"}
    if has("outlook", "email", "mail"):
        return {"intent": "Knowledge Question", "category": "Email / Outlook", "priority": "P3", "team": "Service Desk"}
    if has("vscode", "visual studio", "software", "install"):
        return {"intent": "Service Request", "category": "Software Installation", "priority": "P3", "team": "Software Support"}
    if has("computer", "laptop", "system", "printer", "wifi", "wi-fi"):
        return {"intent": "Incident", "category": "Hardware / Device", "priority": "P2", "team": "IT Support"}
    return {"intent": "Escalation", "category": "Unknown", "priority": "P3", "team": "Helpdesk"}


def classify(text):
    result = rule_classify(text)
    out = llm(
        [
            {
                "role": "system",
                "content": "You are an IT service desk triage AI. Reply with JSON only, with keys intent, category, priority, team. "
                "intent is one of: Incident, Service Request, Knowledge Question, Automatable Issue, Escalation. priority is P1, P2, P3 or P4.",
            },
            {"role": "user", "content": text},
        ],
        120,
    )
    if out:
        try:
            data = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            for k in ("intent", "category", "priority", "team"):
                if isinstance(data.get(k), str) and data[k].strip():
                    result[k] = data[k].strip()
            if result["priority"] not in ("P1", "P2", "P3", "P4"):
                result["priority"] = "P3"
        except Exception:
            pass
    result["self_heal"] = any(w in text.lower() for w in ("password", "vpn", "locked", "unlock"))
    return result


# ---------------- API ----------------
class AnalyzeIn(BaseModel):
    text: str


class HealIn(BaseModel):
    category: str
    reference: str = ""


class ChatIn(BaseModel):
    message: str


class SearchIn(BaseModel):
    query: str


class SoftwareIn(BaseModel):
    software: str
    justification: str = ""


class GenerateIn(BaseModel):
    reference: str


@app.get("/api/health")
def health():
    return {
        "mongodb": tickets_col is not None,
        "huggingface": bool(HF_TOKEN),
        "servicenow": bool(SN_INSTANCE and SN_USER and SN_PASS),
        "vector_mode": index.mode,
    }


# Capability 1: Intelligent Ticket Intake
@app.post("/api/tickets/analyze")
def analyze(body: AnalyzeIn):
    text = body.text.strip()
    result = classify(text)
    sn_number = servicenow_create(text, text, result["priority"])
    ticket = {
        **result,
        "reference": sn_number or local_ref("INC"),
        "description": text,
        "status": "Open",
        "servicenow": bool(sn_number),
        "created_at": now(),
    }
    save_ticket(ticket)
    return ticket


@app.get("/api/tickets")
def tickets():
    return list_tickets()


# Capability 2: Auto Resolution / Self-Heal Agents (simulated actions)
HEAL_ACTIONS = {
    "password": "Self-heal agent verified your identity and sent a password reset link to your registered email.",
    "vpn": "Self-heal agent reset your VPN session and refreshed your VPN profile. Please reconnect now.",
}


@app.post("/api/self-heal")
def self_heal(body: HealIn):
    cat = body.category.lower()
    msg = next((m for k, m in HEAL_ACTIONS.items() if k in cat), None)
    if msg is None:
        return {"message": "No automatic fix for this category. The ticket stays with the assigned team."}
    if body.reference:
        update_ticket(body.reference, {"status": "Resolved by self-heal"})
    return {"message": msg}


# Capability 3: Knowledge Management Automation
@app.post("/api/kb/search")
def kb_search(body: SearchIn):
    return index.search(body.query, 5)


@app.post("/api/kb/generate")
def kb_generate(body: GenerateIn):
    t = find_ticket(body.reference)
    if not t:
        return {"error": "Ticket not found"}
    out = llm(
        [
            {"role": "system", "content": "Write a short IT knowledge base article from this resolved ticket. Reply with JSON only, keys title and content (content in 2 to 4 sentences with clear steps)."},
            {"role": "user", "content": f"Category: {t['category']}\nProblem: {t['description']}"},
        ],
        250,
    )
    article = {"title": f"How to fix: {t['category']}", "content": f"Problem: {t['description']}. Raise a ticket if the issue continues."}
    if out:
        try:
            data = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            article = {"title": str(data["title"]), "content": str(data["content"])}
        except Exception:
            pass
    if kb_col is not None:
        kb_col.insert_one(dict(article))
    index.add(article)
    return article


# Capability 4: Self-Service Portal & AI Chatbot (RAG)
@app.post("/api/chat")
def chat(body: ChatIn):
    hits = index.search(body.message, 3)
    if not hits:
        return {"reply": "I could not find a matching article. Please raise a ticket in Ticket Intake.", "sources": []}
    context = "\n\n".join(f"{h['title']}: {h['snippet']}" for h in hits)
    answer = llm(
        [
            {"role": "system", "content": "You are an IT helpdesk assistant. Answer only from the knowledge base context. Be short and give clear steps. If the context does not answer the question, say so."},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {body.message}"},
        ],
        300,
    )
    return {"reply": answer or hits[0]["snippet"], "sources": [h["title"] for h in hits]}


# Capability 5: Software Provisioning Automation
STANDARD_SOFTWARE = {"Visual Studio Code", "Git", "Python", "Postman"}


@app.post("/api/software/request")
def software(body: SoftwareIn):
    auto = body.software in STANDARD_SOFTWARE
    status = "Approved" if auto else "Pending manager approval"
    sn_number = servicenow_create(
        f"Software request: {body.software}", body.justification or body.software, "P3", table="sc_request"
    )
    ref = sn_number or local_ref("REQ")
    save_ticket(
        {
            "intent": "Service Request",
            "category": f"Software: {body.software}",
            "priority": "P3",
            "team": "Software Support",
            "reference": ref,
            "description": body.justification or body.software,
            "status": status,
            "servicenow": bool(sn_number),
            "created_at": now(),
        }
    )
    msg = (
        f"{body.software} is a standard tool. The install job is queued."
        if auto
        else f"{body.software} needs manager approval. You will be notified."
    )
    return {"reference": ref, "status": status, "message": msg}