# 密码文件放这里(容器内以 /run/secrets/pg_password 挂载),生成方式:
#   openssl rand -hex 24 | tr -d '\n' > secrets/pg_password.txt
#   chmod 600 secrets/pg_password.txt
# 本目录内容已被 .gitignore 排除,严禁提交到版本库。
