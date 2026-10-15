# 阶段 0 样本与依赖清单

登记日期：2026-10-13。小型可重建夹具在 tests/phase0_reference.py 与 tests/phase0_fixtures.py。

| 样本 | 构造或位置 | 物理/像素特征 | 已完成与后续用途 |
| --- | --- | --- | --- |
| RGB X/Y 梯度 | gradient()/pixels()，8×4，R=10x、G=20y、B=0 | 默认原生 MPP=(1,1) | 独立点核参考、相位、边界与 legacy 快照 |
| 脉冲/阶跃 | impulse() 7×7 中心255；test_keys_impulse_and_saturation 中 1×8 阶跃 | float64 插值，uint8 饱和 | Keys 核及量化参考 |
| PNG | write_backend_samples(tmp_path)，gradient.png | RGB uint8 8×4，无内置 MPP，显式 source_mpp=(0.25,0.5) | 实际 ImageReader 全区读回已核对；精确采样待阶段 2 |
| TIFF 金字塔 | write_backend_samples(tmp_path,1000,333)；另 (800,267) | tiled RGB SubIFD，两层，MPP0=(0.25,0.5)，level1 的坐标色 R=x%256、G=y%256、B=(x+y)%256 | 实际 TIFF 多个原生起点读回已核对；插值/掩码待阶段 2/5 |
| 非等轴 downsample / 800/271 | 验收 A04 中明确 metadata=(1000/333,800/271)，各轴独立参考 | 与物理非等轴 MPP 区分 | native 整数起点解析回归已通过；混合轴真实后端校准待阶段 5 |
| uint16 native | ArrayReader(native=True) | 单通道梯度×100，uint16 | 旧 native 快照；真实高位深 TIFF 可复用 tests/test_tiff_reader.py 的写入器 |
| 灰度/RGBA | tests/test_image_reader.py 的实际 PNG fixture；A14 固定合成像素 | uint8 灰度、四通道白底合成 | 既有读图回归；新增精确核格式测试待阶段 2 |
| OpenSlide 候选 | slides/133S.tif（本机，642,004,494 字节） | OpenSlide.detect_format 返回 generic-tiff | 已核实存在与格式识别；未验收多层 metadata/MPP/像素相位；阶段 5 读取多个层、边界及偏移区域校准 |
| KFB 候选 | uploads/2643101-7.kfb（本机，4,533,525,363 字节）；其他 uploads/*.kfb 可作补充 | 真实 vendor 文件，未加载大型索引 | 已核实存在与 Python backend 可导入；层数、MPP、区域解码与网格尚未验收；阶段 5 单独运行有界校准 |

TIFF 自动识别金字塔的启发式不接受任意比例：本环境 800→271 SubIFD 被识别为独立系列，
不进入 series[0].levels。因此可重建真实样本使用 800→267，800/271 仍保留解析测试和后端待验收项。
不修改 TIFF reader 来让阶段 0 夹具通过；本阶段仅冻结基线。
vendor 文件不提交到仓库，也不加入普通 pytest 自动运行。部署端要提供具有可确定网格的代表性样本，
记录来源授权、hash、层级尺寸、逐轴 downsample、MPP、选层和参考 region；本地候选不等于完成校准。

## 依赖

环境由 uv 管理，解释器 .venv/bin/python，测试 .venv/bin/python -m pytest。
本次依赖版本：numpy 2.5.3、Pillow 12.3.0、tifffile 2026.9.9、pytest 9.1.1、
openslide-python 1.4.6、openslide-bin 4.0.1.2、kfbslide 0.3.3；确切解析依赖见 uv.lock。
OpenSlide 需要 libopenslide（此环境由 openslide-bin 提供）；KFB 需要 backend 及其实际 tile 解码依赖。
导入成功不能作为整个后端验收。当前 PNG/无压缩 TIFF 夹具无需额外 imagecodecs；
压缩 vendor 样本需在校准时记录 codec 可用性。

## 阶段 0 状态

原始需求已恢复归档、契约已冻结、A01–A16 执行清单已落实；独立参考自测与旧像素回归作为阶段 0 自动检查。
未来阶段仍需实现公共 API、严格迁移提示、结果流和 vendor 校准。预计投入以这些阶段的实际范围另行评估。
