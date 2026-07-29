output "function_url" {
  description = "Public HTTPS endpoint for the matching engine. Set this as RESUME_MATCH_API_URL in the Ocean Blue app."
  value       = aws_lambda_function_url.api.function_url
}

output "dynamodb_table" {
  value = aws_dynamodb_table.vectors.name
}

output "vector_backend" {
  value = local.vector_backend
}

output "opensearch_endpoint" {
  description = "Collection endpoint when use_opensearch=true; empty otherwise."
  value       = var.use_opensearch ? aws_opensearchserverless_collection.vectors[0].collection_endpoint : ""
}
