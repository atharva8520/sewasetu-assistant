# Sewa Setu — Production Deployment Guide

This guide covers deploying and operating the Sewa Setu portal, reverse proxy, and AI assistant infrastructure on a single VM running Docker and Docker Compose.

---

## 1. Prerequisites & Host Security

### DNS Configuration
Configure an `A` record pointing your domain (or subdomain) to the public IPv4 address of the host VM:
```text
sewasetu.yourdomain.com.    IN A    <VM_PUBLIC_IP>
```
*Tip:* If using wildcard IP services during testing, `sewasetu.<ip>.sslip.io` works out of the box with Caddy automatic TLS.

### Host Firewall (UFW / iptables)
Only ports **80** (HTTP) and **443** (HTTPS) must be accessible from the public internet. SSH (port 22) should be restricted to trusted administrator IPs.
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```
**CRITICAL SECURITY RULE:** The database (PostgreSQL 5432), internal application (8000), SMS gateway (8025), and internal agent routes must **NEVER** be published or exposed to the public internet.

---

## 2. Environment Configuration

1. Clone the repository into `/opt/sewasetu-assistant`:
   ```bash
   git clone <REPO_URL> /opt/sewasetu-assistant
   cd /opt/sewasetu-assistant
   ```

2. Create `.env` from `.env.example`:
   ```bash
   cp .env.example .env
   ```

3. Edit `.env` with strong production values:
   ```env
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=<generate-strong-random-password>
   SECRET_KEY=<generate-64-character-hex-secret>
   AGENT_INTERNAL_KEY=<generate-64-character-hex-key>
   AGENT_ENABLED_BLOCKS=Sonari
   
   SITE_ADDRESS=sewasetu.yourdomain.com
   PUBLIC_BASE_URL=https://sewasetu.yourdomain.com
   ```

> [!CAUTION]
> **Exact URL Match Required:** `PUBLIC_BASE_URL` must equal **EXACTLY** what users and MCP clients type into their browser / client (`https://...`, no trailing slash). This URL is stamped into OAuth JWTs as `iss`, into discovery documents (`/.well-known/oauth-authorization-server`), and into canonical MCP audiences (`aud`). A mismatch will cause token validation failure across all clients.

---

## 3. Starting and Managing the Stack

### Production Start
Run Docker Compose with the production override file (`docker-compose.prod.yml`):
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```
This configuration:
* Publishes ports `80` and `443` on Caddy.
* Removes port publishing for the Flask app container (`ports: !reset []`).
* Sets `restart: unless-stopped` on all containers.
* Mounts persistent Docker volumes for PostgreSQL data, uploads, application logs, and Caddy TLS certificates.

### Restarting Services
To restart all services cleanly:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart
```

To rebuild a specific service after a code change (zero downtime for unrelated services):
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build app
```

---

## 4. Health Checks & Verification

### External Endpoint Checks
Verify from a remote machine (outside the VM):

1. **OAuth Discovery Document:**
   ```bash
   curl -fsSL https://<domain>/.well-known/oauth-authorization-server | jq .
   ```
   *Verify:* `"issuer"` equals `https://<domain>`.

2. **Public JWKS:**
   ```bash
   curl -fsSL https://<domain>/.well-known/jwks.json | jq .
   ```
   *Verify:* Contains an active RSA key (`"alg": "RS256"`).

3. **Internal Agent API Isolation (Must Return 404):**
   ```bash
   curl -i https://<domain>/internal/agent/scheme
   ```
   *Verify:* Returns `HTTP/1.1 404 Not Found`.

4. **Container Health:**
   ```bash
   docker compose ps
   ```

---

## 5. Monitoring & Viewing Logs

* **Caddy Reverse Proxy Logs:**
  ```bash
  docker compose logs -f proxy
  ```
* **Portal Application Logs:**
  ```bash
  docker compose logs -f app
  ```
* **Statutory SLA Deemed-Approval Scheduler Logs:**
  ```bash
  docker compose logs -f scheduler
  ```
* **Database Logs:**
  ```bash
  docker compose logs -f db
  ```

---

## 6. Database Backup & Disaster Recovery

### Creating a Snapshot Backup
Execute `pg_dump` inside the running `db` container to generate an atomic compressed SQL dump:
```bash
docker compose exec -T db pg_dump -U sewasetu -d sewasetu -Fc > "backup_$(date +%Y%m%d_%H%M%S).dump"
```

### Restoring from Backup
To restore a snapshot into a clean database:
```bash
# 1. Stop app and scheduler to prevent concurrent writes
docker compose stop app scheduler

# 2. Restore database
docker compose exec -T db pg_restore -U sewasetu -d sewasetu --clean --if-exists < backup_file.dump

# 3. Restart application
docker compose start app scheduler
```

### Backing up Uploaded Documents
All citizen-uploaded proof documents reside in the `uploads` Docker volume:
```bash
docker run --rm -v sewasetu-ai-handout_uploads:/data -v $(pwd):/backup alpine \
  tar -czf /backup/uploads_$(date +%Y%m%d).tar.gz -C /data .
```
