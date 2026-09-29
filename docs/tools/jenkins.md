# Jenkins and GitHub Actions: CI and Security Gates

> **CzarIntel Landing Zone** | Tool guide | Sources: [`ci-cd/Jenkinsfile`](../../ci-cd/Jenkinsfile), [`ci-cd/jenkins/jenkins-deployment.yml`](../../ci-cd/jenkins/jenkins-deployment.yml), [`.github/workflows/`](../../.github/workflows)

CI has two halves:

- **Jenkins** builds, scans and publishes the application image, then hands off to GitOps.
- **GitHub Actions** gates infrastructure and platform code on every pull request.

Neither side holds cluster-admin credentials, and neither uses a static cloud key.

## Jenkins controller

| Aspect | Implementation |
|---|---|
| Image | `jenkins/jenkins:2.462.3-lts-jdk17`, plugins installed by an init container from a pinned `plugins.txt` |
| Configuration | **JCasC** only (`CASC_JENKINS_CONFIG`). The system message warns that manual changes will be overwritten |
| Executors | `numExecutors: 0` on the controller. All builds run in ephemeral Kubernetes pods (`podRetention: never`, cap 20) |
| Authentication | Entra ID via `oic-auth`. Client ID, secret and tenant come from the `jenkins-oidc` Secret |
| Authorisation | `globalMatrix`: `czarintel-platform-admins` → Administer; `czarintel-developers` → Read/Build/Cancel |
| Network | `jenkins` namespace: `default-deny-all`, `jenkins-controller-ingress`, `jenkins-egress` NetworkPolicies |
| Pod security | Non-root, read-only root FS with `tmp` emptyDir, controller RBAC limited to a namespaced `jenkins-agent-manager` Role |
| URL | `https://jenkins.czarintel.io/` |

Deploy: `kubectl apply -f ci-cd/jenkins/jenkins-deployment.yml` (part of `scripts/bootstrap.sh cluster`). The VM alternative is `ansible-playbook configure-jenkins.yml`; see the [Ansible guide](ansible.md).

## Pipeline (`ci-cd/Jenkinsfile`)

Options: 45-minute timeout, keep 30 builds, `disableConcurrentBuilds(abortPrevious: true)`.

| # | Stage | Container | Gate |
|---|---|---|---|
| 1 | Checkout | jnlp | Computes `IMAGE_TAG = ${BUILD_NUMBER}-${sha12}` |
| 2 | Lint ∥ | python / hadolint `v2.12.0` | `flake8 --max-line-length=120 docker/api-service scripts/` · `hadolint --failure-threshold warning` on both Dockerfiles |
| 3 | Unit Tests | python `3.12-slim` | `pytest --cov-fail-under=80`, JUnit and Cobertura published |
| 4 | Docker Build | kaniko `v1.23.2-debug` | `--no-push --reproducible`, image saved as a tarball for scanning |
| 5 | SAST & CVE Scan ∥ | trivy `0.56.2` | **Image:** fails on CRITICAL (`--ignore-unfixed`), SARIF archived. **Filesystem:** `vuln,secret,misconfig` at HIGH/CRITICAL, exit 1 |
| 6 | Docker Push (ECR) | crane plus tools | `main` / `release/*` only. ECR login via **IRSA**; the resulting digest is recorded |
| 7 | Helm Lint | helm `3.16.2` | `helm lint --strict` against staging and production values |
| 8 | Update GitOps Repo | tools `alpine/k8s:1.30.5` | `main` / `release/*` only. `yq` sets `image.tag` (and repository) in `values-${TARGET_ENV}.yaml`, then commits and pushes over `sshagent(gitops-deploy-key)`. 3 push attempts with rebase, then fail |
| post | notifySlack | n/a | `success` / `unstable` / `failure` blocks, each with the build URL |

The pipeline only **builds** the image once. The same scanned tarball is pushed. ECR repositories are `IMMUTABLE`, so the `${BUILD_NUMBER}-${sha12}` tag can never be re-pointed, and the GitOps commit body records the pushed **digest** for audit. What was scanned is what runs.

### Parameters

| Parameter | Values | Use |
|---|---|---|
| `TARGET_ENV` | `staging` (default) / `production` | Which values file receives the new tag. Production promotion is therefore an explicit, attributable rebuild |
| `SKIP_SCAN_GATE` | `false` (default) | **Break-glass.** Trivy image scan reports but exits 0. Requires documented approval (PCI-DSS 6.3.3 exception record); every use is visible in the build parameters |

### Credentials: no static cloud keys

| Need | Mechanism |
|---|---|
| ECR push | Agent pods run as ServiceAccount `jenkins-agent`, annotated with the IRSA role `jenkins_agent_irsa_role_arn` (Terraform AWS module). STS issues 1-hour credentials: `ecr:GetAuthorizationToken` plus the layer-upload and `PutImage` actions on the `czarintel/api-service` repository only |
| GitOps commit | `gitops-deploy-key`: a repository-scoped SSH deploy key (write on this repo only). Replacing it with GitHub App installation tokens is roadmap week 11 |
| Slack | Webhook credential in the Jenkins credential store (roadmap week 11: Slack OAuth app) |

### Running a build

```bash
# Staging (automatic on merge to main)
# Production promotion: Jenkins UI → api-service → Build with Parameters → TARGET_ENV=production
```

Output: an image `123456789012.dkr.ecr.us-east-1.amazonaws.com/czarintel/api-service@sha256:…`, a commit `deploy(api-service): <env> -> <tag>` whose body carries the source SHA and author, the digest and the build URL, then an ArgoCD sync. See the [ArgoCD guide](argocd.md).

## GitHub Actions quality gates

### `terraform-validate.yml`

Runs on pull requests and pushes touching `terraform/**`. Permissions: `contents: read`, `security-events: write`.

| Job | What it does | Blocking |
|---|---|---|
| `fmt-validate` | `terraform fmt -check -recursive`, `init -backend=false`, `validate` | ✔ |
| `tflint` | tflint `v0.53.0`, `--recursive` over all modules (bundled Terraform ruleset) | ✔ |
| `security-scan` | **Trivy config** (HIGH/CRITICAL, exit 1) plus **Checkov** (SARIF to code scanning, advisory with `soft_fail`) | Trivy ✔, Checkov advisory |
| `kubernetes-lint` | `helm lint` (both charts and envs), `kubeconform -strict` on rendered charts and `kubernetes/` | ✔ |

### `platform-quality.yml`

Runs on pull requests touching platform code, pushes to `main`, nightly at 06:00 UTC, and manual dispatch.

| Job | What it does | Blocking |
|---|---|---|
| `ansible-lint` | `yamllint`, `ansible-lint`, `ansible-playbook --syntax-check site.yml` | ✔ |
| `scripts` | `pytest scripts/tests` (moto-mocked AWS) | ✔ |
| `compliance-repo` | `compliance_scan.py repo --fail-on medium`, JSON evidence uploaded as artefact | ✔ |
| `compliance-aws` | Nightly or dispatch only. **OIDC** (`id-token: write`, `aws-actions/configure-aws-credentials@v4`) into the role in repo variable `AWS_AUDIT_ROLE_ARN` (grant it read-only audit permissions); live-account scan evidence | Evidence |

Recommended branch protection on `main`: require all blocking jobs, one CODEOWNERS review, signed commits, and no force-push.

## Compliance mapping

| Control | Evidence |
|---|---|
| PCI-DSS 6.2.4 / 6.3.2, NIST SA-11, SI-2 | Trivy image and fs scans, SARIF retained per build |
| PCI-DSS 6.5.1-6.5.6, SOC2 CC8.1, NIST CM-3 | PR gates, GitOps commit per deploy, `TARGET_ENV` attribution |
| NIST IA-5, SOC2 CC6.1, HIPAA 164.312(d) | SSO-only Jenkins, IRSA and OIDC short-lived credentials |
| NIST SA-10, SR-4 | Digest pinning, reproducible Kaniko builds, OCI labels with commit and build date |
| NIST AU-6, CA-7 | Nightly compliance evidence job |

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `aws sts get-caller-identity` fails in the Push stage | ServiceAccount annotation missing or the IRSA trust `sub` does not equal `system:serviceaccount:jenkins:jenkins-agent` |
| Trivy image gate fails on a CVE with no fix | `--ignore-unfixed` already skips it; a failing CVE has a fix, so bump the base image |
| GitOps stage fails after 3 attempts | Concurrent promotions; re-run. The rebase loop resolves normal races |
| Coverage gate fails | New code without tests; threshold is 80% |
