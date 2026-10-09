# Center：多节点保守位置 CI 估计（实验性，默认关闭）

> 本实现堆叠在 Center V2 `feat/tracklet-association-v2`，主要解决在不同 Edge 定位结果互相关未知时，不能通过简单平均或普通卡尔曼信息相加虚假缩小协方差的问题。**本功能不是经过实机标定的全局三维联合定位系统。**

## 1. 实际完成的能力

新增 `sightmesh_center/fusion_ci.py`，在身份关联分组完成后**只对显式配准且至少 2 个不同物理节点**的有效位置估计运行多源融合：

- 检查每个节点的 class label、经验证的 `alignment_id`、`coordinate_frame_id`、`map_revision`、共同 `shared_sim/shared_utc` 时间域、源位置协方差对称正定与时钟误差；缺任何信息直接拒绝。
- 用事件时间 `event_time_ns + offset_ns` 对齐到组内中间时间，不把中心接包时间当传感器时间。已有速度观测仅用于短时线性外推；没有速度协方差，额外使用运动速度上界与时钟不确定度作为**启发式**方差膨胀。
- 对未知交叉相关的 3×3 位置协方差使用顺序 Covariance Intersection（CI）；每对权重在 `[0,1]` 内以信息矩阵 log-det 目标确定，并记录输入权重；不把同一目标的多节点输入假装相互独立。
- 对 Edge 原 Bearing 的相机位置、方向与切平面协方差，按时间戳窗口计算相对于 CI 位置的法向残差。有效的反向观测/大残差**否决**候选；无有效 Bearing 时输出 `bearing_factor_count=0`，不把该约束当精度提升。
- 输出并保存 `fusion_estimate_status`、`fusion_estimate`（位置、3×3 协方差、输入节点、权重、共同时间域/地图/原点）及 `fusion_diagnostics`（马氏残差、方位残差、拒绝原因）。

## 2. 数学与保守性边界

对于两组均有效的输入位置 `(x1,P1)` 和 `(x2,P2)`：

```text
P_CI^-1 = w * P1^-1 + (1-w) * P2^-1
x_CI    = P_CI * (w * P1^-1 * x1 + (1-w) * P2^-1 * x2)
w       = argmin(w in [0,1]) log(det(P_CI))
```

对 3～4 个来源按照完整 Tracklet UID 的确定性顺序逐次执行 CI。**CI 的一致性前提是原始协方差自身可信、均代表相同随机变量/时刻**。本版对异步外推增加的运动/时钟方差只是显式可调的工程保守近似，不是可靠的统计覆盖保证；必须以 GNSS/仿真真值进行 NEES、NIS、可靠区间覆盖率标定，不能根据输出 P 的变小推断实际定位精度已经提升。

方位因子目前只用于残差验证，**不作为独立信息重复加入 CI**，因为 Edge 的 UKF/GTSAM/PTCEE 很可能已经使用同一 Bearing；重复融合会造成不可解释的双重计数。不存在合适位置观测的纯 Bearing-only 目标，本版仍返回 `unlocalized`，后续需单独实现经可观性检验的滑窗方位因子估计。

## 3. 数据持久化和身份修订

CI 作为**实验性 sidecar** 写入已有 `GlobalTrack` JSON（同 `world_revision` 的 SQLite 事务），不覆盖 `representative`，也不替换当前 Render 的位置。现有对外主状态继续用 `fusion: NOT_FUSED`，忠实代表 Render 看到的仍是 Edge 单源原始位置。这样新模块默认关闭时，原 HTTP/NNG WorldState/IdentityRevision 都无需修改。

每次新 TrackEvent、迟到事件、身份拆分/合并、融合参数变化，均在 `GlobalTrackRepository.recompute` 中重新从当前 Tracklet 成员计算 CI 侧结果；`fusion_policy` 纳入世界签名。单节点或新成员不满足验证条件时，新的 sidecar 记录 `rejected` 和原因，**不会继承之前已拆分身份的估计**。身份 revision 表继续以原 append-only 历史为权威。

## 4. 显式开启，仅用于评估

在 `config/center.json` 配置 `fusion` 参数，实测/仿真验证完共同 clock/map/origin/class 标签以后，手动执行：

```bash
python3 -m sightmesh_center --config config/center.json associate \
  --solver clique --fusion ci --db data/track-inbox.sqlite3
```

持续实验可另加 `--watch --interval 2`；不加 `--fusion ci` 默认 `off`。直接用 `global-state` 读取 JSON 中 `fusion_estimate` / `fusion_diagnostics`，不要据此自动打开 Render 的生产融合显示。`CI` 数学仅用 Python 标准库，不给 D2000 主程序强制新增 numpy/scipy/OR-Tools。

## 5. 最终集中测试（目前未运行）

- `tests/test_fusion_ci.py`：强相关/完全相同输入不能按独立样本虚假缩小方差，互补各向异性协方差、Bearer/角度观测矛盾、无效/迟到时间，身份合并拆分后估计清理。
- 真实/仿真不同帧率（5/10/20/30 FPS），时钟 offset/uncertainty、UKF/GTSAM 协方差标定、NEES/NIS、实际定位 3D RMSE 和不同机动速度下的误差覆盖率。
- 重复运行确定性、不同 node 顺序、极端病态 covariance、协方差被网络序列化截断、关联跨地图修订、断电后 WorldRevision 与 sidecar 一致性。
- D2000 ARM64 Python 标准库运行开销、全历史重建 O(N²) 上限、SQLite WAL 写入、Render HTTP/Wire 兼容性。

验收时应分别报告**关联准确率**、**融合位置误差**、**误差统计覆盖率**和**延迟**，不能只以几帧视觉轨迹更平滑判断改进成功。