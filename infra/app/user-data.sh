#!/bin/bash
# First-boot setup for the app server. Terraform renders this file: the
# $${...} placeholders below are filled in from server.tf. Output is logged
# to /var/log/app-setup.log. The app itself arrives later, with a deploy.
set -euo pipefail
exec > >(tee -a /var/log/app-setup.log) 2>&1
echo "setup started $(date -Is)"

# --- 1. The data volume ------------------------------------------------------------
# Terraform attaches it just after the instance starts, so wait for it.
device="${data_device}"
for _ in $(seq 1 60); do
  [ -e "$device" ] && break
  sleep 5
done
[ -e "$device" ] || { echo "data volume never appeared"; exit 1; }

# Format it only if it's new: a replacement instance finds the old data.
if ! blkid "$device" >/dev/null; then
  mkfs.ext4 -L app-data "$device"
fi
mkdir -p "${data_dir}"
if ! grep -q "LABEL=app-data" /etc/fstab; then
  echo "LABEL=app-data ${data_dir} ext4 defaults,nofail 0 2" >> /etc/fstab
fi
mount -a
mkdir -p "${data_dir}/pgdata" "${data_dir}/backups" "${data_dir}/app"

# --- 2. Swap --------------------------------------------------------------------------
# 2 GiB of RAM is enough for the stack; a little swap rides out spikes.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  swapon /swapfile
fi

# --- 3. Docker, from Docker's apt repository -------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
  > /etc/apt/sources.list.d/docker.list
# Cap container logs so they can't fill the disk.
mkdir -p /etc/docker
echo '{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "5" } }' \
  > /etc/docker/daemon.json
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# --- 4. AWS CLI: ECR login, settings from SSM, backups to S3 ----------------------
snap install aws-cli --classic

echo "setup finished $(date -Is)"
