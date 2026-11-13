#!/usr/bin/env bash
# 刷新平台侧投影：逐个 cell 导出（pod 内 gzip）→ 本机投影进 agent_runs。
#
#   tools/refresh_platform.sh                      # 空跑：只报每个 cell 会吸收多少
#   tools/refresh_platform.sh --apply               # 真写
#   tools/refresh_platform.sh --since 2026-11-10 --cells 1da5 3fdc --apply
#
# 为什么默认空跑：这是**无人值守写生产库**。投影器本身是幂等的（同 (run_key, seq)
# 同指纹跳过、异指纹拒绝重写），但「接上电源」应该是一次显式决定，不是脚本默认行为。
#
# 传输：cell 的 `fleets.db` 全量导出约 11MB，而 tailnet 上一次 cp 超过 ~1.5MB 就会被
# 截断（本项目实测多次）。所以导出在 pod 内直接 gzip 再走 stdout：两天增量 ~1.8MB →
# gzip 后 ~240KB，稳在限内。
#
# 前置：
#   * kubectl 能 exec 进 fd-prod 的 platform pod（按 label 动态取，pod 名轮换很快）
#   * FD_AGENT_RUN_DSN 指向**有写权限**的角色（只读角色 fd_dashboards_ro 会 InsufficientPrivilege）
set -uo pipefail

SINCE=""
CELLS=""
APPLY=0
POD_LABEL="app.kubernetes.io/name=platform"
NS="fd-prod"
CTX="cheap-ts"

while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --cells) CELLS="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --context) CTX="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# 默认增量窗口：昨天（UTC）起。多算一天是安全的——投影按 (run_key, seq) 幂等。
if [ -z "$SINCE" ]; then
  SINCE=$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d 'yesterday' +%Y-%m-%d)
fi

POD=$(kubectl --context "$CTX" -n "$NS" get pods -l "$POD_LABEL" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "FATAL: 在 $CTX/$NS 找不到 platform pod（label $POD_LABEL）" >&2
  exit 2
fi
echo "pod=$POD  since=$SINCE  apply=$APPLY"

if [ -z "$CELLS" ]; then
  CELLS=$(kubectl --context "$CTX" -n "$NS" exec "$POD" -c platform -- \
            sh -lc 'ls -d /data/cells/*/data/data/fleets.db 2>/dev/null | cut -d/ -f4' | tr '\n' ' ')
fi
if [ -z "$CELLS" ]; then
  echo "FATAL: 没有发现任何 cell 的 fleets.db" >&2
  exit 2
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/refresh-platform.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# 解释器：优先 FD_PYTHON，其次 finddata 的 micromamba（实测带 psycopg 3），最后 python3。
# 不写死 /tmp 下的 venv —— 那东西重启就没了，而这是要挂定时器的脚本。
PY="${FD_PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in /Users/chengsishi/micromamba/bin/python3 python3; do
    if "$cand" -c 'import psycopg' >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "FATAL: 找不到带 psycopg 的 python（用 FD_PYTHON 指定）" >&2
  exit 2
fi

# 导出器每次重新拷进去：pod 会重启（本会话实测一小时内两次），/tmp 里的东西不留。
# 拷失败就等于没有导出器，直接停，别拿着上一次留下的旧脚本跑。
HERE=$(cd "$(dirname "$0")" && pwd)
if ! kubectl --context "$CTX" -n "$NS" cp "$HERE/export_platform.mjs" \
       "$POD:/tmp/export_platform.mjs" -c platform >/dev/null 2>&1; then
  echo "FATAL: 无法把 export_platform.mjs 拷进 $POD（pod 刚重启？）" >&2
  exit 2
fi

fail=0
total_absorbed=0
for cell in $CELLS; do
  out="$WORK/$cell.jsonl"
  # 该 cell 已吸收到的最大 seq → 只导更新的行。
  # 不这么做的话，表一加列就让**每一行**的指纹变化，投影器按契约拒绝重写历史
  # （2026-11-14 实测：cell 的行从 38 键变 40 键，四个 cell 立刻 REFUSED）。
  after=$("$PY" -c "
import os, psycopg, sys
cell = sys.argv[1]
with psycopg.connect(os.environ['FD_AGENT_RUN_DSN'], connect_timeout=10) as c, c.cursor() as cur:
    cur.execute('select coalesce(max(seq),0) from agent_runs.agent_run_event where run_key like %s',
                ('platform|cell:' + cell + '|%',))
    print(cur.fetchone()[0])
" "$cell")
  [ -z "$after" ] && after=0
  # pod 内 gzip → 管道 → 本地落盘（绕开单次 cp 的截断上限）。
  # **不能**写成 `node … | gzip`：管道的退出码取的是最后一个命令，node 崩了也会被
  # 当成成功，于是拿到一个 0 行的空导出、投影器报 "empty export"、脚本却报 OK。
  # 所以先落文件再压缩，显式 && 串起来。
  if ! kubectl --context "$CTX" -n "$NS" exec "$POD" -c platform -- \
        sh -lc "cd /app && node /tmp/export_platform.mjs --cell $cell --since $SINCE --after-id $after > /tmp/exp.jsonl && gzip -c /tmp/exp.jsonl" \
        > "$out.gz" 2>"$WORK/$cell.err"; then
    echo "  FAIL $cell: 导出失败 — $(tail -2 "$WORK/$cell.err" | tr '\n' ' ')"; fail=1; continue
  fi
  gzip -dc "$out.gz" > "$out" || { echo "  FAIL $cell: 解压失败"; fail=1; continue; }

  args=(--export "$out")
  [ "$APPLY" -eq 0 ] && args+=(--dry-run)
  stats=$("$PY" -m fd_agent_run.cli project "${args[@]}" 2>&1)
  rc=$?
  echo "  $stats"
  if [ $rc -ne 0 ]; then fail=1; fi
done

echo "----"
if [ "$APPLY" -eq 0 ]; then
  echo "全部空跑完成（未写库）。要真写加 --apply。"
else
  echo "已写入。"
fi
exit $fail
