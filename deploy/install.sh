#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

repository="SchmiedmayerLab/heartwood"
root="${PWD}"
installer_release="__HEARTWOOD_RELEASE_VERSION__"
platform="auto"
bundle=""
checksums=""
dry_run="false"
minimum_free_gib="8"
stage_number=0
stage_count=7

stage() {
  stage_number=$((stage_number + 1))
  printf '\n[%d/%d] %s\n' "${stage_number}" "${stage_count}" "$1"
}

require_command() {
  local command="$1"
  local purpose="$2"
  if ! command -v "${command}" >/dev/null 2>&1; then
    printf '%s is required %s\n' "${command}" "${purpose}" >&2
    exit 69
  fi
}

usage() {
  cat <<'EOF'
Usage: heartwood-installer [options]

  --root PATH          Installation root (default: current directory)
  --platform NAME      auto, carina, or generic
  --bundle PATH        Use a local heartwood-native.tar.gz
  --checksums PATH     SHA256SUMS for a local bundle
  --minimum-free-gib N Required free space in GiB (default: 8)
  --dry-run            Verify and display the installation without changing it
EOF
}

while (($#)); do
  case "$1" in
    --root) root="${2:?missing installation root}"; shift 2 ;;
    --platform) platform="${2:?missing platform}"; shift 2 ;;
    --bundle) bundle="${2:?missing bundle}"; shift 2 ;;
    --checksums) checksums="${2:?missing checksum manifest}"; shift 2 ;;
    --minimum-free-gib) minimum_free_gib="${2:?missing minimum free space}"; shift 2 ;;
    --dry-run) dry_run="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ "${platform}" == "auto" ]]; then
  if [[ -n "${SLURM_CLUSTER_NAME:-}" || -n "${HEARTWOOD_CARINA:-}" || "${HEARTWOOD_PLATFORM:-}" == "carina" ]]; then
    platform="carina"
  else
    platform="generic"
  fi
fi
if [[ "${platform}" != "carina" && "${platform}" != "generic" ]]; then
  echo "unsupported native platform: ${platform}" >&2
  exit 64
fi
if [[ ! "${minimum_free_gib}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--minimum-free-gib must be a positive integer" >&2
  exit 64
fi

root_preexisting="true"
if [[ ! -e "${root}" ]]; then
  root_preexisting="false"
elif [[ ! -d "${root}" ]]; then
  echo "installation root is not a directory: ${root}" >&2
  exit 64
fi
umask 077
dry_run_state=""
installer_base=""
installer_state=""
installer_lock=""
lock_acquired="false"
workspace=""
generation_root=""
installation_succeeded="false"
publication_started="false"
previous_current_present="false"
previous_current_target=""
publication_backup=""
managed_commands=(heartwood heartwood-jupyter hf)
cleanup() {
  set +e
  if [[ "${publication_started}" == "true" && "${installation_succeeded}" != "true" ]]; then
    for command_name in "${managed_commands[@]}"; do
      command_path="${root}/bin/${command_name}"
      backup_path="${publication_backup}/${command_name}"
      rm -f "${command_path}"
      if [[ -e "${backup_path}" || -L "${backup_path}" ]]; then
        mv "${backup_path}" "${command_path}"
      fi
    done
    if [[ "${previous_current_present}" == "true" ]]; then
      replace_symlink "${previous_current_target}" "${root}/current"
    else
      rm -f "${root}/current"
    fi
  fi
  if [[ -n "${generation_root}" && "${installation_succeeded}" != "true" && -d "${generation_root}" ]]; then
    rm -rf "${generation_root}"
  fi
  if [[ -n "${workspace}" ]]; then
    rm -rf "${workspace}"
  fi
  if [[ -n "${installer_state}" ]]; then
    rm -rf "${installer_state}"
  fi
  if [[ -n "${installer_base}" ]]; then
    rmdir "${installer_base}" >/dev/null 2>&1 || true
  fi
  if [[ "${lock_acquired}" == "true" ]]; then
    rm -f "${installer_lock}/pid"
    rmdir "${installer_lock}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${dry_run_state}" ]]; then
    rm -rf "${dry_run_state}"
  elif [[ "${installation_succeeded}" != "true" && "${root_preexisting}" == "false" ]]; then
    rmdir "${root}/bin" "${root}/installations" "${root}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${dry_run}" == "true" ]]; then
  dry_run_state="$(mktemp -d "${TMPDIR:-/tmp}/heartwood-installer-state.XXXXXX")"
  installer_state="${dry_run_state}/.installer"
else
  mkdir -p "${root}"
  root="$(cd "${root}" && pwd -P)"
  chmod 700 "${root}"
  for owned_directory in "${root}/.installer" "${root}/installations"; do
    if [[ -L "${owned_directory}" || ( -e "${owned_directory}" && ! -d "${owned_directory}" ) ]]; then
      echo "installer-owned path must be a directory, not a redirect: ${owned_directory}" >&2
      exit 73
    fi
  done
  if [[ -e "${root}/current" && ! -L "${root}/current" ]]; then
    echo "installation current path must be a symbolic link: ${root}/current" >&2
    exit 73
  fi
  if [[ -L "${root}/bin" || ( -e "${root}/bin" && ! -d "${root}/bin" ) ]]; then
    echo "installation command path must be a directory: ${root}/bin" >&2
    exit 73
  fi
  installer_lock="${root}/.installer.lock"
  if ! mkdir -m 700 "${installer_lock}" 2>/dev/null; then
    echo "another installation is using this root: ${root}" >&2
    echo "If no installer is running, remove ${installer_lock} and retry." >&2
    exit 75
  fi
  lock_acquired="true"
  printf '%s\n' "$$" >"${installer_lock}/pid"
  installer_base="${root}/.installer"
  mkdir -p "${installer_base}"
  chmod 700 "${installer_base}"
  if [[ "$(cd "${installer_base}" && pwd -P)" != "${installer_base}" ]]; then
    echo "installer state escaped the installation root" >&2
    exit 73
  fi
  installer_state="$(mktemp -d "${installer_base}/run.XXXXXX")"
fi
installer_directories=(
  "${installer_state}"
  "${installer_state}/home"
  "${installer_state}/tmp"
  "${installer_state}/cache/uv"
  "${installer_state}/cache/xdg"
  "${installer_state}/cache/mamba"
  "${installer_state}/cache/pip"
  "${installer_state}/cache/huggingface"
  "${installer_state}/cache/torch"
  "${installer_state}/cache/cuda"
  "${installer_state}/cache/numba"
  "${installer_state}/cache/triton"
  "${installer_state}/config"
  "${installer_state}/data"
  "${installer_state}/state"
)
mkdir -p "${installer_directories[@]}"
chmod 700 "${installer_directories[@]}"
export HOME="${installer_state}/home"
export TMPDIR="${installer_state}/tmp"
export TMP="${TMPDIR}"
export TEMP="${TMPDIR}"
export XDG_CACHE_HOME="${installer_state}/cache/xdg"
export XDG_CONFIG_HOME="${installer_state}/config"
export XDG_DATA_HOME="${installer_state}/data"
export XDG_STATE_HOME="${installer_state}/state"
export UV_CACHE_DIR="${installer_state}/cache/uv"
export MAMBA_ROOT_PREFIX="${installer_state}/cache/mamba"
export PIP_CACHE_DIR="${installer_state}/cache/pip"
export HF_HOME="${installer_state}/cache/huggingface"
export TORCH_HOME="${installer_state}/cache/torch"
export CUDA_CACHE_PATH="${installer_state}/cache/cuda"
export NUMBA_CACHE_DIR="${installer_state}/cache/numba"
export TRITON_CACHE_DIR="${installer_state}/cache/triton"

workspace="$(mktemp -d "${TMPDIR}/heartwood-installer.XXXXXX")"

stage "Resolve and verify the release assets"
require_command tar "to inspect the Heartwood release"
require_command sha256sum "to verify the Heartwood release"
if [[ -z "${bundle}" ]]; then
  if [[ "${installer_release}" == __HEARTWOOD_* ]]; then
    echo "the source installer is not release-stamped; use --bundle for local testing" >&2
    exit 64
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to retrieve a GitHub Release" >&2
    exit 69
  fi
  release_root="https://github.com/${repository}/releases/download/${installer_release}"
  bundle="${workspace}/heartwood-native.tar.gz"
  checksums="${workspace}/SHA256SUMS"
  curl --fail --location --show-error --progress-bar "${release_root}/heartwood-native.tar.gz" --output "${bundle}"
  curl --fail --location --show-error --progress-bar "${release_root}/SHA256SUMS" --output "${checksums}"
elif [[ -z "${checksums}" ]]; then
  echo "--checksums is required with --bundle" >&2
  exit 64
fi

if [[ ! -f "${bundle}" || ! -f "${checksums}" ]]; then
  echo "bundle and checksum manifest must be regular files" >&2
  exit 66
fi
checksum_line_count="$(wc -l <"${checksums}" | tr -d ' ')"
checksum_line="$(cat "${checksums}")"
if [[ "${checksum_line_count}" != "1" || ! "${checksum_line}" =~ ^[0-9a-f]{64}[[:space:]][[:space:]]heartwood-native\.tar\.gz$ ]]; then
  echo "checksum manifest must contain exactly heartwood-native.tar.gz" >&2
  exit 66
fi
if [[ "${bundle}" != "${workspace}/heartwood-native.tar.gz" ]]; then
  cp "${bundle}" "${workspace}/heartwood-native.tar.gz"
fi
if [[ "${checksums}" != "${workspace}/SHA256SUMS" ]]; then
  cp "${checksums}" "${workspace}/SHA256SUMS"
fi
(
  cd "${workspace}"
  sha256sum --check --strict SHA256SUMS
)

release_version="$(tar -xOf "${workspace}/heartwood-native.tar.gz" heartwood/HEARTWOOD_VERSION)"
if [[ ! "${release_version}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]]; then
  echo "bundle contains an unsafe release version" >&2
  exit 66
fi
if [[ "${installer_release}" != __HEARTWOOD_* && "${release_version}" != "${installer_release}" ]]; then
  echo "installer release ${installer_release} does not match bundle ${release_version}" >&2
  exit 66
fi

printf 'Heartwood native installation\n\n'
printf 'Platform: %s\n' "${platform}"
printf 'Version: %s\n' "${release_version}"
printf 'Root: %s\n' "${root}"
if [[ "${dry_run}" == "true" ]]; then
  installation_succeeded="true"
  printf 'Result: verified dry run; no files changed\n'
  exit 0
fi

stage "Check persistent storage"
existing_parent="${root}"
while [[ ! -e "${existing_parent}" && "${existing_parent}" != "/" ]]; do
  existing_parent="$(dirname "${existing_parent}")"
done
available_kib="$(df -Pk "${existing_parent}" | awk 'NR == 2 {print $4}')"
required_kib=$((minimum_free_gib * 1024 * 1024))
if [[ -z "${available_kib}" || "${available_kib}" -lt "${required_kib}" ]]; then
  echo "installation requires at least ${minimum_free_gib} GiB free under ${existing_parent}" >&2
  exit 73
fi
printf 'Available: %d GiB; installation minimum: %d GiB\n' \
  "$((available_kib / 1024 / 1024))" "${minimum_free_gib}"

installations_root="${root}/installations"
stage "Create the Heartwood installation layout"
if [[ -L "${installations_root}" || ( -e "${installations_root}" && ! -d "${installations_root}" ) ]]; then
  echo "installer-owned path must be a directory, not a redirect: ${installations_root}" >&2
  exit 73
fi
if [[ -e "${root}/current" && ! -L "${root}/current" ]]; then
  echo "installation current path must be a symbolic link: ${root}/current" >&2
  exit 73
fi
if [[ -L "${root}/bin" || ( -e "${root}/bin" && ! -d "${root}/bin" ) ]]; then
  echo "installation command path must be a directory: ${root}/bin" >&2
  exit 73
fi
mkdir -p \
  "${installations_root}" \
  "${root}/bin"
if [[ "$(cd "${installations_root}" && pwd -P)" != "${installations_root}" ]]; then
  echo "installation generations escaped the installation root" >&2
  exit 73
fi
chmod 700 \
  "${root}" \
  "${root}/bin" \
  "${installations_root}"
for command_name in "${managed_commands[@]}"; do
  command_path="${root}/bin/${command_name}"
  if [[ -e "${command_path}" && ! -f "${command_path}" && ! -L "${command_path}" ]]; then
    echo "installation command path is not replaceable: ${command_path}" >&2
    exit 73
  fi
done

stage "Assemble a private installation generation"
generation_root="$(mktemp -d "${installations_root}/${release_version}.XXXXXX")"
source_root="${generation_root}/source"
runtime_root="${generation_root}/runtime"
generation_bin="${generation_root}/bin"
mkdir -p "${source_root}" "${runtime_root}" "${generation_bin}"
chmod 700 "${generation_root}" "${source_root}" "${runtime_root}" "${generation_bin}"
tar -xzf "${workspace}/heartwood-native.tar.gz" -C "${source_root}" --strip-components=1

stage "Install the locked application and inference runtimes; this can take several minutes"
if [[ "${platform}" == "carina" ]]; then
  (
    cd "${source_root}"
    deploy/carina/bootstrap.sh \
      --environment-root "${runtime_root}" \
      --installer-state "${installer_state}"
  )
else
  require_command curl "to install the managed inference runtime"
  require_command git "for OpenHands coding tools"
  require_command tmux "for OpenHands terminal sessions"
  require_command uv "for a generic native installation"
  (
    cd "${source_root}"
    deploy/install-llama-cpp.sh "${runtime_root}/llama.cpp"
    UV_PROJECT_ENVIRONMENT="${runtime_root}/heartwood" \
    UV_PYTHON_INSTALL_DIR="${runtime_root}/python" \
      uv sync --locked --no-dev --all-extras
  )
fi

stage "Verify and publish the Heartwood installation"
command_path="${runtime_root}/heartwood/bin/heartwood"
if [[ ! -x "${command_path}" ]]; then
  echo "installed heartwood command is unavailable" >&2
  exit 70
fi
if ! "${command_path}" --version >/dev/null; then
  echo "installed heartwood command cannot start" >&2
  exit 70
fi
write_command_wrapper() {
  local output="$1"
  local executable="$2"
  local support_runtime
  if [[ "${platform}" == "carina" ]]; then
    support_runtime="${runtime_root}/bootstrap"
  else
    support_runtime="${runtime_root}/llama.cpp"
  fi
  {
    printf '#!/usr/bin/env bash\n'
    if [[ "${platform}" == "carina" ]]; then
      printf 'export HEARTWOOD_PLATFORM=carina\n'
    fi
    printf 'runtime=%q\n' "${support_runtime}"
    cat <<'EOF'
export PATH="${runtime}/bin:${runtime}:${PATH}"
export LD_LIBRARY_PATH="${runtime}/lib:${runtime}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
EOF
    printf 'exec %q "$@"\n' "${executable}"
  } >"${output}"
  chmod +x "${output}"
}

write_command_wrapper "${generation_bin}/heartwood" "${command_path}"
if ! "${generation_bin}/heartwood" --version >/dev/null; then
  echo "installed Heartwood wrapper cannot start" >&2
  exit 70
fi
if [[ "${platform}" == "generic" ]]; then
  jupyter_path="${runtime_root}/heartwood/bin/jupyter-lab"
  if [[ ! -x "${jupyter_path}" ]]; then
    echo "installed Jupyter command is unavailable" >&2
    exit 70
  fi
  write_command_wrapper "${generation_bin}/heartwood-jupyter" "${jupyter_path}"
  if ! "${generation_bin}/heartwood-jupyter" --version >/dev/null; then
    echo "installed Jupyter wrapper cannot start" >&2
    exit 70
  fi
fi
if [[ -x "${runtime_root}/vllm/bin/hf" ]]; then
  ln -s "${runtime_root}/vllm/bin/hf" "${generation_bin}/hf"
fi

replace_symlink() {
  local target="$1"
  local destination="$2"
  local temporary="${destination}.next.$$"
  rm -f "${temporary}"
  ln -s "${target}" "${temporary}"
  if mv -fT "${temporary}" "${destination}" 2>/dev/null; then
    return 0
  fi
  if mv -fh "${temporary}" "${destination}"; then
    return 0
  fi
  rm -f "${temporary}"
  return 1
}

current_target="installations/$(basename "${generation_root}")"
if [[ -L "${root}/current" ]]; then
  previous_current_present="true"
  previous_current_target="$(readlink "${root}/current")"
fi
publication_backup="${workspace}/publication-backup"
mkdir -m 700 "${publication_backup}"
for command_name in "${managed_commands[@]}"; do
  command_path="${root}/bin/${command_name}"
  backup_path="${publication_backup}/${command_name}"
  if [[ -L "${command_path}" ]]; then
    ln -s "$(readlink "${command_path}")" "${backup_path}"
  elif [[ -f "${command_path}" ]]; then
    cp -p "${command_path}" "${backup_path}"
  fi
done
publication_started="true"
replace_symlink "../${current_target}/bin/heartwood" "${root}/bin/heartwood"
if [[ "${platform}" == "generic" ]]; then
  replace_symlink "../${current_target}/bin/heartwood-jupyter" "${root}/bin/heartwood-jupyter"
  rm -f "${root}/bin/hf"
else
  rm -f "${root}/bin/heartwood-jupyter"
  if [[ -x "${generation_bin}/hf" ]]; then
    replace_symlink "../${current_target}/bin/hf" "${root}/bin/hf"
  else
    rm -f "${root}/bin/hf"
  fi
fi
if ! "${root}/bin/heartwood" --version >/dev/null; then
  echo "published Heartwood command cannot start" >&2
  exit 70
fi
replace_symlink "${current_target}" "${root}/current"
installation_succeeded="true"

stage "Installation complete"
rm -rf "${installer_state}"
printf 'Installed %s in %d seconds.\n' "${release_version}" "${SECONDS}"
printf 'Add %s to PATH, then run: heartwood doctor\n' "${root}/bin"
