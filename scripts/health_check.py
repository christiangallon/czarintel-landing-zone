#!/usr/bin/env python3
"""
CzarIntel platform health check.

Probes HTTP endpoints, TCP services and (optionally) Prometheus alert state in
parallel and prints a single RAG status. Used for post-deploy verification,
by the on-call runbooks and as a synthetic check from outside the cluster.

Targets come from a YAML file (default: scripts/health_targets.yml):

    http:
      - name: api-health
        url: https://api.czarintel.io/health
        expect_status: 200
        expect_json: {status: ok}
        max_latency_ms: 500
    tcp:
      - name: mysql-proxysql
        host: proxysql.internal
        port: 6033
    prometheus:
      url: http://kps-prometheus.monitoring:9090
      fail_on_severity: critical

    python3 scripts/health_check.py                     # uses default targets file
    python3 scripts/health_check.py -t targets.yml --format json
    python3 scripts/health_check.py --url https://api.czarintel.io/health   # ad hoc

Exit codes: 0 healthy, 1 degraded (warnings only), 2 unhealthy.
Only the standard library + PyYAML are required so it runs on any Jenkins agent.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TARGETS = Path(__file__).resolve().parent / "health_targets.yml"
USER_AGENT = "czarintel-health-check/1.0"


@dataclass
class Result:
    name: str
    kind: str
    status: str            # ok | warn | fail
    latency_ms: float | None
    detail: str = ""


def _ssl_context(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:  # only for lab environments with self-signed certs
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _subset(expected: Any, actual: Any) -> bool:
    """True if every key/value in ``expected`` is present in ``actual`` (recursive)."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    return expected == actual


def check_http(t: dict, timeout: float, insecure: bool) -> Result:
    name = t.get("name", t["url"])
    expect_status = int(t.get("expect_status", 200))
    max_latency = t.get("max_latency_ms")
    req = urllib.request.Request(t["url"], method=t.get("method", "GET"),
                                 headers={"User-Agent": USER_AGENT, **t.get("headers", {})})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(insecure)) as resp:
            body = resp.read(1_000_000)
            code = resp.status
    except urllib.error.HTTPError as exc:
        body, code = exc.read(1_000_000) if exc.fp else b"", exc.code
    except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError) as exc:
        return Result(name, "http", "fail", None, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")
    latency = round((time.perf_counter() - start) * 1000, 1)

    if code != expect_status:
        return Result(name, "http", "fail", latency, f"HTTP {code} (expected {expect_status})")
    if "expect_json" in t:
        try:
            payload = json.loads(body)
        except ValueError:
            return Result(name, "http", "fail", latency, "response is not JSON")
        if not _subset(t["expect_json"], payload):
            return Result(name, "http", "fail", latency, f"JSON mismatch: expected {t['expect_json']}")
    if "expect_text" in t and t["expect_text"] not in body.decode("utf-8", "replace"):
        return Result(name, "http", "fail", latency, f"text {t['expect_text']!r} not found")
    if max_latency is not None and latency > float(max_latency):
        return Result(name, "http", "warn", latency, f"slow: {latency}ms > {max_latency}ms")
    return Result(name, "http", "ok", latency, f"HTTP {code}")


def check_tcp(t: dict, timeout: float) -> Result:
    name = t.get("name", f"{t['host']}:{t['port']}")
    start = time.perf_counter()
    try:
        with socket.create_connection((t["host"], int(t["port"])), timeout=timeout):
            pass
    except OSError as exc:
        return Result(name, "tcp", "fail", None, f"{type(exc).__name__}: {exc}")
    return Result(name, "tcp", "ok", round((time.perf_counter() - start) * 1000, 1), "connected")


def check_prometheus(cfg: dict, timeout: float, insecure: bool) -> list[Result]:
    """Turns firing Prometheus alerts into results (critical -> fail, warning -> warn)."""
    url = cfg["url"].rstrip("/") + "/api/v1/alerts"
    fail_sev = cfg.get("fail_on_severity", "critical")
    ignore = set(cfg.get("ignore_alerts", ["Watchdog", "InfoInhibitor"]))
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(insecure)) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, socket.timeout, ValueError, ConnectionError) as exc:
        return [Result("prometheus", "alerts", "fail", None, f"cannot query alerts: {exc}")]
    latency = round((time.perf_counter() - start) * 1000, 1)
    firing = [a for a in data.get("data", {}).get("alerts", [])
              if a.get("state") == "firing" and a.get("labels", {}).get("alertname") not in ignore]
    if not firing:
        return [Result("prometheus", "alerts", "ok", latency, "no firing alerts")]
    out = []
    for a in firing:
        labels = a.get("labels", {})
        sev = labels.get("severity", "warning")
        where = labels.get("namespace") or labels.get("instance") or labels.get("job", "")
        out.append(Result(f"alert:{labels.get('alertname')}", "alerts",
                          "fail" if sev == fail_sev else "warn", latency,
                          f"{sev} {where} - {a.get('annotations', {}).get('summary', '')}".strip(" -")))
    return out


def run(targets: dict, timeout: float, insecure: bool, workers: int = 16) -> list[Result]:
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t in targets.get("http", []) or []:
            jobs.append(pool.submit(check_http, t, timeout, insecure))
        for t in targets.get("tcp", []) or []:
            jobs.append(pool.submit(check_tcp, t, timeout))
        prom = targets.get("prometheus")
        prom_job = pool.submit(check_prometheus, prom, timeout, insecure) if prom else None
        results = [j.result() for j in jobs]
        if prom_job:
            results.extend(prom_job.result())
    return results


def overall(results: list[Result]) -> tuple[str, int]:
    if any(r.status == "fail" for r in results):
        return "RED", 2
    if any(r.status == "warn" for r in results):
        return "AMBER", 1
    return "GREEN", 0


def render_text(results: list[Result]) -> str:
    rag, _ = overall(results)
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"CzarIntel health: {rag}", "-" * 72]
    for r in sorted(results, key=lambda x: ({"fail": 0, "warn": 1, "ok": 2}[x.status], x.name)):
        lat = f"{r.latency_ms:>8.1f}ms" if r.latency_ms is not None else "       -  "
        lines.append(f"[{icon[r.status]}] {r.kind:<6} {r.name:<32} {lat}  {r.detail}")
    return "\n".join(lines) + "\n"


def load_targets(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with http/tcp/prometheus keys")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CzarIntel platform health check")
    ap.add_argument("-t", "--targets", type=Path, default=DEFAULT_TARGETS)
    ap.add_argument("--url", action="append", default=[], help="ad-hoc HTTP URL (repeatable, skips targets file)")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (lab only)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    try:
        targets = {"http": [{"url": u} for u in args.url]} if args.url else load_targets(args.targets)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error loading targets: {exc}", file=sys.stderr)
        return 2

    results = run(targets, args.timeout, args.insecure)
    rag, code = overall(results)
    if args.format == "json":
        print(json.dumps({"status": rag, "results": [asdict(r) for r in results]}, indent=2))
    else:
        sys.stdout.write(render_text(results))
    return code


if __name__ == "__main__":
    sys.exit(main())
