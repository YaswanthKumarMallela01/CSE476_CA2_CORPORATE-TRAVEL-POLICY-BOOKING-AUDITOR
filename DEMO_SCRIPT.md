# Demo Script — 6-Minute Viva Walkthrough

**CSE476 CA2 — Corporate Travel Policy Booking Auditor**
**Group 15, Q15 · Yaswanth Kumar Mallela (12315067)**

---

## Timing Plan (6 minutes)

| Time | Section | What to show |
|------|---------|--------------|
| 0:00-0:30 | Intro | Problem statement + architecture diagram |
| 0:30-1:30 | Architecture | Code walkthrough: 5 agents, memory, RAG |
| 1:30-3:00 | Live Demo | Run 3 requests: APPROVE, MODIFY, ESCALATE |
| 3:00-4:00 | Eval | Run `python main.py eval`, show metrics |
| 4:00-5:00 | HITL | Human review loop: escalate → approve → verify |
| 5:00-6:00 | Design | Key decisions + Q&A |

---

## Step-by-Step

### 1. Introduction (0:00-0:30)

> "We built a multi-agent system that audits corporate travel requests
> against an 8-clause policy. It uses 5 specialised agents coordinated
> in a manager-worker pattern, with RAG over the policy and shared
> SQLite memory."

Show: `README.md` architecture diagram.

### 2. Architecture Walkthrough (0:30-1:30)

Show these files briefly:

```
agents/approval_manager.py   → Manager: orchestrates workers
agents/request_parser_agent.py → Worker 1: NL → structured request
agents/policy_retrieval_agent.py → Worker 2: RAG retrieval
agents/fare_search_agent.py → Worker 3: fare lookup
agents/budget_checker_agent.py → Worker 4: budget/cap check
core/decision_rules.py → Deterministic rules (no LLM)
core/memory.py → SQLite shared memory
core/rag.py → ChromaDB per-clause ingestion
```

Point out:
- Each agent is a separate module
- Pydantic schemas for all inter-agent messages
- Every agent reads AND writes shared memory
- Decision is deterministic; LLM only for parsing/justification

### 3. Live Demo (1:30-3:00)

**APPROVE case:**
```bash
python main.py audit "I need to fly Delhi to Mumbai on 2 Dec 2026 for a client meeting. Economy. Employee E101."
```
→ Shows APPROVE, TP-2.2, best fare 9,500

**MODIFY case:**
```bash
python main.py audit "Book me Business class Delhi to Bangalore, 27 Nov 2026. Client meeting. Employee E101."
```
→ Shows MODIFY, TP-2.1, proposes economy fare 7,800

**ESCALATE case:**
```bash
python main.py audit "Delhi to Goa on 15 Nov 2026. Client meeting. Economy. Employee E101."
```
→ Shows ESCALATE, TP-1.1 (only 3 days notice)

### 4. Evaluation (3:00-4:00)

```bash
python main.py eval
```

Point at:
- Per-case results table (all 10 cases)
- Decision accuracy, clause correctness, fare accuracy
- Confusion matrix
- `eval/report.md` for the generated report

### 5. Human-in-the-Loop (4:00-5:00)

```bash
# Show escalated requests
python main.py review list

# Approve one
python main.py review decide REQ-XXXX --verdict approve --note "Manager approved for urgent client need"

# Show the audit trail update
python main.py review show REQ-XXXX
```

Or show the Streamlit UI:
```bash
streamlit run ui/review_app.py
```

Point out: budget deducted, trip history updated, audit trail logged.

### 6. Design Decisions (5:00-6:00)

Summarise and take questions.

---

## Likely Examiner Questions & Answers

### Q1: Why manager-worker instead of peer-to-peer?

> The travel audit is a sequential pipeline: parse → retrieve → check → decide.
> A central manager ensures correct ordering, handles retries, and provides a
> single point of logging. Workers are stateless — they receive structured
> inputs and return structured outputs. This makes the system testable and
> modular.

### Q2: Why are decisions deterministic and not made by the LLM?

> Three reasons: (1) Reproducibility — the same input always gives the same
> decision. (2) Testability — we can unit-test every rule without LLM calls.
> (3) Free-tier resilience — if the Gemini quota runs out, the system still
> works. The LLM adds value in parsing natural language and writing
> human-readable justifications, but never overrides the rule engine.

### Q3: How does RAG work per clause?

> The travel policy is split into 8 chunks, one per clause (TP-1.1 through
> TP-9.1). Each chunk is embedded using sentence-transformers (local, no API)
> and stored in ChromaDB with the clause_id as metadata. For each request, we
> generate 3-5 targeted queries (about class, caps, advance booking, purpose,
> budget) and run multi-query retrieval, merging and deduplicating by clause_id.

### Q4: How is memory shared between agents?

> All 5 agents receive the same `MemoryStore` instance backed by SQLite. It
> exposes typed read/write methods — no agent touches SQL directly. Tables
> include employees, trip_history, dept_budget_ledger, audit_log, and
> review_queue. Every agent reads from AND writes to memory at least once per
> request, verified by tests.

### Q5: How does the human loop flow back?

> When the Approval Manager decides ESCALATE, it calls `hitl.enqueue()` which
> creates a PENDING row in review_queue. A human (via CLI or Streamlit) submits
> a verdict (approve/reject/modify). `hitl.submit_verdict()` then:
> (1) updates review_queue status, (2) updates trip_history status,
> (3) deducts the fare from dept_budget_ledger if approved,
> (4) appends to audit_log. A follow-up request by the same employee will
> see the updated trip count and budget.

### Q6: How do you handle free-tier rate limits?

> Every LLM call goes through a shared async rate limiter (configurable RPM)
> with exponential backoff + jitter on 429/503 errors. Responses are cached
> on disk keyed by hash(model, prompt, schema). At most 3 LLM calls per
> request. Eval cases run sequentially. If quota is exhausted, the parser
> falls back to regex, re-ranking is skipped, and justification uses a
> template.

### Q7: What framework features of Semantic Kernel are used?

> We use `GoogleAIChatCompletion` for LLM integration, `ChatHistory` for
> conversation management, `Kernel` for service registration, and
> `kernel_function` decorator for exposing the fare search tool as a plugin.
> The manager creates and orchestrates agents, each built around SK services.

### Q8: Why does Case 10 use TP-9.1 instead of TP-2.2?

> Case 10 (INR 145,000 business fare) violates both TP-9.1 (>100K threshold)
> and TP-2.2 (>120K business cap). Under the clause precedence rule
> `TP-9.1 > ... > TP-2.2`, TP-9.1 outranks TP-2.2 and becomes the primary
> clause. TP-2.2 appears as a secondary violation.
