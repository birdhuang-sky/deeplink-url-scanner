#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
CSV_PATH="${REPO_ROOT}/results/repo_keywords_no_test.csv"
CLONE_JOBS="${CLONE_JOBS:-6}"

if [[ ! -f "${CSV_PATH}" ]]; then
  echo "CSV not found: ${CSV_PATH}" >&2
  exit 1
fi

mkdir -p "${REPO_ROOT}/logs/clone_repos"
LOG_DIR="${REPO_ROOT}/logs/clone_repos"

cd "${REPO_ROOT}"

mapfile -t repos < <(
  awk -F',' 'NR>1 && $1 != "" {print $1}' "${CSV_PATH}" \
    | tr -d '\r' \
    | sort -u
)

if (( ${#repos[@]} == 0 )); then
  echo "No repositories found in ${CSV_PATH}" >&2
  exit 0
fi

echo "Cloning ${#repos[@]} repositories with up to ${CLONE_JOBS} concurrent jobs…"

running_jobs=0
for repo in "${repos[@]}"; do
  [[ -z "${repo}" ]] && continue
  name="${repo##*/}"
  log_file="${LOG_DIR}/${name}.log"

  if [[ -d "${name}" ]]; then
    echo "Skipping ${repo} (directory '${name}' already exists)"
    continue
  fi

  git_url="git@github.com:${repo}.git"
  echo "[$(date '+%H:%M:%S')] Cloning ${git_url}"

  (
    if git clone "${git_url}" &> "${log_file}"; then
      echo "[$(date '+%H:%M:%S')] Clone succeeded: ${repo}"
    else
      echo "[$(date '+%H:%M:%S')] Clone failed: ${repo} (see ${log_file})"
    fi
  ) &

  ((running_jobs+=1))
  if (( running_jobs >= CLONE_JOBS )); then
    wait -n
    ((running_jobs-=1))
  fi
done

wait
echo "All clone jobs completed."
