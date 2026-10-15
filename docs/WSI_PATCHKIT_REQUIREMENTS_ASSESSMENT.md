# wsi-patchkit 读取与采样需求评估

评估日期：2026-10-13。需求来源：根目录 `WSI_PATCHKIT_REQUIREMENTS.md`。
依据：当前源码、相关现有测试，以及本次小型梯度图和生命周期复现。

## 1. 结论

需求文档的主要缺口判断成立。现有库已经提供格式读取、物理元数据、整数虚拟画布采样和有界并行流，但尚未提供文档要求的连续物理网格精确采样契约。完成需求属于采样核心和流生命周期的中等偏大扩展，不能通过增加几个插值参数完成。

建议新增严格校验的物理请求、独立采样计划和单请求精确读取入口，保留旧 helper 的默认像素行为。精确入口统一承担坐标、插值、填充、掩码和读取限额；直接读取和流均调用它。vendor reader 继续执行原生区域 IO，无需各自实现插值。

P0 共 6 项（R01/R02/R05/R06/R07/R10），P1 共 4 项（R03/R04/R08/R09）；这些不是十项彼此独立的实现。R01/R02/R05 是同一套输出网格契约，R03/R04/R08 共同决定读取计划，R06/R09 共同影响流的结果与资源管理。

## 2. 逐项缺口与改动

| 需求 | 当前证据 | 实现改动 | 复杂度 |
| --- | --- | --- | --- |
| R01 连续物理坐标，P0 | `types.py:190` 对 x/y 执行 `int()`；现有请求只有目标画布整数坐标 | 新增 `PhysicalPatchRequest`，保存 `origin_um`、目标 MPP、尺寸和逐请求选层参数；提供旧请求显式转换 | 中 |
| R02 精确采样，P0 | `geometry.py:99` 规划 floor/ceil 原生包围框；`:269` 将整个读取区域 resize 到裁剪输出尺寸 | 新增采样计划与中心坐标重采样；原生整数范围扩展只服务 IO，不改变输出相位；结果保存输出网格与实际原生层 | 高 |
| R03 插值，P1 | `geometry.py:17` 只有 nearest/bilinear/area；nearest 使用左边缘索引，bilinear/area 使用 Pillow resize | 为新入口实现中心索引 nearest、双线性、Keys cubic a=-0.5 和规定的量化；单独定义精确 area 语义；native 类别数据只允许 nearest | 高 |
| R04 填充，P1 | `PatchRequest.fill_value` 是单个 uint8；没有 padding_mode；旧 helper 已能对完全越界请求跳过区域读取 | 增加 constant/reflect、RGB 三通道填充值；逐插值邻点反射；长度 1 和远距离越界处理；沿用灰度与 RGBA 转换 | 中至高 |
| R05 几何掩码，P0 | `geometry.py:285` 将 destination_box 内全部设为 true，边界来自 round 画布 | 按每个输出中心与 level-0 物理半开范围比较；与填充、插值邻域和 canvas 舍入解耦 | 中，依赖 R02 |
| R06 流结果与选层，P0 | `stream.py:76` 只构造 `Patch(request,image)`；没有 level/level_policy；已有独立 reader、元数据缓存和顺序输出 | 扩展兼容结果字段或新增结果流；逐请求显式 level 优先；统一调用单请求读取；保留已有有界调度 | 中 |
| R07 舍入策略，P0 | `SlideSpec.from_metadata` 与 `virtual_canvas_size` 分别硬编码 round；读取时重新计算画布；网格只接收 canvas_size | 抽取共同画布计算，公开 round/ceil；策略经 SlideSpec、sampler、request/读取配置传递；精确掩码独立判断 | 中 |
| R08 读取限额，P1 | patch helper 和 stream 没有输出或原生读取像素限额；`TileRenderer` 有独立的分块读取限制 | 在大数组分配前检查输出像素，在 IO 前检查包括核邻域的原生读取像素；公开计划与有上下文的超限错误 | 中 |
| R09 生命周期，P1 | `stream.py:127` 在消费线程关闭 worker reader；ExitStack 清理错误可能覆盖读取错误；请求迭代器没有显式 close；串行空流会创建 reader | worker 内关闭自有 reader；显式请求迭代器所有权；延迟创建；保持原始异常优先；取消排队任务并等待已开始 IO | 高 |
| R10 校验与兼容，P0 | `as_size` 对序列元素 int 转换，`as_mpp(True)` 接受 bool；helper 的显式 level 直接索引 levels，负数可选最后一层 | 新入口严格字段验证，统一选项/类型错误；保留旧行为或明确发布破坏性变更；新增结果字段有默认值 | 中，横跨模块 |

源码行号指评估时版本。

## 3. 已有能力与需求描述的细化

### 3.1 非整数层级起点已有修复

`geometry.py:137` 已使用 `ceil(nextafter(native_index * downsample, -inf))`，避免 TIFF 的 `floor(location/downsample)` 读到前一个像素。`tests/test_tiff_reader.py:112` 已覆盖 10/3 层级比。

因此 A04 不是从零建设。新增精确计划应复用该思路并扩展非等轴和多个索引测试；现有修复解决原生读取起点，不解决扩展包围框 resize 导致的相位变化。

### 3.2 max_pending 的主要行为已符合需求

并行流先提交最多 max_pending 项，`yield pending.popleft().result()` 返回后，消费者请求下一项才补交一项。在暂停于 yield 时，pending 队列虽已减一，但当前 yielded 请求仍占据原有容量，没有额外预取。

主要工作是准确文档化并补充 `max_pending=1`、小于 worker 数、慢消费者及提前关闭测试。当前限制是请求数量，不是内存字节预算。

### 3.3 数据类型和输出所有权已有基础

`geometry._as_rgb` 已拒绝非 uint8，扩展灰度为 RGB，并将 RGBA 合成到白色背景；`ImageReader` 已拒绝不支持的高位深图像模式。TIFF native 读取已支持 uint16。

旧 helper 将读取结果写入自己分配的输出数组，已有输出所有权基础。新入口仍须保证返回数组不引用会复用的后端缓冲区，补充 A15 生命周期回归。完全越界 native 请求不能继续只凭默认的 uint8 单通道填充推断格式：应利用元数据或明确限制支持范围。

### 3.4 TileRenderer 有相关实现，但不是直接替代品

`tiles.py` 的大区域分块渲染已有全局输出网格与读取块控制，可参考其邻域规划。它使用 Pillow transform、独立选层规则和不同的 IO 起点取整，没有所需物理请求、完整掩码和统一插值量化契约。

本次无需更改 viewer/IIIF 行为。后续抽取共用内核必须单独做像素与缓存回归，不能直接将 viewer 路径视为已完成 R02/R08。

## 4. 建议的代码结构与影响范围

| 文件/模块 | 建议改动 | 对现有调用的影响 |
| --- | --- | --- |
| `types.py` | 物理请求、输出网格、严格校验工具；为 Patch 增加默认可选字段；SlideSpec 增加舍入配置 | 保留 Patch(request,image)；避免直接全局收紧旧转换函数导致隐式破坏 |
| 新增 `sampling_plan.py` | 选层、输出网格、插值邻域、边界映射、读取范围和限额检查 | 新入口使用；旧 ReadPlan 保留自身契约 |
| 新增 `resampling.py` | nearest/bilinear/bicubic 核、量化、clamp/reflect、面积采样（按确定的契约） | 新精确行为与 Pillow 旧滤波分开 |
| 新增 `reading.py` 或精确读取模块 | `plan_physical_read` / `read_physical_patch_result` 公共入口，共享格式转换与结果封装 | 新调用方显式接入；旧 helper 保留 |
| `geometry.py` | 统一可复用画布计算、RGB 转换及原生起点转换；为旧模式增加显式可选 canvas 配置 | round 及旧 resize 默认不变；ceil 需完整传递 |
| `sampling/grid.py` | 传递画布策略，提供转物理请求路径 | align/drop 算法无需重写 |
| `sampling/random.py`、`indexed.py`、`tissue.py`、`base.py` | 核查请求类型注解、构造和坐标假设；策略传播按公开支持范围处理 | 组织掩码仍在已有画布空间；不能直接将任意物理请求当整数请求处理 |
| `stream.py` | 精确入口分派、完整结果、逐请求选层、限额、请求所有权、异常清理和线程归属 | 旧流默认结果像素保留；新模式与配置明确 |
| `io/base.py` 与具体 reader | 文档化原生起点映射、返回数组和线程约束；校准测试，必要时局部适配 | 保持 read_region 签名；无需后端实现重采样 |
| `__init__.py` | 导出新的请求、网格、计划、结果及读取函数 | 增量公共 API |
| `torch/dataset.py` | 回归现有请求/transform 行为；如承诺精确采样支持则透传模式与参数 | 默认字典 keys 和张量转换保持现状；不是本次必需的新输出形式 |
| `tests/`、README、CHANGELOG、示例 | 增加 A01–A16 契约测试、兼容输出回归与串行/并行接入示例 | 明确新旧行为与版本迁移 |

上表中的新增文件名是职责建议，不是验收约束。采用新公开结果流也可减少旧 PatchStream 的配置分支，但调度和单请求算法应共享。

建议最小 API：

```text
PhysicalPatchRequest(
    slide, origin_um, output_size, target_mpp,
    source_mpp=None, level=None, level_policy="nearest",
    interpolation=..., padding_mode=..., fill_value=...
)

plan_physical_read(metadata, request, limits=...) -> SamplingPlan
read_physical_patch_result(reader, request, metadata=None, limits=...) -> Result

Result: request / image / valid_mask / grid / source_level / plan
```

请求与读取配置的实际拆分可调整，但选项必须有一个明确的最终值。若同时支持流默认和请求覆盖，需要定义请求覆盖流默认、显式 level 覆盖 policy，且非法 policy 不因当前显式 level 存在而静默忽略。

## 5. 实现前需要确定的语义与风险

### 5.1 area 在精确入口中的语义还不够具体

现有 area 是原生整数包围框上的 Pillow BOX resize，只允许两个轴均不放大，不能直接作为连续物理网格的精确面积采样。

建议新入口定义：输出像素对应物理矩形，与原生像素单元计算重叠面积后加权平均；仅 RGB，两个轴均不放大，使用规定 uint8 量化。同时确定边界 footprint 的处理，尤其输出中心无效时 constant 覆盖整像素，或按覆盖面积混入填充值。旧入口继续保留 BOX 行为。该决定会影响计划所需范围与 A03/A05/A07。

### 5.2 constant、reflect 与几何掩码是不同判定

建议 constant 对几何有效中心执行核插值，核越过原生边缘时 clamp；无效中心输出填充值。reflect 则在原生全轴索引上反射每个核邻点，无效中心仍输出反射图像，而 mask 始终 false。

长度 n>1 的整数索引反射周期为 `2*(n-1)`，长度 1 固定映射到 0。不能对已经裁剪的小块简单 np.pad 后宣称等价于整张切片反射。对 bilinear/bicubic 应固定“先选核邻点再逐点反射”的规则，作为参考计算的一部分。

### 5.3 reflect 与原生读取限额相互影响

远距离越界本身不应产生一个从请求原点延伸到切片的巨大读取框；应先求需要的反射索引，再规划 IO。反射后离散索引的单个包围框也可能很大，可以超限时报错，或选择多个区域读取。

如果使用多区域，必须定义 max_read_pixels 是每次 read_region 的限制、请求总读取像素的限制，还是两者。建议第一版采用可预测的单区域计划并在超限时给出明确错误；高效分块可后续增加，需求并未要求所有超限请求自动分块。

### 5.4 OpenSlide/KFB 的实际网格仍需后端校准

TIFF 明确使用尺寸比和 floor 起点转换，现有测试可解析验证；OpenSlide 的 metadata 用逐轴尺寸比构造 downsample，但 read_region 直接转交底层库。KFB 的 metadata 使用后端标量 downsample。

不能仅凭共享公式和 level-0 起点签名保证所有后端返回块与声明原生网格完全一致。需要用代表性多层样本验证尺寸比、底层 downsample、区域起点与像素相位；不一致时在 reader 适配层校正几何或读取映射，不让 vendor backend 重写采样核。目前 OpenSlide 测试主要验证 level 0，不能覆盖 A04。

### 5.5 同线程关闭需要改变 worker 资源归属

ThreadPoolExecutor 没有用于每个 worker 的标准终结回调；简单提交 N 个 close 任务不能保证每个线程恰好执行一次。可以让固定 worker 循环在 try/finally 内完成 reader 生命周期，通过有界队列/结果 Future 保留输入顺序；也可采用能保证线程归属的等价封装。

不必改变用户可见的顺序和 max_pending 语义，但这部分不是改一行 close 所能完成。factory 自身创建一半后抛异常、没有返回 reader 时，由 factory 负责清理未交付资源；这一边界也需文档化。

### 5.6 校验收紧与兼容要求存在边界

直接修改旧 PatchRequest 拒绝所有过去允许的分数输入，会改变既有行为；同时需求要求分数 x/y 不得静默截断。建议明确新物理入口完全严格，旧入口的这类输入作为兼容例外进行弃用提示并制定拒绝的版本；若立即严格化旧入口，应将其记为兼容性变更。

新入口还应拒绝非法字符串选项、负 level/bool level、NaN/Inf 和派生坐标计算溢出。原请求字段均有限不代表 origin+size*mpp 一定有限。数值校验可在创建请求时完成，读图返回的 shape/dtype 校验在采样执行时完成。

### 5.7 限额默认值和迭代器所有权需落定

需求没有指定默认像素上限。应根据 patch 工作负载给出默认值与显式禁用方式；不应直接继承 viewer 的分块限额。计划阶段先用整数运算检查输出面积，再创建坐标、掩码和图像；原生范围应先检查包含核邻域的面积。

建议请求迭代器默认借用，提供显式拥有选项；拥有时在正常结束、失败及提前 close 均关闭。若默认拥有，则需说明复用同一个迭代器的影响。成功执行后的清理失败传播，已有读取/生成错误优先，其他清理错误保留为上下文或附加信息。

## 6. 实施顺序与验收关口

1. **固定契约与参考计算**：明确新旧入口、area/reflect 边界、选项优先级、限额及请求所有权。用独立梯度/脉冲参考锁定 R01/R02/R05/R10；仅原点保留还不构成 P0 完成。
2. **建立共享精确读取核心**：完成请求、计划、nearest/bilinear、中心掩码、显式选层和 constant，再补 bicubic/reflect/area、RGB 填充、round/ceil 和限额。同步入口通过 A01–A08/A13–A15。
3. **接入完整结果流与资源归属**：复用同步入口，保留元数据缓存、顺序和预取上限，完成 reader 与请求迭代器清理、错误优先级。通过 A09–A12，特别测试多个错误组合和 max_pending=1。
4. **兼容与格式验证**：旧构造及参考像素回归，PNG/TIFF 金字塔真实读取；按部署样本验收 OpenSlide/KFB；交付文档和示例。
5. **SlideInfer 集成**：在其项目适配层转物理请求、合并 ROI/valid_region_sample，移除重复采样与通用调度后运行下游回归。本仓库评估不能证明 SlideInfer 当前代码已可直接替换。

P0 可以先交付满足必要接入条件的明确子集，但 bicubic/reflect 等未实现公开选项必须报错。完整需求交付包含 P1 和相应资源安全验收。

最大工作量在精确重采样、reflect/area 边界和并行生命周期；类型扩展、画布策略、文档和流结果字段相对直接。建议按上述阶段拆成可单独审查的改动。未完成后端校准、area 定义与限额口径前，不宜承诺精确人日或完整接入等价性。

## 7. 本次验证结果与限制

使用项目 `.venv/bin/python` 执行，与需求直接相关的现有测试：

```text
.venv/bin/python -m pytest -q \
  tests/test_types.py tests/test_sampling.py tests/test_image_reader.py \
  tests/test_tiff_reader.py tests/test_openslide_reader.py tests/test_stream.py

74 passed in 0.34s
```

对小型 RGB X 梯度模拟 reader 的本次复现：

| 场景 | 当前 helper 实测红通道 | 需求公式的期望 |
| --- | --- | --- |
| origin=(0,0)、MPP=(3,1)、size=(2,1)、nearest | [0,30] | [10,40] |
| origin=(1.5,0)、MPP=(1.5,1)、size=(3,1)、bilinear | [14,30,46] | [18,33,48] |
| 分数虚拟 x=0.5、MPP=(1,1)、size=(3,1)、bilinear | [0,10,20]，x 被转为 0 | 保留物理 origin=0.5 后应为 [5,15,25] |
| 原生宽=3、origin=(0,0)、MPP=(2,1)、size=(2,1)、nearest | image=[0,10]，mask=[true,true] | image=[10,255]，mask=[true,false] |

另已复现：串行空流仍调用 factory；并行 close 线程不同于 reader 创建线程；读取抛 OSError 且 close 抛 RuntimeError 时，最终传播 RuntimeError，原 OSError 仅保留在异常上下文。

全量 pytest 曾推进到 110 项通过后持续无新输出，已中断；本次不声称全量测试通过，也未将停滞归因于具体模块。针对需求的上述 74 项测试已完整结束。没有运行 SlideInfer，也没有对所有真实 WSI/vendor 多层样本做验收。

本次交付为评估文档，未修改库实现。
