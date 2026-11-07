"""Shared setup for every shop service: logging, Prometheus metrics, health endpoints."""

import logging
import os
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "unknown")

REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["service", "method", "route", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["service", "method", "route"]
)
DEPENDENCY_LATENCY = Histogram(
    "dependency_request_duration_seconds",
    "Latency of calls to downstream dependencies",
    ["service", "dependency", "outcome"],
)

SKIP_METRICS = {"/metrics", "/healthz", "/readyz"}


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s {SERVICE} %(message)s",
    )
    # Per-request access logs from uvicorn and httpx bury the lines that matter
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(SERVICE)


def create_app(**kwargs) -> FastAPI:
    app = FastAPI(title=SERVICE, docs_url=None, redoc_url=None, **kwargs)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = route.path if route else "unmatched"
            if path not in SKIP_METRICS:
                LATENCY.labels(SERVICE, request.method, path).observe(time.perf_counter() - start)
                REQUESTS.labels(SERVICE, request.method, path, str(status)).inc()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        return {"status": "ready"}

    return app


class timed_dependency:
    """Context manager that records how long a call to a dependency took and whether it failed."""

    def __init__(self, dependency: str):
        self.dependency = dependency

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        outcome = "error" if exc_type else "ok"
        DEPENDENCY_LATENCY.labels(SERVICE, self.dependency, outcome).observe(
            time.perf_counter() - self.start
        )
        return False
