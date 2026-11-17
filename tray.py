"""Icône OPEN-NPU de la zone de notification Windows."""

import ctypes
import logging
import subprocess
from threading import Event, Thread
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from network import MAX_RATE, MIN_UPLOAD_LIMIT, optional_number
from preferences import clock_minutes


def icon_image(color):
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), radius=14, fill="#17372e")
    draw.line([(32, 13), (48, 29), (32, 45), (16, 29), (32, 13)], fill="#d3f8e4", width=6, joint="curve")
    draw.ellipse((41, 41, 62, 62), fill=color, outline="#17372e", width=3)
    return image


def show_settings(preferences, stop, version=None):
    root = tk.Tk()
    try:
        root.title("Paramètres OPEN-NPU")
        root.resizable(False, False)
        settings = preferences.settings()
        limit = settings["upload_limit_mbps"]
        enabled = tk.BooleanVar(root, value=limit is not None)
        value = tk.StringVar(root, value=format(limit, "g").replace(".", ",") if limit is not None else "")
        panel = ttk.Frame(root, padding=20)
        panel.grid(sticky="nsew")
        ttk.Label(panel, text="Limite d’envoi").grid(row=0, columnspan=2, sticky="w")
        ttk.Label(panel, text="Le plafond s’applique aux résultats et aux sondes d’envoi.",
                  wraplength=340).grid(row=1, columnspan=2, sticky="w", pady=(5, 13))
        rate_field = ttk.Frame(panel)
        rate_field.grid(row=3, columnspan=2, sticky="w", pady=(6, 12))
        amount = ttk.Entry(rate_field, textvariable=value, width=12)

        def update_field():
            amount.state(("!disabled",) if enabled.get() else ("disabled",))
            if enabled.get():
                amount.focus_set()

        ttk.Checkbutton(panel, text="Limiter les envois", variable=enabled, command=update_field).grid(
            row=2, columnspan=2, sticky="w")
        amount.pack(side="left")
        ttk.Label(rate_field, text="Mbit/s").pack(side="left", padx=(8, 0))
        contribution = ttk.LabelFrame(panel, text="Contribution", padding=10)
        contribution.grid(row=4, columnspan=2, sticky="ew", pady=(0, 12))
        ac_only = tk.BooleanVar(root, value=settings["ac_only"])
        scheduled = tk.BooleanVar(root, value=settings["schedule_start"] is not None)
        start = tk.StringVar(root, value=settings["schedule_start"] or "22:00")
        end = tk.StringVar(root, value=settings["schedule_end"] or "07:00")
        ttk.Checkbutton(contribution, text="Uniquement sur secteur", variable=ac_only).grid(row=0, sticky="w")
        times = ttk.Frame(contribution)
        times.grid(row=2, sticky="w", pady=(4, 0))
        start_entry = ttk.Entry(times, textvariable=start, width=7)
        end_entry = ttk.Entry(times, textvariable=end, width=7)

        def update_schedule():
            state = ("!disabled",) if scheduled.get() else ("disabled",)
            start_entry.state(state)
            end_entry.state(state)

        ttk.Checkbutton(contribution, text="Limiter à une plage horaire quotidienne", variable=scheduled,
                        command=update_schedule).grid(row=1, sticky="w")
        start_entry.pack(side="left")
        ttk.Label(times, text="à").pack(side="left", padx=5)
        end_entry.pack(side="left")
        ttk.Label(times, text="(heure Windows)").pack(side="left", padx=5)
        update_schedule()

        def save():
            try:
                rate = None
                if enabled.get():
                    rate = optional_number(float(value.get().strip().replace(",", ".")), MIN_UPLOAD_LIMIT, MAX_RATE)
            except ValueError:
                messagebox.showerror("Plafond invalide", "Saisissez une valeur entre 0,05 et 100 000 Mbit/s.", parent=root)
                amount.focus_set()
                return
            try:
                first = start.get().strip() if scheduled.get() else None
                last = end.get().strip() if scheduled.get() else None
                if scheduled.get() and clock_minutes(first) == clock_minutes(last):
                    raise ValueError("Choisissez des heures de début et de fin différentes.")
                preferences.update(upload_limit_mbps=rate, ac_only=ac_only.get(),
                                   schedule_start=first, schedule_end=last)
            except ValueError as error:
                messagebox.showerror("Plage horaire invalide", str(error), parent=root)
                return
            except OSError:
                logging.exception("Impossible d’enregistrer le plafond.")
                messagebox.showerror("Paramètres OPEN-NPU", "Impossible d’enregistrer le plafond.", parent=root)
                return
            root.quit()

        buttons = ttk.Frame(panel)
        buttons.grid(row=5, columnspan=2, sticky="e")
        if version:
            ttk.Label(panel, text=f"Agent {version}").grid(row=6, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Annuler", command=root.quit).pack(side="left")
        ttk.Button(buttons, text="Enregistrer", command=save).pack(side="left", padx=(8, 0))
        root.bind("<Escape>", lambda _: root.quit())
        root.bind("<Return>", lambda _: save())
        root.protocol("WM_DELETE_WINDOW", root.quit)

        def wait_for_stop():
            if stop.is_set():
                root.quit()
            else:
                root.after(250, wait_for_stop)

        update_field()
        root.after(250, wait_for_stop)
        root.mainloop()
    finally:
        root.destroy()


def run_tray(log_file, preferences, run_agent, *, version=None, updates=None):
    stop = Event()
    status = "Démarrage"
    colors = {"ready": "#74e4a3", "working": "#8ccbff", "waiting": "#ffd182", "error": "#ff948a"}
    images = {key: icon_image(color) for key, color in colors.items()}
    settings_open = Event()
    update_open = Event()
    settings_thread = None

    def toggle_share(icon, item):
        try:
            preferences.update(paused=not preferences.snapshot()[0])
            icon.update_menu()
        except OSError:
            logging.exception("Impossible de changer l’état du partage.")
            icon.notify("Impossible d’enregistrer l’état du partage.", "OPEN-NPU")

    def settings_worker():
        try:
            show_settings(preferences, stop, version)
        except Exception:
            logging.exception("Impossible d’ouvrir les paramètres.")
        finally:
            settings_open.clear()

    def open_settings(icon, item):
        nonlocal settings_thread
        if not settings_open.is_set():
            settings_open.set()
            settings_thread = Thread(target=settings_worker, name="open-npu-settings", daemon=True)
            settings_thread.start()

    def open_log(icon, item):
        try:
            subprocess.Popen(["notepad.exe", str(log_file.resolve())])
        except OSError:
            logging.exception("Impossible d’ouvrir le journal.")

    def quit_agent(icon, item):
        stop.set()
        preferences.changed.set()
        icon.stop()

    def open_updates(icon, item):
        import webbrowser

        try:
            if not webbrowser.open(updates.connection() + "/#agent"):
                raise OSError("Navigateur indisponible.")
        except (OSError, ValueError, KeyError):
            logging.exception("Impossible d’ouvrir la page de mise à jour.")
            icon.notify("Impossible d’ouvrir la page de mise à jour.", "OPEN-NPU")

    def notify_update(release):
        try:
            tray.update_menu()
            tray.notify(f"Version {release['version']} téléchargée. Choisissez « Installer la mise à jour » dans le menu OPEN-NPU.",
                        "OPEN-NPU")
        except OSError:
            logging.exception("Impossible d’afficher la notification de mise à jour.")

    def install_update(icon, item):
        release = updates.ready()
        if release is None or update_open.is_set():
            return
        update_open.set()

        def confirm():
            try:
                message = f"Installer OPEN-NPU {release['version']} ? L’agent va redémarrer."
                if not release["signed"]:
                    message += "\n\nCette version d’essai locale est non signée."
                # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND
                if ctypes.windll.user32.MessageBoxW(None, message, "OPEN-NPU", 0x10124) == 6:
                    updates.install_ready(release)
            except Exception:
                logging.exception("Impossible d’installer la mise à jour.")
                icon.notify("Échec de la mise à jour. Consultez updates\\install.log.", "OPEN-NPU")
            finally:
                update_open.clear()

        Thread(target=confirm, name="open-npu-update-confirmation", daemon=True).start()

    tray = Icon("OPEN-NPU", images["waiting"], f"OPEN-NPU · {status}", Menu(
        MenuItem(lambda _: status, None, enabled=False),
        MenuItem(lambda _: "Activer le partage" if preferences.snapshot()[0] else "Désactiver le partage", toggle_share),
        MenuItem("Paramètres…", open_settings),
        MenuItem("Téléchargements et mises à jour…", open_updates, visible=updates is not None),
        MenuItem("Installer la mise à jour…", install_update,
                 visible=lambda _: updates is not None and updates.installed and updates.ready() is not None),
        MenuItem("Ouvrir le journal", open_log),
        Menu.SEPARATOR,
        MenuItem("Quitter", quit_agent),
    ))

    def set_status(text, category):
        nonlocal status
        if stop.is_set() or text == status:
            return
        status = text
        tray.icon = images[category]
        tray.title = f"OPEN-NPU · {text}"
        tray.update_menu()

    def work():
        run_agent(stop, set_status)

    worker = Thread(target=work, name="open-npu-worker", daemon=True)

    def setup(icon):
        icon.visible = True
        if updates is not None and updates.installed:
            Thread(target=updates.watch, args=(stop, notify_update), name="open-npu-updates", daemon=True).start()
        worker.start()

    try:
        tray.run(setup)
    finally:
        stop.set()
        preferences.changed.set()
        if settings_thread is not None:
            settings_thread.join(timeout=1)
        if worker.ident is not None:
            worker.join(timeout=20)
