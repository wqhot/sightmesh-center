# sightmesh-center 实施规划（D2000 ARM64 / x86）

> 更新：2026-10-09。此文件只规划 Center 的建设；跨项目 schema、Topic、时钟、坐标、NNG ACK 语义统一参考 [sightmesh 的 Wire/Transport 契约](https://github.com/wqhot/sightmesh/blob/master/docs/TRANSPORT_WIRE_PROTOCOL.md)。**当前仓库主要实现地图导入、Cesium 资源与 C++/Python 地图查询服务；V1 全局关联、310P ReID、Open3D/OctoMap/OR-Tools 并非已交付。**

## 当前代码基线与架构边界

- `cpp/src/importer.cpp`、`cpp/src/mesh.cpp`、`cpp/src/server.cpp`：WGS84 锚点导入、版本化 Mesh/BVH、`surface/raycast/visibility`、地图 API 与 Cesium 资源；现有使用中的真实能力须保留。
- `cpp/src/geodesy.cpp` 可选择 `SIGHTMESH_USE_GEOGRAPHICLIB=ON`，默认 OFF。只替换导入器的地理锚点计算；Python 地图处理还未统一，不能宣称地图全栈 GeographicLib 化。
- `docs/map-api.md` 已将 `occupancy`、`esdf` 等定义为 **unknown / 无实现**；不能为了路线图直接改变 API 含义。历史地图 revision、几何哈希、source 校验和 last-known-good 快照仍是兼容边界。
- 目标中心硬件为 Phytium D2000 ARM64，外观推理目标 Ascend 310P；x86 是开发/分析平台，**不能把 x86-only solver 或 Python 重依赖强制带进 D2000 runtime**。

## C1：可靠事件接入与 Wire 兼容（首要）

1. 新增 Center Inbox / `TrackletRepository` 的明确持久存储边界。消费 Edge 现有 `TrackEvent + Blob`，并在 Wire v1 发布后加 NNG adapter。
2. 源身份至少 `node_id + camera_id + session_id + local_id`；持久事件去重键 `node_id + session_id + stream_sequence`，记录 event_time/record_time/clock_domain/map_revision。
3. ACK **只能在事件本体与必要关联状态完成持久提交后**发出；网络 REQ/REP 返回不代表事务已经完成。缺失 blob、乱序事件、重试、网络重启与迟到修订必须可恢复且幂等。
4. 迁移期保持 HTTP Push + 旧 JSON/Event Inbox 兼容；若 Center 还未能够识别 Protobuf v1，Edge 不能自动改用 v1。
5. 运行时日志、状态接口清楚区分“接收/排队/持久提交/关联完成”；不能把 HTTP 200 当作已建立 global_id。

## C2：Global Association V1（物理模型自研 + 求解器可替换）

- 输入 `WorldState`、`BearingState`、covariance、class、appearance embedding、keyframe 及相机姿态。
- SightMesh 实现候选生成、Mahalanobis、速度、时间 gate、bearing/cross-camera 几何、cannot-link 与关联不确定度；这些是研究核心，**不是 OR-Tools 的责任**。
- 第一阶段可先在现有求解器/简化 assignment 上构建可解释基线；定义 `IAssignmentSolver` 后引入可选 OR-Tools assignment / min-cost-flow（x86 与 D2000 分别验证工具链、ABI、依赖规模）。
- ReID encoder 在 Ascend 310P 上跑；Center 保存 embedding/gallery、global_id/revision/late observation 与全局运动状态；310P 不拥有 ID。
- `GlobalTrack` 和 `WorldState` 后通过 NNG 状态 PUB 供 Render 消费；可靠的 `GlobalIdMerge/Revision` 使用独立 ACK/replay 事件，不依赖 PUB 的可靠性。
- V2 Tracklet Graph + Multicut 与 Python Stone Soup 科学对照均是后续研究，不阻塞 V1 上线；不能把求解器引入当成“全局关联已完成”。

## C3：地图三层职责（不把 Open3D 强行带到所有设备）

| 层 | 建议选型 | Center 拥有的能力 / 集成门槛 |
| --- | --- | --- |
| Occupancy/free/unknown | OctoMap（ezvcpkg 可选） | 由实测 Point Cloud / Mapping UAV 构建，地图版本/时间有效性/未知单元不能猜测 |
| Full Mesh | Open3D RaycastingScene（源码锁定/依赖 ezvcpkg） | 只负责近点、射线、遮挡几何；先衡量 D2000 ARM64 构建、依赖体积、内存与查询耗时，再决定启用 |
| Semantic World Model | SightMesh 自研 | 道路/地形/建筑/目标/可视性、来源可信度、语义与 revision；保留现有 map API 中性未知返回 |

- 继续维护现有轻量 `cpp/src/mesh.cpp` 作为基本/回退实现。Open3D 不替代 Center 的版本管理、语义概率、查询质量和查询时间过滤；极端大地图需考虑分块/LOD/局部缓存。
- `occupancy/esdf` 在真实地图导入且符合版本约定前仍为 unknown。Voxblox/ESDF 放在后续需求门槛，不与 OctoMap 误认为同一功能。
- 显示 Cesium 瓦片/坐标与定位 Mesh 使用同一 `map_revision` 和 WGS84 锚点，变更源地图不能只更新一侧。

## C4：Mapping UAV / FAST-LIO2 对接（需真实输入才开始）

- 由 Mapping UAV 运行 FAST-LIO2（源码锁定），生成带时间/位姿/协方差的点云或局部建图结果；Center 通过 adapter 接收 Point Cloud / 增量 Mesh / 地图 revision。
- LiDAR/IMU 外参、逐点 de-skew、时钟同步由 mapping 数据契约明确；**仿真真值不得作为在线 LIO 输入**。
- OctoMap 更新频率、地图合并、一致性冲突和多版本生命周期按现场资源预算设计。

## C5：依赖与移植

| 类型 | 限制 |
| --- | --- |
| Protobuf / NNG | 可选 ezvcpkg，C++17/ARM64 ABI；Wire adapter 不渗透 Domain Core |
| GeographicLib | 导入器已有可选后端；不要求 Edge/Render 额外安装 |
| Open3D / OR-Tools | 先按 D2000 sysroot、编译器与性能确定可行性，否则 x86 可选工具或旧算法 fallback |
| OctoMap | 仅 Occupancy layer，不能无实测数据就宣称支持占据地图 |
| linuxptp / mavlink-router | 系统服务/外部进程，不作为 Center 的共享库依赖 |

## 最终集中验收（此轮不执行）

- HTTP Push/NNG event 同源多节点长期断链、重发幂等、inbox/blob 与 ACK 事务；late event revision 回放一致。
- 物理 gate/代价可解释性，IDF1/IDSW/HOTA 用有效 GT，对照 baseline 而非臆造优劣。
- D2000 + 310P 真机编译/推理/内存预算、x86 开发配置；几何 backend 同一 Mesh 与相同 map_revision 的 ray/nearest/unknown 一致性。
- 真实 LIO/Occtomap 源时钟、地图有效期，地图/ECEF/ENU/PX4 Home 对齐。
- 默认不开新功能即可维持当前地图导入/查询与 Cesium 服务。

相关现有实现：[地图接口](map-api.md)、[GeographicLib 接入](geographiclib.md)。跨节点关联的历史设计仍见 [Edge 侧中心设计文档](https://github.com/wqhot/sightmesh-edge/blob/dev/docs/center_global_reid_and_association_design.md)，Center 真正实现时以本仓库代码和协议 PR 为准。
