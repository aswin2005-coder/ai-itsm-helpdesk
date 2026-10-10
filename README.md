# AI-Powered ITSM Helpdesk

A working prototype of an AI-powered IT Service Management (ITSM) helpdesk. Employees describe a problem in plain English, the system classifies it, creates a ticket, tries to fix it automatically, and answers questions from a knowledge base using RAG.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React.js (Vite) |
| Backend | Python, FastAPI |
| Database | MongoDB Atlas |
| AI | Hugging Face Inference (open-source LLM and embeddings) |
| Knowledge search | RAG with vector similarity search |
| ITSM | ServiceNow REST Table API |

## The five capabilities

1. **Intelligent Ticket Intake**: free-text problem in, structured ticket out (intent, category, priority, assigned team, reference number). Uses the LLM, with a rule-based fallback.
2. **Auto Resolution / Self-Heal Agents**: for known issues (password, VPN) a self-heal agent runs a fix and marks the ticket resolved. The agent actions are simulated in this prototype.
3. **Knowledge Management Automation**: knowledge base articles are embedded and searched by similarity. `POST /api/kb/generate` writes a new article from a resolved ticket and adds it to the index.
4. **Self-Service Portal and AI Chatbot**: the chatbot retrieves the best articles and the LLM answers using only that context (RAG), with the source articles shown.
5. **Software Provisioning Automation**: standard tools are approved automatically, others go to manager approval. The install job is simulated.

## Architecture

```
React (Vite)  ->  FastAPI  ->  MongoDB Atlas (tickets, KB articles)
                     |-------> Hugging Face (classification, chat, embeddings)
                     |-------> ServiceNow REST API (incident / request)
```

If MongoDB, Hugging Face or ServiceNow is not configured, the backend degrades gracefully instead of failing: tickets are kept in memory, search uses TF-IDF, classification uses rules, and reference numbers are generated locally. The frontend shows "Demo mode" when the backend is unreachable.

## Run locally

### 1. Backend

```
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Create `backend/.env` (never commit this file):

```
MONGODB_URI=
HF_TOKEN=
HF_CHAT_MODEL=meta-llama/Llama-3.2-3B-Instruct
HF_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
SERVICENOW_INSTANCE=
SERVICENOW_USER=
SERVICENOW_PASSWORD=
```

Check status at `http://localhost:8000/api/health`.

### 2. Frontend

```
npm install
npm run dev
```

Open the URL shown in the terminal (for example `http://localhost:5173`). To point the frontend at a deployed backend, set `VITE_API_URL`.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/tickets/analyze` | Classify a problem and create a ticket |
| GET | `/api/tickets` | List tickets |
| POST | `/api/self-heal` | Run the self-heal agent |
| POST | `/api/kb/search` | Search the knowledge base |
| POST | `/api/kb/generate` | Generate a KB article from a ticket |
| POST | `/api/chat` | RAG chatbot |
| POST | `/api/software/request` | Software provisioning |
| GET | `/api/health` | Shows which integrations are active |

## ServiceNow integration status

The ServiceNow integration is implemented with the REST Table API (`incident` for tickets, `sc_request` for software requests) and is switched on by setting the three `SERVICENOW_*` variables. Without credentials the app uses locally generated reference numbers.
- Verified live against a ServiceNow developer instance: tickets raised in the app are created as real incidents (e.g. INC0010002).
- Uses a dedicated integration user with the `itil` and `snc_basic_auth_api_access` roles (ServiceNow now restricts Basic Auth for REST by default).

## Limitations

- Self-heal and software install actions are simulated and do not touch real machines.
- No user login or role-based access in this prototype.
- LLM output quality depends on the Hugging Face model and availability.