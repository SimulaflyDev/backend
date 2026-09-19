#!/bin/bash

# ==============================================================================
# Simulafly Backend Production Deployment Script
# Target OS: Ubuntu 20.04 / 22.04 / 24.04 LTS (Azure VM)
# Domain: api.simulatech.org
# SSL: Let's Encrypt (Certbot)
# ==============================================================================

# Exit immediately if a command exits with a non-zero status
set -e

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Indicators
INFO="💡 [INFO]"
SUCCESS="✅ [SUCCESS]"
WARNING="⚠️  [WARNING]"
ERROR="❌ [ERROR]"
PROGRESS="⚡ [PROGRESS]"

# Configuration variables
DOMAIN="api.simulatech.org"
EMAIL="admin@simulatech.org" # Email for Let's Encrypt certificate renewal alerts
APP_DIR="/var/www/simulafly-backend"
SERVICE_NAME="simulafly-backend"
PORT=8000

# Get the actual user who invoked sudo (if applicable)
RUNNING_USER=${SUDO_USER:-$(whoami)}
RUNNING_GROUP=$(id -gn "$RUNNING_USER")

echo -e "${CYAN}======================================================================${NC}"
echo -e "${BLUE}          🚀 SIMULAFLY BACKEND DEPLOYMENT SCRIPT FOR AZURE UBUNTU 🚀   ${NC}"
echo -e "${CYAN}======================================================================${NC}"
echo -e "${INFO} Target Domain:  ${YELLOW}https://${DOMAIN}${NC}"
echo -e "${INFO} Running User:   ${YELLOW}${RUNNING_USER}${NC}"
echo -e "${INFO} Target Folder:  ${YELLOW}${APP_DIR}${NC}"
echo -e "${CYAN}----------------------------------------------------------------------${NC}"

# Ensure script is run with root privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${ERROR} ${RED}This script must be run with sudo or as root.${NC}"
  echo -e "Please execute as: ${YELLOW}sudo ./deploy.sh${NC}"
  exit 1
fi

# Step 1: Update System Packages & Install Dependencies
echo -e "\n${PROGRESS} ${CYAN}Step 1: Installing system updates and packages...${NC}"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv python3-dev build-essential libpq-dev git nginx certbot python3-certbot-nginx rsync ufw

echo -e "${SUCCESS} ${GREEN}System packages installed successfully!${NC}"

# Step 2: Set up Target Directory & Get App Files (Sync or Clone)
echo -e "\n${PROGRESS} ${CYAN}Step 2: Preparing application directory in ${APP_DIR}...${NC}"
mkdir -p "$APP_DIR"
chown -R "$RUNNING_USER:$RUNNING_GROUP" "$APP_DIR"

REPO_URL="https://github.com/SimulaflyDev/backend"

# Detect if the current directory is a local checkout of the application
if [ -f "./requirements.txt" ] && [ -d "./app" ]; then
    echo -e "${INFO} ${GREEN}Local application files detected. Syncing to ${APP_DIR} using rsync...${NC}"
    rsync -av \
        --exclude='venv' \
        --exclude='.git' \
        --exclude='.env' \
        --exclude='.claude' \
        --exclude='.pytest_cache' \
        --exclude='__pycache__' \
        --exclude='deploy.sh' \
        ./ "$APP_DIR/"
else
    echo -e "${INFO} ${YELLOW}No local application files found. Cloning repository from GitHub...${NC}"
    # If the target directory already contains a git repo, just pull the latest changes
    if [ -d "$APP_DIR/.git" ]; then
        echo -e "${INFO} ${YELLOW}Existing Git repository found in ${APP_DIR}. Pulling latest changes...${NC}"
        cd "$APP_DIR"
        sudo -u "$RUNNING_USER" git fetch --all
        sudo -u "$RUNNING_USER" git reset --hard origin/main || sudo -u "$RUNNING_USER" git reset --hard origin/master
        cd - > /dev/null
    else
        # Otherwise clone the repository directly
        echo -e "${INFO} Cloning ${REPO_URL} into ${APP_DIR}...${NC}"
        # Clear out files in the app directory to prevent git clone collision
        find "$APP_DIR" -mindepth 1 -delete
        sudo -u "$RUNNING_USER" git clone "$REPO_URL" "$APP_DIR"
    fi
fi

# Ensure correct permissions
chown -R "$RUNNING_USER:$RUNNING_GROUP" "$APP_DIR"
echo -e "${SUCCESS} ${GREEN}Application files prepared and permissions configured for '${RUNNING_USER}'.${NC}"

# Step 3: Set up Python Virtual Environment
echo -e "\n${PROGRESS} ${CYAN}Step 3: Creating Python virtual environment & installing dependencies...${NC}"

# Check if venv directory exists but is broken or empty (e.g., cloned from Git by accident)
if [ -d "$APP_DIR/venv" ] && [ ! -f "$APP_DIR/venv/bin/pip" ]; then
    echo -e "${WARNING} ${YELLOW}Found a broken or empty 'venv' directory. Deleting and recreating...${NC}"
    rm -rf "$APP_DIR/venv"
fi

if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$RUNNING_USER" python3 -m venv "$APP_DIR/venv"
    echo -e "${SUCCESS} ${GREEN}Virtual environment created.${NC}"
fi

echo -e "${INFO} Upgrading pip & installing dependencies... (this might take a few moments)"
sudo -u "$RUNNING_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$RUNNING_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Install gunicorn inside the virtual environment for process management in production
echo -e "${INFO} Installing Gunicorn inside venv..."
sudo -u "$RUNNING_USER" "$APP_DIR/venv/bin/pip" install gunicorn

echo -e "${SUCCESS} ${GREEN}Dependencies installed successfully!${NC}"

# Step 4: Configure environment variables (.env)
echo -e "\n${PROGRESS} ${CYAN}Step 4: Configuring application environment variables...${NC}"
ENV_FILE="$APP_DIR/.env"

if [ -f "./.env" ] && [ ! -f "$ENV_FILE" ]; then
    echo -e "${INFO} ${GREEN}Found a local .env file. Copying it to production directory...${NC}"
    sudo -u "$RUNNING_USER" cp "./.env" "$ENV_FILE"
elif [ ! -f "$ENV_FILE" ]; then
    echo -e "${INFO} ${YELLOW}No existing .env found. Generating from example template...${NC}"
    sudo -u "$RUNNING_USER" cp "$APP_DIR/.env.example" "$ENV_FILE"
    
    # Generate secure random cryptographic secrets for production
    RAND_SECRET=$(openssl rand -hex 32)
    RAND_ADMIN=$(openssl rand -hex 32)
    
    # Securely update keys in .env
    sed -i "s/SECRET_KEY=change-me-openssl-rand-hex-32/SECRET_KEY=$RAND_SECRET/g" "$ENV_FILE"
    sed -i "s/ADMIN_API_KEY=/ADMIN_API_KEY=$RAND_ADMIN/g" "$ENV_FILE"
    sed -i "s/ENV=development/ENV=production/g" "$ENV_FILE"
    
    echo -e "${SUCCESS} ${GREEN}Generated secure SECRET_KEY and ADMIN_API_KEY in production .env file!${NC}"
    echo -e "${WARNING} ${YELLOW}Please open and update your actual production variables (e.g. DATABASE_URL, Azure keys) in: ${NC}"
    echo -e "         ${CYAN}nano $ENV_FILE${NC}"
else
    echo -e "${INFO} ${GREEN}Existing production .env file found in $APP_DIR. Keeping it unchanged.${NC}"
fi

# Step 5: Configure Nginx Reverse Proxy
echo -e "\n${PROGRESS} ${CYAN}Step 5: Configuring Nginx reverse proxy...${NC}"
NGINX_CONF="/etc/nginx/sites-available/$DOMAIN"

cat <<EOF > "$NGINX_CONF"
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    # Match maximum request body size with your FastAPI configurations
    client_max_body_size 10M;

    # Performance optimization: Serve style static images directly via Nginx
    location /static/styles/ {
        alias $APP_DIR/data/style_templates/images/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    # Reverse proxy backend requests to Gunicorn/FastAPI
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_cache_bypass \$http_upgrade;
        
        # IP and Protocol header propagation
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        # Standard production timeouts
        proxy_connect_timeout 60s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
EOF

# Enable configuration by creating a symlink
ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/"

# Disable default Nginx server block to avoid domain collision
if [ -f "/etc/nginx/sites-enabled/default" ]; then
    echo -e "${INFO} Disabling default Nginx site configuration..."
    rm -f "/etc/nginx/sites-enabled/default"
fi

# Test Nginx and reload
nginx -t
systemctl restart nginx
echo -e "${SUCCESS} ${GREEN}Nginx configured and reloaded successfully!${NC}"

# Step 6: Configure Firewall (UFW)
echo -e "\n${PROGRESS} ${CYAN}Step 6: Setting up UFW firewall rules...${NC}"
ufw allow OpenSSH
ufw allow 'Nginx Full'
# Enable UFW non-interactively
ufw --force enable
echo -e "${SUCCESS} ${GREEN}Firewall is active and allowing SSH, HTTP, and HTTPS ports.${NC}"

# Step 7: Create and Configure Systemd Service
echo -e "\n${PROGRESS} ${CYAN}Step 7: Creating Systemd daemon service for FastAPI...${NC}"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Simulafly FastAPI Backend Service
After=network.target

[Service]
User=$RUNNING_USER
Group=$RUNNING_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:$PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd, enable and start service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo -e "${SUCCESS} ${GREEN}Systemd service '$SERVICE_NAME' created and enabled.${NC}"

# Step 8: Run Alembic Database Migrations (required before starting code)
echo -e "\n${PROGRESS} ${CYAN}Step 8: Running database migrations...${NC}"
DB_URL=$(grep -E "^DATABASE_URL=" "$ENV_FILE" | cut -d'=' -f2- || true)

if [[ -z "$DB_URL" || "$DB_URL" == *"localhost"* || "$DB_URL" == *"change-me"* ]]; then
    echo -e "${ERROR} ${RED}Production database is not configured; refusing to start code against an unknown schema.${NC}"
    exit 1
else
    echo -e "${INFO} DATABASE_URL detected, running migrations..."
    if sudo -u "$RUNNING_USER" ENV=production "$APP_DIR/venv/bin/alembic" upgrade head; then
        echo -e "${SUCCESS} ${GREEN}Database schema updated to the latest revision successfully!${NC}"
    else
        echo -e "${ERROR} ${RED}Database migration failed; the service was not restarted with incompatible code.${NC}"
        echo -e "${INFO} Verify your DATABASE_URL in $ENV_FILE, open firewall ports to your Azure Postgres/Neon instance if needed, and run migrations manually."
        exit 1
    fi
fi

# Step 9: Start the Service
echo -e "\n${PROGRESS} ${CYAN}Step 9: Starting the application service...${NC}"
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${SUCCESS} ${GREEN}Simulafly backend service is now running!${NC}"
else
    echo -e "${WARNING} ${YELLOW}Service started but is not reporting as fully active.${NC}"
    echo -e "${INFO} This is normal if your Database connection string is not configured yet."
    echo -e "${INFO} View active service logs using: ${CYAN}journalctl -u $SERVICE_NAME -n 50 --no-pager${NC}"
fi

# Step 10: Configure Let's Encrypt SSL (Certbot)
echo -e "\n${PROGRESS} ${CYAN}Step 10: Fetching Let's Encrypt SSL certificate...${NC}"
echo -e "${INFO} Checking if DNS is propagated for ${DOMAIN}..."

# We will try to fetch the cert, but fail gracefully if DNS is not yet pointed to the VM
if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --no-eff-email --redirect; then
    echo -e "${SUCCESS} ${GREEN}SSL certificate successfully obtained and configured!${NC}"
    echo -e "${SUCCESS} ${GREEN}Your backend is now live with SSL at: ${YELLOW}https://${DOMAIN}${NC}"
else
    echo -e "${WARNING} ${YELLOW}Certbot could not obtain a certificate at this time.${NC}"
    echo -e "${INFO} If this is a fresh server, make sure your A/AAAA records for ${YELLOW}${DOMAIN}${NC} point to this server's public IP."
    echo -e "${INFO} Once your DNS resolves to this server, manually trigger SSL setup by running:"
    echo -e "      ${CYAN}sudo certbot --nginx -d $DOMAIN${NC}"
fi

echo -e "\n${CYAN}======================================================================${NC}"
echo -e "${GREEN}                     🎉 DEPLOYMENT COMPLETE 🎉                        ${NC}"
echo -e "${CYAN}======================================================================${NC}"
echo -e "${INFO} Helpful Commands to manage your backend:${NC}"
echo -e "  - View live logs:      ${CYAN}sudo journalctl -u simulafly-backend -f${NC}"
echo -e "  - Restart backend:     ${CYAN}sudo systemctl restart simulafly-backend${NC}"
echo -e "  - Check Nginx status:  ${CYAN}sudo systemctl status nginx${NC}"
echo -e "  - Edit Configuration:  ${CYAN}nano $ENV_FILE${NC}"
echo -e "   ${CYAN}======================================================================${NC}"
