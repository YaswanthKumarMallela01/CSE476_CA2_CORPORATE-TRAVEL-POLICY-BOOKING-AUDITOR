"""
Shared SQLite memory store for the Travel Auditor system.
All agents interact with the database ONLY through this MemoryStore class.
Tables: employees, trip_history, dept_budget_ledger, audit_log, review_queue.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.schemas import (
    AuditLogEntry,
    AuditResult,
    Employee,
    ReviewQueueItem,
    TripRecord,
    TripStatus,
)

logger = logging.getLogger(__name__)


class MemoryStore:
    """Centralised SQLite store injected into every agent."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    # ------------------------------------------------------------------
    # Schema creation
    # ------------------------------------------------------------------
    def _create_tables(self) -> None:
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                employee_id TEXT PRIMARY KEY,
                name TEXT,
                grade TEXT,
                department TEXT,
                email TEXT
            );
            CREATE TABLE IF NOT EXISTS trip_history (
                trip_id TEXT PRIMARY KEY,
                employee_id TEXT,
                origin TEXT,
                destination TEXT,
                depart_date TEXT,
                fare_inr REAL,
                status TEXT,
                clause_id TEXT DEFAULT '',
                decided_by TEXT DEFAULT 'system',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS dept_budget_ledger (
                dept TEXT PRIMARY KEY,
                remaining_inr REAL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                agent TEXT,
                action TEXT,
                payload_json TEXT DEFAULT '',
                ts TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS review_queue (
                request_id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'PENDING',
                human_verdict TEXT,
                human_note TEXT,
                reviewed_at TEXT,
                audit_result_json TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS booking_requests (
                request_id TEXT PRIMARY KEY,
                employee_id TEXT DEFAULT '',
                employee_name TEXT DEFAULT '',
                department TEXT DEFAULT '',
                grade TEXT DEFAULT '',
                raw_text TEXT DEFAULT '',
                origin_iata TEXT DEFAULT '',
                destination_iata TEXT DEFAULT '',
                depart_date TEXT DEFAULT '',
                return_date TEXT DEFAULT '',
                purpose TEXT DEFAULT '',
                requested_class TEXT DEFAULT '',
                decision TEXT DEFAULT '',
                primary_clause_id TEXT DEFAULT '',
                status TEXT DEFAULT '',
                fare_inr REAL DEFAULT 0,
                airline TEXT DEFAULT '',
                justification TEXT DEFAULT '',
                human_verdict TEXT DEFAULT '',
                human_note TEXT DEFAULT '',
                reviewed_by TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Seeding from JSON
    # ------------------------------------------------------------------
    def seed_from_json(
        self,
        employees_path: Path,
        budgets_path: Path,
        trip_history_path: Path,
    ) -> None:
        """Load initial data from JSON files."""
        with open(employees_path) as f:
            employees = json.load(f)
        for emp in employees:
            self.conn.execute(
                "INSERT OR REPLACE INTO employees VALUES (?,?,?,?,?)",
                (emp["employee_id"], emp["name"], emp["grade"], emp["department"], emp["email"]),
            )

        with open(budgets_path) as f:
            budgets = json.load(f)
        for b in budgets:
            self.conn.execute(
                "INSERT OR REPLACE INTO dept_budget_ledger (dept, remaining_inr) VALUES (?,?)",
                (b["department"], b["remaining_inr"]),
            )

        with open(trip_history_path) as f:
            trips = json.load(f)
        for t in trips:
            self.conn.execute(
                "INSERT OR REPLACE INTO trip_history (trip_id, employee_id, origin, destination, depart_date, fare_inr, status, clause_id, decided_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (t["trip_id"], t["employee_id"], t["origin"], t["destination"],
                 t["depart_date"], t["fare_inr"], t["status"], t.get("clause_id", ""), t.get("decided_by", "system")),
            )
        self.conn.commit()
        logger.info("Seeded memory: %d employees, %d budgets, %d trips", len(employees), len(budgets), len(trips))

    def reset_to_seed(
        self,
        employees_path: Path,
        budgets_path: Path,
        trip_history_path: Path,
    ) -> None:
        """Drop all data and re-seed for evaluation isolation."""
        cur = self.conn.cursor()
        for table in ("employees", "trip_history", "dept_budget_ledger", "audit_log", "review_queue", "booking_requests"):
            cur.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.seed_from_json(employees_path, budgets_path, trip_history_path)

    # ------------------------------------------------------------------
    # Employee operations
    # ------------------------------------------------------------------
    def get_employee(self, employee_id: str) -> Optional[Employee]:
        row = self.conn.execute(
            "SELECT * FROM employees WHERE UPPER(employee_id) = UPPER(?)", (employee_id.strip(),)
        ).fetchone()
        if row is None:
            return None
        return Employee(**dict(row))

    def get_all_employees(self) -> list[Employee]:
        rows = self.conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
        return [Employee(**dict(r)) for r in rows]

    def get_employee_by_name_or_id(self, query: str) -> Optional[Employee]:
        """Resolve an employee by exact ID, full name, or unique first-name/email match."""
        q = query.strip()
        if not q:
            return None
        emp = self.get_employee(q)
        if emp:
            return emp
        ql = q.lower()
        for e in self.get_all_employees():
            if e.name.lower() == ql or e.email.lower() == ql:
                return e
        for e in self.get_all_employees():
            parts = e.name.lower().split()
            if ql in parts or ql in e.name.lower():
                return e
        return None

    def write_employee_access(self, employee_id: str, request_id: str) -> None:
        """Log that an employee profile was accessed (audit trail)."""
        self.write_audit_log(request_id, "memory", "read_employee", json.dumps({"employee_id": employee_id}))

    # ------------------------------------------------------------------
    # Trip history operations
    # ------------------------------------------------------------------
    def get_trips_for_employee(self, employee_id: str) -> list[TripRecord]:
        rows = self.conn.execute(
            "SELECT * FROM trip_history WHERE employee_id = ? ORDER BY depart_date DESC",
            (employee_id,),
        ).fetchall()
        return [TripRecord(**dict(r)) for r in rows]

    def count_trips_in_quarter(self, employee_id: str, ref_date: date) -> int:
        """Count approved/completed trips for an employee in the quarter containing ref_date."""
        q_start, q_end = _quarter_range(ref_date)
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM trip_history WHERE employee_id = ? AND status IN ('approved','completed') AND depart_date >= ? AND depart_date <= ?",
            (employee_id, q_start.isoformat(), q_end.isoformat()),
        ).fetchone()
        return row["cnt"] if row else 0

    def add_trip(self, trip: TripRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trip_history (trip_id, employee_id, origin, destination, depart_date, fare_inr, status, clause_id, decided_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (trip.trip_id, trip.employee_id, trip.origin, trip.destination,
             trip.depart_date.isoformat(), trip.fare_inr, trip.status.value, trip.clause_id, trip.decided_by),
        )
        self.conn.commit()

    def update_trip_status(
        self,
        trip_id: str,
        status: TripStatus,
        fare_inr: Optional[float] = None,
        decided_by: Optional[str] = None,
    ) -> None:
        if fare_inr is not None and decided_by is not None:
            self.conn.execute(
                "UPDATE trip_history SET status = ?, fare_inr = ?, decided_by = ? WHERE trip_id = ?",
                (status.value, fare_inr, decided_by, trip_id),
            )
        elif fare_inr is not None:
            self.conn.execute(
                "UPDATE trip_history SET status = ?, fare_inr = ? WHERE trip_id = ?",
                (status.value, fare_inr, trip_id),
            )
        elif decided_by is not None:
            self.conn.execute(
                "UPDATE trip_history SET status = ?, decided_by = ? WHERE trip_id = ?",
                (status.value, decided_by, trip_id),
            )
        else:
            self.conn.execute(
                "UPDATE trip_history SET status = ? WHERE trip_id = ?",
                (status.value, trip_id),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Rich booking requests history operations
    # ------------------------------------------------------------------
    def save_booking_request(self, result: AuditResult) -> None:
        """Persist full audit request details for user-facing history and reviews."""
        fare_obj = result.best_compliant_fare or result.recommended_fare
        fare_price = fare_obj.price_inr if fare_obj else (result.requested_fare_inr or 0.0)
        airline = fare_obj.airline if fare_obj else ""
        self.conn.execute(
            """INSERT OR REPLACE INTO booking_requests (
                request_id, employee_id, employee_name, department, grade,
                raw_text, origin_iata, destination_iata, depart_date, return_date,
                purpose, requested_class, decision, primary_clause_id, status,
                fare_inr, airline, justification
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.request_id,
                result.employee_id,
                result.employee_name,
                result.employee_department,
                result.employee_grade,
                result.raw_request_text,
                result.origin_iata,
                result.destination_iata,
                result.depart_date or "",
                result.return_date or "",
                result.purpose_text,
                result.requested_class,
                result.decision.value,
                result.primary_clause_id,
                result.status.value,
                fare_price,
                airline,
                result.justification,
            ),
        )
        self.conn.commit()

    def update_booking_request_verdict(
        self,
        request_id: str,
        final_status: str,
        verdict: str,
        note: str,
        reviewer: str,
        fare_inr: Optional[float] = None,
        airline: Optional[str] = None,
    ) -> None:
        """Update booking_requests record when a human manager submits a verdict."""
        if fare_inr is not None and airline:
            self.conn.execute(
                """UPDATE booking_requests
                   SET status=?, human_verdict=?, human_note=?, reviewed_by=?, fare_inr=?, airline=?
                   WHERE request_id=?""",
                (final_status, verdict, note, reviewer, fare_inr, airline, request_id),
            )
        elif fare_inr is not None:
            self.conn.execute(
                """UPDATE booking_requests
                   SET status=?, human_verdict=?, human_note=?, reviewed_by=?, fare_inr=?
                   WHERE request_id=?""",
                (final_status, verdict, note, reviewer, fare_inr, request_id),
            )
        else:
            self.conn.execute(
                """UPDATE booking_requests
                   SET status=?, human_verdict=?, human_note=?, reviewed_by=?
                   WHERE request_id=?""",
                (final_status, verdict, note, reviewer, request_id),
            )
        self.conn.commit()

    def get_booking_history(self, limit: int = 100) -> list[dict]:
        """Return full audit/booking request history joined with employee info."""
        rows = self.conn.execute(
            """SELECT br.*, COALESCE(NULLIF(br.employee_name, ''), e.name, br.employee_id) as resolved_name,
                      COALESCE(NULLIF(br.department, ''), e.department, '') as resolved_dept,
                      COALESCE(NULLIF(br.grade, ''), e.grade, '') as resolved_grade
               FROM booking_requests br
               LEFT JOIN employees e ON br.employee_id = e.employee_id
               ORDER BY br.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Budget operations
    # ------------------------------------------------------------------
    def get_department_budget(self, department: str) -> float:
        row = self.conn.execute(
            "SELECT remaining_inr FROM dept_budget_ledger WHERE dept = ?", (department,)
        ).fetchone()
        return row["remaining_inr"] if row else 0.0

    def deduct_budget(self, department: str, amount: float) -> None:
        self.conn.execute(
            "UPDATE dept_budget_ledger SET remaining_inr = remaining_inr - ?, updated_at = datetime('now') WHERE dept = ?",
            (amount, department),
        )
        self.conn.commit()
        logger.info("Deducted %.0f from %s budget", amount, department)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def write_audit_log(self, request_id: str, agent: str, action: str, payload: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log (request_id, agent, action, payload_json) VALUES (?,?,?,?)",
            (request_id, agent, action, payload),
        )
        self.conn.commit()

    def get_audit_log(self, request_id: str) -> list[AuditLogEntry]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE request_id = ? ORDER BY ts", (request_id,)
        ).fetchall()
        return [AuditLogEntry(**dict(r)) for r in rows]

    def get_all_audit_log(self) -> list[AuditLogEntry]:
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY ts").fetchall()
        return [AuditLogEntry(**dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------
    def enqueue_review(self, item: ReviewQueueItem) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO review_queue (request_id, status, audit_result_json) VALUES (?,?,?)",
            (item.request_id, item.status, item.audit_result_json),
        )
        self.conn.commit()

    def get_review_queue(self, status: Optional[str] = None) -> list[ReviewQueueItem]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM review_queue WHERE status = ?", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM review_queue").fetchall()
        return [ReviewQueueItem(**dict(r)) for r in rows]

    def get_review_item(self, request_id: str) -> Optional[ReviewQueueItem]:
        row = self.conn.execute(
            "SELECT * FROM review_queue WHERE request_id = ?", (request_id,)
        ).fetchone()
        return ReviewQueueItem(**dict(row)) if row else None

    def update_review(self, request_id: str, verdict: str, note: str, reviewer: str) -> None:
        """Update a review queue item with a human verdict."""
        now = datetime.now().isoformat()
        status_map = {"approve": "HUMAN_APPROVED", "reject": "HUMAN_REJECTED", "modify": "HUMAN_MODIFIED"}
        new_status = status_map.get(verdict, "PENDING")
        self.conn.execute(
            "UPDATE review_queue SET status=?, human_verdict=?, human_note=?, reviewed_at=? WHERE request_id=?",
            (new_status, verdict, note, now, request_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------
    def close(self) -> None:
        self.conn.close()


def _quarter_range(d: date) -> tuple[date, date]:
    """Return (start, end) dates for the calendar quarter containing d."""
    q = (d.month - 1) // 3
    start_month = q * 3 + 1
    end_month = start_month + 2
    if end_month == 12:
        end_date = date(d.year, 12, 31)
    else:
        end_date = date(d.year, end_month + 1, 1).replace(day=1)
        from datetime import timedelta
        end_date = end_date - timedelta(days=1)
    start_date = date(d.year, start_month, 1)
    return start_date, end_date
