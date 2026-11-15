"""
Flask Backend Application for the Corporate Travel Policy Booking Auditor.
Provides RESTful APIs and a responsive, full-featured web interface.

Run: python app.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from flask import Flask, jsonify, render_template, request

import config
from agents.approval_manager import ApprovalManager
from core import hitl
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import Decision

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=str(PROJECT_DIR / "templates"), static_folder=str(PROJECT_DIR / "static"))
app.config["JSON_SORT_KEYS"] = False

# Global shared services
_memory: Optional[MemoryStore] = None
_rag: Optional[PolicyRAG] = None


def get_memory() -> MemoryStore:
    global _memory
    if _memory is None:
        _memory = MemoryStore(config.SQLITE_DB_PATH)
        row = _memory.conn.execute("SELECT COUNT(*) as cnt FROM employees").fetchone()
        if row["cnt"] == 0:
            _memory.seed_from_json(
                config.EMPLOYEES_PATH,
                config.BUDGETS_PATH,
                config.TRIP_HISTORY_SEED_PATH,
            )
    return _memory


def get_rag() -> PolicyRAG:
    global _rag
    if _rag is None:
        _rag = PolicyRAG(
            persist_dir=config.CHROMA_PERSIST_DIR,
            embedding_model=config.EMBEDDING_MODEL_NAME,
            top_k=config.RAG_TOP_K,
        )
        _rag.ingest(config.POLICY_PATH)
    return _rag


def run_async(coro):
    """Safely run async coroutine in Flask's thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Web Page Route
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Render the main single-page application."""
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Return dashboard summary statistics."""
    mem = get_memory()
    emp_cnt = mem.conn.execute("SELECT COUNT(*) as c FROM employees").fetchone()["c"]
    trip_cnt = mem.conn.execute("SELECT COUNT(*) as c FROM trip_history").fetchone()["c"]
    pending_cnt = mem.conn.execute("SELECT COUNT(*) as c FROM review_queue WHERE status='PENDING'").fetchone()["c"]
    audit_cnt = mem.conn.execute("SELECT COUNT(*) as c FROM audit_log").fetchone()["c"]
    
    budgets = [
        {"dept": r["dept"], "remaining_inr": r["remaining_inr"]}
        for r in mem.conn.execute("SELECT dept, remaining_inr FROM dept_budget_ledger ORDER BY dept").fetchall()
    ]
    
    return jsonify({
        "status": "ok",
        "employees_count": emp_cnt,
        "trips_count": trip_cnt,
        "pending_reviews_count": pending_cnt,
        "audit_events_count": audit_cnt,
        "budgets": budgets,
        "today": str(config.TODAY),
        "llm_enabled": bool(config.GOOGLE_API_KEY),
        "model": config.GEMINI_MODEL_NAME,
    })


@app.route("/api/audit", methods=["POST"])
def api_audit():
    """Audit a travel request through the 5-agent system."""
    data = request.get_json(force=True) or {}
    req_text = data.get("request_text", "").strip()
    if not req_text:
        return jsonify({"error": "Request text cannot be empty"}), 400

    use_llm = data.get("use_llm", bool(config.GOOGLE_API_KEY))
    mem = get_memory()
    rag = get_rag()
    manager = ApprovalManager(mem, rag, use_llm=use_llm)

    start = time.time()
    result = run_async(manager.process_request(req_text))
    elapsed = time.time() - start

    # Retrieve audit trail for this request
    logs = mem.get_audit_log(result.request_id)
    audit_trail = [
        {
            "ts": str(entry.ts or ""),
            "agent": entry.agent,
            "action": entry.action,
            "payload": json.loads(entry.payload_json) if entry.payload_json.startswith("{") else entry.payload_json,
        }
        for entry in logs
    ]

    fare_dict = None
    if result.best_compliant_fare:
        f = result.best_compliant_fare
        fare_dict = {
            "fare_id": f.fare_id,
            "airline": f.airline,
            "price_inr": f.price_inr,
            "cabin": f.cabin.value,
            "duration_hours": f.duration_hours,
            "contracted": f.contracted,
            "refundable": f.refundable,
        }

    return jsonify({
        "status": "ok",
        "request_id": result.request_id,
        "decision": result.decision.value,
        "primary_clause_id": result.primary_clause_id,
        "secondary_clause_ids": result.secondary_clause_ids,
        "best_compliant_fare": fare_dict,
        "justification": result.justification,
        "routed_to_human": result.routed_to_human,
        "request_status": result.status.value,
        "elapsed_seconds": round(elapsed, 2),
        "audit_trail": audit_trail,
    })


@app.route("/api/reviews", methods=["GET"])
def api_reviews():
    """Return all review queue items."""
    mem = get_memory()
    items = mem.get_review_queue()
    results = []
    for it in items:
        audit_res = json.loads(it.audit_result_json) if it.audit_result_json else {}
        results.append({
            "request_id": it.request_id,
            "status": it.status,
            "human_verdict": it.human_verdict,
            "human_note": it.human_note,
            "reviewed_at": it.reviewed_at,
            "audit_result": audit_res,
        })
    return jsonify({"reviews": results})


@app.route("/api/reviews/<request_id>/verdict", methods=["POST"])
def api_submit_verdict(request_id):
    """Submit a human review verdict (approve / reject / modify)."""
    data = request.get_json(force=True) or {}
    verdict = data.get("verdict", "").lower()
    note = data.get("note", "").strip()
    reviewer = data.get("reviewer", "admin").strip()

    if verdict not in ("approve", "reject", "modify"):
        return jsonify({"error": "Verdict must be one of: approve, reject, modify"}), 400

    mem = get_memory()
    item = mem.get_review_item(request_id)
    if not item:
        return jsonify({"error": f"Request {request_id} not found"}), 404

    # Extract fare from audit result for budget deduction
    fare_inr = None
    if item.audit_result_json:
        try:
            aud = json.loads(item.audit_result_json)
            if aud.get("best_compliant_fare"):
                fare_inr = aud["best_compliant_fare"].get("price_inr")
        except Exception:
            pass

    try:
        res = hitl.submit_verdict(request_id, verdict, note, reviewer, mem, fare_inr)
        return jsonify({"status": "ok", "result": res})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/eval/run", methods=["POST"])
def api_run_eval():
    """Run the 10 ground-truth evaluation benchmark cases."""
    data = request.get_json(silent=True) or {}
    use_llm = data.get("use_llm", False)

    with open(config.EVAL_CASES_PATH) as f:
        cases = json.load(f)

    results = []
    total_start = time.time()

    for case in cases:
        # Isolated memory per case
        mem = MemoryStore(config.SQLITE_DB_PATH)
        mem.reset_to_seed(config.EMPLOYEES_PATH, config.BUDGETS_PATH, config.TRIP_HISTORY_SEED_PATH)
        rag = get_rag()
        mgr = ApprovalManager(mem, rag, use_llm=use_llm)

        c_start = time.time()
        res = run_async(mgr.process_request(case["request_text"]))
        c_lat = time.time() - c_start

        actual_fare = res.best_compliant_fare.price_inr if res.best_compliant_fare else None
        expected_fare = case.get("expected_best_fare_inr")

        dec_ok = res.decision.value == case["expected_decision"]
        cls_ok = res.primary_clause_id == case["expected_primary_clause"]

        if expected_fare is None and case["expected_decision"] == "ESCALATE":
            fare_ok = True
        else:
            fare_ok = (actual_fare == expected_fare) or (actual_fare is None and expected_fare is None)

        results.append({
            "case_id": case["case_id"],
            "request_text": case["request_text"],
            "expected_decision": case["expected_decision"],
            "actual_decision": res.decision.value,
            "decision_ok": dec_ok,
            "expected_clause": case["expected_primary_clause"],
            "actual_clause": res.primary_clause_id,
            "clause_ok": cls_ok,
            "expected_fare": expected_fare,
            "actual_fare": actual_fare,
            "fare_ok": fare_ok,
            "latency": round(c_lat, 2),
            "justification": res.justification,
        })
        mem.close()

    # Reconnect memory
    global _memory
    _memory = None
    get_memory()

    total = len(results)
    dec_acc = sum(1 for r in results if r["decision_ok"]) / total
    cls_acc = sum(1 for r in results if r["clause_ok"]) / total
    fare_acc = sum(1 for r in results if r["fare_ok"]) / total
    avg_lat = sum(r["latency"] for r in results) / total

    # Confusion matrix
    decisions = ["APPROVE", "MODIFY", "ESCALATE"]
    confusion = {(e, a): 0 for e in decisions for a in decisions}
    for r in results:
        confusion[(r["expected_decision"], r["actual_decision"])] += 1

    return jsonify({
        "status": "ok",
        "results": results,
        "metrics": {
            "decision_accuracy": round(dec_acc * 100, 1),
            "clause_correctness": round(cls_acc * 100, 1),
            "fare_accuracy": round(fare_acc * 100, 1),
            "avg_latency": round(avg_lat, 2),
            "total_elapsed": round(time.time() - total_start, 2),
        },
        "confusion_matrix": {f"{e}->{a}": confusion[(e, a)] for e, a in confusion},
    })


@app.route("/api/employees", methods=["GET"])
def api_employees():
    """Return employee list."""
    mem = get_memory()
    rows = mem.conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
    return jsonify({"employees": [dict(r) for r in rows]})


@app.route("/api/budgets", methods=["GET"])
def api_budgets():
    """Return department budget ledger."""
    mem = get_memory()
    rows = mem.conn.execute("SELECT * FROM dept_budget_ledger ORDER BY dept").fetchall()
    return jsonify({"budgets": [dict(r) for r in rows]})


@app.route("/api/trips", methods=["GET"])
def api_trips():
    """Return trip history."""
    mem = get_memory()
    rows = mem.conn.execute("SELECT * FROM trip_history ORDER BY depart_date DESC").fetchall()
    return jsonify({"trips": [dict(r) for r in rows]})


@app.route("/api/audit-log", methods=["GET"])
def api_audit_log():
    """Return audit log entries."""
    mem = get_memory()
    req_id = request.args.get("request_id")
    agent = request.args.get("agent")
    limit = int(request.args.get("limit", 100))

    query = "SELECT * FROM audit_log"
    params = []
    clauses = []
    if req_id:
        clauses.append("request_id = ?")
        params.append(req_id)
    if agent:
        clauses.append("agent = ?")
        params.append(agent)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    rows = mem.conn.execute(query, params).fetchall()
    entries = []
    for r in rows:
        payload = r["payload_json"]
        if payload and (payload.startswith("{") or payload.startswith("[")):
            try:
                payload = json.loads(payload)
            except Exception:
                pass
        entries.append({
            "id": r["id"],
            "request_id": r["request_id"],
            "agent": r["agent"],
            "action": r["action"],
            "payload": payload,
            "ts": r["ts"],
        })
    return jsonify({"logs": entries})


@app.route("/api/policy", methods=["GET"])
def api_policy():
    """Return policy clauses and precedence order."""
    policy_content = ""
    if config.POLICY_PATH.exists():
        policy_content = config.POLICY_PATH.read_text(encoding="utf-8")
    return jsonify({
        "policy_markdown": policy_content,
        "precedence": ["TP-6.1", "TP-1.1", "TP-7.1", "TP-9.1", "TP-4.1", "TP-2.1", "TP-2.2"],
    })


@app.route("/api/reset-seed", methods=["POST"])
def api_reset_seed():
    """Reset database to initial seed data."""
    mem = get_memory()
    mem.reset_to_seed(config.EMPLOYEES_PATH, config.BUDGETS_PATH, config.TRIP_HISTORY_SEED_PATH)
    return jsonify({"status": "ok", "message": "Memory reset to seed data"})


if __name__ == "__main__":
    print(f"\n🚀 Travel Auditor Flask Server starting on http://127.0.0.1:5000 ...")
    app.run(host="0.0.0.0", port=5000, debug=False)
