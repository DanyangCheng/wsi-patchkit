# KFB 转 TIFF

转换器复用项目的 `KfbSlideReader` 和持久化 SQLite 索引，输出分块 BigTIFF。
默认保留 KFB 原有的所有金字塔层、各层尺寸、MPP 和扫描属性。
标签图、宏观图等关联图像不导出，也不会自动生成 KFB 中不存在的金字塔层。

## 单文件转换

在项目根目录运行：

```powershell
uv run --extra tiff-codecs kfb-to-tiff D:/slides/sample.kfb D:/tiff/sample.tiff --workers 4
```

也可以直接以 Python 模块运行：

```powershell
uv run --extra tiff-codecs python -m wsi_patchkit.kfb_to_tiff D:/slides/sample.kfb D:/tiff/sample.tiff --workers 4
```

`tiff-codecs` 安装 JPEG TIFF 读写所需的 `imagecodecs`。
无损 Deflate 和无压缩输出无需安装该额外依赖：

```powershell
uv run kfb-to-tiff D:/slides/sample.kfb D:/tiff/sample.tiff --compression deflate --workers 4
```

## 性能策略

- 默认 `--compression jpeg`：对块尺寸、JPEG 颜色格式和色度采样兼容的块，
  直接复制原始 JPEG 字节，省去解码、拼接和重新编码，且这些块没有新增有损压缩。
- 边缘块、缺失块、采样不一致的块或重设分块尺寸后的块，通过项目的读取器转换；
  透明背景合成白色。无法直接复制的块会按 `--quality` 重新 JPEG 压缩，默认质量 90。
- 分块读取和必要的编码使用线程池。每个线程有独立的 KFB 句柄和索引连接，
  TIFF 按行优先顺序由一个线程写入，避免共享读取句柄或并发写 TIFF。
- 不把整张切片解码进内存；默认最多排队 `2 × workers` 个块。
  `--buffer-tiles` 可以进一步限制队列。TIFF 块偏移表仍随块数量增长。
- 原生分块尺寸能用于 TIFF 时默认沿用，否则使用 512。
  `--tile-size` 必须是 16 的正整数倍；改变块尺寸会取消原始 JPEG 直接复制。
- 索引缓存默认保存在输出文件旁的 `.kfb-index` 文件夹，后续运行可以复用。
  可用 `--index-cache-dir D:/cache/kfb-index` 指定共享缓存目录。

`--workers` 默认取 CPU 数量与 4 中的较小值。建议从 2–4 个线程开始测量，
也可以尝试 1 个线程：如果绝大多数块可以直接复制，磁盘吞吐通常比 CPU 更关键，
增加线程不一定更快。机械硬盘、网络共享尤其需要根据实际读写速度调整。
实际速度还受切片尺寸、索引首次构建、异常块修复和磁盘性能影响。

进度会显示每层已完成的块数。完成后输出耗时、文件大小和直接复制的块数，
可据此判断是否走到了快速路径。

本机合成数据测试：4096 × 4096 RGB 图像、256 个 256 × 256 JPEG 块、
KFB 文件约 18.9 MiB。以下时间包含 TIFF 写入；第一次复制还包含索引构建，
后续运行复用索引。测试源数据和磁盘缓存条件不同于真实大切片，结果用于比较路径：

| 转换方式 | 1 个线程 | 4 个线程 |
| --- | --- | --- |
| 原始 JPEG 复制 | 0.082 秒 | 0.077 秒 |
| 全部重新 JPEG 编码 | 0.504 秒 | 0.331 秒 |
| 无损 Deflate | 1.639 秒 | 0.475 秒 |

另测 32 × 32 小块时，线程调度开销超过并行收益。因此对于小块切片或纯复制场景，
可尝试 `--workers 1`，并通过 `--jobs` 并行转换多个文件。

## 批量并行转换

```powershell
uv run --extra tiff-codecs kfb-to-tiff D:/slides D:/tiff --recursive --jobs 2 --workers 4
```

输入目录只选择 `.kfb` 文件，扩展名大小写不敏感。`--recursive` 包含子目录，
输出保留相对目录结构，例如 `D:/slides/group/a.kfb` 写入 `D:/tiff/group/a.tiff`。

`--jobs` 控制同时转换的文件数，默认 1；`--workers` 是每个文件的块处理线程数。
上例最多使用 8 个块处理线程，另有文件调度线程。避免同时把两者设得过大。
批处理中单文件失败会报告原因并继续处理其他文件，最终退出码为 1；全部成功为 0。

## 质量和输出选项

| 参数 | 用途 |
| --- | --- |
| `--compression jpeg` | 默认快速路径，兼容的原始块直接复制 |
| `--verify-jpeg` | 复制前完整解码检查 JPEG，降低损坏压缩数据被原样复制的风险；增加开销 |
| `--reencode --quality 95` | 所有 JPEG 块统一重新编码，质量参数作用于全部块；新增有损压缩 |
| `--compression deflate` | 快速级别 1 的无损压缩，保留 KFB 读取器返回的 RGB 像素 |
| `--compression none` | 无压缩，保留 RGB 像素，但文件可能非常大 |
| `--level0-only` | 只输出最高分辨率层，减少总读写量 |
| `--overwrite` | 成功转换后替换已有目标，默认拒绝覆盖 |

默认快速路径检查 JPEG 标记、尺寸和颜色采样，不完整解码每个原始块。
若需要检查原始压缩数据，请加 `--verify-jpeg`；若要求全部像素无新增有损变化，
使用 `--compression deflate` 或 `--compression none`。
“无损”指保留已解码的 KFB 像素，不能恢复 KFB 原始 JPEG 压缩前的信息。

输出总是 BigTIFF，支持超过 4 GiB 的文件；打开软件需要支持 BigTIFF。
原有缩小层使用 TIFF SubIFDs 保存。一些第三方读取器仅识别整数倍率金字塔；
本项目的 `TiffReader` 也支持 KFB 常见的非整数倍率缩小层。

脚本先写同目录下的临时 `.partial` 文件，成功后再发布目标。
正常异常退出会清理临时文件，已有目标只有在 `--overwrite` 且转换成功后才会被替换。
默认的无覆盖发布使用文件系统硬链接，支持 NTFS 和常规 Linux 文件系统；
不支持硬链接的输出文件系统可使用 `--overwrite` 发布到允许替换的目标路径。

## Python 调用

```python
from wsi_patchkit.kfb_to_tiff import convert_kfb_to_tiff

result = convert_kfb_to_tiff(
    "D:/slides/sample.kfb",
    "D:/tiff/sample.tiff",
    workers=4,
    compression="jpeg",
    buffer_tiles=8,
    progress=print,
)
print(result.seconds, result.copied_tiles, result.tiles)
```
