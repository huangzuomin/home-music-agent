# 备份与恢复（IMP-04）

## 备份内容与方式

| 对象 | 方式 | 频率建议 |
| --- | --- | --- |
| 控制库 control.db | SQLite **在线备份 API**（一致性快照，不停写） | 每日 |
| data/sessions/*.jsonl | tar 打包（追加型文件，直接打包安全） | 每日 |
| HA 配置 + 包 + MA 配置 + compose.yaml | tar 打包 | 每次变更后 |
| 镜像 digest/ID | MANIFEST.txt（docker inspect） | 每次部署后 |
| **NAS 主曲库** | **不在本脚本范围**——主曲库只读，由 NAS 自身快照保护 | NAS 侧 |

运行：`bash tools/backup-vm.sh`（可用 cron 每日一次；保留最近 N 份，
超期清理由管理员策略决定）。

## 恢复步骤（按序，须在授权窗口）

1. 停写入方：暂停每日 automation；确认无运行中补库作业（`/jobs` 全终态）。
2. 还原文件：解包 backup-<stamp> 覆盖对应路径（先备份当前为 rollback）。
3. 还原控制库：覆盖 control.db 后启动网关，**校验**：
   `SELECT version FROM schema_migrations` 与当前代码迁移版本一致；
   重复执行 `migrate()` 不得产生额外写入。
4. 启动服务，**只读对账**：/readyz 各组件 ok；不自动恢复播放意图
   （计划 §7.4：回滚后的播放状态从 MA 重新获取，不从数据库播回）。
5. 验证：播放一条已验证曲目；画像/最近播放与备份前一致或标记 unknown。

## 红线

- 禁止在服务运行时直接 `cp control.db` 冒充备份（页撕裂）——用脚本内的
  在线备份 API。
- 恢复不自动播放；不删除 NAS 原始音乐；不运行未验证的反向迁移。
