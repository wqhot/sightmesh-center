# Center V2：方位约束、多节点关联与 NNG 全局状态

> 实验性功能，全部默认不启动；此阶段仅暂存测试，不合并。

## 关联算法

沿用 Edge 的 `spatial.bearing.camera_position_world_m`、`direction_world` 与 `tangent_covariance_rad2`。两条已验证同一 ENU/地图/时间域的射线求最近正向距离，计算交角及归一化距离；相互矛盾的有效射线否决候选，近平行或未知协方差只标记证据不足。**不自动产生三角化位置、联合协方差或融合精度承诺。**

Center 新增 `--solver clique`：每次合并必须保证团内所有 Tracklet 的两两候选都成立且来自不同物理 node_id，默认最多 4 个节点；A-B、B-C 不成立 A-C 时不可传递合并。保留原 `greedy` / `ortools` V1 对照；状态 `associated_group` 只代表身份关联、`fusion=NOT_FUSED`。

## Center Wire Protobuf PUB

独立 `cpp/world_wire` 工程，默认不构建，使用 ezvcpkg 提供 protobuf / nng / libcurl / jsoncpp。发布器只读取本机 Track Inbox 的受认证 `GET /api/v1/mtmct/global-tracks` 和 `global-id-revisions`，不在 C++ 复制 Tracklet 算法，也不创建第二份数据库。

Topic：`/sightmesh/center/world_state` 是完整 Protobuf WorldState 快照；`/sightmesh/center/identity_revision_hint` 是非可靠身份变更提示。数据帧为 `topic + NUL + protobuf Envelope`。WorldState 包括 world_revision、identity_revision_watermark、GlobalTrack 成员、关联状态、真实类别、map/frame/alignment ID 和非融合代表位置。发布 Envelope 的 UTC 时间是 Center 发送时刻而不是 Edge 观测时刻。

新订阅者依靠定期重发完整快照获取最新身份。NNG PUB 不保证修订送达，历史修订必须通过 Center 原有持久化数据库与 HTTP revision API 补读；有序 ACK/GlobalIdMerge Command 以后由可靠通道实现。

## 可选构建与运行（留待集中验收）

    cmake -S cpp/world_wire -B build/world_wire -DSIGHTMESH_WIRE_ROOT=/path/to/sightmesh/wire -DCMAKE_TOOLCHAIN_FILE=/path/to/ezvcpkg/scripts/buildsystems/vcpkg.cmake
    cmake --build build/world_wire
    python3 -m sightmesh_center associate --solver clique --watch --interval 2
    build/world_wire/sightmesh-center-world-wire --listen tcp://127.0.0.1:19704 --inbox-port 18081 --token-file ~/.config/sightmesh/track-inbox.token

跨设备必须明确 `--allow-private-network`，并部署在隔离网/VPN 中；NNG TCP 默认不加密，不能作为公网发布通道。

## 集中测试清单

- `tests/test_association_v2.py`：正常/反向/近平行射线、协方差异常、三节点全团和非传递候选。
- Wire schema unknown-field/major version、NNG 晚加入重播、断连、世界版本变化、身份历史完整补读。
- 不同 PX4 Home、地图版本、时钟域或模型类别标签不同时，严禁自动跨节点关联。
- x86 和 D2000 编译与 SQLite 性能；RK3588 Render/Cesium/FastPlayer 后续统一联调。