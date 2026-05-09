# 部署指南

## 前置条件

1. 域名 `ocapture.xyz` 已解析到服务器 IP `121.196.170.9`
2. 服务器已开放 80 和 443 端口
3. 服务器可以 SSH 登录

## DNS 解析配置

在你的域名管理后台添加以下记录：

| 主机记录 | 记录类型 | 记录值 |
|---------|---------|--------|
| @ | A | 121.196.170.9 |
| www | A | 121.196.170.9 |

## 部署步骤

### 1. 上传代码到 GitHub

```bash
# 在本地项目目录执行
git add .
git commit -m "feat: 添加部署配置"
git push origin main
```

### 2. 登录服务器

```bash
ssh root@121.196.170.9
```

### 3. 下载并执行部署脚本

```bash
# 下载项目代码
git clone git@github.com:CAPTURE760/ai-ops-platform.git /opt/ai-ops-platform
cd /opt/ai-ops-platform

# 修改 deploy.sh 中的邮箱地址
sed -i 's/your-email@example.com/your-real-email@example.com/' deploy.sh

# 执行部署
chmod +x deploy.sh
./deploy.sh
```

### 4. 验证部署

部署完成后，访问 https://ocapture.xyz 应该可以看到前端页面。

## 常用命令

```bash
cd /opt/ai-ops-platform

# 查看日志
docker-compose logs -f

# 重启服务
docker-compose restart

# 停止服务
docker-compose down

# 更新部署（拉取最新代码并重新构建）
git pull
docker-compose up -d --build
```

## SSL 证书

- 证书会自动续期，每天凌晨 3 点检查
- 证书存放在 `certbot/conf/` 目录
- 手动续期命令：
  ```bash
  docker run --rm -v $(pwd)/certbot/conf:/etc/letsencrypt -v $(pwd)/certbot/www:/var/www/certbot certbot/certbot renew
  ```

## 故障排查

### 端口被占用
```bash
# 查看 80/443 端口占用
lsof -i :80
lsof -i :443

# 停止占用端口的服务
systemctl stop nginx  # 如果有 nginx 在运行
systemctl stop apache2  # 如果有 apache 在运行
```

### SSL 证书申请失败
1. 确认域名解析已生效：`ping ocapture.xyz`
2. 确认 80 端口可以从外网访问
3. 检查防火墙设置

### Docker 构建失败
```bash
# 清理 Docker 缓存
docker system prune -a

# 重新构建
docker-compose build --no-cache
docker-compose up -d
```
