import hashlib
import json
import os
import random
import smtplib
import ssl
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_CHAT_MODEL = os.getenv("HF_CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct")
HF_EMBED_MODEL = os.getenv("HF_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
SN_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "")
SN_USER = os.getenv("SERVICENOW_USER", "")
SN_PASS = os.getenv("SERVICENOW_PASSWORD", "")
# winget = really install on this Windows machine, simulate = only show status steps
INSTALL_MODE = os.getenv("INSTALL_MODE", "winget").lower()

MANAGER_EMAIL = os.getenv("MANAGER_EMAIL", "")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "")
INSTALL_APPROVAL = os.getenv("INSTALL_APPROVAL", "true").lower() == "true"
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")

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
# HF_CHAT_MODEL is tried first, then these fallbacks. The model that works is remembered.
CHAT_MODELS = []
for _m in (HF_CHAT_MODEL, "meta-llama/Llama-3.1-8B-Instruct", "openai/gpt-oss-20b", "Qwen/Qwen3-14B"):
    if _m and _m not in CHAT_MODELS:
        CHAT_MODELS.append(_m)
_working_model = None
_llm_down_until = 0.0


def llm(messages, max_tokens=300):
    global _working_model, _llm_down_until
    if not HF_TOKEN or time.time() < _llm_down_until:
        return None
    try:
        from huggingface_hub import InferenceClient

        client = InferenceClient(token=HF_TOKEN, timeout=40)
    except Exception as e:
        print("LLM error:", e)
        return None
    for model in [_working_model] if _working_model else CHAT_MODELS:
        try:
            r = client.chat_completion(messages=messages, model=model, max_tokens=max_tokens, temperature=0.2)
            text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
            if text:
                if _working_model != model:
                    print("LLM model in use:", model)
                _working_model = model
                return text
        except Exception as e:
            print(f"LLM error ({model}):", str(e)[:200].replace("\n", " "))
    _working_model = None
    _llm_down_until = time.time() + 60  # do not retry for a minute, keeps the demo fast
    return None


# ---------------- Knowledge base + vector index (RAG) ----------------
SEED_KB = [
    {"title": "Reset your password", "content": "Open the self-service password portal, click Forgot password, verify with the OTP sent to your phone, then set a new password."},
    {"title": "Fix VPN not connecting", "content": "Check your internet, close and restart the VPN client, sign in again with your SSO account, then reconnect. If it still fails, raise a ticket."},
    {"title": "Outlook not syncing", "content": "Restart Outlook, remove and re-add your email account, then clear the OST cache and restart again."},
    {"title": "Request new software", "content": "Use the Software tab or tell the chatbot, for example Install VS Code. VS Code, Visual Studio, Python, Git, GitHub, Docker, Postman and other common tools are approved and installed automatically."},
    {"title": "Wi-Fi keeps disconnecting", "content": "Wifi or wireless internet drops. Forget the office Wi-Fi network, reconnect with your company login, and update the wireless driver from Device Manager."},
    {"title": "Printer not working", "content": "Check the printer is online, remove the printer from Settings, add it again using the printer name shown on the device, and print a test page."},
    {"title": "Laptop running slow", "content": "Restart your laptop, close unused apps, check Task Manager for high CPU usage, and run Windows Update. Raise a ticket if it stays slow."},
    {"title": "Set up multi-factor authentication", "content": "MFA, 2FA or two-step verification setup. Install the authenticator app, scan the QR code from the security portal, and save your backup codes in a safe place."},
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
        self.tfidf = TfidfVectorizer(stop_words="english", ngram_range=(1, 2)).fit(texts)
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
        threshold = 0.18
        if self.mode == "huggingface":
            try:
                scores = self.hf_matrix @ self._hf_embed([query])[0]
                threshold = 0.4
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
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["result"]["number"]
    except Exception as e:
        print("ServiceNow error:", e)
        return None


# ---------------- Ticket classification ----------------
QUESTION_START = re.compile(r"^\s*(how|what|why|when|where|which|can i|can you|is there|does)\b", re.I)


def rule_classify(text):
    t = text.lower()
    has = lambda *w: any(x in t for x in w)
    if has("vpn"):
        return {"intent": "Incident", "category": "Network / VPN", "priority": "P2", "team": "Network Support"}
    if has("password", "locked", "unlock"):
        return {"intent": "Automatable Issue", "category": "Account / Password", "priority": "P2", "team": "Service Desk"}
    if has("outlook", "email", "mail"):
        intent = "Knowledge Question" if QUESTION_START.match(text) else "Incident"
        return {"intent": intent, "category": "Email / Outlook", "priority": "P3", "team": "Service Desk"}
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


def create_ticket(text, info, table="incident"):
    """Create the ticket in ServiceNow (if configured) and save it in MongoDB / memory."""
    sn_number = servicenow_create(f"{info['category']}: {text}", text, info["priority"], table)
    ticket = {
        "intent": info["intent"],
        "category": info["category"],
        "priority": info["priority"],
        "team": info["team"],
        "reference": sn_number or local_ref("REQ" if table == "sc_request" else "INC"),
        "description": text,
        "status": info.get("status", "New"),
        "servicenow": bool(sn_number),
        "created_at": now(),
    }
    save_ticket(ticket)
    return ticket


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
        "llm_model": _working_model,
        "install_mode": INSTALL_MODE,
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


# ---------------- Password reset: manager approval -> email OTP -> new password ----------------
PW: dict = {}  # ticket reference -> flow state
PENDING_INSTALL: dict = {}  # ticket reference -> install waiting for manager approval
MEM_USERS: dict = {}
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def send_mail(to, subject, body):
    """Send with Gmail SMTP. Returns True if sent. Without SMTP settings the mail is printed in the terminal."""
    if not (SMTP_USER and SMTP_APP_PASSWORD):
        print(f"\n[MAIL not configured] To: {to}\nSubject: {subject}\n{body}\n")
        return False
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = SMTP_USER, to, subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s:
            s.login(SMTP_USER, SMTP_APP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        print("Mail error:", str(e)[:200])
        print(f"[MAIL not sent] To: {to}\nSubject: {subject}\n{body}\n")
        return False


def servicenow_update(number, fields, table="incident"):
    if not (SN_INSTANCE and SN_USER and SN_PASS) or not number.startswith(("INC", "REQ")):
        return False
    try:
        base = f"https://{SN_INSTANCE}.service-now.com/api/now/table/{table}"
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        r = httpx.get(base, params={"sysparm_query": f"number={number}", "sysparm_limit": 1, "sysparm_fields": "sys_id"},
                      auth=(SN_USER, SN_PASS), headers=h, timeout=30)
        r.raise_for_status()
        sys_id = r.json()["result"][0]["sys_id"]
        httpx.patch(f"{base}/{sys_id}", auth=(SN_USER, SN_PASS), headers=h, json=fields, timeout=30).raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print("ServiceNow update error:", str(e)[:150])
        return False


def hash_password(pw, salt=None):
    salt = salt or os.urandom(8).hex()
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def save_user_password(email, pw):
    doc = {"email": email, "password_hash": hash_password(pw), "updated_at": now()}
    if tickets_col is not None:
        tickets_col.database["users"].update_one({"email": email}, {"$set": doc}, upsert=True)
    else:
        MEM_USERS[email] = doc


def start_password_flow(ref, text):
    token = uuid.uuid4().hex
    PW[ref] = {"state": "waiting_approval", "token": token, "email": "", "otp": "", "tries": 0, "expires": 0}
    link = f"{PUBLIC_URL}/api/password/decide/{ref}?token={token}"
    sent = False
    if MANAGER_EMAIL:
        sent = send_mail(MANAGER_EMAIL, f"Approval needed: password reset {ref}",
                         f"A user asked for a password reset.\n\nRequest: {text}\nTicket: {ref}\n\n"
                         f"Approve: {link}&decision=approve\nReject:  {link}&decision=reject\n")
    else:
        print(f"[No MANAGER_EMAIL set] Approve: {link}&decision=approve  Reject: {link}&decision=reject")
    return sent


@app.get("/api/password/decide/{ref}", response_class=HTMLResponse)
def password_decide(ref: str, token: str = "", decision: str = ""):
    p = PENDING_INSTALL.get(ref)
    if p:
        if p["token"] != token:
            return HTMLResponse("<h2>Invalid or expired link.</h2>", status_code=400)
        if p["done"]:
            return HTMLResponse("<h2>Already decided.</h2>")
        p["done"] = True
        if decision == "approve":
            update_ticket(ref, {"status": "Approved by manager"})
            servicenow_update(ref, {"work_notes": f"Manager approved installing {p['name']}."}, "sc_request")
            start_install(p["name"], ref)
            return HTMLResponse(f"<h2>Approved. {p['name']} will be installed now.</h2>")
        update_ticket(ref, {"status": "Rejected by manager"})
        servicenow_update(ref, {"work_notes": f"Manager rejected installing {p['name']}."}, "sc_request")
        INSTALLS[ref] = {"state": "rejected", "steps": ["Rejected by manager"]}
        return HTMLResponse("<h2>Rejected.</h2>")
    f = PW.get(ref)
    if not f or f["token"] != token:
        return HTMLResponse("<h2>Invalid or expired link.</h2>", status_code=400)
    if f["state"] != "waiting_approval":
        return HTMLResponse(f"<h2>Already {f['state'].replace('_', ' ')}.</h2>")
    if decision == "approve":
        f["state"] = "approved"
        update_ticket(ref, {"status": "Approved by manager"})
        servicenow_update(ref, {"work_notes": "Manager approved the password reset."})
        return HTMLResponse("<h2>Approved. The user can continue in the chatbot.</h2>")
    f["state"] = "rejected"
    update_ticket(ref, {"status": "Rejected by manager"})
    servicenow_update(ref, {"work_notes": "Manager rejected the password reset."})
    return HTMLResponse("<h2>Rejected.</h2>")


@app.get("/api/password/status/{ref}")
def password_status(ref: str):
    f = PW.get(ref)
    return {"state": f["state"] if f else "unknown"}


class PwEmailIn(BaseModel):
    reference: str
    email: str


class PwOtpIn(BaseModel):
    reference: str
    otp: str


class PwResetIn(BaseModel):
    reference: str
    password: str


@app.post("/api/password/email")
def password_email(body: PwEmailIn):
    f = PW.get(body.reference)
    email = body.email.strip().lower()
    if not f or f["state"] not in ("approved", "otp_sent"):
        return {"ok": False, "message": "This request is not approved yet."}
    if not EMAIL_RE.match(email):
        return {"ok": False, "message": "That does not look like an email address. Please enter your Gmail again."}
    f.update(email=email, otp=f"{random.randint(0, 999999):06d}", tries=0, expires=time.time() + 300, state="otp_sent")
    sent = send_mail(email, "Your password reset OTP", f"Your OTP is {f['otp']}. It is valid for 5 minutes.")
    note = "" if sent else " (email is not configured, so the OTP is printed in the backend terminal)"
    return {"ok": True, "message": f"I sent a 6-digit OTP to {email}{note}. Please type it here."}


@app.post("/api/password/otp")
def password_otp(body: PwOtpIn):
    f = PW.get(body.reference)
    if not f or f["state"] != "otp_sent":
        return {"ok": False, "message": "Please start again."}
    if time.time() > f["expires"]:
        return {"ok": False, "message": "The OTP expired. Type your Gmail again to get a new one.", "restart": True}
    f["tries"] += 1
    if f["tries"] > 5:
        f["state"] = "approved"
        return {"ok": False, "message": "Too many wrong tries. Type your Gmail again to get a new OTP.", "restart": True}
    if body.otp.strip() != f["otp"]:
        return {"ok": False, "message": "Wrong OTP. Try again."}
    f["state"] = "otp_ok"
    return {"ok": True, "message": "OTP verified. Now type your new password (at least 8 characters, with a number)."}


@app.post("/api/password/reset")
def password_reset(body: PwResetIn):
    f = PW.get(body.reference)
    if not f or f["state"] != "otp_ok":
        return {"ok": False, "message": "Please verify the OTP first."}
    pw = body.password
    if len(pw) < 8 or not re.search(r"\d", pw):
        return {"ok": False, "message": "Password needs at least 8 characters and one number. Try again."}
    save_user_password(f["email"], pw)
    f["state"] = "done"
    update_ticket(body.reference, {"status": "Resolved"})
    note = "Password reset completed after manager approval and email OTP verification."
    # try to resolve the incident; if the integration user is not allowed to, at least leave a work note
    if not servicenow_update(body.reference, {"state": "6", "close_code": "Solved (Permanently)", "close_notes": note}):
        if not servicenow_update(body.reference, {"state": "6", "close_notes": note}):
            servicenow_update(body.reference, {"work_notes": note + " (Resolved in the helpdesk app.)"})
    return {"ok": True, "message": "Your password was changed successfully. Please sign in with the new password."}


# Capability 4: Self-Service Portal & AI Chatbot (RAG)
PASSWORD_WORDS = ("password", "passcode", "forgot my pass", "locked out", "account locked", "unlock my account")
INSTALL_WORDS = re.compile(r"\b(install|installation|download|set ?up|need|want|get|provision)\b", re.I)
HOWTO = re.compile(r"^\s*(how|what|where|which|why)\b|\bsteps?\b", re.I)
PASSWORD_STEPS = (
    "1. Open the self-service password portal.\n"
    "2. Click 'Forgot password' and enter your work email.\n"
    "3. Verify with the OTP sent to your registered mobile or email.\n"
    "4. Set a new password (12+ characters, one number, one symbol).\n"
    "5. Sign in again on all your devices."
)


def chat_intent(q):
    t = q.lower()
    if any(w in t for w in PASSWORD_WORDS):
        return "password"
    if (find_software(q) and INSTALL_WORDS.search(q)) or re.search(r"\bsoftware\b", t):
        return "software"
    return "other"


def ref_note(ticket):
    return "" if ticket["servicenow"] else " (local reference, ServiceNow was not reachable)"


@app.post("/api/chat")
def chat(body: ChatIn):
    q = body.message.strip()
    if not q:
        return {"reply": "Please type your question.", "sources": []}
    intent = chat_intent(q)

    # 1a. "How do I change my password?" is a question: show the steps first, no ticket yet
    if intent == "password" and HOWTO.match(q):
        reply = ("Here is how to change your password:\n" + PASSWORD_STEPS +
                 "\n\nWant me to do it for you? Type \"reset my password\" and I will raise a request for your manager's approval, "
                 "then verify you by email OTP.")
        return {"reply": reply, "sources": ["Reset your password"], "intent": "kb"}

    # 1-2. Password: ticket + manager approval, then email OTP and new password (handled by the /api/password endpoints)
    if intent == "password":
        info = {"intent": "Automatable Issue", "category": "Account / Password", "priority": "P2",
                "team": "Service Desk", "status": "Waiting for manager approval"}
        t = create_ticket(q, info)
        mailed = start_password_flow(t["reference"], q)
        where = f"The approval request was emailed to {MANAGER_EMAIL}." if mailed else "The approval link is printed in the backend terminal (email is not configured)."
        reply = (f"I created ticket {t['reference']}{ref_note(t)} for your password change. "
                 f"It needs your manager's approval. {where} I will continue here as soon as it is approved.")
        return {"reply": reply, "sources": ["Reset your password"], "intent": "password", "reference": t["reference"],
                "flow": "waiting_approval"}

    # 3. Software: ticket + approval + automatic install
    if intent == "software":
        name = find_software(q)
        if not name:
            return {"reply": "Which software do you need? I can install automatically: " + ", ".join(SOFTWARE) + ".",
                    "sources": [], "intent": intent}
        res = provision_software(name, q)
        reply = (f"Request {res['reference']}{'' if res['servicenow'] else ' (local reference)'} created for {name}. "
                 f"Status: {res['status']}. {res['message']}")
        return {"reply": reply, "sources": [], "intent": intent, "reference": res["reference"],
                "install_started": res["install_started"]}

    # 4. Everything else: knowledge base (RAG) first
    hits = index.search(q, 3)
    if hits:
        context = "\n\n".join(f"{h['title']}: {h['snippet']}" for h in hits)
        answer = llm(
            [
                {"role": "system", "content": "You are an IT helpdesk assistant. Answer ONLY from the context. Be short and use numbered steps. "
                                              "If the context does not answer the question, reply exactly NO_ANSWER."},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {q}"},
            ],
            300,
        )
        if answer is None:  # LLM unavailable: the matching article is the answer
            answer = hits[0]["snippet"]
        if not answer.strip().startswith("NO_ANSWER"):
            return {"reply": f"{answer}\n\nArticle: {hits[0]['title']}", "sources": [h["title"] for h in hits], "intent": "kb"}

    # 5. Not in the knowledge base: general AI answer, else raise a ticket
    answer = llm(
        [
            {"role": "system", "content": "You are a helpful assistant inside an IT helpdesk. Answer any question clearly and briefly. "
                                          "If you are not confident, or it needs hands-on IT action, reply exactly NO_ANSWER."},
            {"role": "user", "content": q},
        ],
        350,
    )
    if answer and not answer.strip().startswith("NO_ANSWER"):
        return {"reply": answer + "\n\n(AI answer, not from the knowledge base)", "sources": [], "intent": "llm"}
    info = classify(q)
    info["status"] = "New"
    t = create_ticket(q, info)
    reply = (f"I could not resolve this myself, so I raised ticket {t['reference']}{ref_note(t)}. "
             f"The {info['team']} team will follow up.")
    return {"reply": reply, "sources": [], "intent": "ticket", "reference": t["reference"]}


# Capability 5: Software Provisioning Automation (real install with winget)
SOFTWARE = {
    "Visual Studio Code": {"aliases": ("visual studio code", "vs code", "vscode", "visual code"), "standard": True},
    "Visual Studio": {"aliases": ("visual studio",), "standard": True},
    "Python": {"aliases": ("python",), "standard": True},
    "Git": {"aliases": ("git",), "standard": True},
    "GitHub Desktop": {"aliases": ("github desktop", "github"), "standard": True},
    "Docker Desktop": {"aliases": ("docker desktop", "docker"), "standard": True},
    "Postman": {"aliases": ("postman",), "standard": True},
    "Node.js": {"aliases": ("node.js", "nodejs", "node js"), "standard": True},
    "Java JDK": {"aliases": ("java", "jdk"), "standard": True},
    "Google Chrome": {"aliases": ("google chrome", "chrome"), "standard": True},
    "Mozilla Firefox": {"aliases": ("firefox",), "standard": True},
    "7-Zip": {"aliases": ("7-zip", "7zip"), "standard": True},
    "Notepad++": {"aliases": ("notepad++", "notepad plus"), "standard": True},
    "Slack": {"aliases": ("slack",), "standard": True},
    "Zoom": {"aliases": ("zoom",), "standard": True},
    "VLC Media Player": {"aliases": ("vlc",), "standard": True},
}
INSTALL_STEPS = ["Queued", "Downloading package", "Installing", "Installed successfully"]


def find_software(text: str):
    t = text.lower()
    # check longer aliases first so "visual studio code" wins over "visual studio"
    pairs = sorted(((a, name) for name, d in SOFTWARE.items() for a in d["aliases"]), key=lambda p: -len(p[0]))
    for alias, name in pairs:
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", t):
            return name
    return None


# Only these package IDs can ever be installed (allow-list, nothing user-typed reaches the shell)
WINGET_IDS = {
    "Visual Studio Code": "Microsoft.VisualStudioCode",
    "Visual Studio": "Microsoft.VisualStudio.2022.Community",
    "Python": "Python.Python.3.12",
    "Git": "Git.Git",
    "GitHub Desktop": "GitHub.GitHubDesktop",
    "Docker Desktop": "Docker.DockerDesktop",
    "Postman": "Postman.Postman",
    "Node.js": "OpenJS.NodeJS.LTS",
    "Java JDK": "EclipseAdoptium.Temurin.21.JDK",
    "Google Chrome": "Google.Chrome",
    "Mozilla Firefox": "Mozilla.Firefox",
    "7-Zip": "7zip.7zip",
    "Notepad++": "Notepad++.Notepad++",
    "Slack": "SlackTechnologies.Slack",
    "Zoom": "Zoom.Zoom",
    "VLC Media Player": "VideoLAN.VLC",
}
# Packages installed together with the tool
VSCODE_EXTENSIONS = [
    "ms-python.python", "ms-python.vscode-pylance", "dbaeumer.vscode-eslint", "esbenp.prettier-vscode",
    "eamodio.gitlens", "ritwickdey.liveserver", "ms-azuretools.vscode-docker",
]
PIP_PACKAGES = ["requests", "numpy", "pandas", "flask", "fastapi", "uvicorn"]
NPM_PACKAGES = ["typescript", "nodemon", "yarn"]
EXTRAS_TEXT = {
    "Visual Studio Code": "VS Code extensions (Python, Pylance, ESLint, Prettier, GitLens, Live Server, Docker)",
    "Python": "pip packages (" + ", ".join(PIP_PACKAGES) + ")",
    "Node.js": "npm packages (" + ", ".join(NPM_PACKAGES) + ")",
}
INSTALLS: dict = {}  # reference -> {"state": ..., "steps": [...]}


def _set_install(ref: str, state: str, msg: str) -> None:
    inst = INSTALLS.setdefault(ref, {"state": "queued", "steps": []})
    inst["state"] = state
    inst["steps"].append(msg)
    print(f"[install {ref}] {state}: {msg}")
    status = {"installed": "Installed", "failed": "Install failed"}.get(state, "Installing")
    try:
        update_ticket(ref, {"status": status})
    except Exception:  # noqa: BLE001
        pass


def _run(cmd: list, timeout: int = 1800):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _find_code_cmd():
    for base in (os.getenv("LOCALAPPDATA", ""), os.getenv("ProgramFiles", "")):
        if base:
            for sub in (r"Programs\Microsoft VS Code\bin\code.cmd", r"Microsoft VS Code\bin\code.cmd"):
                path = os.path.join(base, sub)
                if os.path.exists(path):
                    return path
    return shutil.which("code")


def _find_python():
    import glob

    patterns = [os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Python", "Python3*", "python.exe"),
                os.path.join(os.getenv("ProgramFiles", ""), "Python3*", "python.exe")]
    found = sorted(f for pat in patterns for f in glob.glob(pat))
    return found[-1] if found else None


def _find_npm():
    path = os.path.join(os.getenv("ProgramFiles", ""), "nodejs", "npm.cmd")
    return path if os.path.exists(path) else shutil.which("npm")


def install_packages(name: str, ref: str) -> None:
    """Install the extra packages that belong to a tool. Never raises."""
    try:
        if name == "Visual Studio Code":
            cmd = _find_code_cmd()
            if not cmd:
                _set_install(ref, "installing", "VS Code installed; extensions skipped (code command not found yet)")
                return
            for ext in VSCODE_EXTENSIONS:
                _set_install(ref, "installing", f"Installing VS Code extension {ext}")
                _run([cmd, "--install-extension", ext, "--force"], timeout=300)
        elif name == "Python":
            py = _find_python()
            if not py:
                _set_install(ref, "installing", "Python installed; pip packages skipped (python.exe not found yet)")
                return
            _set_install(ref, "installing", "Installing pip packages: " + ", ".join(PIP_PACKAGES))
            code, out = _run([py, "-m", "pip", "install", "--upgrade", "pip", *PIP_PACKAGES], timeout=900)
            if code != 0:
                _set_install(ref, "installing", "pip packages failed: " + out.strip()[-150:])
        elif name == "Node.js":
            npm = _find_npm()
            if not npm:
                _set_install(ref, "installing", "Node.js installed; npm packages skipped (npm not found yet)")
                return
            _set_install(ref, "installing", "Installing npm packages: " + ", ".join(NPM_PACKAGES))
            code, out = _run([npm, "install", "-g", *NPM_PACKAGES], timeout=900)
            if code != 0:
                _set_install(ref, "installing", "npm packages failed: " + out.strip()[-150:])
    except Exception as e:  # noqa: BLE001
        _set_install(ref, "installing", f"Package step skipped: {str(e)[:120]}")


def do_install(name: str, ref: str) -> None:
    try:
        winget = shutil.which("winget")
        if INSTALL_MODE != "winget" or platform.system() != "Windows" or not winget:
            for step in INSTALL_STEPS[:-1]:
                _set_install(ref, "installing", step + " (simulated)")
                time.sleep(2)
            if name in EXTRAS_TEXT:
                _set_install(ref, "installing", "Installing " + EXTRAS_TEXT[name] + " (simulated)")
                time.sleep(2)
            _set_install(ref, "installed", f"{name} installed (simulated)")
            return
        _set_install(ref, "installing", f"Installing {name} with winget. A Windows permission prompt may appear, click Yes.")
        code, out = _run([winget, "install", "--id", WINGET_IDS[name], "-e", "--silent",
                          "--accept-package-agreements", "--accept-source-agreements"])
        low = out.lower()
        already = "already installed" in low or "no newer package" in low or "no available upgrade" in low
        if code != 0 and not already:
            _set_install(ref, "failed", f"winget failed (code {code}): {out.strip()[-200:]}")
            return
        install_packages(name, ref)
        extra = f" with {EXTRAS_TEXT[name]}" if name in EXTRAS_TEXT else ""
        _set_install(ref, "installed", f"{name} installed successfully{extra}" + (" (was already installed)" if already else ""))
    except Exception as e:  # noqa: BLE001
        _set_install(ref, "failed", str(e)[:200])


def start_install(name: str, ref: str) -> None:
    INSTALLS[ref] = {"state": "queued", "steps": ["Queued"]}
    threading.Thread(target=do_install, args=(name, ref), daemon=True).start()


def provision_software(name: str, justification: str = "") -> dict:
    standard = SOFTWARE.get(name, {}).get("standard", False)
    info = {"intent": "Service Request", "category": f"Software: {name}", "priority": "P3",
            "team": "Software Support", "status": "Approved" if standard else "Pending approval"}
    doc = create_ticket(f"Install {name}. {justification}".strip(), info, table="sc_request")
    started = False
    if standard and name in WINGET_IDS:
        extra = f" with {EXTRAS_TEXT[name]}" if name in EXTRAS_TEXT else ""
        if INSTALL_APPROVAL:
            ref = doc["reference"]
            token = uuid.uuid4().hex
            PENDING_INSTALL[ref] = {"name": name, "token": token, "done": False}
            INSTALLS[ref] = {"state": "waiting_approval", "steps": ["Waiting for manager approval"]}
            update_ticket(ref, {"status": "Waiting for manager approval"})
            doc["status"] = "Waiting for manager approval"
            link = f"{PUBLIC_URL}/api/password/decide/{ref}?token={token}"
            mailed = False
            if MANAGER_EMAIL:
                mailed = send_mail(MANAGER_EMAIL, f"Approval needed: install {name} ({ref})",
                                   f"A user requested to install {name}.\nTicket: {ref}\n\n"
                                   f"Approve: {link}&decision=approve\nReject:  {link}&decision=reject\n")
            else:
                print(f"[No MANAGER_EMAIL set] Approve: {link}&decision=approve  Reject: {link}&decision=reject")
            where = f"The approval request was emailed to {MANAGER_EMAIL}." if mailed else "The approval link is printed in the backend terminal."
            message = f"{name} needs your manager's approval first. {where} The install starts automatically{extra} once it is approved."
            started = True  # the frontend keeps watching the status
        else:
            start_install(name, doc["reference"])
            started = True
            how = "on this machine" if INSTALL_MODE == "winget" else "(simulated)"
            message = f"{name} is a standard tool, so it was auto-approved. Installation started {how}{extra}. It takes a few minutes."
    else:
        message = f"{name} is not on the auto-install list. Your request has been sent to the Software Support team."
    return {"reference": doc["reference"], "status": doc["status"], "message": message,
            "servicenow": doc["servicenow"], "install_started": started}


@app.get("/api/software/status/{ref}")
def install_status(ref: str):
    inst = INSTALLS.get(ref)
    if not inst:
        return {"state": "unknown", "message": "No install running for this reference.", "steps": []}
    return {"state": inst["state"], "message": inst["steps"][-1], "steps": inst["steps"]}


@app.post("/api/software/request")
def software(body: SoftwareIn):
    name = find_software(body.software) or body.software
    return provision_software(name, body.justification)
