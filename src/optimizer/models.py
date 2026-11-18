from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Resources(BaseModel):
    """Per-container, per-replica resources; None means unspecified, not zero."""

    cpu_request_millicores: float | None = Field(default=None, ge=0)
    cpu_limit_millicores: float | None = Field(default=None, ge=0)
    memory_request_mib: float | None = Field(default=None, ge=0)
    memory_limit_mib: float | None = Field(default=None, ge=0)


class DeploymentSpec(BaseModel):
    namespace: str
    name: str
    container_name: str
    replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)
    resources: Resources
    uid: str | None = None
    spec_hash: str | None = None


class Metrics(BaseModel):
    """CPU/memory and the percentage of throttled CPU periods."""

    cpu_avg_millicores: float | None = Field(default=None, ge=0)
    cpu_p95_millicores: float | None = Field(default=None, ge=0)
    memory_peak_mib: float | None = Field(default=None, ge=0)
    cpu_throttling_percent: float | None = Field(default=None, ge=0, le=100)


class Workload(BaseModel):
    spec: DeploymentSpec
    metrics: Metrics


class NodePrice(BaseModel):
    name: str
    vm_size: str
    region: str
    hourly_usd: float = Field(ge=0, allow_inf_nan=False)
    checked_at: datetime


class NodeCosts(BaseModel):
    nodes: list[NodePrice] = Field(min_length=1)
    hourly_usd: float = Field(ge=0, allow_inf_nan=False)


class Observation(BaseModel):
    observed_at: datetime
    window_seconds: int
    workloads: list[Workload]
    node_costs: NodeCosts | None = None


class WorkloadsResponse(Observation):
    error: str | None = None


Issue = Literal["over_provisioned", "under_provisioned", "memory_risk"]
Provider = Literal["openai", "fake"]
RecommendationStatus = Literal["pending", "queued", "executing", "executed", "failed", "dismissed"]
AnalysisReadinessStatus = Literal["ready", "waiting_for_metrics", "no_issues", "no_changes", "analyzing"]
AnalysisStatus = Literal["completed", "failed", "no_issues", "waiting_for_metrics", "no_changes"]

# These execution bounds are code-owned, not settings supplied by the model.
BOUNDS = {"cpu_millicores": (50, 2000), "memory_mib": (64, 2048), "replicas": (1, 5)}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cpu_request_millicores: int
    cpu_limit_millicores: int
    memory_request_mib: int
    memory_limit_mib: int
    replicas: int

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        """Check bounds in code, independently of the model's JSON schema.

        This lets response validation drop one out-of-bounds proposal while
        retaining valid proposals from the same batch.
        """
        values = self.model_dump()
        groups = {
            "cpu_millicores": ("cpu_request_millicores", "cpu_limit_millicores"),
            "memory_mib": ("memory_request_mib", "memory_limit_mib"),
            "replicas": ("replicas",),
        }
        for group, fields in groups.items():
            minimum, maximum = BOUNDS[group]
            for name in fields:
                if not minimum <= values[name] <= maximum:
                    raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.cpu_request_millicores > self.cpu_limit_millicores:
            raise ValueError("CPU request must not exceed its limit")
        if self.memory_request_mib > self.memory_limit_mib:
            raise ValueError("Memory request must not exceed its limit")
        return self


# Required, flat fields keep the OpenAI strict-output schema straightforward.
class Proposal(Allocation):
    namespace: str
    workload: str
    action: Literal["resize", "scale"]
    reason: str = Field(min_length=1, max_length=1000)
    risk: Literal["low", "medium", "high"]

    def allocation(self) -> Allocation:
        return Allocation(
            cpu_request_millicores=self.cpu_request_millicores,
            cpu_limit_millicores=self.cpu_limit_millicores,
            memory_request_mib=self.memory_request_mib,
            memory_limit_mib=self.memory_limit_mib,
            replicas=self.replicas,
        )


class ProposalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[Proposal]


class Evidence(BaseModel):
    """The observation used for analysis, not a reference to the latest live state."""

    observed_at: datetime
    window_seconds: int
    workload: Workload
    issues: list[Issue]
    issue_first_seen_at: datetime
    node_costs: NodeCosts | None = None


class ExecutionRecord(BaseModel):
    call_id: str
    queued_at: datetime | None = None
    started_at: datetime | None = None
    previous_values: DeploymentSpec
    applied_spec: DeploymentSpec | None = None
    patch_attempted_at: datetime | None = None
    applied_at: datetime | None = None
    finished_at: datetime | None = None
    skip_until: datetime | None = None
    summary: str | None = None
    error: str | None = None
    after_observation: Observation | None = None


class Recommendation(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    status: RecommendationStatus = "pending"
    dismissed_reason: str | None = None
    provider: Provider | Literal["manual"]
    llm_call_id: str
    action: Literal["resize", "scale"]
    new_values: Allocation
    reason: str
    risk: Literal["low", "medium", "high"]
    evidence: Evidence
    monthly_cost_before_usd: float | None
    monthly_cost_after_usd: float | None
    monthly_cost_delta_usd: float | None
    execution: ExecutionRecord | None = None
    reverts_id: str | None = None
    revert_id: str | None = None


class TokenUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)


class ModelAnswer(BaseModel):
    answer: str
    model: str
    usage: TokenUsage | None = None
    error: str | None = None


class ToolCallRecord(BaseModel):
    call_id: str
    name: str
    arguments: str
    result: str
    succeeded: bool


class LLMCall(BaseModel):
    """Shared audit storage for model calls and deterministic manual Revert."""

    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utc_now)
    provider: Provider | Literal["manual"]
    model: str | None = None
    kind: Literal["recommend", "execute", "revert"] = "recommend"
    recommendation_id: str | None = None
    status: Literal["queued", "running", "succeeded", "failed"] = "failed"
    prompt: str
    answer: str | None = None
    evidence: list[Evidence]
    usage: TokenUsage | None = None
    latency_ms: float = 0
    estimated_cost_usd: float | None = None
    invalid_proposals: int = 0
    rejections: list[str] = Field(default_factory=list)
    missing_workloads: list[str] = Field(default_factory=list)
    discarded_workloads: list[str] = Field(default_factory=list)
    error: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class AnalysisWait(BaseModel):
    namespace: str
    workload: str
    reason: Literal["queued", "executing", "waiting_for_metrics"]
    retry_at: datetime | None = None


class AnalysisReadiness(BaseModel):
    status: AnalysisReadinessStatus
    observed_at: datetime
    ready_workloads: list[str] = Field(default_factory=list)
    waiting_workloads: list[AnalysisWait] = Field(default_factory=list)
    retry_at: datetime | None = None
    minimum_sized_workloads: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    status: AnalysisStatus
    call_id: str | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    invalid_proposals: int = 0
    rejections: list[str] = Field(default_factory=list)
    missing_workloads: list[str] = Field(default_factory=list)
    discarded_workloads: list[str] = Field(default_factory=list)
    error: str | None = None
    waiting_workloads: list[AnalysisWait] = Field(default_factory=list)
    retry_at: datetime | None = None
    minimum_sized_workloads: list[str] = Field(default_factory=list)
