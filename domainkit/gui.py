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
import copy
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
from typing import Any, Dict, List, Tuple

from .aws import DistributionOptions, OriginSpec, ORIGIN_TYPES
from .batch import load_batch, run_batch
from .cli import _make_onboarder, cmd_doctor
from .config import Settings
from .errors import DomainKitError
from .gitlab import GitLabClient, normalize_project_id
from .naming import branch_name, normalize_domain, service_domain, variable_key
from .vault import (
    CREDENTIAL_KEYS, PROFILE_KEYS, apply_to_env, describe, empty_profiles,
    load_credentials, load_profiles, profile_current, put_profile,
    remove_profile, save_profiles, store_path,
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
        self.profile_store = empty_profiles()
        self._profile_store_error = False
        self.gitlab_projects_by_label: Dict[str, Dict[str, Any]] = {}
        self._load_error = ""
        self._defaults_load_error = ""
        try:
            self.profile_store = load_profiles(path=self.store_path)
            self.stored = load_credentials(path=self.store_path)
        except (DomainKitError, OSError) as exc:
            self._load_error = str(exc)
            self._profile_store_error = True
        try:
            self.defaults = self._load_defaults()
            self.origin_presets = _load_origin_presets(self.defaults)
        except Exception as exc:  # noqa: BLE001 - surface defaults errors in the log
            self._defaults_load_error = str(exc)
        # Saved credentials win over inherited environment variables: the user
        # typed them here on purpose, and blanks must not overwrite them.
        for category, keys in PROFILE_KEYS.items():
            if self.profile_store['selected'][category] is not None:
                apply_to_env(self.env, self.stored, keys)

        self._build_ui()
        for category in PROFILE_KEYS:
            if self.profile_store['selected'][category] is not None:
                self._apply_profile_values(category, self._profile_values(category))
                profile = profile_current(self.profile_store, category)
                self._log_profile(category, profile['name'], profile['values'])
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
        info_text = "AWS / GoDaddy / Spaceship / GitLab"
        ttk.Label(info_frame, text=info_text, justify=tk.LEFT).pack()

        row = 1
        aws_frame = ttk.LabelFrame(form, text="AWS 凭证", padding=10)
        aws_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=5)
        row += 1

        aws_row = 0
        ttk.Label(aws_frame, text="Access Key ID:").grid(row=aws_row, column=0, sticky=tk.W, pady=3)
        self.aws_key_var = tk.StringVar(value=self.env.get("AWS_ACCESS_KEY_ID", ""))
        self.aws_key_entry = ttk.Entry(aws_frame, textvariable=self.aws_key_var, width=50, show='*')
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
        self.gl_project_entry = ttk.Entry(gitlab_frame, textvariable=self.gl_project_var, width=50)
        self.gl_project_entry.grid(
            row=gl_row, column=1, sticky=tk.EW, pady=3, padx=5, columnspan=2
        )
        self.gl_project_entry.bind('<Return>', self._remember_manual_gitlab_project)
        self.gl_project_entry.bind('<FocusOut>', self._remember_manual_gitlab_project)
        gitlab_frame.columnconfigure(1, weight=1)
        self._build_profile_controls({"aws": aws_frame, "godaddy": godaddy_frame,
                                      "spaceship": spaceship_frame, "gitlab": gitlab_frame})

        ttk.Label(form, text="凭证仅在本机保存；每类账号请使用各自区域的操作按钮。").grid(
            row=row, column=0, columnspan=3, pady=10
        )

        form.columnconfigure(0, weight=0)
        form.columnconfigure(1, weight=1)

    def _profile_vars(self):
        return dict(zip(CREDENTIAL_KEYS, (self.aws_key_var, self.aws_secret_var, self.aws_token_var,
            self.gd_key_var, self.gd_secret_var, self.ss_key_var, self.ss_secret_var,
            self.ss_endpoint_var, self.gl_url_var, self.gl_project_var, self.gl_token_var)))

    def _build_profile_controls(self, frames):
        self.profile_store_error = self._profile_store_error
        self.profile_choices, self.profile_combos, self.profile_baselines = {}, {}, {}
        self.profile_drafts = set()
        for category, frame in frames.items():
            bar = ttk.Frame(frame)
            bar.grid(row=frame.grid_size()[1], column=0, columnspan=3, sticky=tk.EW, pady=8)
            bar.columnconfigure(1, weight=1)
            ttk.Label(bar, text='已保存凭证').grid(row=0, column=0, padx=4)
            choice = tk.StringVar()
            combo = ttk.Combobox(bar, textvariable=choice)
            combo.grid(row=0, column=1, sticky=tk.EW, columnspan=5)
            self.profile_choices[category], self.profile_combos[category] = choice, combo
            combo.bind('<KeyRelease>', lambda event, c=category: self._filter_profiles(c))
            combo.bind('<<ComboboxSelected>>', lambda event, c=category: self._switch_profile(c))
            commands = [('新增', lambda c=category: self._new_profile(c)),
                        ('保存', lambda c=category: self._save_profile(c)),
                        ('另存为', lambda c=category: self._save_profile(c, as_new=True)),
                        ('重命名', lambda c=category: self._rename_profile(c)),
                        ('删除', lambda c=category: self._delete_profile(c)),
                        ('测试连接', lambda c=category: self._test_profile(c))]
            for index, (label, command) in enumerate(commands):
                ttk.Button(bar, text=label, command=command).grid(row=1 + index // 3,
                    column=(index % 3) * 2, columnspan=2, sticky=tk.EW, pady=2)
            self.profile_baselines[category] = self._profile_values(category)
            self._refresh_profiles(category)

    def _profile_values(self, category):
        variables = self._profile_vars()
        return {k: variables[k].get().strip() for k in PROFILE_KEYS[category]}

    def _refresh_profiles(self, category):
        if category in self.profile_drafts:
            self.profile_choices[category].set('')
            self.profile_combos[category].configure(values=[p['name'] for p in self.profile_store['profiles'][category]])
            return
        current = profile_current(self.profile_store, category)
        self.profile_choices[category].set(current['name'] if current else '')
        self.profile_combos[category].configure(values=[p['name'] for p in self.profile_store['profiles'][category]])

    def _filter_profiles(self, category):
        query = self.profile_choices[category].get().casefold()
        self.profile_combos[category].configure(values=[p['name'] for p in
            self.profile_store['profiles'][category] if query in p['name'].casefold()])

    def _persist_profiles(self, candidate):
        if self.profile_store_error:
            messagebox.showerror('保存失败', '原凭证文件读取失败，请先修复文件后重启，避免覆盖原数据。')
            return False
        try:
            save_profiles(candidate, path=self.store_path)
        except (DomainKitError, OSError) as exc:
            messagebox.showerror('保存失败', str(exc))
            return False
        self.profile_store = candidate
        return True

    def _log_profile(self, category, name, values):
        summary = describe(values, PROFILE_KEYS[category])
        shown = ', '.join('{0}={1}'.format(key, value) for key, value in summary.items())
        self._log('凭证 {0} / {1}: {2}'.format(category, name, shown))

    def _apply_profile_values(self, category, values):
        values = dict(values)
        if category == 'spaceship':
            values.setdefault('SPACESHIP_API_ENDPOINT', 'https://spaceship.dev/api')
        for key, var in self._profile_vars().items():
            if key in PROFILE_KEYS[category]:
                var.set(values.get(key, ''))
        apply_to_env(self.env, values, PROFILE_KEYS[category])
        apply_to_env(os.environ, values, PROFILE_KEYS[category])
        if category == 'gitlab':
            self.gitlab_projects_by_label = {}
            self.gl_project_choice_var.set('')
            self.gl_project_search_var.set('')
            self.gl_project_combo.configure(values=[])
            if hasattr(self, 'project_var'):
                self.project_var.set(values.get('GITLAB_PROJECT_ID', ''))
        self._mark_service_status(category, 'unknown')
        self.profile_baselines[category] = self._profile_values(category)
        self._refresh_profiles(category)

    def _confirm_profile_edits(self, category):
        if self._profile_values(category) == self.profile_baselines[category]:
            return True
        answer = messagebox.askyesnocancel('未保存的修改', '是否保存后切换？\n是：保存并切换；否：放弃修改；取消：保留当前输入。')
        if answer is True:
            return self._save_profile(category)
        if answer is False:
            current = profile_current(self.profile_store, category)
            self.profile_drafts.discard(category)
            self._apply_profile_values(category, current['values'] if current else
                                       ({'SPACESHIP_API_ENDPOINT': 'https://spaceship.dev/api'}
                                        if category == 'spaceship' else {}))
            return True
        return False

    def _save_profile(self, category, as_new=False):
        if self.running:
            return False
        current = profile_current(self.profile_store, category)
        is_new = as_new or category in self.profile_drafts
        name = current['name'] if current and not is_new else simpledialog.askstring('保存凭证', '凭证名称：', parent=self.root)
        if name is None:
            return False
        candidate = copy.deepcopy(self.profile_store)
        values = self._profile_values(category)
        try:
            put_profile(candidate, category, name, values,
                        profile_id=current['id'] if current and not is_new else None)
        except (DomainKitError, OSError) as exc:
            messagebox.showerror('保存失败', str(exc))
            return False
        if not self._persist_profiles(candidate):
            return False
        self.profile_drafts.discard(category)
        self._apply_profile_values(category, values)
        self._log('已保存凭证：{0} / {1}'.format(category, name))
        self._log_profile(category, name, values)
        return True

    def _switch_profile(self, category):
        selected = self.profile_choices[category].get()
        target = next((p for p in self.profile_store['profiles'][category] if p['name'] == selected), None)
        if self.running or target is None or not self._confirm_profile_edits(category):
            self._refresh_profiles(category)
            return
        target = next((p for p in self.profile_store['profiles'][category] if p['id'] == target['id']), None)
        if target is None:
            self._refresh_profiles(category)
            return
        candidate = copy.deepcopy(self.profile_store)
        candidate['selected'][category] = target['id']
        if self._persist_profiles(candidate):
            self.profile_drafts.discard(category)
            self._apply_profile_values(category, target['values'])
            self._log('已切换凭证：{0} / {1}'.format(category, target['name']))
            self._log_profile(category, target['name'], target['values'])
        else:
            self._refresh_profiles(category)

    def _new_profile(self, category):
        if self.running or not self._confirm_profile_edits(category):
            return
        self.profile_drafts.add(category)
        self._apply_profile_values(category, {'SPACESHIP_API_ENDPOINT': 'https://spaceship.dev/api'}
                                   if category == 'spaceship' else {})

    def _rename_profile(self, category):
        if self.running or category in self.profile_drafts or not self._confirm_profile_edits(category):
            return
        current = profile_current(self.profile_store, category)
        if not current:
            return
        name = simpledialog.askstring('重命名', '凭证名称：', initialvalue=current['name'], parent=self.root)
        if name is None:
            return
        candidate = copy.deepcopy(self.profile_store)
        try:
            put_profile(candidate, category, name, current['values'], profile_id=current['id'])
        except DomainKitError as exc:
            messagebox.showerror('重命名失败', str(exc))
            return
        if self._persist_profiles(candidate):
            self.profile_baselines[category] = self._profile_values(category)
            self._refresh_profiles(category)

    def _delete_profile(self, category):
        if (self.running or category in self.profile_drafts or not self._confirm_profile_edits(category)):
            return
        current = profile_current(self.profile_store, category)
        if (not current or
                not messagebox.askyesno('删除凭证', '仅删除本机凭证“{0}”？'.format(current['name']))):
            return
        candidate = copy.deepcopy(self.profile_store)
        remove_profile(candidate, category, current['id'])
        if self._persist_profiles(candidate):
            self._apply_profile_values(category, {})

    def _test_profile(self, category):
        if self.running:
            return
        profile = profile_current(self.profile_store, category)
        profile_name = profile['name'] if profile else '新凭证（未保存）'
        self._log('开始只读连接测试：{0} / {1}'.format(category, profile_name))
        self._log_profile(category, profile_name, self._profile_values(category))
        environment = dict(self.env)
        apply_to_env(environment, self._profile_values(category), PROFILE_KEYS[category])
        domain = self.domain_var.get().strip()
        if category == 'spaceship' and not domain:
            domain = simpledialog.askstring('测试 Spaceship', '用于只读检查的 DNS 根域名：', parent=self.root)
            if not domain:
                return
        def task():
            try:
                from .aws import AcmClient, CloudFrontClient
                from .sigv4 import Credentials
                from .godaddy import GoDaddyClient
                from .spaceship import SpaceshipClient
                if category == 'aws':
                    credentials = Credentials.from_env(environment)
                    AcmClient(credentials).list_certificates()
                    CloudFrontClient(credentials).list_distributions()
                elif category == 'godaddy':
                    GoDaddyClient.from_env(environment).list_domains()
                elif category == 'spaceship':
                    SpaceshipClient.from_env(environment).get_records(domain)
                else:
                    GitLabClient.for_instance(environment.get('GITLAB_URL') or 'https://gitlab.com',
                                              environment.get('GITLAB_TOKEN', '')).get_current_user()
                self._log(category + '：只读连接检查通过')
                self.root.after(0, lambda: self._mark_service_status(category, 'ok'))
            except Exception as exc:
                detail = str(exc)
                for key in PROFILE_KEYS[category]:
                    value = environment.get(key, '')
                    if value and ('KEY' in key or 'TOKEN' in key or 'SECRET' in key):
                        detail = detail.replace(value, '********')
                self._log(category + '：检查失败：' + detail)
                self.root.after(0, lambda: self._mark_service_status(category, 'err'))
            finally:
                self._finish()
        self._start(task)

    def _toggle_show(self, entry: ttk.Entry, var: tk.BooleanVar) -> None:
        """Toggle password visibility."""
        if var.get():
            entry.config(show="")
        else:
            entry.config(show="*")

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
        if state:
            self._locked_widgets = []
            def lock(parent):
                for widget in parent.winfo_children():
                    if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Button, ttk.Checkbutton)):
                        self._locked_widgets.append((widget, str(widget.cget('state'))))
                        widget.configure(state='disabled')
                    lock(widget)
            lock(self.credentials_frame)
        else:
            for widget, previous in getattr(self, '_locked_widgets', []):
                widget.configure(state=previous)
            self._locked_widgets = []
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
                self.root.after(0, lambda: self._apply_gitlab_projects(projects)
                    if (self.gl_url_var.get().strip().rstrip('/') or 'https://gitlab.com',
                        self.gl_token_var.get().strip()) == (base_url, token) else None)
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
        previous = self.gl_project_var.get().strip()
        matched = next((label for label, project in self.gitlab_projects_by_label.items()
                        if previous in (str(project.get('id')), project.get('path_with_namespace'))), None)
        if previous and not matched:
            self.gl_project_choice_var.set('')
            self._log('上次项目不在当前列表中，请手动选择。')
            return
        self.gl_project_choice_var.set(matched or labels[0])
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
        if hasattr(self, 'profile_store'):
            current = profile_current(self.profile_store, 'gitlab')
            if (current and 'gitlab' not in self.profile_drafts and
                    all(self._profile_values('gitlab').get(k, '') == current['values'].get(k, '')
                        for k in ('GITLAB_URL', 'GITLAB_TOKEN'))):
                candidate = copy.deepcopy(self.profile_store)
                profile_current(candidate, 'gitlab')['values']['GITLAB_PROJECT_ID'] = project_id
                if self._persist_profiles(candidate):
                    self.profile_baselines['gitlab'] = self._profile_values('gitlab')
        self._update_project_status()
        self._log("已选择 GitLab 项目: {0}".format(self.gl_project_choice_var.get()))

    def _remember_manual_gitlab_project(self, _event=None) -> None:
        project_id = normalize_project_id(self.gl_project_var.get())
        self.gl_project_var.set(project_id)
        self.project_var.set(project_id)
        apply_to_env(self.env, {'GITLAB_PROJECT_ID': project_id}, ('GITLAB_PROJECT_ID',))
        apply_to_env(os.environ, {'GITLAB_PROJECT_ID': project_id}, ('GITLAB_PROJECT_ID',))
        if self.running or 'gitlab' in self.profile_drafts:
            return
        current = profile_current(self.profile_store, 'gitlab')
        if (not current or any(self._profile_values('gitlab').get(key, '') != current['values'].get(key, '')
                               for key in ('GITLAB_URL', 'GITLAB_TOKEN')) or
                current['values'].get('GITLAB_PROJECT_ID', '') == project_id):
            return
        candidate = copy.deepcopy(self.profile_store)
        profile = profile_current(candidate, 'gitlab')
        if project_id:
            profile['values']['GITLAB_PROJECT_ID'] = project_id
        else:
            profile['values'].pop('GITLAB_PROJECT_ID', None)
        if self._persist_profiles(candidate):
            self.profile_baselines['gitlab']['GITLAB_PROJECT_ID'] = project_id

    def _refresh_dns_credentials(self) -> None:
        # Both providers keep independent profiles, even when one is not active.
        self.godaddy_credentials_frame.grid()
        self.spaceship_credentials_frame.grid()

    def _doctor_check(self) -> None:
        if self.running:
            return
        environment = dict(self.env)
        values = self._collect_credentials()
        apply_to_env(environment, values)
        apply_to_env(os.environ, values)
        project = self.project_var.get().strip() or self.gl_project_var.get().strip() or None
        domain = self.domain_var.get().strip() or None
        dns_provider = self.dns_provider_var.get()

        def task():
            self._log("=== 开始凭证自检 ===")
            try:
                args = argparse.Namespace(
                    json=False,
                    project=project,
                    profile=None,
                    prefix=None,
                    domain=domain,
                )
                out = io.StringIO()
                settings = Settings.load(env=environment)
                settings.dns_provider = dns_provider
                cmd_doctor(args, settings, out, environment)
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

        environment = dict(self.env)
        apply_to_env(environment, self._collect_credentials())
        selected_provider = self.dns_provider_var.get()
        selected_project = self.project_var.get().strip()
        selected_base_branch = self.base_branch_var.get().strip()
        create_distribution = self.create_cf_var.get()
        wait_for_certificate = self.wait_cert_var.get()
        spa = self.spa_var.get()
        skip_dns = self.skip_dns_var.get()
        skip_site_dns = self.skip_site_dns_var.get()
        skip_gitlab = self.skip_gitlab_var.get()
        origin_domain = self.origin_var.get().strip()
        origin_type = self.origin_type_var.get()
        origin_path = self.origin_path_var.get().strip()
        distribution_id = self.dist_id_var.get().strip()
        def task():
            mode = "预演" if dry_run else "执行"
            self._log("=== {0}: {1} ===".format(mode, domain))
            self.root.after(0, self._clear_result_summary)
            try:
                settings = Settings.load(env=environment)
                settings.dns_provider = selected_provider
                project = selected_project
                if project:
                    settings.gitlab_project = project
                base_branch = selected_base_branch
                if base_branch:
                    settings.base_branch = base_branch

                onboarder = _make_onboarder(settings, environment, need_write=not dry_run, log=self._log)

                origin = None
                if origin_domain:
                    origin = OriginSpec(
                        domain=origin_domain,
                        type=origin_type,
                        path=origin_path,
                    )

                options = DistributionOptions(spa=spa)

                kwargs: Dict[str, Any] = {
                    "dry_run": dry_run,
                    "wait_for_certificate": wait_for_certificate,
                    "certificate_timeout": DEFAULT_CERTIFICATE_TIMEOUT,
                    "create_distribution": create_distribution,
                    "skip_dns": skip_dns,
                    "skip_site_dns": skip_site_dns,
                    "skip_gitlab": skip_gitlab,
                    "options": options,
                }
                if origin:
                    kwargs["origin"] = origin
                if distribution_id:
                    kwargs["distribution_id"] = distribution_id

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

        environment = dict(self.env)
        apply_to_env(environment, self._collect_credentials())
        selected_provider = self.dns_provider_var.get()
        selected_project = self.project_var.get().strip()
        selected_base_branch = self.base_branch_var.get().strip()
        def task():
            mode = "预演" if dry_run else "执行"
            self._log("=== 批量{0} ===".format(mode))
            try:
                def factory(settings: Settings):
                    return _make_onboarder(settings, environment, need_write=not dry_run, log=self._log)

                base_settings = Settings.load(env=environment)
                base_settings.dns_provider = selected_provider
                if selected_project:
                    base_settings.gitlab_project = selected_project
                if selected_base_branch:
                    base_settings.base_branch = selected_base_branch
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
