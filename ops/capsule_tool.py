"""Créer, signer et importer une capsule OpenVINO sans transmettre la clé privée au serveur."""

import argparse
from getpass import getpass
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capsule_manifest import MAX_ASSET, asset_digest, canonical, verify_manifest


def write_new(path, body):
    with path.open("xb") as output:
        output.write(body)


def keygen(private, public):
    password = getpass("Phrase secrète de la clé privée : ")
    if len(password) < 12 or getpass("Confirmer la phrase secrète : ") != password:
        raise ValueError("Phrase secrète trop courte ou différente.")
    key = Ed25519PrivateKey.generate()
    write_new(private, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.BestAvailableEncryption(password.encode())))
    write_new(public, (key.public_key().public_bytes_raw().hex() + "\n").encode())


def sign(source, key):
    manifest = json.loads((source / "definition.json").read_text(encoding="utf-8"))
    for index, stage in enumerate(manifest["stages"]):
        for extension in ("xml", "bin"):
            path = source / f"{index}.{extension}"
            size = path.stat().st_size
            if size > MAX_ASSET:
                raise ValueError("Artefact trop grand.")
            stage[extension] = {"size": size, "sha256": asset_digest(path)}
    document = {"manifest": manifest, "signature": key.sign(canonical(manifest)).hex()}
    _, digest = verify_manifest(document, key.public_key().public_bytes_raw().hex())
    write_new(source / "manifest.json", json.dumps(document, indent=2, ensure_ascii=False).encode())
    return digest


def publish(source, runtime):
    public = (runtime / "capsule-signing.pub").read_text(encoding="ascii").strip()
    document = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest, digest = verify_manifest(document, public)
    target = runtime / "capsules" / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Capsule déjà importée : {digest}")
    with TemporaryDirectory(prefix=".import-", dir=target.parent) as temporary:
        staged = Path(temporary) / digest
        staged.mkdir()
        for index, stage in enumerate(manifest["stages"]):
            for extension in ("xml", "bin"):
                filename = f"{index}.{extension}"
                candidate = source / filename
                expected = stage[extension]
                if candidate.stat().st_size != expected["size"]:
                    raise ValueError(f"Taille inattendue : {filename}")
                copied = staged / filename
                with candidate.open("rb") as input_file, copied.open("wb") as output:
                    remaining = expected["size"]
                    while remaining:
                        chunk = input_file.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError(f"Artefact tronqué : {filename}")
                        output.write(chunk)
                        remaining -= len(chunk)
                    if input_file.read(1):
                        raise ValueError(f"Artefact trop grand : {filename}")
                if asset_digest(copied) != expected["sha256"]:
                    raise ValueError(f"Artefact altéré : {filename}")
        write_new(staged / "manifest.json", json.dumps(document, ensure_ascii=False).encode())
        staged.rename(target)
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generator = commands.add_parser("keygen")
    generator.add_argument("private", type=Path)
    generator.add_argument("public", type=Path)
    signer = commands.add_parser("sign")
    signer.add_argument("source", type=Path)
    signer.add_argument("private", type=Path)
    publisher = commands.add_parser("import")
    publisher.add_argument("source", type=Path)
    publisher.add_argument("runtime", type=Path)
    args = parser.parse_args()
    if args.command == "keygen":
        keygen(args.private, args.public)
    elif args.command == "sign":
        password = getpass("Phrase secrète : ").encode()
        key = serialization.load_pem_private_key(args.private.read_bytes(), password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("La clé doit être Ed25519.")
        print(sign(args.source, key))
    else:
        print(publish(args.source, args.runtime))


if __name__ == "__main__":
    main()
