from datetime import datetime, timedelta, timezone

import boto3
from moto import mock_aws

import compliance_scan as cs

GOOD_DEPLOY = {
    "kind": "Deployment", "metadata": {"name": "api"},
    "spec": {"template": {"spec": {
        "securityContext": {"runAsNonRoot": True},
        "containers": [{"name": "api", "image": "ghcr.io/czarintel/api:1.2.3",
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True},
                        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}]}}},
}


def _by_id(findings):
    return {f.check_id: f for f in findings}


def test_compliant_workload_passes_all_checks():
    findings = cs.check_workload(GOOD_DEPLOY, "x.yml")
    assert findings and all(f.passed for f in findings)


def test_insecure_workload_fails_expected_checks():
    bad = {"kind": "CronJob", "metadata": {"name": "bad"},
           "spec": {"jobTemplate": {"spec": {"template": {"spec": {
               "hostNetwork": True,
               "containers": [{"name": "c", "image": "nginx:latest", "securityContext": {"privileged": True}}]}}}}}}
    f = _by_id(cs.check_workload(bad, "bad.yml"))
    for cid in ("K8S-001", "K8S-002", "K8S-003", "K8S-004", "K8S-005", "K8S-007"):
        assert not f[cid].passed, cid
    assert f["K8S-002"].severity == "critical"
    assert "164.312(a)(1)" in f["K8S-001"].frameworks["HIPAA"]


def test_network_policy_detection():
    docs = [({"kind": "Namespace", "metadata": {"name": "prod"}}, "ns.yml"),
            ({"kind": "Namespace", "metadata": {"name": "open"}}, "ns.yml"),
            ({"kind": "NetworkPolicy", "metadata": {"name": "deny", "namespace": "prod"},
              "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}, "np.yml")]
    res = {f.resource.split("/")[-1]: f.passed for f in cs.check_network_policies(docs)}
    assert res == {"open": False, "prod": True}


def test_dockerfile_checks(tmp_path):
    good = tmp_path / "Dockerfile"
    good.write_text("FROM python:3.12-slim AS build\nRUN pip install x\n"
                    "FROM python:3.12-slim\nCOPY --from=build /x /x\nUSER 10001\nHEALTHCHECK CMD true\n")
    assert all(f.passed for f in cs.check_dockerfile(good, tmp_path))
    good.write_text("FROM ubuntu\nRUN apt-get update\n")
    f = _by_id(cs.check_dockerfile(good, tmp_path))
    assert not f["DOCKER-001"].passed and not f["DOCKER-002"].passed and not f["DOCKER-003"].passed


def test_terraform_secret_detection(tmp_path):
    (tmp_path / "terraform").mkdir()
    (tmp_path / "terraform" / "main.tf").write_text(
        'provider "aws" {\n  access_key = "AKIAABCDEFGHIJKLMNOP"\n}\n'
        'resource "aws_db_instance" "db" {\n  engine = "mysql"\n}\n')
    f = _by_id(cs.check_terraform(tmp_path))
    assert not f["TF-002"].passed and not f["TF-003"].passed


def test_this_repository_is_compliant():
    """Regression gate: the landing zone itself must pass every static check."""
    findings = cs.scan_repo()
    failed = [(f.check_id, f.resource) for f in findings if not f.passed]
    assert len(findings) > 30
    assert failed == []


def test_summary_and_exit_code(tmp_path, capsys):
    rc = cs.main(["repo", "--format", "json", "-o", str(tmp_path / "r.json"), "--fail-on", "low"])
    assert rc == 0
    assert '"score_pct": 100.0' in (tmp_path / "r.json").read_text()


@mock_aws
def test_aws_scan_detects_gaps_and_passes_after_remediation():
    region = "us-east-1"
    s = boto3.Session(region_name=region)
    s3 = s.client("s3")
    s3.create_bucket(Bucket="czarintel-open")
    iam = s.client("iam")
    iam.create_user(UserName="legacy")
    iam.create_access_key(UserName="legacy")

    before = _by_id(cs.scan_aws(s, region, now=datetime.now(timezone.utc) + timedelta(days=120)))
    assert not before["AWS-001"].passed  # no CloudTrail
    assert not before["AWS-002"].passed  # no GuardDuty
    assert not before["AWS-003"].passed  # public access not blocked
    assert not before["AWS-006"].passed  # key older than 90 days
    assert not before["AWS-007"].passed

    s3.create_bucket(Bucket="czarintel-trail")
    for b in ("czarintel-open", "czarintel-trail"):
        s3.put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True})
    ct = s.client("cloudtrail")
    ct.create_trail(Name="org", S3BucketName="czarintel-trail", IsMultiRegionTrail=True)
    ct.start_logging(Name="org")
    s.client("guardduty").create_detector(Enable=True)
    s.client("ec2").enable_ebs_encryption_by_default()

    after = cs.scan_aws(s, region)
    for cid in ("AWS-001", "AWS-002", "AWS-003", "AWS-006", "AWS-007"):
        relevant = [f for f in after if f.check_id == cid]
        assert relevant and all(f.passed for f in relevant), cid
