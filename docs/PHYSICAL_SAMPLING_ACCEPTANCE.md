# 精确采样验收追踪表（阶段 0）

需求：[归档原文](WSI_PATCHKIT_REQUIREMENTS.md)，决策：[契约 v1](PHYSICAL_SAMPLING_CONTRACT.md)。
以下保持原始 A 编号；“阶段”表示生产实现何时验收，不表示当前已经通过。
层次：O=独立 oracle 自测；P=纯规划；S=同步模拟 reader；F=流生命周期；B=真实后端；C=旧 API 回归。
所有 uint8/native/mask 使用完全相等；几何与 float64 参考用 rtol=0、atol=1e-12。
生产测试应导入 tests/phase0_reference.py 计算预期；该模块不能导入生产采样代码。

| 验收 / 需求 | 固定输入与必要变体 | 预期与断言 | 层次 / 实现阶段 / 当前证据 |
| --- | --- | --- | --- |
| A01 / R01,R02,R10 | 梯度 8×4，origin=(1.25,0.25)、(-0.25,-1.5)，target=(0.5,1.5)，size=(3,2)；source_mpp=(0.25,0.5) | 中心逐点与公式相等；首个正中心=(1.5,1)；原点不截断，HWC=(2,3,3)，负原点保留；仿射作用于整数 (col,row) 得中心 | O/P/S；1–2；test_non_square_grid_and_adjacent_phase 已锁定正分数网格，负数规划待 1 |
| A02 / R02 | 原文六个差异样例；origin=(1.25,0.25) 和 (2.25,0.25)，相同步长 (0.5,1.5)、size=(3,2)；nearest/bilinear/bicubic | 相交物理中心图像一致；整数下采样红=[10,40]，非整数双线性=[18,33,48]，分数 origin 红=[5,15,25]；扩大 IO 框不改相位 | O/S/B；2–3、5；test_requirement_examples 与相邻中心 oracle 已通过 |
| A03 / R03 | X/Y 梯度、7×7 中心 255 脉冲、0/255 阶跃；s=-1.5/-0.5/0.5/1.5；s=3.5 脉冲；area footprint=[0.5,2.5) | nearest ties 向较大整数；Keys(0.5)=0.5625、Keys(1.5)=-0.0625；脉冲 raw=143.4375；阶跃 raw=[-15.9375,127.5,270.9375]→[0,128,255]；area 权重=[.25,.5,.25]，放大拒绝 | O/S；2–3；test_nearest_ties/test_keys_impulse_and_saturation/test_area_phase_boundary_and_reflection |
| A04 / R02 | downsample=(1000/333,800/271)，native index=0,1,2,17,29,270；非等轴 MPP；真实 TIFF 1000→333 与 800→267 | level-0 整数起点经 floor(location/downsample) 恢复目标原生索引；选层/输出 grid 正确；多个位置 2×2 region 与写入数组相等 | P/S/B；1、5；test_native_start_reference_round_trip 和真实 TIFF 基线；800/271 与混合轴 backend 待校准 |
| A05 / R04,R05 | origin=(-1,0)/(7,0)/(0,-1)/(0,3) 及四角；fill=255 或 (1,2,3)；完全越界 (-100,-100)；size=(2,2) | 固定大小；无效中心整像素填充；有效中心边缘 clamp；完全越界 read_calls=0（metadata 可读取）；area 有效中心跨边界不混入 fill | O/S；2–3；四边/四角/完全越界 oracle 已通过；IO 跳过待 S |
| A06 / R04 | 原生轴 n=4、整数索引 -12…15，与 np.pad 比较；n=1；origin=(-100.5,-999)，size=(1,1)，bilinear；各点核和 area | 整轴反射且不重复端点，远距红=15，单像素轴复制；全 false mask；计划根据映射索引而非远距原点预算 | O/P/S；3；test_reflection_matches_numpy_and_far_single_axis，area 反射边界已锁定 |
| A07 / R05 | 宽=3，origin=(0,0)，target=(2,1)，size=(2,1)，nearest；左右/上下各界中心及 ±1e-9；constant/reflect/round/ceil | 红=[10,255]，mask=[true,false]；中心等于右/下边界无效；左/上等于零有效；不因核或策略变化 | O/S/B；2–3；test_half_open_mask_and_full_constant；四界精确相等生产回归待 2 |
| A08 / R07 | 物理宽=5、高=3，target=(2,2)，round=(2,2)，ceil=(3,2)；stride=(2,2)，patch=(2,2) 与 patch=(8,8)；align/drop | ties-to-even round，ceil；SlideSpec→sampler→request→helper 一致；最后 ceil 请求不被 round 裁剪；drop 无越界窗，align 保持原定义；精确 mask 独立 | P/S/C；1–2；执行清单已冻结，生产实现待 1 |
| A09 / R06 | 3 个请求分属两个 slide；level=0/1 与 policy=finer；阻塞首个任务令后续先完成；所有支持核/padding | 直接、串行、并行 image/mask/grid/source_level 完全一致，顺序保持输入；显式 level 胜 policy，非法 policy 仍报错；不同 source_mpp 缓存隔离 | S/F；4；待完整流实现 |
| A10 / R06,R09 | num_workers=4；max_pending=1、2、5；可计数请求迭代器+Event 阻塞读取；消费者停在 yield；提前 close | 产出 k 项并停在 yield 时已消费≤k-1+max_pending；下一次消费前不替换当前 yielded 容量；未开始任务取消、开始任务完成后 close 返回 | F；4；使用 Event/Barrier 确定同步，避免 sleep 时间推测 |
| A11 / R09 | 正常/空流/提前 close；metadata/open、factory、read、请求生成失败；每种与 reader.close/iterator.close 失败交叉；own_requests True/False | 自有 reader 全释放，借用不关闭；空流 factory=0；原异常对象优先，全部 cleanup_errors 可查；无原异常时首个清理失败传播；实际迭代器只关闭一次 | F；4；factory 未交付资源由 factory 清理，测试包含部分构造失败 |
| A12 / R09 | reader 在 create/metadata/read/close 记录 thread ID；workers=1、4；每 worker 多 slide；正常、异常、提前关闭 | 每个自有 reader 所有操作同线程，parallel 所属 worker，串行消费线程；每个 reader close 一次 | F；4；待固定 worker 循环实现 |
| A13 / R08,R10 | 输出/read 面积 limit-1、limit、limit+1；默认/None；bicubic 邻域，远距 reflect；NaN/Inf/bool、fraction/int-valued float 尺寸/level/limits，负 level、非法 policy，fill=(-1,2,3)；有限输入派生溢出 | 等于限额允许，超过拒绝；任何大数组分配/IO 前报错；plan 无像素 IO/HW 数组；消息字段/请求/选层/尺寸/上限；禁用单项独立生效 | P/S/F；1、3–4；待严格 API，禁止构造真实巨型输出来测试 |
| A14 / R03,R04,R10 | RGB、2D/HWC 灰度、RGBA ([100,50,0,128])、uint16 灰度；非法双通道、float RGB；native 完全越界有/无 PixelFormat | 灰度复制；RGBA→[177,152,127] 后采样；uint16 RGB 报错；native uint16 nearest 保值、非 nearest 拒绝；未知完全越界 native 无像素 IO 且报错 | S/B/C；2–3、5；旧 native uint16 快照已保存，新增格式验证待 2 |
| A15 / R09,R10 | 复用缓冲区 reader，读下一项覆盖 buffer；结束流及 reader.close 后再访问结果；修改返回 image | 之前 image/mask 保持不变且独立持有数据；修改结果不改变后续读取或其他结果；直接/串行/并行均验证 | S/F/B；2、4–5；待生产结果类型 |
| A16 / R10 | Patch(request,image)、旧整数调用、round 默认、8 个 legacy 固定像素快照；旧分数/bool 构造；torch 默认 item | 旧整数 image/mask/dtype/IO次数逐项一致；0.3.0 拒绝旧分数/bool/浮点整数参数及 bool MPP，ValueError 含字段；精确 API 首次发布即严格 | C；1–5；test_legacy_pixel_snapshot 当前 8 例；test_legacy_deprecated_inputs_are_rejected、test_shared_size_rejects_deprecated_inputs、test_shared_mpp_rejects_bool 覆盖 0.3.0 拒绝行为 |

## 基线运行与维护

```sh
.venv/bin/python -m pytest -q tests/test_phase0_reference.py tests/test_phase0_baseline.py
.venv/bin/python -m pytest -q tests/test_types.py tests/test_sampling.py tests/test_image_reader.py tests/test_tiff_reader.py tests/test_openslide_reader.py tests/test_stream.py
```

legacy_pixels.json 是 0.1.0/Pillow 12.3.0 在 2026-10-13 的一次实际输出捕获；测试只读取，
不在运行中再生成 expected。修订必须说明像素兼容影响并审查具体差异。
oracle expected 来自解析公式与手算固定值，不来自生产函数；后续生产测试可复用 oracle。
PNG/TIFF 文件在 tmp_path 生成，不依赖大文件；vendor 实例按样本清单单独验收。
阶段 0 的完成证据是全部 R 决策与全部 A 的输入/预期/层次落定，不能将此表写成 A01–A16 已全部通过。

2026-10-13 执行记录：上述两组测试合并运行，139 passed in 0.52s
（阶段 0 新增 65 项、相关既有回归 74 项）；新增四个 Python 文件的 Ruff 检查通过。
本轮未运行全量 web/torch 测试，也未执行 vendor 精确采样校准；这些保留在阶段 5。

## 阶段 1 更新

A01/A04/A08/A13 的规划与类型子集、A16 兼容基础已实现并验证，详细输入/证据见
[阶段 1 实施记录](PHASE1_IMPLEMENTATION.md)。新测试为 test_physical_types.py、
test_sampling_plan.py、test_canvas_rounding.py；上表其余像素/流/后端条件继续按所属阶段执行。
不能将规划通过等同于图像/mask/完整结果流已经可用。
本轮相关整组回归：283 passed（含新阶段 1 测试 129 项），Ruff 检查通过；
PyTorch 多进程用例使用沙箱外环境完成，详见实施记录的运行限制。

## 阶段 2 更新

A01/A02/A03/A05/A07/A14/A15 对 nearest/bilinear + constant 的同步子集已完成，
见 [阶段 2 实施记录](PHASE2_IMPLEMENTATION.md)。tests/test_physical_reading.py 使用独立 scalar oracle、
固定原文样例、PNG/非整数 TIFF 验证图像、mask、网格、源层及输出所有权。
扩展插值/reflect 按阶段 3，流与 vendor 完整验收按阶段 4–5；原始 A 编号与输入/预期保持。
本轮相关整组：401 passed（新增阶段 2 测试 118 项），Ruff/格式/差异检查通过；
运行环境与限制见阶段 2 实施记录。

## 阶段 3 更新

A03/A05/A06/A13/A14 的同步模式已补齐，见 [阶段 3 实施记录](PHASE3_IMPLEMENTATION.md)。
tests/test_physical_sampling_modes.py 覆盖四种核×两种填充、单像素轴、远距反射、area footprint、
所有限额组合与 native 约束；PNG/TIFF 的已有同步回归扩展到四种核。
area 半值参考使用独立有理数运算修复浮点累加误差，具体证据见实施记录。
完整流验收及 vendor 多层校准按阶段 4–5，不将同步模拟/可重建样本通过等同于全部 backend 验收。
本轮相关整组：530 passed，新增 129 项覆盖；Ruff/格式/差异检查通过，运行环境见阶段 3 实施记录。
