# vault/agent.hcl
# Vault Agent configuration — runs as sidecar in Docker/K8s
# Authenticates via AppRole, writes secrets to tmpfs, auto-renews token
#
# Docker Compose: mount this file into vault-agent container
# Kubernetes:     use vault.hashicorp.com/agent-inject annotations instead
#
# Usage: vault agent -config=/vault/config/agent.hcl

# ── Auto-auth: AppRole ────────────────────────────────────────
# role_id is baked into the image at build time (not secret)
# secret_id is injected by CI/CD pipeline (one-time, 10 min TTL)
auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path   = "/vault/config/role_id"      # baked into image
      secret_id_file_path = "/vault/config/secret_id"    # injected by CI/CD
      remove_secret_id_file_after_reading = true          # destroy after use
    }
  }

  # Write token to tmpfs — never to persistent disk
  sink "file" {
    config = {
      path = "/vault/secrets/.token"
      mode = 0400
    }
  }
}

# ── Cache: reuse token across restarts without re-auth ────────
cache {
  use_auto_auth_token = true
}

# ── Vault connection ──────────────────────────────────────────
vault {
  address = "http://vault:8200"   # internal Docker network
  retry {
    num_retries = 5
  }
}

# ── Template: render secrets as env file ─────────────────────
# Written to tmpfs — disappears on container stop
template {
  source      = "/vault/templates/app-secrets.ctmpl"
  destination = "/vault/secrets/app.env"
  perms       = "0400"
  # Restart app process when secrets change (rotation)
  exec {
    command = ["/bin/sh", "-c", "kill -HUP $(cat /tmp/app.pid) 2>/dev/null || true"]
    timeout = "5s"
  }
}

# ── Template: database credentials (dynamic, 1h TTL) ─────────
template {
  source      = "/vault/templates/db-creds.ctmpl"
  destination = "/vault/secrets/db.env"
  perms       = "0400"
}

# ── Logging ───────────────────────────────────────────────────
log_level  = "warn"
log_format = "json"
