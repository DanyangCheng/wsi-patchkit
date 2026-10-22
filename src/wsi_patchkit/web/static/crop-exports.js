"use strict";
// Both result pages use the server registry as their format catalog.
window.patchkitExports = {
  format: () => document.querySelector("#crop-label-format")?.value || "indexed-png",
  url(base) {
    return `${base}?${new URLSearchParams({label_format: this.format()})}`;
  },
  async load(onChange) {
    const selector = document.querySelector("#crop-label-format");
    selector.addEventListener("change", onChange);
    const response = await fetch("/api/crops/exporters", {cache: "no-store"});
    const formats = await response.json();
    if (!response.ok) throw new Error(formats.detail || "无法读取标签格式");
    const selected = this.format();
    selector.replaceChildren(...formats.map(format => new Option(format.name, format.id)));
    selector.value = formats.some(format => format.id === selected) ? selected : formats[0]?.id || "";
    onChange();
  },
};
