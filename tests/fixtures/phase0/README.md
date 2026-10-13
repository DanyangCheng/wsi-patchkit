# 阶段 0 固定输出

legacy_pixels.json 保存 wsi-patchkit 0.1.0 的旧 read_aligned_patch_result 输出，
使用 tests/phase0_fixtures.py 的 LEGACY_CASES/ArrayReader，Pillow 12.3.0。
image、mask、dtype 和 read_calls 均固定；测试不得自动刷新快照。
独立精确参考在 tests/phase0_reference.py，核/量化自测的 expected 来自公式与手算。

需求快照和数值/边界/迁移规则见 docs/PHYSICAL_SAMPLING_CONTRACT.md；
全量验收输入与层次见 docs/PHYSICAL_SAMPLING_ACCEPTANCE.md。
