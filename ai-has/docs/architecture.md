# AI-HAS Architecture

```mermaid
flowchart LR
    U[Employee] --> FE[Chat UI]
    FE -->|POST /chat| API[FastAPI Backend]
    API --> G1[Input Guardrails<br/>PII redaction + injection check]
    G1 --> IC[Intent Classifier<br/>gpt-4o-mini]
    IC -->|how-to / troubleshooting| RAG[RAG Retriever]
    RAG <--> VDB[(ChromaDB)]
    IC -->|action request| FC[Function Calling]
    FC --> N8N[n8n Webhooks]
    N8N --> EXT[Mock Jira / AD]
    IC -->|unknown / low confidence| ESC[Escalate to Human]
    RAG --> LLM[Grounded Answer + Citations]
    LLM --> G2[Output Guardrails]
    G2 --> FE
    API <--> DB[(PostgreSQL)]
    ESC --> AG[Agent Dashboard]
    API --> ADM[Admin KPI Dashboard]
```

## Components
| Component | Tech | Role |
|---|---|---|
| Backend | FastAPI | Chat API, RAG orchestration, tool handlers |
| Vector DB | ChromaDB | SOP embeddings |
| Relational DB | PostgreSQL | Users, tickets, messages, audit logs |
| Automation | n8n | Ticket creation, password reset webhooks |
| LLM | gpt-4o-mini | Intent, answers, function calling |

## Request flow
1. Redact PII and check for prompt injection.
2. Classify intent.
3. Route: RAG answer, function call, or escalate.
4. Save message with citations, confidence, latency.
5. Return the answer (streamed).
