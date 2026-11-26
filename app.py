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
import llm_factory

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
    """Safely run async coroutine in Flask's thread and close pending client transports."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(asyncio.sleep(0.05))
        except Exception:
            pass
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
    req_cnt = mem.conn.execute("SELECT COUNT(*) as c FROM booking_requests").fetchone()["c"]
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
        "requests_count": req_cnt,
        "pending_reviews_count": pending_cnt,
        "audit_events_count": audit_cnt,
        "budgets": budgets,
        "today": str(config.TODAY),
        "llm_enabled": bool(config.GOOGLE_API_KEY),
        "model": llm_factory.get_active_model(),
    })


def _serialize_fare(f) -> Optional[dict]:
    if not f:
        return None
    return {
        "fare_id": f.fare_id,
        "airline": f.airline,
        "price_inr": f.price_inr,
        "cabin": f.cabin.value if hasattr(f.cabin, "value") else str(f.cabin),
        "duration_hours": f.duration_hours,
        "contracted": f.contracted,
        "refundable": f.refundable,
    }


@app.route("/api/audit", methods=["POST"])
def api_audit():
    """Audit a travel request through the 5-agent system (supports interactive follow-ups)."""
    data = request.get_json(force=True) or {}
    req_text = data.get("request_text", "").strip()
    if not req_text:
        return jsonify({"error": "Request text cannot be empty"}), 400

    use_llm = data.get("use_llm", bool(config.GOOGLE_API_KEY))
    allow_clarification = data.get("allow_clarification", True)
    employee_hint = data.get("employee_id") or None
    clarifications = data.get("clarifications") or None
    request_id = data.get("request_id") or None

    mem = get_memory()
    rag = get_rag()
    manager = ApprovalManager(mem, rag, use_llm=use_llm)

    start = time.time()
    result = run_async(
        manager.process_request(
            req_text,
            allow_clarification=allow_clarification,
            employee_hint=employee_hint,
            clarifications=clarifications,
            request_id=request_id,
        )
    )
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

    return jsonify({
        "status": "ok",
        "request_id": result.request_id,
        "decision": result.decision.value,
        "primary_clause_id": result.primary_clause_id,
        "secondary_clause_ids": result.secondary_clause_ids,
        "best_compliant_fare": _serialize_fare(result.best_compliant_fare),
        "recommended_fare": _serialize_fare(result.recommended_fare),
        "available_fares": [_serialize_fare(f) for f in (result.available_fares or [])],
        "justification": result.justification,
        "clarification_message": result.clarification_message,
        "follow_up_questions": result.follow_up_questions,
        "extracted_partial": result.extracted_partial,
        "routed_to_human": result.routed_to_human,
        "request_status": result.status.value,
        "raw_request_text": result.raw_request_text or req_text,
        "employee_id": result.employee_id,
        "employee_name": result.employee_name,
        "employee_department": result.employee_department,
        "employee_grade": result.employee_grade,
        "origin_iata": result.origin_iata,
        "origin_city": config.IATA_TO_CITY.get(result.origin_iata, result.origin_iata),
        "destination_iata": result.destination_iata,
        "destination_city": config.IATA_TO_CITY.get(result.destination_iata, result.destination_iata),
        "depart_date": result.depart_date,
        "return_date": result.return_date,
        "days_ahead": result.days_ahead,
        "purpose_text": result.purpose_text,
        "requested_class": result.requested_class,
        "requested_fare_inr": result.requested_fare_inr,
        "fare_cap_inr": result.fare_cap_inr,
        "dept_remaining_inr": result.dept_remaining_inr,
        "quarter_trip_count": result.quarter_trip_count,
        "elapsed_seconds": round(elapsed, 2),
        "audit_trail": audit_trail,
    })


@app.route("/api/reviews", methods=["GET"])
def api_reviews():
    """Return all review queue items enriched with employee, itinerary, budget, and fare details."""
    mem = get_memory()
    items = mem.get_review_queue()
    budgets_map = {
        r["dept"]: r["remaining_inr"]
        for r in mem.conn.execute("SELECT dept, remaining_inr FROM dept_budget_ledger").fetchall()
    }
    results = []
    for it in items:
        audit_res = json.loads(it.audit_result_json) if it.audit_result_json else {}

        # Backfill rich fields from audit_log if legacy item lacks them
        if not audit_res.get("origin_iata") and not audit_res.get("employee_id"):
            logs = mem.get_audit_log(it.request_id)
            for l in logs:
                if l.agent == "request_parser" and l.action == "parsed_request" and l.payload_json:
                    try:
                        p = json.loads(l.payload_json)
                        audit_res.setdefault("employee_id", p.get("employee_id", ""))
                        audit_res.setdefault("origin_iata", p.get("origin_iata", ""))
                        audit_res.setdefault("destination_iata", p.get("destination_iata", ""))
                        audit_res.setdefault("depart_date", p.get("depart_date", ""))
                        audit_res.setdefault("return_date", p.get("return_date"))
                        audit_res.setdefault("purpose_text", p.get("purpose_text", ""))
                        audit_res.setdefault("requested_class", p.get("requested_class", "economy"))
                        audit_res.setdefault("requested_fare_inr", p.get("requested_fare"))
                        audit_res.setdefault("raw_request_text", p.get("raw_text", ""))
                    except Exception:
                        pass

        emp_id = audit_res.get("employee_id", "")
        if emp_id:
            emp = mem.get_employee(emp_id)
            if emp:
                audit_res.setdefault("employee_name", emp.name)
                audit_res.setdefault("employee_department", emp.department)
                audit_res.setdefault("employee_grade", emp.grade)
                audit_res["live_dept_remaining_inr"] = budgets_map.get(emp.department, 0.0)

        results.append({
            "request_id": it.request_id,
            "status": it.status,
            "human_verdict": it.human_verdict,
            "human_note": it.human_note,
            "reviewed_at": it.reviewed_at,
            "audit_result": audit_res,
        })
    # Pending first, then newest resolved
    results.sort(key=lambda r: (0 if r["status"] == "PENDING" else 1, r["reviewed_at"] or ""), reverse=False)
    return jsonify({"reviews": results})


@app.route("/api/reviews/<request_id>/verdict", methods=["POST"])
def api_submit_verdict(request_id):
    """Submit a human review verdict (approve / reject / modify) with optional fare selection."""
    data = request.get_json(force=True) or {}
    verdict = data.get("verdict", "").lower()
    note = data.get("note", "").strip()
    reviewer = data.get("reviewer", "Manager").strip() or "Manager"
    fare_inr_input = data.get("fare_inr")
    airline_input = data.get("airline")

    if verdict not in ("approve", "reject", "modify"):
        return jsonify({"error": "Verdict must be one of: approve, reject, modify"}), 400

    mem = get_memory()
    item = mem.get_review_item(request_id)
    if not item:
        return jsonify({"error": f"Request {request_id} not found"}), 404

    fare_inr = None
    if fare_inr_input not in (None, ""):
        try:
            fare_inr = float(fare_inr_input)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid fare_inr amount"}), 400

    try:
        res = hitl.submit_verdict(
            request_id=request_id,
            verdict=verdict,
            note=note,
            reviewer=reviewer,
            memory=mem,
            fare_inr=fare_inr,
            airline=airline_input,
        )
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
        # Isolated in-memory store per case so live user bookings & reviews are preserved
        mem = MemoryStore(":memory:")
        mem.seed_from_json(config.EMPLOYEES_PATH, config.BUDGETS_PATH, config.TRIP_HISTORY_SEED_PATH)
        rag = get_rag()
        mgr = ApprovalManager(mem, rag, use_llm=use_llm)

        c_start = time.time()
        res = run_async(mgr.process_request(case["request_text"], allow_clarification=False))
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

    total = len(results)
    dec_acc = sum(1 for r in results if r["decision_ok"]) / total
    cls_acc = sum(1 for r in results if r["clause_ok"]) / total
    fare_acc = sum(1 for r in results if r["fare_ok"]) / total
    avg_lat = sum(r["latency"] for r in results) / total

    # Confusion matrix
    decisions = ["APPROVE", "MODIFY", "ESCALATE"]
    confusion = {(e, a): 0 for e in decisions for a in decisions}
    for r in results:
        if (r["expected_decision"], r["actual_decision"]) in confusion:
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
    """Return trip history enriched with employee name and department."""
    mem = get_memory()
    rows = mem.conn.execute(
        """SELECT t.*, COALESCE(e.name, t.employee_id) as employee_name,
                  COALESCE(e.department, '') as department,
                  COALESCE(e.grade, '') as grade
           FROM trip_history t
           LEFT JOIN employees e ON UPPER(t.employee_id) = UPPER(e.employee_id)
           ORDER BY t.depart_date DESC, t.created_at DESC"""
    ).fetchall()
    return jsonify({"trips": [dict(r) for r in rows]})


@app.route("/api/history", methods=["GET"])
def api_history():
    """Return full rich history of all audited booking requests, prices, decisions, and human verdicts."""
    mem = get_memory()
    rows = mem.get_booking_history(limit=100)
    # Include legacy trip_history rows that are not yet in booking_requests (seed trips or prior DB rows)
    seen_ids = {r["request_id"] for r in rows}
    trips = mem.conn.execute(
        """SELECT t.*, COALESCE(e.name, t.employee_id) as resolved_name,
                  COALESCE(e.department, '') as resolved_dept,
                  COALESCE(e.grade, '') as resolved_grade
           FROM trip_history t
           LEFT JOIN employees e ON UPPER(t.employee_id) = UPPER(e.employee_id)
           ORDER BY t.depart_date DESC"""
    ).fetchall()
    for t in trips:
        if t["trip_id"] not in seen_ids:
            rows.append({
                "request_id": t["trip_id"],
                "employee_id": t["employee_id"],
                "resolved_name": t["resolved_name"],
                "resolved_dept": t["resolved_dept"],
                "resolved_grade": t["resolved_grade"],
                "raw_text": "Historical Seed / Prior Record",
                "origin_iata": t["origin"],
                "destination_iata": t["destination"],
                "depart_date": t["depart_date"],
                "return_date": "",
                "purpose": "corporate travel",
                "requested_class": "economy",
                "decision": "APPROVE" if t["status"] in ("approved", "completed") else ("ESCALATE" if t["status"] == "pending" else "ESCALATE"),
                "primary_clause_id": t["clause_id"] or "TP-2.2",
                "status": t["status"].upper(),
                "fare_inr": t["fare_inr"],
                "airline": "-",
                "justification": "",
                "human_verdict": "",
                "human_note": "",
                "reviewed_by": t["decided_by"] or "system",
                "created_at": t["created_at"],
            })
    return jsonify({"history": rows})


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
