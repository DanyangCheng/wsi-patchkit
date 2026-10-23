"use strict";
// Both result pages use the server registry as their format catalog.
window.patchkitExports = {
  format: () => document.querySelector("#crop-label-format")?.value || "segmentation-mask",
  url(base) {
    return `${base}?${new URLSearchParams({label_format: this.format(), include_images: "false"})}`;
  },
  async load(onChange) {
    const selector = document.querySelector("#crop-label-format");
    selector.addEventListener("change", onChange);
    const response = await fetch("/api/crops/exporters", {cache: "no-store"});
    const result = await response.json();
    const formats = response.ok ? result.filter(format => format.id !== "none") : [];
    if (!response.ok) throw new Error(result.detail || "无法读取标签格式");
    const selected = selector.value || selector.options[0]?.value || "segmentation-mask";
    selector.replaceChildren(...formats.map(format => new Option(format.name, format.id)));
    selector.value = formats.some(format => format.id === selected) ? selected : formats[0]?.id || "";
    onChange();
  },
};
