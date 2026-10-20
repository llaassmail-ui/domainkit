"""Tkinter GUI for domainkit.

Usage:
    python -m domainkit.gui

Credentials can be configured in the GUI or read from environment variables.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import Any, Dict, List, Tuple

from .aws import DistributionOptions, OriginSpec, ORIGIN_TYPES
from .batch import load_batch, run_batch
from .cli import _make_onboarder, cmd_doctor
from .config import Settings
from .errors import DomainKitError
from .gitlab import GitLabClient, normalize_project_id
from .naming import branch_name, normalize_domain, service_domain, variable_key
from .vault import (
    CREDENTIAL_KEYS,
    apply_to_env,
    delete_credentials,
    describe,
    load_credentials,
    save_credentials,
    store_path,
)
from .workflow import DEFAULT_CERTIFICATE_TIMEOUT, Outcome


MAX_ORIGIN_PRESETS = 50


STATUS_LABELS = {
    "unknown": "待自检",
    "ok": "正常",
    "err": "失败",
    "missing": "未配置",
    "running": "运行中",
    "idle": "空闲",
}


STEP_OUTCOME_LABELS = {
    Outcome.CREATED.value: "已创建",
    Outcome.UPDATED.value: "已更新",
    Outcome.EXISTS.value: "已存在",
    Outcome.PLANNED.value: "将执行",
    Outcome.SKIPPED.value: "已跳过",
    Outcome.FAILED.value: "失败",
}


STEP_TITLES = {
    "certificate": "ACM 证书",
    "dns-validation": "DNS 验证",
    "certificate-issued": "证书签发",
    "cloudfront-create": "创建 CloudFront",
    "cloudfront": "CloudFront 绑定",
    "site-dns": "www DNS",
    "apex-forward": "裸域名 301",
    "gitlab": "GitLab",
    "gitlab-branch": "GitLab 分支",
    "gitlab-variable": "GitLab 变量",
    "aborted": "中止",
}


def _status_text(state: str, detail: str = "") -> str:
    label = STATUS_LABELS.get(state, state)
    return "{0}: {1}".format(label, detail) if detail else label


def _derive_domain_preview(domain: str, *, prefix: str = "CLOUDFRONT") -> Dict[str, str]:
    normalized = normalize_domain(domain)
    served = service_domain(normalized)
    return {
        "domain": normalized,
        "service_domain": served,
        "branch": branch_name(normalized),
        "variable_key": variable_key(normalized, prefix),
        "certificate": served,
    }


def _format_step_summary(step: Any) -> Tuple[str, str, str]:
    name = getattr(step, "name", "")
    outcome = getattr(step, "outcome", "")
    value = getattr(outcome, "value", outcome)
    title = STEP_TITLES.get(str(name), str(name) or "步骤")
    status = STEP_OUTCOME_LABELS.get(str(value), str(value) or "未知")
    detail = str(getattr(step, "detail", "") or "")
    return title, status, detail


def _normalize_origin_preset(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    origin = str(raw.get("origin") or "").strip()
    if not origin:
        return {}
    origin_type = str(raw.get("origin_type") or "s3-website").strip() or "s3-website"
    if origin_type not in ORIGIN_TYPES:
        origin_type = "s3-website"
    origin_path = str(raw.get("origin_path") or "").strip()
    return {"origin": origin, "origin_type": origin_type, "origin_path": origin_path}


def _origin_preset_key(preset: Dict[str, str]) -> Tuple[str, str, str]:
    return (
        preset.get("origin", "").strip().lower(),
        preset.get("origin_type", "").strip(),
        preset.get("origin_path", "").strip(),
    )


def _origin_preset_label(preset: Dict[str, str]) -> str:
    origin = preset.get("origin", "").strip()
    path = preset.get("origin_path", "").strip()
    origin_type = preset.get("origin_type", "s3-website").strip() or "s3-website"
    return "{0}{1} ({2})".format(origin, path, origin_type)


def _merge_origin_preset(
    presets: List[Dict[str, str]],
    preset: Dict[str, str],
    *,
    limit: int = MAX_ORIGIN_PRESETS,
) -> List[Dict[str, str]]:
    normalized = _normalize_origin_preset(preset)
    if not normalized:
        return list(presets)
    key = _origin_preset_key(normalized)
    merged = [item for item in presets if _origin_preset_key(item) != key]
    return [normalized] + merged[: max(0, limit - 1)]


def _load_origin_presets(defaults: Dict[str, Any]) -> List[Dict[str, str]]:
    seen = set()
    presets: List[Dict[str, str]] = []
    for raw in defaults.get("origin_presets") or []:
        preset = _normalize_origin_preset(raw)
        if not preset:
            continue
        key = _origin_preset_key(preset)
        if key in seen:
            continue
        seen.add(key)
        presets.append(preset)
    legacy = _normalize_origin_preset(
        {
            "origin": defaults.get("origin"),
            "origin_type": defaults.get("origin_type"),
            "origin_path": defaults.get("origin_path"),
        }
    )
    if legacy and _origin_preset_key(legacy) not in seen:
        presets.insert(0, legacy)
    return presets[:MAX_ORIGIN_PRESETS]


class DomainKitGUI:
    """Main GUI window for domainkit."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("DomainKit v2")
        self.root.geometry("900x700")
        self.env = dict(os.environ)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.store_path = store_path(self.env)
        self.defaults_path = self.store_path.with_name("gui-defaults.json")
        self.defaults: Dict[str, Any] = {}
        self.origin_presets: List[Dict[str, str]] = []
        self.origin_presets_by_label: Dict[str, Dict[str, str]] = {}
        self.stored: Dict[str, str] = {}
        self.gitlab_projects_by_label: Dict[str, Dict[str, Any]] = {}
        self._load_error = ""
        self._defaults_load_error = ""
        try:
            self.stored = load_credentials(path=self.store_path)
        except DomainKitError as exc:
            self._load_error = str(exc)
        try:
            self.defaults = self._load_defaults()
            self.origin_presets = _load_origin_presets(self.defaults)
        except Exception as exc:  # noqa: BLE001 - surface defaults errors in the log
            self._defaults_load_error = str(exc)
        # Saved credentials win over inherited environment variables: the user
        # typed them here on purpose, and blanks must not overwrite them.
        for key, value in self.stored.items():
            if value:
                self.env[key] = value

        self._build_ui()
        self._check_log_queue()
        if self._load_error:
            self._log("读取已保存凭证失败: {0}".format(self._load_error))
        elif self.stored:
            self._log("已从 {0} 加载 {1} 项凭证".format(self.store_path, len(self.stored)))
        if self._defaults_load_error:
            self._log("读取默认配置失败: {0}".format(self._defaults_load_error))
        elif self.defaults:
            self._log("已从 {0} 加载默认配置".format(self.defaults_path))

    def _load_defaults(self) -> Dict[str, Any]:
        if not self.defaults_path.is_file():
            return {}
        data = json.loads(self.defaults_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _collect_defaults(self) -> Dict[str, Any]:
        return {
            "dns_provider": self.dns_provider_var.get(),
            "spaceship_endpoint": self.ss_endpoint_var.get().strip(),
            "gitlab_project": self.project_var.get().strip(),
            "base_branch": self.base_branch_var.get().strip() or "dev",
            "origin": self.origin_var.get().strip(),
            "origin_type": self.origin_type_var.get().strip() or "s3-website",
            "origin_path": self.origin_path_var.get().strip(),
            "origin_presets": self.origin_presets,
            "create_distribution": self.create_cf_var.get(),
            "spa": self.spa_var.get(),
            "wait_certificate": self.wait_cert_var.get(),
            "skip_dns": self.skip_dns_var.get(),
            "skip_gitlab": self.skip_gitlab_var.get(),
            "skip_site_dns": self.skip_site_dns_var.get(),
        }

    def _save_defaults(self) -> None:
        values = self._collect_defaults()
        self.defaults_path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults_path.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")
        self.defaults = values
        self.origin_presets = _load_origin_presets(self.defaults)
        self._refresh_origin_presets()
        self._log("默认配置已保存到 {0}".format(self.defaults_path))
        messagebox.showinfo("已保存", "默认配置已保存。下次启动会自动加载这些表单值。")

    def _apply_defaults(self) -> None:
        data = self.defaults
        self.dns_provider_var.set(str(data.get("dns_provider") or "godaddy"))
        if data.get("spaceship_endpoint"):
            self.ss_endpoint_var.set(str(data["spaceship_endpoint"]))
        if not data:
            return
        self.project_var.set(str(data.get("gitlab_project") or ""))
        self.base_branch_var.set(str(data.get("base_branch") or "dev"))
        self.origin_var.set(str(data.get("origin") or ""))
        self.origin_type_var.set(str(data.get("origin_type") or "s3-website"))
        self.origin_path_var.set(str(data.get("origin_path") or ""))
        self.create_cf_var.set(bool(data.get("create_distribution", False)))
        self.spa_var.set(bool(data.get("spa", False)))
        self.wait_cert_var.set(bool(data.get("wait_certificate", False)))
        self.skip_dns_var.set(bool(data.get("skip_dns", False)))
        self.skip_gitlab_var.set(bool(data.get("skip_gitlab", False)))
        self.skip_site_dns_var.set(bool(data.get("skip_site_dns", False)))

    def _write_defaults(self) -> None:
        self.defaults_path.parent.mkdir(parents=True, exist_ok=True)
        self.defaults_path.write_text(json.dumps(self.defaults, indent=2, ensure_ascii=False), encoding="utf-8")

    def _refresh_origin_presets(self) -> None:
        if not hasattr(self, "origin_preset_combo"):
            return
        query = ""
        if hasattr(self, "origin_preset_search_var"):
            query = self.origin_preset_search_var.get().strip().lower()
        labels = []
        self.origin_presets_by_label = {}
        for preset in self.origin_presets:
            label = _origin_preset_label(preset)
            if query and query not in label.lower():
                continue
            labels.append(label)
            self.origin_presets_by_label[label] = preset
        self.origin_preset_combo.configure(values=labels)
        current = self.origin_preset_var.get()
        if current not in self.origin_presets_by_label:
            self.origin_preset_var.set("")

    def _filter_origin_presets(self) -> None:
        self._refresh_origin_presets()
        count = len(self.origin_presets_by_label)
        self._log("源站预设筛选结果: {0} 条".format(count))

    def _select_origin_preset(self, _event=None) -> None:
        preset = self.origin_presets_by_label.get(self.origin_preset_var.get())
        if not preset:
            return
        self.origin_var.set(preset.get("origin", ""))
        self.origin_type_var.set(preset.get("origin_type", "s3-website"))
        self.origin_path_var.set(preset.get("origin_path", ""))
        self._log("已选择源站预设: {0}".format(self.origin_preset_var.get()))

    def _save_origin_preset(self) -> None:
        preset = _normalize_origin_preset(
            {
                "origin": self.origin_var.get(),
                "origin_type": self.origin_type_var.get(),
                "origin_path": self.origin_path_var.get(),
            }
        )
        if not preset:
            messagebox.showwarning("警告", "请先填写源域名")
            return
        self.origin_presets = _merge_origin_preset(self.origin_presets, preset)
        self.defaults = dict(self.defaults)
        self.defaults["origin_presets"] = self.origin_presets
        self.defaults["origin"] = preset["origin"]
        self.defaults["origin_type"] = preset["origin_type"]
        self.defaults["origin_path"] = preset["origin_path"]
        self._write_defaults()
        self.origin_preset_search_var.set("")
        self._refresh_origin_presets()
        label = _origin_preset_label(preset)
        self.origin_preset_var.set(label)
        self._log("源站预设已保存到 {0}: {1}".format(self.defaults_path, label))
        messagebox.showinfo("已保存", "源站预设已保存。下次启动会自动出现在下拉框里。")

    def _delete_origin_preset(self) -> None:
        label = self.origin_preset_var.get()
        preset = self.origin_presets_by_label.get(label)
        if not preset:
            messagebox.showwarning("警告", "请先选择要删除的源站预设")
            return
        self.origin_presets = [
            item for item in self.origin_presets if _origin_preset_key(item) != _origin_preset_key(preset)
        ]
        self.defaults = dict(self.defaults)
        self.defaults["origin_presets"] = self.origin_presets
        self._write_defaults()
        self._refresh_origin_presets()
        self._log("已删除源站预设: {0}".format(label))

    def _build_ui(self) -> None:
        self.root.geometry("1120x780")
        self.root.minsize(980, 680)

        shell = ttk.Frame(self.root, padding=8)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.rowconfigure(1, weight=1)
        shell.rowconfigure(2, weight=0)
        shell.columnconfigure(1, weight=1)

        self._build_status_bar(shell)

        nav_frame = ttk.LabelFrame(shell, text="导航", padding=(8, 8))
        nav_frame.grid(row=1, column=0, sticky=tk.NS, padx=(0, 8))
        self.nav = tk.Listbox(nav_frame, width=14, height=8, exportselection=False, activestyle="none")
        for label in ("单域名上线", "批量上线", "凭证与项目", "日志"):
            self.nav.insert(tk.END, label)
        self.nav.pack(fill=tk.BOTH, expand=True)
        self.nav.bind("<<ListboxSelect>>", self._switch_page)

        self.content_frame = ttk.Frame(shell)
        self.content_frame.grid(row=1, column=1, sticky=tk.NSEW)
        self.content_frame.rowconfigure(0, weight=1)
        self.content_frame.columnconfigure(0, weight=1)

        self.single_frame = ttk.Frame(self.content_frame)
        self.batch_frame = ttk.Frame(self.content_frame)
        self.credentials_frame = ttk.Frame(self.content_frame)
        self.log_page_frame = ttk.Frame(self.content_frame)
        self.pages = {
            "单域名上线": self.single_frame,
            "批量上线": self.batch_frame,
            "凭证与项目": self.credentials_frame,
            "日志": self.log_page_frame,
        }

        self._build_credentials_tab()
        self._build_single_tab()
        self._build_batch_tab()
        self._build_log_area(shell)
        self._build_log_page()
        self.nav.selection_set(0)
        self._show_page("单域名上线")

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.LabelFrame(parent, text="状态", padding=(10, 6))
        bar.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        for index in range(6):
            bar.columnconfigure(index, weight=1)

        self.aws_status_var = tk.StringVar(value=_status_text("unknown"))
        self.godaddy_status_var = tk.StringVar(value=_status_text("unknown"))
        self.spaceship_status_var = tk.StringVar(value=_status_text("unknown"))
        self.gitlab_status_var = tk.StringVar(value=_status_text("unknown"))
        self.project_status_var = tk.StringVar(value="项目: 未选择")
        self.run_status_var = tk.StringVar(value=_status_text("idle"))

        ttk.Label(bar, text="AWS").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(bar, textvariable=self.aws_status_var).grid(row=1, column=0, sticky=tk.W)
        ttk.Label(bar, text="GoDaddy").grid(row=0, column=1, sticky=tk.W)
        ttk.Label(bar, textvariable=self.godaddy_status_var).grid(row=1, column=1, sticky=tk.W)
        ttk.Label(bar, text="GitLab").grid(row=0, column=2, sticky=tk.W)
        ttk.Label(bar, textvariable=self.gitlab_status_var).grid(row=1, column=2, sticky=tk.W)
        ttk.Label(bar, text="当前项目").grid(row=0, column=3, sticky=tk.W)
        ttk.Label(bar, textvariable=self.project_status_var).grid(row=1, column=3, sticky=tk.W)
        ttk.Label(bar, text="任务").grid(row=0, column=4, sticky=tk.W)
        ttk.Label(bar, textvariable=self.run_status_var).grid(row=1, column=4, sticky=tk.W)
        ttk.Label(bar, text="Spaceship").grid(row=0, column=5, sticky=tk.W)
        ttk.Label(bar, textvariable=self.spaceship_status_var).grid(row=1, column=5, sticky=tk.W)

    def _switch_page(self, _event=None) -> None:
        selection = self.nav.curselection()
        if not selection:
            return
        self._show_page(self.nav.get(selection[0]))

    def _show_page(self, name: str) -> None:
        for frame in self.pages.values():
            frame.grid_forget()
        self.pages[name].grid(row=0, column=0, sticky=tk.NSEW)

    def _build_credentials_tab(self) -> None:
        """Build the credentials configuration tab."""
        canvas = tk.Canvas(self.credentials_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.credentials_frame, orient=tk.VERTICAL, command=canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        form = ttk.Frame(canvas, padding=10)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        info_frame = ttk.LabelFrame(form, text="说明", padding=10)
        info_frame.grid(row=0, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        info_text = (
            "在这里配置 AWS、GoDaddy 和 GitLab 的凭证。\n"
            "勾选「记住凭证」后点击「保存凭证」,会用 Windows DPAPI 按当前用户加密保存到本机,\n"
            "下次启动自动加载,日志只显示掩码摘要。留空的字段不会写入,也不会覆盖已保存的值。"
        )
        ttk.Label(info_frame, text=info_text, justify=tk.LEFT).pack()

        row = 1
        aws_frame = ttk.LabelFrame(form, text="AWS 凭证", padding=10)
        aws_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row += 1

        aws_row = 0
        ttk.Label(aws_frame, text="Access Key ID:").grid(row=aws_row, column=0, sticky=tk.W, pady=3)
        self.aws_key_var = tk.StringVar(value=self.env.get("AWS_ACCESS_KEY_ID", ""))
        self.aws_key_entry = ttk.Entry(aws_frame, textvariable=self.aws_key_var, width=50)
        self.aws_key_entry.grid(row=aws_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.aws_key_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(aws_frame, text="显示", variable=self.aws_key_show_var, 
                        command=lambda: self._toggle_show(self.aws_key_entry, self.aws_key_show_var)).grid(
            row=aws_row, column=2, pady=3
        )

        aws_row += 1
        ttk.Label(aws_frame, text="Secret Access Key:").grid(row=aws_row, column=0, sticky=tk.W, pady=3)
        self.aws_secret_var = tk.StringVar(value=self.env.get("AWS_SECRET_ACCESS_KEY", ""))
        self.aws_secret_entry = ttk.Entry(aws_frame, textvariable=self.aws_secret_var, width=50, show="*")
        self.aws_secret_entry.grid(row=aws_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.aws_secret_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(aws_frame, text="显示", variable=self.aws_secret_show_var,
                        command=lambda: self._toggle_show(self.aws_secret_entry, self.aws_secret_show_var)).grid(
            row=aws_row, column=2, pady=3
        )

        aws_row += 1
        ttk.Label(aws_frame, text="Session Token (可选):").grid(row=aws_row, column=0, sticky=tk.W, pady=3)
        self.aws_token_var = tk.StringVar(value=self.env.get("AWS_SESSION_TOKEN", ""))
        self.aws_token_entry = ttk.Entry(aws_frame, textvariable=self.aws_token_var, width=50, show="*")
        self.aws_token_entry.grid(row=aws_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.aws_token_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(aws_frame, text="显示", variable=self.aws_token_show_var,
                        command=lambda: self._toggle_show(self.aws_token_entry, self.aws_token_show_var)).grid(
            row=aws_row, column=2, pady=3
        )
        aws_frame.columnconfigure(1, weight=1)

        godaddy_frame = ttk.LabelFrame(form, text="GoDaddy API 凭证", padding=10)
        godaddy_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row += 1

        gd_row = 0
        ttk.Label(godaddy_frame, text="API Key:").grid(row=gd_row, column=0, sticky=tk.W, pady=3)
        self.gd_key_var = tk.StringVar(value=self.env.get("GODADDY_API_KEY", ""))
        self.gd_key_entry = ttk.Entry(godaddy_frame, textvariable=self.gd_key_var, width=50, show="*")
        self.gd_key_entry.grid(row=gd_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.gd_key_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(godaddy_frame, text="显示", variable=self.gd_key_show_var,
                        command=lambda: self._toggle_show(self.gd_key_entry, self.gd_key_show_var)).grid(
            row=gd_row, column=2, pady=3
        )

        gd_row += 1
        ttk.Label(godaddy_frame, text="API Secret:").grid(row=gd_row, column=0, sticky=tk.W, pady=3)
        self.gd_secret_var = tk.StringVar(value=self.env.get("GODADDY_API_SECRET", ""))
        self.gd_secret_entry = ttk.Entry(godaddy_frame, textvariable=self.gd_secret_var, width=50, show="*")
        self.gd_secret_entry.grid(row=gd_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.gd_secret_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(godaddy_frame, text="显示", variable=self.gd_secret_show_var,
                        command=lambda: self._toggle_show(self.gd_secret_entry, self.gd_secret_show_var)).grid(
            row=gd_row, column=2, pady=3
        )
        godaddy_frame.columnconfigure(1, weight=1)
        self.godaddy_credentials_frame = godaddy_frame

        spaceship_frame = ttk.LabelFrame(form, text="Spaceship DNS", padding=10)
        spaceship_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row += 1
        self.ss_key_var = tk.StringVar(value=self.env.get("SPACESHIP_API_KEY", ""))
        self.ss_secret_var = tk.StringVar(value=self.env.get("SPACESHIP_API_SECRET", ""))
        self.ss_endpoint_var = tk.StringVar(value=self.env.get("SPACESHIP_API_ENDPOINT", "https://spaceship.dev/api"))
        for index, (label, variable, secret) in enumerate([
            ("API Key:", self.ss_key_var, True),
            ("API Secret:", self.ss_secret_var, True),
            ("API Endpoint:", self.ss_endpoint_var, False),
        ]):
            ttk.Label(spaceship_frame, text=label).grid(row=index, column=0, sticky=tk.W)
            entry = ttk.Entry(spaceship_frame, textvariable=variable, show="*" if secret else "")
            entry.grid(row=index, column=1, sticky=tk.EW, padx=5, pady=3)
            if secret:
                show = tk.BooleanVar(value=False)
                ttk.Checkbutton(spaceship_frame, text="显示", variable=show,
                                command=lambda e=entry, v=show: self._toggle_show(e, v)).grid(row=index, column=2)
        spaceship_frame.columnconfigure(1, weight=1)
        self.spaceship_credentials_frame = spaceship_frame

        gitlab_frame = ttk.LabelFrame(form, text="GitLab 凭证", padding=10)
        gitlab_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row += 1

        gl_row = 0
        ttk.Label(gitlab_frame, text="GitLab URL:").grid(row=gl_row, column=0, sticky=tk.W, pady=3)
        self.gl_url_var = tk.StringVar(value=self.env.get("GITLAB_URL", ""))
        ttk.Entry(gitlab_frame, textvariable=self.gl_url_var, width=50).grid(row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5, columnspan=2)

        gl_row += 1
        ttk.Label(
            gitlab_frame,
            text="留空表示使用公有 https://gitlab.com;自建实例请填完整地址",
            foreground="#666666",
        ).grid(row=gl_row, column=1, sticky=tk.W, padx=5, columnspan=2)

        gl_row += 1
        ttk.Label(gitlab_frame, text="Access Token:").grid(row=gl_row, column=0, sticky=tk.W, pady=3)
        self.gl_token_var = tk.StringVar(value=self.env.get("GITLAB_TOKEN", ""))
        self.gl_token_entry = ttk.Entry(gitlab_frame, textvariable=self.gl_token_var, width=50, show="*")
        self.gl_token_entry.grid(row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5)
        self.gl_token_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(gitlab_frame, text="显示", variable=self.gl_token_show_var,
                        command=lambda: self._toggle_show(self.gl_token_entry, self.gl_token_show_var)).grid(
            row=gl_row, column=2, pady=3
        )

        gl_row += 1
        ttk.Label(gitlab_frame, text="项目搜索:").grid(row=gl_row, column=0, sticky=tk.W, pady=3)
        self.gl_project_search_var = tk.StringVar()
        ttk.Entry(gitlab_frame, textvariable=self.gl_project_search_var, width=50).grid(
            row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5
        )
        self.load_projects_btn = ttk.Button(gitlab_frame, text="加载 GitLab 项目", command=self._load_gitlab_projects)
        self.load_projects_btn.grid(row=gl_row, column=2, pady=3)

        gl_row += 1
        ttk.Label(gitlab_frame, text="项目列表:").grid(row=gl_row, column=0, sticky=tk.W, pady=3)
        self.gl_project_choice_var = tk.StringVar()
        self.gl_project_combo = ttk.Combobox(
            gitlab_frame,
            textvariable=self.gl_project_choice_var,
            values=[],
            state="readonly",
            width=50,
        )
        self.gl_project_combo.grid(row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5, columnspan=2)
        self.gl_project_combo.bind("<<ComboboxSelected>>", self._select_gitlab_project)

        gl_row += 1
        ttk.Label(gitlab_frame, text="Project ID:").grid(row=gl_row, column=0, sticky=tk.W, pady=3)
        self.gl_project_var = tk.StringVar(value=self.env.get("GITLAB_PROJECT_ID", ""))
        self.gl_project_var.trace_add("write", lambda *_: self._update_project_status())
        ttk.Entry(gitlab_frame, textvariable=self.gl_project_var, width=50).grid(
            row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5, columnspan=2
        )
        gitlab_frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=15)
        self.remember_var = tk.BooleanVar(value=bool(self.stored))
        ttk.Checkbutton(btn_frame, text="记住凭证(本机加密)", variable=self.remember_var).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="保存凭证", command=self._save_credentials).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="测试连接", command=self._doctor_check).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="查看已保存", command=self._show_saved).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="清空所有", command=self._clear_credentials).pack(side=tk.LEFT, padx=5)

        form.columnconfigure(0, weight=0)
        form.columnconfigure(1, weight=1)

    def _toggle_show(self, entry: ttk.Entry, var: tk.BooleanVar) -> None:
        """Toggle password visibility."""
        if var.get():
            entry.config(show="")
        else:
            entry.config(show="*")

    def _save_credentials(self) -> None:
        """Save credentials to environment variables for the current session."""
        values = self._collect_credentials()
        provider = self.dns_provider_var.get().upper()
        missing = [key for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                   provider + "_API_KEY", provider + "_API_SECRET",
                                   "GITLAB_PROJECT_ID", "GITLAB_TOKEN") if not values.get(key)]

        apply_to_env(self.env, values)
        apply_to_env(os.environ, values)  # type: ignore[arg-type]

        self._log("=== 凭证已应用到当前会话 ===")
        for key, shown in describe(values).items():
            self._log("  {0:<22} {1}".format(key, shown))

        note = ""
        if self.remember_var.get():
            try:
                saved_to = save_credentials(values, path=self.store_path)
                self.stored = {k: v for k, v in values.items() if v}
                self._log("已加密保存到 {0}".format(saved_to))
                note = "\n已加密保存到本机,下次启动自动加载。"
            except DomainKitError as exc:
                self._log("保存到本机失败: {0}".format(exc))
                note = "\n注意: 写入本机存储失败,本次仅在当前会话生效。"
        else:
            note = "\n未勾选「记住凭证」,关闭程序后需要重新输入。"

        if missing:
            self._log("以下字段为空,已跳过(不会覆盖原有值): {0}".format(", ".join(missing)))
            note += "\n\n仍为空的字段: {0}".format(", ".join(missing))

        messagebox.showinfo("已保存", "凭证已应用。{0}\n\n可以点击「测试连接」验证配置。".format(note))

    def _collect_credentials(self) -> Dict[str, str]:
        """Read the form, normalizing the GitLab fields as we go."""
        project_id = normalize_project_id(self.gl_project_var.get())
        if project_id != self.gl_project_var.get().strip():
            self.gl_project_var.set(project_id)
        if project_id and hasattr(self, "project_var"):
            self.project_var.set(project_id)
        url = self.gl_url_var.get().strip().rstrip("/")
        if url and url != self.gl_url_var.get().strip():
            self.gl_url_var.set(url)
        return {
            "AWS_ACCESS_KEY_ID": self.aws_key_var.get().strip(),
            "AWS_SECRET_ACCESS_KEY": self.aws_secret_var.get().strip(),
            "AWS_SESSION_TOKEN": self.aws_token_var.get().strip(),
            "GODADDY_API_KEY": self.gd_key_var.get().strip(),
            "GODADDY_API_SECRET": self.gd_secret_var.get().strip(),
            "SPACESHIP_API_KEY": self.ss_key_var.get().strip(),
            "SPACESHIP_API_SECRET": self.ss_secret_var.get().strip(),
            "SPACESHIP_API_ENDPOINT": self.ss_endpoint_var.get().strip(),
            "GITLAB_URL": url,
            "GITLAB_PROJECT_ID": project_id,
            "GITLAB_TOKEN": self.gl_token_var.get().strip(),
        }

    def _show_saved(self) -> None:
        """Print the masked contents of the encrypted store."""
        self._log("=== 已保存的凭证 ({0}) ===".format(self.store_path))
        try:
            stored = load_credentials(path=self.store_path)
        except DomainKitError as exc:
            self._log("读取失败: {0}".format(exc))
            return
        if not stored:
            self._log("本机没有保存任何凭证")
            return
        for key, shown in describe(stored).items():
            self._log("  {0:<22} {1}".format(key, shown))

    def _clear_credentials(self) -> None:
        """Clear all credential fields."""
        if not messagebox.askyesno(
            "确认",
            "清空所有输入框,并删除本机已加密保存的凭证?\n此操作不可撤销。",
        ):
            return

        self.aws_key_var.set("")
        self.aws_secret_var.set("")
        self.aws_token_var.set("")
        self.gd_key_var.set("")
        self.gd_secret_var.set("")
        self.ss_key_var.set("")
        self.ss_secret_var.set("")
        self.ss_endpoint_var.set("https://spaceship.dev/api")
        self.gl_url_var.set("")
        self.gl_project_var.set("")
        self.gl_project_search_var.set("")
        self.gl_project_choice_var.set("")
        self.gl_project_combo.configure(values=[])
        self.gitlab_projects_by_label = {}
        self.gl_token_var.set("")
        self.remember_var.set(False)

        for key in CREDENTIAL_KEYS:
            self.env.pop(key, None)
            os.environ.pop(key, None)
        self.stored = {}

        try:
            removed = delete_credentials(path=self.store_path)
        except OSError as exc:
            self._log("删除本机凭证文件失败: {0}".format(exc))
            removed = False
        self._log("已清空所有凭证字段{0}".format("并删除本机保存的凭证" if removed else ""))

    def _build_single_tab(self) -> None:
        provider_bar = ttk.Frame(self.single_frame, padding=10)
        provider_bar.pack(fill=tk.X)
        ttk.Label(provider_bar, text="DNS 服务商:").pack(side=tk.LEFT)
        self.dns_provider_var = tk.StringVar(value="godaddy")
        self.dns_provider_var.trace_add("write", lambda *_: self._refresh_dns_credentials())
        self._refresh_dns_credentials()
        ttk.Combobox(provider_bar, textvariable=self.dns_provider_var,
                     values=("godaddy", "spaceship"), state="readonly", width=16).pack(side=tk.LEFT, padx=8)
        form = ttk.Frame(self.single_frame, padding=10)
        form.pack(fill=tk.BOTH, expand=True)
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)
        form.rowconfigure(4, weight=1)

        basic = ttk.LabelFrame(form, text="基础信息", padding=10)
        basic.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6), pady=(0, 8))
        basic.columnconfigure(1, weight=1)

        ttk.Label(basic, text="域名").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.domain_var = tk.StringVar()
        domain_entry = ttk.Entry(basic, textvariable=self.domain_var, width=38)
        domain_entry.grid(row=0, column=1, sticky=tk.EW, pady=4, padx=(8, 0))
        self.domain_var.trace_add("write", lambda *_: self._update_domain_preview())

        ttk.Label(basic, text="GitLab 项目").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.project_var = tk.StringVar(value=str(self.defaults.get("gitlab_project") or ""))
        ttk.Entry(basic, textvariable=self.project_var, width=38).grid(row=1, column=1, sticky=tk.EW, pady=4, padx=(8, 0))
        self.project_var.trace_add("write", lambda *_: self._update_project_status())

        ttk.Label(basic, text="基准分支").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.base_branch_var = tk.StringVar(value=str(self.defaults.get("base_branch") or "dev"))
        ttk.Entry(basic, textvariable=self.base_branch_var, width=18).grid(row=2, column=1, sticky=tk.W, pady=4, padx=(8, 0))

        origin_box = ttk.LabelFrame(form, text="源站与 CloudFront", padding=10)
        origin_box.grid(row=0, column=1, sticky=tk.EW, padx=(6, 0), pady=(0, 8))
        origin_box.columnconfigure(1, weight=1)

        ttk.Label(origin_box, text="源站预设").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.origin_preset_var = tk.StringVar()
        self.origin_preset_combo = ttk.Combobox(origin_box, textvariable=self.origin_preset_var, values=[], state="readonly")
        self.origin_preset_combo.grid(row=0, column=1, sticky=tk.EW, pady=4, padx=(8, 0))
        self.origin_preset_combo.bind("<<ComboboxSelected>>", self._select_origin_preset)

        ttk.Label(origin_box, text="搜索预设").grid(row=1, column=0, sticky=tk.W, pady=4)
        search_frame = ttk.Frame(origin_box)
        search_frame.grid(row=1, column=1, sticky=tk.EW, pady=4, padx=(8, 0))
        self.origin_preset_search_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=self.origin_preset_search_var, width=22).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(search_frame, text="筛选", command=self._filter_origin_presets).pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(origin_box, text="源域名").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.origin_var = tk.StringVar(value=str(self.defaults.get("origin") or ""))
        ttk.Entry(origin_box, textvariable=self.origin_var, width=38).grid(row=2, column=1, sticky=tk.EW, pady=4, padx=(8, 0))

        ttk.Label(origin_box, text="源路径").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.origin_path_var = tk.StringVar(value=str(self.defaults.get("origin_path") or ""))
        ttk.Entry(origin_box, textvariable=self.origin_path_var, width=38).grid(row=3, column=1, sticky=tk.EW, pady=4, padx=(8, 0))

        ttk.Label(origin_box, text="源类型").grid(row=4, column=0, sticky=tk.W, pady=4)
        self.origin_type_var = tk.StringVar(value=str(self.defaults.get("origin_type") or "s3-website"))
        ttk.Combobox(origin_box, textvariable=self.origin_type_var, values=list(ORIGIN_TYPES), width=18, state="readonly").grid(
            row=4, column=1, sticky=tk.W, pady=4, padx=(8, 0)
        )

        preset_btn_frame = ttk.Frame(origin_box)
        preset_btn_frame.grid(row=5, column=1, sticky=tk.W, pady=(2, 0), padx=(8, 0))
        ttk.Button(preset_btn_frame, text="保存当前源站", command=self._save_origin_preset).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(preset_btn_frame, text="删除所选源站", command=self._delete_origin_preset).pack(side=tk.LEFT)

        preview = ttk.LabelFrame(form, text="命名预览", padding=10)
        preview.grid(row=1, column=0, sticky=tk.EW, padx=(0, 6), pady=(0, 8))
        preview.columnconfigure(1, weight=1)
        self.preview_service_var = tk.StringVar(value="-")
        self.preview_branch_var = tk.StringVar(value="-")
        self.preview_variable_var = tk.StringVar(value="-")
        self.preview_cert_var = tk.StringVar(value="-")
        for idx, (label, var) in enumerate(
            (
                ("访问域名", self.preview_service_var),
                ("GitLab 分支", self.preview_branch_var),
                ("CI/CD 变量", self.preview_variable_var),
                ("证书域名", self.preview_cert_var),
            )
        ):
            ttk.Label(preview, text=label).grid(row=idx, column=0, sticky=tk.W, pady=2)
            ttk.Label(preview, textvariable=var).grid(row=idx, column=1, sticky=tk.W, pady=2, padx=(8, 0))

        options = ttk.LabelFrame(form, text="执行选项", padding=10)
        options.grid(row=1, column=1, sticky=tk.EW, padx=(6, 0), pady=(0, 8))
        self.create_cf_var = tk.BooleanVar(value=bool(self.defaults.get("create_distribution", False)))
        self.spa_var = tk.BooleanVar(value=bool(self.defaults.get("spa", False)))
        self.wait_cert_var = tk.BooleanVar(value=bool(self.defaults.get("wait_certificate", False)))
        ttk.Checkbutton(options, text="创建 CloudFront 分配", variable=self.create_cf_var).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Checkbutton(options, text="等待证书签发", variable=self.wait_cert_var).grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Checkbutton(options, text="SPA 模式 (403/404 到 /index.html)", variable=self.spa_var).grid(row=2, column=0, sticky=tk.W, pady=3)

        advanced = ttk.LabelFrame(form, text="高级选项", padding=10)
        advanced.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        advanced.columnconfigure(1, weight=1)
        ttk.Label(advanced, text="Distribution ID").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.dist_id_var = tk.StringVar()
        ttk.Entry(advanced, textvariable=self.dist_id_var, width=30).grid(row=0, column=1, sticky=tk.W, pady=3, padx=(8, 16))
        self.skip_dns_var = tk.BooleanVar(value=bool(self.defaults.get("skip_dns", False)))
        self.skip_gitlab_var = tk.BooleanVar(value=bool(self.defaults.get("skip_gitlab", False)))
        self.skip_site_dns_var = tk.BooleanVar(value=bool(self.defaults.get("skip_site_dns", False)))
        ttk.Checkbutton(advanced, text="跳过所有 DNS 操作", variable=self.skip_dns_var).grid(row=0, column=2, sticky=tk.W, pady=3)
        ttk.Checkbutton(advanced, text="跳过 GitLab 操作", variable=self.skip_gitlab_var).grid(row=0, column=3, sticky=tk.W, pady=3, padx=(10, 0))
        ttk.Checkbutton(advanced, text="跳过 www 记录与 apex 转发", variable=self.skip_site_dns_var).grid(
            row=0, column=4, sticky=tk.W, pady=3, padx=(10, 0)
        )

        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        self.single_preview_btn = ttk.Button(btn_frame, text="预演", command=self._single_preview)
        self.single_preview_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.single_apply_btn = ttk.Button(btn_frame, text="执行", command=self._single_apply)
        self.single_apply_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="自检凭证", command=self._doctor_check).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="保存默认配置", command=self._save_defaults).pack(side=tk.LEFT, padx=6)

        result_box = ttk.LabelFrame(form, text="结果摘要", padding=8)
        result_box.grid(row=4, column=0, columnspan=2, sticky=tk.NSEW, pady=(0, 8))
        result_box.rowconfigure(0, weight=1)
        result_box.columnconfigure(0, weight=1)
        columns = ("step", "status", "detail")
        self.result_tree = ttk.Treeview(result_box, columns=columns, show="headings", height=4)
        self.result_tree.heading("step", text="步骤")
        self.result_tree.heading("status", text="状态")
        self.result_tree.heading("detail", text="详情")
        self.result_tree.column("step", width=150, stretch=False)
        self.result_tree.column("status", width=90, stretch=False)
        self.result_tree.column("detail", width=620)
        self.result_tree.grid(row=0, column=0, sticky=tk.NSEW)
        result_scroll = ttk.Scrollbar(result_box, orient=tk.VERTICAL, command=self.result_tree.yview)
        result_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.result_tree.configure(yscrollcommand=result_scroll.set)

        self._refresh_origin_presets()
        self._update_domain_preview()
        self._update_project_status()

    def _build_batch_tab(self) -> None:
        form = ttk.Frame(self.batch_frame, padding=10)
        form.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(form, text="批次文件:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.batch_path_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.batch_path_var, width=50).grid(row=row, column=1, sticky=tk.EW, pady=5)
        ttk.Button(form, text="浏览…", command=self._browse_batch).grid(row=row, column=2, padx=5, pady=5)

        row += 1
        columns = ("domain", "project", "origin")
        self.batch_tree = ttk.Treeview(form, columns=columns, show="headings", height=8)
        self.batch_tree.heading("domain", text="域名")
        self.batch_tree.heading("project", text="GitLab 项目")
        self.batch_tree.heading("origin", text="源")
        self.batch_tree.column("domain", width=200)
        self.batch_tree.column("project", width=250)
        self.batch_tree.column("origin", width=300)
        self.batch_tree.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=5)

        scrollbar = ttk.Scrollbar(form, orient=tk.VERTICAL, command=self.batch_tree.yview)
        scrollbar.grid(row=row, column=3, sticky=tk.NS, pady=5)
        self.batch_tree.configure(yscrollcommand=scrollbar.set)

        row += 1
        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="加载批次", command=self._load_batch).pack(side=tk.LEFT, padx=5)
        self.batch_preview_btn = ttk.Button(btn_frame, text="批量预演", command=self._batch_preview)
        self.batch_preview_btn.pack(side=tk.LEFT, padx=5)
        self.batch_apply_btn = ttk.Button(btn_frame, text="批量执行", command=self._batch_apply)
        self.batch_apply_btn.pack(side=tk.LEFT, padx=5)

        form.columnconfigure(1, weight=1)
        form.rowconfigure(1, weight=1)

    def _build_log_area(self, parent: ttk.Frame) -> None:
        log_frame = ttk.LabelFrame(parent, text="日志", padding=5)
        log_frame.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.grid(row=0, column=0, sticky=tk.EW)

        log_btns = ttk.Frame(log_frame)
        log_btns.grid(row=0, column=1, sticky=tk.NS, padx=(6, 0))
        ttk.Button(log_btns, text="清空", command=self._clear_log).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(log_btns, text="复制", command=self._copy_log).pack(fill=tk.X, pady=4)
        ttk.Button(log_btns, text="保存", command=self._save_log).pack(fill=tk.X, pady=4)

    def _build_log_page(self) -> None:
        frame = ttk.Frame(self.log_page_frame, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="完整日志在窗口底部持续显示。这里保留快捷操作,方便复盘和发给别人排查。").pack(
            anchor=tk.W, pady=(0, 10)
        )
        btns = ttk.Frame(frame)
        btns.pack(anchor=tk.W)
        ttk.Button(btns, text="清空日志", command=self._clear_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="复制日志", command=self._copy_log).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="保存日志", command=self._save_log).pack(side=tk.LEFT, padx=6)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _copy_log(self) -> None:
        text = self.log_text.get("1.0", tk.END).strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("日志已复制到剪贴板")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(self.log_text.get("1.0", tk.END), encoding="utf-8")
        self._log("日志已保存到 {0}".format(path))

    def _update_project_status(self) -> None:
        if not hasattr(self, "project_status_var"):
            return
        project = ""
        if hasattr(self, "project_var"):
            project = self.project_var.get().strip()
        if not project and hasattr(self, "gl_project_var"):
            project = self.gl_project_var.get().strip()
        self.project_status_var.set("项目: {0}".format(project or "未选择"))

    def _update_domain_preview(self) -> None:
        if not hasattr(self, "preview_service_var"):
            return
        domain = self.domain_var.get().strip()
        if not domain:
            values = ("-", "-", "-", "-")
        else:
            try:
                preview = _derive_domain_preview(domain)
                values = (
                    preview["service_domain"],
                    preview["branch"],
                    preview["variable_key"],
                    preview["certificate"],
                )
            except DomainKitError as exc:
                values = ("域名格式有误", str(exc), "-", "-")
        self.preview_service_var.set(values[0])
        self.preview_branch_var.set(values[1])
        self.preview_variable_var.set(values[2])
        self.preview_cert_var.set(values[3])

    def _clear_result_summary(self) -> None:
        if not hasattr(self, "result_tree"):
            return
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

    def _add_result_step(self, step: Any) -> None:
        if not hasattr(self, "result_tree"):
            return
        title, status, detail = _format_step_summary(step)
        self.result_tree.insert("", tk.END, values=(title, status, detail))

    def _mark_service_status(self, service: str, state: str, detail: str = "") -> None:
        mapping = {
            "aws": self.aws_status_var,
            "godaddy": self.godaddy_status_var,
            "spaceship": self.spaceship_status_var,
            "gitlab": self.gitlab_status_var,
        }
        var = mapping.get(service)
        if var is not None:
            var.set(_status_text(state, detail))

    def _update_status_from_doctor_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped.startswith(("ok  ", "err ")):
            return
        state = "ok" if stripped.startswith("ok") else "err"
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            return
        name = parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        if name in {"aws-credentials", "acm", "cloudfront"}:
            self._mark_service_status("aws", state, name)
        elif name == "godaddy":
            self._mark_service_status("godaddy", state, detail[:60])
        elif name == "spaceship":
            self._mark_service_status("spaceship", state, detail[:60])
        elif name in {"gitlab", "gitlab-target"}:
            self._mark_service_status("gitlab", state, detail[:60])

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _check_log_queue(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, message + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._check_log_queue)

    def _set_running(self, state: bool) -> None:
        """Only ever runs on the Tk main thread; workers go through _finish."""
        self.running = state
        btn_state = tk.DISABLED if state else tk.NORMAL
        if hasattr(self, "run_status_var"):
            self.run_status_var.set(_status_text("running" if state else "idle"))
        self.single_preview_btn.config(state=btn_state)
        self.single_apply_btn.config(state=btn_state)
        self.batch_preview_btn.config(state=btn_state)
        self.batch_apply_btn.config(state=btn_state)
        self.load_projects_btn.config(state=btn_state)

    def _finish(self) -> None:
        """Re-enable the buttons from a worker thread."""
        self.root.after(0, lambda: self._set_running(False))

    def _start(self, task) -> None:
        self._set_running(True)
        threading.Thread(target=task, daemon=True).start()

    def _browse_batch(self) -> None:
        path = filedialog.askopenfilename(
            title="选择批次文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if path:
            self.batch_path_var.set(path)

    def _load_batch(self) -> None:
        path_str = self.batch_path_var.get().strip()
        if not path_str:
            messagebox.showwarning("警告", "请先选择批次文件")
            return

        try:
            data = load_batch(Path(path_str))
            defaults = data.get("defaults", {})
            domains = data["domains"]

            for item in self.batch_tree.get_children():
                self.batch_tree.delete(item)

            for entry in domains:
                merged = {**defaults, **entry}
                domain = merged.get("domain", "")
                project = merged.get("gitlab_project", "")
                origin = merged.get("origin", merged.get("distribution_id", ""))
                self.batch_tree.insert("", tk.END, values=(domain, project, origin))

            self._log("已加载 {0} 个域名".format(len(domains)))
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))
            self._log("加载失败: {0}".format(exc))

    def _load_gitlab_projects(self) -> None:
        if self.running:
            return
        base_url = self.gl_url_var.get().strip().rstrip("/") or "https://gitlab.com"
        token = self.gl_token_var.get().strip()
        search = self.gl_project_search_var.get().strip()
        if not token:
            messagebox.showwarning("警告", "请先填写 GitLab Access Token")
            return

        def task():
            self._log("=== 加载 GitLab 项目 ===")
            try:
                client = GitLabClient.for_instance(base_url, token)
                user = client.get_current_user()
                username = user.get("username") or user.get("name") or "unknown"
                self._log("GitLab token 可用: {0}".format(username))
                projects = client.list_accessible_projects(search=search)
                self.root.after(0, lambda: self._apply_gitlab_projects(projects))
            except Exception as exc:  # noqa: BLE001 - turn API failures into actionable GUI logs
                self._log("加载 GitLab 项目失败: {0}".format(self._friendly_gitlab_error(exc)))
            finally:
                self._finish()

        self._start(task)

    def _friendly_gitlab_error(self, exc: BaseException) -> str:
        text = str(exc)
        if "401" in text:
            return "Token 无效,或 GitLab URL 与 token 所属实例不匹配 ({0})".format(text)
        if "403" in text:
            return "Token 有效但权限不足 ({0})".format(text)
        return text

    def _project_label(self, project: Dict[str, Any]) -> str:
        path = project.get("path_with_namespace") or project.get("name_with_namespace") or project.get("name") or "unknown"
        default_branch = project.get("default_branch") or "无默认分支"
        return "{0} (ID: {1}, default: {2})".format(path, project.get("id", ""), default_branch)

    def _apply_gitlab_projects(self, projects) -> None:
        self.gitlab_projects_by_label = {}
        labels = []
        for project in projects:
            if not isinstance(project, dict):
                continue
            label = self._project_label(project)
            labels.append(label)
            self.gitlab_projects_by_label[label] = project
        self.gl_project_combo.configure(values=labels)
        if not labels:
            self.gl_project_choice_var.set("")
            self._log("没有找到可访问项目;换搜索词或检查 token 权限")
            return
        self.gl_project_choice_var.set(labels[0])
        self._select_gitlab_project()
        self._log("已加载 {0} 个 GitLab 项目".format(len(labels)))

    def _select_gitlab_project(self, _event=None) -> None:
        project = self.gitlab_projects_by_label.get(self.gl_project_choice_var.get())
        if not project:
            return
        project_id = str(project.get("id") or "")
        if not project_id:
            return
        self.gl_project_var.set(project_id)
        if hasattr(self, "project_var"):
            self.project_var.set(project_id)
        self.env["GITLAB_PROJECT_ID"] = project_id
        os.environ["GITLAB_PROJECT_ID"] = project_id
        self._update_project_status()
        self._log("已选择 GitLab 项目: {0}".format(self.gl_project_choice_var.get()))

    def _refresh_dns_credentials(self) -> None:
        if self.dns_provider_var.get() == "spaceship":
            self.godaddy_credentials_frame.grid_remove()
            self.spaceship_credentials_frame.grid()
        else:
            self.spaceship_credentials_frame.grid_remove()
            self.godaddy_credentials_frame.grid()

    def _doctor_check(self) -> None:
        if self.running:
            return
        values = self._collect_credentials()
        apply_to_env(self.env, values)
        apply_to_env(os.environ, values)  # type: ignore[arg-type]

        def task():
            self._log("=== 开始凭证自检 ===")
            try:
                args = argparse.Namespace(
                    json=False,
                    project=self.project_var.get().strip() or self.gl_project_var.get().strip() or None,
                    profile=None,
                    prefix=None,
                    domain=self.domain_var.get().strip() or None,
                )
                out = io.StringIO()
                settings = Settings.load(env=self.env)
                settings.dns_provider = self.dns_provider_var.get()
                cmd_doctor(args, settings, out, self.env)
                for line in out.getvalue().splitlines():
                    self._log(line)
                    self.root.after(0, lambda value=line: self._update_status_from_doctor_line(value))
            except Exception as exc:  # noqa: BLE001 - surface every failure in the log
                self._log("自检失败: {0}".format(exc))
            finally:
                self._finish()

        self._start(task)

    def _single_preview(self) -> None:
        self._run_single(dry_run=True)

    def _single_apply(self) -> None:
        services = ["AWS ACM/CloudFront"]
        if not self.skip_dns_var.get() and not self.skip_site_dns_var.get():
            services.append(self.dns_provider_var.get() + " DNS")
        if not self.skip_gitlab_var.get():
            services.append("GitLab 分支和变量")
        text = "将真实变更以下服务:\n\n{0}\n\n确认继续?".format("\n".join("- " + item for item in services))
        if not messagebox.askyesno("确认执行", text):
            return
        self._run_single(dry_run=False)

    def _run_single(self, dry_run: bool) -> None:
        if self.running:
            return

        domain = self.domain_var.get().strip()
        if not domain:
            messagebox.showwarning("警告", "请输入域名")
            return
        if self.create_cf_var.get() and not self.origin_var.get().strip() and not self.dist_id_var.get().strip():
            messagebox.showwarning("警告", "创建 CloudFront 分配需要填写源域名,或者在高级选项里填写已有 Distribution ID")
            return

        def task():
            mode = "预演" if dry_run else "执行"
            self._log("=== {0}: {1} ===".format(mode, domain))
            self.root.after(0, self._clear_result_summary)
            try:
                settings = Settings.load(env=self.env)
                settings.dns_provider = self.dns_provider_var.get()
                project = self.project_var.get().strip()
                if project:
                    settings.gitlab_project = project
                base_branch = self.base_branch_var.get().strip()
                if base_branch:
                    settings.base_branch = base_branch

                onboarder = _make_onboarder(settings, self.env, need_write=not dry_run, log=self._log)

                origin = None
                origin_str = self.origin_var.get().strip()
                if origin_str:
                    origin_path = self.origin_path_var.get().strip()
                    origin = OriginSpec(
                        domain=origin_str,
                        type=self.origin_type_var.get(),
                        path=origin_path,
                    )

                options = DistributionOptions(spa=self.spa_var.get())

                kwargs: Dict[str, Any] = {
                    "dry_run": dry_run,
                    "wait_for_certificate": self.wait_cert_var.get(),
                    "certificate_timeout": DEFAULT_CERTIFICATE_TIMEOUT,
                    "create_distribution": self.create_cf_var.get(),
                    "skip_dns": self.skip_dns_var.get(),
                    "skip_site_dns": self.skip_site_dns_var.get(),
                    "skip_gitlab": self.skip_gitlab_var.get(),
                    "options": options,
                }
                if origin:
                    kwargs["origin"] = origin
                dist_id = self.dist_id_var.get().strip()
                if dist_id:
                    kwargs["distribution_id"] = dist_id

                report = onboarder.run(domain, **kwargs)
                self._log("分支: {0}, 变量: {1}".format(report.plan.branch, report.plan.variable_key))
                for step in report.steps:
                    self._log("  {0}: {1}".format(step.name, step.detail))
                    self.root.after(0, lambda value=step: self._add_result_step(value))
                self._log("[等待验证，可稍后重试]" if report.pending_validation else ("[失败]" if report.failed else "[完成]"))
            except Exception as exc:  # noqa: BLE001 - surface every failure in the log
                self._log("错误: {0}".format(exc))
            finally:
                self._finish()

        self._start(task)

    def _batch_preview(self) -> None:
        self._run_batch(dry_run=True)

    def _batch_apply(self) -> None:
        if not messagebox.askyesno("确认批量执行", "将对所有域名进行真实变更，确认继续？"):
            return
        self._run_batch(dry_run=False)

    def _run_batch(self, dry_run: bool) -> None:
        if self.running:
            return

        path_str = self.batch_path_var.get().strip()
        if not path_str:
            messagebox.showwarning("警告", "请先选择批次文件")
            return

        def task():
            mode = "预演" if dry_run else "执行"
            self._log("=== 批量{0} ===".format(mode))
            try:
                def factory(settings: Settings):
                    return _make_onboarder(settings, self.env, need_write=not dry_run, log=self._log)

                base_settings = Settings.load(env=self.env)
                result = run_batch(
                    Path(path_str),
                    onboarder_factory=factory,
                    dry_run=dry_run,
                    log=self._log,
                    base_settings=base_settings,
                )
                self._log("完成: {0} 成功, {1} 失败".format(len(result.reports), len(result.failures)))
                if result.failures:
                    self._log("失败域名:")
                    for domain, error in result.failures:
                        self._log("  {0}: {1}".format(domain, error))
            except Exception as exc:  # noqa: BLE001 - surface every failure in the log
                self._log("批量处理失败: {0}".format(exc))
            finally:
                self._finish()

        self._start(task)


def main() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(
            "GUI 启动失败: 当前 Python 缺少 Tcl/Tk 运行文件。请安装官方 Python 3.11/3.12,"
            "安装时保留 tcl/tk and IDLE 组件,或修改 domainkit.cmd 里的 PY 路径。原始错误: {0}".format(exc)
        ) from exc
    DomainKitGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
