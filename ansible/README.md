# Ansible — Configuration Management & CIS Hardening

Converges every VM in the landing zone (Jenkins, self-managed K8s workers, MySQL, ProxySQL)
to a CIS Level 1/2 baseline, then layers observability agents and middleware.

## Layout

| Path | Purpose |
|------|---------|
| `site.yml` | Master playbook — pre-flight → harden → agents → docker → jenkins → mysql |
| `harden.yml` | CIS baseline. K8s nodes are drained/uncordoned one at a time |
| `install-agents.yml` | Prometheus node_exporter + Fluent Bit (→ Loki) |
| `configure-docker.yml` | CIS Docker Benchmark daemon hardening on build agents |
| `configure-jenkins.yml` | Jenkins LTS controller + WebSocket inbound agents with Trivy/Helm/kubectl |
| `configure-mysql.yml` | MySQL 8.0 GTID primary/replica + ProxySQL read/write split |
| `inventory/hosts.yml` | Static inventory (groups: `jenkins_*`, `k8s_nodes`, `mysql`, `proxysql`, `docker_hosts`) |
| `inventory/aws_ec2.yml` | Dynamic AWS inventory producing the same groups from the `czarintel:role` tag |

## Roles

| Role | Highlights | Controls |
|------|-----------|----------|
| `cis_hardening` | modprobe blacklist, `/tmp` + `/dev/shm` noexec, sysctl, auditd rules (immutable at L2), sshd (`PermitRootLogin no`, cert-only auth), login.defs, sudo logging | NIST CM-6, CM-7, AC-17, AU-2, AU-12 · PCI 2.2, 10.2 · HIPAA 164.312(b) |
| `node_exporter` | checksum-verified install, sandboxed systemd unit, post-install probe | NIST SI-4 · SOC2 CC7.2 |
| `fluent_bit` | journald + auditd + sudo + container logs, filesystem buffering, TLS to Loki | NIST AU-6, AU-9 · PCI 10.5 |
| `docker_hardening` | userns-remap, no-new-privileges, icc off, live-restore, log rotation, asserted post-apply | CIS Docker 2.x · NIST CM-7 |
| `jenkins` | LTS controller bound to localhost behind ingress, agents via WebSocket (no inbound ports) | NIST SA-11, CM-3 |
| `mysql` | TLS-only transport, GTID replication, redo/undo encryption, password policy, test DB/anon removal | PCI 8.x · HIPAA 164.312(a)(2)(iv) |
| `proxysql` | replication-hostgroup auto-failover, SSL to backends, read/write split | SOC2 A1.2 |

## Usage

```bash
cd ansible
pip install ansible-core==2.17.* ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections

# Secrets come from Vault / AWS Secrets Manager — never from the repo
export MYSQL_ROOT_PASSWORD=$(aws secretsmanager get-secret-value --secret-id czarintel/mysql/root --query SecretString --output text)
export MYSQL_REPL_PASSWORD=...  PROXYSQL_MONITOR_PASSWORD=...  PROXYSQL_ADMIN_PASSWORD=...  MYSQL_APP_PASSWORD=...
export FLUENT_BIT_TLS_DIR=$HOME/.czarintel/logging-mtls   # ca.crt, client.crt, client.key for the Loki mTLS ingress

ansible-playbook site.yml --check --diff          # drift report, no changes
ansible-playbook site.yml                          # converge everything
ansible-playbook harden.yml --tags cis_ssh         # single CIS control family
ansible-playbook -i inventory/aws_ec2.yml site.yml --limit jenkins
```

## Quality gates

```bash
yamllint -c .yamllint .
ansible-lint                 # production profile
ansible-playbook --syntax-check site.yml
```

> Managed K8s node groups (EKS/AKS/GKE) should use hardened images (Bottlerocket / CIS AMIs).
> `harden.yml` targets self-managed or custom-AMI worker nodes; `kubectl` is delegated to the
> first Jenkins agent, which must hold a kubeconfig obtained via OIDC.
