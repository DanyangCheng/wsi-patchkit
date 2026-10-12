# 上游 → WSI Patchkit 检测预测交付契约

状态：v1
适用范围：细胞核中心检测、由实例分割提取的中心检测、YOLO 等框检测
规范关键词：`必须`、`不得`、`应当`、`可以`

本文用于上游实现预测结果导出和双方确认接口。当前 viewer 支持中心点打点；仅有框时使用框中心。
框和轮廓暂不绘制。显示限于 level 0–1 对应的高倍率，低倍率清空点图层且停止点查询。

## 1. 交付目标与职责

上游交付最终对象记录，每条记录对应一个检测对象。中心点、框和轮廓使用统一的 WSI 物理坐标。

- 上游负责推理、阈值筛选、实例提取、坐标还原、跨窗口去重、类别定义和有效预测区域定义。
- Patchkit 接入后负责契约校验、空间查询、图层绘制、类别和置信度筛选、对象拾取。
- Patchkit 不根据检测结果推断预测区域，也不重新执行模型后处理。
- 点大小、线宽、颜色、透明度和低倍率聚合属于展示配置，不改变交付对象的几何语义。

## 2. 交付目录

每个 WSI 的每次检测运行交付一个独立目录：

```text
case-001/nuclei/run-001/
  prediction.json          # manifest，必需
  objects.jsonl            # 对象记录，必需；零检出时为空文件
  prediction-coverage.tif  # coverage.type = raster 时必需
```

- 所有文件路径相对 `prediction.json` 所在目录解析；必须位于交付目录内。
- 路径不得使用绝对路径、远程 URL 或通过 `..`、符号链接越出交付目录。
- 文件使用 UTF-8；JSON 数值必须有限，不允许 NaN、Infinity。
- JSONL 每个非空行必须是一个完整 JSON 对象；不得包含表头或注释。
- 必须在全部数据文件完成后发布 manifest，避免读取未完成的交付。
- 同一个发布版本应当保持不可变；内容变化时必须更新 `revision`，并作为完整版本发布。
- 目录名不作为元数据；以 manifest 中的 ID 为准。

v1 规范交换格式为 JSONL。SQLite R-tree 等空间索引可以由接收侧构建；其他对象存储编码需要另行定义协议。

## 3. Manifest 示例

```json
{
  "schema": "wsi-patchkit-detection/v1",
  "slide_id": "case-001",
  "overlay_id": "nuclei",
  "display_name": "细胞核检测",
  "encoding": "objects",
  "coordinate_system": "slide_um",
  "revision": "run-001",
  "default_opacity": 0.8,
  "initially_visible": false,
  "classes": [
    {"id": 0, "name": "Nucleus", "rgba": [255, 80, 80, 220]}
  ],
  "objects": {
    "path": "objects.jsonl",
    "format": "jsonl",
    "count": 2
  },
  "coverage": {
    "type": "rectangles",
    "bounds_um": [[1000.0, 500.0, 3048.0, 1524.0]]
  },
  "provenance": {
    "model_name": "nuclei-center-detector",
    "model_revision": "weights-sha256-or-version",
    "source_mpp": [0.25, 0.25],
    "source_mpp_source": "slide_metadata",
    "score_definition": "中心检测头的对象置信度，未校准",
    "center_definition": "中心热图局部峰值",
    "postprocessing": {
      "score_threshold": 0.3,
      "deduplication": "按类别在全片坐标执行中心距离去重，距离阈值 3 微米，保留高分对象"
    }
  }
}
```

### 3.1 顶层字段

| 字段                  | 必需 | 约束                                                   |
| --------------------- | ---- | ------------------------------------------------------ |
| `schema`            | 是   | 固定为`wsi-patchkit-detection/v1`                    |
| `slide_id`          | 是   | 必须匹配接收侧注册的 WSI                               |
| `overlay_id`        | 是   | 在同一 WSI 内唯一；替换同一图层时更新 revision         |
| `encoding`          | 是   | 固定为`objects`                                      |
| `coordinate_system` | 是   | 固定为`slide_um`                                     |
| `revision`          | 是   | 非空字符串；对象、coverage、类别或元数据变化时必须更新 |
| `classes`           | 是   | 至少一个类别；类别 ID 唯一                             |
| `objects`           | 是   | 包含`path`、`format: "jsonl"`、非负整数 `count`  |
| `coverage`          | 是   | 按第 6 节声明有效预测区域                              |
| `provenance`        | 是   | 按第 7 节提供模型及后处理信息                          |
| `display_name`      | 否   | 非空字符串；省略时使用 overlay_id                      |
| `default_opacity`   | 否   | `[0,1]`，建议默认 0.8                                |
| `initially_visible` | 否   | 布尔值，建议默认 false                                 |

`slide_id` 和 `overlay_id` 使用 1–128 个 ASCII 字母、数字、点、短横线或下划线，与现有 Patchkit 公共 ID 规则一致。

类别字段：`id` 为非负整数，`name` 为非空字符串，`rgba` 为四个 `[0,255]` 整数。
类别 `0` 是合法类别，不隐含背景或未预测。布尔值不得作为整数使用。

## 4. 对象记录

### 4.1 必需与可选字段

| 字段           | 必需     | 约束                                                           |
| -------------- | -------- | -------------------------------------------------------------- |
| `id`         | 是       | 非空字符串；同一 overlay 的本次 revision 内唯一                |
| `class_id`   | 是       | 非负整数，必须出现在 Manifest 的 classes 中                   |
| `center_um`  | 条件必需 | 两个有限数值`[x,y]`；中心点检测必须提供                      |
| `bbox_um`    | 条件必需 | 四个有限数值`[x0,y0,x1,y1]`；框检测必须提供                  |
| `score`      | 否       | 有限数值，范围`[0,1]`；语义在 Manifest 的 provenance 中声明 |
| `outline_um` | 否       | 第 4.3 节定义的多边形结构；提供时必须同时提供 bbox_um          |
| `properties` | 否       | JSON 对象；用于附加信息，不参与基础几何解释                    |

每条记录至少提供 `center_um` 或 `bbox_um`。两者可以同时提供。
框必须满足 `x0 < x1`、`y0 < y1`；同时提供中心与框时，中心必须位于框内。
轮廓必须被 bbox 包围，允许轮廓顶点等于 bbox 的最大边界。

`score` 不存在时必须省略，不得填入 `1` 或用 `null` 代替未知分数。
score 不自动表示校准概率，不应跨模型直接比较。
对象 ID 不依赖文件行号；同一对象仅修改展示或附加信息时应保留 ID。跨推理运行的对象 ID 稳定性不作保证。

### 4.2 中心点与框示例

中心检测的 `objects.jsonl`，与第 3 节 manifest 对应：

```jsonl
{"id":"n-0001","class_id":0,"center_um":[1012.5,518.0],"score":0.94}
{"id":"n-0002","class_id":0,"center_um":[1032.0,542.5],"score":0.87}
```

YOLO 等框检测的单条记录：

```json
{"id":"d-0001","class_id":0,"bbox_um":[1005.0,510.0,1020.0,526.0],"score":0.91}
```

框检测不要求输出中心点。若额外输出框中心，必须在 `center_definition` 中声明其来源。
由实例分割提取的中心必须说明采用质心、距离变换峰值还是其他规则。

### 4.3 可选实例轮廓

`outline_um` 固定采用以下结构，以支持孔洞和多个连通部分：

```json
{
  "type": "MultiPolygon",
  "coordinates": [
    [
      [[1005,510],[1020,510],[1020,526],[1005,526],[1005,510]]
    ]
  ]
}
```

- `coordinates` 的嵌套顺序为：多边形 → 环 → 顶点 → `[x,y]`。
- 每个多边形的第一个环是外环，后续环是孔洞；环方向不作为语义依据。
- 每个环必须首尾闭合，至少有三个不同顶点，面积大于零，不得自交。
- 孔洞必须位于对应外环内，不得与外环或其他孔洞交叉；不同多边形内部不得重叠。
- 全部坐标仍为 `slide_um`，不使用地理经纬度。这是本协议的几何结构。
- 简化后的轮廓用于展示；上游应在 provenance 声明简化方法和容差。

若需保留精确实例 mask，可以额外交付独立实例 ID 栅格及 `instance_id → object id` 映射。
该附件不属于 v1 必需字段，接入格式需另行约定；类别索引 mask 不能替代实例身份。

## 5. 空间坐标与转换

### 5.1 统一坐标

- 原点是 WSI level-0 左上像素的左上边界。
- 单位为微米，X 向右、Y 向下；数值保持浮点精度。
- 矩形使用半开区间 `[x0,x1) × [y0,y1)`。
- WSI 物理范围为 `[0,width × source_mpp_x) × [0,height × source_mpp_y)`。
- 必须使用与接收侧一致的 WSI 和 X/Y MPP；MPP 缺失时由上游与接收侧约定显式覆盖值，不得猜测。

### 5.2 从推理 patch 转换

上游必须先撤销模型输入的 letterbox、resize、padding、旋转或翻转，得到原始采样 patch 的坐标，再执行物理坐标转换。
当 patch 的左上像素边界为 `origin_um`，采样分辨率为 `patch_mpp` 时：

```text
x_um = origin_um_x + x_patch × patch_mpp_x
y_um = origin_um_y + y_patch × patch_mpp_y
```

这里的 `x_patch/y_patch` 是相对 patch 左上边界的连续坐标：

- 数组第 `(row,col)` 个像素的中心为 `(col + 0.5, row + 0.5)`。
- 模型已给出连续中心坐标时，不得再无条件加 `0.5`。
- bbox 的边界坐标按边界直接转换，不添加中心偏移。
- 若 patch 原点来自目标 MPP 虚拟画布，使用该画布的 MPP 换算原点；不得直接当作 level-0 像素原点。
- 推理输入中的填充区域不属于有效预测区域。

例如 `origin_um=[1000,500]`、`patch_mpp=[0.5,0.5]`，中心热图像素 `(row=35,col=24)` 对应 `[1012.25,517.75]` 微米。

最终中心必须在 WSI 和 coverage 内；仅有 bbox 的对象以框中心作为 coverage 归属点，这不表示生物学中心。
bbox 和轮廓可以跨 coverage 边界，但必须位于 WSI 闭合外边界内；超出 WSI 的几何应由上游裁剪，空框或退化轮廓不得交付。

## 6. Coverage：已预测区域

coverage 表示经过本次推理与后处理、可以解释检测结果的区域，与组织区域、置信度、类别和对象包围范围不同。
在 coverage 内零检出表示“按本次模型及阈值没有检出对象”；coverage 外表示“本次没有有效预测”。

每个 manifest 必须选择以下一种形式，不得混用：

### 6.1 全片有效

```json
{"type":"full_slide"}
```

只有整张 WSI 范围均存在有效预测时允许使用。仅推理组织 ROI 时应交付 ROI coverage。

### 6.2 矩形并集

```json
{"type":"rectangles","bounds_um":[[1000,500,3048,1524],[3048,500,4000,1524]]}
```

矩形使用半开区间，必须在 WSI 范围内且面积为正；有效区域为它们的并集，允许重叠。
空列表表示没有有效预测，此时对象数必须为零。
不得用对象 bbox 的并集充当推理 coverage，也不得用大矩形包围盒掩盖未预测空洞。

### 6.3 二值栅格

```json
{
  "type":"raster",
  "path":"prediction-coverage.tif",
  "origin_um":[1000,500],
  "mpp":[0.5,0.5],
  "dimensions":[4096,2048]
}
```

沿用分割契约的 coverage 规则：单通道 `uint8` TIFF，仅允许 `0/1`，无损压缩，金字塔使用 nearest-neighbor。
`dimensions` 为 `[width,height]`，必须与文件一致；MPP 必须为有限正数。
值 `1` 表示有效预测，值 `0` 以及栅格范围外表示未预测。
点的归属通过 `floor((point_um - origin_um) / mpp)` 定位栅格像素。
coverage 分辨率必须足以表达有效区域边界；不得因降采样把未预测区域声明为有效。

## 7. Provenance 与最终后处理

`provenance` 必须包含：

| 字段                  | 约束                                                 |
| --------------------- | ---------------------------------------------------- |
| `model_name`        | 非空模型名称                                         |
| `model_revision`    | 可追踪的权重版本、校验和或运行版本                   |
| `source_mpp`        | 两个有限正数，必须与接收侧用于该 WSI 的 MPP 一致     |
| `source_mpp_source` | 非空字符串，说明来自切片元数据或显式覆盖             |
| `postprocessing`    | JSON 对象，至少包含 score_threshold 和 deduplication |

条件必需字段：记录含 `score` 时必须提供非空 `score_definition`；含 `center_um` 时必须提供非空 `center_definition`。
建议额外提供输入 MPP、输入尺寸、软件版本、运行时间，以及实例提取、轮廓简化等规则。

- `score_threshold` 必须为 `[0,1]` 数值，或在没有分数过滤时为 `null`。分类别阈值应附带类别到阈值的映射及解释。
- `deduplication` 必须用非空字符串说明跨窗口去重算法、阈值、单位、是否按类别以及保留策略。
- 无需去重时必须说明原因，例如所有输出来自互不重叠的负责区域。
- 上游必须完成滑窗重叠区融合，保证一个最终对象只交付一次。
- 细胞核中心检测应选择适合相邻细胞的中心距离或实例匹配策略；框检测应声明采用的 NMS 或其他融合规则。
- 应删除由 patch 填充产生的对象，并明确边缘对象的裁剪及保留规则。
- 上游输出阈值决定可交付对象集合；浏览器只能进一步筛选，降低显示阈值不能恢复未交付对象。

## 8. 双方验收

### 8.1 接收校验

接入实现应在注册或导入阶段校验以下事项，违规交付应拒绝：

- schema、encoding、coordinate_system 均为支持值，必需字段完整。
- slide_id 已注册，公共 ID 合法；重复图层注册具有明确的替换流程。
- MPP 与 WSI 一致，revision 非空，引用文件存在且位于交付目录内。
- 对象文件可解析，实际记录数等于 count，对象 ID 唯一。
- 类别 ID、RGBA、score 和所有几何数值合法；未知类别、NaN、Infinity 不得通过。
- 对象至少有中心或框；框、轮廓及其关系满足第 4 节要求。
- coverage 形式合法；TIFF 的通道、dtype、尺寸、取值符合声明。
- 对象归属点位于 WSI 和 coverage 内，几何不超出 WSI。
- provenance 及条件必需的 score、center 语义说明完整。

接收侧只能校验可验证的结构和空间约束；模型是否正确、coverage 是否真实、是否仍有重复对象由上游负责。

### 8.2 上游必须提供的联调样例

首次接入至少交付以下样例及对应 WSI 或双方可访问的 slide_id：

1. 局部中心点检测：提供已知点位，核对 patch 原点和像素中心转换。
2. 局部框检测：包含靠近 ROI/切片边缘的框，核对 resize、padding 和边界裁剪。
3. 零检出但 coverage 非空：`count=0`，对象文件为空，证明结果可与未预测区区分。
4. 重叠滑窗：提供融合前后的对象数和策略，证明交付的是最终去重结果。
5. 存在孔洞的预测区域：使用精确矩形并集或二值 coverage，证明空洞仍为未预测。
6. 若交付轮廓，提供带孔洞或多个连通部分的对象，验证结构与 bbox 一致。

应当在高倍率 WSI 上核对中心、框和轮廓对齐，再核对低倍率空间查询；可附着色预览辅助检查，但预览不替代规范数据。

## 9. 版本演进

- 不兼容字段、坐标或编码变更必须提升 schema 主版本。
- 新增可选展示信息可以保留 v1；接收侧可以忽略未知可选展示字段。
- 未知 schema、encoding、coordinate_system、coverage.type 或几何 type 必须拒绝，不能通过忽略字段猜测坐标或语义。
- objects 与 indexed 分割图层分别注册；共享 slide_id 和类别约定时，应由上游显式保证两者一致。

## 10. 当前 viewer 的接入与显示

与 indexed overlay 共用 `--overlay` 和 `--overlay-root` 入口：

```bash
uv run patchkit-web --slide case-001=/data/case-001.svs \
  --overlay /data/case-001/nuclei/run-001/prediction.json
```

JSONL 在注册时导入临时磁盘 SQLite R-tree，运行期按视口查询，前端 Canvas 批量绘制。
索引在服务关闭时清理，重启后重新导入。已注册的数据是不可变快照；更新交付需重新注册或重启服务。

- 高倍率条件：每个屏幕 CSS 像素对应的 level-0 像素数不超过原生 level 1 的最小 X/Y 降采样比例。
  更高倍率也显示；只有 level 0 的切片使用比例 1。此处 level 是切片原生层级，不是浏览器瓦片层级。
- 点半径以屏幕 CSS 像素计，范围 1–24，默认 4；支持实心和空心，空心描边宽度为 1.5 px。
- 颜色按类别调整；全局透明度与类别 RGBA 的 alpha 相乘。
- 点查询分页返回，视口外保留圆半径余量；拖动或缩放时取消旧请求。
- 类别全选、反选和显隐与已有分割图层保持一致。
- 当前接入校验轮廓类型、闭环结构、面积和 bbox 包围关系；轮廓拓扑合法性仍须由上游保证。
