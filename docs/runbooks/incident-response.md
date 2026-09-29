# Incident Response Runbook

> **CzarIntel Landing Zone** | Runbook | Owner: Platform SRE and Security | Review: quarterly, plus after every SEV1/SEV2
> Controls: HIPAA 164.308(a)(6) · SOC2 CC7.3, CC7.4, CC7.5 · PCI-DSS 12.10 · NIST IR-4, IR-5, IR-6, IR-8

Every Prometheus alert in [`api-alerts.yml`](../../monitoring/prometheus/custom-rules/api-alerts.yml) carries a `runbook_url` that links to an anchor in **§4** of this page. Section anchors equal the lower-cased alert name, so do not rename headings without updating the rules.

---

## 1. Severity and roles

| Sev | Definition | Examples | Response | Comms |
|---|---|---|---|---|
| **SEV1** | Customer-facing outage, or a confirmed security breach or PHI/PAN exposure | `ApiServiceDown`, `ErrorBudgetBurnFast`, GuardDuty high on prod, leaked production credential | Page (PagerDuty), IC within 5 min, all hands | Status page within 15 min, exec update every 30 min |
| **SEV2** | Degraded service or SLO at risk, or a contained security event | `HighErrorRate`, `MySQLDown` (staging), `PodCrashLooping` on prod | Page on-call, IC within 15 min | `#czarintel-incident` every 60 min |
| **SEV3** | No customer impact yet | `HighLatency`, `HPAMaxedOut`, `NodeDiskFillingUp` | Slack `#czarintel-alerts`, handle in business hours | Ticket |

**Roles:** Incident Commander (IC) coordinates and decides. Ops Lead executes. Comms Lead handles stakeholders and customers. Scribe keeps the timeline. For security incidents, add the **Security Lead**, who owns evidence and the regulatory clock.

**Regulatory clocks** (Security Lead plus Legal):

- HIPAA breach notification: without unreasonable delay, and no later than 60 days after discovery.
- PCI-DSS: notify the acquirer and card brands immediately on a suspected account-data compromise.
- Contractual SOC2 customer notices: per the MSA.

## 2. First five minutes (every alert)

```bash
python3 scripts/health_check.py                      # RAG snapshot; paste into the incident record
kubectl config current-context                       # confirm prod (EKS) vs DR (AKS)
argocd app list -p czarintel                         # anything OutOfSync / Degraded? was there a recent sync?
argocd app history api-service-production | tail -5  # correlate with the last deploy
```

- **Grafana** (`grafana.czarintel.io`, Entra ID SSO): open *CzarIntel / API Service - SLO & Golden Signals*, *CzarIntel / Platform - Multi-Cloud Cluster Overview* and *CzarIntel / Data - MySQL & ProxySQL*. Each dashboard has a "CzarIntel runbooks" link to `runbooks.czarintel.io`, which publishes this page. In Explore → Loki, filter by `{namespace="production", container="api-service"} |= "ERROR"`. Node journal and auditd logs have `job="fluent-bit-node"`.
- **If a deploy happened in the last 60 minutes, roll back first and debug later:** `git revert <promotion commit>` in the GitOps repo, and ArgoCD reconciles within about 3 minutes (see [argocd.md](../tools/argocd.md)).
- **Check the change timeline:** the ArgoCD Slack notifications, the Jenkins build history, and CloudTrail `eventName` in the last hour.

## 3. Security playbooks

### 3.1 GuardDuty finding (severity ≥ 7)

**Flow:** GuardDuty (EKS audit logs, runtime, S3, RDS, malware) sends findings to the EventBridge rule `czarintel-prod-guardduty-high-severity`, which publishes to the SNS topic `czarintel-prod-security-alerts` (KMS-encrypted). From there, Security Lead is paged.

1. **Triage (15 min).** Pull the finding and classify it as a true positive, a benign true positive or a false positive.

   ```bash
   DET=$(aws guardduty list-detectors --query 'DetectorIds[0]' --output text)
   aws guardduty get-findings --detector-id "$DET" --finding-ids "$FINDING_ID" \
     --query 'Findings[0].{Type:Type,Sev:Severity,Resource:Resource,First:Service.EventFirstSeen,Count:Service.Count}'
   ```

2. **Contain** based on the resource type. Contain first and preserve evidence. **Do not delete anything.**

   | Resource | Containment |
   |---|---|
   | EKS pod (for example `Execution:Runtime/*`, `CryptoCurrency:Runtime/*`) | `kubectl -n <ns> label pod <pod> quarantine=true` and apply a deny-all NetworkPolicy that selects `quarantine=true`. `kubectl cordon <node>`. Capture evidence with `kubectl debug node/<node>` (journal, `crictl inspect`). Only then delete the pod, so the Deployment reschedules it onto a clean node. |
   | EC2 node | Swap its security group for an isolation SG with no ingress or egress. Snapshot the EBS volumes (encrypted by default). Terminate only after forensics; the EKS node group replaces it. |
   | IAM principal (`UnauthorizedAccess:IAMUser/*`, `Credential Access`) | Follow [§3.2](#32-leaked-or-compromised-credential). |
   | S3 (`Exfiltration:S3/*`, `Policy:S3/BucketBlockPublicAccessDisabled`) | Re-apply Terraform, which restores the public access block and TLS-only policy. Review CloudTrail data events and S3 server access logs. |

3. **Eradicate.** Find the root cause (a vulnerable image, an over-broad IRSA policy). Rebuild the image through the pipeline with Trivy gates. Tighten the policy through a Terraform PR.
4. **Recover.** Uncordon or replace nodes, then confirm `health_check.py` is GREEN.
5. **Evidence.** Keep the finding JSON, CloudTrail extract and kubectl transcripts in the incident record (400-day retention). This covers SOC2 CC7.3 and CC7.4 and HIPAA 164.308(a)(6)(ii).

### 3.2 Leaked or compromised credential

Examples: a GitHub secret-scanning alert, a Trivy `fs --scanners secret` hit in Jenkins, a key found on a paste site, or a GuardDuty IAM finding.

The platform has **no long-lived cloud keys by design**. People use SSO; CI uses OIDC; workloads use IRSA, Workload Identity or GKE Workload Identity; RDS uses a Secrets Manager–managed password plus IAM auth. A leaked key is therefore either an exception that must be removed, or a third-party token.

| Credential type | Revoke immediately | Then |
|---|---|---|
| AWS access key (`AKIA…`) | `aws iam update-access-key --status Inactive --access-key-id <id> --user-name <u>`, then `delete-access-key` | Attach an explicit deny for sessions issued before now (`aws:TokenIssueTime`). Query CloudTrail for the key: `aws cloudtrail lookup-events --lookup-attributes AttributeKey=AccessKeyId,AttributeValue=<id>`. |
| AWS SSO / role session | Revoke active sessions from the IAM console (*Revoke active sessions*). Disable the user in Entra ID. | Check which role sessions were used. |
| RDS master password | `aws secretsmanager rotate-secret --secret-id "$(terraform output -raw aws_rds_master_secret_arn)"` | Run `kubectl rollout restart` on consumers. Check `/aws/rds/instance/czarintel-prod-mysql/audit`. |
| Entra ID user or service principal | Revoke sign-in sessions and disable the account. Remove any client secret (federated credentials are preferred). | Review the Entra sign-in logs in Log Analytics. |
| Kubernetes ServiceAccount token | Delete the token Secret or the ServiceAccount. Projected tokens expire within about 1 h. | Review the EKS audit log in CloudWatch. |
| GitHub / Slack / PagerDuty token | Revoke in the vendor console. Rotate the Key Vault or Secrets Manager entry. The CSI driver re-syncs it. | Roadmap week 11 replaces the remaining PATs and webhooks with a GitHub App (1 h installation tokens) and Slack OAuth. |

After revoking:

1. Purge the secret from Git history if it was committed (`git filter-repo`), and force-push after approval.
2. Add a detection rule (a Trivy secret pattern or a pre-commit hook) for that key type.
3. Record the exposure window (first-seen to revoked) for breach assessment.

### 3.3 SLO burn-rate incident

The API SLO is **99.9% availability over 30 days**, which is an error budget of 43.2 minutes. Two alerts protect it:

- `ErrorBudgetBurnFast` pages when the burn rate exceeds 14.4× over **both** 1 h and 5 m. At that rate the budget is gone in about 2 days.
- `HighErrorRate` (5xx above 1%) catches sustained failures.

Procedure: see [§4.3 ErrorBudgetBurnFast](#errorbudgetburnfast). When the budget is exhausted, **freeze feature deploys** until the rolling 30-day availability is back above 99.9%. Only fixes and security patches are promoted during the freeze, with IC approval recorded on the promotion PR.

---

## 4. Alert runbooks

Format for each alert: **Meaning**, then **Diagnose**, then **Mitigate**, then **Escalate**.

### HighErrorRate

**§4.1 · critical · 5xx ratio > 1% for 5 min.** Users are seeing errors.

- **Diagnose:**
  - Grafana *API Service - SLO & Golden Signals*, *Request rate by status* panel.
  - Loki: `{namespace="production", container="api-service"} |= "ERROR"`.
  - `kubectl -n production get pods -l app.kubernetes.io/name=api-service`.
  - Is MySQL healthy (see `MySQLDown`)? Was there a recent sync in `argocd app history`?
- **Mitigate:**
  - After a recent deploy, run `git revert` on the promotion commit.
  - If dependency errors come from the DB, go to [MySQLTooManyConnections](#mysqltoomanyconnections) or [MySQLDown](#mysqldown).
  - If a single bad pod is the cause, run `kubectl delete pod` on it.
- **Escalate:** SEV2. Raise to SEV1 if `ErrorBudgetBurnFast` also fires.

### HighLatency

**§4.2 · warning · p95 > 500 ms for 5 min.**

- **Diagnose:**
  - Check whether it affects every route or only one (latency panels in the SLO dashboard).
  - CPU throttling: `container_cpu_cfs_throttled_periods_total`.
  - Is the HPA at max (`HPAMaxedOut`)?
  - MySQL slow queries ([MySQLSlowQueries](#mysqlslowqueries)). For RDS, use Performance Insights to find the top SQL.
- **Mitigate:** Scale out (`kubectl -n production scale deploy/api-service --replicas=<n>` is temporary, since ArgoCD self-heal reverts it; raise `autoscaling.maxReplicas` through Git). Add the missing index through a migration PR.
- **Escalate:** SEV3. Raise to SEV2 if it lasts more than 30 min or error rate starts climbing.

### ErrorBudgetBurnFast

**§4.3 · critical · burn rate > 14.4× over both 1 h and 5 m.**

- **Diagnose:** Same as [HighErrorRate](#higherrorrate). Also check how much budget remains in the *Error budget remaining* and *Error-budget burn rate (1h)* panels.
- **Mitigate:** Roll back straight away if a deploy happened in the last 2 h. Otherwise shed load or fail the dependency closed. Declare SEV1 and open a status-page incident.
- **After the incident:** Apply the deploy freeze policy ([§3.3](#33-slo-burn-rate-incident)). A post-mortem is mandatory.

### ApiServiceDown

**§4.4 · critical · no healthy api-service scrape targets for 3 min.** The API may be down entirely, or Prometheus may have lost visibility.

- **Diagnose:**
  - `kubectl -n production get deploy,rs,pods -l app.kubernetes.io/name=api-service`.
  - `kubectl -n production describe pod <pod>` (ImagePullBackOff, a Gatekeeper denial in events, or OOMKilled?).
  - `python3 scripts/health_check.py --url https://api.czarintel.io/health`. If the external check is GREEN, suspect a scrape or NetworkPolicy problem: the `allow-prometheus-scrape` policy and the ServiceMonitor labels.
- **Mitigate:**
  - Image pull failure: check the ECR tag exists and the node role can pull, then revert the promotion.
  - Gatekeeper denial: fix the labels in the chart. **Never disable the constraint.**
  - Whole cluster down: go to the [DR runbook](disaster-recovery.md).
- **Escalate:** SEV1.

### PodCrashLooping

**§4.5 · critical · a container keeps restarting.**

- **Diagnose:**
  - `kubectl -n <ns> logs <pod> --previous`.
  - `kubectl -n <ns> describe pod <pod>` and read *Last State* (OOMKilled means limits are too low; exit code 1 means an app or config error).
  - Common causes: a missing Secret or ConfigMap key, a DB that is unreachable (NetworkPolicy), or a read-only root filesystem when the app writes outside `/tmp`.
- **Mitigate:** Revert the offending change. Raise memory limits through Git only if the data shows real growth.
- **Escalate:** SEV2 if production capacity drops below HPA `minReplicas` (4).

### DeploymentReplicasMismatch

**§4.6 · warning · available ≠ desired replicas for 15 min.**

- **Diagnose:**
  - `kubectl -n <ns> get pods -o wide | grep -v Running`.
  - Pending pods: `kubectl describe pod` (insufficient CPU or memory, taints, PVC binding).
  - `kubectl get nodes` and the cluster autoscaler logs.
- **Mitigate:** Add node capacity (managed node group `desired_size` or max through Terraform). Resolve a PodDisruptionBudget deadlock during node drains.
- **Escalate:** SEV3. Raise to SEV2 if it coincides with an error-rate alert.

### HPAMaxedOut

**§4.7 · warning · HPA at maxReplicas for 15 min.** There is no headroom left.

- **Diagnose:** Is this legitimate traffic growth or abuse? Check the *Request rate by status* panel and the ingress logs in Loki. Also rule out a latency regression, which inflates CPU per request.
- **Mitigate:** For real growth, raise `autoscaling.maxReplicas` in `values-production.yaml` through a PR. For abuse, apply a rate limit or WAF rule.
- **Escalate:** SEV3. Capacity review in the weekly ops meeting.

### NodeHighCPU

**§4.8 · warning · node CPU > 85% for 10 min.**

- **Diagnose:** `kubectl top pods -A --sort-by=cpu | head`, then `kubectl describe node <node>` (allocated vs capacity). Look for noisy neighbours without limits. Gatekeeper and K8S-003 should prevent those, so check for exemptions.
- **Mitigate:** Cordon and drain the node so pods reschedule. Scale the node group. Fix the requests and limits of the offending workload.
- **Escalate:** SEV3.

### NodeDiskFillingUp

**§4.9 · warning · disk predicted to fill within 24 h.**

- **Diagnose:** `kubectl debug node/<node> -it --image=busybox -- df -h /host`. The usual culprits are container images (`crictl images`), container logs, and journald on nodes without a size cap.
- **Mitigate:** `crictl rmi --prune`. Cap journald with `SystemMaxUse` in `/etc/systemd/journald.conf`, and add the cap to the CIS baseline if it is missing. Increase the EBS volume size in Terraform. For a PVC, expand the PersistentVolumeClaim (gp3 supports online expansion).
- **Escalate:** SEV3. Raise to SEV2 if the volume is a MySQL data PVC.

### MySQLDown

**§4.10 · critical · `mysql_up == 0` or the metric is absent for 1 min.**

- **Diagnose:**
  - In-cluster (staging): `kubectl -n staging get pods -l app.kubernetes.io/name=mysql` and `kubectl -n staging logs mysql-0 -c mysql --previous`. Check the PVC status.
  - Exporter only: if `mysql` is Running but the exporter is not, the exporter password may be wrong.
  - RDS (production): `aws rds describe-events --source-identifier czarintel-prod-mysql --duration 60` (Multi-AZ failover in progress?).
- **Mitigate:**
  - A failover is automatic. Once it finishes, run `rollout restart` on the API to reset its connection pools.
  - Data corruption: go to [DR runbook §1](disaster-recovery.md#1-rds-point-in-time-restore-pitr) (RDS) or [§2](disaster-recovery.md#2-in-cluster-mysql-logical-dump-restore) (in-cluster).
- **Escalate:** SEV1 for production, SEV2 for staging.

### MySQLTooManyConnections

**§4.11 · warning · connections > 80% of `max_connections` (200 in-cluster) for 5 min.**

- **Diagnose:**
  - `SHOW PROCESSLIST;` and count by user and host.
  - Look for leaked connections after a deploy (pool size × replicas).
  - For long-running transactions, query `information_schema.innodb_trx`.
- **Mitigate:** Kill idle or long-running sessions (`KILL <id>`). Reduce the app pool size or HPA max. On RDS, scale the instance class through Terraform. For VM fleets, ProxySQL multiplexing absorbs spikes.
- **Escalate:** SEV2 if connection errors are showing up in `HighErrorRate`.

### MySQLSlowQueries

**§4.12 · warning · more than 10 slow queries (> `long_query_time` = 1 s) per 5 min, for 10 min.**

- **Diagnose:** RDS: Performance Insights top SQL, or the CloudWatch `slowquery` log export. In-cluster: the slow log in the container. Run `EXPLAIN` on the top offenders.
- **Mitigate:** Add indexes through a reviewed migration. Roll back the query change if it came with a deploy.
- **Escalate:** SEV3. Raise to SEV2 if `HighLatency` also fires.

---

## 5. Post-incident

1. Hold a **blameless post-mortem within 5 business days** for every SEV1 and SEV2. It covers the timeline, root cause, what went well and what did not, and action items with owners and due dates.
2. Turn action items into tickets. Every detection gap gets a new alert or compliance check (`scripts/compliance_scan.py`) so it is caught automatically next time.
3. Update this runbook and the [DR runbook](disaster-recovery.md) where they turned out to be wrong.
4. Attach the evidence bundle (timeline, health snapshots, CloudTrail and kubectl transcripts) to the SOC2 evidence store (400-day retention).

## 6. Exercises

| Exercise | Frequency |
|---|---|
| Tabletop: ransomware or credential leak (Security plus Legal) | Semi-annually |
| Game day: kill api-service pods, force an RDS failover, simulate a GuardDuty finding | Quarterly (first: roadmap week 12) |
| Alert fire-drill: `amtool alert add` for each critical alert to test PagerDuty routing | Monthly |
