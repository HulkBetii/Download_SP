const els = {};
const selectedIds = new Set();
let eventSource = null;
let fallbackPollTimer = null;
let lastStateVersion = 0;
let inputsInitialized = false;
let renderedCount = 0;
let currentState = null;

const STORAGE_KEYS = {
  outputFolder: "video_downloader_output_folder",
  cookieFile: "video_downloader_cookie_file",
  useCookies: "video_downloader_use_cookies",
  optimizeMode: "video_downloader_optimize_mode",
  useBrowserCookies: "video_downloader_use_browser_cookies",
  browserName: "video_downloader_browser_name",
  browserProfile: "video_downloader_browser_profile",
  browserContainer: "video_downloader_browser_container",
  proxy: "video_downloader_proxy",
  impersonate: "video_downloader_impersonate",
  downloadArchive: "video_downloader_download_archive",
  checkFormats: "video_downloader_check_formats",
  formatSort: "video_downloader_format_sort",
  concurrentFragments: "video_downloader_concurrent_fragments",
  skipUnavailableFragments: "video_downloader_skip_unavailable_fragments",
};

function $(id) {
  return document.getElementById(id);
}

function cacheDom() {
  els.headerSummary = $("header-summary");
  els.headerPill = $("header-pill");
  els.queueSummary = $("queue-summary");
  els.selectionCount = $("selection-count");
  els.autoStage = $("auto-stage");
  els.queueBody = $("queue-body");
  els.candidateBody = $("candidate-body");
  els.candidateWrap = els.candidateBody.closest(".table-wrap");
  els.candidateEmpty = $("candidate-empty");
  els.diagnostics = $("diagnostics");
  els.log = $("log");
  els.footerStatus = $("footer-status");
  els.progress = $("progress");
  els.batchDetail = $("batch-detail");
  els.singleUrl = $("single-url");
  els.bulkUrls = $("bulk-urls");
  els.outputFolder = $("output-folder");
  els.chooseOutputFolder = $("choose-output-folder");
  els.useCookies = $("use-cookies");
  els.cookieFile = $("cookie-file");
  els.chooseCookieFile = $("choose-cookie-file");
  els.autoDownload = $("auto-download");
  els.useBrowserCookies = $("use-browser-cookies");
  els.browserName = $("browser-name");
  els.browserProfile = $("browser-profile");
  els.browserContainer = $("browser-container");
  els.browserCookieNote = $("browser-cookie-note");
  els.proxy = $("proxy");
  els.impersonate = $("impersonate");
  els.downloadArchive = $("download-archive");
  els.formatSort = $("format-sort");
  els.concurrentFragments = $("concurrent-fragments");
  els.checkFormats = $("check-formats");
  els.skipUnavailableFragments = $("skip-unavailable-fragments");
  els.capabilityPill = $("capability-pill");
  els.addUrl = $("add-url");
  els.pasteClipboard = $("paste-clipboard");
  els.analyzeUrl = $("analyze-url");
  els.inspectMedia = $("inspect-media");
  els.sniffMedia = $("sniff-media");
  els.addBulk = $("add-bulk");
  els.startDownload = $("start-download");
  els.removeSelected = $("remove-selected");
  els.clearAll = $("clear-all");
  els.clearLog = $("clear-log");
  els.modeRadios = Array.from(document.querySelectorAll('input[name="optimize-mode"]'));
  els.hero = $("hero");
  els.heroHint = $("hero-hint");
  els.setupStrip = $("setup-strip");
  els.setupMessage = $("setup-message");
  els.runSetup = $("run-setup");
  els.nowCard = $("now-card");
  els.nowTitle = $("now-title");
  els.queueDetails = $("queue-details");
}

async function init() {
  cacheDom();
  bindEvents();
  await fetchState();
  applySavedConfig();
  inputsInitialized = true;
  connectEventStream();
}

function bindEvents() {
  els.autoDownload.addEventListener("click", handlePrimaryDownload);
  els.runSetup.addEventListener("click", handleRunSetup);
  bindDropZone();
  els.addUrl.addEventListener("click", handleAddUrl);
  els.pasteClipboard.addEventListener("click", handlePasteClipboard);
  els.analyzeUrl.addEventListener("click", handleAnalyzeUrl);
  els.inspectMedia.addEventListener("click", handleInspectMedia);
  els.sniffMedia.addEventListener("click", handleSniffMedia);
  els.addBulk.addEventListener("click", handleAddBulk);
  els.startDownload.addEventListener("click", handleStartDownload);
  els.removeSelected.addEventListener("click", handleRemoveSelected);
  els.clearAll.addEventListener("click", handleClearAll);
  els.clearLog.addEventListener("click", handleClearLog);
  els.chooseOutputFolder.addEventListener("click", handleChooseOutputFolder);
  els.chooseCookieFile.addEventListener("click", handleChooseCookieFile);
  els.useCookies.addEventListener("change", () => {
    saveConfig();
    updateCookieFieldState();
  });
  els.useBrowserCookies.addEventListener("change", () => {
    saveConfig();
    updateAdvancedFieldState(Boolean(currentState?.running), currentState?.capabilities || {});
  });
  els.browserName.addEventListener("change", () => {
    updateAdvancedFieldState(Boolean(currentState?.running), currentState?.capabilities || {});
  });
  els.cookieFile.addEventListener("input", saveConfig);
  els.outputFolder.addEventListener("input", saveConfig);
  [
    els.useBrowserCookies,
    els.browserName,
    els.browserProfile,
    els.browserContainer,
    els.proxy,
    els.impersonate,
    els.downloadArchive,
    els.formatSort,
    els.concurrentFragments,
    els.checkFormats,
    els.skipUnavailableFragments,
  ].forEach((control) => {
    control.addEventListener("input", saveConfig);
    control.addEventListener("change", saveConfig);
  });
  els.singleUrl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      handleAddUrl();
    }
  });
  els.bulkUrls.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      handleAddBulk();
    }
  });
  els.modeRadios.forEach((radio) => radio.addEventListener("change", saveConfig));
  window.addEventListener("beforeunload", () => {
    if (eventSource) {
      eventSource.close();
    }
    if (fallbackPollTimer) {
      clearInterval(fallbackPollTimer);
    }
  });
}

function applySavedConfig() {
  const storedOutput = localStorage.getItem(STORAGE_KEYS.outputFolder);
  const storedCookie = localStorage.getItem(STORAGE_KEYS.cookieFile);
  const storedUseCookies = localStorage.getItem(STORAGE_KEYS.useCookies);
  const storedMode = localStorage.getItem(STORAGE_KEYS.optimizeMode);
  const storedUseBrowserCookies = localStorage.getItem(STORAGE_KEYS.useBrowserCookies);
  const storedBrowserName = localStorage.getItem(STORAGE_KEYS.browserName);
  const storedBrowserProfile = localStorage.getItem(STORAGE_KEYS.browserProfile);
  const storedBrowserContainer = localStorage.getItem(STORAGE_KEYS.browserContainer);
  const storedProxy = localStorage.getItem(STORAGE_KEYS.proxy);
  const storedImpersonate = localStorage.getItem(STORAGE_KEYS.impersonate);
  const storedDownloadArchive = localStorage.getItem(STORAGE_KEYS.downloadArchive);
  const storedCheckFormats = localStorage.getItem(STORAGE_KEYS.checkFormats);
  const storedFormatSort = localStorage.getItem(STORAGE_KEYS.formatSort);
  const storedConcurrentFragments = localStorage.getItem(STORAGE_KEYS.concurrentFragments);
  const storedSkipUnavailableFragments = localStorage.getItem(STORAGE_KEYS.skipUnavailableFragments);

  if (storedOutput) {
    els.outputFolder.value = storedOutput;
  }
  if (storedCookie !== null) {
    els.cookieFile.value = storedCookie;
  }
  if (storedUseCookies !== null) {
    els.useCookies.checked = storedUseCookies === "true";
  }
  if (storedMode) {
    const radio = els.modeRadios.find((item) => item.value === storedMode);
    if (radio) {
      radio.checked = true;
    }
  }
  if (storedUseBrowserCookies !== null) {
    els.useBrowserCookies.checked = storedUseBrowserCookies === "true";
  }
  if (storedBrowserName) {
    els.browserName.value = storedBrowserName;
  }
  if (storedBrowserProfile !== null) {
    els.browserProfile.value = storedBrowserProfile;
  }
  if (storedBrowserContainer !== null) {
    els.browserContainer.value = storedBrowserContainer;
  }
  if (storedProxy !== null) {
    els.proxy.value = storedProxy;
  }
  if (storedImpersonate !== null) {
    els.impersonate.value = storedImpersonate;
  }
  if (storedDownloadArchive !== null) {
    els.downloadArchive.value = storedDownloadArchive;
  }
  if (storedCheckFormats !== null) {
    els.checkFormats.checked = storedCheckFormats === "true";
  }
  if (storedFormatSort !== null) {
    els.formatSort.value = storedFormatSort;
  }
  if (storedConcurrentFragments !== null) {
    els.concurrentFragments.value = storedConcurrentFragments;
  }
  if (storedSkipUnavailableFragments !== null) {
    els.skipUnavailableFragments.checked = storedSkipUnavailableFragments === "true";
  }
  updateCookieFieldState(Boolean(currentState?.running));
}

function saveConfig() {
  localStorage.setItem(STORAGE_KEYS.outputFolder, els.outputFolder.value.trim());
  localStorage.setItem(STORAGE_KEYS.cookieFile, els.cookieFile.value.trim());
  localStorage.setItem(STORAGE_KEYS.useCookies, String(els.useCookies.checked));
  localStorage.setItem(STORAGE_KEYS.useBrowserCookies, String(els.useBrowserCookies.checked));
  localStorage.setItem(STORAGE_KEYS.browserName, els.browserName.value);
  localStorage.setItem(STORAGE_KEYS.browserProfile, els.browserProfile.value.trim());
  localStorage.setItem(STORAGE_KEYS.browserContainer, els.browserContainer.value.trim());
  localStorage.setItem(STORAGE_KEYS.proxy, els.proxy.value.trim());
  localStorage.setItem(STORAGE_KEYS.impersonate, els.impersonate.value.trim());
  localStorage.setItem(STORAGE_KEYS.downloadArchive, els.downloadArchive.value.trim());
  localStorage.setItem(STORAGE_KEYS.checkFormats, String(els.checkFormats.checked));
  localStorage.setItem(STORAGE_KEYS.formatSort, els.formatSort.value.trim());
  localStorage.setItem(STORAGE_KEYS.concurrentFragments, els.concurrentFragments.value.trim());
  localStorage.setItem(STORAGE_KEYS.skipUnavailableFragments, String(els.skipUnavailableFragments.checked));
  const selectedMode = getSelectedMode();
  if (selectedMode) {
    localStorage.setItem(STORAGE_KEYS.optimizeMode, selectedMode);
  }
}

async function fetchState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    applyState(payload.state, { forceInputSync: !inputsInitialized });
  } catch (error) {
    setFooterStatus(`Không thể tải trạng thái: ${error.message}`);
  }
}

function connectEventStream() {
  if (!("EventSource" in window)) {
    startFallbackPolling();
    return;
  }

  if (eventSource) {
    eventSource.close();
  }

  eventSource = new EventSource("/api/events");
  eventSource.addEventListener("state", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.version <= lastStateVersion) {
      return;
    }
    applyState(payload.state, { forceInputSync: !inputsInitialized, version: payload.version });
  });
  eventSource.onerror = () => {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    startFallbackPolling();
  };
}

function startFallbackPolling() {
  if (fallbackPollTimer) {
    return;
  }
  fallbackPollTimer = setInterval(fetchState, 2000);
}

function stopFallbackPolling() {
  if (fallbackPollTimer) {
    clearInterval(fallbackPollTimer);
    fallbackPollTimer = null;
  }
}

function applyState(state, options = {}) {
  currentState = state;
  if (typeof options.version === "number") {
    lastStateVersion = Math.max(lastStateVersion, options.version);
  }
  if (options.forceInputSync) {
    syncInputsFromState(state);
    inputsInitialized = true;
  }

  selectedIds.forEach((id) => {
    if (!state.items.some((item) => item.id === id)) {
      selectedIds.delete(id);
    }
  });

  renderSummary(state);
  renderDependencies(state.dependencies || {});
  renderNowCard(state);
  renderCapabilities(state.capabilities || {});
  renderQueue(state.items);
  renderCandidates(state.lastCandidates || [], state.autoJob || {});
  renderDiagnostics(state.diagnostics || []);
  renderAutoState(state.autoJob || {});
  renderLogs(state.logs);
  renderFooter(state);
  renderControls(state);

  if (state.running) {
    stopFallbackPolling();
  }
}

function syncInputsFromState(state) {
  const config = state.config || {};
  if (config.outputFolder) {
    els.outputFolder.value = config.outputFolder;
  } else if (!els.outputFolder.value) {
    els.outputFolder.value = "";
  }

  els.useCookies.checked = Boolean(config.useCookies);
  els.cookieFile.value = config.cookieFile || "";
  setSelectedMode(config.optimizeMode || "quality");
  els.useBrowserCookies.checked = Boolean(config.useBrowserCookies);
  els.browserName.value = config.browserName || "chrome";
  els.browserProfile.value = config.browserProfile || "";
  els.browserContainer.value = config.browserContainer || "";
  els.proxy.value = config.proxy || "";
  els.impersonate.value = config.impersonate || "";
  els.downloadArchive.value = config.downloadArchive || "";
  els.checkFormats.checked = Boolean(config.checkFormats);
  els.formatSort.value = config.formatSort || "";
  els.concurrentFragments.value = config.concurrentFragments || "";
  els.skipUnavailableFragments.checked = Boolean(config.skipUnavailableFragments);
}

function renderSummary(state) {
  // Ba chỗ này từng nhận cùng một chuỗi, nên cùng câu tóm tắt hiện ba lần trên
  // màn hình. Mỗi chỗ giờ nói một việc khác nhau.
  els.headerSummary.textContent = state.summary || "Sẵn sàng.";
  els.headerPill.textContent = state.progress?.detail || "0 / 0";

  const total = (state.items || []).length;
  els.queueSummary.textContent = total ? `${total} mục` : "Trống";
  els.selectionCount.textContent = `${selectedIds.size} mục được chọn`;
}

// The setup strip only appears when something is genuinely missing, so a
// healthy install shows nothing but the URL box and the button.
function renderDependencies(dependencies) {
  if (!els.setupStrip) {
    return;
  }
  // "Thiếu" và "có nhưng cũ" là hai chuyện khác nhau. Gộp chung khiến một
  // yt-dlp đang chạy tốt bị báo là thiếu, mâu thuẫn ngay với chip capability
  // bên cạnh đang hiện đúng số phiên bản của nó.
  const missing = [];
  Object.entries(dependencies.pythonPackages || {}).forEach(([name, present]) => {
    if (!present) {
      missing.push(name);
    }
  });
  if (dependencies.ffmpeg === false) {
    missing.push("ffmpeg");
  }

  const parts = [];
  if (missing.length) {
    parts.push(`Thiếu: ${missing.join(", ")}`);
  }
  if (dependencies.ytDlpStale) {
    const version = dependencies.ytDlpVersion ? ` (${dependencies.ytDlpVersion})` : "";
    parts.push(`Có bản yt-dlp mới hơn${version}`);
  }

  if (!parts.length) {
    els.setupStrip.hidden = true;
    return;
  }

  els.setupStrip.hidden = false;
  const frozen = Boolean(dependencies.frozen);
  els.setupMessage.textContent = frozen
    ? `${parts.join(" · ")}. Bản đóng gói không tự cài được.`
    : `${parts.join(" · ")}.`;
  els.runSetup.textContent = missing.length ? "Cài đặt" : "Cập nhật";
  els.runSetup.hidden = frozen;
}

function renderNowCard(state) {
  if (!els.nowCard) {
    return;
  }
  // Chỉ hiện khi thực sự có việc đang chạy. Điều kiện cũ còn tính cả lô đã
  // xong, nên một thẻ "Sẵn sàng · 0/0" nằm lại trên màn hình không làm gì.
  const running = Boolean(state.running);
  els.nowCard.hidden = !running;

  const autoJob = state.autoJob || {};
  const active = (state.items || []).find((item) => item.status === "Đang tải" || item.status === "Đang xử lý");
  if (els.nowTitle) {
    els.nowTitle.textContent = active
      ? hostFromUrl(active.url)
      : (autoJob.sourceUrl ? hostFromUrl(autoJob.sourceUrl) : "Sẵn sàng");
  }
}

function renderCapabilities(capabilities) {
  const parts = [];
  if (capabilities.ytDlpVersion) {
    parts.push(`yt-dlp ${capabilities.ytDlpVersion}`);
  }
  if (capabilities.ffmpegAvailable) {
    parts.push("ffmpeg");
  }
  if (capabilities.curlCffiAvailable) {
    parts.push("impersonate");
  }
  if (capabilities.browserCookiesAvailable) {
    parts.push("browser cookies");
  }
  els.capabilityPill.textContent = parts.length ? parts.join(" · ") : "Không rõ";
  els.impersonate.disabled = !capabilities.curlCffiAvailable;
  if (!capabilities.curlCffiAvailable) {
    els.impersonate.title = "curl_cffi chưa có sẵn, trường này sẽ bị bỏ qua.";
  } else {
    els.impersonate.title = "";
  }
}

function renderQueue(items) {
  const body = els.queueBody;
  body.textContent = "";

  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.dataset.id = String(item.id);

    if (selectedIds.has(item.id)) {
      row.classList.add("is-selected");
    }
    const rowStatusClass = statusRowClass(item.status);
    if (rowStatusClass) {
      row.classList.add(rowStatusClass);
    }

    const indexCell = document.createElement("td");
    indexCell.className = "index-cell";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedIds.has(item.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        selectedIds.add(item.id);
      } else {
        selectedIds.delete(item.id);
      }
      renderSelectionCount();
      renderQueue(items);
    });
    const indexLabel = document.createElement("span");
    indexLabel.textContent = String(item.index);
    indexCell.append(checkbox, indexLabel);

    const urlCell = document.createElement("td");
    urlCell.className = "queue-cell-url";
    urlCell.textContent = item.url;
    urlCell.title = item.url;

    const hostCell = document.createElement("td");
    hostCell.className = "queue-cell-host";
    hostCell.textContent = item.host;

    const statusCell = document.createElement("td");
    statusCell.className = "queue-cell-status";
    const statusChip = document.createElement("span");
    statusChip.className = `status-chip ${statusChipClass(item.status)}`;
    statusChip.textContent = item.status;
    statusCell.appendChild(statusChip);

    const progressCell = document.createElement("td");
    progressCell.className = "queue-cell-progress";
    progressCell.textContent = `${item.progress}%`;

    const updateCell = document.createElement("td");
    updateCell.textContent = item.updateText || "Sẵn sàng";

    row.addEventListener("click", (event) => {
      if (event.target.tagName === "INPUT") {
        return;
      }
      checkbox.checked = !checkbox.checked;
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    });

    row.append(indexCell, urlCell, hostCell, statusCell, progressCell, updateCell);
    fragment.appendChild(row);
  });

  body.appendChild(fragment);
  renderedCount = items.length;
  renderSelectionCount();
}

function renderSelectionCount() {
  els.selectionCount.textContent = `${selectedIds.size} mục được chọn`;
}

function renderCandidates(candidates, autoJob = {}) {
  const body = els.candidateBody;
  body.textContent = "";
  const selectedId = String(autoJob.selectedCandidateId || "");
  els.candidateEmpty.hidden = candidates.length > 0;
  els.candidateWrap.hidden = candidates.length === 0;

  const fragment = document.createDocumentFragment();
  candidates.forEach((candidate) => {
    const row = document.createElement("tr");
    if (selectedId && candidate.id === selectedId) {
      row.classList.add("is-selected");
    }
    if (candidate.drmSignals?.length) {
      row.classList.add("is-failed");
    }

    const scoreCell = document.createElement("td");
    scoreCell.className = "candidate-score";
    scoreCell.textContent = String(candidate.score ?? "-");

    const kindCell = document.createElement("td");
    kindCell.textContent = mediaKindLabel(candidate.kind);

    const hostCell = document.createElement("td");
    hostCell.className = "candidate-host";
    hostCell.textContent = candidate.host || hostFromUrl(candidate.url);
    hostCell.title = candidate.url || "";

    const statusCell = document.createElement("td");
    statusCell.textContent = candidate.status ? String(candidate.status) : "-";

    const mimeCell = document.createElement("td");
    mimeCell.className = "candidate-mime";
    mimeCell.textContent = candidate.mimeType || "-";

    const sizeCell = document.createElement("td");
    sizeCell.textContent = candidate.sizeText || "-";

    const reasonCell = document.createElement("td");
    reasonCell.className = "candidate-reasons";
    const reasons = Array.isArray(candidate.reasons) ? candidate.reasons : [];
    reasonCell.textContent = candidate.drmSignals?.length
      ? `DRM/license: ${candidate.drmSignals.join(", ")}`
      : reasons.slice(0, 3).join(" · ");
    reasonCell.title = [...reasons, ...(candidate.drmSignals || [])].join("\n");

    const actionCell = document.createElement("td");
    const button = document.createElement("button");
    button.className = "btn btn-ghost btn-small";
    button.type = "button";
    button.textContent = "Tải link này";
    button.disabled = Boolean(currentState?.running) || Boolean(candidate.drmSignals?.length);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      handleCandidateDownload(candidate);
    });
    actionCell.appendChild(button);

    row.append(scoreCell, kindCell, hostCell, statusCell, mimeCell, sizeCell, reasonCell, actionCell);
    fragment.appendChild(row);
  });
  body.appendChild(fragment);
}

function renderDiagnostics(diagnostics) {
  els.diagnostics.textContent = "";
  els.diagnostics.hidden = diagnostics.length === 0;
  const fragment = document.createDocumentFragment();
  diagnostics.forEach((item) => {
    const line = document.createElement("div");
    line.className = "diagnostic-line";
    const type = document.createElement("span");
    type.className = "diagnostic-type";
    type.textContent = item.type || "diag";
    const message = document.createElement("span");
    message.textContent = item.message || item.url || "";
    line.append(type, message);
    fragment.appendChild(line);
  });
  els.diagnostics.appendChild(fragment);
}

function renderAutoState(autoJob = {}) {
  const stage = autoJob.stage || "idle";
  const message = autoJob.message || "Sẵn sàng";
  els.autoStage.textContent = stage === "idle" ? "Auto: sẵn sàng" : `Auto: ${stageLabel(stage)} · ${message}`;
  els.autoStage.className = `meta-chip auto-stage ${autoJob.level === "error" ? "is-error" : ""}`;
}

function renderLogs(logs) {
  const log = els.log;
  log.textContent = "";
  const fragment = document.createDocumentFragment();
  logs.forEach((entry) => {
    const line = document.createElement("div");
    line.className = "log-entry";

    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = `[${entry.timestamp}]`;

    const message = document.createElement("span");
    message.className = `log-message ${logClass(entry.level)}`;
    message.textContent = entry.message;

    line.append(time, message);
    fragment.appendChild(line);
  });
  log.appendChild(fragment);
  log.scrollTop = log.scrollHeight;
}

function renderFooter(state) {
  els.footerStatus.textContent = state.footerStatus || "Sẵn sàng.";
  els.progress.value = state.progress?.value || 0;
  els.batchDetail.textContent = state.progress?.detail || "0 / 0";
}

function renderControls(state) {
  const running = Boolean(state.running);
  const canStart = Boolean(state.canStart);
  const hasSelection = selectedIds.size > 0;
  const hasItems = renderedCount > 0;

  els.addUrl.disabled = running;
  els.autoDownload.disabled = running;
  els.pasteClipboard.disabled = running;
  els.analyzeUrl.disabled = running;
  els.inspectMedia.disabled = running;
  els.sniffMedia.disabled = running;
  els.addBulk.disabled = running;
  els.startDownload.disabled = running || !canStart;
  els.removeSelected.disabled = running || !hasSelection;
  els.clearAll.disabled = running || !hasItems;
  els.clearLog.disabled = false;

  Array.from(document.querySelectorAll('input[name="optimize-mode"]')).forEach((radio) => {
    radio.disabled = running;
  });
  els.useCookies.disabled = running;
  els.outputFolder.disabled = running;
  els.chooseOutputFolder.disabled = running;
  updateCookieFieldState(running);
  updateAdvancedFieldState(running, state.capabilities || {});
  els.singleUrl.disabled = running;
  els.bulkUrls.disabled = running;
}

function updateCookieFieldState(running = false) {
  els.cookieFile.disabled = running || !els.useCookies.checked;
  els.chooseCookieFile.disabled = running;
}

function updateAdvancedFieldState(running = false, capabilities = {}) {
  const browserCookiesEnabled = els.useBrowserCookies.checked;
  const browserName = String(els.browserName.value || "").toLowerCase();
  const browserMayLockCookies = ["coccoc", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"].includes(browserName);
  const currentBrowserName = detectCurrentBrowserName();
  const sameBrowserSelected = currentBrowserName && browserName === currentBrowserName;
  els.useBrowserCookies.disabled = running;
  els.browserName.disabled = running || !browserCookiesEnabled;
  els.browserProfile.disabled = running || !browserCookiesEnabled;
  els.browserContainer.disabled = running || !browserCookiesEnabled;
  els.browserCookieNote.hidden = !(browserCookiesEnabled && browserMayLockCookies);
  if (browserCookiesEnabled && browserMayLockCookies) {
    els.browserCookieNote.textContent = sameBrowserSelected
      ? `Bạn đang mở UI bằng ${browserLabel(currentBrowserName)} và cũng chọn lấy cookies từ ${browserLabel(browserName)}. Hãy mở UI bằng Edge/Firefox hoặc xuất Cookie file rồi tắt mục này.`
      : `${browserLabel(browserName)} có thể khóa cookie database khi đang mở. Có thể giữ UI ở trình duyệt hiện tại, nhưng hãy đóng toàn bộ ${browserLabel(browserName)} trước khi probe, hoặc dùng Cookie file.`;
  }
  els.proxy.disabled = running;
  els.downloadArchive.disabled = running;
  els.formatSort.disabled = running;
  els.concurrentFragments.disabled = running;
  els.checkFormats.disabled = running;
  els.skipUnavailableFragments.disabled = running;
  els.impersonate.disabled = running || !capabilities.curlCffiAvailable;
}

function detectCurrentBrowserName() {
  const ua = navigator.userAgent || "";
  if (/Edg\//.test(ua)) {
    return "edge";
  }
  if (/CocCoc|coc_coc|CocCocBrowser/i.test(ua)) {
    return "coccoc";
  }
  if (/Firefox\//.test(ua)) {
    return "firefox";
  }
  if (/OPR\//.test(ua)) {
    return "opera";
  }
  if (/Chrome\//.test(ua) && !/Edg\//.test(ua) && !/OPR\//.test(ua)) {
    return "chrome";
  }
  if (/Safari\//.test(ua) && !/Chrome\//.test(ua)) {
    return "safari";
  }
  return "";
}

function browserLabel(name = "") {
  const labels = {
    brave: "Brave",
    chrome: "Chrome",
    chromium: "Chromium",
    coccoc: "Cốc Cốc",
    edge: "Edge",
    firefox: "Firefox",
    opera: "Opera",
    safari: "Safari",
    vivaldi: "Vivaldi",
  };
  return labels[String(name).toLowerCase()] || "trình duyệt này";
}

function mediaKindLabel(kind = "") {
  const labels = {
    hls: "HLS",
    dash: "DASH",
    video: "Video",
    audio: "Audio",
  };
  return labels[String(kind).toLowerCase()] || kind || "-";
}

function stageLabel(stage = "") {
  const labels = {
    idle: "sẵn sàng",
    probing: "probe yt-dlp",
    sniffing: "bắt media",
    selecting: "chọn stream",
    downloading: "đang tải",
    retrying: "thử lại",
    done: "hoàn tất",
    failed: "lỗi",
  };
  return labels[String(stage).toLowerCase()] || stage || "auto";
}

function hostFromUrl(url = "") {
  try {
    return new URL(url).host;
  } catch {
    return "-";
  }
}

async function handleAddUrl() {
  const url = els.singleUrl.value.trim();
  if (!url) {
    setFooterStatus("Vui lòng nhập URL.");
    return;
  }
  await postAction("/api/queue/add", { url, source: "ô nhập URL" });
  els.singleUrl.value = "";
}

async function handlePasteClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) {
      setFooterStatus("Clipboard trống.");
      return;
    }
    await postAction("/api/queue/add", { text, source: "clipboard" });
  } catch (error) {
    setFooterStatus(`Không đọc được clipboard: ${error.message}`);
  }
}

async function handleAddBulk() {
  const text = els.bulkUrls.value.trim();
  if (!text) {
    setFooterStatus("Vui lòng nhập ít nhất một URL.");
    return;
  }
  await postAction("/api/queue/add", { text, source: "vùng dán" });
  els.bulkUrls.value = "";
}

async function handleStartDownload() {
  await postAction("/api/download/start", buildDownloadOptionsPayload());
}

async function handleAnalyzeUrl() {
  const url = els.singleUrl.value.trim();
  if (!url) {
    setFooterStatus("Vui lòng nhập URL để phân tích.");
    return;
  }
  const payload = buildDownloadOptionsPayload({ url });
  const result = await postAction("/api/analyze", payload);
  const analysis = result?.result;
  if (analysis) {
    const title = analysis.title || url;
    const formatCount = Number(analysis.formatCount || 0);
    setFooterStatus(`Probe xong: ${title} · ${formatCount} format`);
  }
}

async function handleAutoDownload() {
  const url = els.singleUrl.value.trim();
  if (!url) {
    setFooterStatus("Vui lòng nhập URL để tải tự động.");
    return;
  }
  await postAction("/api/download/auto", buildDownloadOptionsPayload({
    url,
    browserMode: "visible",
    autoSelect: true,
  }));
}

// The single button in the hero. One link runs the auto ladder directly;
// several become a queue batch, so the user never has to pick a mode.
async function handlePrimaryDownload() {
  const raw = els.singleUrl.value.trim();
  if (!raw) {
    setFooterStatus("Hãy dán link video vào ô phía trên.");
    els.singleUrl.focus();
    return;
  }

  const urls = raw.split("\n").map((line) => line.trim()).filter(Boolean);
  if (urls.length === 1) {
    await handleAutoDownload();
    els.singleUrl.value = "";
    return;
  }

  const added = await postAction("/api/queue/add", { text: raw, source: "ô nhập" });
  if (!added) {
    return;
  }
  els.singleUrl.value = "";
  if (els.queueDetails) {
    els.queueDetails.open = true;
  }
  await postAction("/api/download/start", buildDownloadOptionsPayload());
}

async function handleRunSetup() {
  els.runSetup.disabled = true;
  els.setupMessage.textContent = "Đang kiểm tra và cài đặt...";
  try {
    await postAction("/api/setup/ensure", {});
  } finally {
    els.runSetup.disabled = false;
  }
}

function bindDropZone() {
  const zone = els.hero;
  if (!zone) {
    return;
  }
  ["dragenter", "dragover"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("hero-dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("hero-dragging");
    });
  });
  zone.addEventListener("drop", (event) => {
    const text = event.dataTransfer?.getData("text/uri-list")
      || event.dataTransfer?.getData("text/plain")
      || "";
    if (!text.trim()) {
      return;
    }
    const current = els.singleUrl.value.trim();
    els.singleUrl.value = current ? `${current}\n${text.trim()}` : text.trim();
    els.singleUrl.focus();
  });
}

async function handleInspectMedia() {
  const url = els.singleUrl.value.trim();
  if (!url) {
    setFooterStatus("Vui lòng nhập URL để inspect media.");
    return;
  }
  const result = await postAction("/api/media/inspect", buildDownloadOptionsPayload({
    url,
    browserMode: "visible",
  }));
  if (result?.state?.lastCandidates?.length) {
    setFooterStatus(`Đã tìm thấy ${result.state.lastCandidates.length} candidate media.`);
  }
}

async function handleSniffMedia() {
  const url = els.singleUrl.value.trim();
  if (!url) {
    setFooterStatus("Vui lòng nhập URL để bắt media.");
    return;
  }
  els.sniffMedia.disabled = true;
  setFooterStatus("Đang mở browser để bắt media. Hãy bấm play trong cửa sổ mới nếu cần.");
  try {
    const payload = buildDownloadOptionsPayload({ url, sniffTimeoutSeconds: 45 });
    if (els.browserName.value === "chrome" && detectCurrentBrowserName() === "coccoc") {
      payload.browserName = "coccoc";
      els.browserName.value = "coccoc";
      saveConfig();
      updateAdvancedFieldState(Boolean(currentState?.running), currentState?.capabilities || {});
    }
    const result = await postAction("/api/media/sniff", payload);
    const sniff = result?.result;
    if (sniff) {
      const count = Number(sniff.mediaUrls?.length || 0);
      const added = Number(sniff.queue?.added || 0);
      setFooterStatus(count ? `Bắt được ${count} link media, đã thêm ${added} link mới vào hàng đợi.` : "Chưa bắt được link media. Chạy lại và bấm play trong cửa sổ browser mới.");
    }
  } finally {
    els.sniffMedia.disabled = Boolean(currentState?.running);
  }
}

async function handleCandidateDownload(candidate) {
  if (!candidate?.url) {
    setFooterStatus("Candidate không hợp lệ.");
    return;
  }
  await postAction("/api/media/download", buildDownloadOptionsPayload({
    url: candidate.url,
    candidateId: candidate.id,
    sourceUrl: candidate.pageUrl || els.singleUrl.value.trim(),
    browserMode: "visible",
  }));
}

function buildDownloadOptionsPayload(extra = {}) {
  return {
    outputFolder: els.outputFolder.value.trim(),
    useCookies: els.useCookies.checked,
    cookieFile: els.cookieFile.value.trim(),
    optimizeMode: getSelectedMode(),
    useBrowserCookies: els.useBrowserCookies.checked,
    browserName: els.browserName.value,
    browserProfile: els.browserProfile.value.trim(),
    browserContainer: els.browserContainer.value.trim(),
    proxy: els.proxy.value.trim(),
    impersonate: els.impersonate.value.trim(),
    downloadArchive: els.downloadArchive.value.trim(),
    checkFormats: els.checkFormats.checked,
    formatSort: els.formatSort.value.trim(),
    concurrentFragments: els.concurrentFragments.value.trim(),
    skipUnavailableFragments: els.skipUnavailableFragments.checked,
    browserMode: "visible",
    ...extra,
  };
}

async function handleRemoveSelected() {
  if (!selectedIds.size) {
    setFooterStatus("Chưa chọn mục nào.");
    return;
  }
  await postAction("/api/queue/remove", { ids: Array.from(selectedIds) });
  selectedIds.clear();
}

async function handleClearAll() {
  if (!renderedCount) {
    setFooterStatus("Hàng đợi trống.");
    return;
  }
  if (!window.confirm("Bạn có chắc muốn xóa toàn bộ hàng đợi?")) {
    return;
  }
  await postAction("/api/queue/clear", {});
  selectedIds.clear();
}

async function handleClearLog() {
  await postAction("/api/log/clear", {});
}

async function handleChooseOutputFolder() {
  const path = await choosePath("/api/dialog/output-folder", els.outputFolder.value.trim());
  if (!path) {
    return;
  }
  els.outputFolder.value = path;
  saveConfig();
  setFooterStatus("Đã chọn thư mục lưu.");
}

async function handleChooseCookieFile() {
  const path = await choosePath("/api/dialog/cookie-file", els.cookieFile.value.trim());
  if (!path) {
    return;
  }
  els.useCookies.checked = true;
  els.cookieFile.value = path;
  updateCookieFieldState(Boolean(currentState?.running));
  saveConfig();
  setFooterStatus("Đã chọn cookie file.");
}

async function choosePath(path, initialPath = "") {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initialPath }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.path || "";
  } catch (error) {
    setFooterStatus(error.message);
    return "";
  }
}

async function postAction(path, body) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    if (payload.state) {
      applyState(payload.state, { forceInputSync: false });
    }
    saveConfig();
    setFooterStatus(payload.state?.footerStatus || "Hoàn tất.");
    return payload;
  } catch (error) {
    if (isBrowserCookieDatabaseError(error.message)) {
      disableBrowserCookiesAfterLockError();
    }
    setFooterStatus(error.message);
    return null;
  }
}

function isBrowserCookieDatabaseError(message = "") {
  const text = String(message).toLowerCase();
  return text.includes("cookie database") && (text.includes("khoa") || text.includes("locked") || text.includes("could not copy"));
}

function disableBrowserCookiesAfterLockError() {
  if (!els.useBrowserCookies.checked) {
    return;
  }
  els.useBrowserCookies.checked = false;
  updateAdvancedFieldState(Boolean(currentState?.running), currentState?.capabilities || {});
  saveConfig();
}

function getSelectedMode() {
  const checked = els.modeRadios.find((radio) => radio.checked);
  return checked ? checked.value : "quality";
}

function setSelectedMode(mode) {
  const targetMode = mode || "quality";
  const radio = els.modeRadios.find((item) => item.value === targetMode) || els.modeRadios[2];
  if (radio) {
    radio.checked = true;
  }
}

function setFooterStatus(message) {
  const text = message || "";
  els.footerStatus.textContent = text;
  els.footerStatus.title = text;
}

function statusRowClass(status) {
  switch (status) {
    case "Hoàn tất":
      return "is-done";
    case "Lỗi":
      return "is-failed";
    case "Đang tải":
      return "is-running";
    case "Đang xử lý":
      return "is-processing";
    default:
      return "";
  }
}

function statusChipClass(status) {
  switch (status) {
    case "Hoàn tất":
      return "is-done";
    case "Lỗi":
      return "is-failed";
    case "Đang tải":
      return "is-running";
    case "Đang xử lý":
      return "is-processing";
    default:
      return "";
  }
}

function logClass(level) {
  switch (level) {
    case "success":
      return "is-success";
    case "warning":
      return "is-warning";
    case "error":
      return "is-error";
    default:
      return "";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  init();
});
