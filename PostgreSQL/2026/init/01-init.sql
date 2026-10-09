-- 01-init.sql
-- 仅在数据目录为空、【首次初始化】时执行(按文件名排序),此后不再执行。
-- compose 的 command 已 preload pg_stat_statements,这里在业务库中启用它。

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- (可选)监控专用低权限账号(prometheus postgres-exporter 用),取消注释并改密码:
-- CREATE ROLE monitor LOGIN PASSWORD '改成强密码';
-- GRANT pg_monitor TO monitor;
