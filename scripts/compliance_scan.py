#!/usr/bin/env python3
"""
CzarIntel compliance scanner.

Two scanners share one findings model, each check mapped to HIPAA, SOC2,
PCI-DSS and NIST 800-53 controls:

* ``repo``  - static checks over this repository (Kubernetes manifests,
              Dockerfiles, Terraform, Ansible SSH baseline). Runs offline in CI.
* ``aws``   - live account checks via boto3 (CloudTrail, GuardDuty, S3 public
              access / encryption, IAM root MFA & key age, EBS default encryption).

    python3 scripts/compliance_scan.py repo
    python3 scripts/compliance_scan.py aws --region us-east-1 --format json -o compliance-report.json
    python3 scripts/compliance_scan.py all --fail-on high          # CI gate

Exit codes: 0 pass, 1 findings at/above --fail-on, 3 scanner error.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import yaml

LOG = logging.getLogger("compliance_scan")
REPO_ROOT = Path(__file__).resolve().parent.parent
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Control catalogue: check id -> (title, severity, framework mapping)
CONTROLS: dict[str, tuple[str, str, dict[str, list[str]]]] = {
    "K8S-001": ("Containers must run as non-root", "high",
                {"NIST": ["AC-6", "CM-7"], "PCI": ["2.2.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(1)"]}),
    "K8S-002": ("Privileged containers / privilege escalation forbidden", "critical",
                {"NIST": ["AC-6(10)"], "PCI": ["2.2.5"], "SOC2": ["CC6.8"], "HIPAA": ["164.312(a)(1)"]}),
    "K8S-003": ("CPU/memory limits required", "medium",
                {"NIST": ["SC-5"], "PCI": ["6.4"], "SOC2": ["A1.1"], "HIPAA": ["164.308(a)(7)"]}),
    "K8S-004": ("Images must be pinned (no :latest)", "high",
                {"NIST": ["CM-2", "SI-7"], "PCI": ["6.3.2"], "SOC2": ["CC8.1"], "HIPAA": ["164.312(c)(1)"]}),
    "K8S-005": ("Read-only root filesystem", "medium",
                {"NIST": ["CM-7", "SI-7"], "PCI": ["2.2"], "SOC2": ["CC6.8"], "HIPAA": ["164.312(c)(1)"]}),
    "K8S-006": ("Namespaces must have a default-deny NetworkPolicy", "high",
                {"NIST": ["SC-7", "AC-4"], "PCI": ["1.3"], "SOC2": ["CC6.6"], "HIPAA": ["164.312(e)(1)"]}),
    "K8S-007": ("hostNetwork/hostPID/hostIPC forbidden", "critical",
                {"NIST": ["SC-39"], "PCI": ["2.2.1"], "SOC2": ["CC6.8"], "HIPAA": ["164.312(a)(1)"]}),
    "DOCKER-001": ("Dockerfile must switch to a non-root USER", "high",
                   {"NIST": ["AC-6"], "PCI": ["2.2.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(1)"]}),
    "DOCKER-002": ("Base image must be pinned", "medium",
                   {"NIST": ["CM-2"], "PCI": ["6.3.2"], "SOC2": ["CC8.1"], "HIPAA": ["164.312(c)(1)"]}),
    "DOCKER-003": ("HEALTHCHECK defined", "low",
                   {"NIST": ["SI-4"], "PCI": ["10.6"], "SOC2": ["A1.2"], "HIPAA": ["164.308(a)(7)"]}),
    "TF-001": ("Terraform state backend encrypted", "high",
               {"NIST": ["SC-28"], "PCI": ["3.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(2)(iv)"]}),
    "TF-002": ("No hard-coded credentials in Terraform", "critical",
               {"NIST": ["IA-5(7)"], "PCI": ["8.3.1"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(d)"]}),
    "TF-003": ("Storage/database encryption at rest enabled", "high",
               {"NIST": ["SC-28"], "PCI": ["3.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(2)(iv)"]}),
    "SSH-001": ("SSH root login disabled in baseline", "high",
                {"NIST": ["AC-6(2)", "AC-17"], "PCI": ["8.2.1"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(1)"]}),
    "SSH-002": ("SSH password authentication disabled", "high",
                {"NIST": ["IA-2", "IA-5"], "PCI": ["8.3"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(d)"]}),
    "AWS-001": ("Multi-region CloudTrail logging enabled", "critical",
                {"NIST": ["AU-2", "AU-12"], "PCI": ["10.2"], "SOC2": ["CC7.2"], "HIPAA": ["164.312(b)"]}),
    "AWS-002": ("GuardDuty enabled", "high",
                {"NIST": ["SI-4"], "PCI": ["11.5"], "SOC2": ["CC7.2"], "HIPAA": ["164.308(a)(1)(ii)(D)"]}),
    "AWS-003": ("S3 buckets block public access", "critical",
                {"NIST": ["AC-3", "SC-7"], "PCI": ["1.3.1"], "SOC2": ["CC6.6"], "HIPAA": ["164.312(a)(1)"]}),
    "AWS-004": ("S3 buckets encrypted by default", "high",
                {"NIST": ["SC-28"], "PCI": ["3.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(2)(iv)"]}),
    "AWS-005": ("Root account MFA enabled", "critical",
                {"NIST": ["IA-2(1)"], "PCI": ["8.4"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(d)"]}),
    "AWS-006": ("IAM access keys rotated within 90 days", "medium",
                {"NIST": ["IA-5(1)"], "PCI": ["8.3.9"], "SOC2": ["CC6.1"], "HIPAA": ["164.308(a)(5)(ii)(D)"]}),
    "AWS-007": ("EBS encryption by default", "high",
                {"NIST": ["SC-28"], "PCI": ["3.5"], "SOC2": ["CC6.1"], "HIPAA": ["164.312(a)(2)(iv)"]}),
}


@dataclass
class Finding:
    check_id: str
    resource: str
    passed: bool
    detail: str = ""

    @property
    def title(self) -> str:
        return CONTROLS[self.check_id][0]

    @property
    def severity(self) -> str:
        return CONTROLS[self.check_id][1]

    @property
    def frameworks(self) -> dict[str, list[str]]:
        return CONTROLS[self.check_id][2]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(title=self.title, severity=self.severity, frameworks=self.frameworks)
        return d


# --------------------------------------------------------------------------- #
# Repository scanner
# --------------------------------------------------------------------------- #
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet"}
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(secret_key|access_key|password)\s*=\s*\"[^\"$\{]{8,}\""),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def _load_yaml_docs(path: Path) -> Iterator[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("cannot read %s: %s", path, exc)
        return
    if "{{" in text:          # Helm templates are scanned via rendered output in CI, skip raw
        return
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                yield doc
    except yaml.YAMLError as exc:
        LOG.warning("invalid YAML %s: %s", path, exc)


def _pod_spec(doc: dict) -> dict | None:
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    if kind in WORKLOAD_KINDS:
        return (spec.get("template") or {}).get("spec")
    return None


def check_workload(doc: dict, source: str) -> list[Finding]:
    spec = _pod_spec(doc)
    if spec is None:
        return []
    name = f"{source}:{doc.get('kind')}/{(doc.get('metadata') or {}).get('name', '?')}"
    out: list[Finding] = []
    pod_sc = spec.get("securityContext") or {}
    host_ns = [k for k in ("hostNetwork", "hostPID", "hostIPC") if spec.get(k)]
    out.append(Finding("K8S-007", name, not host_ns, f"enabled: {host_ns}" if host_ns else ""))

    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        cname = f"{name}/{c.get('name', '?')}"
        sc = c.get("securityContext") or {}
        non_root = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot")) is True or \
            (sc.get("runAsUser", pod_sc.get("runAsUser")) not in (None, 0))
        out.append(Finding("K8S-001", cname, non_root, "" if non_root else "runAsNonRoot not set"))
        escalates = sc.get("privileged") is True or sc.get("allowPrivilegeEscalation", True) is not False
        out.append(Finding("K8S-002", cname, not escalates,
                           "" if not escalates else "privileged or allowPrivilegeEscalation not false"))
        limits = (c.get("resources") or {}).get("limits") or {}
        has_limits = "memory" in limits and "cpu" in limits
        out.append(Finding("K8S-003", cname, has_limits, "" if has_limits else f"limits={limits}"))
        image = str(c.get("image", ""))
        pinned = "@sha256:" in image or (":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest"))
        out.append(Finding("K8S-004", cname, pinned, "" if pinned else f"image={image}"))
        ro = sc.get("readOnlyRootFilesystem") is True
        out.append(Finding("K8S-005", cname, ro, "" if ro else "readOnlyRootFilesystem not true"))
    return out


def check_network_policies(docs: Iterable[tuple[dict, str]]) -> list[Finding]:
    namespaces: dict[str, str] = {}
    denied: set[str] = set()
    for doc, src in docs:
        md = doc.get("metadata") or {}
        if doc.get("kind") == "Namespace":
            namespaces[md.get("name", "?")] = src
        elif doc.get("kind") == "NetworkPolicy":
            spec = doc.get("spec") or {}
            selects_all = not (spec.get("podSelector") or {}).get("matchLabels") and \
                not (spec.get("podSelector") or {}).get("matchExpressions")
            if selects_all and "ingress" not in spec and "Ingress" in (spec.get("policyTypes") or []):
                denied.add(md.get("namespace", "default"))
    return [Finding("K8S-006", f"{src}:Namespace/{ns}", ns in denied,
                    "" if ns in denied else "no empty-podSelector deny-ingress policy")
            for ns, src in sorted(namespaces.items())]


def check_dockerfile(path: Path, root: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(root))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    users = [ln.split(None, 1)[1] for ln in lines if ln.upper().startswith("USER ") and len(ln.split()) > 1]
    final_user = users[-1] if users else None
    non_root = final_user is not None and final_user.split(":")[0] not in ("root", "0")
    out = [Finding("DOCKER-001", rel, non_root, f"final USER={final_user}")]
    froms = [ln.split()[1] for ln in lines if ln.upper().startswith("FROM ") and len(ln.split()) > 1]
    stage_names = {ln.split()[-1].lower() for ln in lines if ln.upper().startswith("FROM ") and " AS " in ln.upper()}
    external = [f for f in froms if f.lower() not in stage_names and not f.startswith("$")]
    unpinned = [f for f in external if "@sha256:" not in f and (":" not in f or f.endswith(":latest"))]
    out.append(Finding("DOCKER-002", rel, not unpinned, f"unpinned: {unpinned}" if unpinned else ""))
    out.append(Finding("DOCKER-003", rel, any(ln.upper().startswith("HEALTHCHECK") for ln in lines)))
    return out


def check_terraform(root: Path) -> list[Finding]:
    tf_dir = root / "terraform"
    out: list[Finding] = []
    files = [p for p in tf_dir.rglob("*.tf") if ".terraform" not in p.parts]
    backend = tf_dir / "backend.tf"
    if backend.exists():
        enc = re.search(r"encrypt\s*=\s*true", backend.read_text())
        out.append(Finding("TF-001", "terraform/backend.tf", bool(enc)))
    for f in files:
        rel = str(f.relative_to(root))
        text = f.read_text(encoding="utf-8")
        hits = [p.pattern for p in SECRET_PATTERNS if p.search(text)]
        out.append(Finding("TF-002", rel, not hits, f"matched {hits}" if hits else ""))
        for m in re.finditer(r'resource\s+"(aws_db_instance|aws_rds_cluster|aws_ebs_volume)"\s+"(\w+)"\s*\{', text):
            body = text[m.end(): m.end() + 4000]
            ok = re.search(r"(storage_encrypted|encrypted)\s*=\s*true", body) is not None
            out.append(Finding("TF-003", f"{rel}:{m.group(1)}.{m.group(2)}", ok))
    return out


def check_ssh_baseline(root: Path) -> list[Finding]:
    tpl = root / "ansible/roles/cis_hardening/templates/sshd_config.j2"
    if not tpl.exists():
        return [Finding("SSH-001", str(tpl), False, "baseline template missing")]
    text = tpl.read_text()
    rel = str(tpl.relative_to(root))
    return [
        Finding("SSH-001", rel, re.search(r"^PermitRootLogin\s+no\b", text, re.M) is not None),
        Finding("SSH-002", rel, re.search(r"^PasswordAuthentication\s+no\b", text, re.M) is not None),
    ]


def scan_repo(root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    docs: list[tuple[dict, str]] = []
    for d in ("kubernetes", "ci-cd", "argocd"):
        for p in sorted((root / d).rglob("*.y*ml")) if (root / d).exists() else []:
            rel = str(p.relative_to(root))
            for doc in _load_yaml_docs(p):
                docs.append((doc, rel))
                findings.extend(check_workload(doc, rel))
    findings.extend(check_network_policies(docs))
    for df in sorted(root.rglob("Dockerfile")):
        if "collections" not in df.parts:
            findings.extend(check_dockerfile(df, root))
    if (root / "terraform").exists():
        findings.extend(check_terraform(root))
    findings.extend(check_ssh_baseline(root))
    return findings


# --------------------------------------------------------------------------- #
# AWS account scanner
# --------------------------------------------------------------------------- #
def scan_aws(session, region: str, now: datetime | None = None) -> list[Finding]:
    from botocore.exceptions import ClientError

    now = now or datetime.now(timezone.utc)
    out: list[Finding] = []

    ct = session.client("cloudtrail", region_name=region)
    trails = ct.describe_trails(includeShadowTrails=True).get("trailList", [])
    logging_multi = []
    for t in trails:
        if t.get("IsMultiRegionTrail"):
            status = ct.get_trail_status(Name=t["TrailARN"])
            if status.get("IsLogging"):
                logging_multi.append(t["Name"])
    out.append(Finding("AWS-001", f"account/{region}", bool(logging_multi),
                       f"logging trails: {logging_multi}" if logging_multi else "no logging multi-region trail"))

    gd = session.client("guardduty", region_name=region)
    detectors = gd.list_detectors().get("DetectorIds", [])
    enabled = [d for d in detectors if gd.get_detector(DetectorId=d).get("Status") == "ENABLED"]
    out.append(Finding("AWS-002", f"guardduty/{region}", bool(enabled)))

    s3 = session.client("s3", region_name=region)
    for b in s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        try:
            cfg = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            blocked = all(cfg.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls",
                                               "BlockPublicPolicy", "RestrictPublicBuckets"))
        except ClientError:
            blocked = False
        out.append(Finding("AWS-003", f"s3://{name}", blocked))
        try:
            rules = s3.get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]["Rules"]
            encrypted = bool(rules)
        except ClientError:
            encrypted = False
        out.append(Finding("AWS-004", f"s3://{name}", encrypted))

    iam = session.client("iam")
    summary = iam.get_account_summary().get("SummaryMap", {})
    out.append(Finding("AWS-005", "iam/root", summary.get("AccountMFAEnabled", 0) == 1))
    for page in iam.get_paginator("list_users").paginate():
        for u in page.get("Users", []):
            for k in iam.list_access_keys(UserName=u["UserName"]).get("AccessKeyMetadata", []):
                if k.get("Status") != "Active":
                    continue
                age = (now - k["CreateDate"]).days
                out.append(Finding("AWS-006", f"iam/{u['UserName']}/{k['AccessKeyId']}", age <= 90,
                                   f"age {age}d"))

    ec2 = session.client("ec2", region_name=region)
    ebs = ec2.get_ebs_encryption_by_default().get("EbsEncryptionByDefault", False)
    out.append(Finding("AWS-007", f"ec2/{region}", bool(ebs)))
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarize(findings: list[Finding]) -> dict:
    failed = [f for f in findings if not f.passed]
    by_fw: dict[str, dict[str, int]] = {}
    for f in findings:
        for fw, ctrls in f.frameworks.items():
            for c in ctrls:
                slot = by_fw.setdefault(fw, {}).setdefault(c, 0)
                by_fw[fw][c] = slot + (0 if f.passed else 1)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(findings),
        "passed": len(findings) - len(failed),
        "failed": len(failed),
        "score_pct": round(100 * (len(findings) - len(failed)) / len(findings), 1) if findings else 100.0,
        "failed_by_severity": {s: sum(1 for f in failed if f.severity == s) for s in SEVERITY_ORDER},
        "failing_controls_by_framework": {fw: sorted(c for c, n in m.items() if n) for fw, m in by_fw.items()},
    }


def render_text(findings: list[Finding], summary: dict, show_passed: bool = False) -> str:
    lines = [f"CzarIntel compliance scan - score {summary['score_pct']}% "
             f"({summary['passed']}/{summary['total_checks']} passed)", "=" * 72]
    for f in sorted(findings, key=lambda x: (-SEVERITY_ORDER[x.severity], x.check_id, x.resource)):
        if f.passed and not show_passed:
            continue
        mark = "PASS" if f.passed else "FAIL"
        lines.append(f"[{mark}] {f.severity.upper():<8} {f.check_id:<10} {f.resource}")
        if not f.passed:
            fw = "; ".join(f"{k} {','.join(v)}" for k, v in f.frameworks.items())
            lines.append(f"        {f.title}. {f.detail}".rstrip() + f"\n        -> {fw}")
    lines.append("")
    lines.append("Failing controls: " + json.dumps(summary["failing_controls_by_framework"]))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CzarIntel compliance scanner")
    ap.add_argument("target", choices=["repo", "aws", "all"])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("-o", "--output")
    ap.add_argument("--fail-on", choices=list(SEVERITY_ORDER), default="critical",
                    help="exit 1 if any failed finding is at or above this severity")
    ap.add_argument("--show-passed", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    scanners: list[Callable[[], list[Finding]]] = []
    if args.target in ("repo", "all"):
        scanners.append(lambda: scan_repo(args.root))
    if args.target in ("aws", "all"):
        def _aws() -> list[Finding]:
            import boto3
            sess = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
            return scan_aws(sess, args.region)
        scanners.append(_aws)

    findings: list[Finding] = []
    try:
        for s in scanners:
            findings.extend(s())
    except Exception as exc:  # noqa: BLE001 - surface any scanner failure as exit 3
        LOG.error("scanner error: %s", exc)
        return 3

    summary = summarize(findings)
    if args.format == "json":
        text = json.dumps({"summary": summary, "findings": [f.to_dict() for f in findings]}, indent=2) + "\n"
    else:
        text = render_text(findings, summary, args.show_passed)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    threshold = SEVERITY_ORDER[args.fail_on]
    return 1 if any(not f.passed and SEVERITY_ORDER[f.severity] >= threshold for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
