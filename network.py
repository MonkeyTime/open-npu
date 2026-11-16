"""Contrat des mesures réseau partagé par l’agent et l’ordonnanceur."""

import math
import time

PROBE_BYTES = 64 * 1024
PROBE_INTERVAL = 30 * 60
SAMPLE_VALID_SECONDS = 2 * 60 * 60
MIN_UPLOAD_LIMIT = 0.05
MAX_RATE = 100_000
MARGIN_BYTES = 512
RESULT_BYTES_PER_VALUE = 28
COMPUTE_MARGIN_SECONDS = 15


def optional_number(value, minimum, maximum):
    if value is None:
        return None
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError("Valeur réseau hors limites.")
    return float(value)


def upload_seconds(byte_count, mbps):
    return byte_count * 8 / (mbps * 1_000_000) if mbps else 0


def paced_body(body, limit_mbps):
    """Cadence le corps envoyé. Les en-têtes HTTP restent à la charge du transport."""
    byte_rate = limit_mbps * 1_000_000 / 8
    chunk_size = max(1, min(8192, int(byte_rate / 10)))
    started = time.monotonic()
    for offset in range(0, len(body), chunk_size):
        end = min(offset + chunk_size, len(body))
        delay = started + end / byte_rate - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        yield body[offset:end]


def transfer_profile(row, now):
    if row is None:
        return None, None, None
    fresh = row["sampled_at"] is not None and now - row["sampled_at"] <= SAMPLE_VALID_SECONDS
    upload = row["upload_mbps"] if fresh else None
    cap = row["upload_limit_mbps"]
    if cap is not None:
        upload = min(cap, upload) if upload is not None else cap
    return upload, row["download_mbps"] if fresh else None, row["rtt_ms"] if fresh else None


def fits_lease(profile, input_bytes, value_count, lease_seconds):
    upload, download, rtt = profile
    estimated = (COMPUTE_MARGIN_SECONDS + (rtt or 0) / 500 +
                 upload_seconds(input_bytes + MARGIN_BYTES, download) +
                 upload_seconds(MARGIN_BYTES + value_count * RESULT_BYTES_PER_VALUE, upload))
    return estimated <= lease_seconds * 0.75


def estimate_mbps(byte_count, elapsed, rtt_seconds):
    # La résolution d'un petit échantillon devient insuffisante quand le réseau est très rapide.
    transfer = elapsed - rtt_seconds
    if transfer < 0.005:
        return None
    return min(MAX_RATE, round(byte_count * 8 / (transfer * 1_000_000), 3)) or None
