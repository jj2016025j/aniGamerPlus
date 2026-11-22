"""去重與增量過濾模組（支援「依實體檔案」與「檔案+歷史+DB」兩種模式）。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .episode_fetcher import EpisodeInfo

FILENAME_PATTERN = re.compile(
    r"^(?:【動畫瘋】)?(?P<title>.+?)\[(?P<ep>[^\]]+?)\](?:\[(?P<res>\d{3,4})[pP]\])?$"
)
VIDEO_EXTS = {".mp4", ".mkv", ".ts", ".webm"}


@dataclass
class LocalFileRecord:
    title: str
    episode_label: str
    resolution: int
    file_path: str


@dataclass
class EpisodeDiffResult:
    already_downloaded: list[EpisodeInfo]
    missing_episodes: list[EpisodeInfo]
    upgradable_episodes: list[tuple[EpisodeInfo, int]]


def normalize_title(text: str) -> str:
    """移除常見標點與空白，讓檔名與網頁標題比對更穩定。"""
    text = text.replace("【動畫瘋】", "")
    text = re.sub(r"[\s！!？?：:、，,。.\-—_「」『』（）()\[\]]+", "", text)
    return text.lower()


def normalize_episode_label(label: str) -> str:
    """統一集數格式，例如 '01' -> '1'。"""
    stripped = str(label).strip()
    if stripped.isdigit():
        return str(int(stripped))
    return stripped.lower()


def parse_resolution_int(res_text: str | int | None) -> int:
    if res_text is None:
        return 0
    match = re.search(r"(\d{3,4})", str(res_text))
    return int(match.group(1)) if match else 0


class HistoryTracker:
    """支援 file_only（刪影片就自動重抓）與 file_and_history（外接硬碟歸檔防重抓）。"""

    def __init__(self, history_path: Path, db_path: Path, bangumi_dir: Path) -> None:
        self.history_path = history_path
        self.db_path = db_path
        self.bangumi_dir = bangumi_dir
        self._data: dict = self._load_json()

    def _load_json(self) -> dict:
        if not self.history_path.is_file():
            return {"downloaded_sns": {}}
        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("downloaded_sns"), dict):
                return data
        except Exception:
            pass
        return {"downloaded_sns": {}}

    def save(self) -> None:
        with self.history_path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def mark_downloaded(self, episode: EpisodeInfo, resolution: str | int, file_path: str = "") -> None:
        res_int = parse_resolution_int(resolution)
        self._data["downloaded_sns"][str(episode.sn)] = {
            "sn": str(episode.sn),
            "anime_title": episode.anime_title,
            "episode_label": episode.episode_label,
            "resolution": res_int,
            "file_path": file_path,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()

    def clear_history_by_sns(self, sns: list[str]) -> int:
        """清除指定 SN 的本地 JSON 紀錄（測試重抓用）。"""
        removed = 0
        store = self._data.get("downloaded_sns", {})
        for sn in sns:
            if str(sn) in store:
                del store[str(sn)]
                removed += 1
        if removed:
            self.save()
        return removed

    def read_anigamer_db_sns(self) -> dict[str, int]:
        """安全唯讀 aniGamerPlus 原生 aniGamer.db。"""
        found: dict[str, int] = {}
        if not self.db_path.is_file():
            return found

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                cols_info = cur.execute(f'PRAGMA table_info("{table}")').fetchall()
                col_names = [c[1] for c in cols_info]
                sn_col = next((c for c in col_names if c.lower() == "sn"), None)
                if not sn_col:
                    continue
                status_col = next((c for c in col_names if c.lower() in ("status", "downloaded", "state")), None)
                res_col = next((c for c in col_names if c.lower() in ("resolution", "quality", "res")), None)

                select_cols = [f'"{sn_col}"']
                if status_col:
                    select_cols.append(f'"{status_col}"')
                if res_col:
                    select_cols.append(f'"{res_col}"')

                rows = cur.execute(f'SELECT {", ".join(select_cols)} FROM "{table}"').fetchall()
                for row in rows:
                    sn_val = str(row[0]).strip()
                    if not sn_val.isdigit():
                        continue
                    idx = 1
                    if status_col:
                        status_val = row[idx]
                        idx += 1
                        if str(status_val) in ("0", "False", "false", "None"):
                            continue
                    res_val = parse_resolution_int(row[idx]) if res_col else 0
                    found[sn_val] = max(found.get(sn_val, 0), res_val)
            conn.close()
        except Exception:
            pass

        return found

    def scan_local_files(self) -> list[LocalFileRecord]:
        """遞迴掃描 bangumi 目錄下所有已存在的影片檔。"""
        records: list[LocalFileRecord] = []
        if not self.bangumi_dir.is_dir():
            return records

        for path in self.bangumi_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                continue
            if path.stat().st_size < 1024 * 100:
                continue
            stem = path.stem
            match = FILENAME_PATTERN.match(stem)
            if match:
                title = match.group("title").strip()
                ep = match.group("ep").strip()
                res = parse_resolution_int(match.group("res"))
                records.append(
                    LocalFileRecord(
                        title=title,
                        episode_label=ep,
                        resolution=res,
                        file_path=str(path),
                    )
                )
        return records

    def compute_diff(
        self,
        online_episodes: list[EpisodeInfo],
        target_resolution: str | int = "1080",
        upgrade_lower_resolution: bool = False,
        anime_name_hint: str = "",
        dedup_mode: str = "file_only",
    ) -> EpisodeDiffResult:
        """
        比對線上集數與本地狀態：
        - dedup_mode == "file_only"：只以 bangumi/ 實體影片是否存在為準（刪掉檔案就會自動重抓）。
        - dedup_mode == "file_and_history"：三層比對（實體檔 + download_history.json + aniGamer.db）。
        """
        target_res = parse_resolution_int(target_resolution)
        json_sns: dict = self._data.get("downloaded_sns", {}) if dedup_mode == "file_and_history" else {}
        db_sns = self.read_anigamer_db_sns() if dedup_mode == "file_and_history" else {}
        local_files = self.scan_local_files()

        file_index: dict[tuple[str, str], LocalFileRecord] = {}
        for rec in local_files:
            key = (normalize_title(rec.title), normalize_episode_label(rec.episode_label))
            if key not in file_index or rec.resolution > file_index[key].resolution:
                file_index[key] = rec

        already_downloaded: list[EpisodeInfo] = []
        missing_episodes: list[EpisodeInfo] = []
        upgradable_episodes: list[tuple[EpisodeInfo, int]] = []

        for ep in online_episodes:
            found_res: int | None = None

            candidate_titles = {
                normalize_title(ep.anime_title),
                normalize_title(anime_name_hint),
            }
            ep_norm = normalize_episode_label(ep.episode_label)
            for norm_t in candidate_titles:
                if not norm_t:
                    continue
                matched = file_index.get((norm_t, ep_norm))
                if matched:
                    found_res = matched.resolution
                    if ep.sn not in self._data.get("downloaded_sns", {}):
                        self.mark_downloaded(ep, matched.resolution, matched.file_path)
                    break

            if found_res is None and dedup_mode == "file_and_history":
                if ep.sn in json_sns:
                    found_res = parse_resolution_int(json_sns[ep.sn].get("resolution"))
                elif ep.sn in db_sns:
                    found_res = db_sns[ep.sn]

            if found_res is None:
                missing_episodes.append(ep)
            elif upgrade_lower_resolution and target_res > 0 and 0 < found_res < target_res:
                upgradable_episodes.append((ep, found_res))
            else:
                already_downloaded.append(ep)

        return EpisodeDiffResult(
            already_downloaded=already_downloaded,
            missing_episodes=missing_episodes,
            upgradable_episodes=upgradable_episodes,
        )
