# -*- coding: utf-8 -*-
"""
公域平台逆向造单

独立启动: python reverse_order_gui.py
集成启动: python api_tool.py（左侧导航「公域平台逆向造单」）
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Optional

from order_service import format_db_error
from reverse_message_service import format_reverse_json, parse_reverse_editor
from reverse_order_service import (
    build_reverse_message,
    ensure_retail_order_platform_match,
    insert_platform_return_order,
    run_reverse_transfer,
)
from settings import (
    env_label_map,
    get_env_by_label,
    list_platform_folders,
    list_reverse_scenarios,
    load_config,
)

from ui_theme import (
    MAIN_BG,
    SECTION_GAP,
    SECTION_PADX,
    apply_theme,
    configure_log_text,
)


class ReverseOrderApp:
    def __init__(self, parent: tk.Misc, *, standalone: bool = True):
        self.root = parent
        cfg = load_config()
        if standalone and isinstance(parent, tk.Tk):
            self.root.title((cfg.get("ui") or {}).get("reverse_window_title", "公域平台逆向造单"))
            self.root.geometry("1000x720")
            self.root.minsize(880, 620)
            self.root.configure(bg=MAIN_BG)

        self.cfg = cfg
        self.env_labels = list(env_label_map(self.cfg).values())
        default_label = (list(env_label_map(self.cfg).values()) or ["TEST"])[0]

        self.env_var = tk.StringVar(value=default_label)
        self.platform_var = tk.StringVar()
        self.scenario_var = tk.StringVar()
        self.tid_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.last_build: Optional[ReverseBuildResult] = None
        self._font_default = None
        self.mono_font = ("Consolas", 10)

        self._init_theme()
        self._build_ui()
        self._reload_platforms()
        self._on_platform_change()

    def _init_theme(self) -> None:
        root = self.root.winfo_toplevel()
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        fonts = apply_theme(style, root)
        self._font_default = fonts.default
        self.mono_font = fonts.mono

    def _env_key(self) -> str:
        return get_env_by_label(self.env_var.get(), self.cfg)

    def _build_ui(self) -> None:
        fd = self._font_default
        top = ttk.Frame(self.root, style="Main.TFrame", padding=(SECTION_PADX, 8, SECTION_PADX, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="环境", style="Hint.TLabel", font=fd).grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        self._env_combo = ttk.Combobox(
            top, textvariable=self.env_var, values=self.env_labels, state="readonly", width=12, font=fd
        )
        self._env_combo.grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(top, text="平台", style="Hint.TLabel", font=fd).grid(row=0, column=2, sticky="e", padx=(20, 6), pady=3)
        self.platform_combo = ttk.Combobox(
            top, textvariable=self.platform_var, state="readonly", width=14, font=fd
        )
        self.platform_combo.grid(row=0, column=3, sticky="w", pady=3)
        self.platform_combo.bind("<<ComboboxSelected>>", lambda e: self._on_platform_change())

        ttk.Label(top, text="场景报文", style="Hint.TLabel", font=fd).grid(
            row=0, column=4, sticky="e", padx=(20, 6), pady=3
        )
        self.scenario_combo = ttk.Combobox(
            top, textvariable=self.scenario_var, state="readonly", width=28, font=fd
        )
        self.scenario_combo.grid(row=0, column=5, sticky="w", pady=3)

        form = ttk.LabelFrame(self.root, text=" 平台单号（零售发货单 tid） ", style="Card.TLabelframe")
        form.pack(fill=tk.X, padx=SECTION_PADX, pady=(0, SECTION_GAP))
        row = ttk.Frame(form, style="Card.TFrame")
        row.pack(fill=tk.X)
        ttk.Label(row, text="平台单号", style="Card.TLabel", font=fd).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.tid_var, width=36, font=fd).pack(side=tk.LEFT, padx=(6, 8))

        btn_frame = ttk.Frame(self.root, style="Main.TFrame", padding=(SECTION_PADX, 4, SECTION_PADX, 4))
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="仅预览", command=self._preview, style="Cyan.TButton").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="生成售后申请", command=self._generate, style="Primary.TButton").pack(side=tk.LEFT)
        ttk.Label(
            btn_frame,
            text="流程：选环境/逆向场景 → 填平台单号 → 仅预览 → 生成售后申请",
            style="Hint.TLabel",
            font=fd,
        ).pack(side=tk.LEFT, padx=(12, 0))

        preview_frame = ttk.LabelFrame(
            self.root,
            text=" 报文预览（明细字段已从 oc_b_order / oc_b_order_item 回填） ",
            style="Card.TLabelframe",
        )
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=SECTION_PADX, pady=(0, SECTION_GAP))
        lines = int((self.cfg.get("ui") or {}).get("message_preview_lines", 22))
        self.preview_text = scrolledtext.ScrolledText(preview_frame, height=lines, wrap=tk.NONE)
        configure_log_text(self.preview_text, mono=self.mono_font)
        self.preview_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(self.root, textvariable=self.status_var, style="Hint.TLabel", font=fd, anchor="w").pack(
            fill=tk.X, padx=SECTION_PADX, pady=(0, 8)
        )

    @staticmethod
    def _fill_readonly_combo(combo, var, values, *, prefer: str = "") -> None:
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
            var.set("")
        combo.configure(state="readonly")

    def _reload_platforms(self) -> None:
        platforms = list_platform_folders()
        prefer = self.platform_var.get() if self.platform_var.get() in platforms else ""
        self._fill_readonly_combo(self.platform_combo, self.platform_var, platforms, prefer=prefer)

    def _on_platform_change(self) -> None:
        platform = self.platform_var.get()
        scenarios = list_reverse_scenarios(platform, self.cfg) if platform else []
        prefer = self.scenario_var.get() if self.scenario_var.get() in scenarios else ""
        self._fill_readonly_combo(self.scenario_combo, self.scenario_var, scenarios, prefer=prefer)
        if not scenarios:
            self.status_var.set(f"{platform or '平台'} 下无逆向场景（需含拒收/退货/换货）")

    def _validate_inputs(self) -> tuple:
        platform = self.platform_var.get().strip()
        scenario = self.scenario_var.get().strip()
        tid = self.tid_var.get().strip()
        if not platform or not scenario:
            raise ValueError("请选择平台与逆向场景报文")
        if not tid:
            raise ValueError("请填写平台单号（零售发货单 tid）")
        return platform, scenario, tid

    def _preview(self) -> None:
        try:
            platform, scenario, tid = self._validate_inputs()
            build = build_reverse_message(self._env_key(), platform, scenario, tid)
            self.last_build = build
            self.preview_text.delete("1.0", tk.END)
            self.preview_text.insert(tk.END, format_reverse_json(build.envelope))
            item_cnt = len(build.payload.get("items") or [])
            msg = (
                f"预览完成 [{build.message_family}] return_no={build.return_no}"
                + (f"，明细 {item_cnt} 条" if item_cnt else "")
            )
            if build.warnings:
                msg += " | " + "; ".join(build.warnings)
            self.status_var.set(msg)
        except Exception as e:
            messagebox.showerror("预览失败", format_db_error(e))

    def _generate(self) -> None:
        def task():
            try:
                platform, scenario, tid = self._validate_inputs()
                editor_text = self.preview_text.get("1.0", tk.END).strip()
                if editor_text:
                    ensure_retail_order_platform_match(self._env_key(), platform, tid, cfg=self.cfg)
                    build = parse_reverse_editor(editor_text, self.last_build)
                else:
                    build = build_reverse_message(self._env_key(), platform, scenario, tid)

                row_id = insert_platform_return_order(self._env_key(), build)
                transfer_result = run_reverse_transfer(self._env_key(), row_id)
                lines = [
                    f"已写入 ip_b_platform_return_order id={row_id}",
                    f"return_no={build.return_no} tid={build.tid}",
                    f"转单: {json.dumps(transfer_result, ensure_ascii=False, default=str)[:600]}",
                ]

                def done():
                    messagebox.showinfo("生成成功", "\n".join(lines))
                    self.status_var.set(f"售后申请已生成 id={row_id} return_no={build.return_no}")

                self.root.after(0, done)
            except Exception as e:
                self.root.after(0, lambda err=e: messagebox.showerror("生成失败", format_db_error(err)))

        self.status_var.set("正在生成售后申请...")
        threading.Thread(target=task, daemon=True).start()


def main() -> None:
    from win_dpi import enable_windows_dpi_awareness

    enable_windows_dpi_awareness()
    root = tk.Tk()
    ReverseOrderApp(root, standalone=True)
    root.mainloop()


if __name__ == "__main__":
    main()
