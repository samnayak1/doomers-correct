#!/usr/bin/env bash
# Add 2 GiB of swap. Run once, as root, before the first deploy.
#
# A 1 GiB instance has no headroom for a pip install or a pandas spike; without
# swap the OOM killer takes whatever is largest, which is usually sshd or the
# container you were mid-way through building.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }

if swapon --show | grep -q .; then
  echo "Swap already active:"; swapon --show; exit 0
fi

fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# A small box benefits from swapping early rather than OOMing late.
sysctl -w vm.swappiness=20 >/dev/null
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=20' >> /etc/sysctl.conf

echo "Swap enabled:"; free -h
