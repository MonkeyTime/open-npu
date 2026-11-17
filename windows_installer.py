"""Self-contained graphical installer for the local Windows edition."""

import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import shutil
import subprocess
import tempfile
from threading import Thread
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit
from zipfile import ZipFile

from agent_updates import AgentUpdates, read_object, unpack_release, version_tuple

DEFAULT_SERVER = "http://127.0.0.1:8787"
INVITATION = re.compile(r"[a-zA-Z0-9_-]{43}")


def local_server(text):
    text = text.strip().rstrip("/")
    try:
        address = urlsplit(text)
        port = address.port
    except ValueError as error:
        raise ValueError("L’adresse du serveur est invalide.") from error
    if (address.scheme not in {"http", "https"} or address.hostname not in {"127.0.0.1", "localhost", "::1"}
            or address.username is not None or address.password is not None or address.path
            or address.query or address.fragment or port == 0 or any(char.isspace() for char in text)):
        raise ValueError("Cet installateur d’essai fonctionne avec un serveur local. Demandez un paquet signé pour un serveur distant.")
    return text


def package_version(archive):
    with ZipFile(archive) as bundle:
        version = json.loads(bundle.read("release.json").decode("utf-8-sig"))["version"]
    version_tuple(version)
    return version


def installer_command(script, *args):
    result = subprocess.run(AgentUpdates.powershell(script, *args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stdout.decode("oem", errors="replace").strip() or "L’installateur n’a pas abouti.")


def install_local(archive, destination, server, invitation, startup):
    version = package_version(archive)
    config_path = destination / "node.json"
    existing = read_object(config_path)
    existing_version = read_object(destination / "agent-release.json").get("version")
    pending = "enrollment_key" in existing or existing.get("public_enrollment") is True
    update = bool(existing) and not pending
    server = local_server(existing.get("server", server))
    if update and existing_version and version_tuple(existing_version) >= version_tuple(version):
        return "current"
    if invitation and not INVITATION.fullmatch(invitation):
        raise ValueError("La clé d’invitation est invalide.")

    with tempfile.TemporaryDirectory(prefix="open-npu-setup-") as temporary:
        package = Path(temporary)
        unpack_release(archive, package)
        # The executable ships its own installer script for this unsigned local package.
        shutil.copyfile(Path(__file__).with_name("install-agent.ps1"), package / "install-agent.ps1")
        args = ["-InstallDirectory", destination]
        if update:
            args.append("-Update")
        else:
            bootstrap = {"enrollment_key": invitation} if invitation else {"public_enrollment": True}
            (package / "enrollment.json").write_text(json.dumps({"server": server, **bootstrap}), encoding="utf-8")
            if not startup:
                args.append("-NoStartup")
        installer_command(package / "install-agent.ps1", *args)
    if update:
        return "updated"
    return enrollment_result(config_path)


def install_signed(archive, destination, setup, startup):
    config = destination / "node.json"
    existing = read_object(config)
    if existing and not (existing.get("public_enrollment") or existing.get("enrollment_key")):
        raise ValueError("Utilisez la mise à jour de l’agent déjà installé pour conserver son inscription.")
    with tempfile.TemporaryDirectory(prefix="open-npu-signed-setup-") as temporary:
        package = Path(temporary)
        unpack_release(archive, package, enrollment=True)
        bootstrap = read_object(package / "enrollment.json")
        if bootstrap.get("server") != setup["server"] or bootstrap.get("public_enrollment") is not True:
            raise ValueError("L’inscription publique du paquet signé est invalide.")
        fingerprint = setup["publisher_sha256"]
        verifier = Path(__file__).with_name("verify-agent-package.ps1")
        installer_command(verifier, "-Package", package, "-PublisherSha256", fingerprint)
        args = ["-InstallDirectory", destination, "-PublisherSha256", fingerprint]
        if not startup:
            args.append("-NoStartup")
        installer_command(package / "install-agent.ps1", *args)
    return enrollment_result(config)


def enrollment_result(config_path):
    for _ in range(24):
        settings = read_object(config_path)
        if settings.get("token") and not settings.get("enrollment_key") and not settings.get("public_enrollment"):
            return "review" if settings.get("awaiting_approval") else "enrolled"
        time.sleep(0.5)
    return "pending"


def main():
    archive = Path(__file__).with_name("release.zip")
    setup = read_object(Path(__file__).with_name("setup.json"))
    destination = Path(os.environ["LOCALAPPDATA"]) / "OPEN-NPU"
    existing = read_object(destination / "node.json")
    needs_enrollment = not existing or "enrollment_key" in existing or existing.get("public_enrollment") is True

    root = tk.Tk()
    root.title(f"Installation d’OPEN-NPU {package_version(archive)}")
    root.resizable(True, False)
    root.minsize(510, 0)
    panel = ttk.Frame(root, padding=24)
    panel.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    panel.columnconfigure(0, weight=1)
    ttk.Label(panel, text="Installer l’agent OPEN-NPU", font=("Segoe UI", 16, "bold")).grid(sticky="w")
    ttk.Label(panel, text="Windows x64 · Installation dans votre compte", padding=(0, 6, 0, 16)).grid(sticky="w")

    server = tk.StringVar(value=setup.get("server", existing.get("server", DEFAULT_SERVER)))
    startup = tk.BooleanVar(value=True)
    status = tk.StringVar()
    messages = SimpleQueue()

    if needs_enrollment:
        ttk.Label(panel, text="Votre poste sera inscrit sans invitation. Le partage de sa NPU commencera après validation.",
                  wraplength=465).grid(sticky="w", pady=(0, 12))
        ttk.Label(panel, text="Adresse du serveur").grid(sticky="w")
        origin = ttk.Entry(panel, textvariable=server)
        origin.grid(sticky="ew", pady=(4, 12))
        if existing or setup:
            origin.state(["readonly"])
        ttk.Checkbutton(panel, text="Lancer l’agent à l’ouverture de session", variable=startup).grid(sticky="w", pady=(12, 0))
    else:
        ttk.Label(panel, text="Votre inscription et vos préférences seront conservées.", wraplength=465).grid(sticky="w")

    ttk.Label(panel, textvariable=status, wraplength=465).grid(sticky="w", pady=(14, 6))
    progress = ttk.Progressbar(panel, mode="indeterminate")
    progress.grid(sticky="ew", pady=(0, 12))
    buttons = ttk.Frame(panel)
    buttons.grid(sticky="e")
    close = ttk.Button(buttons, text="Fermer", command=root.destroy)
    close.pack(side="left", padx=(0, 8))

    def read_result():
        try:
            outcome, detail = messages.get_nowait()
        except Empty:
            root.after(100, read_result)
            return
        progress.stop()
        close.state(["!disabled"])
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        if outcome == "error":
            status.set("Installation interrompue.")
            action.state(["!disabled"])
            messagebox.showerror("Installation interrompue", detail, parent=root)
            return
        status.set({
            "current": "La version de l’agent est déjà à jour.",
            "updated": "Mise à jour terminée. L’agent est dans la zone de notification.",
            "enrolled": "Agent installé et inscrit. Retrouvez-le dans la zone de notification.",
            "review": "Poste inscrit. Il attend la validation d’un administrateur.",
            "pending": "Agent lancé. L’inscription reste en attente. Vérifiez l’adresse et l’accessibilité du serveur.",
        }[outcome])
        close.focus_set()

    def begin():
        inputs = server.get(), "", startup.get()
        try:
            if not setup:
                local_server(inputs[0])
        except ValueError as error:
            messagebox.showerror("Paramètres d’installation", str(error), parent=root)
            return
        action.state(["disabled"])
        close.state(["disabled"])
        root.protocol("WM_DELETE_WINDOW", root.bell)
        status.set("Installation en cours…")
        progress.start(12)

        def work():
            try:
                outcome = install_signed(archive, destination, setup, inputs[2]) if setup else install_local(archive, destination, *inputs)
                messages.put((outcome, ""))
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                messages.put(("error", str(error)))

        Thread(target=work, daemon=True).start()
        root.after(100, read_result)

    action = ttk.Button(buttons, text="Installer et contribuer" if needs_enrollment else "Mettre à jour", command=begin)
    action.pack(side="left")
    root.mainloop()


if __name__ == "__main__":
    main()
