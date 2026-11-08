# app.py（Streamlit）と公式 Nutanix MCP Server を 1 つのコンテナに入れる
# 公式 MCP Server は stdio のみ対応のため、app.py が子プロセスとして起動する
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# ── 公式 MCP Server（v0.8）を入れる ─────────────────────────
ARG MCP_VERSION=v0.8
WORKDIR /opt/ntnx-api-mcp-server
RUN git clone --branch ${MCP_VERSION} --depth 1 https://github.com/nutanix/ntnx-api-mcp-server . \
 && pip install --no-cache-dir -e .

# API 仕様ファイル（artifacts）をビルド時に取ってきて焼き込む
# PC_HOST を設定しないので、開発者ポータルの最新版（latest_release モード）を取る
# ※ 起動時に entrypoint.sh が PC に合わせて取り直す。これは失敗したときの予備
ENV ARTIFACTS_DIR=/opt/ntnx-api-mcp-server/artifacts \
    LOG_DIR=/tmp/nutanix-mcp-logs
RUN nutanix-mcp init

# ── app.py を入れる ─────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY --chmod=755 entrypoint.sh .

# root で動かさない（init で root が作ったログのフォルダを app に渡す）
RUN useradd --create-home app \
 && mkdir -p /tmp/nutanix-mcp-logs \
 && chown -R app /tmp/nutanix-mcp-logs
USER app

# 接続先などは起動時に環境変数で渡す（Prism の接続情報はイメージに入れない）
ENV MCP_DIR=/opt/ntnx-api-mcp-server \
    LLM_BASE_URL=http://localhost:11434/v1 \
    LLM_MODEL=qwen2.5:7b

EXPOSE 8501
ENTRYPOINT ["./entrypoint.sh"]
