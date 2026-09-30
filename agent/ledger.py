"""The local SQLite ledger.

This is written to *before* anything is synced to Moramba's Postgres
(see README section 5) — it's the fast, always-available copy the agent
reads from for its own limit checks, and it's also the outbox queue for
the sync step: every record starts with `synced_at IS NULL` and stays
that way until `sync.py` confirms Moramba accepted it.

Amounts are stored as TEXT and handled as Decimal throughout — payment
amounts must never touch float arithmetic.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    rail TEXT NOT NULL,
    recipient TEXT NOT NULL,
    token TEXT NOT NULL,
    amount TEXT NOT NULL,
    chain_id INTEGER,
    status TEXT NOT NULL,
    tx_hash TEXT,
    signature TEXT,
    reason TEXT,
    receiving_agent_id TEXT,
    raw_request TEXT,
    raw_response TEXT,
    synced_at TEXT,
    sync_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments (created_at);
CREATE INDEX IF NOT EXISTS idx_payments_synced_at ON payments (synced_at);
"""

STATUS_SETTLED = "settled"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class PaymentRecord:
    id: int
    created_at: str
    rail: str
    recipient: str
    token: str
    amount: Decimal
    chain_id: int | None
    status: str
    tx_hash: str | None
    signature: str | None
    reason: str | None
    receiving_agent_id: str | None
    synced_at: str | None
    sync_attempts: int

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "PaymentRecord":
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            rail=row["rail"],
            recipient=row["recipient"],
            token=row["token"],
            amount=Decimal(row["amount"]),
            chain_id=row["chain_id"],
            status=row["status"],
            tx_hash=row["tx_hash"],
            signature=row["signature"],
            reason=row["reason"],
            receiving_agent_id=row["receiving_agent_id"],
            synced_at=row["synced_at"],
            sync_attempts=row["sync_attempts"],
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, db_path: str):
        # check_same_thread=False: FastAPI (and any other threaded caller)
        # runs sync route handlers in a worker thread pool, not the thread
        # that constructed this Ledger — SQLite's default same-thread
        # restriction would otherwise raise on every request.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_add_receiving_agent_id()
        self._conn.commit()

    def _migrate_add_receiving_agent_id(self) -> None:
        # `CREATE TABLE IF NOT EXISTS` above doesn't add a new column to an
        # already-existing local db file from before this column existed.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(payments)")}
        if "receiving_agent_id" not in columns:
            self._conn.execute("ALTER TABLE payments ADD COLUMN receiving_agent_id TEXT")

    def close(self) -> None:
        self._conn.close()

    def record(
        self,
        *,
        rail: str,
        recipient: str,
        token: str,
        amount: Decimal,
        status: str,
        chain_id: int | None = None,
        tx_hash: str | None = None,
        signature: str | None = None,
        reason: str | None = None,
        receiving_agent_id: str | None = None,
        raw_request: dict | None = None,
        raw_response: dict | None = None,
    ) -> PaymentRecord:
        created_at = _now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO payments
                (created_at, rail, recipient, token, amount, chain_id,
                 status, tx_hash, signature, reason, receiving_agent_id,
                 raw_request, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                rail,
                recipient,
                token,
                str(amount),
                chain_id,
                status,
                tx_hash,
                signature,
                reason,
                receiving_agent_id,
                json.dumps(raw_request) if raw_request is not None else None,
                json.dumps(raw_response) if raw_response is not None else None,
            ),
        )
        self._conn.commit()
        return self.get(cur.lastrowid)

    def get(self, record_id: int) -> PaymentRecord:
        row = self._conn.execute("SELECT * FROM payments WHERE id = ?", (record_id,)).fetchone()
        return PaymentRecord._from_row(row)

    def mark_synced(self, record_id: int) -> None:
        self._conn.execute(
            "UPDATE payments SET synced_at = ? WHERE id = ?", (_now_iso(), record_id)
        )
        self._conn.commit()

    def bump_sync_attempts(self, record_id: int) -> None:
        self._conn.execute(
            "UPDATE payments SET sync_attempts = sync_attempts + 1 WHERE id = ?",
            (record_id,),
        )
        self._conn.commit()

    def pending_sync(self) -> list[PaymentRecord]:
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE synced_at IS NULL ORDER BY id ASC"
        ).fetchall()
        return [PaymentRecord._from_row(r) for r in rows]

    def spent_since(
        self,
        since_iso: str,
        *,
        rail: str | None = None,
        token: str | None = None,
        recipient: str | None = None,
    ) -> Decimal:
        """Sum of settled amounts since `since_iso` — the number a limit
        check compares against. Scoped by `token` when given, since limits
        that mix tokens aren't meaningfully comparable; scoped by
        `recipient` for vendor-wise limit checks."""
        query = "SELECT amount FROM payments WHERE status = ? AND created_at >= ?"
        params: list = [STATUS_SETTLED, since_iso]
        if rail is not None:
            query += " AND rail = ?"
            params.append(rail)
        if token is not None:
            query += " AND token = ?"
            params.append(token)
        if recipient is not None:
            query += " AND recipient = ?"
            params.append(recipient)
        rows = self._conn.execute(query, params).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def count_since(self, since_iso: str, *, rail: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM payments WHERE status = ? AND created_at >= ?"
        params: list = [STATUS_SETTLED, since_iso]
        if rail is not None:
            query += " AND rail = ?"
            params.append(rail)
        row = self._conn.execute(query, params).fetchone()
        return int(row["n"])

    def history(
        self,
        *,
        since: str | None = None,
        recipient: str | None = None,
        rail: str | None = None,
        limit: int = 100,
    ) -> list[PaymentRecord]:
        query = "SELECT * FROM payments WHERE 1=1"
        params: list = []
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        if recipient is not None:
            query += " AND recipient = ?"
            params.append(recipient)
        if rail is not None:
            query += " AND rail = ?"
            params.append(rail)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [PaymentRecord._from_row(r) for r in rows]
