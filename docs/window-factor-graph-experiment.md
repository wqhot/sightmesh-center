# Center 可观测滑窗因子优化（实验性，默认关闭）

> 叠加在 Center [实验性 CI PR #7](https://github.com/wqhot/sightmesh-center/pull/7) 上。此分支只为验证窗口模型/可观测性/残差诊断；不切换 Render 输出，不宣称完成了 GTSAM 多传感器因子图生产后端。当前不执行任何测试或编译。

## 范围与算法

新增 `sightmesh_center/window_factor_graph.py`。复用 **已持久提交**的 Edge TrackEvent → Tracklet → 当前跨节点全局身份组，只对每个组选择一个固定时间窗。受限历史由现有 TrackletRepository 负责，所有计算均从观测重建，迟到帧/身份修订会触发重新求解，不复用上一组的估计。

支持两种独立、可对照的观测模型：

- `static_position`：只有 3 个未知量 `(x,y,z)`；必须将 `stationary_target_verified=true`，否则拒绝。
- `constant_velocity`：6 个未知量，参考时刻位置 `(x,y,z)` 和速度 `(vx,vy,vz)`，恒速预测 `p(t)=p0+v(t-t0)`。要求足够时间跨度、雅可比满秩和合理速度，**没有用人为零速度先验来凑满秩**。

真实 Bearing JSON 取自 Edge 的 `spatial.bearing`：相机 ENU 位置 `camera_position_world_m`、单位方向 `direction_world`、两行 2×3 的 `tangent_basis_world`、正定 `tangent_covariance_rad2` 和 `timestamp_ns`。角度残差为观测切平面上的两个分量，按 2×2 协方差 Cholesky 白化。唯一位置因子使用完整 3×3 covariance 白化。初值由线性射线约束的最小二乘求得，后端调用 **SciPy `least_squares`**，而不是手写通用 Levenberg–Marquardt。

这只是为 GTSAM/PTCEE 扩展验证算法与数据约束的开发 baseline；目前不是增量 iSAM2、滑窗边缘化、动态 IMU 因子图，也不在 Edge/Center 之间传优化状态。

## 为什么默认严格拒绝

Edge 的 UKF/GTSAM/PTCEE 可能已经融合本机 Bearing。因而同一物理节点的 **position 和 Bearing 不能同时当作两条独立因子**，否则重复计数。当前仅选择**全局一个**有效位置 anchor，跳过该节点所有 Bearing；其他节点使用 Bearing-only 约束。没有 anchor 时，要求至少两个不同节点的方位射线与足够交角。

跨节点共享地图外参、标定、时钟、地图定位误差仍会产生相关性；这不是靠把观测写成独立残差就能消除的。必须由实验记录/部署工程确认：

- `independent_source_errors_verified=true`：跨节点所选因子的误差建模已由人工/实验确认适用（默认 false）；
- `bearing_world_pose_frame_verified=true`：Bearing 相机位置/方向已按同一 ENU/WGS84 和地图版本统一，不能仅因字段同名就认为已对齐（默认 false）；
- `independent_temporal_errors_verified=true`：只有在同一节点连续帧误差相关性已经纳入实验论证时才使用该节点多个 Bearing 样本（默认 false；否则每节点最多一个最新样本）；
- `stationary_target_verified=true`：启用静态目标模型时的额外人工验证（默认 false）。

另有来源 map/frame/clock-domain/语义类别一致性，最大时间偏差、射线时间戳差、相机正向、最小交角、窗口最大点数、线性初始化秩、优化雅可比最小奇异值/条件数、归一化残差和最大物理速度等门限。缺少的事实不作虚构；例如 Bearing-only 目标在现有 **需要 3D 位置的关联候选机制**中尚不能自动形成 GlobalTrack 组，当前优化器只接受**已经关联**的跨节点轨迹组。以后需要独立的 Bearing-only candidate/association 前端。

## 估计值的统计意义

输出 `window_estimate` 的位置与（常速度模式下）速度，同时提供一个局部雅可比信息矩阵逆 `conditional_information_covariance`。该矩阵**没有**边缘化相机姿态、地图、时钟与跨源共享误差，也未经 NEES/NIS/统计覆盖率标定；仅可作为优化器的条件曲率诊断，**不能替换正式的 GlobalTrack covariance、不能作为置信区间发布**。窗口静态模型同样不能用于高速运动目标。

每个 GlobalTrack 旁路字段包括 `window_estimate_status`（`experimental_conditional` 或 `rejected`）、`window_rejection_reason`、`window_diagnostics`（样本、秩、条件数、残差、来源、时间跨度）和 `window_estimate`。原 `representative`、`fusion=NOT_FUSED`、CI `fusion_estimate` 完全保留，各自独立。所有字段随身份组和世界 revision 同一 SQLite 事务持久化。对外 Protobuf WorldState/Render 仍使用**原始代表位置**，不会暗中切换。

## 启动方式（留待集中验收）

此模式需要额外 Python 依赖 **NumPy + SciPy**；未安装时返回 `optional_numpy_scipy_unavailable`，其余 Center 地图/事件接收/CI 完全不依赖它们。仅在验证完 `config/center.json` 中 `association.sources` 和 `window_factor_graph` 相关门限后使用：

```bash
python3 -m sightmesh_center --config config/center.json associate \
  --db data/track-inbox.sqlite3 \
  --solver clique --fusion ci --window-factors
```

可以不加 `--fusion ci`，单独评估窗口优化。`--watch --interval 2` 重新计算快照而非启动另一个系统服务，默认不启用。运行结果在 `global-state` JSON 的 `window_estimate` 和 `window_diagnostics` 中，默认 `--window-factors` 关闭。

## 最终验收用例（此时不执行）

`tests/test_window_factor_graph.py` 覆盖双摄静态三角几何、近乎平行射线、同一时刻速度不可观、4 节点有时间基线的动态目标、同源位置/Bearing 禁止重复计数、同节点多帧默认仅取一帧、过期 Bearing、跨源独立性默认拒绝。

后续联合评估应在同一 sightmesh-sim 回放包上按身份关联准确率/轨迹 RMSE 与 P95/首次收敛延迟、Jacobian condition、跨节点时钟偏差、目标运动速度、物理视线夹角、NeES/NIS 和统计覆盖率报告。并与 V1 单源 representative、V2 CI sidecar、当前滑窗因子解作严格对照。只有通过这些指标和板端资源评估后，才考虑建立真正的 GTSAM 增量图优化后端与经过校准的可发布位置。

参考部署约束：D2000 ARM64 Python/SciPy 依赖为**可选**，缺包不得影响主 Center 路径；现有 Rockchip Edge 和 OGRE Render 不新增依赖。