#!/usr/bin/env bash
set -euo pipefail
install_dir="${RUNNER_TEMP:?}/kf-tools"
mkdir -p "$install_dir"
curl -fsSL https://github.com/Azure/bicep/releases/download/v0.41.2/bicep-linux-x64 -o "$install_dir/bicep"
echo "cfe52532f77d9d183329f7e94a7fc9268a18d7d9dbaa9ec28d632734cdbfd58e  $install_dir/bicep" | sha256sum --check --status
chmod 755 "$install_dir/bicep"
echo "$install_dir" >> "$GITHUB_PATH"
