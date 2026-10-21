# -*- coding: utf-8 -*-
"""
公域平台正向造单 - 可视化工具

独立启动: python platform_order_gui.py
集成启动: python api_tool.py（左侧导航「公域平台正向造单」）
配置: config/config.yaml（修改后点「刷新配置」重新加载）
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Tuple

from message_service import (
    SkuSlot,
    build_preview_message,
    detect_message_family,
    detect_sku_slots,
    extract_seller_nick,
    format_message_json,
    load_template,
    parse_message_text,
)
from order_service import (
    finalize_build_for_insert,
    format_db_error,
    insert_source,
    query_shop_by_seller_nick,
    query_shops,
    query_skus,
    run_transfer,
)
from settings import (
    env_label_map,
    get_env_by_label,
    get_platform_meta,
    list_platform_folders,
    list_scenarios,
    load_config,
    sku_query_page_size,
)

from ui_theme import (
    CARD,
    CARD_BORDER,
    CARD_PADDING,
    MAIN_BG,
    SECTION_GAP,
    SECTION_PADX,
    TEXT_MUTED,
    apply_theme,
    configure_log_text,
)

BG = MAIN_BG

SKU_QUERY_HINT = "查询后在下拉框中浏览 SKU，用右侧翻页切换页码（翻页时列表保持展开）"

SHOP_AUTO_LABEL = "（自动：从报文取卖家昵称，可不选）"


class HoverTooltip:
    """鼠标悬停时弹出说明，移开即隐藏。"""

    def __init__(self, widget: tk.Misc, text: str, *, font: Any = None) -> None:
        self.widget = widget
        self.text = text.strip()
        self.font = font
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event: Optional[tk.Event] = None) -> None:
        if self._tip or not self.text:
            return
        self.widget.update_idletasks()
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.attributes("-topmost", True)
        tk.Label(
            tip,
            text=self.text,
            justify=tk.LEFT,
            wraplength=380,
            bg=CARD,
            fg=TEXT_MUTED,
            font=self.font,
            padx=10,
            pady=8,
            relief=tk.SOLID,
            borderwidth=1,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        ).pack()
        self._tip = tip

    def _hide(self, _event: Optional[tk.Event] = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class PlatformOrderApp:
    def __init__(self, parent: tk.Misc, *, standalone: bool = True, auto_load: Optional[bool] = None):
        self.root = parent
        cfg = load_config()
        if standalone and isinstance(parent, tk.Tk):
            self.root.title((cfg.get("ui") or {}).get("window_title", "公域平台正向造单"))
            self.root.geometry("1100x820")
            self.root.minsize(960, 700)
            self.root.configure(bg=BG)
        elif standalone:
            try:
                self.root.configure(bg=BG)
            except tk.TclError:
                pass

        self.cfg = cfg
        self.env_labels = list(env_label_map(self.cfg).values())
        self.env_key_map = {v: k for k, v in env_label_map(self.cfg).items()}
        default_label = (list(env_label_map(self.cfg).values()) or ["TEST"])[0]

        self.env_var = tk.StringVar(value=default_label)
        self.platform_var = tk.StringVar()
        self.scenario_var = tk.StringVar()
        self.shop_var = tk.StringVar()
        self.shop_search_var = tk.StringVar()
        self.sku_search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")

        self.shops: List[Dict[str, Any]] = []
        self.shop_by_label: Dict[str, Dict[str, Any]] = {}
        self.skus: List[Dict[str, Any]] = []
        self.sku_by_label: Dict[str, str] = {}
        self.sku_slots: List[SkuSlot] = []
        self.sku_vars: List[tk.StringVar] = []
        self.sku_combos: List[ttk.Combobox] = []
        self.last_build = None
        self.selected_shop: Optional[Dict[str, Any]] = None
        self.sku_page = 1
        self.sku_has_next = False
        self._last_sku_combo_focused: Optional[ttk.Combobox] = None
        self._sku_page_reopen_index: Optional[int] = None
        self._master_data_loaded = False
        self._font_default = None
        self._font_title = None
        self.mono_font = ("Consolas", 10)

        self._init_theme()
        self._build_ui()
        self._reload_platforms()
        self._on_platform_change()
        if auto_load if auto_load is not None else standalone:
            self.root.after(200, lambda: self.ensure_master_data(silent=True))

    def ensure_master_data(self, *, silent: bool = False, force: bool = False) -> None:
        """首次进入公域造单页或独立启动时加载店铺/SKU。"""
        if self._master_data_loaded and not force:
            return
        self._master_data_loaded = True
        self._load_master_data(silent=silent)

    def _env_key(self) -> str:
        return get_env_by_label(self.env_var.get(), self.cfg)

    @staticmethod
    def _block_page_wheel(_event: tk.Event) -> str:
        """阻止滚轮冒泡到外层页面滚动容器。"""
        return "break"

    def _guard_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._block_page_wheel, add="+")

    @staticmethod
    def _fill_readonly_combo(
        combo: ttk.Combobox,
        var: tk.StringVar,
        values: List[str],
        *,
        prefer: str = "",
    ) -> None:
        """readonly 的 Combobox 须先切 normal 再写 values，否则 Windows 下拉列表为空。"""
        prefer = (prefer or var.get()).strip()
        combo.configure(state="normal")
        combo["values"] = values
        if prefer and prefer in values:
            idx = values.index(prefer)
            combo.current(idx)
            var.set(values[idx])
        elif values:
            combo.current(0)
            var.set(values[0])
        else:
            combo.set("")
            var.set("")
        combo.configure(state="readonly")

    def _attach_tooltip(self, widget: tk.Misc, text: str) -> None:
        HoverTooltip(widget, text, font=self._font_default)

    def _init_theme(self) -> None:
        root = self.root.winfo_toplevel()
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        fonts = apply_theme(style, root)
        self._font_default = fonts.default
        self._font_title = fonts.title
        self.mono_font = fonts.mono

    def _build_ui(self) -> None:
        fd = self._font_default
        top = ttk.Frame(self.root, style="Main.TFrame", padding=(SECTION_PADX, 8, SECTION_PADX, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="环境", style="Hint.TLabel", font=fd).grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        env_combo = ttk.Combobox(
            top, textvariable=self.env_var, values=self.env_labels, state="readonly", width=12, font=fd
        )
        env_combo.grid(row=0, column=1, sticky="w", pady=3)
        env_combo.bind("<<ComboboxSelected>>", lambda e: self._load_master_data(force=True))
        self._guard_wheel(env_combo)
        self._env_combo = env_combo

        ttk.Label(top, text="平台", style="Hint.TLabel", font=fd).grid(row=0, column=2, sticky="e", padx=(20, 6), pady=3)
        self.platform_combo = ttk.Combobox(
            top, textvariable=self.platform_var, state="readonly", width=14, font=fd
        )
        self.platform_combo.grid(row=0, column=3, sticky="w", pady=3)
        self.platform_combo.bind("<<ComboboxSelected>>", lambda e: self._on_platform_change())
        self._guard_wheel(self.platform_combo)

        ttk.Label(top, text="场景报文", style="Hint.TLabel", font=fd).grid(
            row=0, column=4, sticky="e", padx=(20, 6), pady=3
        )
        self.scenario_combo = ttk.Combobox(
            top, textvariable=self.scenario_var, state="readonly", width=28, font=fd
        )
        self.scenario_combo.grid(row=0, column=5, sticky="w", pady=3)
        self.scenario_combo.bind("<<ComboboxSelected>>", lambda e: self._on_scenario_change())
        self._guard_wheel(self.scenario_combo)

        refresh_btn = ttk.Button(
            top, text="刷新配置", command=self._reload_config, style="Info.TButton"
        )
        refresh_btn.grid(row=0, column=6, padx=(12, 0), pady=3)
        refresh_hint = (self.cfg.get("ui") or {}).get(
            "refresh_config_hint",
            "重新读取 config.yaml：环境地址、平台元数据、转单开关、SKU 分页等",
        )
        self._attach_tooltip(refresh_btn, refresh_hint)

        shop_frame = ttk.LabelFrame(self.root, text=" 店铺档案 ", style="Card.TLabelframe")
        shop_frame.pack(fill=tk.X, padx=SECTION_PADX, pady=(0, SECTION_GAP))

        row1 = ttk.Frame(shop_frame, style="Card.TFrame")
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="搜索", style="Card.TLabel", font=fd).pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.shop_search_var, width=24, font=fd).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(row1, text="查询店铺", command=lambda: self._load_shops(), style="Info.TButton").pack(
            side=tk.LEFT, padx=4
        )
        self.shop_combo = ttk.Combobox(row1, textvariable=self.shop_var, state="readonly", width=22, font=fd)
        self.shop_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.shop_combo.bind("<<ComboboxSelected>>", lambda e: self._on_shop_change())
        self._guard_wheel(self.shop_combo)

        self.sku_frame = ttk.LabelFrame(
            self.root,
            text=" 商品 SKU（根据报文自动识别槽位数量） ",
            style="Card.TLabelframe",
        )
        self.sku_frame.pack(fill=tk.X, padx=SECTION_PADX, pady=(0, SECTION_GAP))

        sku_tool = ttk.Frame(self.sku_frame, style="Card.TFrame")
        sku_tool.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(sku_tool, text="SKU搜索", style="Card.TLabel", font=fd).pack(side=tk.LEFT)
        ttk.Entry(sku_tool, textvariable=self.sku_search_var, width=24, font=fd).pack(side=tk.LEFT, padx=(6, 4))
        sku_query_btn = ttk.Button(
            sku_tool,
            text="查询SKU",
            command=lambda: self._load_skus(reset_page=True),
            style="Info.TButton",
        )
        sku_query_btn.pack(side=tk.LEFT, padx=4)
        sku_hint = (self.cfg.get("ui") or {}).get("sku_query_hint", SKU_QUERY_HINT)
        self._attach_tooltip(sku_query_btn, sku_hint)

        self.sku_page_label_var = tk.StringVar(value="第 1 页")
        self.sku_pager: Optional[ttk.Frame] = None
        self.sku_prev_btn: Optional[ttk.Button] = None
        self.sku_next_btn: Optional[ttk.Button] = None

        self.sku_slots_container = ttk.Frame(self.sku_frame, style="Card.TFrame")
        self.sku_slots_container.pack(fill=tk.X)

        btn_frame = ttk.Frame(self.root, style="Main.TFrame", padding=(SECTION_PADX, 4, SECTION_PADX, 4))
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="仅预览", command=self._preview, style="Cyan.TButton").pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_frame, text="生成订单", command=self._generate, style="Primary.TButton").pack(
            side=tk.LEFT
        )
        ttk.Label(
            btn_frame,
            text="流程：选择环境与报文 → 仅预览 → 可改报文 → 生成订单（店铺由报文卖家昵称自动匹配，店铺下拉仅用于替换昵称）",
            style="Hint.TLabel",
            font=fd,
        ).pack(side=tk.LEFT, padx=(12, 0))

        preview_frame = ttk.LabelFrame(
            self.root,
            text=" 报文预览（可手工修改后再点「生成订单」） ",
            style="Card.TLabelframe",
        )
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=SECTION_PADX, pady=(0, SECTION_GAP))
        lines = int((self.cfg.get("ui") or {}).get("message_preview_lines", 22))
        self.preview_text = scrolledtext.ScrolledText(preview_frame, height=lines, wrap=tk.NONE)
        configure_log_text(self.preview_text, mono=self.mono_font)
        self.preview_text.configure(insertbackground="#FFFFFF")
        self.preview_text.pack(fill=tk.BOTH, expand=True)
        self._guard_wheel(self.preview_text)

        status = ttk.Label(self.root, textvariable=self.status_var, style="Hint.TLabel", font=fd, anchor="w")
        status.pack(fill=tk.X, padx=SECTION_PADX, pady=(0, 8))

    def _reload_config(self) -> None:
        try:
            self.cfg = load_config()
            self.env_labels = list(env_label_map(self.cfg).values())
            env_combo_vals = getattr(self, "_env_combo", None)
            if env_combo_vals is not None:
                self._fill_readonly_combo(
                    env_combo_vals, self.env_var, self.env_labels, prefer=self.env_var.get()
                )
            current_platform = self.platform_var.get()
            self._reload_platforms()
            if current_platform in list(self.platform_combo["values"]):
                self.platform_var.set(current_platform)
                self._on_platform_change()
            self.sku_page = 1
            self.status_var.set(
                f"配置已刷新：默认环境={(self.env_labels or ['TEST'])[0]}，"
                f"SKU 每页 {sku_query_page_size(self.cfg)} 条，场景已过滤逆向报文"
            )
        except Exception as e:
            messagebox.showerror("配置错误", str(e))

    def _reload_platforms(self) -> None:
        platforms = list_platform_folders()
        prefer = self.platform_var.get() if self.platform_var.get() in platforms else ""
        self._fill_readonly_combo(self.platform_combo, self.platform_var, platforms, prefer=prefer)

    def _current_platform_id(self) -> Optional[int]:
        platform = self.platform_var.get()
        if not platform:
            return None
        pid = get_platform_meta(platform, self.cfg).get("platform_id")
        if pid is None:
            return None
        return int(pid)

    def _on_platform_change(self) -> None:
        platform = self.platform_var.get()
        scenarios = list_scenarios(platform, self.cfg) if platform else []
        prefer = self.scenario_var.get() if self.scenario_var.get() in scenarios else ""
        self._fill_readonly_combo(self.scenario_combo, self.scenario_var, scenarios, prefer=prefer)
        self._on_scenario_change()
        if self._master_data_loaded:
            self._load_shops(silent=True)

    def _on_scenario_change(self) -> None:
        platform = self.platform_var.get()
        scenario = self.scenario_var.get()
        if not platform or not scenario:
            return
        try:
            raw = load_template(platform, scenario)
            self.sku_slots = detect_sku_slots(raw)
            self._rebuild_sku_ui()
            if self.sku_by_label:
                self._refresh_sku_combo_values()
            self.status_var.set(f"已加载 {platform}/{scenario}，识别到 {len(self.sku_slots)} 个 SKU 槽位")
            self.preview_text.delete("1.0", tk.END)
            self.last_build = None
        except Exception as e:
            messagebox.showerror("加载报文失败", str(e))

    def _update_sku_pager_ui(self, page: int, count: int, has_next: bool) -> None:
        if not hasattr(self, "sku_page_label_var"):
            return
        self.sku_page_label_var.set(f"第 {page} 页 · {count} 条")
        if not self.sku_prev_btn or not self.sku_next_btn:
            return
        self.sku_prev_btn.configure(state="normal" if page > 1 else "disabled")
        self.sku_next_btn.configure(state="normal" if has_next else "disabled")

    def _bind_sku_combo_focus(self, combo: ttk.Combobox) -> None:
        combo.bind("<FocusIn>", lambda _e, c=combo: self._on_sku_combo_focus(c), add="+")

    def _on_sku_combo_focus(self, combo: ttk.Combobox) -> None:
        self._last_sku_combo_focused = combo

    def _sku_combo_paging_index(self) -> Optional[int]:
        combo = self._last_sku_combo_focused
        if combo and combo in self.sku_combos:
            return self.sku_combos.index(combo)
        if self.sku_combos:
            return 0
        return None

    def _reopen_sku_dropdown(self, combo: ttk.Combobox) -> None:
        if not combo.winfo_exists():
            return
        combo.focus_set()
        try:
            combo.event_generate("<Down>")
        except tk.TclError:
            pass

    def _schedule_sku_dropdown_reopen(self) -> None:
        reopen_idx = self._sku_page_reopen_index
        self._sku_page_reopen_index = None
        if reopen_idx is None or not self.sku_combos:
            return
        if 0 <= reopen_idx < len(self.sku_combos):
            self.root.after(100, lambda i=reopen_idx: self._reopen_sku_dropdown(self.sku_combos[i]))

    def _mount_sku_pager(self, parent: ttk.Frame) -> None:
        """分页按钮紧跟 SKU 下拉框；控件须与 parent 同层级创建，避免跨父 pack。"""
        if self.sku_pager is not None and self.sku_pager.winfo_exists():
            self.sku_pager.destroy()
        pager = ttk.Frame(parent, style="Card.TFrame")
        pager.pack(side=tk.LEFT, padx=(6, 0))
        self.sku_pager = pager
        self.sku_prev_btn = ttk.Button(
            pager, text="上一页", command=self._sku_prev_page, style="Secondary.TButton", width=6
        )
        self.sku_prev_btn.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(
            pager, textvariable=self.sku_page_label_var, style="CardMuted.TLabel", font=self._font_default
        ).pack(side=tk.LEFT, padx=2)
        self.sku_next_btn = ttk.Button(
            pager, text="下一页", command=self._sku_next_page, style="Secondary.TButton", width=6
        )
        self.sku_next_btn.pack(side=tk.LEFT, padx=(4, 0))
        page = self.sku_page
        count = len(self.sku_by_label)
        self._update_sku_pager_ui(page, count, self.sku_has_next)

    def _refresh_sku_combo_values(self) -> None:
        values = list(self.sku_by_label.keys())
        for combo, var in zip(self.sku_combos, self.sku_vars):
            self._fill_readonly_combo(combo, var, values)

    def _rebuild_sku_ui(self) -> None:
        self.sku_pager = None
        self.sku_prev_btn = None
        self.sku_next_btn = None
        for w in self.sku_slots_container.winfo_children():
            w.destroy()
        self.sku_vars.clear()
        self.sku_combos.clear()
        self._last_sku_combo_focused = None
        values = list(self.sku_by_label.keys())
        if not self.sku_slots:
            holder = ttk.Frame(self.sku_slots_container, style="Card.TFrame")
            holder.pack(fill=tk.X, pady=2)
            ttk.Label(
                holder,
                text="当前报文无 SKU 槽位，仍可翻页浏览 SKU 档案",
                style="CardMuted.TLabel",
                font=self._font_default,
            ).pack(side=tk.LEFT)
            self._mount_sku_pager(holder)
            return
        for idx, slot in enumerate(self.sku_slots):
            row = ttk.Frame(self.sku_slots_container, style="Card.TFrame")
            row.pack(fill=tk.X, pady=2)
            var = tk.StringVar()
            prefer = ""
            if slot.current_value:
                for lbl, code in self.sku_by_label.items():
                    if code == slot.current_value:
                        prefer = lbl
                        break
            combo = ttk.Combobox(
                row, textvariable=var, state="readonly", width=36, font=self._font_default
            )
            combo.pack(side=tk.LEFT, anchor="w")
            if idx == 0:
                self._mount_sku_pager(row)
            self._fill_readonly_combo(combo, var, values, prefer=prefer)
            self._guard_wheel(combo)
            self._bind_sku_combo_focus(combo)
            self.sku_vars.append(var)
            self.sku_combos.append(combo)

    def _report_query_error(self, title: str, err: Exception, *, silent: bool) -> None:
        msg = format_db_error(err)
        self.status_var.set(f"{title}：{msg}")
        if not silent:
            messagebox.showerror(title, msg)

    def _load_master_data(self, *, silent: bool = False, force: bool = False) -> None:
        if force:
            self._master_data_loaded = True
        self._load_shops(silent=silent)
        self._load_skus(reset_page=True, silent=silent)

    def _load_shops(self, *, silent: bool = False) -> None:
        platform = self.platform_var.get()
        platform_id = self._current_platform_id()

        def task():
            try:
                rows = query_shops(
                    self._env_key(),
                    self.shop_search_var.get().strip(),
                    platform_id=platform_id,
                )
                labels = []
                mapping = {}
                for r in rows:
                    label = f"{r.get('cp_c_shop_title', '')} | {r.get('seller_nick', '')} | {r.get('ecode', '')}"
                    labels.append(label)
                    mapping[label] = r
                self.root.after(
                    0,
                    lambda lbs=labels, mp=mapping, cnt=len(rows), plat=platform: self._apply_shops(
                        lbs, mp, cnt, plat
                    ),
                )
            except Exception as e:
                self.root.after(0, lambda err=e: self._report_query_error("查询店铺失败", err, silent=silent))

        threading.Thread(target=task, daemon=True).start()
        plat_hint = f"（{platform}）" if platform else ""
        self.status_var.set(f"正在查询店铺{plat_hint}...")

    def _apply_shops(
        self,
        labels: List[str],
        mapping: Dict[str, Dict[str, Any]],
        count: int,
        platform: str = "",
    ) -> None:
        auto_mapping: Dict[str, Any] = {SHOP_AUTO_LABEL: None}
        auto_mapping.update(mapping)
        auto_labels = [SHOP_AUTO_LABEL] + labels
        self.shop_by_label = auto_mapping
        self._fill_readonly_combo(self.shop_combo, self.shop_var, auto_labels)
        if labels:
            self._on_shop_change()
        else:
            self.selected_shop = None
        plat_hint = f"{platform} · " if platform else ""
        self.status_var.set(f"{self.env_var.get()} 环境 {plat_hint}已加载 {count} 个店铺")

    def _on_shop_change(self) -> None:
        self.selected_shop = self.shop_by_label.get(self.shop_var.get())

    def _load_skus(self, reset_page: bool = False, *, silent: bool = False) -> None:
        if reset_page:
            self.sku_page = 1

        page = self.sku_page
        keyword = self.sku_search_var.get().strip()

        def task():
            try:
                result = query_skus(self._env_key(), keyword, page=page)
                labels = []
                mapping = {}
                for r in result.get("items") or []:
                    name = r.get("ename") or ""
                    label = f"{r.get('ecode', '')} | {name}" if name else str(r.get("ecode", ""))
                    labels.append(label)
                    mapping[label] = r.get("ecode", "")
                self.root.after(
                    0,
                    lambda lbs=labels, mp=mapping, res=result: self._apply_skus(lbs, mp, res),
                )
            except Exception as e:
                self.root.after(0, lambda err=e: self._report_query_error("查询SKU失败", err, silent=silent))

        threading.Thread(target=task, daemon=True).start()
        self.status_var.set(f"正在查询 SKU（第 {page} 页）...")

    def _apply_skus(self, labels: List[str], mapping: Dict[str, str], result: Dict[str, Any]) -> None:
        self.sku_by_label = mapping
        self.sku_has_next = bool(result.get("has_next"))
        self.sku_page = int(result.get("page") or 1)
        page = self.sku_page
        size = int(result.get("page_size") or sku_query_page_size(self.cfg))
        count = int(result.get("count") or len(labels))
        self._refresh_sku_combo_values()
        self._update_sku_pager_ui(page, count, self.sku_has_next)
        self.status_var.set(f"SKU 第 {page} 页已加载 {count} 条（每页 {size} 条）")
        self._schedule_sku_dropdown_reopen()

    def _sku_prev_page(self) -> None:
        if self.sku_page <= 1:
            return
        self._sku_page_reopen_index = self._sku_combo_paging_index()
        self.sku_page -= 1
        self._load_skus()

    def _sku_next_page(self) -> None:
        if not self.sku_has_next:
            return
        self._sku_page_reopen_index = self._sku_combo_paging_index()
        self.sku_page += 1
        self._load_skus()

    def _collect_sku_values(self) -> List[str]:
        values = []
        for var in self.sku_vars:
            label = var.get().strip()
            if label in self.sku_by_label:
                values.append(self.sku_by_label[label])
            elif label:
                values.append(label.split("|")[0].strip())
            else:
                values.append("")
        return values

    def _resolve_shop(self, seller_nick: str, selected: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        """优先用用户手动选择的店铺（替换卖家昵称）；否则从报文卖家昵称反查店铺档案。"""
        if selected and selected.get("seller_nick"):
            return selected["seller_nick"], selected
        if not seller_nick:
            raise ValueError("无法从报文中提取卖家昵称，且未选择店铺，请补充卖家昵称或选择店铺档案")
        shop = query_shop_by_seller_nick(self._env_key(), seller_nick)
        if not shop:
            raise ValueError(f"店铺档案中未找到卖家昵称 [{seller_nick}] 对应的店铺，请检查 cp_c_shop")
        return seller_nick, shop

    def _preview(self) -> None:
        try:
            platform = self.platform_var.get()
            scenario = self.scenario_var.get()
            raw = load_template(platform, scenario)
            msg_seller = extract_seller_nick(raw) or ""
            selected = self.selected_shop or self.shop_by_label.get(self.shop_var.get())
            seller, shop = self._resolve_shop(msg_seller, selected)
            build = build_preview_message(
                platform,
                scenario,
                seller,
                shop,
                self._collect_sku_values(),
                base_raw=raw,
            )
            self.last_build = build
            text = format_message_json(build.mq_message)
            self.preview_text.delete("1.0", tk.END)
            self.preview_text.insert(tk.END, text)
            warn = "\n".join(build.warnings) if build.warnings else ""
            msg = f"预览完成 tid={build.tid}"
            if warn:
                msg += f" | 提示: {warn}"
            self.status_var.set(msg)
        except Exception as e:
            messagebox.showerror("预览失败", str(e))

    def _generate(self) -> None:
        def task():
            try:
                platform = self.platform_var.get()
                editor_text = self.preview_text.get("1.0", tk.END).strip()
                if not editor_text:
                    self.root.after(0, lambda: messagebox.showwarning("提示", "报文为空，请先点击「仅预览」"))
                    return

                editor_mq = parse_message_text(editor_text)
                family = detect_message_family(editor_mq)
                seller, shop = self._resolve_shop(extract_seller_nick(editor_mq) or "", None)

                from ip_order_service import insert_taobao_order, insert_jingdong_order

                if family in ("jdp_array", "jdp_object"):
                    mid, tid = insert_taobao_order(self._env_key(), editor_mq, shop)
                    lines = [
                        f"淘宝订单已写入中间表 ip_b_taobao_order id={mid}",
                        f"TID={tid}",
                    ]

                    def done():
                        messagebox.showinfo("生成成功", "\n".join(lines))
                        self.status_var.set(f"淘宝订单已生成 tid={tid} id={mid}")

                    self.root.after(0, done)
                    return

                if family == "jd_array":
                    mid, order_id = insert_jingdong_order(self._env_key(), editor_mq, shop)
                    lines = [
                        f"京东订单已写入中间表 ip_b_jingdong_order id={mid}",
                        f"orderId={order_id}",
                    ]

                    def done():
                        messagebox.showinfo("生成成功", "\n".join(lines))
                        self.status_var.set(f"京东订单已生成 orderId={order_id} id={mid}")

                    self.root.after(0, done)
                    return

                from settings import get_platform_meta

                meta = get_platform_meta(platform)
                if self.last_build:
                    build = self.last_build
                    build.seller_nick = seller
                    build.shop = shop
                    build.platform_id = int(meta["platform_id"])
                    build.method = meta.get("method") or build.method
                else:
                    build = build_preview_message(
                        platform,
                        self.scenario_var.get(),
                        seller,
                        shop,
                        self._collect_sku_values(),
                        base_raw=editor_mq,
                    )

                db_msg, tid = finalize_build_for_insert(self._env_key(), build, editor_mq)
                source_id = insert_source(self._env_key(), build, db_msg, tid)
                transfer_result = run_transfer(self._env_key(), source_id, tid)

                lines = [
                    f"报文已写入 source_id={source_id}",
                    f"TID={tid}",
                    f"转单: {json.dumps(transfer_result, ensure_ascii=False, default=str)[:600]}",
                ]

                def done():
                    messagebox.showinfo("生成成功", "\n".join(lines))
                    self.status_var.set(f"订单已生成 tid={tid} source_id={source_id}")

                self.root.after(0, done)
            except Exception as e:
                self.root.after(0, lambda err=e: messagebox.showerror("生成失败", str(err)))

        self.status_var.set("正在生成订单...")
        threading.Thread(target=task, daemon=True).start()


def main() -> None:
    from win_dpi import enable_windows_dpi_awareness

    enable_windows_dpi_awareness()
    root = tk.Tk()
    PlatformOrderApp(root, standalone=True)
    root.mainloop()


if __name__ == "__main__":
    main()
