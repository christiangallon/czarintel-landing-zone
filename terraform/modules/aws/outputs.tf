output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "nat_gateway_public_ips" {
  description = "Egress IPs — allow-list these on third-party services."
  value       = aws_eip.nat[*].public_ip
}

output "eks_cluster_name" {
  value = aws_eks_cluster.main.name
}

output "eks_cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

output "eks_cluster_ca_data" {
  value     = aws_eks_cluster.main.certificate_authority[0].data
  sensitive = true
}

output "eks_oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.eks.arn
}

output "eks_node_security_group_id" {
  value = aws_security_group.eks_nodes.id
}

output "rds_endpoint" {
  value = aws_db_instance.mysql.address
}

output "rds_port" {
  value = aws_db_instance.mysql.port
}

output "rds_master_secret_arn" {
  value = aws_db_instance.mysql.master_user_secret[0].secret_arn
}

output "backup_bucket_name" {
  value = aws_s3_bucket.backups.bucket
}

output "backup_bucket_arn" {
  value = aws_s3_bucket.backups.arn
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "api_service_irsa_role_arn" {
  value = aws_iam_role.api_service.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api_service.repository_url
}

output "guardduty_detector_id" {
  value = aws_guardduty_detector.main.id
}

output "security_alerts_topic_arn" {
  value = aws_sns_topic.security_alerts.arn
}

output "jenkins_agent_irsa_role_arn" {
  value = aws_iam_role.jenkins_agent.arn
}
