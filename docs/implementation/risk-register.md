# 风险登记表（IMP-00）

状态：open / mitigated / accepted。新增风险以 R-NEW 编号；与计划 §9 重叠处注明来源。

| ID | 风险 | 阶段发现 | 等级 | 状态 | 处置 |
| --- | --- | --- | --- | --- | --- |
| R-NEW-01 | 部署目录 `/opt/home-music-agent` 未纳入版本管理，现场配置与仓库无法对账 | IMP-00 | 高 | open | IMP-01 起建立部署清单快照机制；中期把部署目录纳入 git 或以导出清单固化 |
| R-NEW-02 | HA/MA 使用浮动 tag（stable/latest），重启即可能换版本 | IMP-00 | 高 | open | IMP-04 固定已验证镜像 digest；升级走显式变更 |
| R-NEW-03 | 开源 compose.yaml 与实际部署漂移：缺 pwa/proxy 两个服务定义 | IMP-00 | 中 | open | IMP-07 前把 pwa/proxy 服务化定义回填 compose |
| R-NEW-04 | 五个业务端口全部 0.0.0.0 裸监听（仅 443 经 caddy 有 TLS） | IMP-00 | 高 | open | IMP-04：内部端口改绑 127.0.0.1 或加访问控制，统一走 HTTPS 入口 |
| R-NEW-05 | MA HTTP 命令面不可用（命令面仅 WS），且 players 响应缺 active/queue_id | IMP-00 | 中 | open | IMP-03 建 ma_reader WS 探针；player_bindings 用配置显式绑定 |
| R-NEW-06 | 根分区使用率 81%（19G 剩余），迁移与备份空间受限 | IMP-00 | 中 | open | IMP-04 备份策略做容量预算；考虑把大文件放 NAS 卷 |
| R-NEW-07 | PWA 已存在但从未被审查（app.js/sw.js 缓存与写路径未知） | IMP-00 | 中 | open | IMP-05/07 前置审查，命中计划风险表第 1 行（优先接回，不另写） |
| R09 | HA/MA 现场版本与文档不同 | IMP-00（计划§9） | 中 | open | 已证实：HA/MA 浮动 tag + OpenAPI 极薄；按计划处置 |
| R15 | 部署资源紧张（根分区 81%） | IMP-00（计划§9） | 中 | open | 控并发与下载体量；不自动扩容 |
| G01 | 延迟补库可能改写已变化的播放意图 | 静态审查 | 高 | open | IMP-02 阻断自动播放 + IMP-03 控制核心 |
| G03 | 受理/确认/出声未分离 | 静态审查 | 高 | open | IMP-03 命令生命周期 + 真实状态回执 |
| G05 | 闭麦语义与默认触发方式信任缺口 | 静态审查 | 高 | open | IMP-02（规则拆出）+ IMP-12（设备输入端闭麦） |
| 其余 G02/G04/G06–G08、T01–T25 | 见 review_31ad436 | 静态审查 | 中 | open | 分属 IMP-03/05/06/07/09–15 |

## 已核实无害项

- `.env` 键名齐全且未泄露值；容器只读挂载 ✓
- NAS 挂载 soft/tcp 可用，容量充足；曲库卷与系统盘分离 ✓
- 会话数据（jsonl）有独立挂载，迁移时可整体快照 ✓

## 登记规则

新风险一律先登记再处置；处置完成后状态改 mitigated 并附证据链接（测试名/提交号）。
本表由 IMP-00 建立，后续任务卡继续追加。
