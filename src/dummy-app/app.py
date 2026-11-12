import math
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query


def allocate_memory(mb: int) -> bytearray:
    memory = bytearray(mb * 1024 * 1024)
    # Touch each page so Linux counts the allocation as resident memory.
    for offset in range(0, len(memory), 4096):
        memory[offset] = 1
    return memory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    baseline_mb = int(os.environ.get("BASELINE_MEM_MB", "0"))
    if baseline_mb < 0:
        raise ValueError("BASELINE_MEM_MB must be non-negative")
    # Keep a reference across requests so the baseline lasts until shutdown.
    app.state.baseline_memory = allocate_memory(baseline_mb)
    try:
        yield
    finally:
        del app.state.baseline_memory


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Keep probes on the event loop, outside the synchronous stress-worker queue."""
    return {"status": "healthy"}


@app.get("/cpu")
def cpu(ms: int = Query(default=100, ge=0)) -> dict[str, float]:
    # CPU time, not wall time: throttling must not shorten the requested work.
    deadline = time.thread_time() + ms / 1000
    result = 0.0001
    while time.thread_time() < deadline:
        result += math.sqrt(result)
    return {"cpu_ms": ms, "result": result}


@app.get("/mem")
def memory(mb: int = Query(default=16, ge=0)) -> dict[str, int]:
    """Hold memory for this handler only; freed pages may remain resident."""
    allocation = allocate_memory(mb)
    return {"allocated_mb": len(allocation) // (1024 * 1024)}


@app.get("/stress")
def stress() -> dict[str, str | float]:
    # Heavy calculation to burn CPU cycles
    x = 0.0001
    for _ in range(5000000):
        x += math.sqrt(x)
    return {"message": "CPU stressed", "result": x}
