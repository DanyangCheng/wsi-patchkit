# 阶段 2 实施记录

日期：2026-10-14；开发版本仍为 0.2.0，兼容拒绝版本保持 0.3.0。
依据原始需求快照与阶段 0 契约，实施范围为 nearest/bilinear + constant 的精确同步读取。

## 交付

* reading.py：公共同步入口 read_physical_patch_result，获取或使用 metadata，经阶段 1 规划执行
  最多一次已有 read_region；可传入 SamplingPlan，其与当前 metadata/request/limits 必须一致。
  不匹配、超限、非法层级在 pixel IO/输出分配前失败；输出形状超过平台可寻址范围时明确报错。
* resampling.py：全局物理中心决定相位；nearest 用 floor(s+0.5)，bilinear 用四邻点可分离权重，
  float64 累加，clip(floor(v+0.5),0,255)。64 行 strip 限制临时插值数组，strip 不改变坐标相位。
  无效中心整像素 constant 填充；有效中心邻点 clamp，HW bool 掩码由物理半开范围单独判断。
* types.py：PhysicalPatchResult 的 request/image/valid_mask/grid/source_level/plan；数组独立持有数据，
  可修改，在后续读取、普通迭代器结束和 reader.close 后保持有效。
* 图像格式：复用旧灰度/RGBA 白底合成规则，精确入口仅接受 uint8 灰度/RGB/RGBA 转 RGB；
  native 只执行 nearest，保留 bool/整数 dtype 与通道，二维转 HWC 单通道，不经 float 量化。
  元数据可靠时预检格式，否则在有界读取后验证。完全越界 native 必须有可靠 PixelFormat，不能探测像素 IO。
* 公共导出、README 可运行同步示例、契约与验收状态同步更新。

完全越界 constant 返回固定尺寸填充与全 false mask，不调用 read_region；metadata 查询仍允许。
新入口对 bicubic/area/reflect 在 metadata/pixel IO 前明确报错，不降级为其他模式。
同步调用不拥有 reader、不关闭 reader，也不替换其原始异常。
旧 helper 的缩放/滤波与矩形 mask、旧流/torch 默认结构继续保留；未改写 vendor reader。

## 阶段验收证据

测试入口：tests/test_physical_reading.py；预期来自 tests/phase0_reference.py 的独立标量实现及手算固定样例。

| 验收编号 | 已验证的阶段 2 子集 |
| --- | --- |
| A01 | 正负分数 origin、非等轴 source/target MPP、输出尺寸与 affine 不变；独立参考随机样例 |
| A02 | 原始差异样例、同层相邻请求相同物理中心一致、读取框改变不改变相位；多个 strip 的连续网格 |
| A03 | nearest 半整数、bilinear 梯度/脉冲、float64 权重、uint8 半值量化；扩展核按阶段 3 |
| A05 | 标量/RGB fill、四边/四角/完全越界、远距正负坐标，无效中心整填，完全越界零 pixel IO |
| A07 | 四条物理边界与 ±1e-9，右/下恰好边界无效，左/上零有效；宽=3/MPP=2 红=[10,255]，mask=[true,false] |
| A14 | RGB、二维/HWC 灰度、RGBA 先白底再插值，uint16 RGB 拒绝；native uint16/uint64/int64/bool 保值，未知格式及返回 shape/dtype 不一致明确报错 |
| A15 | 后端返回共享 view、重复读、close 覆写源、普通生成器关闭后，已返回 image/mask 仍独立有效；uint64 超过 float64 精度的 label 未改变 |
| A04/A13 额外同步回归 | 真实 TIFF 1000→333、800→267 多起点与边缘；显式 level/policy 传到 IO；自定义限额、非法 level、地址范围溢出与不一致 plan 在 IO/分配前失败 |
| A16 兼容 | 阶段 0 的 8 个旧像素快照保留，继续运行已有类型/采样/读取/流/torch 回归 |

PNG 与 TIFF 使用可重建小夹具，量化后图像与 mask 逐元素相等。测试不调用生产采样函数生成 expected。
OpenSlide/KFB 多层真实相位校准仍属于阶段 5；流产出的完整结果与生命周期属于阶段 4。

## 验证命令

```sh
.venv/bin/python -m pytest -q tests/test_physical_reading.py
.venv/bin/python -m pytest -q \
  tests/test_physical_reading.py \
  tests/test_physical_types.py tests/test_sampling_plan.py tests/test_canvas_rounding.py \
  tests/test_phase0_reference.py tests/test_phase0_baseline.py \
  tests/test_types.py tests/test_sampling.py tests/test_image_reader.py \
  tests/test_tiff_reader.py tests/test_openslide_reader.py tests/test_stream.py \
  tests/test_auto_reader.py tests/test_tiles.py tests/test_torch_dataset.py
```

2026-10-14 执行：阶段 2 新增 118 项测试，相关整组 **401 passed in 2.80s**。
为避免已知的沙箱内 PyTorch persistent-worker 停滞，整组在沙箱外用 45 秒 timeout 运行，完整结束。
修改与新增 Python 文件 Ruff 检查、格式检查及 git diff --check 通过。
README 同步示例在实际小 RGBA PNG 上 smoke 通过，返回 HWC=(128,256,3)、HW=(128,256)，
关闭 reader 后 image/mask 仍独立持有数据。本轮不包含 web 全量、vendor 多层校准或分发产物验收。
