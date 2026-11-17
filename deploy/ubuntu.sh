#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage:
  ./deploy/ubuntu.sh --domain npu.example.org --admin-cidr 10.20.0.0/24
  ./deploy/ubuntu.sh --domain localhost
  ./deploy/ubuntu.sh

Use --public-enrollment on the first run to accept volunteers without invitations.
Run without options to deploy again using the existing deploy/.env.
For a public hostname, point DNS to this server and allow TCP ports 80 and 443.
EOF
}

fail() { printf 'OPEN-NPU: %s\n' "$*" >&2; exit 1; }

args=("$@")
domain=
admin_cidr=
public=
while (( $# )); do
    case "$1" in
        --domain|--admin-cidr)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "Missing value for $1."
            if [[ "$1" == --domain ]]; then domain="${2,,}"; else admin_cidr="$2"; fi
            shift 2 ;;
        --public-enrollment) public=1; shift ;;
        -h|--help) usage; exit ;;
        *) fail "Unknown option $1. See --help." ;;
    esac
done

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ -f compose.yaml && -f Caddyfile ]] || fail 'Run this script from the OPEN-NPU checkout.'
if [[ ! -f .env ]]; then
    [[ -n "$domain" ]] || fail 'Choose a hostname with --domain for the first run.'
    if [[ "$domain" == localhost ]]; then
        # The published ports bind to loopback. Caddy may see the Docker bridge
        # address for traffic coming from the host.
        admin_cidr="${admin_cidr:-0.0.0.0/0}"
    else
        [[ -n "$admin_cidr" ]] || fail 'Set --admin-cidr to the trusted administration network.'
    fi
fi

. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail 'This bootstrap script uses Ubuntu packages.'
if (( EUID != 0 )); then
    exec sudo -- bash "$PWD/ubuntu.sh" "${args[@]}"
fi

packages=()
for tool in curl openssl python3; do
    command -v "$tool" >/dev/null 2>&1 || packages+=("$tool")
done
docker_local=(docker --host unix:///var/run/docker.sock)
if ! command -v docker >/dev/null 2>&1; then
    packages+=(docker.io docker-compose-v2)
elif ! "${docker_local[@]}" compose version >/dev/null 2>&1; then
    if dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q '^install ok installed$'; then
        packages+=(docker-compose-plugin)
    else
        packages+=(docker-compose-v2)
    fi
fi
if (( ${#packages[@]} )); then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
fi
if ! "${docker_local[@]}" info >/dev/null 2>&1; then
    systemctl enable --now docker
    "${docker_local[@]}" info >/dev/null
fi

install -d -m 0700 secrets
exec 9>secrets/.deploy.lock
flock -n 9 || fail 'Another deployment is running from this checkout.'

if [[ ! -f .env ]]; then
    python3 - "$domain" "$admin_cidr" <<'PY'
import ipaddress
import re
import sys

domain, cidr = sys.argv[1:]
label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
if domain != "localhost" and (
    len(domain) > 253 or "." not in domain
    or not all(label.fullmatch(part) for part in domain.split("."))
    or domain.split(".")[-1].isdigit()
):
    sys.exit("Use a DNS hostname without a scheme, port or path.")
try:
    network = ipaddress.ip_network(cidr, strict=False)
except ValueError:
    sys.exit("Use an IPv4 or IPv6 CIDR for --admin-cidr.")
if "/" not in cidr or (domain != "localhost" and network.prefixlen == 0):
    sys.exit("Use a CIDR limited to your administration network.")
PY
    bind=
    if [[ "$domain" == localhost ]]; then bind=127.0.0.1:; fi
    (
        set -o noclobber
        printf 'OPEN_NPU_DOMAIN=%s\nOPEN_NPU_ADMIN_CIDR=%s\nOPEN_NPU_ADMIN_SECRET_PATH=./secrets/admin.txt\nOPEN_NPU_PUBLIC_ENROLLMENT=%s\nOPEN_NPU_PORT_BIND=%s\n' \
            "$domain" "$admin_cidr" "${public:-0}" "$bind" > .env
    )
fi

# Use the checkout's Compose configuration even when the caller exported these variables.
unset OPEN_NPU_DOMAIN OPEN_NPU_ADMIN_CIDR OPEN_NPU_ADMIN_SECRET_PATH OPEN_NPU_PUBLIC_ENROLLMENT OPEN_NPU_PORT_BIND
compose=("${docker_local[@]}" compose --env-file .env -f compose.yaml)
config=$("${compose[@]}" config --format json)
mapfile -t settings < <(python3 -c '
import json, os, sys
c = json.load(sys.stdin)
edge = c["services"]["edge"]["environment"]
print(edge["OPEN_NPU_DOMAIN"])
print(edge["OPEN_NPU_ADMIN_CIDR"])
print(c["services"]["open-npu"]["environment"]["OPEN_NPU_PUBLIC_ENROLLMENT"])
print(os.path.abspath(c["secrets"]["admin"]["file"]))
' <<< "$config")
(( ${#settings[@]} == 4 )) || fail 'Could not read the deployment settings.'
[[ -z "$domain" || "$domain" == "${settings[0]}" ]] || fail 'Edit OPEN_NPU_DOMAIN in deploy/.env to change the existing hostname.'
[[ -z "$admin_cidr" || "$admin_cidr" == "${settings[1]}" ]] || fail 'Edit OPEN_NPU_ADMIN_CIDR in deploy/.env to change the administration network.'
[[ -z "$public" || "$public" == "${settings[2]}" ]] || fail 'Set OPEN_NPU_PUBLIC_ENROLLMENT=1 in deploy/.env to open enrollment.'
domain=${settings[0]}
secret=${settings[3]}
default_secret="$PWD/secrets/admin.txt"

if [[ "$secret" == "$default_secret" ]]; then
    [[ ! -L "$secret" ]] || fail 'The generated administrator secret may not be a symbolic link.'
    if [[ ! -e "$secret" ]]; then
        (set -o noclobber; openssl rand -hex 32 > "$secret")
    fi
    [[ -f "$secret" ]] || fail 'The administrator secret must be a regular file.'
    chown 0:10001 "$secret"
    chmod 0640 "$secret"
fi
python3 - "$secret" <<'PY'
from pathlib import Path
import sys

secret = Path(sys.argv[1])
if not secret.is_file() or len(secret.read_text(encoding="utf-8").strip()) < 32:
    sys.exit("Set the administrator secret to a file containing at least 32 characters.")
PY

if ! "${compose[@]}" up --build --wait --wait-timeout 150; then
    "${compose[@]}" ps
    "${compose[@]}" logs --tail 40 open-npu edge
    fail 'Docker Compose did not bring up the services.'
fi

ca=
curl_opts=(--noproxy '*' --fail --silent --connect-timeout 3 --max-time 5)
if [[ "$domain" == localhost ]]; then
    ca=$(mktemp)
    trap 'rm -f -- "$ca"' EXIT
    curl_opts+=(--cacert "$ca")
fi

for (( deadline=SECONDS+150; SECONDS<deadline; )); do
    if [[ -n "$ca" && ! -s "$ca" ]] &&
        ! "${compose[@]}" exec -T edge cat /data/caddy/pki/authorities/local/root.crt > "$ca" 2>/dev/null; then
        sleep 3
        continue
    fi
    if curl "${curl_opts[@]}" --resolve "$domain:443:127.0.0.1" "https://$domain/health/ready" >/dev/null; then
        printf '\nPortal: https://%s/\nAdministration: https://%s/admin\nUsername: superadmin\n' "$domain" "$domain"
        printf 'Read the administrator password on this server with: sudo cat -- %q\n' "$secret"
        exit
    fi
    sleep 3
done

"${compose[@]}" logs --tail 40 edge
fail 'Services started, but HTTPS readiness failed. Check DNS, inbound ports 80/443 and the Caddy logs.'
