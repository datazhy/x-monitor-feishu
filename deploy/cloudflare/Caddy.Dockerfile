# 带 Cloudflare DNS 插件的 Caddy（标准镜像不含，需自构建）。
# DNS-01 验证无需占用 80/443，适配本服务器端口被 nginx/xray 占满的情况。
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
