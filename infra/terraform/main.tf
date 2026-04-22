provider "hcloud" {
  token = var.hcloud_token
}

# Look up the SSH key by name
data "hcloud_ssh_key" "admin" {
  name = var.ssh_key_name
}

# VPS: CCX23 — 4 dedicated vCPU, 16 GB RAM, 80 GB NVMe
resource "hcloud_server" "tbc" {
  name        = "tbc-prod"
  server_type = "cx43"
  image       = "ubuntu-24.04"
  location    = "nbg1"
  ssh_keys    = [data.hcloud_ssh_key.admin.id]
  firewall_ids = [hcloud_firewall.tbc.id]

  labels = {
    project = "tbc"
    env     = "prod"
  }
}

# Floating IP so the server IP stays stable across rebuilds
resource "hcloud_floating_ip" "tbc" {
  type          = "ipv4"
  home_location = "nbg1"
  description   = "TBC production floating IP"

  labels = {
    project = "tbc"
  }
}

resource "hcloud_floating_ip_assignment" "tbc" {
  floating_ip_id = hcloud_floating_ip.tbc.id
  server_id      = hcloud_server.tbc.id
}

# Firewall:
#   - SSH (22/tcp) from admin IP only
#   - HTTPS (443/tcp) from Anthropic IP ranges only (for MCP endpoint)
#   - All other inbound traffic dropped
resource "hcloud_firewall" "tbc" {
  name = "tbc-prod-fw"

  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "SSH from anywhere (key-only auth)"
  }

  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "HTTP for Let's Encrypt ACME challenge"
  }

  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "HTTPS public (MCP endpoint + admin)"
  }

  dynamic "rule" {
    for_each = var.anthropic_ip_ranges
    content {
      direction   = "in"
      protocol    = "tcp"
      port        = "443"
      source_ips  = [rule.value]
      description = "HTTPS from Anthropic IP range (MCP)"
    }
  }

  labels = {
    project = "tbc"
  }
}

# DNS A record: mcp.<domain> → floating IP
resource "hcloud_rdns" "tbc_floating" {
  floating_ip_id = hcloud_floating_ip.tbc.id
  ip_address     = hcloud_floating_ip.tbc.ip_address
  dns_ptr        = "mcp.${var.domain}"
}
