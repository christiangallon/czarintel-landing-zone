# MySQL: Data Tier (RDS, In-Cluster Chart, VM Fleet and ProxySQL)

> **CzarIntel Landing Zone** | Tool guide | Sources: [`terraform/modules/aws`](../../terraform/modules/aws), [`helm/mysql`](../../helm/mysql), [`argocd/applications/mysql-app.yml`](../../argocd/applications/mysql-app.yml), [`ansible/roles/mysql`](../../ansible/roles/mysql), [`ansible/roles/proxysql`](../../ansible/roles/proxysql)

The landing zone supports three MySQL deployment models. Each has its own purpose, and all three share the same controls: encryption, least privilege, monitoring and backups.

| Model | Where | Use it for | Source |
|---|---|---|---|
| **Amazon RDS for MySQL 8.0** | AWS private subnets | **Production system of record** (cardholder data and ePHI) | Terraform AWS module |
| **In-cluster StatefulSet** | `staging` namespace (EKS), or the DR site (AKS) | Staging parity, ephemeral environments, DR restore target | `helm/mysql` via ArgoCD `mysql-app` |
| **VM fleet with ProxySQL** | EC2 / on-prem VMs | Clients that must self-manage MySQL (licensing, plugins, lift-and-shift) | Ansible `mysql` plus `proxysql` roles |

## 1. Amazon RDS (production)

| Setting | Value | Control |
|---|---|---|
| Engine / class | MySQL `8.0` on `db.t3.medium`; 50 GB gp3, autoscaling to 4× | n/a |
| Availability | **Multi-AZ** synchronous standby | SOC2 A1.2, NIST CP-10 |
| Encryption at rest | `storage_encrypted` with the landing-zone **KMS CMK**; Performance Insights encrypted with the same key | HIPAA 164.312(a)(2)(iv), PCI-DSS 3.5 |
| Encryption in transit | Parameter group `require_secure_transport = 1` | HIPAA 164.312(e)(1), PCI-DSS 4.2 |
| Credentials | `manage_master_user_password`: the master password is generated and **rotated by Secrets Manager** (output `aws_rds_master_secret_arn`). **IAM database authentication** is enabled for applications (15-minute tokens instead of passwords) | NIST IA-5, PCI-DSS 8.3 |
| Backups | Automated, **14-day** retention, **PITR** to any second in the window, window 03:00-04:00 UTC, `copy_tags_to_snapshot` | NIST CP-9, HIPAA 164.308(a)(7) |
| Protection | `deletion_protection` and a final snapshot when `environment = prod` | NIST CP-9 |
| Network | Private DB subnets only, security group from EKS nodes; `allow-api-to-mysql` NetworkPolicy permits api-service egress to `10.10.128.0/17:3306` | PCI-DSS 1.3, NIST SC-7 |
| Logging | Slow query log (`long_query_time = 1`) | NIST AU-12 |

To connect an application without storing any password, do three things. First, create a DB user once with `CREATE USER czapp_iam IDENTIFIED WITH AWSAuthenticationPlugin AS 'RDS'; ALTER USER czapp_iam REQUIRE SSL;`. Second, grant `rds-db:connect` on `arn:aws:rds-db:us-east-1:<account>:dbuser:<DbiResourceId>/czapp_iam` to the workload's IRSA role. Third, connect with a token:

```bash
TOKEN=$(aws rds generate-db-auth-token --hostname "$RDS_HOST" --port 3306 --username czapp_iam --region us-east-1)
mysql --host="$RDS_HOST" --user=czapp_iam --password="$TOKEN" --ssl-mode=VERIFY_IDENTITY \
      --ssl-ca=/etc/ssl/certs/rds-global-bundle.pem
```

## 2. In-cluster MySQL (`helm/mysql`)

A single-replica StatefulSet running `mysql:8.0.39`, with a `mysqld-exporter v0.15.1` sidecar, managed by ArgoCD `mysql-app`:

- **Credentials.** In ArgoCD the chart uses `existingSecret: mysql-credentials`, which External Secrets syncs from Azure Key Vault. Standalone installs generate random 32-character passwords once. `lookup` preserves them across upgrades, and the Secret carries `helm.sh/resource-policy: keep`.
- **Safety.** ArgoCD `prune: false`, so a mistaken Git deletion cannot delete the database. The backup PVC is `resource-policy: keep`.
- **Security context.** UID/GID 999, `runAsNonRoot`, `RuntimeDefault` seccomp, all capabilities dropped, no privilege escalation.
- **Tuning** for a 1 Gi limit: 512 M buffer pool, `innodb_flush_log_at_trx_commit=1`, `sync_binlog=1` (durable), binlog retained 7 days, `local_infile=OFF`, `skip_name_resolve`.
- **Transport.** `require_secure_transport = OFF` in this chart. Staging traffic is confined by NetworkPolicy: only `api-service` and the backup job can reach port 3306 (`mysql-accept-from-api-and-backup`). **Regulated data must live in RDS**, where TLS is enforced.
- **Nightly backup CronJob** (`templates/backup-cronjob.yaml`):
  - 02:30 America/New_York, `concurrencyPolicy: Forbid`.
  - Runs `mysqldump --single-transaction --set-gtid-purged=ON --source-data=2 --routines --events --triggers`, gzip-compressed with a `.sha256` sidecar, written to the `-backup` PVC.
  - Retention 7 days. Read-only root FS, no ServiceAccount token.
  - Its pods are labelled `app.kubernetes.io/name: mysql-backup` for the NetworkPolicy allow-list.

```bash
helm lint --strict helm/mysql
helm template mysql helm/mysql | kubeconform -strict -summary     # 7 resources, all valid
kubectl -n staging create job --from=cronjob/mysql-backup mysql-backup-manual   # on-demand backup
```

## 3. VM fleet: MySQL with ProxySQL (Ansible)

`ansible-playbook configure-mysql.yml` (serial: 1) converges:

**`mysql` role**:
- MySQL 8.0 **GTID** primary and replicas (`mysql_role` host var); `require_secure_transport`.
- `innodb_redo_log_encrypt` / `innodb_undo_log_encrypt`, binlog expiry 7 days.
- Anonymous users and the `test` database removed. The app user is scoped to `10.%`.
- Passwords come from the `MYSQL_*` environment variables.

**`proxysql` role** (v2.6.3):
- **Writer hostgroup 10 / reader hostgroup 20**, with `mysql_replication_hostgroups` (`check_type="read_only"`). When a replica is promoted, ProxySQL moves it to the writer group automatically.
- `use_ssl=1` to every backend. `max_replication_lag=10` removes stale readers.
- `max_connections 2048` on the frontend. Ports **6033** (SQL) and **6032** (admin, local).
- Query rules send `SELECT … FOR UPDATE` to the writer and plain `SELECT` to readers.

## Monitoring

`mysqld-exporter` (chart sidecar or VM) plus the ProxySQL stats feed:

- the **`mysql` Grafana dashboard**: replication lag, connections, buffer-pool hit ratio, slow queries, ProxySQL backend status and hostgroup routing;
- the alerts `MySQLDown` (critical), `MySQLTooManyConnections` and `MySQLSlowQueries`.

See [Observability](observability.md) and the [incident runbook](../runbooks/incident-response.md#mysqldown).

## Backups and DR summary

| Tier | Mechanism | RPO | Restore |
|---|---|---|---|
| RDS | Multi-AZ plus automated backups, PITR (14 days) | ≤ 5 min | `aws rds restore-db-instance-to-point-in-time` |
| In-cluster | Nightly `mysqldump` CronJob (7 days) | ≤ 24 h | `gunzip -c dump \| mysql` |
| Cross-cloud (AWS → Azure) | Daily RDS logical dump to Azure Blob (**roadmap week 10**) | ≤ 24 h | Restore into `helm/mysql` on AKS |

Step-by-step procedures: [Disaster Recovery runbook](../runbooks/disaster-recovery.md).
