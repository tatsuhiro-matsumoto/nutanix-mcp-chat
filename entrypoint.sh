#!/bin/sh
# 起動時に、つなぐ Prism Central に合わせて API 仕様ファイル（artifacts）を取り直す
# （PC_HOST があると nutanix-mcp init は pc_compatible モードになる）
# 失敗したら、イメージに焼き込んだ最新版（latest_release）をそのまま使う
if [ -n "$PC_HOST" ] && [ "${INIT_ON_START:-true}" = "true" ]; then
  echo "[entrypoint] ${PC_HOST} に合わせて artifacts を取得します"
  if (cd /opt/ntnx-api-mcp-server && ARTIFACTS_DIR=/tmp/artifacts nutanix-mcp init); then
    export ARTIFACTS_DIR=/tmp/artifacts
  else
    echo "[entrypoint] 取得に失敗したので、イメージ内の artifacts を使います"
  fi
fi
echo "[entrypoint] ARTIFACTS_DIR=${ARTIFACTS_DIR}"
ls "${ARTIFACTS_DIR}"

exec streamlit run app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true
