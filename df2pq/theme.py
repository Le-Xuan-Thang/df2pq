"""Dark developer-tool theme for the Tk GUI (palette from ui-ux-pro-max: Dark Mode / OLED)."""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

C = {
    "bg": "#0B1120",
    "card": "#131C31",
    "card_alt": "#18233C",
    "field": "#0F1729",
    "border": "#27344F",
    "border_strong": "#3A4A6B",
    "ink": "#F1F5F9",
    "muted": "#94A3B8",
    "faint": "#64748B",
    "accent": "#22C55E",
    "accent_hover": "#4ADE80",
    "accent_ink": "#04120A",
    "blue": "#60A5FA",
    "blue_soft": "#1B2D52",
    "amber": "#FBBF24",
    "amber_soft": "#3A2E0E",
    "red": "#F87171",
    "red_soft": "#3B1717",
    "green_soft": "#10301F",
}


@dataclass(frozen=True)
class Fonts:
    ui: str
    mono: str
    scale: float = 1.0

    def px(self, value: int) -> int:
        """Scale a pixel measure for HiDPI displays (fonts already follow Tk scaling)."""
        return round(value * self.scale)

    def ui_font(self, size: int = 10, weight: str = "normal") -> tuple:
        return (self.ui, size, weight)

    def mono_font(self, size: int = 9) -> tuple:
        return (self.mono, size)


def _pick(available: set[str], candidates: tuple[str, ...], default: str) -> str:
    return next((name for name in candidates if name in available), default)


def apply_theme(root: tk.Tk) -> Fonts:
    families = set(tkfont.families(root))
    fonts = Fonts(
        ui=_pick(families, ("Inter", "Segoe UI", "SF Pro Text", "Noto Sans", "DejaVu Sans"),
                 "TkDefaultFont"),
        mono=_pick(families, ("JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono"),
                   "TkFixedFont"),
        scale=max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72)),
    )
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=fonts.ui, size=10)

    root.configure(background=C["bg"])
    # Dropdown lists and menus are plain Tk widgets; ttk styles do not reach them.
    root.option_add("*TCombobox*Listbox.background", C["card_alt"])
    root.option_add("*TCombobox*Listbox.foreground", C["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["blue_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["ink"])
    root.option_add("*TCombobox*Listbox.font", fonts.ui_font(10))
    root.option_add("*Menu.background", C["card_alt"])
    root.option_add("*Menu.foreground", C["ink"])
    root.option_add("*Menu.activeBackground", C["blue_soft"])
    root.option_add("*Menu.activeForeground", C["ink"])
    root.option_add("*Menu.relief", "flat")

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    f = fonts.ui_font

    style.configure(".", background=C["bg"], foreground=C["ink"], bordercolor=C["border"],
                    focuscolor=C["blue"], font=f(10))
    style.configure("App.TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["card"])
    style.configure("Inset.TFrame", background=C["field"])
    style.configure("TLabel", background=C["bg"], foreground=C["ink"])
    style.configure("Card.TLabel", background=C["card"], foreground=C["ink"])
    style.configure("Muted.TLabel", background=C["card"], foreground=C["muted"], font=f(9))
    style.configure("BgMuted.TLabel", background=C["bg"], foreground=C["muted"], font=f(9))
    style.configure("Title.TLabel", background=C["bg"], foreground=C["ink"], font=f(20, "bold"))
    style.configure("Subtitle.TLabel", background=C["bg"], foreground=C["muted"], font=f(10))
    style.configure("CardTitle.TLabel", background=C["card"], foreground=C["ink"],
                    font=f(11, "bold"))
    style.configure("Step.TLabel", background=C["blue_soft"], foreground=C["blue"],
                    font=f(9, "bold"), padding=(7, 2))
    style.configure("Field.TLabel", background=C["card"], foreground=C["muted"],
                    font=f(9, "bold"))
    style.configure("Empty.TLabel", background=C["field"], foreground=C["muted"], font=f(10))
    style.configure("EmptyTitle.TLabel", background=C["field"], foreground=C["ink"],
                    font=f(12, "bold"))
    style.configure("Summary.TLabel", background=C["card"], foreground=C["ink"], font=f(10, "bold"))
    for tone, (fg, bg) in {
        "Gpu": (C["accent"], C["green_soft"]),
        "Cpu": (C["muted"], C["card_alt"]),
        "Busy": (C["amber"], C["amber_soft"]),
        "Ok": (C["accent"], C["green_soft"]),
        "Warn": (C["amber"], C["amber_soft"]),
        "Err": (C["red"], C["red_soft"]),
        "Info": (C["blue"], C["blue_soft"]),
    }.items():
        style.configure(f"{tone}.Pill.TLabel", foreground=fg, background=bg,
                        padding=(10, 4), font=f(9, "bold"))
    style.configure("Status.TLabel", background=C["card"], foreground=C["ink"], font=f(10, "bold"))

    style.configure("TEntry", fieldbackground=C["field"], foreground=C["ink"],
                    insertcolor=C["ink"], bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], padding=(9, 7))
    style.map("TEntry", bordercolor=[("focus", C["blue"])], lightcolor=[("focus", C["blue"])],
              darkcolor=[("focus", C["blue"])], fieldbackground=[("disabled", C["card"])],
              foreground=[("disabled", C["faint"])])
    style.configure("TCombobox", fieldbackground=C["field"], background=C["card_alt"],
                    foreground=C["ink"], arrowcolor=C["muted"], bordercolor=C["border"],
                    lightcolor=C["border"], darkcolor=C["border"], padding=(9, 6))
    style.map("TCombobox", fieldbackground=[("readonly", C["field"]), ("disabled", C["card"])],
              foreground=[("disabled", C["faint"])], bordercolor=[("focus", C["blue"])],
              arrowcolor=[("disabled", C["faint"])],
              selectbackground=[("readonly", C["field"])],
              selectforeground=[("readonly", C["ink"])])
    style.configure("TCheckbutton", background=C["card"], foreground=C["ink"],
                    indicatorbackground=C["field"], indicatorforeground=C["accent"],
                    padding=(2, 4))
    style.map("TCheckbutton", foreground=[("disabled", C["faint"])],
              indicatorbackground=[("selected", C["accent"]), ("disabled", C["card"])],
              indicatorforeground=[("selected", C["accent_ink"])])

    style.configure("Bar.TCheckbutton", background=C["bg"], foreground=C["muted"], padding=(2, 4))
    style.map("Bar.TCheckbutton", indicatorbackground=[("selected", C["accent"])],
              indicatorforeground=[("selected", C["accent_ink"])],
              background=[("active", C["bg"])], foreground=[("active", C["ink"])])

    base_btn = dict(padding=(14, 8), borderwidth=1, relief="flat", font=f(10))
    style.configure("Primary.TButton", background=C["accent"], foreground=C["accent_ink"],
                    bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                    font=f(10, "bold"), padding=(20, 10))
    style.map("Primary.TButton",
              background=[("disabled", C["card_alt"]), ("pressed", C["accent"]),
                          ("active", C["accent_hover"])],
              foreground=[("disabled", C["faint"])],
              bordercolor=[("focus", C["ink"]), ("disabled", C["card_alt"])],
              lightcolor=[("disabled", C["card_alt"])], darkcolor=[("disabled", C["card_alt"])])
    style.configure("Secondary.TButton", background=C["card_alt"], foreground=C["ink"],
                    bordercolor=C["border_strong"], lightcolor=C["card_alt"],
                    darkcolor=C["card_alt"], **base_btn)
    style.map("Secondary.TButton",
              background=[("disabled", C["card"]), ("pressed", C["blue_soft"]),
                          ("active", C["blue_soft"])],
              foreground=[("disabled", C["faint"])],
              bordercolor=[("focus", C["blue"]), ("active", C["blue"]),
                           ("disabled", C["border"])])
    style.configure("Quiet.TButton", background=C["card"], foreground=C["muted"],
                    bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"], **base_btn)
    style.map("Quiet.TButton",
              background=[("disabled", C["card"]), ("active", C["card_alt"])],
              foreground=[("disabled", C["faint"]), ("active", C["ink"])],
              bordercolor=[("focus", C["blue"])])

    style.configure("Data.Treeview", background=C["field"], fieldbackground=C["field"],
                    foreground=C["ink"], rowheight=fonts.px(34), borderwidth=0, font=f(10))
    style.configure("Data.Treeview.Heading", background=C["card_alt"], foreground=C["muted"],
                    font=f(9, "bold"), relief="flat", padding=(8, 7), borderwidth=0)
    style.map("Data.Treeview", background=[("selected", C["blue_soft"])],
              foreground=[("selected", C["ink"]), ("disabled", C["faint"])])
    style.map("Data.Treeview.Heading", background=[("active", C["card_alt"])])
    style.layout("Data.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("Accent.Horizontal.TProgressbar", troughcolor=C["field"],
                    background=C["accent"], bordercolor=C["field"], lightcolor=C["accent"],
                    darkcolor=C["accent"], thickness=8)
    style.configure("Vertical.TScrollbar", background=C["card_alt"], troughcolor=C["field"],
                    bordercolor=C["field"], arrowcolor=C["muted"], lightcolor=C["card_alt"],
                    darkcolor=C["card_alt"], gripcount=0)
    style.configure("Horizontal.TScrollbar", background=C["card_alt"], troughcolor=C["field"],
                    bordercolor=C["field"], arrowcolor=C["muted"], lightcolor=C["card_alt"],
                    darkcolor=C["card_alt"], gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", C["border_strong"])])
    style.map("Horizontal.TScrollbar", background=[("active", C["border_strong"])])
    style.configure("TSeparator", background=C["border"])
    return fonts


class Card(ttk.Frame):
    """A bordered surface with a numbered title row."""

    def __init__(self, master: tk.Misc, title: str, step: str | None = None, **kw) -> None:
        outer_kw = dict(kw)
        super().__init__(master, style="App.TFrame", **outer_kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._border = tk.Frame(self, background=C["border"], padx=1, pady=1)
        self._border.grid(row=0, column=0, sticky="nsew")
        self._border.columnconfigure(0, weight=1)
        self._border.rowconfigure(0, weight=1)
        self.body = ttk.Frame(self._border, style="Card.TFrame", padding=(16, 14))
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        head = ttk.Frame(self.body, style="Card.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        head.columnconfigure(1, weight=1)
        if step:
            ttk.Label(head, text=step, style="Step.TLabel").grid(row=0, column=0, padx=(0, 9))
        ttk.Label(head, text=title, style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
        self.head = head
