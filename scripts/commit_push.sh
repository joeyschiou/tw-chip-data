#!/usr/bin/env bash
# commit_push.sh "<message>" <paths...>
# Actions 用:有變動才 commit;push 被拒(別的 run 先推了)就 rebase 再推,最多 5 次。
# 資料檔都是 append+dedup 的同源資料,rebase 衝突時取本輪版本(-X theirs)即可。
set -euo pipefail
msg="$1"; shift
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add "$@"
if git diff --staged --quiet; then
  echo "no changes, skip commit"
  exit 0
fi
git commit -q -m "$msg"
branch="$(git rev-parse --abbrev-ref HEAD)"
for i in 1 2 3 4 5; do
  if git push origin "HEAD:$branch"; then
    exit 0
  fi
  echo "push rejected, rebase and retry ($i/5)"
  sleep $((i * 5))
  git pull --rebase --autostash -X theirs origin "$branch"
done
echo "push failed after 5 attempts"
exit 1
