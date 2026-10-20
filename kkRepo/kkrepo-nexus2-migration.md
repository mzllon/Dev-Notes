# 从 Nexus OSS 2.15.1-02 迁移到 kkRepo

源端:Sonatype Nexus Repository Manager **OSS 2.15.1-02**,`http://maven.dev.i.mored.tech/`(Maven 私服)。目标:kkRepo,部署见 [kkrepo-deployment.md](./kkrepo-deployment.md)。

迁移脚本:[scripts/migrate_nexus2.py](./scripts/migrate_nexus2.py)(已用本地仿真环境和真实源端验证)。

## 1. 源端现状(2026-10-20 实测)

- **匿名可读**:目录索引、`/content/repositories/...` 下载、`/service/local/*` REST 全部无需登录即可访问,整个迁移只读源端,**不需要旧 Nexus 的账号**。脚本支持 `--user/--password`,以备日后源端开启认证。
- 实例自 2026-08-13 运行,baseUrl `http://maven.dev.i.mored.tech`,共 10 个仓库,全部 maven2 格式:

| 仓库 ID | 类型 | 版本策略 | Remote URL(proxy) | 处理方式 |
| --- | --- | --- | --- | --- |
| `releases` | hosted | RELEASE | — | **迁数据** |
| `snapshots` | hosted | SNAPSHOT | — | **迁数据** |
| `thirdparty` | hosted | RELEASE | — | **迁数据** |
| `central` | proxy | RELEASE | `https://repo1.maven.org/maven2/` | 只迁配置 |
| `aliyun` | proxy | RELEASE | `https://maven.aliyun.com/repository/public/` | 只迁配置 |
| `tencentyun` | proxy | RELEASE | `https://mirrors.cloud.tencent.com/nexus/repository/maven-public/` | 只迁配置 |
| `huaweiyun` | proxy | RELEASE | `https://repo.huaweicloud.com/repository/maven/` | 只迁配置 |
| `apache-snapshots` | proxy | SNAPSHOT | `https://repository.apache.org/snapshots/` | 只迁配置 |
| `Spire-Maven-Mirror` | proxy | RELEASE | `https://repo.e-iceblue.cn/repository/maven-public/` | 只迁配置 |
| `douyin-openapi-repo` | proxy | RELEASE | `https://artifacts-cn-beijing.volces.com/repository/douyin-openapi/` | 只迁配置 |
| `central-m1` | virtual | RELEASE | — | 放弃(maven 1 遗留格式) |

group:`public` 成员(原始顺序)= tencentyun、huaweiyun、releases、snapshots、thirdparty、apache-snapshots、aliyun、central、douyin-openapi-repo、Spire-Maven-Mirror。

**hosted 数据规模**(全量目录爬取统计,已完成):

| 仓库 | 文件总数 | 其中 jar/war | 其中 pom | maven-metadata.xml |
| --- | --- | --- | --- | --- |
| releases | 22,084 | 4,405 | 2,729 | 197 |
| snapshots | 31,059 | 4,969 | 3,125 | 2,087 |
| thirdparty | 101 | 7 | 7 | 6 |
| **合计** | **53,244** | **9,381** | | |

proxy 仓库是远程缓存,kkRepo 侧按部署手册第 4 节重建即可,不迁数据。

## 2. 为什么不能走 kkRepo 内置迁移

kkRepo 自带的 `Nexus Metadata` / `Nexus Repository Data` 迁移**只支持 Nexus 3**:它的读取端全部走 `/service/rest/v1/*` 和 Script REST API(Groovy),仓库文档与源码(`migration-nexus` 模块、`NexusRestClient.java`)里没有任何 Nexus 2 适配。

Nexus 2 的 API 是老式 `/service/local/*`,和 Nexus 3 完全不同,内置迁移无法读取。因此 Maven 制品走**制品重发布(redeploy)**路线:

```text
旧 Nexus 2(匿名读:HTML 目录索引 + GET)
    → migrate_nexus2.py 爬取文件清单,逐个 GET
    → Basic 认证 PUT 到 kkRepo Maven hosted(/repository/<repo>/<path>)
```

备选工具 `maven-repository-provisioner` 也能做,但它按 Maven 坐标递归拉取、依赖 pom 元数据齐全,对元数据残缺的老私服容易漏文件;自写脚本直接信任目录索引,所见即所迁,且支持断点续传和增量。二者择一即可,本文用脚本方案。

## 3. 迁移前检查单

```text
[ ] kkRepo 已按部署手册跑起来,健康检查 UP
[ ] blob store default 已创建
[ ] hosted 仓库 releases / snapshots / thirdparty 已创建(版本策略对齐第 1 节表格)
[ ] 已创建 deploy 账号(对三个 hosted 仓库有写权限),拿到用户名/密码
[ ] 迁移机同时能访问旧 Nexus(80)和 kkRepo(19090)
[ ] kkRepo 磁盘余量 > 源端三个仓库总体积(保守按 2 倍单仓库最大体积预留)
[ ] 和团队约定发包冻结窗口:全量迁移期间旧 Nexus 可以照常拉包,建议冻结 deploy;
    不冻结也行,迁完后走一轮增量(第 7 节)兜底
```

## 4. 迁移脚本说明

[scripts/migrate_nexus2.py](./scripts/migrate_nexus2.py) 的行为约定:

- **数据源**:只依赖源端匿名可读的目录索引(HTML),广度优先爬取 `/content/repositories/<repo>/` 下全部文件,不解析 Maven 坐标,所见即所迁;
- **上传**:`GET 源文件 → 临时文件 → PUT /repository/<target>/<path>`,Basic 认证,流式传输(大 jar 不占内存);
- **断点续传**:进度记在 `--state` JSON,中断重跑自动跳过已完成路径;失败项记录原因,修完直接重跑即可;
- **增量**:`--incremental` 时先 HEAD 源/目标比对 Content-Length,一致跳过,用于切换日补量;
- **顺序**:`maven-metadata.xml` 最后上传(先传制品再传元数据,避免元数据先于内容);
- **校验文件**:`.sha1/.md5/.sha256/.sha512` 默认原样上传,与源端逐字节一致,便于迁移后做 checksum 抽查;
- **限速**:并发 = `--workers`(默认 8),对旧 Nexus 保持克制,按机器和网络情况上调。

用 `plan` 先摸清单(只读源端,不上传):

```bash
python3 scripts/migrate_nexus2.py plan \
  --source http://maven.dev.i.mored.tech --source-repo releases \
  --state state-releases.json
# [plan] releases: files=22084 (artifacts+checksums=21887, maven-metadata.xml=197)
```

## 5. 全量导入

三个 hosted 仓库逐个执行(以 releases 为例):

```bash
python3 scripts/migrate_nexus2.py push \
  --source http://maven.dev.i.mored.tech \
  --source-repo releases \
  --target http://<kkrepo-host>:19090 \
  --target-repo releases \
  --user deploy --password '<deploy密码>' \
  --state state-releases.json \
  --workers 8
```

- 换仓库时同步替换 `--source-repo/--target-repo/--state` 三个参数;
- `--workers` 从 8 起步,观察旧 Nexus 与 kkRepo 的负载后可到 16/32;
- 中断(Ctrl-C、断网、kkRepo 重启)后**原命令重跑**即可,已完成的自动跳过;
- 53,244 个文件、8 并发,实测目录爬取约 3 分钟,含上传预计 2~4 小时(取决于包大小与网络)。

上传完成后 kkRepo 后台 `Nexus Repository Data` 之类的内置迁移**不需要也不会被用到**,数据直接落在 hosted 仓库里,`/browse/` 和 Maven 拉取立即可用。

## 6. 校验

```bash
# ① 数量对比:kkRepo 侧逐仓库递归数一遍与 plan 数比对
#    (用 kkRepo 前台 browse 或存储目录统计均可)

# ② checksum 抽查:随机抽 N 个 .sha1,源端与 kkRepo 各下载比对
for p in $(python3 -c "
import json, random
plan = json.load(open('state-releases.json'))['plan']
sha1s = [p for p in plan if p.endswith('.sha1')]
print('\n'.join(random.sample(sha1s, 20)))"); do
  a=$(curl -s "http://maven.dev.i.mored.tech/content/repositories/releases/$p")
  b=$(curl -s "http://<kkrepo-host>:19090/repository/releases/$p")
  [ "$a" = "$b" ] || echo "MISMATCH: $p (src=$a dst=$b)"
done
echo "checksum spot-check done"

# ③ Maven 真实拉取:从 kkRepo group 拉几个迁移过来的业务包
mvn dependency:get \
  -DremoteUrl=http://<kkrepo-host>:19090/repository/public/ \
  -DgroupId=<迁移包group> -DartifactId=<迁移包artifact> -Dversion=<版本>
```

有 MISMATCH 时:删除 kkRepo 侧该文件对应记录(或整个用空仓库重导),`--incremental` 重跑覆盖。

## 7. 增量与切换

全量迁移到正式切换之间如果旧 Nexus 还有写入,用增量兜底:

```bash
python3 scripts/migrate_nexus2.py push \
  --source http://maven.dev.i.mored.tech \
  --source-repo releases \
  --target http://<kkrepo-host>:19090 \
  --target-repo releases \
  --user deploy --password '<deploy密码>' \
  --state state-releases.json \
  --incremental --workers 8
```

脚本会重新爬目录(只读,分钟级),HEAD 比对后只上传新增/有变化的文件;`maven-metadata.xml` 全部重比一遍(内容变了会覆盖)。

**切换日 runbook**:

```text
1. 冻结旧 Nexus 发包(CI 停 deploy,拉取不受影响)
2. 三个仓库各跑一轮 --incremental 增量
3. 第 6 节校验(数量 + checksum 抽查 + 真实拉取)
4. 域名切换:maven.dev.i.mored.tech 的 Nginx 指向 kkRepo(见部署手册第 6 节,
   含 /content/ 旧路径重写,存量 settings.xml 无需改动)
5. 冒烟:mvn 拉包 + mvn deploy 一个测试版本(发包走 kkRepo)
6. 观察期(建议 1~2 周)后下线旧 Nexus
```

**回退**:域名指回旧 Nexus 即可——整个迁移过程对旧 Nexus 只读,它始终是完整可用的回退点。注意回退后旧 Nexus 上没有的,只有回退窗口内写到 kkRepo 的新包。

## 8. 常见问题

| 现象 | 处理 |
| --- | --- |
| PUT 返回 401/403 | deploy 账号密码错误,或该账号没有目标仓库写权限 |
| PUT 返回 404 | `--target-repo` 拼错,或该 hosted 仓库没建 |
| 源端 404(爬取阶段) | 索引页偶发超时,重跑即可;持续 404 检查 `--source-repo` 拼写 |
| 大文件 PUT 超时 | 加大 `--timeout`(默认 120s);Nginx 侧确认 `client_max_body_size 0`、`proxy_send_timeout` 足够 |
| 迁移中断了怎么办 | 原命令直接重跑,state 文件保证续传;失败清单在 `[done] failed=` 里,也可查 state JSON 的 `failed` 字段 |
| SNAPSHOT 的 `maven-metadata.xml` | 脚本按原样上传;kkRepo Maven 协议对 snapshot 元数据有合并逻辑,真实 `mvn` 拉取验证为准 |
| 想排除 checksum 文件不上传 | 脚本默认全量原样迁移(最保守);确认 kkRepo 会服务端生成 checksum 后再改脚本,否则 Maven 客户端校验会失败 |
| 旧客户端走 `/content/repositories/...` 报 404 | Nginx 未配置重写,见部署手册第 6 节 |
