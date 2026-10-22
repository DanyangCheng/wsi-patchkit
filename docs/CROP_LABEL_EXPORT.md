# 图像与掩码裁剪

裁剪任务默认读取当前切片的全部 indexed overlay，并保存类别 ID、有效区域和空间元数据。
查看器勾选“保存类别掩码”后，可选择两种掩码范围，带掩码裁剪须选择 PNG：

- **全部类别掩码**：保存全部 indexed 图层的完整类别索引，与显示勾选无关。
- **当前显示的类别掩码**：保存提交时已启用、透明度大于 0 的 indexed 图层中，
  已勾选显示的类别。关闭的图层和检测框图层不参与。没有可见类别时仅保存图像和元数据。

第二种方式保留原始类别 ID，不保存显示颜色。未选中的类别像素填 0，同时 coverage 为 0；
原本有效且选中的类别 0 仍保留 coverage 为 1。任务和元数据保存类别选择快照，
后续修改显示状态不影响已提交任务，重试沿用原选择。
关闭“保存类别掩码”则只保存图像和元数据。
API 可用 `overlay_ids` 选择部分图层；空数组表示仅裁剪图像，省略表示全部类别图层。
可选 `overlay_class_ids` 是 `{图层ID: [类别ID, ...]}`，用于筛选已选图层中的类别；
省略某个图层的筛选表示保留该图层全部类别，空类别列表表示该图层输出全无效。
类别 ID 必须是该图层声明的整数，筛选不能引用未选图层。
失败任务重试保留图层选择。

```json
POST /api/slides/case-001/crops
{
  "x": 0, "y": 0, "width": 512, "height": 512,
  "level": 0, "format": "png", "target_mpp": 0,
  "overlay_ids": ["prediction"]
}
```

## 保存结构

每张图像有一个同名附属目录，使用相同的账号隔离、下载和删除权限：

```text
sample.png
sample.png.labels/
  metadata.json
  layer-0/
    mask.png
    coverage.png
  layer-1/                 # 多图层时继续编号
    mask.png
    coverage.png
```

- `mask.png` 是单通道无损类别索引，不使用显示调色板或透明度。类别 ID 不超过 255 时
  使用 uint8，否则使用 uint16；PNG 输出支持 ID 0–65535。
- `coverage.png` 是单通道 uint8，0 表示无预测，1 表示有预测。无覆盖处索引填 0，
  读取标签必须同时检查 coverage，类别 0 本身仍可为合法类别。
  按类别筛选时，coverage 表示“有预测且属于所选类别”，0 也可能表示类别未被选中。
  每层 `coverage_values` 描述具体语义，`selected_class_ids` 为所选 ID（null 表示全部类别）；
  根级 `overlay_class_ids` 保存请求时的筛选映射。
- 两者与输出图像宽高相同，通过原图坐标、层级缩放、掩码原点和 MPP 映射到同一输出
  网格，使用最近邻采样。原生层级保留精确的非整数缩放边界，读取时使用稳定的
  level-0 整数定位，避免选中前一个原生像素。局部覆盖及非等向 MPP 均按各轴处理。
- 元数据 schema 为 `wsi-patchkit-crop/v1`，保存源切片 ID、原生层级区域、level-0 区域、
  源 MPP、实际输出 MPP、输出尺寸、图层版本、类别表及掩码相对路径。
  矩形使用半开边界，采样位置为输出像素中心；输出尺寸取整后的实际 MPP 可能略有变化。
- 仅裁剪图像时也保存元数据。没有这些附属文件的历史图片仍可下载。
- 全部文件先写入临时目录，成功后发布结果。渲染或写入失败时清理本次任务的文件。
  删除裁剪图像时一起删除附属目录；任务历史清理保留图像和附属目录。

## 导出与扩展

结果库支持“下载图像与标签”；批量下载默认包含图像、掩码及元数据。
个人管理与裁剪图管理页面的“标签格式”选择器从后端注册表读取可用格式，
用于单份标签包和批量下载。新增导出器后刷新页面即可选择。
原有 `/api/crops/{filename}/download` 继续提供单张图像。

- `GET /api/crops/exporters`：返回可用格式的 ID 和名称。
- `GET /api/crops/{filename}/export?label_format=indexed-png`：下载单份结果 ZIP。
- `POST /api/crops/batch-download`：JSON 参数为 `filenames`、可选 `include_manifest`、
  可选 `label_format`（默认 `indexed-png`）。未知格式返回 400。

导出读取已保存的数据，不依赖源 WSI 或原始预测文件。应用启动时可注册可信的 Python
适配器，注册 ID 不允许重复。适配器接收 `CropBundle` 和已打开的 `ZipFile`；图像已由
公共流程加入 ZIP，适配器应使用 `bundle.labels.name` 作为目录前缀，避免多份结果重名。
`bundle.metadata` 提供元数据，`bundle.files()` 遍历关联文件。

```python
from wsi_patchkit.web import create_app

app = create_app({"case-001": "/data/case-001.tif"})


def export_custom(bundle, archive):
    metadata = bundle.metadata
    # 在这里读取 metadata["overlays"] 引用的 mask/coverage，并转换成目标标签格式。
    archive.writestr(bundle.labels.name + "/custom.txt", metadata["slide_id"])


app.state.crop_exporters.register("custom", export_custom, name="自定义标签")

# 也可在创建应用前准备注册表：
# from wsi_patchkit.web import ExporterRegistry
# registry = ExporterRegistry()
# registry.register("custom", export_custom, name="自定义标签")
# app = create_app({"case-001": "/data/case-001.tif"}, crop_exporters=registry)
```

首版内置 `indexed-png` 导出器。后续的多边形、检测框或实例标签转换需要各自定义规则；
语义类别索引不包含原始实例 ID。
