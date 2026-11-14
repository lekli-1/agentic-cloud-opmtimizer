import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from optimizer.detect import missing_detection_data
from optimizer.models import (
    Evidence, ExecutionRecord, LLMCall, Observation, Provider, Recommendation,
    RecommendationStatus, utc_now,
)


class RecommendationNotFound(LookupError):
    pass


class RecommendationConflict(RuntimeError):
    pass


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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    document TEXT NOT NULL
                )
            """)
            connection.execute("BEGIN IMMEDIATE")
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'recommendations'"
            ).fetchone()
            migrate_queue = schema is not None and "'queued'" not in schema["sql"]
            if migrate_queue:
                # SQLite cannot alter a CHECK constraint. Rebuild only this table
                # in one transaction, retaining every record and its call-log link.
                connection.execute("ALTER TABLE recommendations RENAME TO recommendations_before_queue")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS recommendations (
                    id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    workload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'queued', 'executing', 'executed', 'failed', 'dismissed')
                    ),
                    created_at TEXT NOT NULL,
                    llm_call_id TEXT NOT NULL REFERENCES llm_calls(id),
                    document TEXT NOT NULL
                )
            """)
            if migrate_queue:
                connection.execute(
                    "INSERT INTO recommendations SELECT * FROM recommendations_before_queue"
                )
                connection.execute("DROP TABLE recommendations_before_queue")
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_workload
                    ON recommendations(namespace, workload) WHERE status = 'pending'
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS recommendation_status
                    ON recommendations(status, created_at)
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
            for row in connection.execute(
                "SELECT document FROM llm_calls "
                "WHERE COALESCE(json_extract(document, '$.kind'), 'recommend') = 'recommend' "
                "ORDER BY created_at DESC"
            ):
                call = LLMCall.model_validate_json(row["document"])
                for evidence in call.evidence:
                    spec = evidence.workload.spec
                    # Newest-first traversal makes the first entry the latest attempt.
                    attempts.setdefault((spec.namespace, spec.name), evidence)
        return attempts

    def latest_call(self) -> LLMCall | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT document FROM llm_calls "
                "WHERE COALESCE(json_extract(document, '$.kind'), 'recommend') = 'recommend' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else LLMCall.model_validate_json(row["document"])

    def recommendation(self, recommendation_id: str) -> Recommendation:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT document FROM recommendations WHERE id = ?", (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFound("Recommendation not found")
            return Recommendation.model_validate_json(row["document"])

    def dismiss_pending(self, recommendation_id: str) -> Recommendation:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM recommendations WHERE id = ?", (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFound("Recommendation not found")
            item = Recommendation.model_validate_json(row["document"])
            if item.status != "pending":
                raise RecommendationConflict("Only pending recommendations can be dismissed")
            self._dismiss(connection, item, "Dismissed by the user", utc_now())
        return self.recommendation(recommendation_id)

    def begin_execution(
        self, recommendation_id: str, provider: Provider,
    ) -> tuple[Recommendation, LLMCall]:
        """Approve once; start immediately or persist it behind earlier approvals."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM recommendations WHERE id = ?", (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFound("Recommendation not found")
            item = Recommendation.model_validate_json(row["document"])
            if item.status != "pending":
                raise RecommendationConflict("Only pending recommendations can be executed")
            waiting = connection.execute(
                "SELECT 1 FROM recommendations WHERE status IN ('queued', 'executing') LIMIT 1"
            ).fetchone() is not None
            now = utc_now()
            call = LLMCall(
                provider=provider, kind="execute", recommendation_id=item.id,
                status="queued" if waiting else "running", prompt="", evidence=[item.evidence],
            )
            item.status = "queued" if waiting else "executing"
            item.updated_at = now
            item.execution = ExecutionRecord(
                call_id=call.id, previous_values=item.evidence.workload.spec.model_copy(deep=True),
                queued_at=now, started_at=None if waiting else now,
            )
            connection.execute(
                "INSERT INTO llm_calls(id, created_at, document) VALUES (?, ?, ?)",
                (call.id, call.created_at.isoformat(), call.model_dump_json()),
            )
            connection.execute(
                "UPDATE recommendations SET status = ?, document = ? WHERE id = ?",
                (item.status, item.model_dump_json(), item.id),
            )
        return item, call

    def next_execution(self) -> tuple[Recommendation, LLMCall] | None:
        """Promote the oldest approval only after the current run has finished."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM recommendations WHERE status = 'executing' LIMIT 1"
            ).fetchone():
                raise RecommendationConflict("An execution is still running")
            row = connection.execute(
                "SELECT document FROM recommendations WHERE status = 'queued' "
                "ORDER BY json_extract(document, '$.execution.queued_at'), id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            item = Recommendation.model_validate_json(row["document"])
            execution = item.execution
            if execution is None:
                raise ValueError("A queued recommendation has no execution record")
            row = connection.execute(
                "SELECT document FROM llm_calls WHERE id = ?", (execution.call_id,),
            ).fetchone()
            if row is None:
                raise ValueError("A queued recommendation has no call log")
            call = LLMCall.model_validate_json(row["document"])
            item.status = "executing"
            item.updated_at = execution.started_at = utc_now()
            call.status = "running"
            self._save_execution(connection, item, call)
        return item, call

    @staticmethod
    def _save_execution(
        connection: sqlite3.Connection, item: Recommendation, call: LLMCall,
    ) -> None:
        result = connection.execute(
            "UPDATE recommendations SET status = ?, document = ? "
            "WHERE id = ? AND status IN ('queued', 'executing')",
            (item.status, item.model_dump_json(), item.id),
        )
        if result.rowcount != 1:
            raise RecommendationConflict("The approved recommendation is no longer available")
        connection.execute(
            "UPDATE llm_calls SET document = ? WHERE id = ?",
            (call.model_dump_json(), call.id),
        )

    def save_execution(self, item: Recommendation, call: LLMCall) -> None:
        """Persist tool progress or finish the run without creating a third table."""
        with self.connection() as connection:
            self._save_execution(connection, item, call)

    def recover_executions(self, window_seconds: int) -> None:
        """Never resume a write after a process crash; preserve uncertainty explicitly."""
        interrupted = self.recommendations("queued", None) + self.recommendations("executing", None)
        for item in interrupted:
            execution = item.execution
            if execution is None:
                raise ValueError("An approved recommendation has no execution record")
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT document FROM llm_calls WHERE id = ?", (execution.call_id,),
                ).fetchone()
            if row is None:
                raise ValueError("An approved recommendation has no call log")
            call = LLMCall.model_validate_json(row["document"])
            item.status = "failed"
            item.updated_at = execution.finished_at = utc_now()
            execution.error = call.error = (
                "Queued execution cancelled by an API restart; analyze again before approving"
                if execution.started_at is None else
                "Execution interrupted by an API restart; check the live Deployment before analyzing again"
            )
            if execution.patch_attempted_at is not None:
                execution.skip_until = execution.finished_at + timedelta(seconds=window_seconds)
            call.status = "failed"
            self.save_execution(item, call)

    def observation_windows(self, snapshot: Observation) -> set[tuple[str, str]]:
        """Skip approved/mixed windows; save the first complete post-window observation."""
        skipped: set[tuple[str, str]] = set()
        workloads = {(item.spec.namespace, item.spec.name): item for item in snapshot.workloads}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT document FROM recommendations "
                "WHERE status IN ('queued','executing','executed','failed')"
            ).fetchall()
            for row in rows:
                item = Recommendation.model_validate_json(row["document"])
                execution = item.execution
                if execution is None:
                    continue
                spec = item.evidence.workload.spec
                key = (spec.namespace, spec.name)
                if item.status in ("queued", "executing"):
                    skipped.add(key)
                    continue
                if execution.patch_attempted_at is None or execution.finished_at is None:
                    continue
                # Account for a changed observer window as well as a saved restart delay.
                cutoff = execution.finished_at + timedelta(seconds=snapshot.window_seconds)
                if execution.skip_until is not None:
                    cutoff = max(cutoff, execution.skip_until)
                if snapshot.observed_at < cutoff:
                    skipped.add(key)
                elif execution.after_observation is None:
                    workload = workloads.get(key)
                    if workload is not None and not missing_detection_data(workload):
                        execution.after_observation = Observation(
                            observed_at=snapshot.observed_at,
                            window_seconds=snapshot.window_seconds, workloads=[workload],
                        )
                        item.updated_at = utc_now()
                        connection.execute(
                            "UPDATE recommendations SET document = ? WHERE id = ?",
                            (item.model_dump_json(), item.id),
                        )
        return skipped
