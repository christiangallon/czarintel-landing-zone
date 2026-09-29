import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import health_check as hc


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        routes = {
            "/health": (200, {"status": "ok", "version": "1.0.0"}),
            "/broken": (503, {"status": "down"}),
            "/api/v1/alerts": (200, {"status": "success", "data": {"alerts": [
                {"state": "firing", "labels": {"alertname": "Watchdog", "severity": "none"}},
                {"state": "firing", "labels": {"alertname": "HighLatency", "severity": "warning",
                                               "namespace": "production"},
                 "annotations": {"summary": "p95 > 500ms"}},
                {"state": "pending", "labels": {"alertname": "MySQLDown", "severity": "critical"}}]}}),
        }
        code, body = routes.get(self.path, (404, {"error": "nf"}))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", srv.server_port
    srv.shutdown()


def test_http_ok_with_json_subset(server):
    base, _ = server
    r = hc.check_http({"url": f"{base}/health", "expect_json": {"status": "ok"}}, 2, False)
    assert r.status == "ok" and r.latency_ms is not None


def test_http_status_and_json_mismatch(server):
    base, _ = server
    assert hc.check_http({"url": f"{base}/broken"}, 2, False).status == "fail"
    r = hc.check_http({"url": f"{base}/health", "expect_json": {"status": "degraded"}}, 2, False)
    assert r.status == "fail" and "mismatch" in r.detail


def test_http_latency_budget_warns(server):
    base, _ = server
    r = hc.check_http({"url": f"{base}/health", "max_latency_ms": 0}, 2, False)
    assert r.status == "warn"


def test_http_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert hc.check_http({"url": f"http://127.0.0.1:{port}/"}, 1, False).status == "fail"


def test_tcp(server):
    _, port = server
    assert hc.check_tcp({"host": "127.0.0.1", "port": port}, 1).status == "ok"


def test_prometheus_alerts_mapping(server):
    base, _ = server
    res = hc.check_prometheus({"url": base}, 2, False)
    assert [(r.name, r.status) for r in res] == [("alert:HighLatency", "warn")]


def test_overall_and_main_exit_codes(server, tmp_path, capsys):
    base, port = server
    targets = tmp_path / "t.yml"
    targets.write_text(f"http:\n  - name: h\n    url: {base}/health\ntcp:\n  - host: 127.0.0.1\n    port: {port}\n")
    assert hc.main(["-t", str(targets)]) == 0
    assert "GREEN" in capsys.readouterr().out
    assert hc.main(["--url", f"{base}/broken", "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "RED"
    assert hc.overall([hc.Result("a", "http", "warn", 1.0)]) == ("AMBER", 1)


def test_default_targets_file_is_valid():
    t = hc.load_targets(hc.DEFAULT_TARGETS)
    assert t["http"] and all("url" in x for x in t["http"])
