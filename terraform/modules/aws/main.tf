###############################################################################
# CzarIntel AWS Landing Zone module
#
#   Network : VPC, 2x public + 2x private subnets, IGW, NAT, route tables,
#             VPC flow logs
#   Compute : EKS (private + restricted public endpoint), managed node group,
#             OIDC provider for IRSA, KMS envelope encryption of secrets
#   Data    : RDS MySQL 8 Multi-AZ, S3 backup bucket (versioned, KMS, TLS-only)
#   Security: GuardDuty (EKS + S3 + malware protection), CloudWatch logs,
#             least-privilege IAM roles, ECR with scan-on-push
###############################################################################

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  azs          = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  cluster_name = "${var.name_prefix}-eks"
  account_id   = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition

  # /16 → /20 subnets: public 0..n, private 8..8+n (leaves room for growth)
  public_subnet_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  nat_gateway_count = var.single_nat_gateway ? 1 : var.az_count

  tags = merge(var.tags, {
    Environment = var.environment
    Project     = var.project
    ManagedBy   = "Terraform"
  })
}

###############################################################################
# KMS — customer-managed key for EKS secrets, RDS, S3, CloudWatch
###############################################################################
data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "EnableRootAccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid = "AllowCloudWatchLogs"
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.name}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${data.aws_region.current.name}:${local.account_id}:*"]
    }
  }
}

resource "aws_kms_key" "main" {
  description             = "${var.name_prefix} landing zone CMK"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json

  tags = merge(local.tags, { Name = "${var.name_prefix}-cmk" })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.name_prefix}-landing-zone"
  target_key_id = aws_kms_key.main.key_id
}

###############################################################################
# Networking
###############################################################################
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.tags, { Name = "${var.name_prefix}-vpc" })
}

# Lock down the default security group — CIS AWS 5.4
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.name_prefix}-default-sg-restricted" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.tags, {
    Name                                          = "${var.name_prefix}-public-${local.azs[count.index]}"
    Tier                                          = "public"
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  })
}

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(local.tags, {
    Name                                          = "${var.name_prefix}-private-${local.azs[count.index]}"
    Tier                                          = "private"
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
  })
}

resource "aws_eip" "nat" {
  count  = local.nat_gateway_count
  domain = "vpc"

  tags       = merge(local.tags, { Name = "${var.name_prefix}-nat-eip-${count.index}" })
  depends_on = [aws_internet_gateway.main]
}

resource "aws_nat_gateway" "main" {
  count         = local.nat_gateway_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags       = merge(local.tags, { Name = "${var.name_prefix}-nat-${local.azs[count.index]}" })
  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-public-rt" })
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = var.az_count
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[var.single_nat_gateway ? 0 : count.index].id
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-private-rt-${local.azs[count.index]}" })
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# VPC Flow Logs → CloudWatch (CIS AWS 3.9)
resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/${var.name_prefix}/flow-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
  tags              = merge(local.tags, { Name = "${var.name_prefix}-vpc-flow-logs" })
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${var.name_prefix}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-vpc-flow-logs" })
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "flow-logs-write"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn
  tags            = merge(local.tags, { Name = "${var.name_prefix}-vpc-flow-log" })
}

###############################################################################
# IAM — EKS control plane role
###############################################################################
data "aws_iam_policy_document" "eks_cluster_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name               = "${local.cluster_name}-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_assume.json
  tags               = merge(local.tags, { Name = "${local.cluster_name}-cluster-role" })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "eks_vpc_resource_controller" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSVPCResourceController"
}

###############################################################################
# IAM — EKS managed node group role
###############################################################################
data "aws_iam_policy_document" "eks_node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
  name               = "${local.cluster_name}-node-role"
  assume_role_policy = data.aws_iam_policy_document.eks_node_assume.json
  tags               = merge(local.tags, { Name = "${local.cluster_name}-node-role" })
}

resource "aws_iam_role_policy_attachment" "eks_node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryReadOnly",
    "AmazonSSMManagedInstanceCore", # Session Manager instead of SSH keys
  ])

  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

###############################################################################
# Security groups
###############################################################################
resource "aws_security_group" "eks_cluster" {
  name        = "${local.cluster_name}-control-plane"
  description = "EKS control plane additional security group"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.tags, { Name = "${local.cluster_name}-control-plane-sg" })
}

resource "aws_security_group" "eks_nodes" {
  name        = "${local.cluster_name}-nodes"
  description = "EKS worker nodes"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.tags, {
    Name                                          = "${local.cluster_name}-nodes-sg"
    "kubernetes.io/cluster/${local.cluster_name}" = "owned"
  })
}

resource "aws_vpc_security_group_ingress_rule" "cluster_from_nodes" {
  security_group_id            = aws_security_group.eks_cluster.id
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  description                  = "Nodes to Kubernetes API"
}

resource "aws_vpc_security_group_egress_rule" "cluster_to_nodes" {
  security_group_id            = aws_security_group.eks_cluster.id
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 1025
  to_port                      = 65535
  ip_protocol                  = "tcp"
  description                  = "Control plane to kubelets / webhooks"
}

resource "aws_vpc_security_group_ingress_rule" "nodes_from_cluster" {
  security_group_id            = aws_security_group.eks_nodes.id
  referenced_security_group_id = aws_security_group.eks_cluster.id
  from_port                    = 1025
  to_port                      = 65535
  ip_protocol                  = "tcp"
  description                  = "Control plane to kubelets / admission webhooks"
}

resource "aws_vpc_security_group_ingress_rule" "nodes_from_cluster_https" {
  security_group_id            = aws_security_group.eks_nodes.id
  referenced_security_group_id = aws_security_group.eks_cluster.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  description                  = "Control plane to extension API servers"
}

resource "aws_vpc_security_group_ingress_rule" "nodes_self" {
  security_group_id            = aws_security_group.eks_nodes.id
  referenced_security_group_id = aws_security_group.eks_nodes.id
  ip_protocol                  = "-1"
  description                  = "Node-to-node (pod networking, CoreDNS)"
}

resource "aws_vpc_security_group_egress_rule" "nodes_all" {
  security_group_id = aws_security_group.eks_nodes.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Egress via NAT (image pulls, AWS APIs)"
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds-mysql"
  description = "RDS MySQL - only reachable from EKS worker nodes"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.tags, { Name = "${var.name_prefix}-rds-mysql-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_nodes" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 3306
  to_port                      = 3306
  ip_protocol                  = "tcp"
  description                  = "MySQL from EKS nodes"
}

###############################################################################
# EKS cluster
###############################################################################
resource "aws_cloudwatch_log_group" "eks" {
  # Name must match what EKS writes to, so the group exists before the cluster
  name              = "/aws/eks/${local.cluster_name}/cluster"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  tags = merge(local.tags, { Name = "${local.cluster_name}-logs" })
}

resource "aws_eks_cluster" "main" {
  name     = local.cluster_name
  version  = var.eks_version
  role_arn = aws_iam_role.eks_cluster.arn

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  vpc_config {
    subnet_ids              = concat(aws_subnet.private[*].id, aws_subnet.public[*].id)
    security_group_ids      = [aws_security_group.eks_cluster.id]
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_access_cidrs) > 0
    public_access_cidrs     = length(var.public_access_cidrs) > 0 ? var.public_access_cidrs : null
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.main.arn
    }
  }

  kubernetes_network_config {
    service_ipv4_cidr = "172.20.0.0/16"
  }

  tags = merge(local.tags, { Name = local.cluster_name })

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_iam_role_policy_attachment.eks_vpc_resource_controller,
    aws_cloudwatch_log_group.eks,
  ]
}

# OIDC provider enables IAM Roles for Service Accounts (IRSA)
data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]

  tags = merge(local.tags, { Name = "${local.cluster_name}-oidc" })
}

# Enforce IMDSv2 + encrypted root volumes on worker nodes
resource "aws_launch_template" "eks_nodes" {
  name_prefix            = "${local.cluster_name}-node-"
  update_default_version = true
  vpc_security_group_ids = [aws_security_group.eks_nodes.id, aws_eks_cluster.main.vpc_config[0].cluster_security_group_id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.node_disk_size
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.main.arn
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.tags, { Name = "${local.cluster_name}-node" })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.tags, { Name = "${local.cluster_name}-node-volume" })
  }

  tags = merge(local.tags, { Name = "${local.cluster_name}-node-lt" })
}

resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.cluster_name}-general"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.private[*].id
  instance_types  = var.node_instance_types
  capacity_type   = "ON_DEMAND"
  ami_type        = "AL2023_x86_64_STANDARD"

  launch_template {
    id      = aws_launch_template.eks_nodes.id
    version = aws_launch_template.eks_nodes.latest_version
  }

  scaling_config {
    desired_size = var.node_desired_size
    min_size     = var.node_min_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable_percentage = 33
  }

  labels = {
    "czarintel.io/node-pool" = "general"
    "czarintel.io/env"       = var.environment
  }

  tags = merge(local.tags, { Name = "${local.cluster_name}-general" })

  lifecycle {
    # Cluster Autoscaler / Karpenter owns desired_size after creation
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.eks_node]
}

resource "aws_eks_addon" "this" {
  for_each = toset(["vpc-cni", "coredns", "kube-proxy", "aws-ebs-csi-driver"])

  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  service_account_role_arn    = each.value == "aws-ebs-csi-driver" ? aws_iam_role.ebs_csi.arn : null

  tags       = merge(local.tags, { Name = "${local.cluster_name}-${each.value}" })
  depends_on = [aws_eks_node_group.general]
}

###############################################################################
# IRSA roles
###############################################################################
locals {
  oidc_issuer_host = replace(aws_iam_openid_connect_provider.eks.url, "https://", "")
}

data "aws_iam_policy_document" "ebs_csi_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${local.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume.json
  tags               = merge(local.tags, { Name = "${local.cluster_name}-ebs-csi" })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# Application pod role — api-service reads its DB secret and writes backups
data "aws_iam_policy_document" "api_service_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${var.api_service_namespace}:${var.api_service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api_service" {
  name               = "${var.name_prefix}-api-service-irsa"
  assume_role_policy = data.aws_iam_policy_document.api_service_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-api-service-irsa" })
}

data "aws_iam_policy_document" "api_service" {
  statement {
    sid       = "ReadDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_db_instance.mysql.master_user_secret[0].secret_arn]
  }

  statement {
    sid       = "DecryptWithLandingZoneKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
  }

  statement {
    sid       = "WriteApplicationBackups"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.backups.arn}/api-service/*"]
  }

  statement {
    sid       = "ListApplicationPrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backups.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["api-service/*"]
    }
  }
}

resource "aws_iam_role_policy" "api_service" {
  name   = "api-service-least-privilege"
  role   = aws_iam_role.api_service.id
  policy = data.aws_iam_policy_document.api_service.json
}

###############################################################################
# ECR
###############################################################################
resource "aws_ecr_repository" "api_service" {
  name                 = "${var.project}/api-service"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-api-service" })
}

resource "aws_ecr_lifecycle_policy" "api_service" {
  repository = aws_ecr_repository.api_service.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 50 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 50
      }
      action = { type = "expire" }
    }]
  })
}

###############################################################################
# IRSA — Jenkins CI agents (ECR push only, no static AWS keys in Jenkins)
###############################################################################
data "aws_iam_policy_document" "jenkins_agent_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${var.jenkins_namespace}:${var.jenkins_agent_service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "jenkins_agent" {
  name                 = "${var.name_prefix}-jenkins-agent"
  assume_role_policy   = data.aws_iam_policy_document.jenkins_agent_assume.json
  max_session_duration = 3600
  tags                 = merge(local.tags, { Name = "${var.name_prefix}-jenkins-agent" })
}

data "aws_iam_policy_document" "jenkins_agent" {
  statement {
    sid       = "EcrAuthToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushApiServiceImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.api_service.arn]
  }
}

resource "aws_iam_role_policy" "jenkins_agent" {
  name   = "ecr-push-api-service"
  role   = aws_iam_role.jenkins_agent.id
  policy = data.aws_iam_policy_document.jenkins_agent.json
}

###############################################################################
# RDS MySQL (Multi-AZ)
###############################################################################
resource "aws_db_subnet_group" "mysql" {
  name       = "${var.name_prefix}-mysql"
  subnet_ids = aws_subnet.private[*].id
  tags       = merge(local.tags, { Name = "${var.name_prefix}-mysql-subnets" })
}

resource "aws_db_parameter_group" "mysql" {
  name   = "${var.name_prefix}-mysql80"
  family = "mysql8.0"

  parameter {
    name  = "require_secure_transport"
    value = "1"
  }

  parameter {
    name  = "slow_query_log"
    value = "1"
  }

  parameter {
    name  = "long_query_time"
    value = "1"
  }

  parameter {
    name  = "log_output"
    value = "FILE"
  }

  tags = merge(local.tags, { Name = "${var.name_prefix}-mysql80-params" })
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${var.name_prefix}-rds-enhanced-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
  tags               = merge(local.tags, { Name = "${var.name_prefix}-rds-enhanced-monitoring" })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "mysql" {
  identifier     = "${var.name_prefix}-mysql"
  engine         = "mysql"
  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  allocated_storage     = var.rds_storage_gb
  max_allocated_storage = var.rds_storage_gb * 4
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  db_name  = var.rds_database_name
  username = var.rds_master_username

  # Password is generated & rotated by RDS in Secrets Manager — never in state
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.key_id

  multi_az               = true
  db_subnet_group_name   = aws_db_subnet_group.mysql.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.mysql.name
  publicly_accessible    = false
  port                   = 3306

  backup_retention_period  = var.rds_backup_retention_days
  backup_window            = "03:00-04:00"
  maintenance_window       = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  deletion_protection                 = var.environment == "prod"
  skip_final_snapshot                 = var.environment != "prod"
  final_snapshot_identifier           = "${var.name_prefix}-mysql-final"

  enabled_cloudwatch_logs_exports       = ["error", "slowquery", "audit"]
  monitoring_interval                   = 30
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.main.arn
  performance_insights_retention_period = 7

  tags = merge(local.tags, { Name = "${var.name_prefix}-mysql" })

  lifecycle {
    ignore_changes = [final_snapshot_identifier]
  }
}

###############################################################################
# S3 — backups & artifacts
###############################################################################
resource "aws_s3_bucket" "backups" {
  bucket        = "${var.name_prefix}-backups-${local.account_id}"
  force_destroy = var.environment != "prod"
  tags          = merge(local.tags, { Name = "${var.name_prefix}-backups" })
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "tiering-and-expiry"
    status = "Enabled"
    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = 180
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "backups_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.backups.arn,
      "${aws_s3_bucket.backups.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket     = aws_s3_bucket.backups.id
  policy     = data.aws_iam_policy_document.backups_tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.backups]
}

###############################################################################
# GuardDuty — threat detection incl. EKS audit logs & runtime
###############################################################################
resource "aws_guardduty_detector" "main" {
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"
  tags                         = merge(local.tags, { Name = "${var.name_prefix}-guardduty" })
}

resource "aws_guardduty_detector_feature" "features" {
  for_each = toset([
    "S3_DATA_EVENTS",
    "EKS_AUDIT_LOGS",
    "EBS_MALWARE_PROTECTION",
    "RDS_LOGIN_EVENTS",
  ])

  detector_id = aws_guardduty_detector.main.id
  name        = each.value
  status      = "ENABLED"
}

resource "aws_guardduty_detector_feature" "runtime_monitoring" {
  detector_id = aws_guardduty_detector.main.id
  name        = "RUNTIME_MONITORING"
  status      = "ENABLED"

  additional_configuration {
    name   = "EKS_ADDON_MANAGEMENT"
    status = "ENABLED"
  }
}

# Route HIGH severity findings to EventBridge for Slack / PagerDuty fan-out
resource "aws_cloudwatch_event_rule" "guardduty_high" {
  name        = "${var.name_prefix}-guardduty-high-severity"
  description = "GuardDuty findings with severity >= 7"
  event_pattern = jsonencode({
    source      = ["aws.guardduty"]
    detail-type = ["GuardDuty Finding"]
    detail = {
      severity = [{ numeric = [">=", 7] }]
    }
  })
  tags = merge(local.tags, { Name = "${var.name_prefix}-guardduty-high" })
}

resource "aws_sns_topic" "security_alerts" {
  name              = "${var.name_prefix}-security-alerts"
  kms_master_key_id = aws_kms_key.main.id
  tags              = merge(local.tags, { Name = "${var.name_prefix}-security-alerts" })
}

resource "aws_cloudwatch_event_target" "guardduty_to_sns" {
  rule      = aws_cloudwatch_event_rule.guardduty_high.name
  target_id = "security-alerts-sns"
  arn       = aws_sns_topic.security_alerts.arn
}

data "aws_iam_policy_document" "security_alerts" {
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.security_alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "security_alerts" {
  arn    = aws_sns_topic.security_alerts.arn
  policy = data.aws_iam_policy_document.security_alerts.json
}
