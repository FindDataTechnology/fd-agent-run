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

HERE=$(cd "$(dirname "$0")" && pwd)

# pod 名不是常量：本会话实测一小时内换了两次（platform-5d794d888 → platform-9cb5dcb4b），
# 多 cell 跑到一半时换掉的那次，剩下的 cell 全打在已死的 pod 上。所以**每个 cell 前
# 重新解析**，并重新拷导出器（/tmp 里的东西不跨 pod 存活）。
resolve_pod() {
  kubectl --context "$CTX" -n "$NS" get pods -l "$POD_LABEL" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}
copy_exporter() {
  kubectl --context "$CTX" -n "$NS" cp "$HERE/export_platform.mjs" \
    "$1:/tmp/export_platform.mjs" -c platform >/dev/null 2>&1
}

POD=$(resolve_pod)
if [ -z "$POD" ]; then
  echo "FATAL: 在 $CTX/$NS 找不到 platform pod（label ${POD_LABEL}）" >&2
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
#
# **`${VAR}` 的花括号不是风格问题**：写成 `$POD` 紧跟一个全角左括号时，
# macOS 自带的 bash 3.2 会把那个括号的首字节读进变量名 ⇒ `set -u` 当场 abort
# （2026-11-14 实测：报 `POD?: unbound variable`，行号指向这里）。
# 而这几处全在**失败分支**上，于是「pod 在重启」这个它本来要报告的情况，
# 变成了一个看不懂的报错。本机 /opt/homebrew/bin/bash 不存在，定时作业也走 3.2
# —— 不是个人环境问题。同类判据见 tests/test_shell_var_adjacent_multibyte.py。
if ! copy_exporter "${POD}"; then
  echo "FATAL: 无法把 export_platform.mjs 拷进 ${POD}（pod 刚重启？）" >&2
  exit 2
fi

fail=0
for cell in $CELLS; do
  out="$WORK/$cell.jsonl"

  # 每个 cell 前刷新 pod + 导出器；失败重试一次（新 pod 起来要几秒）。
  ready=0
  for attempt in 1 2; do
    POD=$(resolve_pod)
    if [ -n "$POD" ] && copy_exporter "$POD"; then ready=1; break; fi
    sleep 3
  done
  if [ "$ready" -eq 0 ]; then
    echo "  FAIL $cell: 拿不到可用 pod（label ${POD_LABEL}，试了 2 次）"; fail=1; continue
  fi
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
  #
  # 导出是**只读且幂等**的，所以失败可重试。两类瞬时故障都命中过：
  #   * pod 在这一格之前被换掉（重解析 + 重拷导出器）
  #   * kube API `read: connection reset by peer`（2026-11-14 全量跑里有一格撞上）
  exported=0
  for attempt in 1 2 3; do
    POD=$(resolve_pod)
    if [ -z "$POD" ] || ! copy_exporter "$POD"; then
      echo "  第 $attempt 次：拿不到可用 pod"; sleep 3; continue
    fi
    if kubectl --context "$CTX" -n "$NS" exec "$POD" -c platform -- \
          sh -lc "cd /app && node /tmp/export_platform.mjs --cell $cell --since $SINCE --after-id $after > /tmp/exp.jsonl && gzip -c /tmp/exp.jsonl" \
          > "$out.gz" 2>"$WORK/$cell.err"; then
      exported=1; break
    fi
    echo "  第 $attempt 次导出失败，重试：$(tail -1 "$WORK/$cell.err" | cut -c1-120)"
    sleep 3
  done
  if [ "$exported" -eq 0 ]; then
    echo "  FAIL $cell: 导出失败（试了 3 次）— $(tail -2 "$WORK/$cell.err" | tr '\n' ' ')"; fail=1; continue
  fi
  gzip -dc "$out.gz" > "$out" || { echo "  FAIL $cell: 解压失败"; fail=1; continue; }

  # 导出必须自带 meta，且 meta 里的 cell 对得上。
  # pod 在导出途中被杀时，`sh -lc` 可能「成功」地写出半截或空文件；把它喂给投影器
  # 只会得到一行全 0 的统计——读起来像「这个 cell 没有新数据」。2026-11-14 就这么
  # 报过一次 `cell None: events=0 … absorbed=None`，而脚本末尾照样说「已写入。」。
  meta=$(head -1 "$out" 2>/dev/null || true)
  case "$meta" in
    *'"k":"meta"'*) ;;
    *) echo "  FAIL $cell: 导出缺 meta 头（空导出？）— $(printf '%s' "$meta" | cut -c1-100)"; fail=1; continue ;;
  esac
  case "$meta" in
    *"\"cell\":\"$cell\""*) ;;
    *) echo "  FAIL $cell: 导出 meta 的 cell 对不上 — $(printf '%s' "$meta" | cut -c1-100)"; fail=1; continue ;;
  esac

  args=(--export "$out")
  [ "$APPLY" -eq 0 ] && args+=(--dry-run)
  stats=$("$PY" -m fd_agent_run.cli project "${args[@]}" 2>&1)
  rc=$?
  echo "  $stats"
  if [ $rc -ne 0 ]; then fail=1; fi
done

echo "----"
# 有任何一个 cell 失败，就**不能**在结尾说「已写入」——那句话会被读成整批成功，
# 而实际可能是某几个 cell 一行都没进去（2026-11-14 实历）。
if [ "$fail" -ne 0 ]; then
  echo "有单元格失败（见上逐条 FAIL）。本次**不是**完整写入。"
  exit 1
fi
if [ "$APPLY" -eq 0 ]; then
  echo "全部空跑完成（未写库）。要真写加 --apply。"
else
  echo "已写入。"
fi
exit 0
