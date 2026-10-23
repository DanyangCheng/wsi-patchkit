(() => {
  "use strict";

  const cropGrid = document.querySelector("#crop-grid");
  const cropEmpty = document.querySelector("#crop-empty");
  const emptyTitle = document.querySelector("#empty-title");
  const emptyDescription = document.querySelector("#empty-description");
  const emptyViewerLink = document.querySelector("#empty-viewer-link");
  const cropCount = document.querySelector("#crop-count");
  const cropTotalSize = document.querySelector("#crop-total-size");
  const cropSearch = document.querySelector("#crop-search");
  const cropStatus = document.querySelector("#crop-status");
  const refreshButton = document.querySelector("#refresh-crops");
  const selectVisible = document.querySelector("#select-visible-crops");
  const selectedCount = document.querySelector("#selected-crop-count");
  const downloadSelectedButton = document.querySelector("#download-selected-crops");
  const downloadLabelsButton = document.querySelector("#download-selected-labels");
  const deleteSelectedButton = document.querySelector("#delete-selected-crops");

  let crops = [];
  let visibleCrops = [];
  const selectedCrops = new Set();
  let deletingCrops = false;

  function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let amount = value / 1024;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${amount.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[unit]}`;
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function setStatus(message, type = "") {
    cropStatus.textContent = message;
    cropStatus.className = `status-message ${type}`.trim();
  }

  function createCropCard(crop) {
    const card = document.createElement("article");
    card.className = "crop-card";

    const previewLink = document.createElement("a");
    previewLink.className = "preview-link";
    previewLink.href = crop.image_url;
    previewLink.target = "_blank";
    previewLink.rel = "noreferrer";
    previewLink.setAttribute("aria-label", `在新标签页查看 ${crop.filename}`);

    const image = document.createElement("img");
    image.src = crop.thumbnail_url;
    image.alt = crop.filename;
    image.loading = "lazy";
    image.decoding = "async";
    previewLink.append(image);

    const selectionLabel = document.createElement("label");
    selectionLabel.className = "card-select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedCrops.has(crop.filename);
    checkbox.setAttribute("aria-label", `选择 ${crop.filename}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedCrops.add(crop.filename);
      else selectedCrops.delete(crop.filename);
      updateSelectionControls();
    });
    const selectionText = document.createElement("span");
    selectionText.textContent = "选择";
    selectionLabel.append(checkbox, selectionText);

    const content = document.createElement("div");
    content.className = "crop-card-content";

    const titleRow = document.createElement("div");
    titleRow.className = "crop-card-title-row";
    const filename = document.createElement("h2");
    filename.className = "crop-filename";
    filename.textContent = crop.filename;
    filename.title = crop.filename;
    const format = document.createElement("span");
    format.className = "format-badge";
    format.textContent = crop.format;
    titleRow.append(filename, format);

    const metadata = document.createElement("div");
    metadata.className = "crop-meta";
    const size = document.createElement("span");
    size.textContent = formatBytes(crop.size_bytes);
    const modified = document.createElement("time");
    modified.dateTime = crop.modified_at;
    modified.textContent = formatDate(crop.modified_at);
    metadata.append(size, modified);

    const actions = document.createElement("div");
    actions.className = "crop-actions";
    const download = document.createElement("a");
    download.className = "action-button";
    download.href = crop.download_url;
    download.download = crop.filename;
    download.textContent = "下载图像";
    const remove = document.createElement("button");
    remove.className = "delete-button";
    remove.type = "button";
    remove.disabled = deletingCrops;
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteCrops([crop.filename]));
    actions.append(download);
    if (crop.export_url) {
      const bundle = document.createElement("a");
      bundle.className = "action-button";
      bundle.href = window.patchkitExports.url(crop.export_url);
      bundle.textContent = "下载标注包";
      actions.append(bundle);
    }
    actions.append(remove);

    content.append(titleRow, metadata, actions);
    card.append(previewLink, selectionLabel, content);
    return card;
  }

  function updateSelectionControls() {
    const selection = [...selectedCrops];
    const selectedVisibleCount = visibleCrops.reduce(
      (count, crop) => count + Number(selectedCrops.has(crop.filename)),
      0,
    );
    selectedCount.textContent = `已选 ${selection.length} 张`;
    selectVisible.checked = visibleCrops.length > 0 && selectedVisibleCount === visibleCrops.length;
    selectVisible.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < visibleCrops.length;
    selectVisible.disabled = visibleCrops.length === 0;
    downloadLabelsButton.disabled = selection.length === 0 || deletingCrops;
    downloadSelectedButton.disabled = selection.length === 0 || deletingCrops;
    deleteSelectedButton.disabled = selection.length === 0 || deletingCrops;
    for (const button of cropGrid.querySelectorAll(".delete-button")) {
      button.disabled = deletingCrops;
    }
  }

  function renderCrops() {
    const query = cropSearch.value.trim().toLocaleLowerCase("zh-CN");
    visibleCrops = crops.filter((crop) =>
      crop.filename.toLocaleLowerCase("zh-CN").includes(query),
    );
    const availableNames = new Set(crops.map((crop) => crop.filename));
    for (const filename of selectedCrops) {
      if (!availableNames.has(filename)) selectedCrops.delete(filename);
    }
    const totalBytes = crops.reduce((total, crop) => total + crop.size_bytes, 0);
    cropCount.textContent = query
      ? `显示 ${visibleCrops.length} / ${crops.length} 张`
      : `共 ${crops.length} 张`;
    cropTotalSize.textContent = crops.length ? `占用 ${formatBytes(totalBytes)}` : "";
    const cards = document.createDocumentFragment();
    for (const crop of visibleCrops) cards.append(createCropCard(crop));
    cropGrid.replaceChildren(cards);
    updateSelectionControls();
    cropEmpty.hidden = visibleCrops.length > 0;

    if (!crops.length) {
      emptyTitle.textContent = "还没有裁剪图";
      emptyDescription.textContent = "在切片查看器中打开“裁剪任务”，保存第一张图像。";
      emptyViewerLink.hidden = false;
    } else if (!visibleCrops.length) {
      emptyTitle.textContent = "没有匹配的图像";
      emptyDescription.textContent = "试试其他文件名关键词。";
      emptyViewerLink.hidden = true;
    }
  }

  async function deleteCrops(filenames) {
    if (!filenames.length) return;
    const confirmation = filenames.length === 1
      ? `确定删除“${filenames[0]}”吗？此操作无法撤销。`
      : `确定删除所选 ${filenames.length} 张裁剪图吗？此操作无法撤销。`;
    if (!window.confirm(confirmation)) return;
    deletingCrops = true;
    refreshButton.disabled = true;
    updateSelectionControls();
    setStatus(`正在删除 ${filenames.length} 张裁剪图…`);
    try {
      const response = await fetch("/api/crops/batch-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      const deleted = new Set(result.filenames);
      crops = crops.filter((item) => !deleted.has(item.filename));
      for (const filename of deleted) selectedCrops.delete(filename);
      renderCrops();
      setStatus(`已删除 ${deleted.size} 张裁剪图`);
    } catch (error) {
      setStatus(`删除失败：${error.message}`, "error");
    } finally {
      deletingCrops = false;
      refreshButton.disabled = false;
      updateSelectionControls();
    }
  }

  async function downloadCrops(filenames, labels = false) {
    if (!filenames.length) return;
    downloadSelectedButton.disabled = downloadLabelsButton.disabled = true;
    setStatus(`正在打包下载 ${filenames.length} 张裁剪图…`);
    try {
      const response = await fetch("/api/crops/batch-download", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames, include_images: !labels, label_format: labels ? window.patchkitExports.format() : "none" }),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.detail || `HTTP ${response.status}`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = labels ? "wsi-patchkit-annotations.zip" : "wsi-patchkit-images.zip";
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      setStatus(`已打包 ${filenames.length} 张裁剪的${labels ? "标注" : "原图"}`);
    } catch (error) {
      setStatus(`下载失败：${error.message}`, "error");
    } finally { updateSelectionControls(); }
  }

  async function loadCrops() {
    refreshButton.disabled = true;
    setStatus("正在读取裁剪图列表…");
    try {
      const response = await fetch("/api/crops", { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      crops = result;
      renderCrops();
      setStatus("");
    } catch (error) {
      if (!crops.length) cropEmpty.hidden = true;
      setStatus(`无法读取裁剪图列表：${error.message}`, "error");
    } finally {
      refreshButton.disabled = false;
    }
  }

  cropSearch.addEventListener("input", renderCrops);
  refreshButton.addEventListener("click", loadCrops);
  selectVisible.addEventListener("change", () => {
    for (const crop of visibleCrops) {
      if (selectVisible.checked) selectedCrops.add(crop.filename);
      else selectedCrops.delete(crop.filename);
    }
    renderCrops();
  });
  downloadSelectedButton.addEventListener("click", () => {
    downloadCrops([...selectedCrops]);
  });
  deleteSelectedButton.addEventListener("click", () => {
    deleteCrops([...selectedCrops]);
  });
  downloadLabelsButton.addEventListener("click", () => downloadCrops([...selectedCrops], true));
  window.patchkitExports.load(renderCrops).catch(error => setStatus(error.message, "error"));
  loadCrops();
})();
