import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from optimizer.config import Settings
from optimizer.detect import detect, missing_detection_data, same_configuration
from optimizer.models import AnalysisResult, Evidence, Observation
from optimizer.observe import ObservationState
from optimizer.recommend import ModelCall, recommend
from optimizer.store import Store

logger = logging.getLogger(__name__)


class AnalysisBusy(RuntimeError):
    pass


class ObservationUnavailable(RuntimeError):
    pass


@dataclass
class TrackedIssue:
    """Track continuity separately from attempts, including failed provider calls."""

    evidence: Evidence
    attempted: bool = False
    interrupted: bool = False


def same_issue(first: Evidence, second: Evidence) -> bool:
    return first.issues == second.issues and same_configuration(
        first.workload.spec, second.workload.spec
    )


class RecommendationService:
    """Coordinate fast observations and one slow analysis in a single process.

    The lock protects tracking/storage transitions, not the model call itself,
    so new observations can invalidate evidence while the model is working.
    """

    def __init__(
        self, settings: Settings, state: ObservationState, store: Store,
        model_call: ModelCall | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.store = store
        self.model_call = model_call
        self.active: dict[tuple[str, str], TrackedIssue] = {}
        self.restored_attempts: dict[tuple[str, str], Evidence] = {}
        self.incomplete: set[tuple[str, str]] = set()
        self.skipped: set[tuple[str, str]] = set()
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[AnalysisResult] | None = None
        self.error: str | None = None
        self.storage_error: str | None = None
        self.unsaved_call = False

    def restore_history(self) -> None:
        """Avoid another automatic charge for a matching attempt after restart."""
        self.restored_attempts = self.store.latest_attempts()
        latest = self.store.latest_call()
        if latest is not None:
            self.error = latest.error

    async def synchronize(self, snapshot: Observation) -> None:
        """Update issue continuity and retire obsolete pending proposals."""
        self.skipped = await asyncio.to_thread(self.store.observation_windows, snapshot)
        if self.state.interrupted:
            for issue in self.active.values():
                issue.interrupted = True
            self.state.interrupted = False
        pending = await asyncio.to_thread(self.store.recommendations, "pending", None)
        pending_by_target = {
            (item.evidence.workload.spec.namespace, item.evidence.workload.spec.name): item
            for item in pending
        }
        workloads = {
            (item.spec.namespace, item.spec.name): item for item in snapshot.workloads
        }
        self.incomplete = set()
        for key in list(self.active):
            if key not in workloads:
                del self.active[key]
        for key in list(self.restored_attempts):
            if key not in workloads:
                del self.restored_attempts[key]
        for key, workload in workloads.items():
            previous = self.active.get(key)
            if key in self.skipped:
                self.active.pop(key, None)
                self.restored_attempts.pop(key, None)
                continue
            if missing_detection_data(workload):
                logger.warning("%s/%s: incomplete metrics; postponing detection", *key)
                self.incomplete.add(key)
                if previous is not None:
                    previous.interrupted = True
                    if not same_configuration(previous.evidence.workload.spec, workload.spec):
                        del self.active[key]
                continue
            issues = detect(workload, self.settings)
            # Consume restart markers only once usable telemetry establishes identity.
            restored = self.restored_attempts.pop(key, None)
            evidence = Evidence(
                observed_at=snapshot.observed_at, window_seconds=snapshot.window_seconds,
                workload=workload, issues=issues, issue_first_seen_at=snapshot.observed_at,
            )
            if not issues:
                self.active.pop(key, None)
                continue
            attempted = False
            if previous is not None and same_issue(previous.evidence, evidence):
                attempted = previous.attempted
                if not previous.interrupted:
                    evidence.issue_first_seen_at = previous.evidence.issue_first_seen_at
            elif restored is not None and same_issue(restored, evidence):
                attempted = True
                evidence.issue_first_seen_at = restored.issue_first_seen_at
            elif key in pending_by_target and same_issue(pending_by_target[key].evidence, evidence):
                attempted = True
                evidence.issue_first_seen_at = pending_by_target[key].evidence.issue_first_seen_at
            self.active[key] = TrackedIssue(evidence=evidence, attempted=attempted)

        dismissals = {}
        for item in pending:
            spec = item.evidence.workload.spec
            key = (spec.namespace, spec.name)
            current = workloads.get(key)
            if current is None:
                dismissals[item.id] = "The workload no longer exists"
            elif not same_configuration(spec, current.spec):
                dismissals[item.id] = "The Deployment resource settings changed"
            elif key not in self.incomplete:
                active = self.active.get(key)
                if active is None:
                    dismissals[item.id] = "The issue is no longer detected"
                elif item.evidence.issues != active.evidence.issues:
                    dismissals[item.id] = "The detected issues changed"
        if dismissals:
            await asyncio.to_thread(self.store.dismiss, dismissals)
        # Reading successfully does not recover an unsaved provider-call log.
        if not self.unsaved_call:
            self.storage_error = None
        if not self.active and not self.incomplete:
            self.error = None

    def eligible(self, now: datetime, manual: bool) -> list[Evidence]:
        """Manual analysis may retry; automatic analysis needs a new issue."""
        candidates = []
        for key, issue in self.active.items():
            if key in self.incomplete:
                continue
            elapsed = (now - issue.evidence.issue_first_seen_at).total_seconds()
            if manual or (
                not issue.attempted and elapsed >= self.settings.issue_persistence_seconds
            ):
                # Freeze this call's evidence while later observations keep arriving.
                candidates.append(issue.evidence.model_copy(deep=True))
        return candidates

    def start_analysis(self, evidence: list[Evidence]) -> asyncio.Task[AnalysisResult]:
        # Mark before scheduling: failures must not trigger automatic paid retries.
        for item in evidence:
            spec = item.workload.spec
            self.active[(spec.namespace, spec.name)].attempted = True
        self.task = asyncio.create_task(self.generate(evidence))
        return self.task

    async def observe(self, snapshot: Observation) -> None:
        """Schedule eligible analysis without making observation wait for the model."""
        try:
            async with self.lock:
                await self.synchronize(snapshot)
                if self.task is not None and not self.task.done():
                    return
                evidence = self.eligible(snapshot.observed_at, manual=False)
                if evidence:
                    self.start_analysis(evidence)
        except sqlite3.Error:
            self.storage_error = "Unable to update recommendation history in SQLite"
            for issue in self.active.values():
                issue.interrupted = True
            logger.exception(self.storage_error)

    async def analyze(self) -> AnalysisResult:
        """Bypass the persistence delay, but still require a usable observation."""
        async with self.lock:
            if self.task is not None and not self.task.done():
                raise AnalysisBusy("A recommendation call is already running")
            snapshot = self.state.snapshot
            if snapshot is None or self.state.error is not None:
                raise ObservationUnavailable(self.state.error or "Waiting for observation")
            await self.synchronize(snapshot)
            evidence = self.eligible(snapshot.observed_at, manual=True)
            if not evidence:
                if self.skipped:
                    raise ObservationUnavailable(
                        "Workloads are queued, executing or waiting for a clean metric window"
                    )
                if self.incomplete:
                    raise ObservationUnavailable("Detection is waiting for complete workload metrics")
                return AnalysisResult(status="no_issues")
            task = self.start_analysis(evidence)
        # A disconnected HTTP caller must not cancel an already billable call and its log.
        return await asyncio.shield(task)

    async def generate(self, evidence: list[Evidence]) -> AnalysisResult:
        """Run the provider outside the lock, then recheck and store its result."""
        call, recommendations = await asyncio.to_thread(
            recommend, evidence, self.settings, self.model_call
        )
        try:
            async with self.lock:
                if self.state.snapshot is not None:
                    await self.synchronize(self.state.snapshot)
                current_recommendations = []
                for recommendation in recommendations:
                    item = recommendation.evidence
                    spec = item.workload.spec
                    key = (spec.namespace, spec.name)
                    current = self.active.get(key)
                    # The same flags can disappear and recur during a call. Matching
                    # first-seen time also requires the same uninterrupted issue.
                    if (
                        self.state.error is None
                        and key not in self.incomplete
                        and key not in self.skipped
                        and current is not None
                        and same_issue(item, current.evidence)
                        and item.issue_first_seen_at == current.evidence.issue_first_seen_at
                    ):
                        current_recommendations.append(recommendation)
                    else:
                        call.discarded_workloads.append(f"{spec.namespace}/{spec.name}")
                        logger.warning("Discarded stale proposal for %s/%s", *key)
                await asyncio.to_thread(self.store.save_analysis, call, current_recommendations)
                self.unsaved_call = False
                self.storage_error = None
                self.error = call.error
                return AnalysisResult(
                    status="completed" if call.status == "succeeded" else "failed",
                    call_id=call.id, recommendations=current_recommendations,
                    invalid_proposals=call.invalid_proposals, rejections=call.rejections,
                    missing_workloads=call.missing_workloads,
                    discarded_workloads=call.discarded_workloads, error=call.error,
                )
        except sqlite3.Error:
            self.unsaved_call = True
            self.storage_error = (
                "The provider call finished, but its results could not be saved in SQLite"
            )
            logger.exception(self.storage_error)
            return AnalysisResult(status="failed", call_id=call.id, error=self.storage_error)

    async def close(self) -> None:
        if self.task is not None:
            await self.task
