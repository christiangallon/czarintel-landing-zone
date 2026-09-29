# Ansible: Configuration Management and CIS Hardening

> **CzarIntel Landing Zone** | Tool guide | Source: [`ansible/`](../../ansible) (operator quick reference: [`ansible/README.md`](../../ansible/README.md))

Terraform creates machines. Ansible makes every VM (Jenkins VMs, self-managed Kubernetes workers, MySQL, ProxySQL, Docker build hosts) converge to the **same audited baseline**. Every run is idempotent, so `--check --diff` doubles as a **drift report**.

## Execution model

| Aspect | Implementation |
|---|---|
| Engine | `ansible-core 2.17`, collections pinned in `requirements.yml` and installed to `./collections` |
| Inventory | `inventory/hosts.yml` (static) or `inventory/aws_ec2.yml` (dynamic, filtered to `tag:Project=czarintel`, grouped by the `czarintel:role` tag). Both produce the same groups: `jenkins*`, `docker_hosts`, `k8s_nodes`, `mysql`, `proxysql` |
| Access | SSH **certificates** signed by the org CA (Vault SSH engine), trusted through `ssh_trusted_user_ca_keys`, and limited to the `ansible` and `sre` groups. Optional `bastion_host` becomes a `ProxyJump`; SSM works too. The dynamic inventory uses the operator's SSO/OIDC session, so there are no static AWS keys |
| Secrets | Environment variables only (`MYSQL_*`, `PROXYSQL_*`, `FLUENT_BIT_TLS_DIR`), populated from AWS Secrets Manager or Key Vault at run time. Nothing secret is committed |
| Rollout safety | `serial` on every play: 30% for hardening, 50% for agents, and **1** for Kubernetes nodes (drain, harden, uncordon), Jenkins, MySQL and ProxySQL |
| Pre-flight | `site.yml` asserts Ubuntu ≥ 22.04 or RHEL ≥ 9 and at least 2 GiB free before touching anything |

## Playbooks

| Playbook | Hosts | Purpose |
|---|---|---|
| `site.yml` | all | Pre-flight → harden → agents → docker → jenkins → mysql |
| `harden.yml` | `all:!k8s_nodes` (30%), then `k8s_nodes` (1 at a time, drained via delegated `kubectl`) | `cis_hardening` role |
| `install-agents.yml` | `all:!k8s_nodes` | `node_exporter` plus `fluent_bit`. Kubernetes nodes get the equivalent DaemonSets |
| `configure-docker.yml` | `docker_hosts` | `docker_hardening` (CIS Docker Benchmark) |
| `configure-jenkins.yml` | `jenkins` | VM-based Jenkins controller and agents (an alternative to the in-cluster controller) |
| `configure-mysql.yml` | `mysql`, `proxysql` | GTID primary/replica, then ProxySQL read/write split |

## Roles

### `cis_hardening`

`cis_level: 2` is set in `group_vars/all.yml`; the role default is 1. Task files are tagged, so one control family can be applied on its own (`--tags cis_ssh`).

| Tag | Task file | Highlights |
|---|---|---|
| `cis_filesystems` | `filesystems.yml` | Blacklists unused filesystems and protocols (cramfs, squashfs, udf, dccp, sctp, rds, tipc…); `/tmp` mounted `nosuid,nodev,noexec`; `/dev/shm` hardened |
| `cis_services` | `services.yml` | Removes legacy packages (telnet, rsh, NIS…), chrony pointed at AWS Time Sync (`169.254.169.123`) and `time.google.com` |
| `cis_network` | `sysctl.yml` | `sysctl-cis.conf.j2`: no redirects or source routing, rp_filter, syncookies, martian logging. IP forwarding only where `cis_ip_forward_allowed` (Kubernetes nodes) |
| `cis_auditd` | `auditd.yml` | `audit.rules.j2`: identity, sudoers, time change, module load, privileged commands, mount, delete. **Immutable (`-e 2`) at Level 2**. Log rotation keeps 10 × 100 MB |
| `cis_ssh` | `ssh.yml` | `sshd_config.j2`: `PermitRootLogin no`, certificate-based auth via `TrustedUserCAKeys`, `AllowGroups`, modern ciphers, MACs and KEX only, `MaxAuthTries 4`, 300 s idle timeout, legal banner. Validated with `sshd -t` before reload |
| `cis_accounts` | `accounts.yml` | `login.defs` (max 365 days, min 1, warn 7), umask 027, 900 s shell timeout, sudo I/O logging to `/var/log/sudo.log` |

### Other roles

| Role | Highlights |
|---|---|
| `node_exporter` | v1.8.2, SHA-256-verified download, dedicated system user, sandboxed systemd unit (`ProtectSystem=strict`, `NoNewPrivileges`), post-install `/metrics` probe |
| `fluent_bit` | journald, auditd and sudo inputs plus per-group `fluent_bit_extra_inputs`; filesystem buffering. **mTLS** to `loki.czarintel.io:443`: CA, client cert and key are copied from `FLUENT_BIT_TLS_DIR` when set |
| `docker_hardening` | `daemon.json`: `userns-remap`, `no-new-privileges`, `icc: false`, `live-restore`, log rotation. Post-apply **assertion** that userns is active (CIS Docker 2.8) |
| `jenkins` | LTS controller bound to localhost behind the ingress; agents connect via WebSocket (no inbound agent port) with Trivy, Helm and kubectl |
| `mysql` | MySQL 8.0, GTID replication, `require_secure_transport`, binlog expiry 7 days, redo/undo log encryption, anonymous users and test DB removed; app user limited to `10.%` |
| `proxysql` | v2.6.3, writer hostgroup 10 / reader hostgroup 20 with replication-hostgroup auto-failover, SSL to backends, `max_connections 2048`, ports 6033 (SQL) and 6032 (admin) |

See [MySQL](mysql.md) for how these fit with RDS and the in-cluster chart.

## Usage

```bash
cd ansible
pip install "ansible-core==2.17.*" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections

export MYSQL_ROOT_PASSWORD="$(aws secretsmanager get-secret-value --secret-id czarintel/mysql/root --query SecretString --output text)"
export FLUENT_BIT_TLS_DIR="$HOME/.czarintel/logging-mtls"   # ca.crt, client.crt, client.key

ansible-playbook site.yml --check --diff            # drift report, no changes
ansible-playbook site.yml                           # converge everything
ansible-playbook harden.yml --tags cis_ssh          # one CIS control family
ansible-playbook -i inventory/aws_ec2.yml site.yml --limit jenkins
```

## Quality gates

These run in GitHub Actions (`platform-quality.yml` → `ansible-lint`) on every PR. They pass on the current tree:

```bash
yamllint -c .yamllint .
ansible-lint                       # production profile
ansible-playbook --syntax-check site.yml
```

## Managed node groups

EKS, AKS and GKE managed node groups should run vendor-hardened images (Bottlerocket, CIS AMIs, COS). `harden.yml` targets self-managed or custom-AMI workers. Its `kubectl drain` is delegated to the first Jenkins agent, which obtains a kubeconfig via OIDC.

## Compliance mapping

| Control | Evidence |
|---|---|
| NIST CM-2, CM-6, PCI-DSS 2.2 | Codified CIS L1/L2 baseline; `--check --diff` drift report |
| NIST CM-7, PCI-DSS 2.2.4 | Kernel module blacklist, removed legacy services |
| NIST AC-17, IA-2, IA-5, PCI-DSS 8.3 | SSH certificates, no root login, strong crypto |
| HIPAA 164.312(b), NIST AU-2, AU-9, AU-12, PCI-DSS 10.2 / 10.5 | Immutable auditd rules shipped off-host over mTLS |
| NIST SC-8, SC-13, HIPAA 164.312(e)(1) | TLS-only MySQL, ProxySQL SSL, mTLS log transport |
