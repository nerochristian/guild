# DigitalOcean Droplet deployment

This deploys the Discord bot and its built-in website as one systemd service.
Nginx exposes the website at `http://DROPLET_IP` and proxies it to port `8080`.

## 1. Create and secure the Droplet

Create an Ubuntu 24.04 LTS Droplet with an SSH key. A Basic Droplet with at
least 1 GiB RAM is the practical minimum; use 2 GiB if image generation or
other memory-heavy commands run on the bot.

Connect as root, then run:

```bash
apt update
apt install -y git nginx python3 python3-venv python3-pip curl ufw
adduser --disabled-password --gecos "" soul
usermod -aG sudo soul

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

Keep port `8080` closed publicly. Nginx reaches it through localhost.

## 2. Give the Droplet repository access

Create a dedicated SSH key as the `soul` user:

```bash
sudo -iu soul
ssh-keygen -t ed25519 -C "soul-droplet" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

In GitHub, open the repository's **Settings > Deploy keys**, add the displayed
public key, and leave write access disabled. Then configure SSH and clone:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts

mkdir -p /opt/soul
git clone --branch guild git@github.com:nerochristian/guild.git /opt/soul/guild
cd /opt/soul/guild
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
exit
```

## 3. Configure secrets

Create `/opt/soul/guild/.env` on the Droplet. Do not commit it. Start with the
same values used locally and set these deployment-specific values:

```dotenv
PORT=8080
PUBLIC_BASE_URL=http://DROPLET_IP
```

Replace `DROPLET_IP` with the Droplet's public IPv4 address. The existing
`DISCORD_BOT_TOKEN`, `DATABASE_URL`, API keys, and Discord IDs must also be in
this file. Lock it down:

```bash
chown soul:soul /opt/soul/guild/.env
chmod 600 /opt/soul/guild/.env
```

If the database is currently local to the Windows machine, replace
`DATABASE_URL` with a PostgreSQL database reachable from the Droplet.

## 4. Install systemd and Nginx

Run as root:

```bash
cp /opt/soul/guild/deploy/soul-bot.service /etc/systemd/system/
cp /opt/soul/guild/deploy/nginx-soul.conf /etc/nginx/sites-available/soul
ln -sfn /etc/nginx/sites-available/soul /etc/nginx/sites-enabled/soul
rm -f /etc/nginx/sites-enabled/default

cat >/etc/sudoers.d/soul-deploy <<'EOF'
soul ALL=(root) NOPASSWD: /usr/bin/systemctl restart soul-bot.service
EOF
chmod 440 /etc/sudoers.d/soul-deploy
visudo -cf /etc/sudoers.d/soul-deploy

systemctl daemon-reload
systemctl enable --now soul-bot.service
nginx -t
systemctl enable --now nginx
```

Verify:

```bash
systemctl status soul-bot.service --no-pager
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://DROPLET_IP/healthz
```

## 5. Enable automatic GitHub deployments

Generate a second key on your trusted computer. This key lets GitHub Actions
log in to the Droplet as `soul`:

```bash
ssh-keygen -t ed25519 -C "github-actions-soul" -f soul_actions -N ""
```

Append `soul_actions.pub` to `/home/soul/.ssh/authorized_keys` on the Droplet.
In the GitHub repository, create a `production` environment and add these
environment secrets:

| Secret | Value |
| --- | --- |
| `DROPLET_HOST` | Droplet public IPv4 address |
| `DROPLET_USER` | `soul` |
| `DROPLET_SSH_KEY` | Complete contents of the private `soul_actions` key |
| `DROPLET_KNOWN_HOSTS` | Output of `ssh-keyscan DROPLET_IP` from a trusted computer |

Each push to the `guild` branch now runs `.github/workflows/deploy-droplet.yml`.
The Droplet pulls with fast-forward-only semantics, installs dependencies,
compiles the Python tree, restarts the service, and rolls back if `/healthz`
does not recover within 40 seconds.

## Operations

```bash
# Follow application logs
journalctl -u soul-bot.service -f

# Run a deployment manually on the Droplet
sudo -iu soul /opt/soul/guild/scripts/deploy.sh

# Validate Nginx
nginx -t
```

An IP-only site can use HTTP, but Discord setup links and credentials should
ultimately use HTTPS. Add a domain pointing to the Droplet, change
`PUBLIC_BASE_URL` to that HTTPS URL, and install a TLS certificate before
exposing the setup portal broadly.
