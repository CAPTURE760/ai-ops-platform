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

# 4. 生成生产环境 nginx 配置
echo "[5/6] 生成 nginx 配置..."
cat > docker/nginx.conf << NGINX_EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Frontend static files
    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    # API proxy
    location /api/ {
        proxy_pass http://backend:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 10s;
    }

    # Certbot webroot challenge
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
}
NGINX_EOF

# 5. 启动服务 (先用 HTTP)
echo "[6/6] 启动服务..."
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
cat > docker/nginx.conf << NGINX_SSL_EOF
# HTTP -> HTTPS redirect
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    # Certbot webroot challenge
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

# HTTPS server
server {
    listen 443 ssl;
    server_name $DOMAIN www.$DOMAIN;

    # SSL certificates
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    # SSL security settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Frontend static files
    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    # API proxy
    location /api/ {
        proxy_pass http://backend:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 10s;
    }
}
NGINX_SSL_EOF

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
