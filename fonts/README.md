# 授权字体目录

把部署方已获授权的 `.ttf`、`.ttc` 或 `.otf` 字体放在此目录，或在 `.env` 中将
`DC_FONTS_DIR` 指向服务器上的专用字体目录。Docker Compose 会把它只读挂载到
容器 `/usr/local/share/fonts/authorized`。

不要把受授权限制的字体文件提交到 Git 仓库，也不要把它们复制进 Docker 镜像。
