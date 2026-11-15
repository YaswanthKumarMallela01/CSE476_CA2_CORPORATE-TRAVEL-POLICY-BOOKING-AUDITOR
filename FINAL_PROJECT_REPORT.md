# CSE476: MULTI-AGENT SYSTEMS (CA2)
## COMPREHENSIVE TECHNICAL PROJECT REPORT

---

# CORPORATE TRAVEL POLICY BOOKING AUDITOR
### An Autonomous Multi-Agent System with Manager-Worker Delegation, Grounded RAG, Shared Memory, Deterministic Precedence Rules, and Human-in-the-Loop Routing

---

| **Project Attribute** | **Specification Details** |
|:---|:---|
| **Course Code & Title** | **CSE476 — Multi-Agent Systems** |
| **Assessment Component** | Continuous Assessment 2 (CA2) · Maximum Marks: 30 |
| **Group & Question Allocation** | **Group 15, Question 15** |
| **Student Name** | **Yaswanth Kumar Mallela** |
| **Registration Number** | **12315067** |
| **Target Course Outcomes** | **CO3** (Design & Coordination of Multi-Agent Systems)<br>**CO4** (Implementation & Evaluation of Collaborative Architectures) |
| **Bloom's Taxonomy Levels** | **L4 (Analysis)** · **L5 (Evaluation)** · **L6 (Creation)** |
| **GitHub Source Repository** | [https://github.com/YaswanthKumarMallela01/CSE476_CA2_CORPORATE-TRAVEL-POLICY-BOOKING-AUDITOR](https://github.com/YaswanthKumarMallela01/CSE476_CA2_CORPORATE-TRAVEL-POLICY-BOOKING-AUDITOR) |
| **Presentation Slides (PPT)** | [Google Slides Presentation](https://docs.google.com/presentation/d/10t75tNwbDFZmGRDx_L-PAPktqg1GbNYs/edit?usp=sharing&ouid=106909517235174732780&rtpof=true&sd=true) |
| **Evaluation Date & Time** | November 2026 |

---

## TABLE OF CONTENTS
1. [Executive Summary & Problem Formulation](#1-executive-summary--problem-formulation)
2. [Multi-Agent System Architecture & Coordination Paradigm](#2-multi-agent-system-architecture--coordination-paradigm)
3. [Specialized Worker & Manager Agent Specifications](#3-specialized-worker--manager-agent-specifications)
4. [Grounded Policy Retrieval (RAG) & Vector Knowledge Base](#4-grounded-policy-retrieval-rag--vector-knowledge-base)
5. [Shared Memory Architecture & Relational Persistence](#5-shared-memory-architecture--relational-persistence)
6. [Deterministic Policy Rule Engine & Precedence Hierarchy](#6-deterministic-policy-rule-engine--precedence-hierarchy)
7. [Group Add-On: Manager-Approval Human-in-the-Loop (HITL) Workflow](#7-group-add-on-manager-approval-human-in-the-loop-hitl-workflow)
8. [Free-Tier Safety, Rate Limiting & Graceful Degradation](#8-free-tier-safety-rate-limiting--graceful-degradation)
9. [Empirical Evaluation Benchmark & Experimental Results](#9-empirical-evaluation-benchmark--experimental-results)
10. [System Implementation, Web UI/UX & Deployment Guide](#10-system-implementation-web-uiux--deployment-guide)
11. [Conclusion, Learnings & Future Enhancements](#11-conclusion-learnings--future-enhancements)

---

# 1. EXECUTIVE SUMMARY & PROBLEM FORMULATION

### 1.1 Problem Statement
Modern corporate enterprises enforce complex, multifaceted travel policies to control operating expenditures, ensure traveler safety, and maintain organizational discipline. However, traditional travel compliance auditing faces severe operational bottlenecks:
- **Rule Complexity and Cross-Dependency:** Compliance checks span advance booking thresholds, cabin class eligibility tied to employee hierarchy and flight duration, category-specific fare caps, dynamic departmental budget balances, contracted airline preferences, frequency limits per calendar quarter, and high-value expenditure thresholds.
- **Manual Overhead:** Human travel desks are inundated with routine requests, leading to slow turnaround times (hours to days), inconsistent application of policy clauses, and human error in arithmetic checks.
- **Rigid Legacy Automation:** Traditional rule engines fail when parsing natural language travel requests submitted via email, chat, or unstructured forms, requiring brittle manual data entry.
- **Safety and Hallucination Risks of Pure LLMs:** Offloading policy compliance entirely to generic Large Language Models (LLMs) introduces stochastic non-determinism, hallucinations, vulnerability to prompt injections, and lack of mathematical auditability.

### 1.2 Objective & Solution Overview
This project delivers a **production-ready, fully functional Multi-Agent System (MAS)** that automates corporate travel policy auditing. Built on **Semantic Kernel for Python**, **Google Gemini Free Tier**, **ChromaDB vector retrieval**, and **SQLite shared memory**, the system achieves:
1. **Natural Language Understanding:** Robust extraction of routes, dates, cabins, purposes, and employee IDs from conversational text.
2. **Grounded Policy Retrieval (RAG):** Clause-level chunking and local dense embedding retrieval over company policies.
3. **Deterministic Compliance Verification:** A zero-hallucination deterministic rule engine that applies strict clause precedence.
4. **Shared Relational Memory:** Five relational tables ensuring inter-agent state sharing, auditable logging, and budget tracking.
5. **Manager-Approval Human Loop:** An end-to-end escalation queue allowing authorized supervisors to review, approve, reject, or modify out-of-policy requests with automatic budget ledger deduction.

```
+-------------------------------------------------------------------------------------------------------+
|                                    PROJECT SUCCESS HIGHLIGHTS                                         |
+------------------------------------+----------------------------------+-------------------------------+
|  100% Decision Accuracy (10/10)    |  100% Clause Correctness (10/10) |  100% Best Fare Match (10/10) |
+------------------------------------+----------------------------------+-------------------------------+
|  Average Latency: 1.173 seconds    |  57/57 Automated Tests Passing   |  Zero False Positive/Negative |
+-------------------------------------------------------------------------------------------------------+
```

---

# 2. MULTI-AGENT SYSTEM ARCHITECTURE & COORDINATION PARADIGM

### 2.1 Coordination Pattern: Manager-Worker Delegation with Human Routing
The system implements a centralized **Manager-Worker Architecture** augmented with **Human-in-the-Loop (HITL) Routing**. Rather than unstructured peer-to-peer communication (which risks deadlock and uncontrolled token expenditure), the coordination model follows strict structural delegation:

```mermaid
graph TD
    User["User / Employee (Web UI / CLI)"] -->|Raw Natural Language Request| AM["Approval Manager Agent<br/>(Manager / Orchestrator)"]

    subgraph Workers ["Specialized Worker Agents (Semantic Kernel Plugins)"]
        RP["Request Parser Agent<br/>(Extracts Route, Dates, Cabin, Purpose)"]
        PR["Policy Retrieval Agent<br/>(Multi-Query RAG over ChromaDB)"]
        FS["Fare Search Agent<br/>(Route Fares & Contracted Airlines)"]
        BC["Budget Checker Agent<br/>(Fare Caps, Budget Math, TP-5.1 Preference)"]
    end

    subgraph MemoryLayer ["Shared Relational Memory Store (SQLite)"]
        DB[("MemoryStore (travel_auditor.db)<br/>- employees<br/>- trip_history<br/>- dept_budget_ledger<br/>- audit_log<br/>- review_queue")]
    end

    subgraph VectorLayer ["Vector Retrieval Engine"]
        VDB[("ChromaDB Vector Store<br/>all-MiniLM-L6-v2 ONNX Embeddings<br/>8 Policy Clauses")]
    end

    subgraph HumanLayer ["Human-in-the-Loop (HITL) Review Queue"]
        HQ["Review Queue (PENDING_HUMAN)"]
        HR["Human Reviewer (Approve / Reject / Modify)"]
    end

    %% Delegation flow
    AM -->|Step 1: Parse Request| RP
    AM -->|Step 2a: Retrieve Applicable Clauses| PR
    AM -->|Step 2b: Search Mock Available Fares| FS
    AM -->|Step 3: Filter Compliance & Check Ledger| BC

    %% Vector Store connection
    PR <-->|Multi-Query Semantic Search| VDB

    %% Shared Memory connections (Mandatory R/W)
    RP <-->|Read Employee / Write Parse Audit| DB
    PR <-->|Read Prior Trips / Write RAG Audit| DB
    FS <-->|Read Preferences / Write Fares Audit| DB
    BC <-->|Read Ledger & Trips / Write Check Audit| DB
    AM <-->|Read Quarter Count / Write AuditResult & Trip| DB

    %% Decision routing
    AM -->|Deterministic Evaluation| DE{"Decision Engine<br/>Precedence Order"}
    DE -->|APPROVE| OUT_APP["APPROVED: Deduct Budget & Issue Booking"]
    DE -->|MODIFY| OUT_MOD["MODIFY: Propose Best Compliant Option"]
    DE -->|ESCALATE| HQ

    %% HITL Flow-back
    HQ --> HR
    HR -->|Submit Verdict + Reviewer Note| DB
```

### 2.2 Inter-Agent Message Contracts (Pydantic v2 Schemas)
To prevent prompt drift and guarantee interface stability, agents communicate exclusively through strongly-typed Pydantic models. Unstructured free-text passing between agents is prohibited.

```
+--------------------------------------------------------------------------------------------------------+
|                                    INTER-AGENT SCHEMAS OVERVIEW                                        |
+---------------------+------------------------------+---------------------------------------------------+
| Schema Class        | Producing Agent              | Key Fields & Data Types                           |
+---------------------+------------------------------+---------------------------------------------------+
| ParsedRequest       | Request Parser Agent         | request_id, employee_id, origin_iata,             |
|                     |                              | destination_iata, depart_date, return_date,        |
|                     |                              | purpose_category, requested_class, requested_fare,|
|                     |                              | is_international, days_ahead                      |
+---------------------+------------------------------+---------------------------------------------------+
| RetrievedPolicy     | Policy Retrieval Agent       | clauses: list[RetrievedClause],                   |
|                     |                              | applicable_clause_ids: list[str]                  |
+---------------------+------------------------------+---------------------------------------------------+
| FareOptions         | Fare Search Agent            | options: list[FareOption] (fare_id, airline,      |
|                     |                              | price_inr, cabin, duration_hours, contracted)     |
+---------------------+------------------------------+---------------------------------------------------+
| BudgetCheck         | Budget Checker Agent         | cap_inr, cap_clause, dept_remaining_inr,          |
|                     |                              | compliant_options: list[FareOption],              |
|                     |                              | cheapest_compliant: Optional[FareOption],         |
|                     |                              | fits_budget: bool, over_cap_requested: bool       |
+---------------------+------------------------------+---------------------------------------------------+
| AuditResult         | Approval Manager Agent       | request_id, decision (APPROVE/MODIFY/ESCALATE),   |
|                     |                              | primary_clause_id, secondary_clause_ids,          |
|                     |                              | best_compliant_fare, justification,               |
|                     |                              | routed_to_human, status                           |
+---------------------+------------------------------+---------------------------------------------------+
```

---

# 3. SPECIALIZED WORKER & MANAGER AGENT SPECIFICATIONS

The system decomposes the auditing responsibility across five dedicated Python modules in `agents/`:

### 3.1 Request Parser Agent (`agents/request_parser_agent.py`)
- **Functional Responsibility:** Transforms unstructured natural language into a canonical `ParsedRequest`.
- **Hybrid Extraction Architecture:**
  - *Primary Pipeline:* Uses Gemini via Semantic Kernel structured prompting (`llm_json_call`) with a JSON schema hint.
  - *Fallback Pipeline:* High-performance deterministic regex and word-boundary dictionary scanner mapping city names to official IATA airport codes (`DEL`, `BOM`, `BLR`, `HYD`, `CCU`, `GOI`, `SIN`, `LHR`, `JFK`).
- **Deterministic Re-computation:** Regardless of whether LLM or regex extracted the raw entities, the agent deterministically computes:
  $$\text{days\_ahead} = \text{depart\_date} - \text{TODAY}$$
  $$\text{is\_international} = (\text{origin} \notin \text{INDIAN\_IATA}) \lor (\text{dest} \notin \text{INDIAN\_IATA})$$
- **Memory Interaction:** Reads employee profile from `MemoryStore`; writes `parsed_request` event to `audit_log`.

### 3.2 Policy Retrieval Agent (`agents/policy_retrieval_agent.py`)
- **Functional Responsibility:** Identifies and ranks company policy clauses applicable to the specific trip context.
- **Multi-Query Semantic Generation:** Rather than issuing a single vague query, it synthesizes 5 targeted search vectors based on request attributes:
  1. Advance booking requirement for domestic/international route.
  2. Travel cabin class eligibility for employee grade and flight duration.
  3. Fare cap rules for route and class.
  4. Purpose eligibility rules for the declared business category.
  5. High-value expenditure and quarterly trip frequency limitations.
- **Memory Interaction:** Reads employee's past trip history to verify historical frequency; writes retrieved clause IDs and similarity scores to `audit_log`.

### 3.3 Fare Search Agent (`agents/fare_search_agent.py`)
- **Functional Responsibility:** Interfaces with available flight inventories to retrieve real-time route fares.
- **Framework Native Plugin:** Encapsulated as a Semantic Kernel Plugin with `@kernel_function(name="search_fares")`.
- **Inventory Slicing:** Queries `tools/fare_tool.py` for matching origin-destination pairs on the requested departure date.
- **Memory Interaction:** Reads corporate airline preferences; writes found fare count and lowest pricing to `audit_log`.

### 3.4 Budget Checker Agent (`agents/budget_checker_agent.py`)
- **Functional Responsibility:** Deterministically computes fare caps, verifies departmental budget headroom, and enforces corporate airline preferences.
- **Filtering Logic:** Filters raw options through:
  1. *Class Authorization (TP-2.1):* Validates if requested cabin is permitted.
  2. *Category Cap (TP-2.2):* Discards options exceeding the strict cap.
  3. *Ledger Balance (TP-4.1):* Discards options exceeding the remaining department budget.
  4. *Contracted Carrier Preference (TP-5.1):* Prioritizes contracted airlines (AirIndia, IndiGo, Vistara-AI). A non-contracted carrier is retained **only if it is cheaper than the lowest contracted fare by at least 10%**.
- **Memory Interaction:** Reads live departmental balance from `dept_budget_ledger`; writes budget compliance verification to `audit_log`.

### 3.5 Approval Manager Agent (`agents/approval_manager.py`)
- **Functional Responsibility:** Central orchestrator managing the workflow lifecycle.
- **Delegation Protocol:** Dispatches tasks sequentially and concurrently (`asyncio.gather` for Policy Retrieval and Fare Search).
- **Single-Retry Resilience:** Wraps each worker delegation in `_delegate_with_retry()`. If a worker returns invalid schemas, the manager retries once before logging failure.
- **Deterministic Decision Delegation:** Passes collected state to `core/decision_rules.py`.
- **Justification Synthesis:** Uses Gemini to synthesize a concise, auditable 1-2 sentence justification that cites the primary clause ID. If LLM output contradicts the rule engine verdict, it is discarded and replaced with a guaranteed deterministic template.
- **Memory Interaction:** Reads quarterly trip count; writes `final_decision`, updates `trip_history`, deducts budget if approved, and enqueues to `review_queue` if escalated.

---

# 4. GROUNDED POLICY RETRIEVAL (RAG) & VECTOR KNOWLEDGE BASE

### 4.1 Policy Clause Chunking Strategy
Generic RAG systems chunk documents arbitrarily by fixed token counts, which splits clauses and destroys semantic coherence. In this system, `data/policy/travel_policy.md` is ingested with **clause-level chunking**: exactly one document chunk per policy clause:

```markdown
# TP-1.1 Advance Booking
Domestic travel must be booked at least 7 days before departure. International travel must be 
booked at least 14 days before departure. Shorter notice is an exception and requires manager approval.

# TP-2.1 Travel Class
Economy class is mandatory by default. Premium Economy or Business is permitted only if the flight 
duration is 6 hours or more AND the employee grade is L5 or above. Otherwise the booking must be changed to Economy.
...
```

### 4.2 Local Embedding Pipeline & Vector Store
- **Vector Database:** **ChromaDB** running in persistent on-disk mode (`chroma_db/`).
- **Embedding Model:** Local `sentence-transformers/all-MiniLM-L6-v2` executed via Chroma's built-in **ONNX runtime engine**.
- **Zero Cost & Zero Network Leakage:** Dense 384-dimensional vector embeddings are computed locally without third-party API dependencies, completely eliminating embedding costs and latency spikes.
- **Multi-Query Aggregation:** The 5 synthetic queries are executed concurrently against ChromaDB. Results are deduplicated by `clause_id`, preserving the highest cosine similarity score for each clause.

---

# 5. SHARED MEMORY ARCHITECTURE & RELATIONAL PERSISTENCE

### 5.1 Relational Schema (`travel_auditor.db`)
The shared memory layer is centralized in `core/memory.py` through `MemoryStore`. All agents interact with SQLite exclusively through this API. Direct SQL queries outside `MemoryStore` are prohibited.

```mermaid
erDiagram
    EMPLOYEES ||--o{ TRIP_HISTORY : "takes"
    EMPLOYEES ||--o{ DEPT_BUDGET_LEDGER : "belongs to"
    TRIP_HISTORY ||--o{ AUDIT_LOG : "generates"
    REVIEW_QUEUE ||--|| AUDIT_LOG : "audited by"

    EMPLOYEES {
        string employee_id PK
        string name
        string grade
        string department
        string email
    }

    TRIP_HISTORY {
        string trip_id PK
        string employee_id FK
        string origin
        string destination
        string depart_date
        float fare_inr
        string status
        string clause_id
        string decided_by
        string created_at
    }

    DEPT_BUDGET_LEDGER {
        string dept PK
        float remaining_inr
        string updated_at
    }

    AUDIT_LOG {
        int id PK
        string request_id
        string agent
        string action
        string payload_json
        string ts
    }

    REVIEW_QUEUE {
        string request_id PK
        string status
        string human_verdict
        string human_note
        string reviewed_at
        string audit_result_json
    }
```

### 5.2 Mandatory Agent Read/Write Verification
Per the design specification, every agent must read from and write to shared memory at least once per run:

| Agent Name | Shared Memory Read Action | Shared Memory Write Action |
|:---|:---|:---|
| **Request Parser** | Reads employee profile (`get_employee`) | Writes parsed request metadata to `audit_log` |
| **Policy Retrieval** | Reads employee's prior trips (`get_trips_for_employee`) | Writes query set and retrieved clause scores to `audit_log` |
| **Fare Search** | Reads preferred airline and cabin rules | Writes found inventory count and price range to `audit_log` |
| **Budget Checker** | Reads department budget and employee grade | Writes cap computation and budget fit result to `audit_log` |
| **Approval Manager** | Reads quarterly trip count (`count_trips_in_quarter`) | Writes final `AuditResult`, trip record, and `review_queue` entry |

---

# 6. DETERMINISTIC POLICY RULE ENGINE & PRECEDENCE HIERARCHY

### 6.1 Why Deterministic Rules Instead of LLM Decisions?
In high-stakes enterprise compliance, LLMs should be used for **perception and semantic extraction**, never for **authoritative judicial ruling**. Relying on an LLM to decide compliance invites non-deterministic outputs, prompt injection vulnerabilities, and unexplainable approvals. 

In this system, compliance evaluation in `core/decision_rules.py` is **100% deterministic, mathematically verifiable, and reproducible**.

### 6.2 Clause Precedence Order
When a travel request violates multiple clauses simultaneously, the primary clause is resolved using strict lexicographical precedence:

$$\mathbf{TP\text{-}6.1} \succ \mathbf{TP\text{-}1.1} \succ \mathbf{TP\text{-}7.1} \succ \mathbf{TP\text{-}9.1} \succ \mathbf{TP\text{-}4.1} \succ \mathbf{TP\text{-}2.1} \succ \mathbf{TP\text{-}2.2}$$

```
+--------------------------------------------------------------------------------------------------------+
|                                    POLICY RULES & DECISION MATRIX                                      |
+-----------+--------------------+-------------------------------------------+---------------------------+
| Clause ID | Policy Rule Name   | Quantitative Logic & Condition            | Resulting Decision Verdict|
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-6.1    | Business Purpose   | purpose_category in (PERSONAL, UNSPECIFIED)| ESCALATE to Manager       |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-1.1    | Advance Booking    | Domestic: days_ahead < 7                  | ESCALATE to Manager       |
|           |                    | International: days_ahead < 14            |                           |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-7.1    | Trip Frequency     | count_trips_in_quarter >= 3               | ESCALATE to Manager       |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-9.1    | High Value Approval| selected_fare > INR 100,000               | ESCALATE to Manager       |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-4.1    | Department Budget  | no compliant fare <= dept_remaining       | ESCALATE to Manager       |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-2.1    | Travel Class       | Cabin != Economy AND NOT (duration >= 6.0 | MODIFY (Change to Economy)|
|           |                    | AND employee_grade in [L5, L6, L7])       |                           |
+-----------+--------------------+-------------------------------------------+---------------------------+
| TP-2.2    | Fare Caps          | Requested fare > Cap AND compliant exists | MODIFY (Propose Fare <=Cap|
|           |                    | Cap: Domestic Economy = INR 15,000        |                           |
|           |                    | Cap: Intl Economy     = INR 90,000        |                           |
|           |                    | Cap: Intl Business    = INR 120,000       |                           |
+-----------+--------------------+-------------------------------------------+---------------------------+
| ALL CLEAR | Full Compliance    | All constraints satisfied without breach   | APPROVE                   |
+-----------+--------------------+-------------------------------------------+---------------------------+
```

### 6.3 Contracted Carrier Preference Algorithm (TP-5.1)
The selection of the "Best Compliant Fare" implements TP-5.1:
1. Identify all compliant fares matching cabin and cap constraints.
2. Separate into contracted carriers ($\mathcal{C}_{\text{contracted}}$) and non-contracted carriers ($\mathcal{C}_{\text{non-contracted}}$).
3. If $\mathcal{C}_{\text{contracted}} \neq \emptyset$:
   $$P_{\text{threshold}} = 0.90 \times \min_{f \in \mathcal{C}_{\text{contracted}}} \text{price}(f)$$
   Retain non-contracted option $f_{\text{nc}}$ **if and only if** $\text{price}(f_{\text{nc}}) \le P_{\text{threshold}}$.
4. Sort eligible candidates by price ascending; break ties by shortest flight duration.

---

# 7. GROUP ADD-ON: MANAGER-APPROVAL HUMAN-IN-THE-LOOP (HITL) WORKFLOW

### 7.1 Architecture of the Human Review Loop
When any escalation trigger fires, the system halts autonomous booking and transitions the request state to `PENDING_HUMAN`. The full audit context is packaged into `review_queue`.

```mermaid
sequenceDiagram
    autonumber
    actor Employee
    participant AM as Approval Manager
    participant DB as SQLite MemoryStore
    participant RQ as Review Queue
    actor Manager as Human Reviewer

    Employee->>AM: Submit Travel Request
    AM->>AM: Evaluate Precedence Rules -> Escalation Triggered
    AM->>RQ: Enqueue Request (Status: PENDING)
    AM->>DB: Log Audit Event: enqueued_for_review
    AM-->>Employee: Return AuditResult (Status: PENDING_HUMAN, routed_to_human=True)

    Note over RQ,Manager: Human Manager Interacts via Web UI or CLI
    Manager->>RQ: Inspect Pending Request & Policy Justification
    Manager->>AM: Submit Verdict (approve / reject / modify, note, reviewer)
    AM->>DB: Update Review Queue Record (Status: HUMAN_APPROVED)
    AM->>DB: Mutate Trip History Status (approved / rejected)
    alt Verdict is APPROVE or MODIFY
        AM->>DB: Deduct Fare from Department Budget Ledger
    end
    AM->>DB: Write Signed Audit Event: human_verdict_approve
    AM-->>Manager: Acknowledge State Mutation & Budget Update
```

### 7.2 Verification of Verdict Flow-Back
The verdict flows back through every layer of persistence:
1. **Request Status Mutation:** `review_queue.status` changes from `PENDING` to `HUMAN_APPROVED`, `HUMAN_REJECTED`, or `HUMAN_MODIFIED`.
2. **Trip History Insertion:** A persistent record is written to `trip_history` with `decided_by='human'` and the reviewer's identity.
3. **Budget Ledger Deduction:** If approved, the exact fare amount is atomically deducted from `dept_budget_ledger` (`remaining_inr = remaining_inr - fare`).
4. **Permanent Audit Log:** A signed entry `human_verdict_<action>` records the reviewer's username, timestamp, and justification note.

---

# 8. FREE-TIER SAFETY, RATE LIMITING & GRACEFUL DEGRADATION

### 8.1 Rate Limiting & Quota Management
Google Gemini free tier enforces a strict limit of 15 Requests Per Minute (RPM). Uncontrolled agent loops quickly trigger HTTP `429 Too Many Requests`. This system enforces four safety shields in `core/rate_limiter.py`:
1. **Asynchronous Token Bucket Rate Limiter:** Enforces a conservative default of 10 RPM across all concurrent agent routines.
2. **Exponential Backoff with Jitter:** On encountering HTTP 429 or 503, retries up to 5 times with randomized exponential backoff:
   $$t_{\text{wait}} = \min(60, 2^{\text{attempt}} + \text{uniform}(0, 1))$$
3. **SHA-256 Persistent Disk Caching:** LLM responses are cached in `llm_cache/` keyed by `sha256(model + prompt + schema_hint)`. Identical requests incur **zero API calls and zero latency**.
4. **Call Budget Cap:** Maximum 3 LLM calls per request (1 parse, 1 optional re-rank, 1 justification).

### 8.2 Seamless Zero-LLM Degradation Mode
If the Gemini API key is missing, invalid, or quota is exhausted mid-run, the system **does not crash**. It degrades gracefully to deterministic fallbacks:
- Request Parser switches to regex and dictionary lookup.
- Policy Retrieval skips LLM re-ranking and uses raw ChromaDB vector cosine scores.
- Approval Manager switches to deterministic template justification.
- **The final compliance decision is 100% unaffected**, maintaining identical decision accuracy.

---

# 9. EMPIRICAL EVALUATION BENCHMARK & EXPERIMENTAL RESULTS

### 9.1 Benchmark Methodology
The system was evaluated against the **10 standard benchmark evaluation cases** (`data/eval/eval_cases.json`). Each case presents a realistic enterprise travel scenario designed to probe specific policy clauses, class eligibility limits, fare caps, budget overruns, frequency limits, and clause conflicts.

### 9.2 Per-Case Evaluation Results Matrix

```
+======================================================================================================================+
|                                        BENCHMARK EVALUATION RESULTS (10/10)                                          |
+------+--------------------+--------------------+--------------------+----------+--------+------+---------+---------+
| Case | Scenario & Route   | Expected Outcome   | Actual System      | Decision | Clause | Fare | Latency | Status  |
| ID   | Description        | (Decision / Clause)| Result             | Correct  | Correct| Match| (sec)   | Check   |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 1    | E101: DEL->BOM     | APPROVE / TP-2.2   | APPROVE / TP-2.2   |    [x]   |   [x]  |  [x] |   1.30s | PASSED  |
|      | Economy, 20d ahead | Fare: INR 9,500    | Fare: INR 9,500    |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 2    | E101: DEL->BLR     | MODIFY / TP-2.1    | MODIFY / TP-2.1    |    [x]   |   [x]  |  [x] |   1.03s | PASSED  |
|      | Biz req (2.7h, L3) | Fare: INR 7,800    | Fare: INR 7,800    |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 3    | E101: DEL->GOI     | ESCALATE / TP-1.1  | ESCALATE / TP-1.1  |    [x]   |   [x]  |  [x] |   1.07s | PASSED  |
|      | Notice: 3d (< 7d)  | Fare: null         | Fare: null         |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 4    | E102: BLR->SIN     | APPROVE / TP-2.2   | APPROVE / TP-2.2   |    [x]   |   [x]  |  [x] |   1.13s | PASSED  |
|      | Intl, 30d notice   | Fare: INR 72,000   | Fare: INR 72,000   |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 5    | E102: DEL->HYD     | MODIFY / TP-2.2    | MODIFY / TP-2.2    |    [x]   |   [x]  |  [x] |   1.22s | PASSED  |
|      | Fare 18.5k (>15k)  | Fare: INR 11,500   | Fare: INR 11,500   |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 6    | E101: DEL->GOI     | ESCALATE / TP-6.1  | ESCALATE / TP-6.1  |    [x]   |   [x]  |  [x] |   1.20s | PASSED  |
|      | Family vacation    | Fare: null         | Fare: null         |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 7    | E103: DEL->LHR     | APPROVE / TP-2.1   | APPROVE / TP-2.1   |    [x]   |   [x]  |  [x] |   1.15s | PASSED  |
|      | Biz (9.0h, L6)     | Fare: INR 85,000   | Fare: INR 85,000   |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 8    | E104: BOM->DEL     | ESCALATE / TP-4.1  | ESCALATE / TP-4.1  |    [x]   |   [x]  |  [x] |   1.20s | PASSED  |
|      | Low Budget (INR 9k)| Fare: null         | Fare: null         |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 9    | E105: DEL->CCU     | ESCALATE / TP-7.1  | ESCALATE / TP-7.1  |    [x]   |   [x]  |  [x] |   1.24s | PASSED  |
|      | 4th trip in Q4     | Fare: null         | Fare: null         |          |        |      |         |         |
+------+--------------------+--------------------+--------------------+:--------:+:------:+:----:+--------:+:-------:+
| 10   | E103: DEL->JFK     | ESCALATE / TP-9.1  | ESCALATE / TP-9.1  |    [x]   |   [x]  |  [x] |   1.18s | PASSED  |
|      | Biz fare INR 145k  | Fare: null         | Fare: null         |          |        |      |         |         |
+======================================================================================================================+
```

### 9.3 Confusion Matrix Analysis

```
                      ACTUAL SYSTEM DECISION
                  APPROVE      MODIFY      ESCALATE
    APPROVE          3           0            0
EXPECTED 
    MODIFY           0           2            0
DECISION 
    ESCALATE         0           0            5
```

- **Accuracy:** $\frac{3 + 2 + 5}{10} = \mathbf{100.0\%}$
- **Precision (Approve / Modify / Escalate):** $1.00$ / $1.00$ / $1.00$
- **Recall (Approve / Modify / Escalate):** $1.00$ / $1.00$ / $1.00$
- **False Positive Rate:** $\mathbf{0.0\%}$ · **False Negative Rate:** $\mathbf{0.0\%}$

### 9.4 Deep Dive: Case 10 Precedence Conflict Resolution
Case 10 represents a critical test of conflict resolution. Employee E103 requests a Business class flight from Delhi to New York JFK at **INR 145,000**:
1. **Condition A:** Selected fare exceeds INR 100,000 $\rightarrow$ Triggers **TP-9.1** (High Value Approval).
2. **Condition B:** Selected fare exceeds Business International cap of INR 120,000 $\rightarrow$ Triggers **TP-2.2** (Fare Cap).
3. **Resolution via Precedence:** Under the strict precedence hierarchy:
   $$\mathbf{TP\text{-}9.1} \succ \text{TP-4.1} \succ \text{TP-2.1} \succ \mathbf{TP\text{-}2.2}$$
   TP-9.1 outranks TP-2.2. The system correctly identifies **TP-9.1** as the primary clause governing the escalation, while recording TP-2.2 in `secondary_clause_ids`.

---

# 10. SYSTEM IMPLEMENTATION, WEB UI/UX & DEPLOYMENT GUIDE

### 10.1 Full-Featured Web Application (Flask + Modern UI)
In addition to the Semantic Kernel CLI, the project includes an interactive web interface built with **Flask** (`app.py`) and a responsive single-page frontend (`templates/index.html`):
- **Dashboard Tab:** Live summary metrics (employees, trips, pending reviews, budget balances, audit events).
- **Audit Request Tab:** Text area with **1-click quick-fill buttons for all 10 benchmark cases**, decision banners, and real-time inter-agent audit trail.
- **Human Review Tab:** Manager approval loop with instant verdict submission (`approve`, `reject`, `modify`), reviewer notes, and automated ledger deduction.
- **Benchmark Evaluation Tab:** Interactive 1-click execution of the 10-case evaluation suite with per-case tables and confusion matrix.
- **Budgets & Employees Tab:** Interactive directory, progress bars for department funds, and trip history.
- **Policy & Precedence Tab:** Visual precedence diagram and clause reference.

```mermaid
graph LR
    subgraph Client ["Frontend (Browser)"]
        UI["Bootstrap 5 + FontAwesome SPA<br/>(templates/index.html)"]
    end

    subgraph Server ["Backend (Python Flask)"]
        API["Flask REST API Server<br/>(app.py)"]
        MGR["Approval Manager<br/>(Orchestrator)"]
    end

    subgraph Storage ["Persistent State"]
        SQL[("SQLite Store<br/>travel_auditor.db")]
        VEC[("ChromaDB<br/>chroma_db/")]
    end

    UI <-->|JSON over HTTP REST| API
    API <-->|asyncio event loop| MGR
    MGR <-->|read / write| SQL
    MGR <-->|vector search| VEC
```

### 10.2 Deployment & Execution Instructions

#### Step 1: Environment Setup & Activation
```powershell
# Navigate to project directory
Set-Location E:\4.1\CSE476\CA2_Project\travel_auditor

# Virtual environment is pre-configured
.\.venv\Scripts\Activate.ps1
```

#### Step 2: Configuration (.env)
Edit `.env` in the root of `travel_auditor/`:
```ini
# Required for Gemini LLM mode (free from https://aistudio.google.com/apikey)
GOOGLE_API_KEY=your_gemini_api_key_here

# Optional configuration (defaults automatically applied if left blank)
GEMINI_MODEL_NAME=gemini-2.0-flash
RATE_LIMIT_RPM=10
TODAY_DATE=2026-11-12
```

#### Step 3: Run the Interactive Web Application
```powershell
.\.venv\Scripts\python.exe app.py
```
Open **[http://127.0.0.1:5000](http://127.0.0.1:5000)** in any web browser.

#### Step 4: Run CLI Commands
```powershell
# Audit a single request via CLI
.\.venv\Scripts\python.exe main.py audit "I need to fly Delhi to Mumbai on 2 Dec 2026 for a client meeting. Economy. Employee E101."

# Run full 10-case benchmark evaluation
.\.venv\Scripts\python.exe eval/run_eval.py

# List pending reviews in human queue
.\.venv\Scripts\python.exe main.py review list

# Submit human manager verdict
.\.venv\Scripts\python.exe main.py review decide REQ-XXXXXXXX --verdict approve --note "Approved by Director"
```

#### Step 5: Execute Automated Test Suite
```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```
*Result: 57 passed in ~19.5 seconds.*

---

# 11. CONCLUSION, LEARNINGS & FUTURE ENHANCEMENTS

### 11.1 Key Technical Learnings
1. **Separation of Cognitive Perception vs Deterministic Judgment:** LLMs excel at transforming noisy natural language into structured data. However, compliance, cap calculations, and precedence ordering must remain in deterministic code to guarantee auditability and safety.
2. **Defensive RAG Design:** Chunks must correspond to semantic rule boundaries rather than arbitrary token lengths. Grounding each policy rule as an independent chunk enabled 100% retrieval precision.
3. **Asynchronous Architecture in Enterprise Desktops:** Windows IOCP event loops require careful handling when bridging synchronous web frameworks (Flask) and asynchronous multi-agent coordination (`asyncio`). Dedicated event loops per request thread provide complete isolation.

### 11.2 Project Links & Submission Metadata
- **GitHub Repository:** [https://github.com/YaswanthKumarMallela01/CSE476_CA2_CORPORATE-TRAVEL-POLICY-BOOKING-AUDITOR](https://github.com/YaswanthKumarMallela01/CSE476_CA2_CORPORATE-TRAVEL-POLICY-BOOKING-AUDITOR)
- **Presentation Slides:** [Google Slides Presentation](https://docs.google.com/presentation/d/10t75tNwbDFZmGRDx_L-PAPktqg1GbNYs/edit?usp=sharing&ouid=106909517235174732780&rtpof=true&sd=true)
- **Candidate:** Yaswanth Kumar Mallela (Reg. No. 12315067)
- **Course:** CSE476 (CA2, Group 15, Q15)

---
*Report successfully compiled and verified against project deliverables.*
