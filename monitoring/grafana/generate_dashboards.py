#!/usr/bin/env python3
"""
Generate CzarIntel Grafana dashboards as JSON (Grafana 10/11 schema v39).

Dashboards are code: edit this file, run it, commit the JSON. The kustomization in
this folder packs every JSON file into a ConfigMap labelled ``grafana_dashboard=1``
which the kube-prometheus-stack Grafana sidecar auto-loads.

    python3 monitoring/grafana/generate_dashboards.py          # writes dashboards/*.json
    python3 monitoring/grafana/generate_dashboards.py --check  # CI: fail if JSON is stale
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "dashboards"
PROM = {"type": "prometheus", "uid": "${datasource}"}
LOKI = {"type": "loki", "uid": "loki"}


# --------------------------------------------------------------------------- #
# Panel builders
# --------------------------------------------------------------------------- #
class Layout:
    """Places panels left-to-right on Grafana's 24-column grid."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.row_h = 0
        self.next_id = 1

    def place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def newline(self) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0

    def pid(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def row(lay: Layout, title: str) -> dict:
    lay.newline()
    panel = {"type": "row", "title": title, "id": lay.pid(), "collapsed": False,
             "gridPos": {"x": 0, "y": lay.y, "w": 24, "h": 1}, "panels": []}
    lay.y += 1
    return panel


def _targets(exprs, ds=PROM):
    out = []
    for i, e in enumerate(exprs):
        expr, legend = (e if isinstance(e, tuple) else (e, ""))
        out.append({"refId": chr(65 + i), "datasource": ds, "expr": expr,
                    "legendFormat": legend, "range": True})
    return out


def stat(lay, title, expr, unit="short", w=6, h=4, thresholds=None, decimals=None, desc=""):
    steps = thresholds or [{"color": "green", "value": None}]
    panel = {
        "type": "stat", "title": title, "id": lay.pid(), "description": desc,
        "datasource": PROM, "gridPos": lay.place(w, h), "targets": _targets([expr]),
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "justifyMode": "center",
                    "textMode": "auto"},
    }
    if decimals is not None:
        panel["fieldConfig"]["defaults"]["decimals"] = decimals
    return panel


def gauge(lay, title, expr, unit="percentunit", w=6, h=6, minv=0, maxv=1, steps=None, desc=""):
    return {
        "type": "gauge", "title": title, "id": lay.pid(), "description": desc,
        "datasource": PROM, "gridPos": lay.place(w, h), "targets": _targets([expr]),
        "fieldConfig": {"defaults": {"unit": unit, "min": minv, "max": maxv,
                                     "thresholds": {"mode": "absolute", "steps": steps or [
                                         {"color": "red", "value": None},
                                         {"color": "orange", "value": 0.99},
                                         {"color": "green", "value": 0.999}]}},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "showThresholdMarkers": True},
    }


def ts(lay, title, exprs, unit="short", w=12, h=8, stack=False, desc="", thresholds=None):
    defaults = {
        "unit": unit,
        "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 15 if stack else 8,
                   "gradientMode": "opacity", "showPoints": "never", "spanNulls": True,
                   "stacking": {"mode": "normal" if stack else "none", "group": "A"}},
    }
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["custom"]["thresholdsStyle"] = {"mode": "line+area"}
    return {
        "type": "timeseries", "title": title, "id": lay.pid(), "description": desc,
        "datasource": PROM, "gridPos": lay.place(w, h), "targets": _targets(exprs),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def table(lay, title, expr, w=12, h=8, desc=""):
    return {
        "type": "table", "title": title, "id": lay.pid(), "description": desc,
        "datasource": PROM, "gridPos": lay.place(w, h),
        "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "format": "table",
                     "instant": True, "range": False}],
        "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def logs(lay, title, expr, w=24, h=10):
    return {
        "type": "logs", "title": title, "id": lay.pid(), "datasource": LOKI,
        "gridPos": lay.place(w, h),
        "targets": [{"refId": "A", "datasource": LOKI, "expr": expr}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none"},
    }


def dashboard(uid, title, tags, panels, templating, desc, refresh="30s", time_from="now-6h"):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["czarintel", *tags],
        "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
        "refresh": refresh, "timezone": "utc", "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
        "annotations": {"list": [
            {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
             "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
             "name": "Annotations & Alerts", "type": "dashboard"},
            {"datasource": PROM, "enable": True, "iconColor": "#8F3BB8", "name": "Deployments",
             "expr": 'changes(kube_deployment_status_observed_generation{namespace=~"$namespace"}[2m]) > 0',
             "titleFormat": "Rollout {{deployment}}", "step": "60s"}]},
        "links": [{"title": "CzarIntel runbooks", "type": "link", "icon": "doc",
                   "url": "https://runbooks.czarintel.io", "targetBlank": True}],
        "templating": {"list": templating},
        "panels": panels,
    }


def ds_var():
    return {"name": "datasource", "label": "Prometheus", "type": "datasource",
            "query": "prometheus", "current": {}, "hide": 0, "refresh": 1}


def query_var(name, label, query, multi=True, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": PROM,
            "query": {"query": query, "refId": f"{name}-var"}, "definition": query,
            "refresh": 2, "multi": multi, "includeAll": include_all, "sort": 1,
            "current": {}, "hide": 0}


# --------------------------------------------------------------------------- #
# Dashboards
# --------------------------------------------------------------------------- #
def api_slo() -> dict:
    lay = Layout()
    sel = 'namespace=~"$namespace", job=~"$job"'
    red_green = [{"color": "green", "value": None}, {"color": "orange", "value": 0.005},
                 {"color": "red", "value": 0.01}]
    p = [
        row(lay, "Service Level Objectives - 99.9% availability / p95 < 500ms (30d)"),
        gauge(lay, "Availability (30d)",
              f'1 - (sum(increase(http_requests_total{{{sel}, status=~"5.."}}[30d])) '
              f'/ clamp_min(sum(increase(http_requests_total{{{sel}}}[30d])), 1))',
              desc="Share of non-5xx responses over the rolling 30-day SLO window."),
        gauge(lay, "Error budget remaining",
              f'1 - ((sum(increase(http_requests_total{{{sel}, status=~"5.."}}[30d])) '
              f'/ clamp_min(sum(increase(http_requests_total{{{sel}}}[30d])), 1)) / 0.001)',
              steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0.25},
                     {"color": "green", "value": 0.5}],
              desc="1.0 = untouched budget, 0 = SLO breached. Freeze releases below 25%."),
        stat(lay, "p95 latency (5m)",
             f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))',
             unit="s", h=6, decimals=3,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.3},
                         {"color": "red", "value": 0.5}]),
        stat(lay, "Error-budget burn rate (1h)",
             f'(sum(rate(http_requests_total{{{sel}, status=~"5.."}}[1h])) '
             f'/ clamp_min(sum(rate(http_requests_total{{{sel}}}[1h])), 1e-9)) / 0.001',
             unit="x", h=6, decimals=2,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6},
                         {"color": "red", "value": 14.4}],
             desc="14.4x burns 2% of the monthly budget in 1h (Google SRE multi-window alert)."),
        row(lay, "Golden signals"),
        ts(lay, "Request rate by status",
           [(f'sum by (status) (rate(http_requests_total{{{sel}}}[5m]))', "{{status}}")],
           unit="reqps", stack=True),
        ts(lay, "Error ratio (5xx)",
           [(f'sum(rate(http_requests_total{{{sel}, status=~"5.."}}[5m])) '
             f'/ clamp_min(sum(rate(http_requests_total{{{sel}}}[5m])), 1e-9)', "5xx ratio")],
           unit="percentunit", thresholds=red_green),
        ts(lay, "Latency percentiles",
           [(f'histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))',
             f"p{int(q * 100)}") for q in (0.5, 0.9, 0.95, 0.99)],
           unit="s", thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.5}]),
        ts(lay, "In-flight requests by pod",
           [(f'sum by (pod) (http_requests_in_flight{{{sel}}})', "{{pod}}")]),
        row(lay, "Endpoints & dependencies"),
        table(lay, "Top endpoints by traffic (5m)",
              f'topk(10, sum by (method, path) (rate(http_requests_total{{{sel}}}[5m])))'),
        table(lay, "Slowest endpoints - p95 (5m)",
              f'topk(10, histogram_quantile(0.95, sum by (path, le) '
              f'(rate(http_request_duration_seconds_bucket{{{sel}}}[5m]))))'),
        ts(lay, "Dependency health (1 = reachable)",
           [(f'czarintel_dependency_up{{{sel}}}', "{{dependency}} @ {{pod}}")], w=12, h=6),
        ts(lay, "Platform service status",
           [(f'czarintel_service_up{{{sel}}}', "{{service}}")], w=12, h=6),
        row(lay, "Saturation"),
        ts(lay, "CPU usage vs request (cores)",
           [('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=~"$namespace", '
             'container="api-service"}[5m]))', "{{pod}}"),
            ('sum(kube_pod_container_resource_requests{namespace=~"$namespace", container="api-service", '
             'resource="cpu"})', "total requested")], unit="short", w=8),
        ts(lay, "Memory working set",
           [('sum by (pod) (container_memory_working_set_bytes{namespace=~"$namespace", '
             'container="api-service"})', "{{pod}}")], unit="bytes", w=8),
        ts(lay, "HPA replicas",
           [('kube_horizontalpodautoscaler_status_current_replicas{namespace=~"$namespace"}', "current"),
            ('kube_horizontalpodautoscaler_spec_max_replicas{namespace=~"$namespace"}', "max")], w=8),
        row(lay, "Logs"),
        logs(lay, "api-service errors (Loki)",
             '{namespace=~"$namespace", app="api-service"} | json | level=~"ERROR|CRITICAL"'),
    ]
    tmpl = [ds_var(),
            query_var("namespace", "Namespace", 'label_values(http_requests_total, namespace)'),
            query_var("job", "Job", 'label_values(http_requests_total{namespace=~"$namespace"}, job)')]
    return dashboard("czarintel-api-slo", "CzarIntel / API Service - SLO & Golden Signals",
                     ["slo", "api"], p, tmpl,
                     "Availability and latency SLOs, error-budget burn and RED metrics for api-service.")


def platform_overview() -> dict:
    lay = Layout()
    p = [
        row(lay, "Cluster health - $cluster"),
        stat(lay, "Nodes Ready", 'sum(kube_node_status_condition{condition="Ready", status="true"})', w=4),
        stat(lay, "Nodes NotReady", 'sum(kube_node_status_condition{condition="Ready", status="true"} == 0) or vector(0)',
             w=4, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        stat(lay, "Pods running", 'sum(kube_pod_status_phase{phase="Running"})', w=4),
        stat(lay, "Pods failing", 'sum(kube_pod_status_phase{phase=~"Failed|Pending|Unknown"})', w=4,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1},
                         {"color": "red", "value": 5}]),
        stat(lay, "Firing alerts (critical)", 'count(ALERTS{alertstate="firing", severity="critical"}) or vector(0)',
             w=4, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        stat(lay, "ArgoCD apps OutOfSync", 'count(argocd_app_info{sync_status!="Synced"}) or vector(0)', w=4,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
        row(lay, "Capacity"),
        gauge(lay, "Cluster CPU utilisation",
              '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', w=6,
              steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.7},
                     {"color": "red", "value": 0.85}]),
        gauge(lay, "Cluster memory utilisation",
              '1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)', w=6,
              steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.75},
                     {"color": "red", "value": 0.9}]),
        gauge(lay, "CPU requests committed",
              'sum(kube_pod_container_resource_requests{resource="cpu"}) '
              '/ sum(kube_node_status_allocatable{resource="cpu"})', w=6, maxv=1.2,
              steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.8},
                     {"color": "red", "value": 1}]),
        gauge(lay, "Memory requests committed",
              'sum(kube_pod_container_resource_requests{resource="memory"}) '
              '/ sum(kube_node_status_allocatable{resource="memory"})', w=6, maxv=1.2,
              steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.8},
                     {"color": "red", "value": 1}]),
        ts(lay, "CPU by node", [('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))',
                                 "{{instance}}")], unit="percentunit"),
        ts(lay, "Memory by node", [('1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes',
                                    "{{instance}}")], unit="percentunit"),
        ts(lay, "Root filesystem usage",
           [('1 - node_filesystem_avail_bytes{mountpoint="/", fstype!="tmpfs"} '
             '/ node_filesystem_size_bytes{mountpoint="/", fstype!="tmpfs"}', "{{instance}}")],
           unit="percentunit", thresholds=[{"color": "transparent", "value": None},
                                           {"color": "red", "value": 0.85}]),
        ts(lay, "Network throughput",
           [('sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|cali.*"}[5m]))', "rx {{instance}}"),
            ('-sum by (instance) (rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cali.*"}[5m]))', "tx {{instance}}")],
           unit="Bps"),
        row(lay, "Workloads"),
        table(lay, "Container restarts (1h)",
              'topk(15, sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[1h])) > 0)'),
        table(lay, "Deployments not at desired replicas",
              'kube_deployment_spec_replicas != kube_deployment_status_replicas_available'),
        ts(lay, "CPU by namespace",
           [('sum by (namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))', "{{namespace}}")],
           stack=True),
        ts(lay, "Memory by namespace",
           [('sum by (namespace) (container_memory_working_set_bytes{container!=""})', "{{namespace}}")],
           unit="bytes", stack=True),
    ]
    tmpl = [ds_var(), query_var("cluster", "Cluster", "label_values(up, cluster)", multi=False, include_all=True),
            query_var("namespace", "Namespace", "label_values(kube_namespace_labels, namespace)")]
    return dashboard("czarintel-platform", "CzarIntel / Platform - Multi-Cloud Cluster Overview",
                     ["kubernetes", "capacity"], p, tmpl,
                     "Node, capacity and workload health across EKS (primary), AKS (DR) and GKE (data).")


def mysql() -> dict:
    lay = Layout()
    inst = 'instance=~"$instance"'
    p = [
        row(lay, "Availability"),
        stat(lay, "MySQL up", f'min(mysql_up{{{inst}}})', w=4,
             thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
        stat(lay, "Uptime", f'min(mysql_global_status_uptime{{{inst}}})', unit="s", w=4),
        stat(lay, "Replication lag", f'max(mysql_slave_status_seconds_behind_master{{{inst}}}) or vector(0)',
             unit="s", w=4, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 5},
                                        {"color": "red", "value": 30}]),
        stat(lay, "Connections used",
             f'max(mysql_global_status_threads_connected{{{inst}}} / mysql_global_variables_max_connections{{{inst}}})',
             unit="percentunit", w=4, thresholds=[{"color": "green", "value": None},
                                                  {"color": "orange", "value": 0.7}, {"color": "red", "value": 0.85}]),
        stat(lay, "Buffer pool hit ratio",
             f'1 - sum(rate(mysql_global_status_innodb_buffer_pool_reads{{{inst}}}[5m])) '
             f'/ clamp_min(sum(rate(mysql_global_status_innodb_buffer_pool_read_requests{{{inst}}}[5m])), 1)',
             unit="percentunit", w=4, decimals=3,
             thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.95},
                         {"color": "green", "value": 0.99}]),
        stat(lay, "Slow queries (1h)", f'sum(increase(mysql_global_status_slow_queries{{{inst}}}[1h]))', w=4,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10},
                         {"color": "red", "value": 100}]),
        row(lay, "Throughput"),
        ts(lay, "Queries per second",
           [(f'sum by (instance) (rate(mysql_global_status_queries{{{inst}}}[5m]))', "{{instance}}")], unit="qps"),
        ts(lay, "Commands by type",
           [(f'topk(8, sum by (command) (rate(mysql_global_status_commands_total{{{inst}}}[5m])))', "{{command}}")],
           unit="ops", stack=True),
        ts(lay, "Threads", [(f'sum by (instance) (mysql_global_status_threads_connected{{{inst}}})', "connected {{instance}}"),
                            (f'sum by (instance) (mysql_global_status_threads_running{{{inst}}})', "running {{instance}}")]),
        ts(lay, "InnoDB row operations",
           [(f'sum by (operation) (rate(mysql_global_status_innodb_row_ops_total{{{inst}}}[5m]))', "{{operation}}")],
           unit="ops"),
        row(lay, "ProxySQL"),
        ts(lay, "ProxySQL backend status (1=ONLINE)",
           [('proxysql_connection_pool_status{status="1"}', "HG{{hostgroup}} {{endpoint}}")], w=12, h=6),
        ts(lay, "ProxySQL queries routed by hostgroup",
           [('sum by (hostgroup) (rate(proxysql_connection_pool_queries_total[5m]))', "HG{{hostgroup}}")],
           unit="qps", w=12, h=6),
    ]
    tmpl = [ds_var(), query_var("instance", "Instance", "label_values(mysql_up, instance)")]
    return dashboard("czarintel-mysql", "CzarIntel / Data - MySQL & ProxySQL",
                     ["mysql", "database"], p, tmpl,
                     "MySQL 8.0 GTID cluster health, replication lag and ProxySQL routing.")


def security() -> dict:
    lay = Layout()
    p = [
        row(lay, "Zero Trust posture"),
        stat(lay, "Gatekeeper violations", 'sum(gatekeeper_violations{enforcement_action="deny"}) or vector(0)', w=6,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
             desc="Live resources that violate a deny-mode OPA constraint (audit loop)."),
        stat(lay, "Admission requests denied (1h)",
             'sum(increase(gatekeeper_validation_request_count{admission_status="deny"}[1h])) or vector(0)', w=6,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
        stat(lay, "Falco critical events (24h)",
             'sum(increase(falcosecurity_falcosidekick_falco_events_total{priority=~"Critical|Emergency|Alert"}[24h])) or vector(0)',
             w=6, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        stat(lay, "Namespaces without default-deny",
             'count(kube_namespace_labels unless on (namespace) kube_networkpolicy_labels{networkpolicy="default-deny-all"}) or vector(0)',
             w=6, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
             desc="Includes system namespaces; application namespaces must be zero (NIST SC-7)."),
        row(lay, "Supply chain & vulnerabilities"),
        stat(lay, "Critical CVEs in running images",
             'sum(trivy_image_vulnerabilities{severity="Critical"}) or vector(0)', w=8,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        stat(lay, "High CVEs in running images",
             'sum(trivy_image_vulnerabilities{severity="High"}) or vector(0)', w=8,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1},
                         {"color": "red", "value": 20}]),
        stat(lay, "Expiring TLS certs (< 14d)",
             'count(certmanager_certificate_expiration_timestamp_seconds - time() < 14 * 86400) or vector(0)', w=8,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        table(lay, "Vulnerabilities by workload",
              'topk(20, sum by (namespace, resource_name, severity) '
              '(trivy_image_vulnerabilities{severity=~"Critical|High"}))', w=24),
        row(lay, "Access & audit"),
        ts(lay, "Kubernetes API requests denied (403)",
           [('sum by (resource, verb) (rate(apiserver_request_total{code="403"}[5m]))', "{{verb}} {{resource}}")]),
        ts(lay, "Gatekeeper violations by constraint",
           [('sum by (constraint_kind) (gatekeeper_violations)', "{{constraint_kind}}")], stack=True),
        logs(lay, "Host audit - privilege escalation & identity changes (auditd via Fluent Bit)",
             '{job="fluent-bit"} |~ "key=\\"(identity|scope|actions|perm_mod|modules)\\""'),
        logs(lay, "SSH authentication failures",
             '{job="fluent-bit"} |~ "sshd.*(Failed|Invalid user|authentication failure)"', h=8),
    ]
    tmpl = [ds_var(), query_var("namespace", "Namespace", "label_values(kube_namespace_labels, namespace)")]
    return dashboard("czarintel-security", "CzarIntel / Security - Zero Trust & Compliance Posture",
                     ["security", "compliance"], p, tmpl,
                     "OPA Gatekeeper, Falco, Trivy and audit evidence mapped to SOC2 CC6/CC7, "
                     "PCI-DSS 10/11 and NIST 800-53 AU/SI families.", refresh="1m", time_from="now-24h")


DASHBOARDS = {
    "api-slo.json": api_slo,
    "platform-overview.json": platform_overview,
    "mysql.json": mysql,
    "security-posture.json": security,
}


def render() -> dict[str, str]:
    return {name: json.dumps(fn(), indent=2, sort_keys=False) + "\n" for name, fn in DASHBOARDS.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if committed JSON differs from generator output")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, content in render().items():
        path = OUT_DIR / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.write_text(content)
            print(f"wrote {path.relative_to(OUT_DIR.parent.parent.parent)}")
    if stale:
        print(f"stale dashboards (re-run generator): {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
