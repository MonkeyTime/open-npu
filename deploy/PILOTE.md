# OPEN-NPU interuniversity pilot

## Install on Ubuntu

Clone this repository on an Ubuntu server. Ubuntu 24.04 LTS is recommended. For a public deployment, point the domain's DNS records at the host and allow inbound TCP ports 80 and 443. Use the administration CIDR that Caddy sees for your trusted operator network.

```bash
./deploy/ubuntu.sh --domain npu.example.org --admin-cidr 10.20.0.0/24
```

The script installs missing Docker and Compose dependencies, starts Caddy and OPEN-NPU, and checks the HTTPS readiness endpoint. It generates the administrator password in `deploy/secrets/admin.txt` and prints the command to read it. Sign in as `superadmin` at `https://npu.example.org/admin` from the trusted network. Add `--public-enrollment` on the first run to accept volunteer registrations without invitations. Remote Windows downloads require a signed release.

For a host-only trial, run `./deploy/ubuntu.sh --domain localhost`. Both web ports bind to loopback. To use a browser on that host, export Caddy's local root certificate and trust it in the browser or system certificate store.

```bash
sudo docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T edge \
  cat /data/caddy/pki/authorities/local/root.crt > open-npu-local-ca.crt
```

To deploy a later checkout, pull the changes and run `./deploy/ubuntu.sh` again. The script reuses `deploy/.env`, the administrator password, and the Docker volumes. Edit `deploy/.env` to change the hostname, administration CIDR, or enrollment setting. To diagnose startup, run `sudo docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 80`.

## Prepare the staging environment

The `pilot` mode keeps the Intel AI Boost demo capsule. At startup, it requires an allowed hostname and an external administrator secret. It disables enrollment with the shared local key. Route public services through the HTTPS reverse proxy and limit administration to the administrative network. Use `superadmin` to create named accounts in **Access**, then have operators sign in at `/login`. Enable TOTP when creating the accounts and send the password and TOTP secret over secure channels. Database backups contain TOTP secrets and must remain encrypted and private.

1. Choose the domain, the administrative VPN network, and a dedicated Linux host. Configure DNS, inbound HTTPS access, and persistent storage.
2. Copy `deploy/pilot.env.example` to `deploy/.env`. Set the domain, the CIDR range Caddy actually sees, and the path to the administrator secret file. Put at least 32 random characters in the secret file. On Linux, make the file readable by the group of service account 10001. Check that the installed Compose version mounts it with the expected permissions.
3. From `deploy`, run `docker compose config` and then `docker compose up --build -d` in staging. Verify the certificate and both health endpoints. Confirm that administration is accessible over the VPN and rejected from an external network.
4. Create an invitation through `POST /admin/enrollment-invites`. For example, send `{"label":"Campus A","hours":24,"max_uses":5}`. Save the response to a private file and prepare the Windows package using the [signing procedure](../docs/CAPSULES.md#distribute-the-signed-windows-package). Distribute it to the selected computers. You can list and revoke invitations through the API. Labels help identify invitations in the console. Isolation by institution requires a separate control.
5. Approve a test project, submit a job with a unique `Idempotency-Key`, and verify the result on several computers. After a connection interruption, retry with the same key and the same data. Cancel an active job with `POST /v1/jobs/{id}/cancel`. If you exceed the quota, the API returns HTTP 429 and `Retry-After`.
6. In a test environment, revoke an invitation, pause a computer, disconnect a node from the network, and verify that the batch resumes.

## Backup and recovery

Run `python ops/backup.py <source-database> <destination-copy>` from an environment that can access the SQLite database file and its WAL. The command uses the SQLite backup API and checks the copy's integrity and foreign keys. Store the encrypted copy with restricted permissions. Keep the required secrets and settings separately.

Restore to a **new location** first using the same command. Start an isolated instance with the restored file and check a project, its batches, and its history. Record the recovery time and the age of the backup. For a live recovery, stop the affected instance before switching the volume. Keep the active database intact during restore.

## Requirements for an open pilot

| Area | Required validation |
|---|---|
| Scientific workload | An approved production capsule, reference data, defined tolerances, and reproducible results |
| Identities | Institutional SSO integration and separation between institutions. Named accounts, TOTP, roles, and node revocation are already implemented. |
| Windows agent | A signed and timestamped installer, tested upgrades and removal, and a driver compatibility matrix |
| Operations | Alerting, on-call coverage, retention and backup policies, and a timed restore test |
| Network and capacity | Cross-campus tests, simulated outages, and a workload-based decision between SQLite and PostgreSQL |
| Data | A policy signed by participating institutions, with public or synthetic data for initial tests |
| Security | An access and isolation review, a component inventory, and documented fixes |
| Launch | Measurable acceptance criteria, a designated decision maker, and a rollback procedure |

The server independently recomputes FP16 squares for the demo capsule. Scientific capsules require their own validation methods. The multi-node LLM pipeline requires a separate validation campaign.

### HTTP service limits

The service applies a budget per client address and request type. Once the budget is exhausted, it returns HTTP `429` with a `Retry-After` header. Computers behind the same public address share the budget. In Compose, Caddy alone forwards the client address to the server through the private `backend` network. Keep port 8787 private and set `OPEN_NPU_PRIVATE_PROXY=1` only on a protected link where the proxy overwrites the header.

Caddy limits request bodies to 96 KiB, with a limit of 8 MiB for the binary output of a v2 stage. It allows 10 seconds to read request headers and five minutes to read the body.
