#!/usr/bin/env bash

# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
publisher="${repository_root}/deploy/publish-versioned-documentation.sh"
stable_branch="heartwood-documentation-stable-smoke-$$"
preview_branch="heartwood-documentation-preview-smoke-$$"
handoff_branch="heartwood-documentation-handoff-smoke-$$"
remote_name="heartwood-documentation-smoke-$$"
remote_root="$(mktemp -d)"
remote_repository="${remote_root}/pages.git"
failure_log=""
source_backup=""
source_path="${repository_root}/build/documentation/index.md"

cleanup() {
  if [[ -n "${failure_log}" ]]; then
    rm -f "${failure_log}"
  fi
  if [[ -n "${source_backup}" && -f "${source_backup}" ]]; then
    cp "${source_backup}" "${source_path}"
    rm -f "${source_backup}"
  fi
  git remote remove "${remote_name}" 2>/dev/null || true
  git update-ref -d "refs/heads/${stable_branch}" || true
  git update-ref -d "refs/heads/${preview_branch}" || true
  for _ in 1 2 3; do
    rm -rf "${remote_root}" 2>/dev/null && break
    sleep 0.1
  done
  if [[ -e "${remote_root}" ]]; then
    echo "warning: could not remove temporary documentation remote: ${remote_root}" >&2
  fi
}
trap cleanup EXIT

export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_AUTHOR_NAME="github-actions[bot]"
export GIT_COMMITTER_EMAIL="${GIT_AUTHOR_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_AUTHOR_NAME}"

expect_failure() {
  local expected="$1"
  shift
  failure_log="$(mktemp)"
  if bash "${publisher}" "$@" >"${failure_log}" 2>&1; then
    echo "documentation publication unexpectedly succeeded" >&2
    exit 1
  fi
  grep --fixed-strings "${expected}" "${failure_log}" >/dev/null
  rm -f "${failure_log}"
  failure_log=""
}

bash "${publisher}" \
  --version 0.1.0 \
  --channel stable \
  --branch "${stable_branch}"
stable_root="$(git rev-parse "${stable_branch}:index.html")"
stable_tip="$(git rev-parse "${stable_branch}")"

expect_failure \
  'documentation version must use strict Semantic Versioning' \
  --version stable \
  --channel stable \
  --branch "${stable_branch}"
expect_failure \
  'documentation branch is invalid: invalid..branch' \
  --version 0.1.1 \
  --channel stable \
  --branch invalid..branch
expect_failure \
  'documentation remote is unavailable: -invalid' \
  --version 0.1.1 \
  --channel stable \
  --branch "${stable_branch}" \
  --remote -invalid
test "$(git rev-parse "${stable_branch}")" = "${stable_tip}"

source_backup="$(mktemp)"
cp "${source_path}" "${source_backup}"
printf '\nVersion publication regression marker.\n' >> "${source_path}"
expect_failure \
  'published documentation for 0.1.0 differs from the existing version' \
  --version 0.1.0 \
  --channel stable \
  --branch "${stable_branch}"
cp "${source_backup}" "${source_path}"
rm -f "${source_backup}"
source_backup=""
git update-ref "refs/heads/${stable_branch}" "${stable_tip}"

bash "${publisher}" \
  --version 0.2.0-beta.1 \
  --channel preview \
  --branch "${stable_branch}"
test "$(git rev-parse "${stable_branch}:index.html")" = "${stable_root}"
git show "${stable_branch}:index.html" | grep --fixed-strings 'stable/' >/dev/null
git show "${stable_branch}:stable/index.html" | grep --fixed-strings '../0.1.0/' >/dev/null
git show "${stable_branch}:preview/index.html" | grep --fixed-strings '../0.2.0-beta.1/' >/dev/null
git show "${stable_branch}:versions.json" | jq --exit-status '
  any(.[]; .version == "0.1.0" and (.aliases | index("stable")) != null)
  and any(.[]; .version == "0.2.0-beta.1" and (.aliases | index("preview")) != null)
' >/dev/null

git init --bare --quiet "${remote_repository}"
git --git-dir="${remote_repository}" config gc.auto 0
git --git-dir="${remote_repository}" config maintenance.auto false
git remote add "${remote_name}" "${remote_repository}"
bash "${publisher}" \
  --version 0.2.0-beta.1 \
  --channel preview \
  --branch "${preview_branch}" \
  --remote "${remote_name}" \
  --push
git --git-dir="${remote_repository}" show \
  "refs/heads/${preview_branch}:index.html" | grep --fixed-strings 'preview/' >/dev/null
git update-ref -d "refs/heads/${preview_branch}"
git fetch "${remote_name}" \
  "+refs/heads/${preview_branch}:refs/heads/${preview_branch}"
bash "${publisher}" \
  --version 0.2.0-beta.2 \
  --channel preview \
  --branch "${preview_branch}" \
  --remote "${remote_name}" \
  --push
git --git-dir="${remote_repository}" show \
  "refs/heads/${preview_branch}:0.2.0-beta.1/index.html" >/dev/null
git --git-dir="${remote_repository}" show \
  "refs/heads/${preview_branch}:preview/index.html" | grep --fixed-strings '../0.2.0-beta.2/' >/dev/null
version_store_commit="$(git rev-parse "refs/heads/${preview_branch}^{commit}")"
git update-ref -d "refs/heads/${preview_branch}"
git push -- "${remote_name}" \
  "${version_store_commit}:refs/heads/${handoff_branch}"
git --git-dir="${remote_repository}" show \
  "refs/heads/${handoff_branch}:preview/index.html" | grep --fixed-strings '../0.2.0-beta.2/' >/dev/null
git update-ref "refs/heads/${preview_branch}" "${version_store_commit}"

for branch in "${stable_branch}" "${preview_branch}"; do
  git show "${branch}:.nojekyll" >/dev/null
  tree="$(git ls-tree -r "${branch}")"
  if grep '^120000 ' <<<"${tree}" >/dev/null; then
    echo "versioned documentation contains a symbolic link" >&2
    exit 1
  fi
done

echo "Versioned documentation smoke test passed"
