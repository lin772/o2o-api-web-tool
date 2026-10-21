# -*- coding: utf-8 -*-
"""
Windows 原生控件字体与配色。

字号（pt，Windows 逻辑单位）：
  10pt — 按钮 / 标签 / 输入框（≈13px）
  11pt — 分组框标题（加粗）
  10pt — Consolas 报文区
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

# ── 颜色（对齐 ECP 参考图）──────────────────────────────────
MAIN_BG = "#1E2430"
CARD = "#252B38"
CARD_BORDER = "#4A5568"
INPUT_BG = "#1E2430"
LOG_BG = "#1A1F28"

NAV_BG = "#2C5282"
NAV_BG_HOVER = "#3565A0"
NAV_BG_SELECTED = "#3B8ED0"
NAV_FG = "#FFFFFF"
NAV_FG_MUTED = "#CCCCCC"

TEXT_MAIN = "#FFFFFF"
TEXT_MUTED = "#CCCCCC"
TEXT_BRIGHT = TEXT_MAIN
LOG_FG = "#E5E7EB"

SUCCESS_GREEN = "#34B76E"
SUCCESS_GREEN_ACTIVE = "#2DA561"
BTN_GRAY = "#5A6478"
BTN_GRAY_ACTIVE = "#4A5568"
INFO_BLUE = "#3B8ED0"
INFO_BLUE_ACTIVE = "#2F7AB8"
CYAN = "#3AB7B0"
CYAN_ACTIVE = "#2F9E98"
WARNING_YELLOW = "#D69E2E"
ERROR_RED = "#E53E3E"

ACCENT = SUCCESS_GREEN
ACCENT_HOVER = SUCCESS_GREEN_ACTIVE
ACCENT_LIGHT = CARD_BORDER

# pt 字号（严格执行）
PT_UI = 10
PT_TITLE = 11
PT_MONO = 10
UI_FAMILY = "Microsoft YaHei"
MONO_FAMILY = "Consolas"

SECTION_PADX = 12
SECTION_GAP = 8
CARD_PADDING: Tuple[int, int] = (14, 10)
CONTENT_PADDING: Tuple[int, int] = (14, 10)

FontLike = Union[Tuple[str, int], Tuple[str, int, str], Any]

UI_FONT: FontLike = (UI_FAMILY, PT_UI)
UI_FONT_SM: FontLike = (UI_FAMILY, PT_UI)
UI_FONT_BTN: FontLike = (UI_FAMILY, PT_UI)
UI_FONT_MODULE: FontLike = (UI_FAMILY, PT_TITLE, "bold")
UI_FONT_NAV: FontLike = (UI_FAMILY, PT_TITLE, "bold")
UI_FONT_HINT: FontLike = (UI_FAMILY, PT_UI)
MONO_FONT: FontLike = (MONO_FAMILY, PT_MONO)


@dataclass(frozen=True)
class FontSet:
    """tkinter.font.Font 对象，供控件 font= 属性使用。"""

    default: Any
    title: Any
    mono_font: Any
    family: str
    mono_family: str

    @property
    def ui(self) -> Any:
        return self.default

    @property
    def btn(self) -> Any:
        return self.default

    @property
    def module(self) -> Any:
        return self.title

    @property
    def hint(self) -> Any:
        return self.default

    @property
    def nav(self) -> Any:
        return self.title

    @property
    def mono(self) -> Any:
        return self.mono_font


def _font(root: Any, family: str, size: int, *, bold: bool = False) -> Any:
    import tkinter.font as tkfont

    return tkfont.Font(root=root, family=family, size=size, weight="bold" if bold else "normal")


def create_app_fonts(root: Any) -> FontSet:
    return FontSet(
        default=_font(root, UI_FAMILY, PT_UI, bold=False),
        title=_font(root, UI_FAMILY, PT_TITLE, bold=True),
        mono_font=_font(root, MONO_FAMILY, PT_MONO, bold=False),
        family=UI_FAMILY,
        mono_family=MONO_FAMILY,
    )


def _publish_fonts(fonts: FontSet) -> None:
    global UI_FONT, UI_FONT_SM, UI_FONT_BTN, UI_FONT_MODULE, UI_FONT_NAV, UI_FONT_HINT, MONO_FONT
    UI_FONT = fonts.default
    UI_FONT_SM = fonts.default
    UI_FONT_BTN = fonts.default
    UI_FONT_MODULE = fonts.title
    UI_FONT_NAV = fonts.title
    UI_FONT_HINT = fonts.default
    MONO_FONT = fonts.mono


def set_default_font(root: Any, fonts: FontSet) -> None:
    """全局默认字体 + 按系统 DPI 设置 Tk scaling（矢量渲染，不拉伸位图）。"""
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi and dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    root.option_add("*Font", fonts.default)
    root.option_add("*Toplevel*Font", fonts.default)
    # Windows ttk.Combobox 弹出列表默认白底白字，深色主题下需单独指定
    root.option_add("*TCombobox*Listbox*Background", CARD)
    root.option_add("*TCombobox*Listbox*Foreground", TEXT_MAIN)
    root.option_add("*TCombobox*Listbox*selectBackground", NAV_BG_SELECTED)
    root.option_add("*TCombobox*Listbox*selectForeground", NAV_FG)


def _json_word_char(ch: str) -> bool:
    """JSON 报文中的“词字符”：字母/数字/下划线（与 monaco 默认 wordPattern 对齐）。"""
    return ch.isalnum() or ch == "_"


def bind_json_word_select(widget) -> None:
    """双击 JSON 里的数字/单词时，仅选中连续词字符（字母数字下划线），
    不带双引号、冒号、逗号等分隔符。幂等，可安全重复调用。"""
    if getattr(widget, "_json_word_select_bound", False):
        return
    widget._json_word_select_bound = True

    def _on_double(event):
        w = event.widget
        try:
            idx = w.index(f"@{event.x},{event.y}")
        except Exception:
            return None
        line, _, col = idx.partition(".")
        try:
            col = int(col)
        except (TypeError, ValueError):
            return None
        line_text = w.get(f"{line}.0", f"{line}.end")
        start = col
        while start > 0 and _json_word_char(line_text[start - 1]):
            start -= 1
        end = col
        n = len(line_text)
        while end < n and _json_word_char(line_text[end]):
            end += 1
        # 落在分隔符（引号/冒号/逗号等）上时走默认双击行为
        if start == end:
            return None
        w.tag_remove("sel", "1.0", "end")
        w.tag_add("sel", f"{line}.{start}", f"{line}.{end}")
        w.mark_set("anchor", f"{line}.{start}")
        w.mark_set("insert", f"{line}.{end}")
        return "break"

    widget.bind("<Double-Button-1>", _on_double, add="+")


def configure_log_text(widget, *, mono: Optional[Any] = None, muted: bool = False) -> None:
    """报文/JSON：Consolas 10pt，行高约 1.4。"""
    widget.configure(
        font=mono or MONO_FONT,
        bg=LOG_BG,
        fg=TEXT_MUTED if muted else LOG_FG,
        insertbackground=TEXT_MAIN,
        selectbackground=CARD_BORDER,
        selectforeground=LOG_FG,
        relief="flat",
        highlightthickness=0,
        borderwidth=0,
        spacing1=0,
        spacing2=3,
        spacing3=5,
    )
    bind_json_word_select(widget)


def apply_colors(style, fonts: FontSet) -> None:
    """ttk 控件配色；字体通过 style.font 或控件 font= 设置（Button 仅支持 style）。"""
    _publish_fonts(fonts)

    style.configure("Nav.TFrame", background=NAV_BG)
    style.configure("Main.TFrame", background=MAIN_BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TFrame", background=MAIN_BG)

    _lf = {
        "background": CARD,
        "relief": "solid",
        "borderwidth": 1,
        "bordercolor": CARD_BORDER,
    }
    style.configure("Card.TLabelframe", padding=CARD_PADDING, **_lf)
    style.configure(
        "Card.TLabelframe.Label",
        background=CARD,
        foreground=TEXT_MAIN,
        font=fonts.title,
    )
    style.configure("TLabelframe", padding=CARD_PADDING, **_lf)
    style.configure(
        "TLabelframe.Label",
        background=CARD,
        foreground=TEXT_MAIN,
        font=fonts.title,
    )

    _lbl = {"font": fonts.default}
    style.configure("TLabel", background=MAIN_BG, foreground=TEXT_MUTED, **_lbl)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT_MUTED, **_lbl)
    style.configure("CardMuted.TLabel", background=CARD, foreground=TEXT_MUTED, **_lbl)
    style.configure("Muted.TLabel", background=CARD, foreground=TEXT_MUTED, **_lbl)
    style.configure("Hint.TLabel", background=MAIN_BG, foreground=TEXT_MUTED, **_lbl)
    style.configure("Bright.TLabel", background=MAIN_BG, foreground=TEXT_MAIN, **_lbl)
    style.configure("Title.TLabel", background=MAIN_BG, foreground=TEXT_MAIN, font=fonts.title)
    style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT_MAIN, font=fonts.title)
    style.configure("Url.TLabel", background=CARD, foreground=LOG_FG, font=fonts.mono)

    _btn_flat = {"relief": "flat", "borderwidth": 0, "focuscolor": CARD_BORDER}
    _btn_font = {"font": fonts.default}

    style.configure(
        "TButton",
        padding=(14, 6),
        background=BTN_GRAY,
        foreground=TEXT_MAIN,
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "TButton",
        background=[("active", BTN_GRAY_ACTIVE), ("pressed", BTN_GRAY_ACTIVE), ("disabled", "#3D4654")],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Primary.TButton",
        background=SUCCESS_GREEN,
        foreground=TEXT_MAIN,
        padding=(16, 7),
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "Primary.TButton",
        background=[
            ("active", SUCCESS_GREEN_ACTIVE),
            ("pressed", SUCCESS_GREEN_ACTIVE),
            ("disabled", "#287A4E"),
        ],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Secondary.TButton",
        background=BTN_GRAY,
        foreground=TEXT_MAIN,
        padding=(14, 6),
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "Secondary.TButton",
        background=[("active", BTN_GRAY_ACTIVE), ("pressed", BTN_GRAY_ACTIVE), ("disabled", "#3D4654")],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Info.TButton",
        background=INFO_BLUE,
        foreground=TEXT_MAIN,
        padding=(14, 6),
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "Info.TButton",
        background=[
            ("active", INFO_BLUE_ACTIVE),
            ("pressed", INFO_BLUE_ACTIVE),
            ("disabled", "#2C5282"),
        ],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Cyan.TButton",
        background=CYAN,
        foreground=TEXT_MAIN,
        padding=(14, 6),
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "Cyan.TButton",
        background=[("active", CYAN_ACTIVE), ("pressed", CYAN_ACTIVE), ("disabled", "#285E61")],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Warning.TButton",
        background=WARNING_YELLOW,
        foreground=TEXT_MAIN,
        padding=(14, 6),
        anchor="center",
        **_btn_flat,
        **_btn_font,
    )
    style.map(
        "Warning.TButton",
        background=[("active", "#B7791F"), ("pressed", "#B7791F"), ("disabled", "#744210")],
        foreground=[("disabled", TEXT_MUTED)],
    )

    style.configure(
        "Ghost.TButton",
        background=CARD,
        foreground=TEXT_MAIN,
        padding=(4, 2),
        borderwidth=1,
        relief="solid",
        anchor="center",
        **_btn_font,
    )
    style.map("Ghost.TButton", background=[("active", CARD_BORDER)], foreground=[("active", TEXT_MAIN)])

    style.configure(
        "Nav.TButton",
        background=NAV_BG,
        foreground=NAV_FG,
        padding=(12, 7),
        anchor="w",
        borderwidth=0,
        relief="flat",
        **_btn_font,
    )
    style.map("Nav.TButton", background=[("active", NAV_BG_HOVER)], foreground=[("active", NAV_FG)])
    style.configure(
        "NavSelected.TButton",
        background=NAV_BG_SELECTED,
        foreground=NAV_FG,
        padding=(12, 7),
        anchor="w",
        **_btn_font,
    )

    style.configure(
        "TEntry",
        font=fonts.default,
        fieldbackground=INPUT_BG,
        foreground=TEXT_MAIN,
        insertcolor=TEXT_MAIN,
        bordercolor=CARD_BORDER,
        padding=(6, 4),
    )
    style.configure(
        "TCombobox",
        font=fonts.default,
        fieldbackground=INPUT_BG,
        background=CARD,
        foreground=TEXT_MAIN,
        arrowcolor=TEXT_MUTED,
        bordercolor=CARD_BORDER,
        insertcolor=TEXT_MAIN,
        insertwidth=2,
        padding=(6, 4),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", INPUT_BG), ("disabled", CARD)],
        foreground=[("readonly", TEXT_MAIN), ("disabled", TEXT_MUTED)],
        selectbackground=[("readonly", CARD_BORDER)],
        selectforeground=[("readonly", TEXT_MAIN)],
    )


def apply_theme(style, root: Optional[Any] = None) -> FontSet:
    if root is None:
        fonts = FontSet(
            default=(UI_FAMILY, PT_UI),
            title=(UI_FAMILY, PT_TITLE, "bold"),
            mono_font=(MONO_FAMILY, PT_MONO),
            family=UI_FAMILY,
            mono_family=MONO_FAMILY,
        )
        _publish_fonts(fonts)
        return fonts
    fonts = create_app_fonts(root)
    set_default_font(root, fonts)
    apply_colors(style, fonts)
    return fonts


def resolve_mono(root: Optional[Any] = None) -> Tuple[str, int]:
    return (MONO_FAMILY, PT_MONO)
