/* AniManager v2.0.1 Frontend Controller */
const state = {
  data: null,
  deviceToken: localStorage.getItem("anm_device_token") || "",
  transferToken: Math.random().toString(36).slice(2, 10),
  triggeredDownloadIds: new Set(),
  activeView: "dashboard",
  previewLock: false,
  qrMode: "auto", // "auto" | "tailscale" | "lan"
  lastQrUrl: "",
  searchCandidates: [],
  filesList: [],
};

const VIEW_TITLES = {
  dashboard: "總覽與下載",
  watchlist: "追番清單",
  files: "影片與傳送",
  auth: "Cookie 與推播",
  settings: "系統設定",
};

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 3200);
}

/* Custom Async Confirm Modal (works in macOS PyWebView where window.confirm is blocked) */
function askConfirm(title, message) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirmModal");
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMessage").textContent = message;
    modal.classList.remove("hidden");

    const okBtn = document.getElementById("btnConfirmOk");
    const cancelBtn = document.getElementById("btnConfirmCancel");
    const cleanup = (ans) => {
      modal.classList.add("hidden");
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      resolve(ans);
    };
    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
  });
}

/* HTTP-safe Clipboard Copy (works on phone HTTP pages & macOS PyWebView) */
async function copyTextSafe(text, label = "網址") {
  if (!text || text === "-" || text.includes("未偵測")) {
    showToast(`目前沒有可複製的${label}`);
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      showToast(`已複製${label}`);
      return;
    }
  } catch (e) {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    document.execCommand("copy");
    ta.remove();
    showToast(`已複製${label}`);
  } catch (e) {
    showToast("複製失敗，請手動選取網址複製");
  }
}

async function api(url, method = "GET", body = null) {
  const headers = { "Content-Type": "application/json" };
  if (state.deviceToken) headers["X-Device-Token"] = state.deviceToken;
  const opts = { method, headers, credentials: "same-origin" };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    state.deviceToken = "";
    localStorage.removeItem("anm_device_token");
    showLockScreen(true);
    throw new Error("需要驗證");
  }
  return { status: res.status, data };
}

/* Render Real ISO/IEC 18004 Scannable QR Code from Backend SVG */
function updateQrDisplay(network) {
  const tsActive = Boolean(network && network.tailscale_active && network.tailscale_url);
  const lanUrl = (network && network.lan_url) || "";
  const tsUrl = (network && network.tailscale_url) || "";

  let mode = state.qrMode;
  if (mode === "auto") {
    mode = tsActive ? "tailscale" : "lan";
  } else if (mode === "tailscale" && !tsActive && lanUrl) {
    mode = "lan";
  }

  const targetUrl = (mode === "tailscale" ? tsUrl : lanUrl) || network.primary_remote_url || network.local_url;

  const btnTs = document.getElementById("btnQrTailscale");
  const btnLan = document.getElementById("btnQrLan");
  btnTs.classList.toggle("active-tab", mode === "tailscale" && tsActive);
  btnLan.classList.toggle("active-tab", mode === "lan" || !tsActive);
  btnTs.disabled = !tsActive;
  btnLan.disabled = !lanUrl;

  if (targetUrl && targetUrl !== state.lastQrUrl) {
    state.lastQrUrl = targetUrl;
    const qrBox = document.getElementById("qrCodeContainer");
    qrBox.innerHTML = `<img src="/api/qr.svg?text=${encodeURIComponent(targetUrl)}" alt="QR Code" style="width:100%;height:100%;display:block;" />`;
  }
}

/* Lock Screen & WebAuthn Biometrics */
function showLockScreen(show, hasWebauthn = false) {
  const overlay = document.getElementById("lockScreen");
  if (!show) {
    overlay.classList.add("hidden");
    return;
  }
  overlay.classList.remove("hidden");
  const bioBtn = document.getElementById("lockBiometricBtn");
  if (hasWebauthn && window.PublicKeyCredential && window.isSecureContext) {
    bioBtn.classList.remove("hidden");
  } else {
    bioBtn.classList.add("hidden");
  }
}

async function handlePinUnlock() {
  const pin = document.getElementById("lockPinInput").value.trim();
  const remember = document.getElementById("lockRememberCheck").checked;
  const errEl = document.getElementById("lockErrorMsg");
  errEl.textContent = "";
  if (state.previewLock) {
    state.previewLock = false;
    showLockScreen(false);
    showToast("已結束鎖定畫面預覽");
    return;
  }
  const { data } = await api("/api/auth/login", "POST", { pin, remember });
  if (data.ok) {
    if (remember && data.token) {
      state.deviceToken = data.token;
      localStorage.setItem("anm_device_token", data.token);
    }
    document.getElementById("lockPinInput").value = "";
    showLockScreen(false);
    await refreshState();
  } else {
    errEl.textContent = data.error || "驗證失敗";
  }
}

function b64urlToBytes(b64url) {
  const pad = "=".repeat((4 - (b64url.length % 4)) % 4);
  const b64 = (b64url + pad).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function bytesToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function handleBiometricRegister() {
  if (!window.PublicKeyCredential || !window.isSecureContext) {
    showToast("瀏覽器限制：指紋/Face ID 需在 HTTPS（如 Tailscale）或本機環境下註冊");
    return;
  }
  try {
    const { data: opt } = await api("/api/auth/webauthn/register-options", "POST", {});
    if (!opt.ok) return showToast(opt.error || "無法取得註冊選項");
    const cred = await navigator.credentials.create({
      publicKey: {
        challenge: b64urlToBytes(opt.challenge),
        rp: { name: "AniManager", id: window.location.hostname },
        user: {
          id: b64urlToBytes(opt.user_id),
          name: "owner",
          displayName: "AniManager Owner",
        },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
        authenticatorSelection: { authenticatorAttachment: "platform", userVerification: "required" },
        timeout: 60000,
      },
    });
    const label = navigator.userAgent.includes("iPhone") ? "iPhone Touch/Face ID" :
                  navigator.userAgent.includes("Mac") ? "Mac Touch ID" : "生物辨識裝置";
    const { data: reg } = await api("/api/auth/webauthn/register", "POST", {
      challenge: opt.challenge,
      id: bytesToB64url(cred.rawId),
      label,
    });
    if (reg.ok) {
      showToast("已成功綁定此裝置生物辨識解鎖！");
      await refreshState();
    } else {
      showToast(reg.error || "綁定失敗");
    }
  } catch (e) {
    showToast("生物辨識註冊已取消或裝置不支援");
  }
}

async function handleBiometricUnlock() {
  const errEl = document.getElementById("lockErrorMsg");
  errEl.textContent = "";
  try {
    const { data: opt } = await api("/api/auth/webauthn/login-options", "POST", {});
    if (!opt.ok) {
      errEl.textContent = opt.error || "無法啟動生物辨識";
      return;
    }
    const assertion = await navigator.credentials.get({
      publicKey: {
        challenge: b64urlToBytes(opt.challenge),
        allowCredentials: opt.allow_ids.map((id) => ({ type: "public-key", id: b64urlToBytes(id) })),
        userVerification: "required",
        timeout: 60000,
      },
    });
    const { data: login } = await api("/api/auth/webauthn/login", "POST", {
      challenge: opt.challenge,
      id: bytesToB64url(assertion.rawId),
    });
    if (login.ok) {
      if (login.token) {
        state.deviceToken = login.token;
        localStorage.setItem("anm_device_token", login.token);
      }
      showLockScreen(false);
      await refreshState();
    } else {
      errEl.textContent = login.error || "生物辨識驗證失敗";
    }
  } catch (e) {
    errEl.textContent = "生物辨識已取消";
  }
}

/* View Navigation */
function switchView(viewName) {
  state.activeView = viewName;
  document.getElementById("pageTitle").textContent = VIEW_TITLES[viewName] || viewName;
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  document.getElementById(`view-${viewName}`).classList.add("active");
  document.querySelectorAll(".nav-item, .m-nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === viewName);
  });
  if (viewName === "files") loadFiles();
}

/* Render State */
function renderState(d) {
  state.data = d;
  if (!d.auth.authenticated && !d.auth.is_local) {
    showLockScreen(true, d.auth.has_webauthn);
    return;
  }
  if (!state.previewLock) showLockScreen(false);

  const badge = document.getElementById("connBadge");
  badge.textContent = d.auth.is_local ? "本機視窗" : "遠端設備連線";
  badge.className = "badge " + (d.auth.is_local ? "badge-ok" : "badge-warn");

  const quitBtn = document.getElementById("btnQuitApp");
  if (quitBtn) quitBtn.classList.toggle("hidden", !d.auth.is_local);

  // Stats
  document.getElementById("statEnv").textContent = d.env.os_name;
  document.getElementById("statEnvSub").textContent = `核心:${d.env.has_core ? "OK" : "缺"} ｜ FFmpeg:${d.env.has_ffmpeg ? "OK" : "未裝"}`;
  document.getElementById("statCookie").textContent = d.cookie.account;
  document.getElementById("statCookieSub").textContent = d.cookie.valid ? d.cookie.tier : "訪客模式";

  const bgLabels = { manual: "僅手動操作", tray: `常駐每 ${d.config.tray_interval_minutes} 分`, scheduler: "系統原生排程" };
  document.getElementById("statBgMode").textContent = bgLabels[d.config.background_mode] || d.config.background_mode;
  document.getElementById("statBgSub").textContent = d.scheduler.installed ? d.scheduler.detail : `預設 ${d.config.default_resolution}P`;

  document.getElementById("statAuth").textContent = d.config.remote_auth_enabled ? "已上鎖 (PIN)" : "未上鎖";
  document.getElementById("statAuthSub").textContent = d.config.remote_auth_enabled
    ? `PIN: ${d.config.remote_pin} ｜ 生物辨識 ${d.auth.webauthn_count} 台`
    : "區網 / Tailscale 直連";

  // Dual LAN + Tailscale Network Card
  const net = d.network || {};
  const tsBadge = document.getElementById("tailscaleBadge");
  if (net.tailscale_active) {
    tsBadge.textContent = `Tailscale 已連線`;
    tsBadge.className = "badge badge-ok";
    document.getElementById("tailscaleUrlText").textContent = net.tailscale_url;
  } else {
    tsBadge.textContent = "Tailscale 未啟動";
    tsBadge.className = "badge badge-muted";
    document.getElementById("tailscaleUrlText").textContent = "未偵測到 Tailscale（開啟後按「刷新網路」）";
  }
  document.getElementById("lanUrlText").textContent = net.lan_url || "未偵測到區網 IP";
  document.getElementById("netCheckedText").textContent = `最近偵測：${net.checked_at || "-"}（${net.tailscale_source || ""}）｜切換上方按鈕可切換 QR Code。`;
  updateQrDisplay(net);

  // Task Banner
  const banner = document.getElementById("taskBanner");
  if (d.task.running) {
    banner.classList.remove("hidden");
    document.getElementById("taskLabel").textContent = d.task.title || "執行中...";
    document.getElementById("taskCounter").textContent = d.task.total ? `${d.task.progress}/${d.task.total}` : "";
  } else {
    banner.classList.add("hidden");
  }

  // Dashboard Mini Watchlist
  const dashWl = document.getElementById("dashWatchlist");
  const activeItems = d.watchlist.filter((w) => w.enabled);
  if (!activeItems.length) {
    dashWl.innerHTML = `<div class="muted small">目前無追蹤中作品</div>`;
  } else {
    dashWl.innerHTML = activeItems
      .map((w) => {
        let tag = `<span class="badge badge-muted">${w.online_episodes ?? "?"}/${w.total_episodes ?? "?"}</span>`;
        if (w.scan && !w.scan.error) {
          tag = w.scan.missing_count > 0
            ? `<span class="badge badge-warn">待下載 ${w.scan.missing_count} 集 (${w.scan.missing_range})</span>`
            : `<span class="badge badge-ok">已下載 (${w.scan.local_count}/${w.scan.online_count})</span>`;
        }
        return `<div class="mini-row"><span><strong>${escapeHtml(w.name)}</strong> <span class="muted small">${escapeHtml(w.schedule_note || "")}</span></span>${tag}</div>`;
      })
      .join("");
  }

  // Logs
  const logBox = document.getElementById("logBox");
  const atBottom = logBox.scrollHeight - logBox.scrollTop <= logBox.clientHeight + 30;
  logBox.innerHTML = d.logs
    .map((l) => `<div class="log-line log-${l.level}">[${l.time}] ${escapeHtml(l.message)}</div>`)
    .join("");
  if (atBottom) logBox.scrollTop = logBox.scrollHeight;

  renderWatchlist(d);
  syncFormValues(d);

  // Auto-transfer newly finished remote downloads
  if (!d.auth.is_local && Array.isArray(d.remote_ready_files)) {
    for (const f of d.remote_ready_files) {
      if (f.transfer_token === state.transferToken && !state.triggeredDownloadIds.has(f.id)) {
        state.triggeredDownloadIds.add(f.id);
        showToast(`開始傳送影片到此裝置：${f.name}`);
        const delParam = f.delete_after ? "&delete_after=1" : "";
        const dlUrl = `/api/files/download?path=${encodeURIComponent(f.rel_path)}${delParam}`;
        const a = document.createElement("a");
        a.href = dlUrl;
        a.download = f.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
    }
  }
}

function renderWatchlist(d) {
  const sortSel = document.getElementById("sortBySelect");
  if (!sortSel.options.length) {
    sortSel.innerHTML = d.sort_options.map((o) => `<option value="${o.key}">${o.label}</option>`).join("");
  }
  sortSel.value = d.config.watchlist_sort_by;
  document.getElementById("btnToggleSortDir").textContent = d.config.watchlist_sort_desc ? "↓" : "↑";

  const container = document.getElementById("watchlistContainer");
  if (!d.watchlist.length) {
    container.innerHTML = `<div class="card muted">目前追番清單為空，點擊「+ 新增作品」開始追番。</div>`;
    return;
  }

  container.innerHTML = d.watchlist
    .map((w, idx) => {
      const ratio = `${w.online_episodes ?? "?"}/${w.total_episodes ?? "?"}`;
      const statusBadge = w.enabled ? `<span class="badge badge-ok">追蹤</span>` : `<span class="badge badge-muted">略過</span>`;
      const scanHint = w.scan && w.scan.missing_count > 0 ? `<span class="badge badge-warn">待下載 ${w.scan.missing_count} 集</span>` : "";
      return `
      <div class="anime-card ${w.enabled ? "" : "paused"}">
        <div class="anime-top">
          <div class="anime-title">${escapeHtml(w.name)}</div>
          <div style="display:flex;gap:4px;flex-shrink:0;">${scanHint}${statusBadge}</div>
        </div>
        <div class="anime-meta">
          <span>SN: ${escapeHtml(w.sn)}</span>
          <span>畫質: ${escapeHtml(w.resolution)}P</span>
          <span>進度: ${ratio}</span>
          <span>熱門: ${escapeHtml(w.views_formatted)}</span>
          <span>${escapeHtml(w.schedule_note || "無排程")}</span>
        </div>
        <div class="anime-actions">
          <button class="btn btn-primary btn-sm" data-act="dl" data-idx="${idx}">下載新集</button>
          <button class="btn btn-secondary btn-sm" data-act="toggle" data-idx="${idx}">${w.enabled ? "設為略過" : "開啟追蹤"}</button>
          <button class="btn btn-ghost btn-sm" data-act="edit" data-idx="${idx}">編輯</button>
          <button class="btn btn-danger btn-sm" data-act="del" data-idx="${idx}">刪除</button>
        </div>
      </div>`;
    })
    .join("");
}

let formsInitialized = false;
function syncFormValues(d) {
  document.getElementById("cookieDetailText").textContent = d.cookie.reason;
  const bioStatus = document.getElementById("bioStatusText");
  if (window.isSecureContext && window.PublicKeyCredential) {
    bioStatus.textContent = `目前環境支援生物辨識（已綁定 ${d.auth.webauthn_count} 台裝置）`;
  } else {
    bioStatus.textContent = "目前為 HTTP 內網連線（已改用自動記住設備 Cookie）；若走 HTTPS 可額外啟用指紋/Face ID";
  }
  document.getElementById("schedulerStatusText").textContent = d.scheduler.detail;

  if (formsInitialized) return;
  formsInitialized = true;

  const c = d.config;
  document.getElementById("chkAutoTransfer").checked = c.auto_transfer_remote_downloads;
  document.getElementById("chkDeleteAfterTransfer").checked = c.delete_host_after_transfer;

  document.getElementById("ntfyEnabled").checked = c.notifications.ntfy.enabled;
  document.getElementById("ntfyEndpoint").value = c.notifications.ntfy.endpoint || "";
  document.getElementById("discordEnabled").checked = c.notifications.discord.enabled;
  document.getElementById("discordWebhook").value = c.notifications.discord.webhook_url || "";
  document.getElementById("tgEnabled").checked = c.notifications.telegram.enabled;
  document.getElementById("tgToken").value = c.notifications.telegram.token || "";
  document.getElementById("tgChatId").value = c.notifications.telegram.chat_id || "";
  document.getElementById("lineEnabled").checked = c.notifications.line.enabled;
  document.getElementById("lineToken").value = c.notifications.line.channel_access_token || "";
  document.getElementById("lineUserId").value = c.notifications.line.user_id || "";

  document.getElementById("setDefaultRes").value = c.default_resolution;
  document.getElementById("setDedupMode").value = c.dedup_mode;
  document.getElementById("setUpgradeRes").checked = c.upgrade_lower_resolution;
  document.getElementById("setCloseToTray").checked = c.close_to_tray;
  document.getElementById("setBgMode").value = c.background_mode;
  document.getElementById("setTrayMinutes").value = c.tray_interval_minutes;
  document.getElementById("setSchedulerTimes").value = (c.scheduler_times || ["02:00", "14:00"]).join(", ");
  document.getElementById("setCorePolicy").value = c.core_update_policy || "latest";
  updateBgModeFields(c.background_mode);

  document.getElementById("setRemoteAuthEnabled").checked = c.remote_auth_enabled;
  document.getElementById("setRemotePin").value = c.remote_pin || "";
}

function updateBgModeFields(mode) {
  document.getElementById("trayIntervalRow").classList.toggle("hidden", mode !== "tray");
  document.getElementById("schedulerTimesRow").classList.toggle("hidden", mode !== "scheduler");
}

/* Watchlist Actions */
async function downloadSingleAnime(sn) {
  const { data } = await api("/api/check", "POST", { download: true, sn, transfer_token: state.transferToken });
  showToast(data.message || "已啟動下載");
  await refreshState();
}

async function toggleAnime(sn) {
  await api("/api/watchlist/toggle", "POST", { sns: [sn] });
  await refreshState();
}

async function deleteAnime(sn, name) {
  const ok = await askConfirm("刪除追番作品", `確定要從追番清單移除《${name}》嗎？（不會刪除硬碟上已下載的影片）`);
  if (!ok) return;
  await api("/api/watchlist/delete", "POST", { sns: [sn] });
  showToast(`已刪除《${name}》`);
  await refreshState();
}

let modalMode = "add";
let pendingMeta = null;

function openAddModal() {
  modalMode = "add";
  pendingMeta = null;
  state.searchCandidates = [];
  document.getElementById("modalTitle").textContent = "新增追番作品";
  document.getElementById("searchStep").classList.remove("hidden");
  document.getElementById("confirmStep").classList.add("hidden");
  document.getElementById("searchAnimeInput").value = "";
  document.getElementById("searchResultsList").innerHTML = "";
  document.getElementById("animeModal").classList.remove("hidden");
}

function openEditAnime(sn) {
  const item = (state.data.watchlist || []).find((w) => w.sn === sn);
  if (!item) return;
  modalMode = "edit";
  document.getElementById("modalTitle").textContent = `編輯：${item.name}`;
  document.getElementById("searchStep").classList.add("hidden");
  document.getElementById("confirmStep").classList.remove("hidden");
  document.getElementById("btnBackToSearch").classList.add("hidden");
  document.getElementById("notifyNewRow").classList.add("hidden");
  document.getElementById("editName").value = item.name;
  document.getElementById("editSn").value = item.sn;
  document.getElementById("editRes").value = item.resolution;
  document.getElementById("editTotal").value = item.total_episodes || "";
  document.getElementById("editSchedule").value = item.schedule_note || "";
  document.getElementById("animeModal").classList.remove("hidden");
}

function populateConfirmStep(meta) {
  pendingMeta = meta;
  document.getElementById("searchStep").classList.add("hidden");
  document.getElementById("confirmStep").classList.remove("hidden");
  document.getElementById("btnBackToSearch").classList.remove("hidden");
  document.getElementById("notifyNewRow").classList.remove("hidden");
  document.getElementById("editName").value = meta.title;
  document.getElementById("editSn").value = meta.base_sn;
  document.getElementById("editRes").value = state.data.config.default_resolution;
  document.getElementById("editTotal").value = meta.total_episodes || "";
  document.getElementById("editSchedule").value = meta.schedule_note || "";
}

async function doAnimeSearch() {
  const q = document.getElementById("searchAnimeInput").value.trim();
  if (!q) return;
  const listEl = document.getElementById("searchResultsList");
  listEl.innerHTML = `<div class="muted small">搜尋解析中...</div>`;
  const { data } = await api("/api/watchlist/search", "POST", { query: q });
  if (!data.ok) {
    listEl.innerHTML = `<div class="error-text">${escapeHtml(data.error || "搜尋失敗")}</div>`;
    return;
  }
  if (data.mode === "resolved") {
    populateConfirmStep(data.meta);
    return;
  }
  state.searchCandidates = data.candidates || [];
  if (!state.searchCandidates.length) {
    listEl.innerHTML = `<div class="muted small">找不到相關作品</div>`;
    return;
  }
  listEl.innerHTML = state.searchCandidates
    .map(
      (c, idx) => `
      <div class="search-item">
        <div>
          <div><strong>${escapeHtml(c.title)}</strong></div>
          <div class="muted small">${escapeHtml(c.year_text || "")} ｜ ${escapeHtml(c.ep_count_text || "")}</div>
        </div>
        <button class="btn btn-primary btn-sm" data-cand-idx="${idx}">選擇</button>
      </div>`
    )
    .join("");
}

async function selectSearchCandidateByIndex(idx) {
  const cand = state.searchCandidates[idx];
  if (!cand) return;
  const listEl = document.getElementById("searchResultsList");
  listEl.innerHTML = `<div class="muted small">正在擷取《${escapeHtml(cand.title)}》基準 SN 與週表...</div>`;
  const { data } = await api("/api/watchlist/resolve-candidate", "POST", cand);
  if (data.ok) {
    populateConfirmStep(data.meta);
  } else {
    listEl.innerHTML = `<div class="error-text">${escapeHtml(data.error || "解析失敗")}</div>`;
  }
}

async function saveAnimeModal() {
  const sn = document.getElementById("editSn").value.trim();
  const name = document.getElementById("editName").value.trim();
  const resolution = document.getElementById("editRes").value;
  const total_episodes = document.getElementById("editTotal").value.trim();
  const schedule_note = document.getElementById("editSchedule").value.trim();
  const status = schedule_note.includes("已完結") ? "已完結" : (schedule_note ? "連載中" : "");

  if (modalMode === "edit") {
    const { data } = await api("/api/watchlist/edit", "POST", { sn, name, resolution, total_episodes, schedule_note, status });
    if (!data.ok) return showToast(data.error || "儲存失敗");
    showToast(`已更新《${name}》`);
  } else {
    const payload = {
      sn,
      name,
      resolution,
      total_episodes,
      schedule_note,
      status,
      online_episodes: pendingMeta ? pendingMeta.online_count : null,
      latest_upload_at: pendingMeta ? pendingMeta.latest_upload_at : "",
      views: pendingMeta ? pendingMeta.views : null,
      notify: document.getElementById("chkNotifyNewAnime").checked,
    };
    const { data } = await api("/api/watchlist/add", "POST", payload);
    if (!data.ok) return showToast(data.error || "新增失敗");
    showToast(`已加入《${name}》`);
  }
  document.getElementById("animeModal").classList.add("hidden");
  await refreshState();
}

/* Files & Transfer */
async function loadFiles() {
  const { data } = await api("/api/files/list");
  state.filesList = (data.ok && data.files) ? data.files : [];
  const container = document.getElementById("filesListContainer");
  if (!state.filesList.length) {
    container.innerHTML = `<div class="card muted">目前下載資料夾尚無影片檔</div>`;
    return;
  }
  container.innerHTML = state.filesList
    .map((f, idx) => {
      const mb = (f.size_bytes / (1024 * 1024)).toFixed(1) + " MB";
      const dlHref = `/api/files/download?path=${encodeURIComponent(f.rel_path)}`;
      return `
      <div class="file-row">
        <div>
          <div class="file-name">${escapeHtml(f.name)}</div>
          <div class="muted small">${escapeHtml(f.series)} ｜ ${escapeHtml(f.resolution)} ｜ ${mb} ｜ ${escapeHtml(f.modified_at)}</div>
        </div>
        <div class="btn-group">
          <a href="${dlHref}" download="${escapeHtml(f.name)}" class="btn btn-primary btn-sm" style="text-decoration:none;">下載到此裝置</a>
          <button class="btn btn-ghost btn-sm" data-file-act="repair" data-file-idx="${idx}">修音軌</button>
          <button class="btn btn-danger btn-sm" data-file-act="del" data-file-idx="${idx}">刪除</button>
        </div>
      </div>`;
    })
    .join("");
}

function escapeHtml(str) {
  return String(str || "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

async function refreshState() {
  try {
    const { data } = await api("/api/state");
    if (data) renderState(data);
  } catch (e) {}
}

/* Bind All Events via Clean Event Delegation */
document.addEventListener("DOMContentLoaded", () => {
  const savedTheme = localStorage.getItem("anm_theme") || "dark";
  document.documentElement.setAttribute("data-theme", savedTheme);
  document.getElementById("themeToggleBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("anm_theme", next);
  });

  document.getElementById("btnQuitApp").addEventListener("click", async () => {
    const ok = await askConfirm("完全結束 AniManager", "結束後背景網頁引擎也會停止，手機將無法連線。確定要完全退出嗎？");
    if (!ok) return;
    await api("/api/system/quit", "POST", {});
    showToast("正在關閉程式...");
  });

  document.querySelectorAll(".nav-item, .m-nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  // Delegated clicks for watchlist cards
  document.getElementById("watchlistContainer").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || !state.data) return;
    const w = state.data.watchlist[Number(btn.dataset.idx)];
    if (!w) return;
    if (btn.dataset.act === "dl") downloadSingleAnime(w.sn);
    else if (btn.dataset.act === "toggle") toggleAnime(w.sn);
    else if (btn.dataset.act === "edit") openEditAnime(w.sn);
    else if (btn.dataset.act === "del") deleteAnime(w.sn, w.name);
  });

  // Delegated clicks for search results
  document.getElementById("searchResultsList").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-cand-idx]");
    if (!btn) return;
    selectSearchCandidateByIndex(Number(btn.dataset.candIdx));
  });

  // Delegated clicks for files list
  document.getElementById("filesListContainer").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-file-act]");
    if (!btn) return;
    const f = state.filesList[Number(btn.dataset.fileIdx)];
    if (!f) return;
    if (btn.dataset.fileAct === "repair") {
      showToast("正在修復音軌...");
      const { data } = await api("/api/files/repair-audio", "POST", { rel_path: f.rel_path });
      showToast(data.ok ? "音軌修復完成" : data.message || "修復失敗");
    } else if (btn.dataset.fileAct === "del") {
      const ok = await askConfirm("刪除本機影片", `確定要永久刪除《${f.name}》嗎？`);
      if (!ok) return;
      await api("/api/files/delete", "POST", { rel_path: f.rel_path });
      showToast("已刪除");
      await loadFiles();
    }
  });

  // Topbar check / download / cancel
  document.getElementById("btnQuickCheck").addEventListener("click", async () => {
    const { data } = await api("/api/check", "POST", { download: false });
    showToast(data.message || "開始檢查");
    await refreshState();
  });
  document.getElementById("btnQuickDownload").addEventListener("click", async () => {
    const { data } = await api("/api/check", "POST", { download: true, transfer_token: state.transferToken });
    showToast(data.message || "開始下載");
    await refreshState();
  });
  document.getElementById("btnCancelTask").addEventListener("click", async () => {
    const { data } = await api("/api/task/cancel", "POST", {});
    showToast(data.message || "已送出中斷");
  });

  // Network refresh, QR mode toggle, and copy URLs
  document.getElementById("btnRefreshNetwork").addEventListener("click", async () => {
    const { data } = await api("/api/network/refresh", "POST", {});
    if (data.ok && data.network) {
      state.lastQrUrl = "";
      if (state.data) {
        state.data.network = data.network;
        renderState(state.data);
      }
      showToast(data.network.tailscale_active ? `已偵測到 Tailscale (${data.network.tailscale_ip})` : "已刷新網路（未偵測到 Tailscale）");
    }
  });
  document.getElementById("btnQrTailscale").addEventListener("click", () => {
    state.qrMode = "tailscale";
    state.lastQrUrl = "";
    if (state.data) updateQrDisplay(state.data.network);
  });
  document.getElementById("btnQrLan").addEventListener("click", () => {
    state.qrMode = "lan";
    state.lastQrUrl = "";
    if (state.data) updateQrDisplay(state.data.network);
  });
  document.getElementById("btnCopyTsUrl").addEventListener("click", () => {
    copyTextSafe(document.getElementById("tailscaleUrlText").textContent, "Tailscale 網址");
  });
  document.getElementById("btnCopyLanUrl").addEventListener("click", () => {
    copyTextSafe(document.getElementById("lanUrlText").textContent, "區網網址");
  });

  document.getElementById("btnPushDigestDash").addEventListener("click", async () => {
    const { data } = await api("/api/notify/test", "POST", {});
    showToast(data.ok ? "已發送追番簡報" : data.error || "發送失敗");
    await refreshState();
  });

  // Watchlist toolbar & modal
  document.getElementById("btnOpenAddModal").addEventListener("click", openAddModal);
  document.getElementById("btnCloseModal").addEventListener("click", () => document.getElementById("animeModal").classList.add("hidden"));
  document.getElementById("btnDoSearch").addEventListener("click", doAnimeSearch);
  document.getElementById("searchAnimeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doAnimeSearch(); });
  document.getElementById("btnBackToSearch").addEventListener("click", () => {
    document.getElementById("confirmStep").classList.add("hidden");
    document.getElementById("searchStep").classList.remove("hidden");
  });
  document.getElementById("btnSaveAnimeModal").addEventListener("click", saveAnimeModal);
  document.getElementById("btnSyncAll").addEventListener("click", async () => {
    showToast("正在同步全部作品資訊...");
    const { data } = await api("/api/watchlist/sync", "POST", { overwrite: false });
    showToast(`已同步 ${data.updated || 0} 部作品`);
    await refreshState();
  });
  document.getElementById("sortBySelect").addEventListener("change", async (e) => {
    const key = e.target.value;
    const opt = (state.data.sort_options || []).find((o) => o.key === key);
    await api("/api/watchlist/sort", "POST", { sort_by: key, desc: opt ? opt.default_desc : false });
    await refreshState();
  });
  document.getElementById("btnToggleSortDir").addEventListener("click", async () => {
    await api("/api/watchlist/sort", "POST", {
      sort_by: state.data.config.watchlist_sort_by,
      desc: !state.data.config.watchlist_sort_desc,
    });
    await refreshState();
  });

  // Files & Transfer
  document.getElementById("btnSaveTransferPrefs").addEventListener("click", async () => {
    await api("/api/settings/update", "POST", {
      auto_transfer_remote_downloads: document.getElementById("chkAutoTransfer").checked,
      delete_host_after_transfer: document.getElementById("chkDeleteAfterTransfer").checked,
    });
    showToast("已儲存遠端傳送設定");
  });
  document.getElementById("btnRefreshFiles").addEventListener("click", loadFiles);
  document.getElementById("btnRepairAllAudio").addEventListener("click", async () => {
    const { data } = await api("/api/files/repair-audio", "POST", {});
    showToast(`已修復 ${data.fixed || 0} 個檔案音軌`);
  });

  // Cookie & Notify
  document.getElementById("btnCookieClipboard").addEventListener("click", async () => {
    const { data } = await api("/api/cookie/clipboard", "POST", {});
    showToast(data.ok ? data.detail : data.error);
    await refreshState();
  });
  document.getElementById("btnCookiePlaywright").addEventListener("click", async () => {
    const { data } = await api("/api/cookie/playwright", "POST", {});
    showToast(data.message || "已啟動瀏覽器");
  });
  document.getElementById("btnCookieManualSave").addEventListener("click", async () => {
    const cookie = document.getElementById("cookieManualInput").value;
    const { data } = await api("/api/cookie/save", "POST", { cookie });
    if (data.ok) document.getElementById("cookieManualInput").value = "";
    showToast(data.ok ? data.detail : data.error);
    await refreshState();
  });
  document.getElementById("btnCheckCookieOnline").addEventListener("click", async () => {
    const { data } = await api("/api/cookie/check", "POST", {});
    showToast(data.ok ? data.detail : data.error);
  });
  document.getElementById("btnSaveNotify").addEventListener("click", async () => {
    await api("/api/settings/update", "POST", {
      notifications: {
        ntfy: { enabled: document.getElementById("ntfyEnabled").checked, endpoint: document.getElementById("ntfyEndpoint").value.trim() },
        discord: { enabled: document.getElementById("discordEnabled").checked, webhook_url: document.getElementById("discordWebhook").value.trim() },
        telegram: { enabled: document.getElementById("tgEnabled").checked, token: document.getElementById("tgToken").value.trim(), chat_id: document.getElementById("tgChatId").value.trim() },
        line: { enabled: document.getElementById("lineEnabled").checked, channel_access_token: document.getElementById("lineToken").value.trim(), user_id: document.getElementById("lineUserId").value.trim() },
      },
    });
    showToast("推播設定已儲存");
    await refreshState();
  });
  document.getElementById("btnTestNotify").addEventListener("click", async () => {
    const { data } = await api("/api/notify/test", "POST", {});
    showToast(data.ok ? "已發送推播簡報" : data.error);
    await refreshState();
  });

  // Settings, Background Mode & Core Updater
  document.getElementById("setBgMode").addEventListener("change", (e) => updateBgModeFields(e.target.value));
  document.getElementById("btnCheckCore").addEventListener("click", async () => {
    const statusEl = document.getElementById("coreStatusText");
    statusEl.textContent = "查詢 GitHub 最新版中...";
    const { data } = await api("/api/core/status");
    if (data.ok && data.core) {
      const c = data.core;
      statusEl.textContent = c.error
        ? `本機：${c.core_file} (${c.local_version}) ｜ ${c.error}`
        : `本機：${c.core_file} (${c.local_version}) ｜ 官方最新：${c.latest_tag || "-"} ｜ FFmpeg：${c.has_ffmpeg ? "正常" : "未安裝"}`;
      document.getElementById("btnUpdateCoreExe").classList.toggle("hidden", !c.can_auto_download_exe);
    }
  });
  document.getElementById("btnUpdateCoreExe").addEventListener("click", async () => {
    showToast("正在從官方 Release 下載最新 aniGamerPlus.exe...");
    const { data } = await api("/api/core/update", "POST", {});
    showToast(data.message || (data.ok ? "更新完成" : "更新失敗"));
    await refreshState();
  });
  document.getElementById("btnSaveGeneral").addEventListener("click", async () => {
    const times = document.getElementById("setSchedulerTimes").value.split(",").map((s) => s.trim()).filter(Boolean);
    const { data } = await api("/api/settings/update", "POST", {
      default_resolution: document.getElementById("setDefaultRes").value,
      dedup_mode: document.getElementById("setDedupMode").value,
      upgrade_lower_resolution: document.getElementById("setUpgradeRes").checked,
      close_to_tray: document.getElementById("setCloseToTray").checked,
      background_mode: document.getElementById("setBgMode").value,
      tray_interval_minutes: parseInt(document.getElementById("setTrayMinutes").value || "60", 10),
      scheduler_times: times,
      core_update_policy: document.getElementById("setCorePolicy").value,
      apply_scheduler: true,
    });
    showToast(data.scheduler_message || "偏好與背景設定已儲存");
    await refreshState();
  });

  // Remote Auth & Biometrics
  document.getElementById("btnSaveRemoteAuth").addEventListener("click", async () => {
    formsInitialized = false;
    await api("/api/settings/update", "POST", {
      remote_auth_enabled: document.getElementById("setRemoteAuthEnabled").checked,
      remote_pin: document.getElementById("setRemotePin").value.trim(),
    });
    showToast("遠端存取安全設定已更新");
    await refreshState();
  });
  document.getElementById("btnRevokeDevices").addEventListener("click", async () => {
    await api("/api/settings/update", "POST", { revoke_devices: true });
    showToast("已登出所有外部已記住設備");
    await refreshState();
  });
  document.getElementById("btnPreviewLock").addEventListener("click", () => {
    state.previewLock = true;
    showLockScreen(true, state.data && state.data.auth.has_webauthn);
  });
  document.getElementById("btnRegisterBiometric").addEventListener("click", handleBiometricRegister);

  document.getElementById("lockSubmitBtn").addEventListener("click", handlePinUnlock);
  document.getElementById("lockPinInput").addEventListener("keydown", (e) => { if (e.key === "Enter") handlePinUnlock(); });
  document.getElementById("lockBiometricBtn").addEventListener("click", handleBiometricUnlock);

  // Backup Export / Import
  document.getElementById("btnExportBackup").addEventListener("click", async () => {
    const password = document.getElementById("backupPassword").value;
    const { data } = await api("/api/backup/export", "POST", { password, include_upstream: true, include_history: false });
    if (!data.ok) return showToast(data.error || "匯出失敗");
    showToast(`已匯出 (${data.cipher})，正在下載...`);
    const a = document.createElement("a");
    a.href = `/api/backup/download?name=${encodeURIComponent(data.filename)}`;
    a.download = data.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  document.getElementById("btnImportBackup").addEventListener("click", async () => {
    const fileInput = document.getElementById("backupFileInput");
    if (!fileInput.files || !fileInput.files[0]) return showToast("請先選擇 .anibak 檔案");
    const file = fileInput.files[0];
    const password = document.getElementById("backupPassword").value;
    const mode = document.getElementById("backupImportMode").value;
    const reader = new FileReader();
    reader.onload = async () => {
      const { data } = await api("/api/backup/import", "POST", {
        file_base64: reader.result,
        filename: file.name,
        password,
        mode,
      });
      if (data.ok) {
        formsInitialized = false;
        showToast(data.message || "匯入還原成功！");
        await refreshState();
      } else {
        showToast(data.error || "匯入失敗");
      }
    };
    reader.readAsDataURL(file);
  });

  refreshState();
  setInterval(refreshState, 2500);
});
