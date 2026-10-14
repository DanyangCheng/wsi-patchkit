# 阶段 1 实施记录

日期：2026-10-13；开发版本：0.2.0。依据阶段 0 契约 v1 与原始需求归档，未修改需求快照。

## 交付范围

* types.py：PhysicalPatchRequest、OutputGrid、SamplingLimits、SamplingPlan，严格数字/选项/fill 校验、
  有限派生范围检查、from_legacy 转换、仿射网格；既有 Patch 两参数构造保持。
* sampling_plan.py：选层与优先级、恒定输出物理网格、点核/area 邻域、整轴 clamp/reflect 后的单区域 IO 框、
  level-0 起点、默认/禁用限额与限额错误。规划不接受 reader，不做像素 IO，不分配 HW 数组。
* _canvas.py / geometry.py：共用 round/ceil 计算，显式策略从 SlideSpec 到 PatchRequest 再到 helper；
  所有生成新请求的 grid/random/tissue sampler 已透传，indexed 保留原请求，TissueFilter 保留原请求。
* 公共导出、README 示例、CHANGELOG、pyproject.toml/uv.lock 版本同步。

规划可表达 nearest/bilinear/bicubic/area 和 constant/reflect，像素实现仍按阶段 2–3 交付。
SamplingPlan 增加 native_mpp/source_extent_um/pixel_format，以便同步执行沿用规划决定，不另行推断几何。
提供 metadata 时 path/source_mpp 必须匹配，override 对所选层须满足 source_mpp*downsample。
native dtype/fill 与 RGB 格式在元数据可靠时提前检查；无 IO 的 native 请求必须有 PixelFormat。

## 兼容校验审计

| 入口 | 0.2.0 行为 | 后续 |
| --- | --- | --- |
| PatchRequest 的 x/y/width/height/fill | int 转换保留；bool、非整数类型输入提示字段、替代入口与 0.3.0 | 0.3.0 拒绝 |
| as_size（SlideSpec、LevelInfo、sampler、TissueMask 复用） | 序列的隐式整数转换和 bool 标量提示；合法整数不提示 | 0.3.0 拒绝被弃用输入 |
| as_mpp（请求、metadata、spec、reader 覆盖复用） | bool 提示；正且有限的分数 MPP 保持支持 | 0.3.0 拒绝 bool |
| 旧 helper 的显式 level/policy | 负 level/bool/浮点/越界立即拒绝，policy 总是校验，错误在 region IO 前 | 合法整数与默认选层保持 |
| 新物理请求、网格、限额 | 首次发布即严格，接受 numpy 整数，拒绝 bool/浮点整数/静默截断 | 无兼容放宽 |
| sampler 自己的 seed/num_samples 与 reader 原生 read_region 参数 | 没有新增物理采样转换；既有接口行为保留，原生后端仍负责自身校验 | 精确入口通过严格计划约束实际 IO；后端校准在阶段 5 |

从旧请求转换不能恢复此前被 int 截断的坐标。ceil 旧 helper 的 mask 仍是兼容矩形 mask，
精确半开物理 mask 属于阶段 2；不能把本阶段画布策略贯通当成精确读取验收。

## 验收覆盖

| 阶段 1 完成条件 | 自动证据 |
| --- | --- |
| A01 规划/类型 | test_physical_types：正负分数原点、非等轴 MPP、中心仿射、numpy 数字、旧请求显式转换 |
| A04 原生起点 | test_sampling_plan：1000/333 与 800/271 同时逐轴测试多个 native index；新计划在真实 TIFF 1000→333、800→267 上多个起点读回与写入数组逐像素比较 |
| A08 画布传播 | test_canvas_rounding：round/ceil、align/drop、大于画布的窗、随机/组织/indexed、旧流透传与 helper 覆盖 |
| A13 规划/校验 | test_physical_types/test_sampling_plan：NaN/Inf/bool/分数/整数值浮点、派生溢出、kernel 预算、限额等值、None、无分配哨兵 |
| A16 兼容基础 | 8 个 legacy_pixels.json 固定快照、Patch 两参数、旧构造与默认 round；提示版本测试 |

额外规划证据：对所有核和两种 padding 与独立 oracle 的随机小输入比较，计划包含全部非零权重邻点；
反射稀疏 stride 保持小读框，远距坐标先映射，巨大 area footprint 用解析反射极值。
计划保留零权重点核邻点，限额按实际保留的框计数。完全越界 constant 仍校验输出限额，读框为零。

同步图像/mask 的 A01/A04/A13 余项及其他 A 项按实施计划进入阶段 2–5。

## 执行结果

2026-10-13：新增阶段 1 测试 129 项，包含阶段 0 夹具与相关既有回归的整组
**283 passed in 2.18s**。运行命令：

```sh
.venv/bin/python -m pytest -q \
  tests/test_physical_types.py tests/test_sampling_plan.py tests/test_canvas_rounding.py \
  tests/test_phase0_reference.py tests/test_phase0_baseline.py \
  tests/test_types.py tests/test_sampling.py tests/test_image_reader.py \
  tests/test_tiff_reader.py tests/test_openslide_reader.py tests/test_stream.py \
  tests/test_auto_reader.py tests/test_tiles.py tests/test_torch_dataset.py
```

沙箱内运行到 PyTorch persistent-worker 用例时停滞；单独用 25 秒超时及 faulthandler 确认
消费线程停在 DataLoader 的 queue 等待。沙箱外该用例单独通过，整组也完成通过；
未为此修改 torch adapter。根本原因没有进一步归因到产品代码。
修改的 Python 文件 Ruff 检查通过，git diff --check 通过。
uv lock 只更新本项目版本至 0.2.0；uv 离线、无依赖重新解析的可编辑安装已更新 .venv，
公共导出及 README 示例用真实小 PNG smoke 验证通过。web 全量、vendor 校准和发布产物仍在后续阶段。
