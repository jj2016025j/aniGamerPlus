"""內建多執行緒 Web 後端引擎（提供桌面 PyWebView 與手機瀏覽器雙端共用 API、串流下載、PIN＋裝置記憶＋生物辨識驗證）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookies
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .audio_tools import inspect_audio, remux_audio_for_quicktime, scan_directory_audio
from .backup_manager import export_backup, import_backup, inspect_backup_file
from .config_store import (
    SORT_LABELS,
    WATCHLIST_SORT_OPTIONS,
    ConfigStore,
    ManagerConfig,
    extract_sn_from_input,
    generate_default_pin,
    validate_hhmm,
    validate_resolution,
    validate_total_episodes,
)
from .cookie_manager import (
    capture_cookie_via_playwright,
    check_vip_upgrade_eligibility,
    inspect_cookie_file,
    read_clipboard,
    validate_and_sanitize_cookie,
    verify_cookie_online,
    write_cookie_file,
)
from .downloader import AniGamerDownloader
from .env_detector import EnvironmentInfo, detect_environment
from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes, format_episode_ranges
from .history_tracker import HistoryTracker, parse_resolution_int
from .metadata_fetcher import enrich_anime_metadata, resolve_candidate_video_sn, search_anigamer
from .notifier import (
    DigestEntry,
    DownloadedRecord,
    Notifier,
    format_anime_added_notice,
    format_check_digest,
    format_download_report,
)
from .scheduler_manager import get_os_schedule_status, install_os_schedule, remove_os_schedule
from .watchlist_store import AnimeWatchItem, WatchlistStore, format_views, now_timestamp, sort_watchlist

DEVICE_COOKIE_NAME = "anm_device_token"
DEVICE_COOKIE_MAX_AGE = 90 * 24 * 3600  # 記住設備 90 天（變更 PIN 碼後舊憑證立即失效）


def detect_network_urls(port: int) -> dict[str, Any]:
    """偵測本機內網 LAN IP 與 Tailscale IP，方便手機掃碼連線。"""
    lan_ip = ""
    tailscale_ip = ""
    all_ips: list[str] = []

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            if ip.startswith("100."):
                tailscale_ip = ip
            else:
                lan_ip = ip
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip.startswith("127.") or ip in all_ips:
                continue
            all_ips.append(ip)
            if ip.startswith("100.") and not tailscale_ip:
                tailscale_ip = ip
            elif not lan_ip and (ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.")):
                lan_ip = ip
    except Exception:
        pass

    primary_ip = lan_ip or tailscale_ip or (all_ips[0] if all_ips else "127.0.0.1")
    return {
        "local_url": f"http://127.0.0.1:{port}",
        "lan_ip": lan_ip,
        "lan_url": f"http://{lan_ip}:{port}" if lan_ip else "",
        "tailscale_ip": tailscale_ip,
        "tailscale_url": f"http://{tailscale_ip}:{port}" if tailscale_ip else "",
        "primary_remote_url": f"http://{primary_ip}:{port}",
    }


def _sign_device_token(secret: str, pin_version: int, issued_ts: int | None = None) -> str:
    ts = issued_ts or int(time.time())
    nonce = secrets.token_hex(8)
    payload = f"v{pin_version}:{ts}:{nonce}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def _verify_device_token(token: str, secret: str, expected_pin_version: int) -> bool:
    if not token or not secret:
        return False
    parts = token.split(":")
    if len(parts) != 4:
        return False
    ver_str, ts_str, nonce, sig = parts
    if ver_str != f"v{expected_pin_version}" or not ts_str.isdigit():
        return False
    issued_ts = int(ts_str)
    if (time.time() - issued_ts) > DEVICE_COOKIE_MAX_AGE:
        return False
    payload = f"{ver_str}:{ts_str}:{nonce}"
    expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected_sig)


class AniManagerBackend:
    """整合所有業務模組、背景工作執行緒、日誌緩衝與遠端安全驗證的單例服務狀態。"""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.explicit_root = root_dir
        self._lock = threading.RLock()
        self._task_lock = threading.Lock()
        self.logs: list[dict[str, str]] = []
        self.task_state: dict[str, Any] = {
            "running": False,
            "type": "",
            "title": "",
            "progress": 0,
            "total": 0,
            "cancel_requested": False,
        }
        self._active_downloader: AniGamerDownloader | None = None
        self.last_snapshots: list[dict[str, Any]] = []
        self.remote_ready_files: list[dict[str, Any]] = []  # 供遠端瀏覽器自動下載到本地的已完成檔案清單
        self._webauthn_challenges: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._tray_thread: threading.Thread | None = None

        self.reload_state()

    def log(self, level: str, message: str) -> None:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,  # "info" | "ok" | "warn" | "err"
            "message": message,
        }
        with self._lock:
            self.logs.append(entry)
            if len(self.logs) > 250:
                self.logs = self.logs[-250:]

    def _resolve_watchlist_path(self, root_dir: Path, cfg: ManagerConfig) -> Path:
        p = Path(cfg.watchlist_file).expanduser()
        return p if p.is_absolute() else (root_dir / p).resolve()

    def reload_state(self) -> None:
        with self._lock:
            base_env = detect_environment(self.explicit_root)
            self.config_store = ConfigStore(
                base_env.root_dir / "manager_config.json",
                sample_path=base_env.root_dir / "manager_config-sample.json",
            )
            self.config, legacy_wl = self.config_store.load_with_legacy_watchlist()
            self.watchlist_store = WatchlistStore(
                self._resolve_watchlist_path(base_env.root_dir, self.config),
                sample_path=base_env.root_dir / "watchlist-sample.json",
            )
            self.watchlist, _ = self.watchlist_store.load(
                default_resolution=self.config.default_resolution,
                legacy_items=legacy_wl,
            )
            self.env: EnvironmentInfo = detect_environment(
                self.explicit_root,
                self.config.bangumi_dir,
                self.config.anigamer_dir,
            )
            self.tracker = HistoryTracker(
                history_path=self.env.root_dir / "download_history.json",
                db_path=self.env.db_path,
                bangumi_dir=self.env.bangumi_dir,
            )
            self.notifier = Notifier(self.config.notifications)

    def start_background_loop(self) -> None:
        """若設定為系統匣常駐定時模式，背景每分鐘檢查是否到達輪詢間隔。"""
        if self._tray_thread and self._tray_thread.is_alive():
            return

        def _worker() -> None:
            last_run = time.monotonic()
            while not self._stop_event.wait(15.0):
                if self.config.background_mode != "tray":
                    last_run = time.monotonic()
                    continue
                interval_sec = max(600, self.config.tray_interval_minutes * 60)
                if (time.monotonic() - last_run) >= interval_sec:
                    last_run = time.monotonic()
                    if not self.task_state["running"]:
                        self.log("info", "背景定時器觸發：開始自動檢查並下載追番更新...")
                        self.start_check_or_download_task(download=True, remote_origin=False)

        self._tray_thread = threading.Thread(target=_worker, daemon=True, name="AniManagerTrayLoop")
        self._tray_thread.start()

    def stop_background_loop(self) -> None:
        self._stop_event.set()

    def get_sorted_watchlist(self) -> list[AnimeWatchItem]:
        missing_map = {s["sn"]: s.get("missing_count", 0) for s in self.last_snapshots}
        return sort_watchlist(
            self.watchlist,
            sort_by=self.config.watchlist_sort_by,
            desc=self.config.watchlist_sort_desc,
            missing_counts=missing_map,
        )

    def get_full_state(self, is_local: bool, is_authenticated: bool) -> dict[str, Any]:
        with self._lock:
            c_info = inspect_cookie_file(self.env.cookie_path) if self.env.has_cookie else None
            cookie_summary = {
                "has_cookie": self.env.has_cookie,
                "valid": bool(c_info and c_info.valid),
                "tier": c_info.tier if c_info else "none",
                "account": c_info.baha_id_masked if c_info else "未設定",
                "reason": c_info.reason if c_info else "尚未設定 cookie.txt（預設免費 720P + 廣告）",
            }
            snap_map = {s["sn"]: s for s in self.last_snapshots}
            wl_rows = []
            for idx, item in enumerate(self.get_sorted_watchlist(), 1):
                row = asdict(item)
                row["index"] = idx
                row["views_formatted"] = format_views(item.views)
                row["scan"] = snap_map.get(item.sn)
                wl_rows.append(row)

            safe_cfg = asdict(self.config)
            safe_cfg.pop("remote_auth_secret", None)

            return {
                "version": "2.0.0",
                "auth": {
                    "enabled": self.config.remote_auth_enabled,
                    "is_local": is_local,
                    "authenticated": is_authenticated,
                    "has_webauthn": len(self.config.remote_webauthn_credentials) > 0,
                    "webauthn_count": len(self.config.remote_webauthn_credentials),
                },
                "env": {
                    "os_name": self.env.os_name,
                    "raw_os": self.env.raw_os,
                    "is_frozen": self.env.is_frozen,
                    "has_core": self.env.anigamer_script is not None,
                    "core_name": self.env.anigamer_script.name if self.env.anigamer_script else "",
                    "has_ffmpeg": self.env.has_ffmpeg,
                    "bangumi_dir": str(self.env.bangumi_dir),
                    "root_dir": str(self.env.root_dir),
                },
                "network": detect_network_urls(self.config.web_port),
                "cookie": cookie_summary,
                "config": safe_cfg,
                "sort_options": [
                    {"key": k, "label": lbl, "default_desc": d_desc}
                    for k, lbl, d_desc in WATCHLIST_SORT_OPTIONS
                ],
                "scheduler": get_os_schedule_status(self.env),
                "watchlist": wl_rows,
                "task": dict(self.task_state),
                "logs": list(self.logs[-100:]),
                "remote_ready_files": list(self.remote_ready_files),
            }

    def start_check_or_download_task(
        self,
        download: bool = False,
        single_sn: str | None = None,
        force_single: bool = False,
        push_notify: bool = False,
        remote_origin: bool = False,
        transfer_token: str = "",
    ) -> tuple[bool, str]:
        with self._task_lock:
            if self.task_state["running"]:
                return False, "目前已有任務正在執行中，請稍候或先中止目前任務。"
            self.task_state.update(
                {
                    "running": True,
                    "type": "download" if download else "check",
                    "title": "強制單集下載" if force_single else ("檢查並下載更新" if download else "掃描追番更新"),
                    "progress": 0,
                    "total": 0,
                    "cancel_requested": False,
                }
            )

        t = threading.Thread(
            target=self._run_check_or_download_worker,
            args=(download, single_sn, force_single, push_notify, remote_origin, transfer_token),
            daemon=True,
        )
        t.start()
        return True, "任務已啟動"

    def cancel_active_task(self) -> tuple[bool, str]:
        if not self.task_state["running"]:
            return False, "目前沒有執行中的任務。"
        self.task_state["cancel_requested"] = True
        self.log("warn", "收到使用者中斷指令，正在安全終止子行程...")
        dl = self._active_downloader
        if dl is not None:
            dl.request_abort()
        return True, "已送出中斷訊號"

    def _run_check_or_download_worker(
        self,
        download: bool,
        single_sn: str | None,
        force_single: bool,
        push_notify: bool,
        remote_origin: bool,
        transfer_token: str,
    ) -> None:
        dl = AniGamerDownloader(self.env)
        self._active_downloader = dl
        try:
            if force_single and single_sn:
                self.task_state["total"] = 1
                ep = EpisodeInfo(sn=single_sn, episode_label=f"sn={single_sn}", anime_title=f"SN-{single_sn}")
                self.log("info", f"強制單集下載：sn={single_sn}（目標 {self.config.default_resolution}P）")
                res = dl.download_episode(ep, resolution=self.config.default_resolution)
                self.task_state["progress"] = 1
                if res.interrupted:
                    self.log("warn", f"已中斷單集下載 (sn={single_sn})")
                elif res.success:
                    self.log("ok", f"單集下載完成：sn={single_sn}")
                else:
                    self.log("err", f"下載異常：{res.message}")
                return

            if single_sn:
                items = [
                    next(
                        (w for w in self.watchlist if w.sn == single_sn),
                        AnimeWatchItem(name=f"SN-{single_sn}", sn=single_sn, resolution=self.config.default_resolution),
                    )
                ]
            else:
                items = [w for w in self.get_sorted_watchlist() if w.enabled]

            if not items:
                self.log("warn", "目前沒有追蹤中的作品，請先在「追番清單」開啟追蹤或新增作品。")
                return

            self.task_state["total"] = len(items)
            new_snaps: list[dict[str, Any]] = []
            diff_objects: list[tuple[AnimeWatchItem, str, Any]] = []
            changed_wl = False

            for idx, item in enumerate(items, 1):
                if self.task_state["cancel_requested"]:
                    self.log("warn", "已手動中斷掃描。")
                    return
                self.task_state["progress"] = idx
                self.log("info", f"掃描 [{idx}/{len(items)}]：《{item.name}》(SN: {item.sn})")
                try:
                    title, online_eps = fetch_anime_episodes(
                        base_sn=item.sn,
                        cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                        fallback_title=item.name,
                    )
                    if item.online_episodes != len(online_eps):
                        item.online_episodes = len(online_eps)
                        changed_wl = True
                    if item.total_episodes is not None and item.total_episodes < len(online_eps):
                        item.total_episodes = None
                        changed_wl = True

                    diff = self.tracker.compute_diff(
                        online_episodes=online_eps,
                        target_resolution=item.resolution,
                        upgrade_lower_resolution=self.config.upgrade_lower_resolution,
                        anime_name_hint=item.name,
                        dedup_mode=self.config.dedup_mode,
                    )
                    miss_cnt = len(diff.missing_episodes)
                    up_cnt = len(diff.upgradable_episodes)
                    range_txt = format_episode_ranges(diff.missing_episodes, compact=True) if miss_cnt else ""
                    new_snaps.append(
                        {
                            "sn": item.sn,
                            "title": title,
                            "online_count": len(online_eps),
                            "local_count": diff.total_local_count,
                            "missing_count": miss_cnt,
                            "missing_range": range_txt,
                            "upgradable_count": up_cnt,
                            "error": "",
                        }
                    )
                    diff_objects.append((item, title, diff))
                    if miss_cnt:
                        self.log("warn", f"  → 發現 {miss_cnt} 集待下載 ({range_txt})")
                    else:
                        self.log("ok", f"  → 已下載 ({diff.total_local_count}/{len(online_eps)} 集)")
                except EpisodeFetchError as exc:
                    new_snaps.append({"sn": item.sn, "title": item.name, "error": str(exc), "missing_count": 0})
                    self.log("err", f"  → 解析失敗：{exc}")

            if changed_wl:
                self.watchlist_store.save(self.watchlist)
            if not single_sn:
                self.last_snapshots = new_snaps

            if not download:
                total_miss = sum(s.get("missing_count", 0) for s in new_snaps)
                self.log("ok", f"檢查完成：共有 {total_miss} 集待下載。" if total_miss else "檢查完成：所有追蹤作品皆已下載。")
                if push_notify and self.notifier.enabled:
                    self._push_digest_from_snapshots(new_snaps)
                return

            # ===== 執行下載 =====
            upgrade_plans: dict[str, list[EpisodeInfo]] = {}
            vip_cache: dict[int, bool] = {}
            skipped_up = 0
            for item, title, diff in diff_objects:
                if not diff.upgradable_episodes:
                    continue
                t_res = parse_resolution_int(item.resolution)
                if t_res not in vip_cache:
                    self.log("info", "確認 720P→1080P 升級 VIP 權限...")
                    elig = check_vip_upgrade_eligibility(self.env.cookie_path, t_res)
                    vip_cache[t_res] = elig.allowed
                if vip_cache[t_res]:
                    upgrade_plans[item.sn] = [ep for ep, _ in diff.upgradable_episodes]
                else:
                    skipped_up += 1
            if skipped_up:
                self.log("warn", f"已略過 {skipped_up} 部畫質升級（目前非 VIP，避免重複下載 720P）。")

            all_tasks: list[tuple[AnimeWatchItem, str, EpisodeInfo, bool]] = []
            for item, title, diff in diff_objects:
                for ep in diff.missing_episodes:
                    all_tasks.append((item, title, ep, False))
                for ep in upgrade_plans.get(item.sn, []):
                    all_tasks.append((item, title, ep, True))

            if not all_tasks:
                self.log("ok", "所有追蹤作品皆已下載，本次無需下載。")
                return

            self.task_state["total"] = len(all_tasks)
            self.task_state["progress"] = 0
            downloaded_ok: list[DownloadedRecord] = []
            downloaded_fail: list[str] = []
            was_interrupted = False
            circuit_broken_sns: set[str] = set()

            for idx, (item, title, ep, is_upgrade) in enumerate(all_tasks, 1):
                if self.task_state["cancel_requested"]:
                    was_interrupted = True
                    break
                if is_upgrade and item.sn in circuit_broken_sns:
                    continue

                self.task_state["progress"] = idx
                tag = "[升級] " if is_upgrade else ""
                self.log("info", f"下載 [{idx}/{len(all_tasks)}]：{tag}《{title}》第 {ep.episode_label} 集 (sn={ep.sn})")

                res = dl.download_episode(ep, resolution=item.resolution)
                if res.interrupted or self.task_state["cancel_requested"]:
                    was_interrupted = True
                    downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（手動中斷）")
                    note = f"（已清理 temp/ {res.cleaned_temp_count} 項殘檔）" if res.cleaned_temp_count else ""
                    self.log("warn", f"已乾淨中斷：《{title}》第 {ep.episode_label} 集{note}")
                    break

                if res.success:
                    actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                    if is_upgrade:
                        target_int = parse_resolution_int(item.resolution)
                        if actual_res < target_int - 100:
                            circuit_broken_sns.add(item.sn)
                            self.log("err", f"後置熔斷：《{title}》實際僅 {actual_res or '未知'}P，中止該作剩餘升級。")
                            continue
                    self.tracker.mark_downloaded(ep, actual_res or item.resolution, fpath)
                    res_tag = f"{actual_res}P" if actual_res else f"{item.resolution}P"
                    downloaded_ok.append(DownloadedRecord(title=title, episode=ep, resolution_label=res_tag))
                    self.log("ok", f"完成：《{title}》第 {ep.episode_label} 集 ({res_tag})")

                    # 若為手機／遠端瀏覽器發起且開啟「下載完成自動傳送到目前裝置」
                    if remote_origin and self.config.auto_transfer_remote_downloads and fpath:
                        p_obj = Path(fpath)
                        if p_obj.is_file():
                            try:
                                rel_p = p_obj.relative_to(self.env.bangumi_dir).as_posix()
                                self.remote_ready_files.append(
                                    {
                                        "id": secrets.token_hex(6),
                                        "transfer_token": transfer_token,
                                        "name": p_obj.name,
                                        "rel_path": rel_p,
                                        "size": p_obj.stat().st_size,
                                        "delete_after": self.config.delete_host_after_transfer,
                                    }
                                )
                            except Exception:
                                pass
                else:
                    downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（{res.message}）")
                    self.log("err", f"下載失敗：《{title}》第 {ep.episode_label} 集 ({res.message})")

                if idx < len(all_tasks) and self.config.cooldown_seconds > 0:
                    self.log("info", f"冷卻 {self.config.cooldown_seconds} 秒...")
                    end_cd = time.monotonic() + self.config.cooldown_seconds
                    while time.monotonic() < end_cd:
                        if self.task_state["cancel_requested"]:
                            was_interrupted = True
                            break
                        time.sleep(0.2)
                    if was_interrupted:
                        break

            # 更新快照與推播報告
            if downloaded_ok or downloaded_fail:
                n_title, n_body = format_download_report(downloaded_ok, downloaded_fail, [], interrupted=was_interrupted)
                self.log("ok" if not was_interrupted else "warn", f"{n_title}\n{n_body}")
                if self.notifier.enabled and not was_interrupted:
                    for label, ok, detail in self.notifier.send_iter(n_title, n_body):
                        self.log("ok" if ok else "warn", f"推播 {label}：{'已送出' if ok else detail}")
        except Exception as exc:
            self.log("err", f"任務執行發生未預期錯誤：{exc}")
        finally:
            self._active_downloader = None
            self.task_state["running"] = False
            self.task_state["cancel_requested"] = False

    def _push_digest_from_snapshots(self, snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snap_map = {s["sn"]: s for s in snaps}
        new_entries: list[DigestEntry] = []
        done_entries: list[DigestEntry] = []
        errors: list[str] = []

        for w in self.get_sorted_watchlist():
            if not w.enabled:
                continue
            s = snap_map.get(w.sn)
            if not s:
                continue
            if s.get("error"):
                errors.append(f"{w.name}：{s['error']}")
                continue
            ratio = f"{s.get('online_count', '?')}/{w.total_episodes or '?'}"
            sched = "已完結" if w.status == "已完結" else (w.schedule_note or "")
            if s.get("missing_count", 0) > 0:
                new_entries.append(
                    DigestEntry(
                        title=s.get("title", w.name),
                        progress=ratio,
                        schedule=sched,
                        missing_text=s.get("missing_range", ""),
                        missing_count=s["missing_count"],
                    )
                )
            else:
                done_entries.append(DigestEntry(title=s.get("title", w.name), progress=ratio, schedule=sched))

        skipped = [w.name for w in self.get_sorted_watchlist() if not w.enabled]
        title, body = format_check_digest(new_entries, done_entries, skipped, errors)
        results = []
        for label, ok, detail in self.notifier.send_iter(title, body):
            results.append({"provider": label, "ok": ok, "detail": detail})
            self.log("ok" if ok else "warn", f"推播 {label}：{'已送出' if ok else detail}")
        return results

    def list_bangumi_files(self) -> list[dict[str, Any]]:
        """列出 bangumi 目錄內所有影片檔與 QuickTime 相容狀態，供手機／遠端直接下載。"""
        if not self.env.bangumi_dir.is_dir():
            return []
        video_exts = {".mp4", ".mkv", ".ts", ".webm"}
        items: list[dict[str, Any]] = []
        for p in sorted(self.env.bangumi_dir.rglob("*"), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
            if not p.is_file() or p.suffix.lower() not in video_exts:
                continue
            st = p.stat()
            if st.st_size < 1024 * 50:
                continue
            rel = p.relative_to(self.env.bangumi_dir).as_posix()
            series = p.parent.name if p.parent != self.env.bangumi_dir else "未分類"
            res_m = re.search(r"\[(\d{3,4})[pP]\]", p.stem)
            items.append(
                {
                    "name": p.name,
                    "series": series,
                    "rel_path": rel,
                    "size_bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "resolution": f"{res_m.group(1)}P" if res_m else "-",
                }
            )
        return items


class _QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AniManagerWebServer:
    """封裝 HTTP Server 生命週期，供 PyWebView 視窗與純背景 --server 模式共用。"""

    def __init__(self, backend: AniManagerBackend) -> None:
        self.backend = backend
        self.httpd: _QuietHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> tuple[str, int]:
        backend = self.backend
        ui_dir = backend.env.bundle_dir / "ani_manager" / "web_ui"
        assets_dir = backend.env.bundle_dir / "assets"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _client_ip(self) -> str:
                return str(self.client_address[0] if self.client_address else "")

            def _is_local(self) -> bool:
                ip = self._client_ip()
                return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

            def _is_authenticated(self) -> bool:
                if not backend.config.remote_auth_enabled:
                    return True
                if self._is_local():
                    return True
                # Check Cookie or X-Device-Token header
                token = self.headers.get("X-Device-Token", "").strip()
                if not token and "Cookie" in self.headers:
                    c = http.cookies.SimpleCookie()
                    try:
                        c.load(self.headers["Cookie"])
                        if DEVICE_COOKIE_NAME in c:
                            token = c[DEVICE_COOKIE_NAME].value
                    except Exception:
                        pass
                return _verify_device_token(token, backend.config.remote_auth_secret, backend.config.remote_pin_version)

            def _send_json(self, payload: Any, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _read_json_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8", errors="replace"))

            def _serve_static(self, file_path: Path) -> None:
                if not file_path.is_file():
                    self.send_error(404)
                    return
                ctype, _ = mimetypes.guess_type(str(file_path))
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8" if (ctype and ctype.startswith("text/")) else (ctype or "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream_file_download(self, target_file: Path, download_name: str, delete_after: bool = False) -> None:
                """支援 HTTP Range 的檔案串流下載，讓 iOS Safari / Android Chrome / 電腦可直接下載影片或備份。"""
                if not target_file.is_file():
                    self.send_error(404, "File not found")
                    return
                file_size = target_file.stat().st_size
                ctype, _ = mimetypes.guess_type(str(target_file))
                ctype = ctype or "application/octet-stream"
                encoded_name = urllib.parse.quote(download_name)
                disp = f"attachment; filename*=UTF-8''{encoded_name}"

                range_header = self.headers.get("Range", "").strip()
                start = 0
                end = file_size - 1
                status_code = 200
                if range_header.startswith("bytes="):
                    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
                    if m:
                        if m.group(1):
                            start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), file_size - 1)
                        if start <= end < file_size:
                            status_code = 206

                chunk_len = end - start + 1
                self.send_response(status_code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", disp)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(chunk_len))
                if status_code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.end_headers()

                with target_file.open("rb") as f:
                    f.seek(start)
                    remaining = chunk_len
                    while remaining > 0:
                        buf = f.read(min(65536, remaining))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        remaining -= len(buf)

                if delete_after and status_code == 200:
                    try:
                        target_file.unlink(missing_ok=True)
                        backend.log("info", f"已傳送至遠端裝置並清除主機端副本：{download_name}")
                    except Exception:
                        pass

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)

                # Static assets (always accessible so lock screen renders cleanly)
                if path in ("/", "/index.html"):
                    return self._serve_static(ui_dir / "index.html")
                if path == "/style.css":
                    return self._serve_static(ui_dir / "style.css")
                if path == "/app.js":
                    return self._serve_static(ui_dir / "app.js")
                if path == "/icon.png":
                    return self._serve_static(assets_dir / "icon.png")

                is_local = self._is_local()
                authed = self._is_authenticated()

                if path == "/api/state":
                    if not authed:
                        return self._send_json(
                            {
                                "version": "2.0.0",
                                "auth": {
                                    "enabled": True,
                                    "is_local": is_local,
                                    "authenticated": False,
                                    "has_webauthn": len(backend.config.remote_webauthn_credentials) > 0,
                                },
                            },
                            status=200,
                        )
                    return self._send_json(backend.get_full_state(is_local=is_local, is_authenticated=True))

                if not authed:
                    return self._send_json({"ok": False, "error": "UNAUTHORIZED"}, status=401)

                if path == "/api/files/list":
                    return self._send_json({"ok": True, "files": backend.list_bangumi_files()})

                if path == "/api/files/download":
                    rel = (qs.get("path") or [""])[0]
                    del_after = (qs.get("delete_after") or ["0"])[0] == "1"
                    if not rel:
                        return self.send_error(400)
                    target = (backend.env.bangumi_dir / rel).resolve()
                    try:
                        target.relative_to(backend.env.bangumi_dir.resolve())
                    except ValueError:
                        return self.send_error(403, "Invalid path")
                    return self._stream_file_download(target, target.name, delete_after=del_after)

                if path == "/api/backup/download":
                    fname = Path((qs.get("name") or [""])[0]).name
                    if not fname.endswith(".anibak"):
                        return self.send_error(400)
                    target = (backend.env.root_dir / "exports" / fname).resolve()
                    return self._stream_file_download(target, fname, delete_after=False)

                self.send_error(404)

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                body = self._read_json_body()

                # Auth endpoints (allowed before login)
                if path == "/api/auth/login":
                    pin = str(body.get("pin", "")).strip()
                    remember = bool(body.get("remember", True))
                    if not backend.config.remote_auth_enabled or (pin and hmac.compare_digest(pin, backend.config.remote_pin)):
                        token = _sign_device_token(backend.config.remote_auth_secret, backend.config.remote_pin_version)
                        headers = {}
                        if remember:
                            headers["Set-Cookie"] = f"{DEVICE_COOKIE_NAME}={token}; Path=/; Max-Age={DEVICE_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax"
                        return self._send_json({"ok": True, "token": token}, extra_headers=headers)
                    return self._send_json({"ok": False, "error": "密碼錯誤"}, status=403)

                if path == "/api/auth/logout":
                    headers = {"Set-Cookie": f"{DEVICE_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"}
                    return self._send_json({"ok": True}, extra_headers=headers)

                if path == "/api/auth/webauthn/login-options":
                    if not backend.config.remote_webauthn_credentials:
                        return self._send_json({"ok": False, "error": "尚未註冊生物辨識憑證"}, status=400)
                    chal = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
                    backend._webauthn_challenges[chal] = time.time()
                    allow_ids = [c["id"] for c in backend.config.remote_webauthn_credentials if c.get("id")]
                    return self._send_json({"ok": True, "challenge": chal, "allow_ids": allow_ids})

                if path == "/api/auth/webauthn/login":
                    chal = str(body.get("challenge", ""))
                    cred_id = str(body.get("id", ""))
                    ts = backend._webauthn_challenges.pop(chal, 0)
                    if not ts or (time.time() - ts) > 120:
                        return self._send_json({"ok": False, "error": "驗證逾時，請重試"}, status=400)
                    known_ids = {c.get("id") for c in backend.config.remote_webauthn_credentials}
                    if cred_id not in known_ids:
                        return self._send_json({"ok": False, "error": "未註冊的裝置生物辨識憑證"}, status=403)
                    token = _sign_device_token(backend.config.remote_auth_secret, backend.config.remote_pin_version)
                    headers = {"Set-Cookie": f"{DEVICE_COOKIE_NAME}={token}; Path=/; Max-Age={DEVICE_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax"}
                    return self._send_json({"ok": True, "token": token}, extra_headers=headers)

                if not self._is_authenticated():
                    return self._send_json({"ok": False, "error": "UNAUTHORIZED"}, status=401)

                if path == "/api/auth/webauthn/register-options":
                    chal = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
                    user_id = base64.urlsafe_b64encode(b"animanager-owner").decode("ascii").rstrip("=")
                    backend._webauthn_challenges[chal] = time.time()
                    return self._send_json({"ok": True, "challenge": chal, "user_id": user_id})

                if path == "/api/auth/webauthn/register":
                    chal = str(body.get("challenge", ""))
                    cred_id = str(body.get("id", ""))
                    label = str(body.get("label", "已註冊生物辨識裝置")).strip() or "已註冊生物辨識裝置"
                    ts = backend._webauthn_challenges.pop(chal, 0)
                    if not ts or (time.time() - ts) > 120 or not cred_id:
                        return self._send_json({"ok": False, "error": "註冊逾時或憑證無效"}, status=400)
                    existing = [c for c in backend.config.remote_webauthn_credentials if c.get("id") != cred_id]
                    existing.append({"id": cred_id, "label": label, "created_at": now_timestamp()})
                    backend.config.remote_webauthn_credentials = existing
                    backend.config_store.save(backend.config)
                    backend.log("ok", f"已註冊遠端生物辨識解鎖裝置：{label}")
                    return self._send_json({"ok": True})

                if path == "/api/check":
                    ok, msg = backend.start_check_or_download_task(
                        download=bool(body.get("download", False)),
                        single_sn=body.get("sn") or None,
                        force_single=bool(body.get("force_single", False)),
                        push_notify=bool(body.get("push_notify", False)),
                        remote_origin=not self._is_local(),
                        transfer_token=str(body.get("transfer_token", "")),
                    )
                    return self._send_json({"ok": ok, "message": msg}, status=200 if ok else 409)

                if path == "/api/task/cancel":
                    ok, msg = backend.cancel_active_task()
                    return self._send_json({"ok": ok, "message": msg})

                if path == "/api/watchlist/search":
                    query = str(body.get("query", "")).strip()
                    if not query:
                        return self._send_json({"ok": False, "error": "請輸入片名、網址或 SN"}, status=400)
                    sn = extract_sn_from_input(query)
                    try:
                        if sn:
                            meta, eps = enrich_anime_metadata(
                                base_sn=sn,
                                cookie_path=backend.env.cookie_path if backend.env.has_cookie else None,
                            )
                            return self._send_json({"ok": True, "mode": "resolved", "meta": asdict(meta)})
                        if query.isdigit():
                            return self._send_json({"ok": False, "error": "SN 至少為 4 碼數字；搜尋請輸入片名"}, status=400)
                        cands = search_anigamer(query, cookie_path=backend.env.cookie_path if backend.env.has_cookie else None)
                        return self._send_json({"ok": True, "mode": "candidates", "candidates": [asdict(c) for c in cands]})
                    except EpisodeFetchError as exc:
                        return self._send_json({"ok": False, "error": str(exc)}, status=400)

                if path == "/api/watchlist/resolve-candidate":
                    from .metadata_fetcher import SearchCandidate

                    cand = SearchCandidate(
                        title=str(body.get("title", "")),
                        sn=str(body.get("sn", "")),
                        is_ref_sn=bool(body.get("is_ref_sn", False)),
                    )
                    try:
                        r_title, r_sn, _ = resolve_candidate_video_sn(
                            cand,
                            cookie_path=backend.env.cookie_path if backend.env.has_cookie else None,
                        )
                        meta, _ = enrich_anime_metadata(
                            base_sn=r_sn,
                            fallback_title=r_title,
                            cookie_path=backend.env.cookie_path if backend.env.has_cookie else None,
                        )
                        return self._send_json({"ok": True, "meta": asdict(meta)})
                    except EpisodeFetchError as exc:
                        return self._send_json({"ok": False, "error": str(exc)}, status=400)

                if path == "/api/watchlist/add":
                    sn = str(body.get("sn", "")).strip()
                    name = str(body.get("name", "")).strip()
                    if not sn or len(name) < 2:
                        return self._send_json({"ok": False, "error": "無效的作品名稱或 SN"}, status=400)
                    res = validate_resolution(str(body.get("resolution", backend.config.default_resolution))) or backend.config.default_resolution
                    tot_raw = str(body.get("total_episodes") or "")
                    ok_tot, tot_val, err_tot = validate_total_episodes(tot_raw)
                    if not ok_tot:
                        return self._send_json({"ok": False, "error": err_tot}, status=400)
                    online_cnt = body.get("online_episodes")
                    item = AnimeWatchItem(
                        name=name,
                        sn=sn,
                        resolution=res,
                        schedule_note=str(body.get("schedule_note", "") or ""),
                        total_episodes=tot_val,
                        online_episodes=int(online_cnt) if isinstance(online_cnt, int) else None,
                        status=str(body.get("status", "") or ""),
                        enabled=True,
                        added_at=now_timestamp(),
                        latest_upload_at=str(body.get("latest_upload_at", "") or ""),
                        views=body.get("views") if isinstance(body.get("views"), int) else None,
                    )
                    backend.watchlist, is_new = backend.watchlist_store.add_or_update(backend.watchlist, item)
                    backend.log("ok", f"{'新增' if is_new else '更新'}追番：《{item.name}》(SN: {item.sn})")
                    if is_new and body.get("notify") and backend.notifier.enabled:
                        nt, nb = format_anime_added_notice(
                            item.name, item.sn, item.online_episodes or 0, item.total_episodes, item.schedule_note, item.resolution
                        )
                        backend.notifier.send(nt, nb)
                    return self._send_json({"ok": True, "is_new": is_new})

                if path == "/api/watchlist/toggle":
                    sns = set(body.get("sns") or [])
                    toggled = backend.watchlist_store.toggle_by_sns(backend.watchlist, sns)
                    for t in toggled:
                        backend.log("info", f"切換追蹤：《{t.name}》→ {'追蹤' if t.enabled else '略過'}")
                    return self._send_json({"ok": True, "count": len(toggled)})

                if path == "/api/watchlist/edit":
                    sn = str(body.get("sn", "")).strip()
                    target = next((w for w in backend.watchlist if w.sn == sn), None)
                    if not target:
                        return self._send_json({"ok": False, "error": "找不到作品"}, status=404)
                    if "name" in body and str(body["name"]).strip():
                        target.name = str(body["name"]).strip()
                    if "resolution" in body:
                        v_res = validate_resolution(str(body["resolution"]))
                        if v_res:
                            target.resolution = v_res
                    if "total_episodes" in body:
                        ok_t, val_t, err_t = validate_total_episodes(str(body["total_episodes"] or ""))
                        if not ok_t:
                            return self._send_json({"ok": False, "error": err_t}, status=400)
                        target.total_episodes = val_t
                    if "status" in body:
                        target.status = str(body["status"] or "")
                    if "schedule_note" in body:
                        target.schedule_note = str(body["schedule_note"] or "")
                    backend.watchlist_store.save(backend.watchlist)
                    backend.log("ok", f"已編輯：《{target.name}》")
                    return self._send_json({"ok": True})

                if path == "/api/watchlist/delete":
                    sns = set(body.get("sns") or [])
                    removed = backend.watchlist_store.remove_by_sns(backend.watchlist, sns)
                    if removed:
                        backend.log("warn", "已刪除追番：" + "、".join(f"《{r.name}》" for r in removed))
                    return self._send_json({"ok": True, "count": len(removed)})

                if path == "/api/watchlist/sort":
                    sort_by = str(body.get("sort_by", "added_at"))
                    desc = bool(body.get("desc", False))
                    if sort_by in SORT_LABELS:
                        backend.config.watchlist_sort_by = sort_by
                        backend.config.watchlist_sort_desc = desc
                        backend.config_store.save(backend.config)
                    return self._send_json({"ok": True})

                if path == "/api/watchlist/sync":
                    sns = set(body.get("sns") or [])
                    overwrite = bool(body.get("overwrite", False))
                    targets = [w for w in backend.watchlist if not sns or w.sn in sns]
                    updated = 0
                    for item in targets:
                        try:
                            meta, _ = enrich_anime_metadata(
                                base_sn=item.sn,
                                fallback_title=item.name,
                                cookie_path=backend.env.cookie_path if backend.env.has_cookie else None,
                                query_external_total=True,
                            )
                            item.online_episodes = meta.online_count
                            if meta.latest_upload_at:
                                item.latest_upload_at = meta.latest_upload_at
                            if meta.views:
                                item.views = meta.views
                            if meta.title and (overwrite or item.name.startswith("SN-")):
                                item.name = meta.title
                            if meta.status and (overwrite or not item.status):
                                item.status = meta.status
                            if meta.schedule_note and (overwrite or not item.schedule_note):
                                item.schedule_note = meta.schedule_note
                            if meta.total_episodes and (overwrite or not item.total_episodes or item.total_episodes < meta.online_count):
                                item.total_episodes = meta.total_episodes
                            updated += 1
                        except Exception as exc:
                            backend.log("err", f"同步失敗 《{item.name}》：{exc}")
                    backend.watchlist_store.save(backend.watchlist)
                    backend.log("ok", f"已完成 {updated} 部作品資訊同步")
                    return self._send_json({"ok": True, "updated": updated})

                if path == "/api/files/delete":
                    rel = str(body.get("rel_path", "")).strip()
                    if not rel:
                        return self._send_json({"ok": False, "error": "缺少路徑"}, status=400)
                    target = (backend.env.bangumi_dir / rel).resolve()
                    try:
                        target.relative_to(backend.env.bangumi_dir.resolve())
                    except ValueError:
                        return self._send_json({"ok": False, "error": "路徑不合法"}, status=403)
                    if target.is_file():
                        target.unlink(missing_ok=True)
                        backend.log("info", f"已刪除本機檔案：{target.name}")
                    return self._send_json({"ok": True})

                if path == "/api/files/repair-audio":
                    rel = str(body.get("rel_path", "")).strip()
                    if rel:
                        target = (backend.env.bangumi_dir / rel).resolve()
                        ok, msg = remux_audio_for_quicktime(target, replace_original=True)
                        backend.log("ok" if ok else "err", f"音軌修復 {target.name}：{msg}")
                        return self._send_json({"ok": ok, "message": msg})
                    inspections = scan_directory_audio(backend.env.bangumi_dir)
                    fixed = 0
                    for ins in inspections:
                        if ins.has_audio and not ins.quicktime_friendly:
                            ok, _ = remux_audio_for_quicktime(ins.file_path, replace_original=True)
                            if ok:
                                fixed += 1
                    backend.log("ok", f"批次音軌檢查完成，共修復 {fixed} 個檔案")
                    return self._send_json({"ok": True, "fixed": fixed})

                if path == "/api/cookie/clipboard":
                    ok, raw = read_clipboard(backend.env.raw_os)
                    if not ok:
                        return self._send_json({"ok": False, "error": raw}, status=400)
                    chk = validate_and_sanitize_cookie(raw)
                    if not chk.valid:
                        return self._send_json({"ok": False, "error": chk.reason}, status=400)
                    write_cookie_file(backend.env.cookie_path, chk.cleaned_cookie)
                    backend.reload_state()
                    online = verify_cookie_online(chk.cleaned_cookie)
                    backend.log("ok", f"已從剪貼簿寫入 Cookie ({chk.baha_id_masked})：{online.detail}")
                    return self._send_json({"ok": True, "detail": online.detail})

                if path == "/api/cookie/save":
                    raw = str(body.get("cookie", ""))
                    chk = validate_and_sanitize_cookie(raw)
                    if not chk.valid:
                        return self._send_json({"ok": False, "error": chk.reason}, status=400)
                    write_cookie_file(backend.env.cookie_path, chk.cleaned_cookie)
                    backend.reload_state()
                    online = verify_cookie_online(chk.cleaned_cookie)
                    backend.log("ok", f"已更新 Cookie ({chk.baha_id_masked})：{online.detail}")
                    return self._send_json({"ok": True, "detail": online.detail})

                if path == "/api/cookie/playwright":
                    def _pw_job() -> None:
                        backend.log("info", "已啟動獨立瀏覽器，請完成巴哈登入（關閉視窗可取消）...")
                        ok, msg, chk = capture_cookie_via_playwright(backend.env.cookie_path)
                        if ok:
                            backend.reload_state()
                            backend.log("ok", msg)
                        else:
                            backend.log("warn", msg)

                    threading.Thread(target=_pw_job, daemon=True).start()
                    return self._send_json({"ok": True, "message": "已在主機開啟登入視窗"})

                if path == "/api/cookie/check":
                    backend.reload_state()
                    if not backend.env.has_cookie:
                        return self._send_json({"ok": False, "error": "尚未設定 cookie.txt"}, status=400)
                    chk = inspect_cookie_file(backend.env.cookie_path)
                    if not chk.valid:
                        return self._send_json({"ok": False, "error": chk.reason}, status=400)
                    online = verify_cookie_online(chk.cleaned_cookie)
                    backend.log("info", f"Cookie 驗證：{online.detail}")
                    return self._send_json({"ok": True, "detail": online.detail})

                if path == "/api/notify/test":
                    if not backend.notifier.enabled:
                        return self._send_json({"ok": False, "error": "尚未啟用任何推播管道"}, status=400)
                    snaps = backend.last_snapshots
                    if not snaps:
                        # Quick synchronous scan for digest
                        backend.start_check_or_download_task(download=False, push_notify=True)
                        return self._send_json({"ok": True, "message": "正在掃描最新進度並推播簡報..."})
                    res = backend._push_digest_from_snapshots(snaps)
                    return self._send_json({"ok": True, "results": res})

                if path == "/api/settings/update":
                    cfg = backend.config
                    if "dedup_mode" in body and body["dedup_mode"] in ("file_only", "file_and_history"):
                        cfg.dedup_mode = body["dedup_mode"]
                    if "upgrade_lower_resolution" in body:
                        cfg.upgrade_lower_resolution = bool(body["upgrade_lower_resolution"])
                    if "check_on_startup" in body:
                        cfg.check_on_startup = bool(body["check_on_startup"])
                    if "default_resolution" in body:
                        v = validate_resolution(str(body["default_resolution"]))
                        if v:
                            cfg.default_resolution = v
                    if "close_to_tray" in body:
                        cfg.close_to_tray = bool(body["close_to_tray"])
                    if "start_minimized" in body:
                        cfg.start_minimized = bool(body["start_minimized"])
                    if "auto_transfer_remote_downloads" in body:
                        cfg.auto_transfer_remote_downloads = bool(body["auto_transfer_remote_downloads"])
                    if "delete_host_after_transfer" in body:
                        cfg.delete_host_after_transfer = bool(body["delete_host_after_transfer"])
                    if "tray_interval_minutes" in body:
                        cfg.tray_interval_minutes = max(10, int(body["tray_interval_minutes"] or 60))
                    if "scheduler_times" in body and isinstance(body["scheduler_times"], list):
                        valid_t = [t for x in body["scheduler_times"] if (t := validate_hhmm(str(x)))]
                        if valid_t:
                            cfg.scheduler_times = valid_t

                    sched_msg = ""
                    if "background_mode" in body and body["background_mode"] in ("manual", "tray", "scheduler"):
                        cfg.background_mode = body["background_mode"]
                        if cfg.background_mode == "scheduler":
                            _, sched_msg = install_os_schedule(backend.env, cfg.scheduler_times)
                        else:
                            _, sched_msg = remove_os_schedule(backend.env)
                    elif body.get("apply_scheduler") and cfg.background_mode == "scheduler":
                        _, sched_msg = install_os_schedule(backend.env, cfg.scheduler_times)

                    if "notifications" in body and isinstance(body["notifications"], dict):
                        for p_name in ("ntfy", "discord", "telegram", "line"):
                            if isinstance(body["notifications"].get(p_name), dict):
                                cfg.notifications[p_name].update(body["notifications"][p_name])

                    # Remote access lock & PIN management
                    if "remote_auth_enabled" in body:
                        want_lock = bool(body["remote_auth_enabled"])
                        cfg.remote_auth_enabled = want_lock
                        if want_lock and not cfg.remote_pin:
                            cfg.remote_pin = generate_default_pin()
                    if "remote_pin" in body and str(body["remote_pin"]).strip():
                        new_pin = str(body["remote_pin"]).strip()
                        if new_pin != cfg.remote_pin:
                            cfg.remote_pin = new_pin
                            cfg.remote_pin_version += 1  # 變更密碼後立即讓所有已記住設備失效
                    if body.get("revoke_devices"):
                        cfg.remote_pin_version += 1
                    if body.get("clear_webauthn"):
                        cfg.remote_webauthn_credentials = []

                    backend.config_store.save(cfg)
                    backend.reload_state()
                    if sched_msg:
                        backend.log("info", sched_msg)
                    backend.log("ok", "系統設定已儲存並生效")
                    return self._send_json({"ok": True, "scheduler_message": sched_msg})

                if path == "/api/backup/export":
                    res = export_backup(
                        env=backend.env,
                        config=backend.config,
                        watchlist_path=backend.watchlist_store.watchlist_path,
                        include_upstream_config=bool(body.get("include_upstream", True)),
                        include_history=bool(body.get("include_history", False)),
                        password=str(body.get("password", "")),
                    )
                    if not res.success or not res.output_path:
                        return self._send_json({"ok": False, "error": res.message}, status=400)
                    backend.log("ok", f"已匯出設定包：{res.output_path.name} ({res.cipher_name})")
                    return self._send_json(
                        {
                            "ok": True,
                            "filename": res.output_path.name,
                            "cipher": res.cipher_name,
                            "included": res.included_files,
                        }
                    )

                if path == "/api/backup/import":
                    b64 = str(body.get("file_base64", ""))
                    fname = str(body.get("filename", "upload.anibak"))
                    pwd = str(body.get("password", ""))
                    mode = str(body.get("mode", "full"))
                    if not b64:
                        return self._send_json({"ok": False, "error": "未收到檔案資料"}, status=400)
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64)
                    tmp_dir = backend.env.root_dir / "exports"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    tmp_file = tmp_dir / f".upload_{secrets.token_hex(4)}_{Path(fname).name}"
                    try:
                        tmp_file.write_bytes(raw_bytes)
                        info = inspect_backup_file(tmp_file)
                        if not info.valid:
                            return self._send_json({"ok": False, "error": info.reason}, status=400)
                        if info.encrypted and not pwd:
                            return self._send_json({"ok": False, "need_password": True, "cipher": info.cipher_name, "error": "此備份檔已加密，請輸入密碼"}, status=400)
                        res = import_backup(
                            file_path=tmp_file,
                            env=backend.env,
                            watchlist_path=backend.watchlist_store.watchlist_path,
                            watchlist_store=backend.watchlist_store,
                            current_watchlist=backend.watchlist,
                            password=pwd,
                            mode=mode,
                        )
                        if res.success:
                            backend.reload_state()
                            backend.log("ok", f"備份還原完成：{res.message}")
                            return self._send_json({"ok": True, "message": res.message, "adjustments": res.path_adjustments})
                        return self._send_json({"ok": False, "error": res.message}, status=400)
                    finally:
                        tmp_file.unlink(missing_ok=True)

                self.send_error(404)

        host = backend.config.web_host
        port = backend.config.web_port
        # 若預設埠被佔用，自動往後找可用埠
        for offset in range(10):
            try:
                self.httpd = _QuietHTTPServer((host, port + offset), Handler)
                port = port + offset
                break
            except OSError:
                continue
        if self.httpd is None:
            raise RuntimeError(f"無法綁定本機連接埠 {port}")

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="AniManagerHTTPD")
        self.thread.start()
        backend.start_background_loop()
        return host, port

    def stop(self) -> None:
        self.backend.stop_background_loop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
