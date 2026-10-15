# 精确物理采样 API 契约 v1（阶段 0）

冻结日期：2026-10-13。此文规定阶段 1–4 的实现；阶段 0 只交付契约、参考计算与回归基线，不表示以下 API 已可调用。

阶段 1 已实现：PhysicalPatchRequest（含 from_legacy）、OutputGrid、SamplingLimits、SamplingPlan、
plan_physical_read 与 canvas_rounding 传播。阶段 2 已实现 read_physical_patch_result 与
PhysicalPatchResult，支持 nearest/bilinear + constant 及 native nearest。
阶段 3 已补齐 bicubic/area/reflect 像素实现与所有模式的统一限额；完整结果流按阶段 4 实施。

## 需求版本

原文从 `/home/cdy/Projects/SlideInfer/docs/WSI_PATCHKIT_REQUIREMENTS.md` 原样归档到
[WSI_PATCHKIT_REQUIREMENTS.md](WSI_PATCHKIT_REQUIREMENTS.md)。SHA-256：
`77897a2e7387fb2c17268c8bd1171dceba79b182e7232f0beea90d3867bbc3aa`。
该快照是本轮实现的需求版本，保留 R01–R10、A01–A16 编号。
本契约补充原文留给实现方的选择；变更快照或下述规则须同步更新参考与追踪表。

## 公共类型与入口

以下名称和职责固定，参数均为关键字（slide 可为首个位置参数）；不新增 vendor 重采样接口。

```text
PhysicalPatchRequest(slide, *, origin_um, output_size, target_mpp,
    source_mpp=None, level=None, level_policy="nearest",
    interpolation="bilinear", padding_mode="constant", fill_value=255,
    color_mode="rgb")
PhysicalPatchRequest.from_legacy(request) -> PhysicalPatchRequest
SamplingLimits(max_output_pixels=16_777_216, max_read_pixels=67_108_864)
OutputGrid(origin_um, target_mpp, output_size)
SamplingPlan(request, grid, source_level, native_origin, read_location,
    read_size, limits, native_mpp, native_dimensions, source_extent_um, pixel_format)
PhysicalPatchResult(request, image, valid_mask, grid, source_level, plan)
plan_physical_read(metadata, request, *, limits=SamplingLimits()) -> SamplingPlan
read_physical_patch_result(reader, request, *, metadata=None,
    limits=SamplingLimits(), plan=None) -> PhysicalPatchResult
PhysicalPatchStream(reader, requests, *, reader_factory=None,
    num_workers=1, max_pending=None, limits=SamplingLimits(),
    own_requests=False) -> iterable[PhysicalPatchResult]
```

值对象不可变；结果数组独立持有数据，可由调用方修改。尺寸一律 `(width,height)`，数组为 HWC、掩码 HW bool。
grid 描述像素边界原点与中心变换，plan 的 native_origin 是选定层整数像素索引，read_location 是 level-0 整数起点。
grid.output_to_slide_um 的齐次矩阵为
`[[target_x,0,origin_x+target_x/2],[0,target_y,origin_y+target_y/2],[0,0,1]]`，
输入向量为 `[col,row,1]`；不要求存储逐像素坐标数组。
无像素 IO 时 read_size=(0,0)，native_origin/read_location=None；仍返回选层、grid 和 plan。
提供已有 metadata 时其 path/source_mpp 必须与请求匹配，否则报错，不静默忽略覆盖。
同步入口可接收已有 plan；在任何像素 IO/输出分配前对 metadata/request/limits 重规划并比较，
不相等时报错。使用自定义 plan.limits 时须显式传入相同 limits，不能通过旧 plan 绕过当前限额。
plan 相等时结果保留传入的 plan 对象。reader 为借用资源，由调用方关闭；后端原始异常保持。
from_legacy 使用 `(x*target_mpp_x,y*target_mpp_y)`；已经丢失的分数无法恢复。

选层、插值、填充和颜色模式只由请求表达，流不提供这些默认配置，以消除覆盖歧义。
limits 只在同步/规划入口或流配置上设置，流原样传到同步入口。显式 level 优先于 policy，
但 policy 即使未被使用也必须合法。nearest 按两个轴 `sum(abs(log(level_mpp/target_mpp)))`
最小选择；finer 在两个轴均不粗于目标的层中取 MPP 面积最大者，无符合层时取面积最小者。
相同分数或面积按较小 level 打破平局。source_mpp 覆盖 level-0 MPP，并按各轴 downsample 推导层 MPP。

## 网格、核、量化

坐标单位微米，原点是 level-0 左上像素边界。第 `(r,c)` 中心：
`u=(origin_x+(c+0.5)*target_x, origin_y+(r+0.5)*target_y)`。
原生中心索引 `s=u/level_mpp-0.5`。输出边界范围为
`[origin_x,origin_x+width*target_x) × [origin_y,origin_y+height*target_y)`。
原生 IO 包围框扩展不改变任何中心。

* nearest：`floor(s+0.5)`，半整数向较大的整数选取，包括负数。
* bilinear：每轴 `floor(s),floor(s)+1`，权重 `1-frac(s),frac(s)`，二维权重乘积。
* bicubic：每轴 `floor(s)-1 … floor(s)+2`，二维可分离 Keys 核，a=-0.5。
  对 t=abs(distance)，t<1 时 `(a+2)t³-(a+3)t²+1`，1≤t<2 时
  `at³-5at²+8at-4a`，t≥2 时 0。不增加抗混叠、不归一化裁剪邻域的权重。
* RGB 累加使用 float64，最终 `clip(floor(value+0.5),0,255)`，再转 uint8；禁止用 bankers round。
  不含额外抗混叠。同层同核同物理中心必须给出相同结果。

有效性只由 level-0 物理范围决定：`0<=u_x<W0*mpp0_x` 且 `0<=u_y<H0*mpp0_y`。
右/下边界相等为 false，不加 epsilon。掩码独立于核、填充、canvas 舍入与 ROI。
几何/仿射和未量化参考比较用 `rtol=0, atol=1e-12`（小型夹具单位微米或像素）；
大坐标案例须单独写出容差，不能放宽边界判定。uint8/native/mask 逐元素完全相等。

## 边界与 area

constant：无效中心整像素填 fill_value；有效中心的所有核邻点按原生整轴 clamp。
reflect：先在原始 s 上选核邻点，再逐个整数索引反射；n>1 时
`q=i mod (2*(n-1)); index=q if q<n else 2*(n-1)-q`，n=1 时固定 0。
反射对整张原生层计算，不能在裁剪块上 pad；越界 mask 始终 false。
clamp 是 constant 的核边界规则，不是第三种公开 padding_mode。

精确 area 支持 uint8 RGB（含灰度/RGBA 转 RGB）且两个轴 `target_mpp>=level_mpp`，
任一轴放大即明确报错。输出像素的物理 footprint 为
`[origin_x+c*target_x,origin_x+(c+1)*target_x) × ...`。
原生单元 i 为 `[i*level_mpp,(i+1)*level_mpp)`，按重叠面积除以整个 footprint 面积加权。
constant 的无效中心仍整像素填充；有效中心越界的 footprint 单元 clamp 到原生边缘，
不混入 fill、不丢弃面积重新归一化。reflect 对 footprint 的每个整轴单元索引执行上式映射。
因此边界语义与点核一致。旧 area 保留原整数包围框 Pillow BOX 行为。
area 计算先累加未归一化的覆盖面积分子，再除以完整 footprint 面积，以避免分数权重提前归一化
引入半值量化偏差；极大覆盖量先缩放，防止 float64 乘法溢出。浮点精度使 footprint 边界重合时，
规划在 IO 前明确报错。独立小型 area 参考用有理数重叠权重累加后转换 float64，并验证半值量化。

## 格式与严格校验

新入口立即拒绝 bool 数字、NaN/Inf；MPP 正且有限，origin 可负可分数且有限。
尺寸、level、限额、worker/pending 数须为整数类型（含 numpy 整数）且拒绝 bool/分数/整数值浮点。
尺寸/限额/worker/pending 为正，level>=0 且在 metadata 层范围内。每个字段、选项、
fill 分量均校验，即使该选项不影响当前执行。派生 extent、中心索引和核范围的溢出也报错。
错误使用 ValueError，消息包含字段与原因；后端原始异常不重写为校验错误。
固定选项值：level_policy=nearest/finer，interpolation=nearest/bilinear/bicubic/area，
padding_mode=constant/reflect，color_mode=rgb/native，canvas_rounding=round/ceil。

RGB 只接受 uint8，二维灰度或 HWC 单通道复制三通道；RGBA 白底合成逐通道
`(rgb*alpha+255*(255-alpha)+127)//255` 后采样，RGB 三通道直接使用，其他通道数拒绝。
RGB fill 为 [0,255] 整数标量或恰好三分量整数元组。
native 只允许 nearest，支持 bool、uint8/16/32/64、int8/16/32/64，二维统一为 HWC 单通道；
浮点、复数、对象与连续核拒绝。native fill 为该 dtype 可表达的整数标量（bool dtype 接受整数 0/1），广播各通道。
dtype/通道来自所选层可靠 PixelFormat；两种 padding 下完全越界且格式未知时均明确报错，禁止为推断格式读取像素。
有 IO 时核查 region 的尺寸/dtype/通道与声明一致；所有返回数据须独立于 reader 缓冲区。

## 读取规划、画布与限额

第一版一个请求最多一次 read_region。先求核/footprint 所需整轴索引，经 clamp/reflect 映射后，
对实际原生像素取最小整数包围框。零权重邻点可省略；非零权重邻点不得省略。
读框位于层内，计数为实际 `read_width*read_height`，包含全部邻域与框内未使用像素；
不包含 backend 内部整 tile 解码、缓存或通道字节数。reflect 远距原点不直接进入包围框。
阶段 1 规划保留点核的零权重邻点，预算含这些像素；constant 用端点及标量二分查找，
reflect 最坏 O(width+height) 标量工作量、O(1) 辅助内存。area 的单个 footprint 反射范围用解析极值计算，
不逐个枚举巨大 footprint 内的原生像素；任何规划路径都不分配 HW 或轴向坐标数组。
area 遇到步长小于坐标 ULP 的精度风险时额外标量检查轴向 footprint，防止内部像素范围坍缩。
SamplingPlan.native_dimensions 保存整张所选原生层尺寸，不能以局部 read_size 作为反射周期。
level-0 起点使用 `ceil(nextafter(native_index*axis_downsample,-inf))`，并通过后端样本验证映射。

默认输出 16,777,216 像素（4096²），读取 67,108,864 像素（8192²），
每项单独设 None 显式禁用；0 不表示禁用。输出面积在分配 image/mask/坐标数组之前检查，
读取面积在 IO 之前检查，规划不做像素 IO、不分配 HW 数组。超过上限时报 ValueError，
包含 slide、origin、所选层、输出/实际读取尺寸及对应限额；不自动分块或降级。
完全越界 constant 输出仍受输出限额，读取计数为 0。

画布 `max(1,round(W0*mpp0/target))` 保留 Python ties-to-even 默认；ceil 用 `max(1,ceil(...))`。
阶段 1 将 canvas_rounding="round"/"ceil" 贯穿 SlideSpec、virtual_canvas_size、sampler 与旧读取配置，
从 SlideSpec 生成的请求必须携带该配置，旧手工请求默认 round。精确请求不依赖虚拟画布裁剪。
align/drop 含义保持；ceil 范围不保证所有末端中心有效。

## 流与资源

PhysicalPatchStream 产出完整 PhysicalPatchResult，同步入口是唯一采样实现；旧 PatchStream 保留旧像素路径。
num_workers=1 使用消费线程；并行使用固定 worker 循环，factory 创建、metadata/read/close
全在同一所属 worker 线程。借用 reader 仅支持串行，调用方关闭；factory 未交付即失败时由 factory 清理部分资源。
空流不调用 factory。缓存键包含 slide/source_mpp，不跨 worker 共享 reader。

max_pending 默认 2*num_workers，包含排队、执行、完成等待与当前 yielded 请求；
下一次消费前不补交。按输入顺序产出，后序错误可能等前序读取完成才可见。
提前 close 取消未开始任务，等待已开始的 native IO，不能强制中断第三方读取。

own_requests=False 默认借用 `iter(requests)`；True 时关闭实际迭代器的 close（若存在），
不同时关闭 iterable，也不对借用迭代器 close。正常结束、生成/factory/read 失败、提前 close 均清理。
原始读取/生成/factory 异常对象及 traceback 优先；清理错误作为该异常的
`cleanup_errors` 元组保存，Python>=3.11 再 add_note。成功执行后的首个清理错误传播，
其他清理错误附在其 cleanup_errors；仍尝试全部清理。提前 close 视为清理成功路径，
GeneratorExit 不吞掉清理失败。清理错误顺序固定：worker 编号、然后请求迭代器。
使用 `with contextlib.closing(iter(stream)) as results:` 支持提前退出；所有历史结果在后续读取/close 后有效。

## 兼容迁移

当前版本 0.1.0 的合法整数 PatchRequest、Patch(request,image)、默认 round、旧 helper/旧流像素行为保留。
旧 helper 最近邻左边缘索引、Pillow bilinear/BOX、旧矩形 mask 保留，快照见 tests/fixtures/phase0。
在 0.2.0（阶段 1）为旧分数/bool 的坐标、尺寸、fill 及 bool MPP 转换发出 FutureWarning，指出字段、
PhysicalPatchRequest 替代方式及 0.3.0 拒绝版本；0.3.0 开始拒绝。拒绝时间以版本为界，不设日历截止日期。
阶段 0 不提前改变运行行为。其他旧入口的相同隐式转换在阶段 1 审计，并遵循同一迁移表。
严格精确入口自首次发布即执行校验。不改变 viewer/IIIF 或 torch 默认数据项结构。
合法的分数 MPP 保持支持。整数值浮点整数参数也提示迁移，0.3.0 与分数整数参数一起拒绝。
旧 helper 的显式 level 已立即拒绝负数/bool/浮点/越界值，显式 level 不屏蔽非法 policy。

## 验收证据

见 [PHYSICAL_SAMPLING_ACCEPTANCE.md](PHYSICAL_SAMPLING_ACCEPTANCE.md) 与
[PHYSICAL_SAMPLING_SAMPLES.md](PHYSICAL_SAMPLING_SAMPLES.md)。tests/phase0_reference.py
仅依赖 numpy/标准库，以标量循环计算，不导入生产采样函数。oracle 自测通过代表基线可用，
不代表阶段 1–4 的功能验收通过。
