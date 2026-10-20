#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_nexus2.py — Nexus OSS 2.x Maven 制品 → kkRepo 迁移脚本

原理:Nexus 2 的目录索引(HTML)匿名可读,脚本广度优先爬取
/content/repositories/<repo>/ 下的全部文件路径,逐个 GET 后以
Basic 认证 PUT 到 kkRepo 的 Maven hosted 仓库(/repository/<repo>/<path>)。

特性:
- 断点续传:进度记录在 state 文件,中断后重跑自动跳过已完成路径;
- 增量模式 --incremental:先 HEAD 源/目标比对大小,一致则跳过(用于切换日补量);
- maven-metadata.xml 最后上传,避免覆盖 kkRepo 端的合并结果;
- 校验文件(*.md5/*.sha1/...)默认原样上传,与源端逐字节一致;
- --plan-only 只爬取并生成清单,不上传。

用法示例:
  python3 migrate_nexus2.py plan \
    --source http://maven.dev.i.mored.tech --source-repo releases \
    --state state-releases.json
  python3 migrate_nexus2.py push \
    --source http://maven.dev.i.mored.tech --source-repo releases \
    --target http://127.0.0.1:19090 --target-repo releases \
    --user admin --password 'xxx' \
    --state state-releases.json --workers 8
"""
import argparse
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

A_RE = re.compile(r'href="([^"]+)"')
CHUNK = 1 << 20


def http(url, method="GET", data=None, headers=None, auth=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_text(url, timeout=30):
    try:
        with http(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def norm_link(link, cur, root):
    """把索引页里的链接规整为仓库内相对路径;目录以 / 结尾,文件不带 /。"""
    if link in ("../", "..", "/", "#"):
        return None
    if link.startswith(("http://", "https://", "/")) and not link.startswith(root):
        return None
    rel = link[len(root):] if link.startswith(root) else cur + link
    return rel[1:] if rel.startswith("/") else rel


def crawl(source, source_repo, workers=8):
    root = f"{source}/content/repositories/{source_repo}/"
    pending, seen, files = ["/"], {"/"}, []
    while pending:
        batch, pending = pending[: workers * 4], pending[workers * 4 :]
        with ThreadPoolExecutor(workers) as ex:
            pages = list(ex.map(lambda p: fetch_text(root + p), batch))
        for path, html in zip(batch, pages):
            for link in A_RE.findall(html or ""):
                rel = norm_link(link, path, root)
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                if rel.endswith("/"):
                    pending.append(rel)
                else:
                    files.append(rel)
    return files


def content_type(path):
    if path.endswith((".pom", ".xml")):
        return "application/xml"
    return "application/octet-stream"


def put_one(path, args, state, lock):
    src = f"{args.source}/content/repositories/{args.source_repo}/{path}"
    dst = f"{args.target}/repository/{args.target_repo}/{path}"

    if args.incremental:
        try:
            with http(src, method="HEAD", timeout=args.timeout) as r:
                src_len = r.headers.get("Content-Length")
            with http(dst, method="HEAD", auth=args.auth, timeout=args.timeout) as r:
                dst_len = r.headers.get("Content-Length")
            if src_len is not None and src_len == dst_len:
                with lock:
                    state["done"][path] = int(src_len)
                return None
        except Exception:
            pass  # HEAD 失败就走完整 GET+PUT

    tmp = None
    last_err = None
    for attempt in range(1, args.retries + 1):
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tmp = tf.name
                with http(src, timeout=args.timeout) as r:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        tf.write(chunk)
            size = os.path.getsize(tmp)
            with open(tmp, "rb") as f:
                with http(dst, method="PUT", data=f, auth=args.auth,
                          headers={"Content-Type": content_type(path),
                                   "Content-Length": str(size)},
                          timeout=args.timeout) as r:
                    if not (200 <= r.status < 300):
                        raise RuntimeError(f"PUT HTTP {r.status}")
            os.unlink(tmp)
            tmp = None
            with lock:
                state["done"][path] = size
                state["failed"].pop(path, None)
            return None
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
                tmp = None
            if attempt < args.retries:
                time.sleep(min(30, 2 ** attempt))
    with lock:
        state["failed"][path] = last_err
    return path


def save_state(state, path, lock=None):
    # 工作线程会并发修改 done/failed,序列化前必须在锁内做快照,
    # 否则 json.dump 迭代期间字典变大直接崩(dictionary changed size)。
    def snapshot(s):
        return {k: dict(v) if isinstance(v, dict) else v for k, v in s.items()}

    if lock is not None:
        with lock:
            data = snapshot(state)
    else:
        data = snapshot(state)
    atomic = path + ".tmp"
    with open(atomic, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(atomic, path)


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("done", {})
    state.setdefault("failed", {})
    return state


def main():
    ap = argparse.ArgumentParser(description="Nexus OSS 2.x -> kkRepo Maven migration")
    ap.add_argument("command", choices=["plan", "push"])
    ap.add_argument("--source", required=True, help="旧 Nexus 2 根地址,如 http://maven.dev.i.mored.tech")
    ap.add_argument("--source-repo", required=True)
    ap.add_argument("--target", help="kkRepo 根地址,如 http://127.0.0.1:19090")
    ap.add_argument("--target-repo", help="kkRepo 侧 Maven hosted 仓库 ID")
    ap.add_argument("--user"); ap.add_argument("--password")
    ap.add_argument("--state", required=True, help="进度/清单 JSON 文件")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--incremental", action="store_true", help="HEAD 比对大小,一致跳过")
    ap.add_argument("--plan-only", action="store_true", help="push 命令只刷新清单不上传")
    args = ap.parse_args()
    if args.user:
        args.auth = (args.user, args.password or "")
    else:
        args.auth = None

    files = crawl(args.source, args.source_repo, args.workers)
    meta = [f for f in files if f.rsplit("/", 1)[-1] == "maven-metadata.xml"]
    body = [f for f in files if f not in set(meta)]
    print(f"[plan] {args.source_repo}: files={len(files)} "
          f"(artifacts+checksums={len(body)}, maven-metadata.xml={len(meta)})")

    if args.command == "plan":
        save_state(load_state(args.state) | {"plan": files}, args.state)
        return 0

    if not args.target or not args.target_repo:
        ap.error("push 需要 --target 与 --target-repo")
    if args.plan_only:
        return 0

    state = load_state(args.state)
    lock = threading.Lock()
    done_before = len(state["done"])
    skipped = sum(1 for p in body if p in state["done"])

    todo = [p for p in body if p not in state["done"]]
    print(f"[push] todo={len(todo)} skipped(done)={skipped} failed_before={len(state['failed'])}")
    fails = []
    with ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(put_one, p, args, state, lock): p for p in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                fails.append(r)
            if i % 200 == 0:
                save_state(state, args.state, lock)
                print(f"  ... {i}/{len(todo)} (failed={len(fails)})", flush=True)

    # maven-metadata.xml 最后传
    todo_meta = [p for p in meta if p not in state["done"]]
    with ThreadPoolExecutor(min(4, args.workers)) as ex:
        list(ex.map(lambda p: put_one(p, args, state, lock), todo_meta))

    save_state(state, args.state, lock)
    print(f"[done] newly_done={len(state['done']) - done_before} "
          f"(uploads+skips) failed={len(state['failed'])} total_done={len(state['done'])}")
    if state["failed"]:
        for p in list(state["failed"])[:20]:
            print(f"  FAILED {p}: {state['failed'][p]}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
