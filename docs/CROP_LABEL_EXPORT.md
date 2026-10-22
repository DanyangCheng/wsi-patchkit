# 图像与掩码裁剪

裁剪任务默认读取当前切片的全部 indexed overlay，并保存类别 ID、有效区域和空间元数据。
查看器勾选“同步保存全部类别掩码”时，带掩码裁剪须选择 PNG。检测框图层不参与该流程。
API 可用 `overlay_ids` 选择部分图层；空数组表示仅裁剪图像，省略表示全部类别图层。
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
- 两者与输出图像宽高相同，通过原图坐标、层级缩放、掩码原点和 MPP 映射到同一输出
  网格，使用最近邻采样。局部覆盖及非等向 MPP 均按各轴处理。
- 元数据 schema 为 `wsi-patchkit-crop/v1`，保存源切片 ID、原生层级区域、level-0 区域、
  源 MPP、实际输出 MPP、输出尺寸、图层版本、类别表及掩码相对路径。
  矩形使用半开边界，采样位置为输出像素中心；输出尺寸取整后的实际 MPP 可能略有变化。
- 仅裁剪图像时也保存元数据。没有这些附属文件的历史图片仍可下载。
- 全部文件先写入临时目录，成功后发布结果。渲染或写入失败时清理本次任务的文件。
  删除裁剪图像时一起删除附属目录；任务历史清理保留图像和附属目录。

## 导出与扩展

结果库支持“下载图像与标签”；批量下载默认包含图像、掩码及元数据。
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
```

首版内置 `indexed-png` 导出器。后续的多边形、检测框或实例标签转换需要各自定义规则；
语义类别索引不包含原始实例 ID。
