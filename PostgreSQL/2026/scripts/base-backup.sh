#!/usr/bin/env bash
# 每日物理基备(pg_basebackup)。配合 db 服务已开启的 WAL 归档,
# 可实现"恢复到任意时间点"(PITR)。逻辑备份(pg_dump)由 compose 里的 backup 服务负责。
#
# 部署到宿主机 crontab(示例:每天 02:30):
#   30 2 * * * /opt/pg-prod-docker/scripts/base-backup.sh >> /var/log/pg-base-backup.log 2>&1
#
# 可选环境变量:KEEP_DAYS=7(基备与归档 WAL 的保留天数,即 PITR 可回溯窗口)
set -euo pipefail
cd "$(dirname "$0")/.."   # 回到项目根目录(保证 docker compose 找到 yml/.env)

KEEP_DAYS="${KEEP_DAYS:-7}"
DEST="/backups/base/$(date +%F_%H%M%S)"   # 带时间戳:同一天可重复跑,不会目录重名

echo "[$(date '+%F %T')] base backup -> $DEST"
# 容器内默认以 root 执行,必须显式指定连接角色为初始化超级用户
# (官方镜像只创建 $POSTGRES_USER 这一个超级用户,不存在 postgres/root 角色)
docker compose exec -T -e DEST="$DEST" db sh -c 'pg_basebackup -U "$POSTGRES_USER" -X stream -c fast -D "$DEST" -l "base-$(date +%F_%H%M%S)"'

echo "[$(date '+%F %T')] 清理过期基备与归档 WAL(保留 ${KEEP_DAYS} 天)"
# 用容器内的 postgres 用户执行清理,权限天然正确;不能早于最老基备的时间点恢复,删之无碍
docker compose exec -T db find /backups/base -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
docker compose exec -T db find /wal_archive -type f -mtime +"$KEEP_DAYS" -delete

echo "[$(date '+%F %T')] done"
