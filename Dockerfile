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
ENV ARTIFACTS_DIR=/opt/ntnx-api-mcp-server/artifacts \
    LOG_DIR=/tmp/nutanix-mcp-logs
RUN nutanix-mcp init

# ── app.py を入れる ─────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# root で動かさない
RUN useradd --create-home app
USER app

# 接続先などは起動時に環境変数で渡す（Prism の接続情報はイメージに入れない）
ENV MCP_DIR=/opt/ntnx-api-mcp-server \
    LLM_BASE_URL=http://localhost:11434/v1 \
    LLM_MODEL=qwen2.5:7b

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
