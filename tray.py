"""Icône OPEN-NPU de la zone de notification Windows."""

import logging
import subprocess
from threading import Event, Thread
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from network import MAX_RATE, MIN_UPLOAD_LIMIT, optional_number


def icon_image(color):
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), radius=14, fill="#17372e")
    draw.line([(32, 13), (48, 29), (32, 45), (16, 29), (32, 13)], fill="#d3f8e4", width=6, joint="curve")
    draw.ellipse((41, 41, 62, 62), fill=color, outline="#17372e", width=3)
    return image


def show_settings(preferences, stop):
    root = tk.Tk()
    try:
        root.title("Paramètres OPEN-NPU")
        root.resizable(False, False)
        limit = preferences.snapshot()[1]
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

        def save():
            try:
                rate = None
                if enabled.get():
                    rate = optional_number(float(value.get().strip().replace(",", ".")), MIN_UPLOAD_LIMIT, MAX_RATE)
                preferences.update(upload_limit_mbps=rate)
            except ValueError:
                messagebox.showerror("Plafond invalide", "Saisissez une valeur entre 0,05 et 100 000 Mbit/s.", parent=root)
                amount.focus_set()
                return
            except OSError:
                logging.exception("Impossible d’enregistrer le plafond.")
                messagebox.showerror("Paramètres OPEN-NPU", "Impossible d’enregistrer le plafond.", parent=root)
                return
            root.quit()

        buttons = ttk.Frame(panel)
        buttons.grid(row=4, columnspan=2, sticky="e")
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


def run_tray(log_file, preferences, run_agent):
    stop = Event()
    status = "Démarrage"
    colors = {"ready": "#74e4a3", "working": "#8ccbff", "waiting": "#ffd182", "error": "#ff948a"}
    images = {key: icon_image(color) for key, color in colors.items()}
    settings_open = Event()
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
            show_settings(preferences, stop)
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

    tray = Icon("OPEN-NPU", images["waiting"], f"OPEN-NPU · {status}", Menu(
        MenuItem(lambda _: status, None, enabled=False),
        MenuItem(lambda _: "Activer le partage" if preferences.snapshot()[0] else "Désactiver le partage", toggle_share),
        MenuItem("Paramètres…", open_settings),
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
