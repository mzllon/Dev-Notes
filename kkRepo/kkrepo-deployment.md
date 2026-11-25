# kkRepo 部署手册(Docker Compose)+ 从 Nexus OSS 2 迁移

[kkRepo](https://github.com/klboke/kkRepo) 是开源(Apache 2.0)的自托管制品仓库,兼容 Nexus 的 `/repository/<repo>/...` URL 布局、客户端协议和权限模型,定位是 Nexus 的平替。本手册把现有 Nexus OSS 2.15.1-02(`http://maven.dev.i.mored.tech/`,Maven 私服)迁移到 kkRepo。

- 部署形态:单机 Docker Compose,MySQL 8 + File blob 存储(生产可换 S3/OSS)
- 迁移流程见 [kkrepo-nexus2-migration.md](./kkrepo-nexus2-migration.md)(内置迁移只支持 Nexus 3,Nexus 2 需走制品重发布路线)
- 线上版本 `1.5.1`(2026-11-25 由 1.3.2 升级,Flyway V71),升级步骤见第 7 节;未使用反向代理,客户端直连 `http://192.168.10.66:19090`

## 1. 环境要求与规划

| 项 | 值 | 说明 |
| --- | --- | --- |
| Docker / Compose | 24+ / v2 | `docker compose version` 确认 |
| 机型 | ≥ 2C4G | 官方建议单实例 2C4G 起 |
| 镜像 | `ghcr.io/klboke/kkrepo:1.5.1` | JVM 运行时;Native 版为 `1.5.1-native` |
| 数据库 | `192.168.10.2:3306`(公司 MySQL 8.0.31) | 官方默认后端;也支持 PostgreSQL 12+,两者为一等公民 |
| 应用端口 | 19090 → 8080 | 应用端口,见下方说明 |
| 管理端口 | 19091 → 8081 | 管理端口,见下方说明 |
| 主机 | `root@192.168.10.66`(mored-test-server-01,AlmaLinux 8.10) | 管理机免密登录 |
| 目录 | `/mnt/old_data/kkrepo/` | compose 文件、.env、数据都在这 |

> **两个端口各管什么**:
>
> - **8080(应用端口)**:全部业务流量——`/repository/...` 的 Maven 拉取与 deploy、`/admin/` 管理后台、`/browse/` 前台、各格式协议 API。Nginx 只需要代理它。
> - **8081(管理端口,Actuator)**:`/actuator/health`(liveness/readiness)和 `/actuator/prometheus`(Prometheus 抓取指标)。与应用流量分开,便于防火墙只对内网开放、公网代理不转发。
>
> **为什么健康检查用独立 sidecar,而不是把 `healthcheck` 写在 kkrepo 容器里**:Docker 的 `healthcheck` 命令在应用容器内部执行,依赖镜像里有 shell/wget 之类的探测工具;kkRepo 的 JVM 和 Native 镜像都是无 shell 的精简镜像,容器内没有可用工具。所以官方 compose 用一个 Alpine sidecar 跨容器探测 `http://kkrepo:8081/actuator/health/readiness`,JVM/Native 通吃。注意两点:`healthy/unhealthy` 状态挂在 sidecar 上(kkrepo 本身显示 `running` 是正常的);探测失败**不会**自动重启 kkrepo——它是给 `docker compose --wait` 和依赖方看的就绪信号,不是自愈机制。

> **数据库选型说明**:直接复用公司现有 MySQL 8 实例。kkRepo 官方默认后端就是 MySQL(schema V1–V29 历史主线);PostgreSQL 12+ 同为一等公民,想换引擎应新建库走官方迁移流程,不要改 URL 硬切。kkRepo 首次启动由 Flyway 在 `kkrepo` 库自动建表,账号需对该库有全部权限(含 DDL,下方 GRANT 已覆盖);共享实例时按需给这台部署机放行白名单,并评估连接数(`KKREPO_HIKARI_MAXIMUM_POOL_SIZE`,默认够用)。

数据规模参考:旧 Nexus 三个 hosted 仓库共约 5.3 万个文件、9400+ jar,File blob 存储完全够用;后续要上 S3/OSS 时在后台换 blob store 即可(元数据在库里,blob 可重新指向)。

## 2. docker-compose 部署

```bash
mkdir -p /mnt/old_data/kkrepo && cd /mnt/old_data/kkrepo
```

`docker-compose.yml`(基于官方 `docker-compose.quickstart.yml` 精简,去掉了默认关闭的安全扫描服务):

```yaml
name: kkrepo

services:
  # JVM 镜像内用户是 999:999;若切 Native 镜像(KKREPO_IMAGE_TAG=1.5.1-native)改成 1002:1001
  app-data-perms:
    image: alpine:3.20
    user: root
    volumes:
      - ./data:/data
    command: ["sh", "-ec", "mkdir -p /data/blobs && chown -R 999:999 /data"]
    network_mode: none

  kkrepo:
    image: ghcr.io/klboke/kkrepo:${KKREPO_IMAGE_TAG:-1.5.1}
    depends_on:
      app-data-perms:
        condition: service_completed_successfully
    environment:
      KKREPO_DATABASE_TYPE: mysql
      SPRING_DATASOURCE_URL: jdbc:mysql://${KKREPO_MYSQL_HOST}:${KKREPO_MYSQL_PORT:-3306}/kkrepo?useUnicode=true&characterEncoding=utf8&useSSL=false&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true
      SPRING_DATASOURCE_USERNAME: ${KKREPO_MYSQL_USER:-kkrepo}
      SPRING_DATASOURCE_PASSWORD: ${KKREPO_MYSQL_PASSWORD:?set in .env}
      # 加密落库的敏感信息(blob store 密钥、LDAP/OIDC secret、API-key payload)
      KKREPO_CREDENTIAL_SECRET: ${KKREPO_CREDENTIAL_SECRET:?set in .env}
      KKREPO_API_KEY_PAYLOAD_SECRET: ${KKREPO_API_KEY_PAYLOAD_SECRET:?set in .env}
      KKREPO_FILE_BASE_DIR: /var/lib/kkrepo/blobs
      SERVER_TOMCAT_MAX_PART_COUNT: 200
      # 仅反代场景用:填 Nginx 出口 IP 让 X-Forwarded-* 被信任;当前未用反代,保持默认即可
      KKREPO_TRUSTED_PROXIES: ${KKREPO_TRUSTED_PROXIES:-127.0.0.1}
      # 日志只写文件(Spring Boot 4 支持;写到 data 卷,默认 10MB/文件 + 7 天滚动)
      LOGGING_FILE_NAME: /var/lib/kkrepo/logs/kkrepo.log
      LOGGING_CONSOLE_ENABLED: "false"
      # 调日志级别时放开(默认 root INFO):
      # LOGGING_LEVEL_COM_GITHUB_KLBOKE_KKREPO: DEBUG
      # 共享 MySQL 实例需要调优连接池时放开:KKREPO_HIKARI_MAXIMUM_POOL_SIZE=50
    ports:
      - "19090:8080"
      - "19091:8081"
    volumes:
      - ./data:/var/lib/kkrepo
    networks:
      - mored_network
    restart: unless-stopped

  # 健康探测放 sidecar 而非容器内 healthcheck:kkRepo 镜像(JVM/Native)无 shell,
  # 容器内没有探测工具可用;Alpine sidecar 跨容器探测 8081,JVM/Native 通吃。
  # healthy 状态挂在这个服务上;探测失败不会自动重启 kkrepo,只是就绪信号。
  kkrepo-health:
    image: alpine:3.20
    init: true
    depends_on:
      - kkrepo
    command: ["sleep", "infinity"]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://kkrepo:8081/actuator/health/readiness >/dev/null"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 30s
    networks:
      - mored_network
    restart: unless-stopped

networks:
  mored_network:
    external: true
```

> `mored_network` 是已存在的外部网络,compose 不会创建它——启动前 `docker network ls | grep mored_network` 确认,没有就 `docker network create mored_network`。kkrepo 连公司 MySQL、proxy 仓库出网、kkrepo-health 探测 `http://kkrepo:8081`(容器间 DNS 要求同网络)都走它。若公司 MySQL 也是 `mored_network` 上的容器,`KKREPO_MYSQL_HOST` 直接填它的容器名;Nginx 若同样在该网络上,可 `proxy_pass http://kkrepo:8080` 免去本机端口映射的中间一跳。

`.env`(两个 secret 一旦写入生产数据就不要再换,否则已加密数据无法解密;下面是首次生成的写法,线上实例的 `.env` 只在服务器上):

```bash
cd /mnt/old_data/kkrepo
cat > .env <<EOF
KKREPO_IMAGE_TAG=1.5.1
KKREPO_MYSQL_HOST=192.168.10.2
KKREPO_MYSQL_PORT=3306
KKREPO_MYSQL_USER=app_kkrepo
KKREPO_MYSQL_PASSWORD=$(openssl rand -hex 16)
KKREPO_CREDENTIAL_SECRET=$(openssl rand -hex 32)
KKREPO_API_KEY_PAYLOAD_SECRET=$(openssl rand -hex 32)
EOF
chmod 600 .env
```

在公司 MySQL 实例上准备库和账号(`KKREPO_MYSQL_HOST/PORT` 按实际填;kkRepo 首次启动由 Flyway 自动建表,账号需含 DDL 权限):

```sql
CREATE DATABASE IF NOT EXISTS kkrepo DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'app_kkrepo'@'%' IDENTIFIED BY '<.env 里的密码>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON `kkrepo`.* TO 'app_kkrepo'@'%';
FLUSH PRIVILEGES;
```

> **`REFERENCES` 不能省**:MySQL 8 起建外键强制检查该权限(5.7 只在文档里、不生效),而 kkRepo 的 schema 大量用外键(71 个 Flyway 迁移中 31 个建 FK),缺了会在首启建表时报 `ERROR 1142`。以上最小权限已覆盖全部需求:schema 无视图/触发器/存储过程/事件,无需相应权限。账号名 `app_kkrepo` 与 `.env` 的 `KKREPO_MYSQL_USER` 保持一致即可。

> `'kkrepo'@'%'` 按公司规范收窄来源网段更好;共享实例上确认磁盘余量与备份策略已覆盖新库。

启动并等待就绪(`healthy` 状态属于 `kkrepo-health`,应用容器本身显示 `running` 是正常的):

```bash
docker compose up -d --wait
docker compose ps
```

## 3. 初始化

访问 `http://<host>:19090/admin/`(通过 ssh 隧道或临时放行),按 UI 提示创建初始 `Local/admin` 管理员密码。然后依次:

1. **创建 blob store**:后台 Blob Store 页面,新建 `id=default`、类型 File。名为 `default` 的 blob store 是后续仓库与迁移功能的前提。
2. **创建仓库**:按下一节的清单建(也可以放到迁移阶段做)。
3. **(可选)匿名只读**:旧 Nexus 是匿名可读的,若要保持,在后台安全设置里打开匿名访问策略。

验证(三连测):

```bash
# ① 管理端口健康检查:UP
curl -s http://127.0.0.1:19091/actuator/health

# ② 前台可访问
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:19090/browse/

# ③ Prometheus 指标有输出
curl -s http://127.0.0.1:19091/actuator/prometheus | head -3
```

## 4. 仓库规划(对齐旧 Nexus 2)

旧 Nexus 共 10 个仓库,全部 maven2 格式。迁移原则:**hosted 仓库迁数据,proxy 仓库只迁配置(在 kkRepo 重建),virtual 仓库(central-m1)放弃**。

hosted(在 kkRepo 建 Maven hosted,数据由迁移脚本导入):

| 仓库 ID | 版本策略 | 写策略 | 用途 |
| --- | --- | --- | --- |
| `releases` | Release | `ALLOW_ONCE` | 业务正式版;同版本不能覆盖重发,要重发先删旧版本 |
| `snapshots` | Snapshot | `ALLOW` | 业务快照版 |
| `thirdparty` | Release | `ALLOW` | 第三方私服 jar(Aspose 等) |

> 三个仓库的布局策略都是 `PERMISSIVE`、`strictContentTypeValidation=false`(迁移容错)。`releases` 原为 `ALLOW`,迁移完成后于 2026-11-25 改为 `ALLOW_ONCE`。

proxy(在 kkRepo 建 Maven proxy,remote URL 实测如下,无需迁数据):

| 仓库 ID | Remote URL |
| --- | --- |
| `central` | `https://repo1.maven.org/maven2/` |
| `aliyun` | `https://maven.aliyun.com/repository/public/` |
| `tencentyun` | `https://mirrors.cloud.tencent.com/nexus/repository/maven-public/` |
| `huaweiyun` | `https://repo.huaweicloud.com/repository/maven/` |
| `apache-snapshots` | `https://repository.apache.org/snapshots/`(Snapshot) |
| `Spire-Maven-Mirror` | `https://repo.e-iceblue.cn/repository/maven-public/` |
| `douyin-openapi-repo` | `https://artifacts-cn-beijing.volces.com/repository/douyin-openapi/` |

group(在 kkRepo 建 Maven group,成员与旧 `public` 组一致):

| 仓库 ID | 成员(按顺序) |
| --- | --- |
| `public` | releases、snapshots、thirdparty、tencentyun、huaweiyun、apache-snapshots、aliyun、central、douyin-openapi-repo、Spire-Maven-Mirror |

> group 成员有序。三个 hosted 已提到最前,优先命中本地、少走远程(与旧 Nexus 的原始顺序不同)。

迁移后新增的仓库(不在 Nexus 2 迁移范围内):

| 仓库 ID | 格式 | 写策略 | 用途 |
| --- | --- | --- | --- |
| `npm-hosted` | npm hosted | `ALLOW_ONCE` | 公司内部 npm 包 |

上传账号:CI 发包用专用 Local 用户 `deployment`(角色 `nx-deployment`,只有三个 hosted 仓库的写权限和 `public` 的读权限),不要直接用 admin。

## 5. Maven 客户端接入

kkRepo 的 URL 布局是 `/repository/<repo>/...`(Nexus 3 风格)。旧 Nexus 2 是 `/content/repositories/...` 和 `/content/groups/public`,两者不兼容。当前未启用反向代理(第 6 节只是备选方案),客户端直接用 `http://192.168.10.66:19090/repository/<repo>/`;仍指向 `/content/...` 的旧配置会 404。

`settings.xml`(拉包走 group):

```xml
<mirrors>
  <mirror>
    <id>mored-maven</id>
    <mirrorOf>*</mirrorOf>
    <url>http://192.168.10.66:19090/repository/public/</url>
  </mirror>
</mirrors>

<profiles>
  <profile>
    <id>mored</id>
    <repositories>
      <repository>
        <id>mored-public</id>
        <url>http://192.168.10.66:19090/repository/public/</url>
        <releases><enabled>true</enabled></releases>
        <snapshots><enabled>true</enabled><updatePolicy>always</updatePolicy></snapshots>
      </repository>
    </repositories>
  </profile>
</profiles>
<activeProfiles><activeProfile>mored</activeProfile></activeProfiles>
```

`pom.xml` 或 `settings.xml`(发包):

```xml
<distributionManagement>
  <repository>
    <id>mored-releases</id>
    <url>http://192.168.10.66:19090/repository/releases/</url>
  </repository>
  <snapshotRepository>
    <id>mored-snapshots</id>
    <url>http://192.168.10.66:19090/repository/snapshots/</url>
  </snapshotRepository>
</distributionManagement>
```

`servers`(Basic 认证,账号是第 4 节建的 deploy 用户):

```xml
<servers>
<server><id>mored-releases</id><username>deployment</username><password>***</password></server>
<server><id>mored-snapshots</id><username>deployment</username><password>***</password></server>
</servers>
```

验证:

```bash
# 匿名拉取(建一个测试 jar 后)
curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:19090/repository/releases/<group/path>/<artifact>/<version>/<artifact>-<version>.pom

# 发包冒烟
mvn deploy:deploy-file -DgroupId=test -DartifactId=smoke -Dversion=0.0.1 -Dpackaging=jar \
  -Dfile=/tmp/smoke.jar -Durl=http://127.0.0.1:19090/repository/releases/ \
  -DrepositoryId=mored-releases
```

## 6. Nginx 反向代理(备选,当前未启用)

> 当前部署**没有**使用反向代理,客户端直连 `http://192.168.10.66:19090`。本节是当初为"域名切换 + `/content/` 旧路径兼容"准备的备选方案,留作参考。

以后要启用时,把 `maven.dev.i.mored.tech` 指到 kkRepo(Nginx → 19090)。关键点:

- `client_max_body_size 0`,大 jar 上传不受限;
- 带全 `X-Forwarded-*` 头,否则生成的仓库 URL 会带内网 host/scheme;
- **`/content/` 兼容重写**:旧 Nexus 2 路径透明映射到 kkRepo,存量 `settings.xml` 不用改。

```nginx
upstream kkrepo_app {
    server 127.0.0.1:19090;
}

server {
    listen 80;
    server_name maven.dev.i.mored.tech;

    client_max_body_size 0;
    proxy_connect_timeout 30s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;

    # Nexus 2 旧路径兼容:/content/repositories/<id>/、/content/groups/<id>/
    rewrite ^/content/(?:repositories|groups)/([^/]+)(/.*)?$ /repository/$1$2 break;

    location / {
        proxy_pass http://kkrepo_app;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Port $server_port;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization $http_authorization;
    }
}
```

对应的 `KKREPO_TRUSTED_PROXIES` 已在 compose 里设为 `127.0.0.1`(Nginx 与 kkRepo 同机);不同机则填 Nginx 出口 IP。不要改写 `/repository/`、`/admin/`、`/browse/`、`/service/` 路径。

验证:

```bash
# 新路径:200/404 都是通的(404=路径下没有该文件)
curl -s -o /dev/null -w '%{http_code}\n' http://maven.dev.i.mored.tech/repository/public/

# 旧路径重写:与上一条等价
curl -s -o /dev/null -w '%{http_code}\n' http://maven.dev.i.mored.tech/content/groups/public/
```

## 7. 运维

**升级**(2026-11-25 按 1.3.2 → 1.5.1 实测,停机约 1 分钟):

1. **备份**(不设定时任务,升级前手动做)。blob 约 5.6G、6 万个文件,这块盘打包要 12 分钟左右,所以在线先备一遍,不放进停机窗口:

   ```bash
   B=/data/backups/kkrepo-upgrade-<日期>-<旧版本>-to-<新版本>
   mkdir -m 700 -p $B && cd /mnt/old_data/kkrepo
   install -m 600 .env $B/env.before && install -m 644 docker-compose.yml $B/
   # 数据库(元数据、用户、权限、仓库定义);宿主机自带 mysqldump,账号没有 PROCESS 权限,必须加 --no-tablespaces
   MYSQL_PWD="$(sed -n 's/^KKREPO_MYSQL_PASSWORD=//p' .env)" \
     mysqldump --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF \
     -h 192.168.10.2 -P 3306 -u app_kkrepo kkrepo | gzip > $B/kkrepo-pre.sql.gz
   touch $B/.blob-marker
   # blob(制品实体),不压缩,jar 本身已压缩;用 S3/OSS blob store 时备份对象桶即可
   tar -C /mnt/old_data/kkrepo -cf $B/blobs-pre.tar data/blobs
   ```

2. **拉镜像**:服务器直连 `ghcr.io` 能取清单,但下载镜像层只有约 27 KB/s,常见镜像站也都不可用,改在管理机(走代理)拉取后传上去:

   ```bash
   docker pull --platform linux/amd64 ghcr.io/klboke/kkrepo:<新版本>
   docker save --platform linux/amd64 -o kkrepo-<新版本>.tar ghcr.io/klboke/kkrepo:<新版本>
   scp kkrepo-<新版本>.tar root@192.168.10.66:$B/
   # 服务器上载入,并核对镜像 ID 与官方 registry 的一致
   docker load -i $B/kkrepo-<新版本>.tar
   ```

3. **切换**:`docker compose stop kkrepo` → 停服后再导一次库为 `kkrepo-final.sql.gz`(命令同上),并打包增量 `find data/blobs -type f -newer $B/.blob-marker -print0 | tar --null -C /mnt/old_data/kkrepo -cf $B/blobs-delta.tar --files-from=-` → 改 `.env` 的 `KKREPO_IMAGE_TAG`(两个 secret 不动)→ `docker compose up -d --wait`。
4. **验证**:健康检查 UP;日志里 Flyway 迁移全部成功;拉一个存量制品、经 `public` 拉一个代理制品、`mvn deploy:deploy-file` 发一个测试包,测完删掉。
5. **保留规则**:备份目录和旧版本镜像留到下一次升级,届时先做完新备份,再清理上一次的旧备份。
6. **回滚**:还原 `kkrepo-final.sql.gz`、用 `env.before` 换回 `.env`、`docker compose up -d`。还原会丢掉升级后上传的制品,回滚要尽早决定。

> **两个坑**:① 取 `.env` 里的值别用 `source`——变量一旦导出,`docker compose` 插值时环境变量优先于 `.env`,会解析出旧版本号;② 救急启动停掉的容器用 `docker start kkrepo-kkrepo-1`,`docker compose start kkrepo` 会因一次性依赖 `app-data-perms` 已退出而拒绝启动。

**监控**:Prometheus 抓 `http://<host>:19091/actuator/prometheus`,官方提供 Grafana 面板(见仓库 `docs/zh/monitoring-observability-guide.md`)。

**日志**:只写文件,不输出 console——compose 里 `LOGGING_FILE_NAME` 指定文件、`LOGGING_CONSOLE_ENABLED=false` 关闭 console(需要 Spring Boot 4,kkRepo 1.5.1 基于 4.1.2)。宿主机路径 `/mnt/old_data/kkrepo/data/logs/kkrepo.log`,默认 10MB/文件 + 保留 7 天滚动。`docker compose logs` 里只剩启动横幅和日志系统初始化前的极早期输出;若排查"还没写日志就挂了"的启动问题,临时把 `LOGGING_CONSOLE_ENABLED` 注释掉重启即可看到完整 console。

## 8. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 忘记 admin 密码 | 无默认密码,首启 UI 创建(`Local/admin`)。恢复方法见下方"重置 admin 密码" |
| 上传大 jar 报 413 | Nginx `client_max_body_size` 没放开,设为 `0` |
| 使用反代后页面/下载 URL 显示内网地址或 http/https 不对 | 反代头问题:确认 `X-Forwarded-*` 齐全、`KKREPO_TRUSTED_PROXIES` 覆盖代理出口 IP |
| `docker compose ps` 里 kkrepo 是 `running` 但整体不 healthy | 看 `kkrepo-health`:`healthy/unhealthy` 属于它;`docker compose logs kkrepo` 查应用日志 |
| 数据库连不上 | 在宿主机用自带的 mysql 客户端验证(连通信+认证一次测完):`MYSQL_PWD="$(sed -n 's/^KKREPO_MYSQL_PASSWORD=//p' /mnt/old_data/kkrepo/.env)" mysqladmin -h 192.168.10.2 -P 3306 -u app_kkrepo ping`;报连不上是白名单/网络,报拒绝访问是账号授权 |
| 客户端访问 `/content/repositories/...` 404 | 还在用 Nexus 2 的旧路径;当前未启用反代兼容,把 `settings.xml` / `distributionManagement` 改成 `/repository/<repo>/...` |
| deploy 报 `Write policy ALLOW_ONCE forbids writing ...` | `releases` 同版本不能覆盖重发;确认版本号,或先删掉旧版本再发 |

**重置 admin 密码**(无默认密码,首启 UI 创建;忘了按此恢复):有其他 admin 角色账号就先走 UI 用户管理;实在没有就直连 MySQL 改 hash——先生成 kkRepo 同格式(PBKDF2WithHmacSHA256,12 万次迭代)的新 hash:

```bash
python3 -c "import hashlib,base64,os; s=os.urandom(16); d=hashlib.pbkdf2_hmac('sha256','新密码'.encode(),s,120000,32); print('{PBKDF2WithHmacSHA256}120000:'+base64.b64encode(s).decode()+':'+base64.b64encode(d).decode())"
```

```sql
UPDATE security_user SET password_hash = '<上一步生成的hash>' WHERE source = 'Local' AND user_id = 'admin';
```

然后 `docker compose restart kkrepo` 清掉认证缓存。顺带说明:kkRepo 的密码校验兼容 `$shiro1$`(Nexus 2/3 的 Shiro 格式)和旧的 MD5/SHA1 hex hash,这正是 Nexus 用户迁移后无需重置密码也能登录的原理。
