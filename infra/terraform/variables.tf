variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "admin_ip" {
  description = "CIDR of admin IP allowed to SSH into the server (e.g. 1.2.3.4/32)"
  type        = string
}

variable "domain" {
  description = "Base domain for the project (e.g. example.com). DNS record mcp.<domain> will be created."
  type        = string
}

variable "anthropic_ip_ranges" {
  description = "Anthropic published IP ranges allowed to reach the MCP endpoint on port 443"
  type        = list(string)
  # Source: https://docs.anthropic.com/en/api/ip-ranges (updated April 2025)
  default = [
    "160.79.104.0/23",
    "2607:6bc0::/48",
  ]
}

variable "ssh_key_name" {
  description = "Name of the SSH key already uploaded to Hetzner Cloud"
  type        = string
}
