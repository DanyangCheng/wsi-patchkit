# Segment → WSI Patchkit 预测 Mask 交付契约

状态：Draft v1<br>
适用范围：Segment 将局部或完整 WSI 分割结果交给 WSI Patchkit 展示<br>
规范关键词：`必须`、`不得`、`应当`、`可以`

## 1. 目标与边界

本契约只规定预测栅格如何被 Patchkit 定位、读取和显示，不规定模型、类别体系、推理流程或指标计算。

- Segment 负责模型推理、窗口融合、类别定义、预测有效性和输出文件生成。
- Patchkit 负责物理坐标映射、局部读取、调色、透明覆盖和浏览器展示。
- Patchkit 不得根据像素值猜测背景、未预测区域或类别含义。
- Patchkit 不负责根据窗口级输出生成最终预测 mask。

## 2. 规范输出

每个预测片段应当包含：

```text
prediction-index.tif
prediction-coverage.tif     # 全区域有效时可以省略
prediction.json
```

可以额外提供已经着色的 RGB/RGBA 预览，但预览不是规范预测结果：

```text
prediction-preview.tif      # 可选、可重新生成
```

## 3. `prediction-index.tif`

`prediction-index.tif` 是单通道类别索引图。每个像素保存 Segment 定义的类别 ID。

### 3.1 必须满足

- 必须是单通道 TIFF。
- 数据类型必须是无符号整数；类别 ID 不超过 255 时应使用 `uint8`，否则使用 `uint16`。
- 文件宽高必须等于 manifest 中的 `dimensions`。
- 必须使用无损压缩。
- 如果包含金字塔，各级必须由 nearest-neighbor 生成。
- coverage 为真的像素值必须出现在 manifest 的类别表中。
- coverage 为假的像素值不具有语义，Patchkit 必须忽略。

### 3.2 不得执行

- 不得使用 JPEG 等有损压缩。
- 不得使用 bilinear、bicubic 或 area 生成 indexed mask 金字塔。
- 不得仅通过颜色保存类别语义。
- 不得让同一个类别 ID 同时表示合法类别和“未预测”。

## 4. `prediction-coverage.tif`

`prediction-coverage.tif` 是单通道二值图，表示每个像素是否存在有效预测：

```text
0 → 没有预测，index 值必须被忽略
1 → 存在预测，index 值有效
```

### 4.1 必须满足

- 必须是单通道 `uint8` TIFF。
- 允许值只能是 `0` 和 `1`。
- 宽高必须与对应的 `prediction-index.tif` 完全一致。
- 必须与 prediction 使用相同的原点、MPP 和像素方向。
- 如果包含金字塔，各级必须由 nearest-neighbor 生成。
- 必须使用无损压缩。

coverage 只表示“是否存在预测”，不得用来表达：

- 组织或非组织；
- 模型置信度；
- 预测是否正确；
- 标注是否有效；
- 背景类别。

如果需要置信度，应另行输出连续值 probability/confidence 栅格。

### 4.2 省略条件

只有片段矩形内每个像素都有有效预测时，才可以省略 coverage 文件，并在 manifest 中声明：

```json
"full_coverage": true
```

`coverage` 与 `full_coverage: true` 不得同时出现。未提供 coverage 时，也未声明 `full_coverage: true`，Patchkit 必须拒绝注册。

## 5. 为什么必须区分 prediction 和 coverage

假设类别 `0` 表示 `Normal`：

```text
prediction-index       prediction-coverage

0 0 1 1                1 1 1 1
0 2 2 1                1 1 1 1
0 0 3 3                0 0 1 1
0 0 3 3                0 0 1 1
```

- 左上方的 `0` 是有效的 `Normal` 预测。
- 左下方的 `0` 位于未预测区域，必须显示为透明。

渲染规则等价于：

```python
rgba = palette[prediction_index]
rgba[coverage == 0, 3] = 0
```

## 6. 空间坐标契约

每个预测片段必须显式提供：

```text
origin_um   片段左上像素边界相对 WSI level-0 原点的物理坐标
mpp         每个像素在 X/Y 方向对应的微米数
dimensions  (width, height)
```

示例：

```json
{
  "origin_um": [1000.0, 500.0],
  "mpp": [0.5, 0.5],
  "dimensions": [4096, 2048]
}
```

其物理覆盖范围为：

```text
x: [1000, 3048) μm
y: [ 500, 1524) μm
```

所有矩形必须使用半开区间 `[x0, x1) × [y0, y1)`。`dimensions` 的顺序是 `(width, height)`，与 NumPy 数组的 `(height, width)` 不同。

Segment 不得仅依赖 mask 尺寸让 Patchkit猜测其与 WSI 的对应关系。

## 7. Manifest

推荐使用一个 manifest 描述完整 overlay。文件路径相对于 manifest 所在目录解析。

```json
{
  "schema": "wsi-patchkit-overlay/v1",
  "slide_id": "case-001",
  "overlay_id": "prediction",
  "display_name": "Prediction",
  "encoding": "indexed",
  "default_opacity": 0.7,
  "initially_visible": false,
  "classes": [
    {"id": 0, "name": "Normal", "rgba": [0, 0, 0, 0]},
    {"id": 1, "name": "G3", "rgba": [255, 0, 0, 160]},
    {"id": 2, "name": "G4", "rgba": [0, 255, 0, 160]},
    {"id": 3, "name": "G5", "rgba": [0, 0, 255, 160]}
  ],
  "fragments": [
    {
      "prediction": "region-01/prediction-index.tif",
      "coverage": "region-01/prediction-coverage.tif",
      "full_coverage": false,
      "origin_um": [1000.0, 500.0],
      "mpp": [0.5, 0.5],
      "dimensions": [4096, 2048],
      "revision": "run-20260922-001"
    }
  ]
}
```

### 7.1 顶层字段

| 字段 | 必需 | 约束 |
| --- | --- | --- |
| `schema` | 是 | 当前必须为 `wsi-patchkit-overlay/v1` |
| `slide_id` | 是 | 必须匹配 Patchkit 中已注册的 WSI |
| `overlay_id` | 是 | 在同一 WSI 内必须唯一 |
| `display_name` | 否 | 浏览器显示名称；省略时使用 `overlay_id` |
| `encoding` | 是 | 本契约必须为 `indexed` |
| `default_opacity` | 否 | 0–1，默认为 0.7 |
| `initially_visible` | 否 | 布尔值，默认为 false |
| `classes` | 是 | 类别 ID 必须唯一，RGBA 每项必须在 0–255 |
| `fragments` | 是 | 至少包含一个片段 |

类别名称仅用于 legend 展示，由 Segment 定义；Patchkit 不解释名称含义。

### 7.2 Fragment 字段

| 字段 | 必需 | 约束 |
| --- | --- | --- |
| `prediction` | 是 | indexed TIFF 相对路径 |
| `coverage` | 条件必需 | `full_coverage` 不为 true 时必须提供 |
| `full_coverage` | 是 | 布尔值 |
| `origin_um` | 是 | 两个有限数值 |
| `mpp` | 是 | 两个有限正数 |
| `dimensions` | 是 | 两个正整数，顺序为 width、height |
| `revision` | 是 | 文件内容变化时必须变化，用于缓存失效 |

第一版中 fragment 应当互不重叠。若存在重叠，Segment 必须先完成融合再输出；Patchkit 不根据类别或概率解决冲突。

## 8. 已着色预览

Segment 可以额外输出 RGB/RGBA 预览，供不支持 indexed mask 的工具使用，但必须满足：

- 不得替代 `prediction-index.tif` 成为唯一规范结果。
- 不得用于指标计算或类别恢复。
- RGB 预览不得隐式承担 coverage；部分区域预测应使用 RGBA alpha 或独立 coverage。
- 必须使用无损压缩，避免类别边界产生混色。

Patchkit 后续可以同时支持 indexed overlay 和 RGBA overlay，但 indexed overlay 是首选路径。

## 9. Patchkit 注册校验

出现以下任一情况时，Patchkit 应拒绝注册，或在首次读取到违规像素时拒绝对应 tile 渲染：

- `slide_id` 未注册；
- manifest schema 不支持；
- prediction 或 coverage 文件不存在；
- TIFF 通道数、dtype 或尺寸不符合契约；
- MPP 非有限正数；
- coverage 包含 `0/1` 之外的值；
- coverage 与 prediction 尺寸不一致；
- coverage 为真的像素包含未声明类别 ID；
- 未提供 coverage 且未声明 `full_coverage: true`；
- 同时提供 coverage 和 `full_coverage: true`；
- fragment 与 WSI 物理范围完全不相交；
- fragment 发生未声明的空间重叠；
- `revision` 缺失。

Patchkit 可以裁剪超出 WSI 物理范围的片段部分，但不得移动、拉伸或自动重新配准片段。

## 10. 双方职责摘要

### Segment 必须负责

- 生成最终融合后的 indexed prediction；
- 准确定义 coverage；
- 提供类别 ID、名称和展示颜色；
- 提供 origin、MPP 和 dimensions；
- 确保 TIFF 与 manifest 一致；
- 在输出变化时更新 revision。

### Patchkit 必须负责

- 验证输入契约；
- 将 WSI 视口映射到 fragment 像素范围；
- 对 indexed mask 使用 nearest 采样；
- 按 palette 生成 RGBA；
- 将 coverage 为假的像素设为透明；
- 缓存和输出 overlay 瓦片。

### Patchkit 不负责

- 推断类别含义；
- 推断哪个值是背景或未预测；
- 执行窗口融合、概率融合或模型推理；
- 修改 Segment 输出；
- 计算分割指标。

## 11. 版本演进

不兼容变更必须提升 schema 主版本，例如 `wsi-patchkit-overlay/v2`。新增可选字段可以保持 v1，但 Patchkit 必须忽略其不理解的可选展示字段，不得忽略未知的坐标或编码字段。
