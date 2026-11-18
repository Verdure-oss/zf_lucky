# 反向代理配置备份

本目录保存 1Panel OpenResty 上的站点配置副本。
这些文件**不在本仓库生效**，实际位置是：

```
/opt/1panel/apps/openresty/openresty/conf/conf.d/zf.vernon.de5.net.conf
/opt/1panel/apps/openresty/openresty/www/sites/zf.vernon.de5.net/proxy/*.conf
```

备份目的是：服务器重装或 1Panel 重置后，能照着恢复，不必重新推导。

## 文件说明

| 文件 | 对应实际位置 | 作用 |
|---|---|---|
| `site.conf` | `conf.d/zf.vernon.de5.net.conf` | server 块：80 跳 443、证书、日志 |
| `panel-proxy.conf` | `sites/.../proxy/proxy.conf` | 面板反代到 `127.0.0.1:9997` |
| `vnc.conf` | `sites/.../proxy/vnc.conf` | noVNC 反代到 `127.0.0.1:7910` |

## 两个关键点（踩过坑）

### 1. `/vnc-x7k2m9/` 必须指向 7910，不能指向 7900

`7900` 是 Selenium 容器里常驻的 noVNC，**永远在监听**。
若 nginx 直连它，面板的「关闭观看」就形同虚设——开关杀了网关进程，
nginx 绕过网关照样能打开那个**免密**的浏览器画面。

指向 `7910`（可启停的网关）后：关闭时无人监听 → 502，URL 真正失效。

### 2. WebSocket 必须放行 Upgrade

```nginx
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

缺这两行，noVNC 页面能打开但**画面永远黑屏**（WebSocket 连不上）。

另外 `proxy_pass` 末尾的 `/` 不能少，否则 `/vnc-x7k2m9/xxx` 的路径
不会剥离前缀，noVNC 的 `websockify` 端点会错位。

## 恢复步骤

```bash
SITE=/opt/1panel/apps/openresty/openresty/www/sites/zf.vernon.de5.net
cp deploy/nginx/site.conf         /opt/1panel/apps/openresty/openresty/conf/conf.d/zf.vernon.de5.net.conf
cp deploy/nginx/panel-proxy.conf  $SITE/proxy/proxy.conf
cp deploy/nginx/vnc.conf          $SITE/proxy/vnc.conf

docker exec 1Panel-openresty-87SN nginx -t
docker exec 1Panel-openresty-87SN nginx -s reload
```

证书由 acme.sh 管理（Cloudflare DNS-01 自动续期），无需手工恢复：

```bash
~/.acme.sh/acme.sh --issue --dns dns_cf -d zf.vernon.de5.net --keylength ec-256
~/.acme.sh/acme.sh --install-cert -d zf.vernon.de5.net --ecc \
  --key-file       $SITE/ssl/privkey.pem \
  --fullchain-file $SITE/ssl/fullchain.pem \
  --reloadcmd      "docker exec 1Panel-openresty-87SN nginx -s reload"
```
