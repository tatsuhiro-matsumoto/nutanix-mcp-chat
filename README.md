# nutanix-mcp-chat

[Nutanix MCP Server](https://github.com/nutanix/ntnx-api-mcp-server) とローカルLLM（Ollama）を使って、Prism Centralに日本語で話しかけるチャットアプリです。

「起動中のVMを一覧で出して」のように聞くと、Prism CentralのV4 APIから情報を取り、表で返します。すべてローカルで動くので、Prismの情報は社外に出ません。

解説記事：[Nutanix MCP Server：ローカルLLMでPrism Centralに話しかけてみる (2026年11月時点) その1](https://qiita.com/matsumo2019/items/d1d8ecaa7449776ec17b)

## 構成

```
ブラウザ ── app.py（Streamlit＋自作クライアント）
              ├─ Ollama（LLM）
              └─ Nutanix MCP Server ── Prism Central（V4 API）
```

- **Ollama**：ローカルでLLMを動かす実行基盤
- **Nutanix MCP Server**：Prism CentralのAPIを実行する（app.pyが子プロセスとして起動する）
- **app.py**：画面、Ollama、MCP Serverの間で伝言する

## 動作確認した環境

- Ubuntu 24.04 LTS（vCPU 8 / メモリ 16GB / GPUなし）
- Nutanix MCP Server v0.8（Technical Preview）
- Ollama ＋ `qwen2.5:7b`

## 使い方

### 1. Ollamaとモデルを入れる

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
```

### 2. Nutanix MCP Serverを入れて設定する

```bash
git clone https://github.com/nutanix/ntnx-api-mcp-server
cd ntnx-api-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
vi .env   # PC_HOST / PC_USERNAME / PC_PASSWORD などを設定
nutanix-mcp init
nutanix-mcp run --validate-only
```

※ `PC_HOST` はPrism Element（クラスタのVIP）ではなく、Prism Centralを指定してください。

### 3. app.pyを置いて起動する

`app.py` と `requirements.txt` を `ntnx-api-mcp-server` フォルダに置き、同じ仮想環境で実行します。

```bash
pip install -r requirements.txt
streamlit run app.py --server.address 0.0.0.0
```

ブラウザで `http://<VMのIP>:8501` を開きます。

## 設定（環境変数）

| 変数 | 初期値 | 内容 |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | LLMの接続先（OpenAI互換API） |
| `LLM_API_KEY` | `ollama` | LLMのAPIキー（Ollamaは任意の文字列でOK） |
| `LLM_MODEL` | `qwen2.5:7b` | 使うモデル |
| `MCP_DIR` | `~/ntnx-api-mcp-server` | MCP Serverのフォルダ（`.env` がある場所） |
| `PREFER_VARIANT` | `ahv` | 候補から外すハイパーバイザー版の指定（AHV環境ならESXi版を外す。空にすると両方） |

例：別のモデルで起動する

```bash
LLM_MODEL=qwen3:8b streamlit run app.py --server.address 0.0.0.0
```

## 注意点

- Nutanix MCP ServerはTechnical Previewです。本番環境での利用はサポートされていません。検証環境で使ってください。
- MCP Serverの `READ_ONLY_MODE=true`（初期値）のままなら、GET以外の操作はブロックされます。
- 小さいモデル（7bクラス）でも動くように、操作の候補探しや絞り込みをapp.py側で肩代わりしています。ハイパーバイザーをAHVに固定するなど、ハードコードしている部分があります。
- VMの一覧は1回のAPI呼び出しで最大100件まで取得します。100台を超える環境では取りこぼします（未対応）。
- 複雑な質問（例：「メモリ使用率が85%を超えているVMをリストアップして」）には、まだ答えられません。
- `.env` には認証情報が入っているので、Gitにコミットしないでください。

## ライセンス

[MIT License](LICENSE)
