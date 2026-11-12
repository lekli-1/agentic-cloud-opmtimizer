import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from optimizer.models import Evidence, LLMCall, Recommendation, RecommendationStatus, utc_now


class Store:
    """Keep full Pydantic records as JSON, with SQL columns for queries and indexes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Give each worker-thread operation its own transaction and connection.

        SQLite's connection context commits or rolls back, but does not close
        the connection; closing() handles that separately.
        """
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # WAL lets API reads continue while another connection writes.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    workload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'executing', 'executed', 'failed', 'dismissed')
                    ),
                    created_at TEXT NOT NULL,
                    llm_call_id TEXT NOT NULL REFERENCES llm_calls(id),
                    document TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_workload
                    ON recommendations(namespace, workload) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS recommendation_status
                    ON recommendations(status, created_at);
            """)

    def recommendations(
        self, status: RecommendationStatus | None = None, limit: int | None = 100, offset: int = 0
    ) -> list[Recommendation]:
        query = "SELECT document FROM recommendations"
        parameters: list[str | int] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC, id"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.extend([limit, offset])
        with self.connection() as connection:
            return [
                Recommendation.model_validate_json(row["document"])
                for row in connection.execute(query, parameters)
            ]

    @staticmethod
    def _dismiss(
        connection: sqlite3.Connection, recommendation: Recommendation, reason: str, now: datetime
    ) -> None:
        """Keep the indexed status and full JSON record in sync."""
        dismissed = recommendation.model_copy(
            update={"status": "dismissed", "dismissed_reason": reason, "updated_at": now}
        )
        connection.execute(
            "UPDATE recommendations SET status = 'dismissed', document = ? "
            "WHERE id = ? AND status = 'pending'",
            (dismissed.model_dump_json(), recommendation.id),
        )

    def dismiss(self, reasons: dict[str, str]) -> None:
        now = utc_now()
        with self.connection() as connection:
            for recommendation_id, reason in reasons.items():
                row = connection.execute(
                    "SELECT document FROM recommendations WHERE id = ? AND status = 'pending'",
                    (recommendation_id,),
                ).fetchone()
                if row is not None:
                    self._dismiss(
                        connection, Recommendation.model_validate_json(row["document"]), reason, now
                    )

    def save_analysis(self, call: LLMCall, recommendations: list[Recommendation]) -> None:
        """Commit the call, replacements and new proposals as one transaction.

        Calls with no accepted recommendations still need an audit record.
        """
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO llm_calls(id, created_at, document) VALUES (?, ?, ?)",
                (call.id, call.created_at.isoformat(), call.model_dump_json()),
            )
            for recommendation in recommendations:
                spec = recommendation.evidence.workload.spec
                previous = connection.execute(
                    "SELECT document FROM recommendations "
                    "WHERE namespace = ? AND workload = ? AND status = 'pending'",
                    (spec.namespace, spec.name),
                ).fetchone()
                if previous is not None:
                    self._dismiss(
                        connection,
                        Recommendation.model_validate_json(previous["document"]),
                        f"Replaced by recommendation {recommendation.id}",
                        recommendation.created_at,
                    )
                connection.execute(
                    "INSERT INTO recommendations "
                    "(id, namespace, workload, status, created_at, llm_call_id, document) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        recommendation.id, spec.namespace, spec.name, recommendation.status,
                        recommendation.created_at.isoformat(), call.id,
                        recommendation.model_dump_json(),
                    ),
                )

    def latest_attempts(self) -> dict[tuple[str, str], Evidence]:
        """Restore failed as well as successful attempts to avoid automatic rebilling."""
        attempts: dict[tuple[str, str], Evidence] = {}
        with self.connection() as connection:
            for row in connection.execute("SELECT document FROM llm_calls ORDER BY created_at DESC"):
                call = LLMCall.model_validate_json(row["document"])
                for evidence in call.evidence:
                    spec = evidence.workload.spec
                    # Newest-first traversal makes the first entry the latest attempt.
                    attempts.setdefault((spec.namespace, spec.name), evidence)
        return attempts

    def latest_call(self) -> LLMCall | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT document FROM llm_calls ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else LLMCall.model_validate_json(row["document"])
