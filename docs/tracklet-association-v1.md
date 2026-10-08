# Center V1：Tracklet Repository + 保守跨节点物理关联

> 实施 PR：**代码完成待验证，尚未执行单元测试、编译、D2000 部署或跨平台联调。** 叠加在 Center Durable Inbox PR #3 上，不要求 NNG bridge PR #4，HTTP/NNG 两种事件传输最终进入同一 SQLite 文件。

## 1. 目前实际实现

1. `tracklets.py` 从 **Center 已连续持久 ACK 且 Blob 齐全**的 TrackEvent 重建 Tracklet。完整局部轨迹身份键为 `(node_id, camera_id, session_id, local_id)`，片段还携带 `segment_start_seq`；原始观测按 **Edge 事件时间**排序（晚到仍进入重建），不按到达顺序或 Center HTTP commit 时间重建。
2. `association_v1.py` 通过明确配置的共同时间域/offset/误差、共同地理原点配准 `alignment_id`、地图版本、坐标帧、类别，以及有效 3×3 协方差的马氏距离 + 速度上界筛选候选。未知地图、未知时钟域、未知或非正定协方差、时间差超过窗口、不同类别或未校准节点 **不自动合并**。
3. `GreedySolver` 为无需依赖的确定性 **one-to-one baseline**；可选 Python `OrToolsSolver` 仅负责最大匹配求解，不计算物理代价。V1 一轮只选互不重叠的**两节点成对匹配**，不借两两高分进行无约束的三节点传递闭包。Python OR-Tools 仅在支持的 x86 开发/实验环境按需安装；D2000 不要求安装。
4. `global_tracks.py` 保留历史 `global_id`（优先继承同一 Tracklet 成员原身份），把新建、合并、拆分和迟到事件导致的身份移动写入不可变 `identity_revisions_v1`，与 `global_tracks_v1` / `world_snapshots_v1` **同一 SQLite 事务**提交。输出独立的 `world_revision` 和 `identity_revision`。
5. `/api/v1/mtmct/global-tracks`、`/api/v1/mtmct/global-id-revisions?after=0` 由已有 Center Durable Inbox HTTP 服务提供**只读**状态；配置 Token 时也保护只读端点，不由 HTTP Handler 决定关联或重算。

**重要限制**：GlobalTrack 的 `representative` 只取一条质量可用的原始观测，显式返回 `fusion: NOT_FUSED`。不进行简单加权平均，也**不声称**已经有多节点协方差交叉相关估计或 GTSAM 联合三维融合。单节点目标状态为 `provisional`；未定位目标为 `unlocalized`；只有有严格物理门限通过的两节点成对目标是 `associated_pair`（仍未完成联合估计）。

## 2. 明确时钟和地图的信任边界

Edge 的历史可靠 JSON Event 含 `event_time_ns` 和位置 `coordinate_frame_id`，但它 **不携带完整 `clock_domain` 与节点地图配准证明**；两节点都叫 `local_enu_v1` 并不保证彼此 ENU 原点相同。

为了避免自动生成物理上错误的全局 ID，需要由真实部署/仿真编排**验证后**在 Center `config/center.json` 中把对应节点 `association.sources.<node>.verified` 改成 `true`，配置共同：

```json
{
  "association": {
    "max_pair_dt_s": 0.25,
    "maximum_speed_mps": 30.0,
    "maximum_sigma_m": 15.0,
    "max_mahalanobis_sq": 11.344866730144373,
    "min_quality": 0.0,
    "clock_error_gate_s": 0.05,
    "class_labels": {
      "tracker_uav_1": {"0": "tank", "1": "person"},
      "tracker_ugv_1": {"0": "tank", "1": "person"}
    },
    "sources": {
      "tracker_uav_1": {
        "verified": true,
        "domain": "shared_sim",
        "offset_ns": 0,
        "clock_uncertainty_ns": 5000000,
        "alignment_id": "ENU_ORIGIN_TESTED_A",
        "coordinate_frame_id": "local_enu_v1",
        "map_revision": "ACTUAL_MAP_REVISION"
      },
      "tracker_ugv_1": {
        "verified": true,
        "domain": "shared_sim",
        "offset_ns": 0,
        "clock_uncertainty_ns": 5000000,
        "alignment_id": "ENU_ORIGIN_TESTED_A",
        "coordinate_frame_id": "local_enu_v1",
        "map_revision": "ACTUAL_MAP_REVISION"
      }
    }
  }
}
```

上面 `verified=true` 只是说明**确实在编排/测量后验证过**的操作方式，不能直接复制进未校准现场。默认 sample 的 `verified=false`；这样只有独立 Global ID，不跨节点关联。

- `domain=shared_sim` 表示各节点真正共用 Gazebo/PX4 仿真时间，或已证明在同一域；`shared_utc` 表示经可靠映射后的 UTC。不同 PX4 boot clock 不能仅因纳秒数形式相同便标为 `shared_utc`。
- `alignment_id` 必须表示同一个已实际对齐的 ENU 源点+姿态定义。地图版本一致不等于本地 PX4 Home 已对齐。
- `offset_ns` 与 `clock_uncertainty_ns` 是实测/经标准协议同步校验的时钟变换，不是根据帧号猜值。
- `class_labels` 是每个模型的**真实标签定义**，不能假设类别 ID=0 都是 person 或 tank。示例中的 `0 → tank` 必须按 RKNN/MNN 的实际 label 文件确认；不同模型的数字 ID 即使相同，类别语义不同也禁止关联。缺少明确标签映射的节点只保留 provisional/unlocalized。
- `map_revision` 需与**事件内** `spatial.localization_quality.map.revision` 且 `map.valid=true` 一致。无地图修订的事件可保留为单节点观测，**当前不会参与跨节点成对关联**。
- 跨节点异构设备位置协方差如未知、零矩阵、异常不对称或数值非正定时，必须拒绝物理自动合并，不能默认把单位阵视为已标定不确定度。

## 3. 启动与数据流

原地图服务与可靠接收仍各自独立（见 [TrackEvent Inbox](reliable-track-inbox.md)）。在 Edge 已向 Inbox 持久上传事件后：

```bash
# 单次生成确定性 GlobalTrack 快照
python3 -m sightmesh_center --config config/center.json associate \
    --solver greedy --db data/track-inbox.sqlite3

# 如需在 Center 上持续计算（显式由部署启动器启用）
python3 -m sightmesh_center --config config/center.json associate \
    --watch --interval 2 --solver greedy

# 读取当前最新持久状态，不触发重算
python3 -m sightmesh_center global-state --db data/track-inbox.sqlite3
```

`--solver ortools` 是 x86 实验可选模式，缺包/未证明最优时明确报错，不会悄悄回退为另一算法并声称相同结果。新提交事件仅影响下次 `associate`；只运行 Inbox **不自动启动关联器**。这确保地图服务、可靠接入和 V1 关联的资源/性能可分别测量。

中心只读 HTTP API（随 18081 的 Inbox 服务启动）：

```text
GET /api/v1/mtmct/global-tracks
GET /api/v1/mtmct/global-id-revisions?after=0&limit=100
```

需以同一 Authorization Bearer Token 访问（Token 已配置的情况下）。状态包含 `world_revision`、每个 `global_id`、`identity_revision`、`members` 和标注为 `representative_observation_not_fused` 的位置；Revisions 是 append-only，Render 不得仅按 local ID 合并。**这两个接口现在是 JSON 只读过渡接口，不是已验证的 Center→Render NNG Protobuf 通道。**

## 4. 与最终设计 6.5 的差距

当前没有上线的研究能力：跨源协方差相关性保守融合、bearing-only 交叉几何与三角化、地图可达性/遮挡代价、ReID 关键帧外观向量、late event bounded incremental factor graph、动态机动模型、三节点以上的全局图匹配、全局目标完整物理语义。这些仍是后续 Center 组件 PR 的算法内容。

不允许把这轮 GlobalTrack baseline 的两节点一对一 matching 指标解释为完整跨镜追踪精度，也不能使用未验证的定位结果校验自己。

## 5. 集中验收阶段（这批暂不执行）

- staged `tests/test_global_track_association.py`：相同局部 ID 在不同节点分开；未验证地图/时间不关联；covariance 失效拒绝；多节点可观测状态配对；Blob 缺失不读；迟到事件触发身份修订；重复执行不升级 world_revision；OR-Tools optional。
- 与真实 Edge Event JSON 核对 `spatial.world.position_covariance_m2`、`spatial.localization_quality.map.valid/revision`、事件时间和采集阶段世界坐标（禁止“仿真同世界”但 PX4 local ENU 不对齐的情况）。
- Center D2000 CPU/SQLite IO 性能、10万级 TrackEvent 扫描成本、关联 candidate O(N²) 基准。本版上限 5000 Tracklets、200k Event、10k candidates，超过直接报错，不声称可直接生产大规模图关联。
- IdentityRevision 晚到修改/历史转移、Render Global ID 去重和版本重放；网络延迟/断线后的可解释性及定量指标。
