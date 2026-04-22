output "server_ip" {
  description = "Primary IPv4 address of the VPS"
  value       = hcloud_server.tbc.ipv4_address
}

output "floating_ip" {
  description = "Floating IPv4 address assigned to the VPS"
  value       = hcloud_floating_ip.tbc.ip_address
}

output "mcp_fqdn" {
  description = "Fully-qualified domain name for the MCP endpoint"
  value       = "mcp.${var.domain}"
}
