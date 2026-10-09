# PostgreSQL 18 生产部署手册(Docker Compose)

> 傻瓜式操作手册:照着复制粘贴、核对每步"预期结果"即可。原理与设计取舍全部放在第四部分附录,第一部分执行时不需要理解。

## 实例信息卡(本手册所有命令基于此实例)

| 项目 | 值 |
|---|---|
| 服务器 | `root@192.168.10.66`(mored-test-server-01,AlmaLinux,12核/32G 混部) |
| 部署目录 | `/data/pg18` |
| 库名 / 超管账号 | `mored_db` / `mored_app_owner`(密码在 `secrets/pg_password.txt`) |
| 对外地址 | `192.168.10.66:5432`,firewalld 白名单 `192.168.10.0/24` |
| 容器 | `pg-prod-db`(healthy)/ `pg-prod-backup`(healthy),compose 项目名 `pg-prod` |
| 上线时间 | 2026-10-09,基线镜像 `postgres:18.6` |

**换一台服务器部署时只需改三处**:① compose 里的绑定 IP;② firewalld 白名单网段;③ `.env` 里的库名/用户名。其余命令照抄。

---

# 第一部分:全新部署(从零到可用)

## 步骤 0:前提检查

```bash
ssh root@<新服务器IP>
docker version && docker compose version    # 预期:都能打印版本(需 Compose v2)
systemctl is-enabled docker                 # 预期: enabled(开机自启,容器 restart 策略依赖它)
df -h /data                                 # 数据盘建议 SSD,按"数据量×10"预留
```

任一不满足先解决(国内机器可配 `/etc/docker/daemon.json` 镜像加速器后重启 docker)。

## 步骤 1:上传并解压

```bash
# 在自己电脑上执行:
scp pg-prod-docker-<日期>.tar.gz root@<新服务器IP>:/data/
# 回到服务器:
cd /data && tar -xzf pg-prod-docker-*.tar.gz && cd pg-prod-docker
ls    # 预期:能看到 docker-compose.yml README.md scripts/ init/ secrets/
```

## 步骤 2:生成超管密码

```bash
openssl rand -hex 24 | tr -d '\n' > secrets/pg_password.txt
chown 999:999 secrets/pg_password.txt   # ★ 必须有这行:backup 容器以 uid 999 读此文件
chmod 600 secrets/pg_password.txt
stat -c '%U:%a' secrets/pg_password.txt # 预期:systemd-coredump:600(或 999:600)
```

> 为什么 chown:compose 的 secrets 是宿主文件 bind mount,权限跟随宿主;root 属主+600 会导致 uid 999 的 backup 容器读不到,报 `Missing POSTGRES_PASSWORD_FILE`。**以后重新生成密码也要带 chown。**

## 步骤 3:环境变量

```bash
cp .env.example .env
vim .env                # 改 POSTGRES_DB(库名)和 POSTGRES_USER(超管账号名)
chmod 600 .env
cat .env                # 预期:两行非注释配置
```

> 这两个值**只在数据目录为空的首次启动生效**,之后改 .env 不影响已建的库。

## 步骤 4:运行时目录与权限

```bash
mkdir -p data wal_archive backups
docker run --rm postgres:18.6 id -u postgres    # 预期输出: 999
chown 999:999 wal_archive backups
```

## 步骤 5:网络绑定(换机器必改!)

```bash
vim docker-compose.yml
#   找到 ports 段,把 192.168.10.66 改成本机内网 IP(不要 0.0.0.0,严禁公网)

# 防火墙只对客户端所在网段放行(网段按实际情况改):
firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.10.0/24 port port=5432 protocol=tcp accept'
firewall-cmd --reload
firewall-cmd --list-rich-rules | grep 5432      # 预期:能看到刚加的规则
```

## 步骤 6:启动

```bash
docker compose config -q && echo 配置OK
docker compose pull
docker compose up -d
```

## 步骤 7:验证清单(逐条跑,每条有预期)

```bash
# 1) 两个容器健康(backup 可能先 starting,一分钟内转 healthy)
docker compose ps
#    预期:pg-prod-db ... Up (healthy)   pg-prod-backup ... Up (healthy)

# 2) 数据库就绪
docker compose logs db | tail -3
#    预期:database system is ready to accept connections

# 3) 数据页校验和(应为 on,仅首次初始化生效)
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW data_checksums;"'

# 4) 慢查询统计扩展已启用(应列出 plpgsql 和 pg_stat_statements)
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extname FROM pg_extension;"'

# 5) WAL 归档正常(failed_count 必须= 0)
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT archived_count, failed_count FROM pg_stat_archiver;"'

# 6) 逻辑备份链路
docker compose exec backup /backup.sh
#    预期最后一行:SQL backup created successfully
ls -lh backups/last/      # 预期:有 <库名>-<时间>.sql.gz 和 -latest 软链

# 7) 客户端路径连通(在任一台客户端机器上)
timeout 3 bash -c "</dev/tcp/<服务器IP>/5432" && echo 通
```

## 步骤 8:定时物理基备(PITR 的另一半,必做)

```bash
chmod +x scripts/base-backup.sh
( crontab -l 2>/dev/null; echo "30 2 * * * $(pwd)/scripts/base-backup.sh >> /var/log/pg-base-backup.log 2>&1" ) | crontab -
crontab -l | grep base-backup    # 预期:打印该行
```

手动跑一次验证:

```bash
bash scripts/base-backup.sh
ls -lh backups/base/             # 预期:出现一个基备目录,大小≈数据库体积
```

## 步骤 9:客户端接入

| 参数 | 值 |
|---|---|
| 主机 / 端口 | `<服务器内网IP>` / `5432` |
| 数据库 / 用户 | `.env` 里的 `POSTGRES_DB` / `POSTGRES_USER` |
| 密码 | `secrets/pg_password.txt` 内容 |
| SSL | 选 disable 或 prefer(未配 TLS) |

日常**不要把超管账号配进业务应用**;确需最小权限账号时,在 psql 里执行 README 附录「按需:创建业务只读写账号」。

---

# 第二部分:日常操作(按需求查)

**状态与健康**

```bash
docker compose ps                                            # 两容器 Up (healthy)
docker inspect --format '{{.State.Health.Status}}' pg-prod-db # healthy
docker stats --no-stream pg-prod-db                          # CPU/内存占用
df -h /data && du -sh data wal_archive backups               # 磁盘巡检
```

**看日志**

```bash
docker compose logs -f db                      # 跟踪
docker compose logs db | grep -iE 'error|fatal' | tail   # 挑错误
```

**进入 psql(管理用)**

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

**重启 / 停止**(stop 走 fast shutdown,给 2 分钟 checkpoint 时间,不要 docker kill)

```bash
docker compose restart db
docker compose stop db && docker compose up -d db
```

**手动备份**

```bash
docker compose exec backup /backup.sh      # 逻辑备份,产物在 backups/last/
bash scripts/base-backup.sh                # 物理基备,产物在 backups/base/
```

**修改超管密码**(三步缺一不可)

```bash
# 1) 生成新密码并更新文件
openssl rand -hex 24 | tr -d '\n' > secrets/pg_password.txt
chown 999:999 secrets/pg_password.txt && chmod 600 secrets/pg_password.txt
# 2) 在 psql 会话里改库(\password 交互输入,避免密码进 history)
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
   \password
# 3) 重启 backup 容器使新凭据生效
docker compose restart backup
```

**新建数据库**(编码不用指定,默认继承集群的 UTF8;建后不可改编码)

```sql
CREATE DATABASE 新库名;                       -- 常规
CREATE DATABASE 新库名 TEMPLATE template0
  ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8';   -- 显式指定
SELECT datname, pg_encoding_to_char(encoding), datcollate FROM pg_database;   -- 查看各库编码
```

**中文按拼音排序**(默认码位序不满足时,按查询指定即可,不改库)

```sql
SELECT name FROM t ORDER BY name COLLATE "zh-x-icu";
```

**每日巡检 SQL**(进 psql 后执行;归档 failed≠0 最优先处理)

```sql
SELECT failed_count, last_failed_time FROM pg_stat_archiver;              -- 归档健康
SELECT count(*) FROM pg_stat_activity;                                    -- 连接数
SELECT pid, now()-xact_start AS dur, left(query,60)
  FROM pg_stat_activity WHERE now()-xact_start > '10 min';                -- 长事务
SELECT calls, round(total_exec_time::numeric) ms, left(query,80)
  FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;         -- Top 慢查询
```

---

# 第三部分:应急手册

## 3.1 恢复演练(逻辑备份,建议每月做)

```bash
cd /data/pg18
docker run --rm -d --name restore-drill -e POSTGRES_PASSWORD=drill postgres:18.6
sleep 6
docker exec restore-drill createdb -U postgres mored_db          # pg_dump 不含建库语句
zcat backups/last/mored_db-latest.sql.gz | docker exec -i restore-drill psql -U postgres -d mored_db
docker exec restore-drill psql -U postgres -d mored_db -c '\dt'  # 核对表
docker rm -f restore-drill
```

## 3.2 PITR:恢复到任意时间点(灾难场景)

前提:`backups/base/` 有基备 + `wal_archive/` 有归档(两者缺一不可)。

```bash
cd /data/pg18
docker compose stop db
mv data data.broken-$(date +%F)                       # 保全坏库现场

# 1) 铺回最近一份基备(容器内操作,权限自动正确)
BASE=$(ls -1 backups/base | tail -1) && echo 使用基备: $BASE
docker compose run --rm db bash -c "mkdir -p /var/lib/postgresql/18/docker && \
  cp -a /backups/base/${BASE}/. /var/lib/postgresql/18/docker/"
docker compose run --rm db bash -c 'touch /var/lib/postgresql/18/docker/recovery.signal'

# 2) 前台恢复(补放 WAL 到归档尽头;另开终端看输出)
docker compose run --rm db postgres -c restore_command="cp /wal_archive/%f %p"
#    要恢复到指定时刻(如误删前 1 分钟),改用:
# docker compose run --rm db postgres \
#   -c restore_command="cp /wal_archive/%f %p" \
#   -c recovery_target_time="2026-10-09 14:59:00+08" \
#   -c recovery_target_action="promote"
#    到达目标点会暂停,在 psql 里执行 SELECT pg_wal_replay_resume();

# 3) 日志出现 ready to accept connections 后核对数据,然后 Ctrl-C,正常拉起:
docker compose up -d
#    稳定数日后:rm -rf data.broken-*
```

## 3.3 小版本升级(同大版本,停机约 1–2 分钟)

```bash
cd /data/pg18
ls -lh backups/daily/                     # 0) 确认最近备份成功
vim docker-compose.yml                    # 1) image: postgres:18.x 改到目标小版本(先看 release notes)
docker compose pull db                    # 2)
docker compose up -d                      # 3) 自动重建变化的容器
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW server_version;"'
```

## 3.4 大版本升级(如 18 → 19,导出/导入)

```bash
cd /data/pg18
# 1) 逻辑全量导出
docker compose exec -T db pg_dumpall -U mored_app_owner > full-$(date +%F).sql
# 2) 停服,旧数据留底
docker compose down
mv data data.v18-$(date +%F) && mkdir data && chown 999:999 data
# 3) 换镜像(db 换 postgres:19.x,backup 换 :19)后全新初始化
vim docker-compose.yml && docker compose up -d db
# 4) 导入并核对
cat full-*.sql | docker compose exec -T db psql -U mored_app_owner -d postgres
# 5) 稳定后归档 data.v18-*
```

---

# 第四部分:附录

## 4.1 故障排查速查

| 症状 | 原因 | 处理 |
|---|---|---|
| 起不来,`data directory has wrong ownership` | bind mount 属主不对 | 数据目录交给 entrypoint 接管;`wal_archive/backups` 需 `chown 999:999` |
| 起不来,提示 `old database layout` | 18+ 镜像挂载点是 `/var/lib/postgresql`(非 `/data`) | 按本手册挂载;存量旧布局数据走 3.4 迁移 |
| backup 刷 `Missing POSTGRES_PASSWORD_FILE` | 密码文件是 root 属主,uid 999 读不到 | `chown 999:999 secrets/pg_password.txt` 后 `docker compose restart backup` |
| base-backup 报 `role "root" does not exist` | 旧版脚本未显式指定角色 | 已修复;确认服务器上 scripts/base-backup.sh 含 `-U "$POSTGRES_USER"` |
| base-backup 报 `directory already exists` | 同一天手动重跑,基备目录重名 | 删掉当天目录再跑,或用带时间戳的新版脚本 |
| 客户端连不上 | 端口只绑了特定 IP,或防火墙未放行 | `ss -ltn \| grep 5432` 看绑定;`firewall-cmd --list-rich-rules` 看白名单 |
| `FATAL: password authentication failed` | 密码改了文件没改库(或反之) | 按第二部分「修改超管密码」三步走 |
| `could not resize shared memory` | shm 不够 | `shm_size` 调大(已设 1gb) |
| 日志刷 `archive command failed` | `wal_archive/` 属主不是 999 | `chown 999:999 wal_archive` |
| 容器 OOM 重启 | 内存参数相对主机过大 | `dmesg \| grep -i oom` 确认;按 4.2 重新配参数 |
| 磁盘持续增长 | 归档卡住/日志未轮转/基备堆积 | 查 `pg_stat_archiver`、`du -sh data wal_archive backups`、cron 是否在清旧基备 |

## 4.2 参数基线与换算(当前按 2C/8G 独占主机定档)

本实例实为 12C/32G 混部,参数刻意保持 8GB 基线(偏保守、够用):`shared_buffers=2GB`(25% 内存,启动即预分配)、`effective_cache_size=6GB`(75%,给优化器参考)、`work_mem=16MB`(每排序节点×并发,宁小勿大)、`max_connections=200`(插槽上限,实际并发靠应用连接池压到 20–50)、并行度按 2 核收敛(`max_parallel_workers=2`/`per_gather=1`)。

- 内存不足的结局不是慢,是 postmaster 被 OOM kill、全员掉线;
- 扩容时同比例调大内存参数、放开并行度即可;`shm_size` 占的也是宿主内存;
- `fsync`/`synchronous_commit`/`full_page_writes` 永远保持 on。

## 4.3 按需:创建业务只读写账号

```sql
-- 在 psql(超管会话)执行;业务应用一律用这种账号,不用超管
CREATE ROLE 业务账号 LOGIN PASSWORD '强密码';
GRANT CONNECT ON DATABASE mored_db TO 业务账号;
GRANT USAGE ON SCHEMA public TO 业务账号;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO 业务账号;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO 业务账号;
-- 让 owner 以后新建的表自动对它可见:
ALTER DEFAULT PRIVILEGES FOR ROLE mored_app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO 业务账号;
ALTER DEFAULT PRIVILEGES FOR ROLE mored_app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO 业务账号;
```

> 注意:PG 16+ **禁止降级初始超级用户**(`mored_app_owner` 永远是 SUPERUSER),所以纪律是:超管只做运维(升级/备份/建角色),业务连接一律用上面建的普通账号。删除这类账号前,先 `REVOKE ALL ON ALL TABLES ... FROM 账号;` 再 `DROP ROLE 账号;`。

## 4.4 设计决策速览(为什么长这样)

| 决策 | 理由 |
|---|---|
| 镜像锁死小版本 `postgres:18.6` | 避免 `latest` 拉到不兼容镜像;小版本只含修复可滚动升级 |
| 挂载 `/var/lib/postgresql`(18+ 新布局) | 数据在宿主 `./data/18/docker`;未来大版本共用挂载点 |
| `stop_signal: SIGINT` + `stop_grace_period: 2m` | 默认 SIGTERM=smart shutdown 会等客户端到被 SIGKILL;SIGINT 干净退出 |
| `shm_size: 1gb` | PG 并行查询共享内存,默认 64MB 会报错 |
| `POSTGRES_INITDB_ARGS=--data-checksums` | 页校验和,尽早发现静默损坏(仅首次 initdb 生效) |
| 密码走 secrets 文件 | 不出现在 `docker inspect` 明文环境里;需 chown 999(见 4.1) |
| 双层备份 | 逻辑备份(pg_dump,日/周/月轮转)救误删;物理基备+WAL 归档救灾难、可 PITR |
| 容器+PG 双层日志轮转 | json-file 限 50m×3;慢查询日志走 stderr 由 docker 收集 |

## 4.5 参考资料

- 版本策略:https://www.postgresql.org/support/versioning/ · 18 发布说明:https://www.postgresql.org/docs/release/18.0/
- 官方镜像(entrypoint/18+ 布局):https://github.com/docker-library/postgres
- 逻辑备份镜像:https://github.com/prodrigestivill/docker-postgres-backup-local
- 待办:为 `backups/` 与 `wal_archive/` 配置异地同步(rclone/rsync,见 compose 注释)
