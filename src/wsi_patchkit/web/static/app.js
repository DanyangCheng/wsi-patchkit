(() => {
  "use strict";

  const cropSizeStorageKey = "wsi-patchkit.crop-size";
  const menuButton = document.querySelector("#slide-menu-button");
  const menu = document.querySelector("#slide-menu");
  const currentSlideLabel = document.querySelector("#current-slide");
  const slideFilter = document.querySelector("#slide-filter");
  const slideCount = document.querySelector("#slide-count");
  const slideList = document.querySelector("#slide-list");
  const slideEmpty = document.querySelector("#slide-empty");
  const slideUploadButton = document.querySelector("#slide-upload-button");
  const slideUploadInput = document.querySelector("#slide-upload-input");
  const slideUploadStatus = document.querySelector("#slide-upload-status");
  const slider = document.querySelector("#zoom-slider");
  const zoomLabel = document.querySelector("#zoom-label");
  const coordinateLabel = document.querySelector("#coordinate-label");
  const emptyState = document.querySelector("#empty-state");
  const statusMessage = document.querySelector("#status-message");
  const scale = document.querySelector("#scale");
  const scaleLabel = document.querySelector("#scale-label");
  const scaleBar = document.querySelector("#scale-bar");
  const cropTool = document.querySelector("#crop-tool");
  const cropPanel = document.querySelector("#crop-panel");
  const cropClose = document.querySelector("#crop-close");
  const cropX = document.querySelector("#crop-x");
  const cropY = document.querySelector("#crop-y");
  const cropWidth = document.querySelector("#crop-width");
  const cropHeight = document.querySelector("#crop-height");
  const cropLevel = document.querySelector("#crop-level");
  const cropLevelMpp = document.querySelector("#crop-level-mpp");
  const cropFormat = document.querySelector("#crop-format");
  const cropFilename = document.querySelector("#crop-filename");
  const cropSave = document.querySelector("#crop-save");
  const cropStatus = document.querySelector("#crop-status");
  const cropOverlay = document.querySelector("#crop-overlay");
  const cropOverlaySize = document.querySelector("#crop-overlay-size");
  const overlayTool = document.querySelector("#overlay-tool");
  const overlayPanel = document.querySelector("#overlay-panel");
  const overlayClose = document.querySelector("#overlay-close");
  const overlayList = document.querySelector("#overlay-list");

  let slides = new Map();
  let currentSlide = null;
  let viewer = null;
  let openSequence = 0;
  let slideRequestController = null;
  const expandedFolders = new Set();
  let cropActive = false;
  let cropOverlayAdded = false;
  let overlayItems = new Map();

  function makePanelMovable(panel) {
    const handle = panel.querySelector("[data-panel-drag-handle]");
    if (!handle) return;
    let offsetX = 0;
    let offsetY = 0;

    const move = (event) => {
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      const left = Math.min(
        Math.max(0, event.clientX - offsetX),
        Math.max(0, window.innerWidth - width),
      );
      const top = Math.min(
        Math.max(0, event.clientY - offsetY),
        Math.max(0, window.innerHeight - height),
      );
      panel.style.left = `${left}px`;
      panel.style.top = `${top}px`;
    };

    const end = (event) => {
      handle.classList.remove("dragging");
      if (handle.hasPointerCapture(event.pointerId)) {
        handle.releasePointerCapture(event.pointerId);
      }
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
    };

    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button, input, select")) return;
      const bounds = panel.getBoundingClientRect();
      panel.style.position = "fixed";
      panel.style.left = `${bounds.left}px`;
      panel.style.top = `${bounds.top}px`;
      panel.style.right = "auto";
      offsetX = event.clientX - bounds.left;
      offsetY = event.clientY - bounds.top;
      handle.classList.add("dragging");
      handle.setPointerCapture(event.pointerId);
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", end);
      handle.addEventListener("pointercancel", end);
      event.preventDefault();
    });
  }

  for (const panel of document.querySelectorAll(
    ".slide-menu, .overlay-panel, .crop-panel",
  )) {
    makePanelMovable(panel);
  }

  function loadCropSize() {
    try {
      const value = JSON.parse(localStorage.getItem(cropSizeStorageKey));
      if (
        Number.isInteger(value?.width) &&
        value.width > 0 &&
        Number.isInteger(value?.height) &&
        value.height > 0
      ) {
        return value;
      }
    } catch (_) {
      // Storage may be unavailable or contain data from an older version.
    }
    return { width: 1024, height: 1024 };
  }

  function saveCropSize(size) {
    try {
      localStorage.setItem(cropSizeStorageKey, JSON.stringify(size));
    } catch (_) {
      // Keep the in-memory preference when persistent storage is unavailable.
    }
  }

  let preferredCropSize = loadCropSize();
  let cropRegion = { x: 0, y: 0, ...preferredCropSize };
  const pendingCropJobs = new Map();
  let latestCropResult = null;

  function showStatus(message, loading = false) {
    statusMessage.textContent = message;
    emptyState.hidden = false;
    const spinner = emptyState.querySelector(".spinner");
    spinner.hidden = !loading;
  }

  function hideStatus() {
    emptyState.hidden = true;
  }

  function currentItem() {
    return viewer && viewer.world.getItemCount() ? viewer.world.getItemAt(0) : null;
  }

  function overlayTileSource(slideId, overlayId, revision, style) {
    return (
      `/iiif/3/${encodeURIComponent(slideId)}/overlays/` +
      `${encodeURIComponent(overlayId)}/revision/${encodeURIComponent(revision)}/` +
      `style/${style}/info.json`
    );
  }

  function setOverlayOpacity(overlayId, opacity) {
    const entry = overlayItems.get(overlayId);
    if (!entry) return;
    entry.opacity = opacity;
    if (entry.item) entry.item.setOpacity(opacity);
  }

  function loadOverlay(record, opacity, style) {
    const existing = overlayItems.get(record.id);
    if (existing?.style === style) {
      setOverlayOpacity(record.id, opacity);
      return;
    }
    const slideId = currentSlide?.id;
    if (!slideId) return;
    if (existing?.item) viewer.world.removeItem(existing.item);
    const entry = { style, opacity, item: null };
    overlayItems.set(record.id, entry);
    viewer.addTiledImage({
      tileSource: overlayTileSource(slideId, record.id, record.revision, style),
      opacity,
      index: viewer.world.getItemCount(),
      success: (event) => {
        if (currentSlide?.id !== slideId || overlayItems.get(record.id) !== entry) {
          viewer.world.removeItem(event.item);
          return;
        }
        entry.item = event.item;
        event.item.setOpacity(entry.opacity);
      },
      error: () => {
        if (currentSlide?.id === slideId && overlayItems.get(record.id) === entry) {
          overlayItems.delete(record.id);
          const checkbox = overlayList.querySelector(
            `input[data-overlay-id="${CSS.escape(record.id)}"]`,
          );
          if (checkbox) checkbox.checked = false;
        }
      },
    });
  }

  function populateOverlayControls() {
    overlayItems = new Map();
    const overlays = currentSlide?.overlays || [];
    overlayTool.disabled = overlays.length === 0;
    if (!overlays.length) overlayPanel.hidden = true;
    const rows = overlays.map((record) => {
      const row = document.createElement("div");
      row.className = "overlay-row";
      const header = document.createElement("div");
      header.className = "overlay-row-header";
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(record.initially_visible);
      checkbox.dataset.overlayId = record.id;
      label.append(checkbox, document.createTextNode(record.display_name || record.id));
      header.append(label);

      const opacityLabel = document.createElement("label");
      opacityLabel.className = "overlay-opacity";
      opacityLabel.append(document.createTextNode("透明度"));
      const opacity = document.createElement("input");
      opacity.type = "range";
      opacity.min = "0";
      opacity.max = "1";
      opacity.step = "0.01";
      opacity.value = String(record.default_opacity ?? 0.7);
      opacityLabel.append(opacity);

      const legend = document.createElement("div");
      legend.className = "overlay-legend";
      const classSettings = (record.classes || []).map((item) => ({
        item,
        color: item.rgba.slice(0, 3),
        alpha: item.rgba[3] || 255,
        visible: item.rgba[3] > 0,
        checkbox: null,
      }));
      const styleValue = () => classSettings.map(({ color, alpha, visible }) =>
        [...color, visible ? alpha : 0]
          .map((channel) => channel.toString(16).padStart(2, "0"))
          .join(""),
      ).join("");
      const updateOverlay = () => {
        if (checkbox.checked) loadOverlay(record, Number(opacity.value), styleValue());
      };
      const classActions = document.createElement("div");
      classActions.className = "overlay-class-actions";
      const selectAll = document.createElement("button");
      selectAll.type = "button";
      selectAll.textContent = "全选";
      selectAll.setAttribute("aria-label", `${record.display_name || record.id} 类别全选`);
      selectAll.addEventListener("click", () => {
        for (const setting of classSettings) {
          setting.visible = true;
          setting.checkbox.checked = true;
        }
        updateOverlay();
      });
      const invertSelection = document.createElement("button");
      invertSelection.type = "button";
      invertSelection.textContent = "反选";
      invertSelection.setAttribute("aria-label", `${record.display_name || record.id} 类别反选`);
      invertSelection.addEventListener("click", () => {
        for (const setting of classSettings) {
          setting.visible = !setting.visible;
          setting.checkbox.checked = setting.visible;
        }
        updateOverlay();
      });
      classActions.append(selectAll, invertSelection);
      for (const setting of classSettings) {
        const entry = document.createElement("div");
        entry.className = "overlay-class";
        const classLabel = document.createElement("label");
        const visible = document.createElement("input");
        visible.type = "checkbox";
        visible.checked = setting.visible;
        setting.checkbox = visible;
        visible.setAttribute("aria-label", `显示 ${setting.item.name}`);
        classLabel.append(visible, document.createTextNode(setting.item.name));
        const color = document.createElement("input");
        color.type = "color";
        color.value = `#${setting.color.map((channel) =>
          channel.toString(16).padStart(2, "0"),
        ).join("")}`;
        color.setAttribute("aria-label", `${setting.item.name} 颜色`);
        visible.addEventListener("change", () => {
          setting.visible = visible.checked;
          updateOverlay();
        });
        color.addEventListener("change", () => {
          setting.color = [1, 3, 5].map((offset) =>
            Number.parseInt(color.value.slice(offset, offset + 2), 16),
          );
          updateOverlay();
        });
        entry.append(classLabel, color);
        legend.append(entry);
      }

      checkbox.addEventListener("change", () => {
        const value = Number(opacity.value);
        if (checkbox.checked) loadOverlay(record, value, styleValue());
        else setOverlayOpacity(record.id, 0);
      });
      opacity.addEventListener("input", () => {
        if (checkbox.checked) {
          const value = Number(opacity.value);
          loadOverlay(record, value, styleValue());
          setOverlayOpacity(record.id, value);
        }
      });
      row.append(header, opacityLabel, classActions, legend);
      return row;
    });
    overlayList.replaceChildren(...rows);
  }

  function loadInitiallyVisibleOverlays() {
    for (const record of currentSlide?.overlays || []) {
      if (record.initially_visible) {
        const style = (record.classes || []).map((item) =>
          item.rgba.map((channel) => channel.toString(16).padStart(2, "0")).join(""),
        ).join("");
        loadOverlay(record, Number(record.default_opacity ?? 0.7), style);
      }
    }
  }

  function niceScaleLength(targetMicrometres) {
    const exponent = 10 ** Math.floor(Math.log10(targetMicrometres));
    const normalized = targetMicrometres / exponent;
    const step = normalized < 2 ? 1 : normalized < 5 ? 2 : 5;
    return step * exponent;
  }

  function updateViewportStatus() {
    const item = currentItem();
    if (!item || !currentSlide) return;
    const contentSize = item.getContentSize();
    if (contentSize.x !== currentSlide.width || contentSize.y !== currentSlide.height) {
      zoomLabel.textContent = "缩放 —";
      coordinateLabel.textContent = "x — · y —";
      scale.hidden = true;
      return;
    }

    const imageZoom = item.viewportToImageZoom(viewer.viewport.getZoom(true));
    if (!Number.isFinite(imageZoom) || imageZoom <= 0) {
      zoomLabel.textContent = "缩放 —";
      scale.hidden = true;
      return;
    }
    slider.value = String(Math.log2(imageZoom));

    if (!currentSlide?.mpp) {
      zoomLabel.textContent = `缩放 ${(imageZoom * 100).toFixed(0)}%`;
      scale.hidden = true;
      return;
    }

    const baseMpp = (currentSlide.mpp[0] + currentSlide.mpp[1]) / 2;
    const screenMpp = baseMpp / imageZoom;
    if (!Number.isFinite(screenMpp) || screenMpp <= 0) {
      scale.hidden = true;
      return;
    }
    zoomLabel.textContent =
      `缩放 ${(imageZoom * 100).toFixed(0)}% · ${screenMpp.toFixed(3)} µm/px`;

    const physicalLength = niceScaleLength(screenMpp * 120);
    const barWidth = Math.max(45, Math.min(180, physicalLength / screenMpp));
    scaleBar.style.width = `${barWidth}px`;
    scaleLabel.textContent =
      physicalLength >= 1000
        ? `${(physicalLength / 1000).toLocaleString()} mm`
        : `${physicalLength.toLocaleString()} µm`;
    scale.hidden = false;
  }

  function setImageZoom(imageZoom) {
    const item = currentItem();
    if (!item) return;
    const viewportZoom = item.imageToViewportZoom(imageZoom);
    viewer.viewport.zoomTo(viewportZoom);
    viewer.viewport.applyConstraints();
  }

  function setCropStatus(message, type = "") {
    cropStatus.textContent = message;
    cropStatus.className = `crop-status ${type}`.trim();
  }

  function updateCropQueueStatus() {
    if (pendingCropJobs.size) {
      const jobs = [...pendingCropJobs.values()];
      const running = jobs.filter((job) => job.status === "running").length;
      const queued = jobs.filter((job) => job.status === "queued").length;
      const checking = jobs.length - running - queued;
      const parts = [];
      if (running) parts.push(`${running} 个处理中`);
      if (queued) parts.push(`${queued} 个排队中`);
      if (checking) parts.push(`${checking} 个等待状态`);
      setCropStatus(`状态：${parts.join(" · ")}`);
      return;
    }
    if (latestCropResult) {
      setCropStatus(latestCropResult.message, latestCropResult.type);
    }
  }

  async function pollCropJob(slideId, jobId) {
    const job = pendingCropJobs.get(jobId);
    if (!job) return;
    try {
      const response = await fetch(
        `/api/slides/${encodeURIComponent(slideId)}/crops/${encodeURIComponent(jobId)}`,
      );
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      if (result.status === "completed") {
        pendingCropJobs.delete(jobId);
        latestCropResult = {
          message: `已保存到服务器：${result.filename}`,
          type: "success",
        };
        updateCropQueueStatus();
        return;
      }
      if (result.status === "failed") {
        pendingCropJobs.delete(jobId);
        latestCropResult = {
          message: `${result.filename} 保存失败：${result.error || "未知错误"}`,
          type: "error",
        };
        updateCropQueueStatus();
        return;
      }
      job.status = result.status;
      updateCropQueueStatus();
    } catch (error) {
      job.status = "checking";
      updateCropQueueStatus();
    }
    window.setTimeout(() => pollCropJob(slideId, jobId), 500);
  }

  function updateCropOverlay() {
    const item = currentItem();
    if (!item || !cropActive) return;
    const level = selectedCropLevel();
    if (!level) return;
    const imageRect = new OpenSeadragon.Rect(
      cropRegion.x * level.downsample[0],
      cropRegion.y * level.downsample[1],
      cropRegion.width * level.downsample[0],
      cropRegion.height * level.downsample[1],
    );
    const viewportRect = item.imageToViewportRectangle(imageRect);
    cropOverlay.hidden = false;
    cropOverlaySize.textContent =
      `Level ${cropLevel.value || 0}: ${cropRegion.width.toLocaleString()} × ${cropRegion.height.toLocaleString()} px`;
    if (cropOverlayAdded) {
      viewer.updateOverlay(cropOverlay, viewportRect);
    } else {
      viewer.addOverlay({ element: cropOverlay, location: viewportRect });
      cropOverlayAdded = true;
    }
  }

  function selectedCropLevel() {
    return currentSlide?.levels?.find(
      (level) => level.level === Number(cropLevel.value),
    );
  }

  function updateCropLevelMpp() {
    const level = selectedCropLevel();
    const mpp = level?.mpp || (
      currentSlide?.mpp && level?.downsample
        ? currentSlide.mpp.map((value, axis) => value * level.downsample[axis])
        : null
    );
    if (!mpp) {
      cropLevelMpp.textContent = "MPP 未知";
      return;
    }
    const format = (value) => Number(value.toPrecision(4)).toString();
    cropLevelMpp.textContent = mpp[0] === mpp[1]
      ? `MPP ${format(mpp[0])} µm/px`
      : `MPP x ${format(mpp[0])} · y ${format(mpp[1])} µm/px`;
  }

  function populateCropLevels() {
    if (!currentSlide) return;
    const previous = cropLevel.value;
    const levels = currentSlide.levels || [];
    cropLevel.replaceChildren(
      ...levels.map((level) => {
        const option = document.createElement("option");
        option.value = String(level.level);
        option.textContent = `Level ${level.level} (${level.width} × ${level.height})`;
        return option;
      }),
    );
    cropLevel.value = levels.some((level) => String(level.level) === previous)
      ? previous
      : "0";
    updateCropLevelMpp();
  }

  function setCropRegion(region, updateInputs = true) {
    const level = selectedCropLevel();
    if (!level) return;
    const width = Math.max(1, Math.min(Math.round(region.width), level.width));
    const height = Math.max(1, Math.min(Math.round(region.height), level.height));
    cropRegion = {
      x: Math.max(0, Math.min(Math.round(region.x), level.width - width)),
      y: Math.max(0, Math.min(Math.round(region.y), level.height - height)),
      width,
      height,
    };
    if (updateInputs) {
      cropX.value = String(cropRegion.x);
      cropY.value = String(cropRegion.y);
      cropWidth.value = String(cropRegion.width);
      cropHeight.value = String(cropRegion.height);
      cropX.max = String(level.width - cropRegion.width);
      cropY.max = String(level.height - cropRegion.height);
      cropWidth.max = String(level.width);
      cropHeight.max = String(level.height);
    }
    updateCropOverlay();
  }

  function centerCropAt(imagePoint) {
    const level = selectedCropLevel();
    if (!level) return;
    setCropRegion({
      ...cropRegion,
      x: imagePoint.x / level.downsample[0] - cropRegion.width / 2,
      y: imagePoint.y / level.downsample[1] - cropRegion.height / 2,
    });
  }

  function initializeCropRegion() {
    const item = currentItem();
    const level = selectedCropLevel();
    if (!item || !level) return;
    const center = item.viewportToImageCoordinates(viewer.viewport.getCenter(true));
    const width = Math.min(preferredCropSize.width, level.width);
    const height = Math.min(preferredCropSize.height, level.height);
    setCropRegion({
      x: center.x / level.downsample[0] - width / 2,
      y: center.y / level.downsample[1] - height / 2,
      width,
      height,
    });
  }

  function setCropActive(active) {
    cropActive = Boolean(active && currentItem() && currentSlide);
    cropTool.setAttribute("aria-pressed", String(cropActive));
    cropPanel.hidden = !cropActive;
    if (cropActive) {
      initializeCropRegion();
      setCropStatus("");
    } else {
      if (cropOverlayAdded) viewer.removeOverlay(cropOverlay);
      cropOverlayAdded = false;
      cropOverlay.hidden = true;
    }
  }

  function syncCropInputs() {
    const values = [cropX, cropY, cropWidth, cropHeight].map((input) =>
      Number(input.value),
    );
    if (!values.every(Number.isFinite)) return;
    setCropRegion({
      x: values[0],
      y: values[1],
      width: values[2],
      height: values[3],
    });
    preferredCropSize = {
      width: cropRegion.width,
      height: cropRegion.height,
    };
    saveCropSize(preferredCropSize);
    setCropStatus("");
  }

  async function saveCrop() {
    if (!currentSlide || !cropActive) return;
    setCropStatus(`正在提交 Level ${cropLevel.value || 0} 裁剪任务…`);
    try {
      const slideId = currentSlide.id;
      const response = await fetch(
        `/api/slides/${encodeURIComponent(slideId)}/crops`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...cropRegion,
            level: Number(cropLevel.value),
            format: cropFormat.value,
            filename: cropFilename.value.trim() || null,
          }),
        },
      );
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      cropFilename.value = "";
      pendingCropJobs.set(result.job_id, {
        filename: result.filename,
        status: result.status,
      });
      updateCropQueueStatus();
      pollCropJob(slideId, result.job_id);
    } catch (error) {
      latestCropResult = {
        message: `任务提交失败：${error.message}`,
        type: "error",
      };
      updateCropQueueStatus();
    }
  }

  async function openSlide(slideId) {
    const record = slides.get(slideId);
    if (!record) return;
    const sequence = ++openSequence;
    slideRequestController?.abort();
    const controller = new AbortController();
    slideRequestController = controller;
    if (viewer) {
      if (cropOverlayAdded) viewer.removeOverlay(cropOverlay);
      cropOverlayAdded = false;
      cropOverlay.hidden = true;
      viewer.close();
      overlayItems = new Map();
    }
    currentSlide = null;
    currentSlideLabel.textContent = record.path || slideId;
    for (const item of slideList.querySelectorAll("button")) {
      const selected = item.dataset.slideId === slideId;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-current", selected ? "true" : "false");
    }
    const selectedButton = [...slideList.querySelectorAll(".slide-entry button")]
      .find((button) => button.dataset.slideId === slideId);
    for (let parent = selectedButton?.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") {
        parent.open = true;
        expandedFolders.add(parent.dataset.folderPath);
      }
      if (parent === slideList) break;
    }
    closeSlideMenu();
    showStatus(`正在打开 ${slideId}…`, true);
    coordinateLabel.textContent = "x — · y —";
    zoomLabel.textContent = "缩放 —";
    scale.hidden = true;
    try {
      const id = encodeURIComponent(slideId);
      const options = { signal: controller.signal, cache: "no-store" };
      // Fetch IIIF ourselves so old dimensions in the browser HTTP cache cannot
      // initialize the viewer independently of the crop/MPP metadata.
      const response = await fetch(`/api/slides/${id}`, options);
      if (!response.ok) throw new Error(`元数据 HTTP ${response.status}`);
      const slideMetadata = await response.json();
      // The first request warms the server metadata cache; fetching IIIF next
      // avoids opening a second cold reader just to obtain the same metadata.
      const infoResponse = await fetch(`/iiif/3/${id}/info.json?metadata=2`, options);
      if (!infoResponse.ok) throw new Error(`IIIF HTTP ${infoResponse.status}`);
      const info = await infoResponse.json();
      if (sequence !== openSequence) return;
      for (const dimensions of [slideMetadata, info]) {
        if (
          !Number.isSafeInteger(dimensions.width) || dimensions.width <= 0 ||
          !Number.isSafeInteger(dimensions.height) || dimensions.height <= 0
        ) {
          throw new Error("切片尺寸无效");
        }
      }
      if (info.width !== slideMetadata.width || info.height !== slideMetadata.height) {
        throw new Error("IIIF 尺寸与切片元数据不一致");
      }
      const metadata = { ...slideMetadata, path: record.path };
      slides.set(slideId, metadata);
      currentSlide = metadata;
      populateCropLevels();
      populateOverlayControls();
      viewer.open(info);
    } catch (error) {
      if (error.name !== "AbortError" && sequence === openSequence) {
        showStatus(`切片加载失败：${error.message}`);
      }
    } finally {
      if (slideRequestController === controller) slideRequestController = null;
    }
  }

  function closeSlideMenu() {
    menu.hidden = true;
    menuButton.setAttribute("aria-expanded", "false");
  }

  function setSlideMenuOpen(open) {
    menu.hidden = !open;
    menuButton.setAttribute("aria-expanded", String(open));
    if (open) {
      slideFilter.focus();
      const selected = slideList.querySelector("button.selected");
      selected?.scrollIntoView({ block: "nearest" });
    }
  }

  function filterSlides() {
    const query = slideFilter.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const item of slideList.querySelectorAll(".slide-entry")) {
      const matches = item.firstElementChild.dataset.search.includes(query);
      item.hidden = !matches;
      if (matches) visible += 1;
    }
    const folders = [...slideList.querySelectorAll(".slide-folder")].reverse();
    for (const item of folders) {
      item.hidden = !item.querySelector(".slide-entry:not([hidden])");
      const details = item.firstElementChild;
      details.open = query ? !item.hidden : expandedFolders.has(details.dataset.folderPath);
    }
    slideCount.textContent = query
      ? `${visible} / ${slides.size} 张切片`
      : `${slides.size} 张切片`;
    slideEmpty.hidden = visible !== 0;
  }

  function populateSlideMenu(records) {
    const root = { folders: new Map(), slides: [] };
    for (const record of records) {
      const parts = (record.path || record.id).split("/");
      let node = root;
      for (const folder of parts.slice(0, -1)) {
        if (!node.folders.has(folder)) {
          node.folders.set(folder, { folders: new Map(), slides: [] });
        }
        node = node.folders.get(folder);
      }
      node.slides.push({ record, name: parts.at(-1) });
    }

    function renderNode(node, list, prefix = "") {
      const compare = (left, right) => left.localeCompare(right, undefined, { numeric: true });
      for (const [name, child] of [...node.folders].sort((a, b) => compare(a[0], b[0]))) {
        const path = prefix ? `${prefix}/${name}` : name;
        const row = document.createElement("li");
        row.className = "slide-folder";
        const details = document.createElement("details");
        details.dataset.folderPath = path;
        details.open = expandedFolders.has(path);
        const summary = document.createElement("summary");
        summary.textContent = name;
        const children = document.createElement("ul");
        renderNode(child, children, path);
        details.append(summary, children);
        summary.addEventListener("click", () => {
          if (slideFilter.value.trim()) return;
          if (details.open) expandedFolders.delete(path);
          else expandedFolders.add(path);
        });
        row.append(details);
        list.append(row);
      }
      for (const { record, name } of node.slides.sort((a, b) => compare(a.name, b.name))) {
        const row = document.createElement("li");
        row.className = "slide-entry";
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.slideId = record.id;
        button.dataset.search = `${record.id} ${record.path || ""}`.toLocaleLowerCase();
        button.textContent = name;
        button.title = record.path || record.id;
        button.addEventListener("click", () => openSlide(record.id));
        row.append(button);
        list.append(row);
      }
    }

    slideList.replaceChildren();
    renderNode(root, slideList);
    for (const button of slideList.querySelectorAll(".slide-entry button")) {
      const selected = button.dataset.slideId === currentSlide?.id;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-current", selected ? "true" : "false");
    }
    filterSlides();
  }

  function uploadSlide(file, position, total) {
    const label = `${position}/${total} ${file.name}`;
    slideUploadStatus.textContent = `${position}/${total} 上传中 0%`;
    slideUploadStatus.title = label;
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", `/api/slides/upload?filename=${encodeURIComponent(file.name)}`);
      request.responseType = "json";
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.total > 0) {
          slideUploadStatus.textContent = event.loaded === event.total
            ? `${position}/${total} 正在验证…`
            : `${position}/${total} 上传中 ${Math.round(event.loaded / event.total * 100)}%`;
        }
      });
      request.addEventListener("load", () => {
        if (request.status === 201) resolve(request.response);
        else reject(new Error(request.response?.detail || `HTTP ${request.status}`));
      });
      request.addEventListener("error", () => reject(new Error("网络连接失败")));
      request.addEventListener("abort", () => reject(new Error("上传已取消")));
      request.send(file);
    });
  }

  async function uploadSlides(files) {
    if (!files.length || slideUploadButton.disabled) return;
    slideUploadButton.disabled = true;
    const failures = [];
    let lastUploaded = null;
    try {
      for (const [index, file] of files.entries()) {
        let record;
        try {
          record = await uploadSlide(file, index + 1, files.length);
        } catch (error) {
          failures.push({ name: file.name, message: error.message });
          continue;
        }
        slides.set(record.id, record);
        populateSlideMenu([...slides.values()]);
        lastUploaded = record;
      }
      const succeeded = files.length - failures.length;
      if (files.length === 1 && failures.length) {
        slideUploadStatus.textContent = `上传失败：${failures[0].message}`;
      } else if (failures.length) {
        slideUploadStatus.textContent =
          `成功 ${succeeded} 张，失败 ${failures.length} 张：${failures[0].name}`;
      } else {
        slideUploadStatus.textContent = `已上传 ${succeeded} 张`;
      }
      slideUploadStatus.title = failures
        .map(({ name, message }) => `${name}：${message}`)
        .join("\n");
      if (lastUploaded) {
        openSlide(lastUploaded.id);
        if (failures.length) setSlideMenuOpen(true);
      }
    } finally {
      slideUploadInput.value = "";
      slideUploadButton.disabled = false;
    }
  }

  async function refreshSlides() {
    try {
      const response = await fetch("/api/slides", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const records = await response.json();
      const ids = records.map((record) => record.id);
      const previous = [...slides.keys()];
      if (ids.length === previous.length && ids.every((id, index) => id === previous[index])) {
        return;
      }
      slides = new Map(records.map((record) => [
        record.id, { ...slides.get(record.id), ...record },
      ]));
      populateSlideMenu(records);
      if (currentSlide && !slides.has(currentSlide.id)) {
        ++openSequence;
        slideRequestController?.abort();
        viewer.close();
        currentSlide = null;
        currentSlideLabel.textContent = "选择切片";
      }
      if (
        !currentSlide && !slideRequestController && records.length &&
        !slideUploadButton.disabled
      ) {
        openSlide(records[0].id);
      } else if (!records.length) {
        showStatus("切片列表为空");
      }
    } catch (error) {
      console.warn("无法刷新切片列表", error);
    }
  }

  async function initialize() {
    if (!window.OpenSeadragon) {
      showStatus("OpenSeadragon 加载失败，请检查网络连接");
      return;
    }

    viewer = OpenSeadragon({
      id: "viewer",
      prefixUrl:
        "https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/images/",
      showNavigator: true,
      navigatorPosition: "TOP_RIGHT",
      navigatorSizeRatio: 0.16,
      showNavigationControl: false,
      preserveViewport: false,
      animationTime: 0.45,
      springStiffness: 8,
      zoomPerScroll: 1.25,
      minZoomImageRatio: 0.8,
      maxZoomPixelRatio: 2,
      visibilityRatio: 0.5,
      constrainDuringPan: true,
      gestureSettingsMouse: {
        scrollToZoom: true,
        dragToPan: true,
        clickToZoom: false,
        dblClickToZoom: true,
      },
      gestureSettingsTouch: {
        pinchToZoom: true,
        dragToPan: true,
        flickEnabled: true,
        dblClickToZoom: true,
      },
    });

    viewer.addHandler("open", () => {
      const item = currentItem();
      if (!item || !currentSlide) return;
      const size = item.getContentSize();
      if (size.x !== currentSlide.width || size.y !== currentSlide.height) {
        viewer.close();
        showStatus("切片加载失败：查看器尺寸与元数据不一致");
        return;
      }
      viewer.viewport.goHome(true);
      viewer.viewport.applyConstraints(true);
      hideStatus();
      updateViewportStatus();
      loadInitiallyVisibleOverlays();
      if (cropActive) initializeCropRegion();
    });
    viewer.addHandler("open-failed", (event) => {
      showStatus(`切片加载失败：${event.message || "未知错误"}`);
    });
    viewer.addHandler("viewport-change", updateViewportStatus);
    const updatePointerCoordinates = (event) => {
      const item = currentItem();
      if (!item || !currentSlide) return;
      const imagePoint = item.windowToImageCoordinates(
        new OpenSeadragon.Point(event.clientX, event.clientY),
      );
      const inBounds =
        Number.isFinite(imagePoint.x) &&
        Number.isFinite(imagePoint.y) &&
        imagePoint.x >= 0 &&
        imagePoint.y >= 0 &&
        imagePoint.x < currentSlide.width &&
        imagePoint.y < currentSlide.height;
      coordinateLabel.textContent = inBounds
        ? `x ${Math.floor(imagePoint.x).toLocaleString()} · y ${Math.floor(imagePoint.y).toLocaleString()}`
        : "x — · y —";
    };
    let pointerFrame = 0;
    let pointerPosition = null;
    viewer.element.addEventListener("pointermove", (event) => {
      pointerPosition = { clientX: event.clientX, clientY: event.clientY };
      if (pointerFrame) return;
      pointerFrame = requestAnimationFrame(() => {
        pointerFrame = 0;
        updatePointerCoordinates(pointerPosition);
      });
    });
    viewer.element.addEventListener("pointerdown", updatePointerCoordinates);
    viewer.element.addEventListener("pointerleave", () => {
      if (pointerFrame) cancelAnimationFrame(pointerFrame);
      pointerFrame = 0;
      pointerPosition = null;
      coordinateLabel.textContent = "x — · y —";
    });
    viewer.addHandler("canvas-click", (event) => {
      if (!cropActive || !event.quick || !event.position) return;
      const item = currentItem();
      if (!item) return;
      const viewportPoint = viewer.viewport.pointFromPixel(event.position);
      const imagePoint = item.viewportToImageCoordinates(viewportPoint);
      centerCropAt(imagePoint);
      event.preventDefaultAction = true;
    });

    new OpenSeadragon.MouseTracker({
      element: cropOverlay,
      clickHandler: (event) => {
        event.preventDefaultAction = true;
      },
      dragHandler: (event) => {
        const item = currentItem();
        if (!item || !cropActive) return;
        const viewportDelta = viewer.viewport.deltaPointsFromPixels(event.delta);
        const imageOrigin = item.viewportToImageCoordinates(
          new OpenSeadragon.Point(0, 0),
        );
        const imageDeltaPoint = item.viewportToImageCoordinates(viewportDelta);
        const level = selectedCropLevel();
        if (!level) return;
        setCropRegion({
          ...cropRegion,
          x:
            cropRegion.x +
            (imageDeltaPoint.x - imageOrigin.x) / level.downsample[0],
          y:
            cropRegion.y +
            (imageDeltaPoint.y - imageOrigin.y) / level.downsample[1],
        });
        event.preventDefaultAction = true;
      },
    }).setTracking(true);

    document.querySelector("#zoom-in").addEventListener("click", () => {
      viewer.viewport.zoomBy(1.5);
      viewer.viewport.applyConstraints();
    });
    document.querySelector("#zoom-out").addEventListener("click", () => {
      viewer.viewport.zoomBy(1 / 1.5);
      viewer.viewport.applyConstraints();
    });
    document.querySelector("#home-view").addEventListener("click", () => {
      viewer.viewport.goHome();
    });
    cropTool.addEventListener("click", () => setCropActive(!cropActive));
    overlayTool.addEventListener("click", () => {
      if (!overlayTool.disabled) overlayPanel.hidden = !overlayPanel.hidden;
    });
    overlayClose.addEventListener("click", () => {
      overlayPanel.hidden = true;
      overlayTool.focus();
    });
    cropClose.addEventListener("click", () => setCropActive(false));
    for (const input of [cropX, cropY, cropWidth, cropHeight]) {
      input.addEventListener("change", syncCropInputs);
    }
    cropLevel.addEventListener("change", () => {
      updateCropLevelMpp();
      initializeCropRegion();
      setCropStatus("");
    });
    cropSave.addEventListener("click", saveCrop);
    slideUploadButton.addEventListener("click", () => slideUploadInput.click());
    slideUploadInput.addEventListener("change", () => {
      const files = [...(slideUploadInput.files || [])];
      if (files.length) uploadSlides(files);
    });
    slider.addEventListener("input", () => setImageZoom(2 ** Number(slider.value)));
    menuButton.addEventListener("click", () => setSlideMenuOpen(menu.hidden));
    slideFilter.addEventListener("input", filterSlides);
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".slide-picker")) closeSlideMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !menu.hidden) {
        closeSlideMenu();
        menuButton.focus();
      } else if (event.key === "Escape" && cropActive) {
        setCropActive(false);
        cropTool.focus();
      }
    });

    try {
      const response = await fetch("/api/slides");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const records = await response.json();
      slides = new Map(records.map((record) => [record.id, record]));
      populateSlideMenu(records);
      if (records.length) openSlide(records[0].id);
      else showStatus("切片列表为空");
      window.setInterval(refreshSlides, 5000);
    } catch (error) {
      showStatus(`无法读取切片列表：${error.message}`);
    }
  }

  window.addEventListener("DOMContentLoaded", initialize);
})();
