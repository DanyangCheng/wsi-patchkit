# 阶段 3 实施记录

日期：2026-10-14；开发版本仍为 0.2.0，旧输入拒绝版本保持 0.3.0。

## 实现范围

* resampling.py：RGB 的 nearest/bilinear/bicubic/area 全部支持 constant/reflect；native 仍仅 nearest。
  Keys a=-0.5 每轴四邻点，二维可分离核，float64 累加，clip(floor(v+0.5),0,255)。
  使用相对于 floor(s) 的分数计算距离，避免巨大坐标减相邻整数时消去精度。
* reflect：在全原生轴选择各个整数邻点后周期映射，不重复端点，n=1 退化复制。
  大于安全整数浮点范围时使用 Python 整数 modulo，避免 int64 溢出或浮点周期丢失。
  映射后沿用规划的单区域读取；完全越界仍有反射图像，mask 全 false。
* area：按物理 footprint 和原生单元重叠面积；两个轴均不放大；边界按冻结契约执行 clamp/reflect。
  合并映射到同一原生像素的覆盖量；远距 constant 尾部与 reflect 完整周期用整数计数处理，
  不枚举巨大 footprint 的每个单元。分像素生成支持、分 64 行原生邻点累加，不构建输出×源轴权重矩阵。
* 数值与限额：覆盖面积分子先累加再归一化，保留半值量化；极大覆盖量先缩放，避免乘积溢出。
  已有输出/实际读框像素限额统一覆盖全部模式；关闭像素限额仍检查平台可寻址 shape。
  有限请求引出的非有限 footprint 宽度或浮点精度坍缩在规划阶段报错，不进行 IO。
* SamplingPlan 新增 native_dimensions，反射始终按整原生层计算，不按 read_size。
  所有完全越界 native 请求均要求 PixelFormat，包括 reflect；已知格式的 dtype/通道/fill 预检仍执行，
  即使 reflect 不使用 fill 也验证其范围。未知格式的范围内 native 读取在 IO 后验证实际返回数组。

未变更旧 helper 的 Pillow BOX/filter 行为、旧流/torch 默认结构、vendor reader 原生接口或请求/结果公共名称。
read_physical_patch_result 已支持上述全部模式；完整物理结果流按阶段 4 实施。

## 数值参考基线维护

阶段 0 area 参考原先将权重先归一化再逐项 float64 累加；对 1/3、1/6 等权重的精确半值，
这会把数学上的半值扰动到相邻浮点数，量化差一档。使用独立 Fraction 重叠面积运算核对后，
area oracle 改为有理数累加后转 float64；生产仍使用 float64 未归一化面积分子。
200 组整数/分数 origin 与 2×2、3×2、3×3、4×3、5×3 footprint 的 constant/reflect 栅格比较，
验证半值按规定量化。nearest/bilinear/bicubic 的原参考、旧 API 像素快照与需求归档没有改变。
该修订解决参考计算的数值误差，不变更面积加权或边界契约。

## 验收覆盖

| 编号 | 证据 |
| --- | --- |
| A03 | 独立梯度/脉冲参考，Keys 固定 [4,15,25]、脉冲143、饱和[0,128,255]；area 分数 footprint 与半值栅格 |
| A05 | 所有支持模式的 constant 填充，有效中心 footprint 的 clamp，不混入 fill；无效中心整填与零 IO 保留 |
| A06 | 所有核 reflect、n=1/单像素轴、远距±1e200 点核、跨局部读框、巨大面积周期，范围外 mask 全 false |
| A13 | 全部 4 核×2 padding 的输出/读框超限哨兵、等值上限、独立 None、地址范围和 footprint 坍缩预检 |
| A14 | 各 RGB 核的灰度/RGBA 白底；native uint64 保值 reflect、浮点/复数/对象拒绝、fill 范围及完全越界未知 PixelFormat 拒绝 |
| A02/A15/A16 回归 | 相邻请求物理相位一致、扩展结果 close 后所有权、全部旧像素快照、既有读取与流/torch 测试 |

真实 PNG 与非整数 TIFF 金字塔已扩展到四种 RGB 核；OpenSlide/KFB 多层部署校准仍留在阶段 5。
测试 expected 使用独立 oracle 或固定手算值，不由生产采样函数生成。

## 验证

```sh
.venv/bin/python -m pytest -q \
  tests/test_physical_sampling_modes.py tests/test_physical_reading.py \
  tests/test_physical_types.py tests/test_sampling_plan.py tests/test_canvas_rounding.py \
  tests/test_phase0_reference.py tests/test_phase0_baseline.py \
  tests/test_types.py tests/test_sampling.py tests/test_image_reader.py \
  tests/test_tiff_reader.py tests/test_openslide_reader.py tests/test_stream.py \
  tests/test_auto_reader.py tests/test_tiles.py tests/test_torch_dataset.py
```

2026-10-14 执行：整组 **530 passed in 3.93s**。相对阶段 2 新增 129 项覆盖，
其中新扩展模式测试模块 112 项，PNG/TIFF/颜色模式参数扩展 16 项，独立 area 半值自测 1 项。
为避免已知的沙箱内 PyTorch persistent-worker 停滞，整组在沙箱外用 45 秒 timeout 运行并完整结束。
修改和新增 Python 文件 Ruff 检查、格式检查与 git diff --check 通过。
另用实际 PNG 对四种核的完全越界 reflect 做 smoke，固定 HWC=(2,3,3)、mask 全 false，读框均在原生轴内。
本轮未进行 web 全量、真实 OpenSlide/KFB 多层校准或发布产物验证，继续按阶段 5 执行。
