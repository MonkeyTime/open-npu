"""Formats signés des capsules et validation des tenseurs v2."""

import hashlib
import json
import math
import re
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_TENSOR = 8 * 1024 * 1024
MAX_ASSET = 512 * 1024 * 1024
DTYPES = {"f16": (2, "e"), "f32": (4, "f"), "i32": (4, "i")}
CAPSULE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
HEX = re.compile(r"[0-9a-f]{64}")
CAPSULE_ASSETS = {1: ("xml", "bin"), 2: ("onnx",)}


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def tensor_size(spec):
    return math.prod(spec["shape"]) * DTYPES[spec["dtype"]][0]


def asset_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_manifest(document, public_key):
    try:
        manifest = document["manifest"]
        signed = canonical(manifest)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(bytes.fromhex(document["signature"]), signed)
        if (type(manifest["format"]) is not int or manifest["format"] not in CAPSULE_ASSETS
                or not CAPSULE_ID.fullmatch(manifest["id"])):
            raise ValueError("Identifiant ou format invalide.")
        if type(manifest["version"]) is not int or not 1 <= manifest["version"] <= 1_000_000:
            raise ValueError("Version invalide.")
        if manifest["kind"] not in ("tensor", "decoder"):
            raise ValueError("Type de capsule inconnu.")
        stages = manifest["stages"]
        if not isinstance(stages, list) or not 1 <= len(stages) <= 8:
            raise ValueError("Une capsule contient entre une et huit étapes.")
        for index, stage in enumerate(stages):
            for name in ("input", "output"):
                spec = stage[name]
                shape = spec["shape"]
                if (spec["dtype"] not in DTYPES or not isinstance(shape, list) or not 1 <= len(shape) <= 6
                        or any(type(d) is not int or not 1 <= d <= MAX_TENSOR for d in shape)
                        or tensor_size(spec) > MAX_TENSOR):
                    raise ValueError("Contrat du tenseur invalide.")
            if index and stages[index - 1]["output"] != stage["input"]:
                raise ValueError("Les contrats de deux étapes successives doivent coïncider.")
            for asset in CAPSULE_ASSETS[manifest["format"]]:
                info = stage[asset]
                if (not HEX.fullmatch(info["sha256"]) or type(info["size"]) is not int
                        or not 0 <= info["size"] <= MAX_ASSET):
                    raise ValueError("Empreinte ou taille de l’artefact invalide.")
        if manifest["kind"] == "decoder":
            head, tail = stages[0]["input"], stages[-1]["output"]
            if (head["dtype"] != "i32" or len(head["shape"]) != 2 or head["shape"][0] != 1 or
                    tail["dtype"] not in ("f16", "f32") or len(tail["shape"]) != 3 or
                    tail["shape"][:2] != head["shape"] or not 2 <= tail["shape"][2] <= 262144):
                raise ValueError("Le décodeur attend des tokens [1, contexte] et retourne [1, contexte, vocabulaire].")
            for name in ("pad", "eos"):
                if type(manifest[name]) is not int or not 0 <= manifest[name] < tail["shape"][2]:
                    raise ValueError("Token de contrôle invalide.")
    except (InvalidSignature, KeyError, TypeError, AttributeError, OverflowError, ValueError) as error:
        raise ValueError("Capsule non valide ou signature non reconnue.") from error
    return manifest, hashlib.sha256(signed).hexdigest()


def pack_values(spec, values):
    count = math.prod(spec["shape"])
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("Nombre de valeurs invalide.")
    if spec["dtype"] == "i32":
        if any(type(value) is not int or not -(2**31) <= value < 2**31 for value in values):
            raise ValueError("Le tenseur attend des entiers i32.")
    elif any(type(value) not in (int, float) or not -3.4028235e38 <= value <= 3.4028235e38
             or not math.isfinite(value) for value in values):
        raise ValueError("Nombre invalide.")
    try:
        return struct.pack(f"<{count}{DTYPES[spec['dtype']][1]}", *values)
    except (OverflowError, struct.error) as error:
        raise ValueError("Valeur hors limites pour le type demandé.") from error


def next_token(spec, binary, position):
    vocab = spec["shape"][-1]
    width, code = DTYPES[spec["dtype"]]
    scores = struct.unpack_from(f"<{vocab}{code}", binary, position * vocab * width)
    if any(not math.isfinite(score) for score in scores):
        raise ValueError("Logits non finis.")
    return max(range(vocab), key=scores.__getitem__)


def validate_output(spec, binary):
    if len(binary) != tensor_size(spec):
        raise ValueError("Taille du tenseur de sortie invalide.")
    if spec["dtype"] != "i32" and any(not math.isfinite(value) for (value,) in
                                    struct.iter_unpack("<" + DTYPES[spec["dtype"]][1], binary)):
        raise ValueError("Le tenseur contient des valeurs non finies.")


def outputs_agree(spec, first, second):
    if spec["dtype"] == "i32" or first == second:
        return first == second
    binary_format = "<" + DTYPES[spec["dtype"]][1]
    return all(math.isclose(x, y, rel_tol=1e-3, abs_tol=1e-3) for (x,), (y,) in
               zip(struct.iter_unpack(binary_format, first), struct.iter_unpack(binary_format, second), strict=True))
