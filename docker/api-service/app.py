"""
CzarIntel API Service
=====================

Reference microservice for the CzarIntel Multi-Cloud DevSecOps Landing Zone.

Endpoints
---------
GET /health               Liveness/readiness probe
GET /ready                Readiness probe (checks optional MySQL dependency)
GET /api/v1/services      Inventory of platform microservices and their status
GET /api/v1/services/{n}  Single service lookup
GET /metrics              Prometheus exposition format

Configuration (environment variables)
-------------------------------------
APP_VERSION        Reported version (default 1.0.0)
APP_ENV            Environment name (default local)
LOG_LEVEL          Python log level (default INFO)
DB_HOST/DB_PORT    Optional MySQL dependency checked by /ready
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
APP_NAME = "czarintel-api-service"
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_ENV = os.getenv("APP_ENV", "local")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_CHECK_TIMEOUT = float(os.getenv("DB_CHECK_TIMEOUT", "1.5"))
HOSTNAME = socket.gethostname()
STARTED_AT = time.time()


# --------------------------------------------------------------------------- #
# Structured JSON logging (Loki / CloudWatch / Log Analytics friendly)
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": APP_NAME,
            "env": APP_ENV,
            "pod": HOSTNAME,
        }
        for key in ("request_id", "method", "path", "status", "duration_ms", "client"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(LOG_LEVEL)
    # Keep uvicorn's access log quiet; we emit our own structured access log.
    logging.getLogger("uvicorn.access").disabled = True
    return logging.getLogger(APP_NAME)


logger = configure_logging()

# --------------------------------------------------------------------------- #
# Prometheus metrics (dedicated registry keeps tests isolated)
# --------------------------------------------------------------------------- #
REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
HTTP_IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "HTTP requests currently being served",
    registry=REGISTRY,
)
SERVICE_UP = Gauge(
    "czarintel_service_up",
    "Reported health of downstream platform services (1=up, 0=down)",
    ["service", "cloud"],
    registry=REGISTRY,
)
DEPENDENCY_UP = Gauge(
    "czarintel_dependency_up",
    "Result of the last dependency check (1=reachable)",
    ["dependency"],
    registry=REGISTRY,
)
BUILD_INFO = Info("czarintel_build", "Build information", registry=REGISTRY)
BUILD_INFO.info({"version": APP_VERSION, "env": APP_ENV, "service": APP_NAME})


# --------------------------------------------------------------------------- #
# Domain models
# --------------------------------------------------------------------------- #
ServiceStatus = Literal["healthy", "degraded", "down"]


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = APP_VERSION
    timestamp: str
    environment: str = APP_ENV
    hostname: str = HOSTNAME
    uptime_seconds: float


class MicroService(BaseModel):
    name: str
    cloud: Literal["aws", "azure", "gcp"]
    cluster: str
    namespace: str
    version: str
    replicas: int = Field(ge=0)
    status: ServiceStatus
    endpoint: str


class ServiceList(BaseModel):
    count: int
    healthy: int
    degraded: int
    down: int
    services: list[MicroService]
    generated_at: str


SERVICE_CATALOG: list[MicroService] = [
    MicroService(name="api-gateway", cloud="aws", cluster="czarintel-prod-eks", namespace="production",
                 version="2.4.1", replicas=4, status="healthy", endpoint="https://api.czarintel.io"),
    MicroService(name="identity-broker", cloud="azure", cluster="czarintel-stg-aks", namespace="staging",
                 version="1.9.0", replicas=3, status="healthy", endpoint="https://id.stg.czarintel.io"),
    MicroService(name="threat-intel-ingest", cloud="aws", cluster="czarintel-prod-eks", namespace="production",
                 version="3.1.2", replicas=6, status="healthy", endpoint="http://threat-intel.production.svc:8080"),
    MicroService(name="compliance-engine", cloud="aws", cluster="czarintel-prod-eks", namespace="production",
                 version="1.2.7", replicas=2, status="degraded", endpoint="http://compliance.production.svc:8080"),
    MicroService(name="analytics-pipeline", cloud="gcp", cluster="czarintel-prod-gke", namespace="production",
                 version="0.8.3", replicas=3, status="healthy", endpoint="http://analytics.production.svc:8080"),
    MicroService(name="mysql-primary", cloud="aws", cluster="czarintel-prod-eks", namespace="production",
                 version="8.0.39", replicas=1, status="healthy", endpoint="mysql.production.svc:3306"),
]

_STATUS_VALUE = {"healthy": 1, "degraded": 1, "down": 0}
for svc in SERVICE_CATALOG:
    SERVICE_UP.labels(service=svc.name, cloud=svc.cloud).set(_STATUS_VALUE[svc.status])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_tcp(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def route_template(request: Request) -> str:
    """Use the route template (e.g. /api/v1/services/{name}) to avoid
    unbounded label cardinality in Prometheus."""
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("service starting", extra={"client": f"version={APP_VERSION}"})
    yield
    logger.info("service shutting down")


app = FastAPI(
    title="CzarIntel API Service",
    version=APP_VERSION,
    description="Reference workload for the CzarIntel Multi-Cloud DevSecOps Landing Zone",
    lifespan=lifespan,
    docs_url="/docs" if APP_ENV != "prod" else None,
    redoc_url=None,
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    start = time.perf_counter()
    HTTP_IN_FLIGHT.inc()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:  # noqa: BLE001 — log and convert to a 500
        logger.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
        response = JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "request_id": request_id},
        )
    finally:
        HTTP_IN_FLIGHT.dec()

    duration = time.perf_counter() - start
    path = route_template(request)
    if path != "/metrics":
        HTTP_REQUESTS.labels(request.method, path, str(status_code)).inc()
        HTTP_LATENCY.labels(request.method, path).observe(duration)

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"

    if path not in ("/health", "/metrics"):
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "duration_ms": round(duration * 1000, 2),
                "client": request.client.host if request.client else None,
            },
        )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "path": request.url.path, "timestamp": utc_now()},
    )


@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness probe — must never depend on downstream systems."""
    return HealthResponse(timestamp=utc_now(), uptime_seconds=round(time.time() - STARTED_AT, 2))


@app.get("/ready", tags=["ops"])
async def ready():
    """Readiness probe — verifies MySQL reachability when DB_HOST is configured."""
    checks: dict[str, str] = {}
    if DB_HOST:
        ok = check_tcp(DB_HOST, DB_PORT, DB_CHECK_TIMEOUT)
        DEPENDENCY_UP.labels("mysql").set(1 if ok else 0)
        checks["mysql"] = "ok" if ok else "unreachable"
        if not ok:
            return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks, "timestamp": utc_now()}


@app.get("/api/v1/services", response_model=ServiceList, tags=["services"])
async def list_services(cloud: str | None = None, status: str | None = None) -> ServiceList:
    services = SERVICE_CATALOG
    if cloud:
        services = [s for s in services if s.cloud == cloud.lower()]
    if status:
        services = [s for s in services if s.status == status.lower()]
    return ServiceList(
        count=len(services),
        healthy=sum(s.status == "healthy" for s in services),
        degraded=sum(s.status == "degraded" for s in services),
        down=sum(s.status == "down" for s in services),
        services=services,
        generated_at=utc_now(),
    )


@app.get("/api/v1/services/{name}", response_model=MicroService, tags=["services"])
async def get_service(name: str) -> MicroService:
    for svc in SERVICE_CATALOG:
        if svc.name == name:
            return svc
    raise HTTPException(status_code=404, detail=f"service '{name}' not found")


@app.get("/metrics", tags=["ops"], include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":  # pragma: no cover — local development entrypoint
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level=LOG_LEVEL.lower())
