#!/bin/bash
set -e

# 配置信息
DOMAIN="ocapture.xyz"
EMAIL="your-email@example.com"  # 修改为你的邮箱
REPO_URL="git@github.com:CAPTURE760/ai-ops-platform.git"
DEPLOY_DIR="/opt/ai-ops-platform"

echo "=========================================="
echo "  AI Ops Platform 部署脚本"
echo "=========================================="

# 1. 安装 Docker 和 Docker Compose
echo "[1/6] 安装 Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker 安装完成"
else
    echo "Docker 已安装"
fi

echo "[2/6] 安装 Docker Compose..."
if ! command -v docker-compose &> /dev/null; then
    curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    echo "Docker Compose 安装完成"
else
    echo "Docker Compose 已安装"
fi

# 2. 克隆代码
echo "[3/6] 克隆代码..."
if [ -d "$DEPLOY_DIR" ]; then
    cd "$DEPLOY_DIR"
    git pull
else
    git clone "$REPO_URL" "$DEPLOY_DIR"
    cd "$DEPLOY_DIR"
fi

# 3. 创建必要目录
echo "[4/6] 创建必要目录..."
mkdir -p certbot/conf certbot/www logs

# 4. 启动服务 (先用 HTTP)
echo "[5/6] 启动服务..."
docker-compose down 2>/dev/null || true
docker-compose up -d --build

# 等待服务启动
echo "等待服务启动..."
sleep 10

# 5. 申请 SSL 证书
echo "[6/6] 申请 SSL 证书..."

# 使用 certbot docker 申请证书
docker run --rm \
    -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
    -v "$(pwd)/certbot/www:/var/www/certbot" \
    certbot/certbot certonly \
        --webroot \
        --webroot-path=/var/www/certbot \
        --email "$EMAIL" \
        --agree-tos \
        --no-eff-email \
        -d "$DOMAIN" \
        -d "www.$DOMAIN"

# 切换到 SSL 配置
cp docker/nginx-ssl.conf docker/nginx.conf

# 重启 nginx
docker-compose restart frontend

echo ""
echo "=========================================="
echo "  部署完成!"
echo "=========================================="
echo ""
echo "访问地址: https://$DOMAIN"
echo ""
echo "常用命令:"
echo "  查看日志: cd $DEPLOY_DIR && docker-compose logs -f"
echo "  重启服务: cd $DEPLOY_DIR && docker-compose restart"
echo "  停止服务: cd $DEPLOY_DIR && docker-compose down"
echo "  更新部署: cd $DEPLOY_DIR && git pull && docker-compose up -d --build"
echo ""
echo "SSL 证书自动续期已配置，certbot 会自动处理。"
echo ""

# 添加证书自动续期 crontab
(crontab -l 2>/dev/null; echo "0 3 * * * cd $DEPLOY_DIR && docker run --rm -v $(pwd)/certbot/conf:/etc/letsencrypt -v $(pwd)/certbot/www:/var/www/certbot certbot/certbot renew && docker-compose restart frontend") | crontab -
echo "已配置 SSL 证书自动续期 (每天凌晨 3 点检查)"
