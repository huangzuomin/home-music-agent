#!/usr/bin/env bash
# IMP-04 — 一致的备份脚本（在 VM1 上运行）。
#
# 内容：控制库（SQLite 在线备份 API，不停写）、会话/画像 jsonl、HA 配置、
#       MA 配置、部署 compose 与 HA 包。**不含** NAS 曲库（只读主曲库，另行
#       由 NAS 自身快照保护）。
# 恢复：见 docs/implementation/backup-restore.md（先停写入方 → 还原 → 校验
#       schema_migrations → 只读对账 → 不自动恢复播放）。
set -euo pipefail
APP="${APP_DIR:-/opt/home-music-agent}"
DEST="${BACKUP_DIR:-/opt/home-music-agent/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/backup-$STAMP"

mkdir -p "$OUT"

# ① 控制库：SQLite 在线备份（一致性快照，不锁写）
python3 - "$APP/data/control/control.db" "$OUT/control.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
print("control.db backed up online")
PY

# ② 会话与画像日志（只读挂载的数据直接打包）
tar czf "$OUT/sessions.tar.gz" -C "$APP/data" sessions 2>/dev/null || true

# ③ HA / MA 配置与编排
tar czf "$OUT/config.tar.gz" -C "$APP" \
    config/home-assistant/packages \
    config/home-assistant/configuration.yaml \
    config/music-assistant \
    compose.yaml 2>/dev/null || true

# ④ 清单（镜像 id、版本、文件数）
{
  echo "# backup manifest $STAMP"
  for c in home-assistant music-assistant stt-service voice-gateway music-fetcher; do
    echo "$c image-id: $(docker inspect $c --format '{{.Image}}' 2>/dev/null)"
  done
  echo "files: $(find "$OUT" -type f | wc -l)"
} > "$OUT/MANIFEST.txt"

echo "backup complete: $OUT"
