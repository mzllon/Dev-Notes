# RustFS 部署(docker-compose)、域名和Nginx的配置

[RustFS](https://rustfs.com) 是 Rust 编写的高性能 S3 兼容对象存储(无 GC 抖动)。

## 1. docker-compose 部署

目录约定(按需调整):`/data/rustfs/{data,logs}`。容器以非 root 用户(UID 10001)运行,数据目录需先授权。

`docker-compose.yml`:

```yaml
services:
  # 数据目录授权(容器内 rustfs 用户 UID=10001)
  rustfs-perms:
    image: alpine
    user: root
    volumes:
      - ./data:/fix-dest
    command: chown -R 10001:10001 /fix-dest

  rustfs:
    image: rustfs/rustfs:latest
    container_name: rustfs-server
    depends_on:
      rustfs-perms:
        condition: service_completed_successfully
    ports:
      - "9010:9000"   # S3 API
      - "9011:9001"   # 管理控制台
    environment:
      # API 和控制台监听地址
      - RUSTFS_ADDRESS: "0.0.0.0:9000"
      - RUSTFS_CONSOLE_ADDRESS: "0.0.0.0:9001"
      
      # CORS 设置，控制台与 S3 API 都放开来源
      - RUSTFS_CORS_ALLOWED_ORIGINS=*
      - RUSTFS_CONSOLE_CORS_ALLOWED_ORIGINS=*
      
      # 访问密钥（生产环境请修改为强密码）
      - RUSTFS_ACCESS_KEY: 改为自己的
      - RUSTFS_SECRET_KEY: 改为自己的
      
      # 启用控制台
      - RUSTFS_CONSOLE_ENABLE: "true"
      
      # 日志级别
      - RUSTFS_OBS_LOGGER_LEVEL=info
      
      # 虚拟主机风格访问的主域名（与客户端 Host 头完全一致）
      - RUSTFS_SERVER_DOMAINS=b-prod-rustfs-s3.mored.tech
    volumes:
      # 存储数据卷
      - ./data:/data
      # 日志目录
      - ./logs:/app/logs
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "curl --fail http://localhost:9000/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 10s
```

> 生产勿用默认 `rustfsadmin`。镜像 tag 固定(勿用 latest)。

```bash
export RUSTFS_ACCESS_KEY=mored-admin RUSTFS_SECRET_KEY=<强随机>
mkdir -p /data/rustfs/data
docker compose up -d
curl --fail http://127.0.0.1:9000/health   # 健康
```

## 2. 创建 bucket

控制台:`http://<宿主机>:9011` 登录(上面配置的 AK/SK)→ 创建桶(如 `files`)。

或 aws-cli:

```bash
aws configure set default.ca_bundle ""   # 视环境
aws --endpoint-url http://127.0.0.1:9010 s3 mb s3://mored-files
aws --endpoint-url http://127.0.0.1:9010 s3 ls
```

## 3. 域名解析(阿里云 DNS)

RustFS 不支持按桶绑定域名,采用"全局主域名 + 通配符 DNS"的虚拟主机风格,每个桶自动获得 `<桶名>.b-prod-rustfs-s3.mored.tech` 地址(主域名已在 compose 的 `RUSTFS_SERVER_DOMAINS` 声明)。

阿里云 DNS 加一条 A 记录即可,**新增桶无需再加 DNS**:

| 项目 | 值 |
|---|---|
| 记录类型 | A |
| 主机记录 | `*.b-prod-rustfs-s3` |
| 记录值 | 服务器公网 IP |

- 桶名由 Host 头解析,`RUSTFS_SERVER_DOMAINS` 须与请求 Host 一致(带端口须匹配;实测不带端口时带 `:9010` 的请求也能识别)
- 内网走路径风格时桶名在路径第一段:`http://<IP>:9010/<桶>/<key>`,两种风格互不影响
- 桶名不要带点:多级子域单域名证书罩不住,通配符证书同样罩不住
- `/etc/hosts` 不支持通配符,别用它做桶域名解析

## 4. Nginx 反向代理(1Panel)

证书:无通配符证书,**每个桶域名一张独立单域名证书**(阿里云免费证书,3 个月有效期,不自动续签),每个桶在 1Panel 建一个网站。

站点反向代理的核心配置(`dev-rfs-berich` 换成桶名,证书路径换成实际路径):

```nginx
location ^~ / {
    # 预检请求:RustFS 不应答预检,代理层直接返回
    if ($request_method = OPTIONS) {
        add_header Access-Control-Allow-Origin $http_origin always;
        add_header Access-Control-Allow-Methods "GET, PUT, POST, DELETE, HEAD" always;
        # 规范中 * 不覆盖 Authorization,带凭证请求必须显式列出
        add_header Access-Control-Allow-Headers "Authorization, Content-Type, Content-MD5, x-amz-date, x-amz-content-sha256, x-amz-security-token" always;
        add_header Access-Control-Max-Age "3000" always;
        return 204;
    }

    # 跨域头统一由代理层输出:先隐藏上游的(避免 GET 响应双份),再全量补齐
    proxy_hide_header Access-Control-Allow-Origin;
    proxy_hide_header Access-Control-Allow-Methods;
    proxy_hide_header Access-Control-Allow-Headers;
    proxy_hide_header Access-Control-Expose-Headers;
    proxy_hide_header Access-Control-Max-Age;
    add_header Access-Control-Allow-Origin $http_origin always;
    add_header Access-Control-Allow-Methods "GET, PUT, POST, DELETE, HEAD" always;
    add_header Access-Control-Allow-Headers "Authorization, Content-Type, Content-MD5, x-amz-date, x-amz-content-sha256, x-amz-security-token" always;
    add_header Access-Control-Expose-Headers "ETag" always;

    proxy_pass http://<RustFS内网IP>:9010;
    proxy_set_header Host $host;                    # 关键:RustFS 靠 Host 解析桶名,必须原样透传
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    client_max_body_size 0;          # 上传不限大小
    proxy_request_buffering off;     # 上传流式转发,不在代理落盘
    proxy_buffering off;             # 下载不缓冲
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

### 为什么 CORS 必须由代理层接管(踩坑)

RustFS(v1.0.1 实测)的桶级 CORS 实现不完整:

- 桶级 CORS 规则**只对 GET 响应生效,PUT 响应不返回任何跨域头**
- **OPTIONS 预检**返回 200 但不带任何 `Access-Control-*` 头

结果:文件实际已上传成功,浏览器因响应缺头判定失败报 CORS error(且诱导重试产生重复文件)。CORS 是浏览器专属机制,curl/SDK 永远"正常",排障必须带 Origin 头检查响应。桶级 CORS 可配可不配——配了也不冲突(被 `proxy_hide_header` 隐藏后统一输出)。

### 验证(三连测)

```bash
# ① 预检:应返回 204 + 4 个 Access-Control-* 头
curl -sk -X OPTIONS "https://<桶>.b-prod-rustfs-s3.mored.tech/x.txt" \
  -H "Origin: http://localhost:5173" \
  -H "Access-Control-Request-Method: PUT" \
  -H "Access-Control-Request-Headers: content-type" -D - -o /dev/null

# ② 匿名读公开桶:200 或 404 NoSuchKey;404 NoSuchBucket = 域名/解析没通
curl -sk "https://<桶>.b-prod-rustfs-s3.mored.tech/<对象key>" -o /dev/null -w "%{http_code}\n"

# ③ 匿名写必须 403
curl -sk -X PUT --data-binary "x" "https://<桶>.b-prod-rustfs-s3.mored.tech/__t.txt" -o /dev/null -w "%{http_code}\n"
```

> 应用侧密钥建议收窄为单桶数据读写(`s3:*` 连改 CORS、删桶都放行);子访问密钥天然登不了控制台(只认部署主密钥)。浏览器直传用预签名 URL(后端签,前端 PUT),SDK endpoint 用 `https://<桶>.b-prod-rustfs-s3.mored.tech`,region 填 `us-east-1`。


