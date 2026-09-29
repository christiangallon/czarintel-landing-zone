# Disaster Recovery Runbook

> **CzarIntel Landing Zone** | Runbook | Owner: Platform SRE | Review: quarterly, plus after every DR test
> Controls: HIPAA 164.308(a)(7)(ii)(A–C) · SOC2 A1.2, A1.3 · PCI-DSS 12.10.1 · NIST CP-2, CP-9, CP-10

This runbook covers four recovery paths, ordered from most to least likely:

| # | Scenario | Path | RPO | RTO |
|---|---|---|---|---|
| 1 | Bad data or accidental delete in production MySQL | [RDS point-in-time restore](#1-rds-point-in-time-restore-pitr) | ≤ 5 min | ≤ 1 h |
| 2 | Single-AZ loss or RDS primary failure | Automatic Multi-AZ failover ([§1.4](#14-multi-az-failover-no-action-normally-required)) | 0 (synchronous standby) | 1–2 min |
| 3 | Staging in-cluster MySQL corrupted or PVC lost | [Logical dump restore](#2-in-cluster-mysql-logical-dump-restore) | ≤ 24 h (nightly 02:30 ET) | ≤ 30 min |
| 4 | AWS us-east-1 regional outage | [Cross-cloud failover to Azure AKS](#3-regional-failover-to-azure-aks-via-argocd) | ≤ 24 h | ≤ 4 h |

These targets match [architecture overview §RPO/RTO](../architecture/overview.md).

**Ground rules**

- **Declare first, then act.** The Incident Commander (IC) declares a DR event in `#czarintel-incident` and opens an incident record (see [incident-response.md](incident-response.md)).
- **Break-glass is audited.** All actions use short-lived SSO sessions (`aws sso login`, `az login`). The `platform-admins` Entra ID group is the only group allowed to restore data, and CloudTrail and Azure Activity Log record every action.
- **Git stays the source of truth.** Infrastructure changes go through Terraform and workload changes go through ArgoCD. Record any emergency manual change in the incident timeline and reconcile it back into Git within 24 h.
- **Check first.** Run `python3 scripts/health_check.py` before and after every procedure and paste both outputs into the incident record.

---

## 1. RDS point-in-time restore (PITR)

Production MySQL is `czarintel-prod-mysql` (RDS MySQL 8.0, Multi-AZ, KMS-encrypted, `backup_retention_period` 14 days by default, `delete_automated_backups = false`, `deletion_protection = true` in prod). PITR always restores to a **new** instance. The original is never overwritten, so the forensic copy survives.

### 1.1 Find the restore point

```bash
export AWS_REGION=us-east-1 SRC=czarintel-prod-mysql
aws rds describe-db-instances --db-instance-identifier "$SRC" \
  --query 'DBInstances[0].[LatestRestorableTime,BackupRetentionPeriod,MultiAZ]' --output table
# Locate the bad statement: Performance Insights, or the slowquery/audit log exports in CloudWatch
aws logs filter-log-events --log-group-name "/aws/rds/instance/$SRC/audit" \
  --start-time "$(date -d '-2 hours' +%s000)" --filter-pattern '"DROP" || "DELETE" || "TRUNCATE"' --max-items 20
```

Choose a `RESTORE_TIME` (UTC, ISO-8601) **just before** the destructive statement.

### 1.2 Restore to a new instance

```bash
export RESTORE_TIME="2026-09-28T14:05:00Z" TGT="czarintel-prod-mysql-pitr-$(date -u +%Y%m%d%H%M)"
SUBNETS=$(aws rds describe-db-instances --db-instance-identifier "$SRC" --query 'DBInstances[0].DBSubnetGroup.DBSubnetGroupName' --output text)
SG=$(aws rds describe-db-instances --db-instance-identifier "$SRC" --query 'DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId' --output text)
PG=$(aws rds describe-db-instances --db-instance-identifier "$SRC" --query 'DBInstances[0].DBParameterGroups[0].DBParameterGroupName' --output text)

aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$SRC" --target-db-instance-identifier "$TGT" \
  --restore-time "$RESTORE_TIME" \
  --db-subnet-group-name "$SUBNETS" --vpc-security-group-ids "$SG" --db-parameter-group-name "$PG" \
  --multi-az --no-publicly-accessible --enable-iam-database-authentication \
  --deletion-protection --copy-tags-to-snapshot \
  --tags Key=CostCenter,Value=CZ-1000 Key=Incident,Value="$INCIDENT_ID"
aws rds wait db-instance-available --db-instance-identifier "$TGT"
```

The restored instance inherits KMS encryption from the source's backups. The subnet group, security group and parameter group are reused, so TLS-only (`require_secure_transport`) and private networking still apply.

### 1.3 Validate and cut over

1. **Validate** from a pod on the EKS nodes. The RDS security group accepts 3306 only from the EKS node security group. Compare row counts for the affected tables against expectations.

   ```bash
   kubectl -n production run mysql-verify --rm -it --restart=Never \
     --image=mysql:8.0.39 --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":999}}}' \
     -- mysql -h "<TGT endpoint>" -u "<read-only user>" -p --ssl-mode=REQUIRED \
        -e 'SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema = DATABASE();'
   ```

2. **Choose a strategy:**
   - **Partial recovery (preferred):** `mysqldump` only the affected tables from `$TGT` and load them into `$SRC`. The endpoint, secrets and Terraform state stay the same.
   - **Full cutover:** rename the instances (`$SRC` becomes `$SRC-old`, then `$TGT` becomes `$SRC`) with `aws rds modify-db-instance --new-db-instance-identifier ... --apply-immediately`. The endpoint DNS name follows the identifier, so the apps need no config change. Then run `terraform plan` and reconcile any drift (for example with `terraform apply -refresh-only`).

3. Restart the API so the connection pools reconnect: `kubectl -n production rollout restart deploy/api-service`.
4. Keep `$SRC-old` for at least 7 days as evidence. Delete it only with IC approval.

### 1.4 Multi-AZ failover (no action normally required)

RDS promotes the synchronous standby automatically, in about 60–120 s. Confirm the event, then check that the `MySQLDown` alert on the RDS exporter has cleared:

```bash
aws rds describe-events --source-identifier czarintel-prod-mysql --source-type db-instance --duration 60
```

To test it, or to force a failover: `aws rds reboot-db-instance --db-instance-identifier czarintel-prod-mysql --force-failover`. Run this in the **quarterly game day only**.

---

## 2. In-cluster MySQL logical dump restore

**Staging** runs MySQL 8.0.39 in-cluster (`helm/mysql`, ArgoCD app `mysql-staging`). A CronJob backs it up every night at **02:30 America/New_York**:

- command: `mysqldump --all-databases --single-transaction --quick --routines --events --triggers --set-gtid-purged=ON --source-data=2 | gzip`
- output: `/backup/mysql-<UTC stamp>.sql.gz` plus a `.sha256` file on the PVC `mysql-backup`
- retention: 7 days (`backup.retentionDays`)

### 2.1 Pause GitOps self-heal

```bash
argocd app set mysql-staging --sync-policy none        # stop self-heal fighting the restore
kubectl -n staging scale deploy/api-service --replicas=0  # quiesce writers
```

### 2.2 Pick and verify a dump

```bash
kubectl -n staging create job mysql-restore-ls --from=cronjob/mysql-backup --dry-run=client -o yaml \
  | yq '.spec.template.spec.containers[0].args=["ls -lh /backup; cd /backup && sha256sum -c *.sha256"]' \
  | kubectl apply -f -
kubectl -n staging logs -f job/mysql-restore-ls
```

Every dump you plan to use must print `OK`. **Never restore a dump that fails its checksum.**

### 2.3 Restore

The restore Job reuses the CronJob pod template and replaces only `args`. The container `command` stays `/bin/bash -ceu`, and `MYSQL_PWD` comes from the chart Secret. That keeps the `app.kubernetes.io/name=mysql-backup` label, which the staging NetworkPolicy uses to allow traffic to `mysql:3306`, and it keeps the same non-root security context, secret reference and PVC mount.

```bash
DUMP=mysql-20260928T063000Z.sql.gz
kubectl -n staging create job mysql-restore --from=cronjob/mysql-backup --dry-run=client -o yaml \
  | yq ".spec.template.spec.containers[0].args=[\"set -o pipefail; gunzip -c /backup/${DUMP} | mysql --host=\$MYSQL_HOST --port=\$MYSQL_PORT --user=root\"]" \
  | kubectl apply -f -
kubectl -n staging wait --for=condition=complete job/mysql-restore --timeout=30m
```

The dump includes `SET @@GLOBAL.GTID_PURGED`. If the server already has GTIDs, restore into a clean data directory first: scale the StatefulSet to 0, delete PVC `data-mysql-0`, scale back to 1, then run the Job.

### 2.4 Resume

```bash
kubectl -n staging scale deploy/api-service --replicas=2
argocd app set mysql-staging --sync-policy automated --self-heal --auto-prune
argocd app sync mysql-staging && python3 scripts/health_check.py -t staging-targets.yml
kubectl -n staging delete job mysql-restore mysql-restore-ls
```

---

## 3. Regional failover to Azure AKS via ArgoCD

Azure `eastus2` is a **warm** DR site. Terraform keeps the AKS cluster, ACR (`acrczarintelprod.azurecr.io`), Key Vault and Log Analytics running, and Entra ID groups map to AKS RBAC. The same Helm charts and ArgoCD Applications that run on EKS deploy to it.

### 3.1 Decision criteria (IC plus an exec sponsor)

- AWS Health Dashboard confirms a us-east-1 event affecting EKS, RDS or ELB, **and**
- `health_check.py` has been RED for more than 30 min with no mitigation ETA, **or**
- the business impact exceeds the cost of up to 24 h data loss (RPO).

### 3.2 Procedure

```bash
# 0. Identity: Entra ID is not in AWS, so SSO and kubectl auth still work
az login && az account set -s "<subscription>"
cd terraform && terraform output -raw azure_aks_cluster_name && terraform output -raw azure_resource_group
az aks get-credentials -g <rg> -n <aks> --overwrite-existing && kubelogin convert-kubeconfig -l azurecli

# 1. Baseline security on AKS (same manifests as EKS)
kubectl apply -f kubernetes/namespaces.yml -f kubernetes/rbac/roles.yml -f kubernetes/network-policies/default-deny.yml
helm upgrade --install gatekeeper gatekeeper/gatekeeper -n gatekeeper-system --create-namespace --version 3.17.1 --wait
kubectl apply -f kubernetes/opa-gatekeeper/require-labels.yml

# 2. Register AKS with ArgoCD. If the EKS-hosted ArgoCD is down, install ArgoCD v2.12.6 on AKS
#    exactly as scripts/bootstrap.sh does and point it at the same Git repo.
argocd cluster add "$(kubectl config current-context)" --name aks-dr
argocd proj add-destination czarintel aks-dr production --name   # AppProject only allows in-cluster by default

# 3. Retarget production Applications: destination aks-dr, images from ACR, DB = in-cluster MySQL
argocd app create api-service-dr --project czarintel \
  --repo https://github.com/czarintel/czarintel-landing-zone.git --path helm/api-service \
  --dest-name aks-dr --dest-namespace production --revision main \
  --values values-production.yaml \
  --helm-set image.repository=acrczarintelprod.azurecr.io/api-service \
  --helm-set config.DB_HOST=mysql.production.svc.cluster.local \
  --helm-set existingSecret=api-service-db
argocd app create mysql-dr --project czarintel \
  --repo https://github.com/czarintel/czarintel-landing-zone.git --path helm/mysql \
  --dest-name aks-dr --dest-namespace production --revision main \
  --helm-set auth.existingSecret=mysql-credentials --helm-set persistence.size=100Gi
argocd app sync mysql-dr && argocd app wait mysql-dr --health --timeout 900

# 4. Load the most recent cross-cloud dump (see §4), then start the API
argocd app sync api-service-dr && argocd app wait api-service-dr --health --timeout 600

# 5. DNS cutover: point api./app.czarintel.io at the AKS ingress (keep TTL low, e.g. 60 s, so cutover is fast)
# 6. Verify
python3 scripts/health_check.py
```

Secrets on AKS come from **Key Vault through the Secrets Store CSI driver** with Workload Identity (the AKS CSI identity already has a Key Vault access policy in Terraform). No secret is copied out of AWS Secrets Manager by hand. **Prerequisite (week 10):** pre-stage the `mysql-credentials` keys (`mysql-root-password`, `mysql-database`, `mysql-user`, `mysql-password`, `mysql-exporter-password`) and `api-service-db` in Key Vault, with a `SecretProviderClass` that syncs them into Kubernetes Secrets. Until then, create those Secrets during the failover from values held in Key Vault.

The Helm keys above (`image.repository`, `config.DB_HOST`, `existingSecret`, `auth.existingSecret`, `persistence.size`) match `helm/api-service/values.yaml` and `helm/mysql/values.yaml`. Game day (week 12) validates this command set end to end.

### 3.3 Failback

1. When AWS recovers, take a fresh dump from AKS MySQL and load it into RDS. For large datasets, use AWS DMS instead.
2. Flip DNS back to EKS, then scale the AKS apps to zero. Keep the AKS apps defined so the warm standby stays in place.
3. Delete the `*-dr` Applications only after a 48 h soak on AWS.

---

## 4. Cross-cloud data synchronisation (roadmap week 10)

**Current state.** In-region protection is complete: Multi-AZ, 14-day PITR, KMS, and versioned S3 backups (Standard-IA at 30 days, Glacier IR at 90 days). A cross-cloud copy is **not automated yet**. Until week 10, the SRE on call runs the manual procedure below **daily**, or at minimum before any planned high-risk change.

```bash
# Manual daily export from a host inside the VPC (e.g. a Jenkins agent pod with IRSA). Uses IAM DB auth
# (a MySQL user created WITH AWSAuthenticationPlugin), so no password is stored anywhere.
TOKEN=$(aws rds generate-db-auth-token --hostname "$RDS_HOST" --port 3306 --username backup_iam --region us-east-1)
mysqldump -h "$RDS_HOST" -u backup_iam --password="$TOKEN" --ssl-mode=VERIFY_IDENTITY --ssl-ca=rds-ca.pem \
  --single-transaction --set-gtid-purged=OFF --routines --events --triggers czarintel \
  | gzip | tee >(sha256sum > dump.sha256) \
  | aws s3 cp - "s3://$(terraform -chdir=terraform output -raw aws_backup_bucket)/mysql/$(date -u +%F).sql.gz" --sse aws:kms
```

**Week 10 deliverable** (tracked in the [README roadmap](../../README.md#12-week-delivery-roadmap)):

- A nightly Kubernetes CronJob runs in `production` with the IRSA role `backup_iam`. It does an RDS logical dump to S3 (KMS), then replicates it to **Azure Blob** (immutable, WORM 35 days) through Workload Identity Federation, and to **GCS** (`gcp_backup_bucket`, CMEK) for analytics sandboxes after de-identification.
- A `MySQLCrossCloudBackupStale` alert fires if the newest Azure object is more than 26 h old.
- A restore test runs weekly: the latest Azure dump is loaded into an ephemeral AKS MySQL and row counts are compared.

---

## 5. Testing and evidence

| Test | Frequency | Evidence kept |
|---|---|---|
| RDS PITR to a scratch instance, then delete | Monthly | CLI transcript, row-count diff |
| Staging logical dump restore | Monthly | Job logs, checksum output |
| Multi-AZ forced failover | Quarterly game day | RDS events, alert timeline |
| AKS failover (full §3) | Semi-annually (first run: week 12) | Timeline, measured RTO/RPO, `health_check.py` JSON |

Store the evidence with the compliance artifacts (400-day retention). Record measured RTO and RPO against targets, and log any miss as a problem ticket.
