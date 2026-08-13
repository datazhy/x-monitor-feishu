#!/usr/bin/env bash
#
# X 博主推文监控 → 飞书  一键部署脚本
#
#   bash deploy/install.sh              # 交互式：缺什么问什么
#   bash deploy/install.sh --yes        # 非交互：只校验 .env，不提问（适合 CI / 重装）
#   bash deploy/install.sh --rebuild    # 仅重建并重启（改完代码用这个）
#
# 做的事：环境自检 → 生成 .env / rules.yml → 构建启动 → 健康检查 → 打印 webhook 地址
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="deploy/cloudflare/docker-compose.yml"
ASSUME_YES=0
REBUILD_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y)   ASSUME_YES=1 ;;
    --rebuild)  REBUILD_ONLY=1 ;;
    -h|--help)  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 1 ;;
  esac
done

# ---------- 输出helpers ----------
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_bld=$'\033[1m'; c_off=$'\033[0m'
info() { echo "${c_bld}▶${c_off} $*"; }
ok()   { echo "${c_grn}✓${c_off} $*"; }
warn() { echo "${c_ylw}!${c_off} $*"; }
die()  { echo "${c_red}✗ $*${c_off}" >&2; exit 1; }

# ---------- 1. 环境自检 ----------
info "检查运行环境"
command -v docker >/dev/null 2>&1 || die "未找到 docker。安装：https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件（需 Docker Compose V2）"
docker info >/dev/null 2>&1 || die "Docker 守护进程未运行，或当前用户无权限（试试 sudo，或把用户加入 docker 组）"
ok "docker $(docker version --format '{{.Server.Version}}') + compose 就绪"

# ---------- 2. 配置文件 ----------
if [[ ! -f .env ]]; then
  [[ -f .env.example ]] || die "缺少 .env.example，仓库不完整"
  cp .env.example .env
  ok "已由 .env.example 生成 .env"
fi

if [[ ! -f config/rules.yml ]]; then
  cp config/rules.example.yml config/rules.yml
  ok "已生成 config/rules.yml（记得填入要监控的博主 handle）"
fi

# 读/写 .env 的小工具（只认行首 KEY=，避免误伤注释）
env_get() { sed -n "s/^$1=//p" .env | head -1; }
env_set() {
  local key="$1" val="$2"
  # 用 | 作分隔符并转义，兼容含 / 的 URL
  local esc=${val//|/\\|}
  if grep -q "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${esc}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# WEBHOOK_SECRET 自动生成（用户不该手填）
secret="$(env_get WEBHOOK_SECRET)"
if [[ -z "$secret" || "$secret" == CHANGE_ME* ]]; then
  new_secret="$(openssl rand -hex 24 2>/dev/null || head -c48 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  env_set WEBHOOK_SECRET "$new_secret"
  ok "已自动生成 WEBHOOK_SECRET"
fi

# ---------- 3. 必填项 ----------
# key|中文说明|是否必填
REQUIRED=(
  "DOMAIN|webhook 域名（需托管在 Cloudflare），如 hook.example.com|1"
  "CF_API_TOKEN|Cloudflare API Token（Zone:DNS:Edit，用于自动签证书）|1"
  "ACME_EMAIL|Let's Encrypt 通知邮箱|1"
  "TWITTERAPI_KEY|TwitterAPI.io API Key|1"
  "FEISHU_WEBHOOK_URL|飞书群机器人 Webhook 地址|1"
  "DEEPSEEK_API_KEY|DeepSeek API Key（翻译/分析/早报）|1"
)

missing=()
for item in "${REQUIRED[@]}"; do
  IFS='|' read -r key desc _ <<< "$item"
  val="$(env_get "$key")"
  if [[ -z "$val" || "$val" == CHANGE_ME* || "$val" == *example.com* ]]; then
    if [[ $ASSUME_YES -eq 1 || $REBUILD_ONLY -eq 1 ]]; then
      missing+=("$key（$desc）")
    else
      info "请填写 ${c_bld}${key}${c_off} —— ${desc}"
      read -r -p "  $key = " input </dev/tty || true
      if [[ -n "${input:-}" ]]; then
        env_set "$key" "$input"
      else
        missing+=("$key（$desc）")
      fi
    fi
  fi
done

if (( ${#missing[@]} > 0 )); then
  warn "以下必填项还没填，请编辑 .env 后重跑："
  printf '    - %s\n' "${missing[@]}"
  die "配置不完整，已中止（未启动任何容器）"
fi
ok "必填配置齐全"

# ---------- 4. 构建启动 ----------
DC=(docker compose --env-file .env -f "$COMPOSE_FILE")
info "构建镜像并启动容器（首次可能需要几分钟）"
"${DC[@]}" up -d --build
ok "容器已启动"

# ---------- 5. 健康检查 ----------
info "等待服务就绪"
app_cid="$("${DC[@]}" ps -q app)"
[[ -n "$app_cid" ]] || die "app 容器未启动，看日志：${DC[*]} logs app"

for i in $(seq 1 30); do
  if docker exec "$app_cid" python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)" \
      >/dev/null 2>&1; then
    ok "服务健康检查通过（/healthz）"
    break
  fi
  [[ $i -eq 30 ]] && { "${DC[@]}" logs --tail 40 app; die "服务 60 秒内未就绪，日志见上"; }
  sleep 2
done

# ---------- 6. 初始化数据库 + 收尾提示 ----------
docker exec "$app_cid" python -m scripts.init_db >/dev/null 2>&1 && ok "数据库已初始化" || warn "数据库初始化跳过（可能已存在）"

if [[ $REBUILD_ONLY -eq 1 ]]; then
  echo
  ok "${c_bld}重建完成，服务已重启${c_off}"
  exit 0
fi

DOMAIN="$(env_get DOMAIN)"
PORT="$(env_get WEBHOOK_PORT)"; PORT="${PORT:-8443}"
SECRET="$(env_get WEBHOOK_SECRET)"

cat <<EOF

${c_grn}${c_bld}部署完成 🎉${c_off}

${c_bld}还差最后 3 步（都在外部平台操作）：${c_off}

  ${c_bld}1.${c_off} Cloudflare 加一条 A 记录，把 ${c_bld}${DOMAIN}${c_off} 指向本机公网 IP
     必须是${c_bld}灰云 / DNS only${c_off}（橙云代理会挡掉非标端口）

  ${c_bld}2.${c_off} 到 TwitterAPI.io 控制台 → Webhook Configuration，填入回调地址：
     ${c_bld}https://${DOMAIN}:${PORT}/webhook/twitterapi/${SECRET}${c_off}
     （账户级全局配置，只需填一次）

  ${c_bld}3.${c_off} 编辑 ${c_bld}config/rules.yml${c_off} 填入要监控的博主，然后同步规则：
     docker compose --env-file .env -f ${COMPOSE_FILE} exec -T app python -m scripts.manage_rules sync

${c_bld}验收：${c_off}
  docker compose --env-file .env -f ${COMPOSE_FILE} exec -T app python -m scripts.send_test
  curl https://${DOMAIN}:${PORT}/healthz

${c_bld}日志：${c_off}
  docker compose --env-file .env -f ${COMPOSE_FILE} logs -f app

EOF
