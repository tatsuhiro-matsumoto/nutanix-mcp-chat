"""Prism Central に聞いてみる（ツール検索版）

流れ:
  1. LLMに質問から英語の検索キーワードを出してもらう
  2. app.py が listOperations で候補の操作を探し、getOperationSchema で定義を取る
  3. 候補と定義を添えて、LLMに「1つ選んで実行」させる（run_operation ツール）
  4. 結果をLLMがまとめて回答
"""
import asyncio
import fnmatch
import json
import os
import re

import streamlit as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

# ===== 設定 =====
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")  # 第3回でNAIやAI GatewayのURLに変える
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
MCP_DIR = os.path.expanduser(os.getenv("MCP_DIR", "~/ntnx-api-mcp-server"))
NO_THINK = os.getenv("NO_THINK", "1") == "1" and "qwen3" in LLM_MODEL  # Qwen3の思考モードを切る（速さ優先）
PREFER_VARIANT = os.getenv("PREFER_VARIANT", "ahv")  # AHV環境なのでESXi版の操作は候補から外す（空にすると両方）
TOP_K = 5                 # LLMに見せる候補の数
SCHEMA_K = 3              # 定義（パラメーター）まで見せる候補の数
MAX_SCHEMA_CHARS = 1500   # 候補1件あたりの定義の上限
MAX_RESULT_CHARS = 6000   # 実行結果の上限
MAX_STEPS = 4             # 実行の最大回数

llm = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=1800)


def chat(messages, **kw):
    if NO_THINK:
        messages = [{"role": "system", "content": "/no_think"}] + messages
    return llm.chat.completions.create(model=LLM_MODEL, messages=messages, **kw).choices[0].message


def clean(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def text_of(result):
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


# ---------- 1. キーワードを出す ----------
KEYWORD_PROMPT = """ユーザーの質問に答えるために、Nutanix Prism Central V4 APIの操作を検索します。
検索キーワード（英語の単語）を2〜4個と、関係しそうなnamespaceを考えてください。
namespaceの例: vmm(VM), clustermgmt(クラスタ・ホスト), monitoring(アラート・イベント),
networking(サブネット), storage(ストレージ), dataprotection(スナップショット・リカバリポイント), prism(タスク・カテゴリ)
JSONだけで答えてください。例: {"keywords": ["list", "vm"], "namespace": "vmm"}

質問: """


def get_keywords(question):
    reply = clean(chat([{"role": "user", "content": KEYWORD_PROMPT + question}]).content)
    m = re.search(r"\{.*\}", reply, flags=re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    keywords = [k for k in data.get("keywords", []) if isinstance(k, str)] or ["list"]
    return keywords, data.get("namespace") or None


# ---------- 2. 候補を探す ----------
def parse_operations(text):
    """listOperations の出力（{"ok":..., "payload":[{namespace, operation, method, summary, ...}]}）から候補を取り出す"""
    try:
        payload = json.loads(text).get("payload") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    return [p for p in payload if isinstance(p, dict) and p.get("operation") and p.get("namespace")]


async def search_candidates(session, keywords, namespace, log):
    found = {}  # operation -> 候補の情報
    queries = [" ".join(keywords)] + keywords  # まとめて検索 → 1語ずつ検索
    for q in queries:
        for ns in ([namespace, None] if namespace else [None]):
            args = {"search": q, "limit": 20}
            if ns:
                args["namespace"] = ns
            for op in parse_operations(text_of(await session.call_tool("listOperations", args))):
                found.setdefault(op["operation"], op)
        if len(found) >= TOP_K * 3:
            break

    ops = list(found.values())
    # この環境はAHVなので、AHV版があるESXi版は外す
    if PREFER_VARIANT:
        ops = [o for o in ops if o.get("path_variant") in (None, PREFER_VARIANT)
               or not any(p.get("spec_operation_id") == o.get("spec_operation_id")
                          and p.get("path_variant") == PREFER_VARIANT for p in ops)]
    # 読み取り専用モードなので GET を優先し、関連度の高い順に並べる
    ops.sort(key=lambda o: (o.get("method") != "GET", -(o.get("relevance_score") or 0)))
    ops = ops[:TOP_K]
    log.write(f"🔍 候補: {', '.join(o['operation'] for o in ops) if ops else '見つからず'}")

    lines = [f"- {o['operation']}（{o.get('method')} {o.get('path')}）: {o.get('summary')}" for o in ops]
    schemas = []
    for o in ops[:SCHEMA_K]:
        s = text_of(await session.call_tool("getOperationSchema", {"operation": o["operation"]}))
        schemas.append(f"### {o['operation']} の定義\n{s[:MAX_SCHEMA_CHARS]}")
    return {o["operation"]: o["namespace"] for o in ops}, "\n".join(lines) + "\n\n" + "\n\n".join(schemas)


# ---------- 3. 選んで実行 ----------
RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "run_operation",
        "description": "候補の中から1つの操作を選んで、Prism Centralに対して実行する",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "候補にある操作名（例: ahv_listVms）"},
                "params": {
                    "type": "object",
                    "description": "APIの引数。通常は空でよい。POST等の本文は request_body",
                },
                "where": {
                    "type": "object",
                    "description": "取得後に絞り込む条件。項目名と値の組。* が使える。例: 起動中なら {\"powerState\": \"ON\"}、名前がnkpで始まるなら {\"name\": \"nkp*\"}、名前にtestを含むなら {\"name\": \"*test*\"}",
                },
            },
            "required": ["operation"],
        },
    },
}

ANSWER_PROMPT = """あなたはNutanix Prism Centralの運用アシスタントです。
下の「候補の操作」から最適なものを選び、run_operation で実行してください。
- 操作名は候補にあるものだけを使う
- 条件（起動中、停止中など）があれば where で絞り込む
- 一覧の表はアプリが自動で表示する。あなたは表や個々の行を書き出さず、件数と合計など全体の傾向を2〜3文で答える
- 実行結果にない情報は推測で答えない
- 回答は必ず日本語で書く

## 候補の操作
{schemas}
"""


QUERY_PARAMS = {"filter", "limit", "page", "orderby", "select", "expand"}


def fix_params(params):
    """小さいモデルが間違えやすい引数名を直す（limit → _limit など）"""
    fixed = {}
    for k, v in (params or {}).items():
        key = k.lstrip("$_")
        fixed[f"_{key}" if key in QUERY_PARAMS else k] = v
    return fixed


def slim(obj):
    """V4 APIの応答から、管理用の項目（$で始まるキー、links）と空の値を取り除く"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.startswith("$") or k == "links":
                continue
            v = slim(v)
            if v not in (None, "", [], {}):
                out[k] = v
        return out
    if isinstance(obj, list):
        return [slim(v) for v in obj]
    return obj


def flatten_item(item):
    """一覧の1件を、値が直接入っている項目だけの1行にする（入れ子の詳細は捨てる）"""
    row = {}
    for k, v in item.items():
        if isinstance(v, (str, int, float, bool)):
            row[k] = v
        elif isinstance(v, dict) and isinstance(v.get("name"), str):
            row[k] = v["name"]  # 例: {"name": "xxx", ...} → "xxx"
    return row


# 表に出すときに優先する列（あるものだけ使う。なければ全部の列）
PREFERRED_COLUMNS = ["name", "powerState", "numSockets", "numCoresPerSocket", "memoryGB",
                     "host", "cluster", "description", "createTime"]


def tidy_rows(rows):
    """人が読みやすい形に整える（バイト→GB、列を絞る）"""
    for r in rows:
        if isinstance(r.get("memorySizeBytes"), int):
            r["memoryGB"] = round(r.pop("memorySizeBytes") / 1024**3, 1)
    cols = [c for c in PREFERRED_COLUMNS if any(c in r for r in rows)]
    if len(cols) < 2:
        cols = list(dict.fromkeys(k for r in rows for k in r))
    return [{c: r.get(c, "") for c in cols} for r in rows]


def apply_where(rows, where):
    """取得後の絞り込み。* で前方一致・部分一致もできる。大文字小文字は区別しない"""
    for key, want in (where or {}).items():
        pattern = str(want).lower()
        rows = [r for r in rows
                if any(k.lower() == key.lower() and fnmatch.fnmatchcase(str(v).lower(), pattern)
                       for k, v in r.items())]
    return rows


def parse_result(text):
    """実行結果を（一覧の行, メッセージ）に分ける。一覧でなければ行は None"""
    try:
        data = slim(json.loads(text))
    except json.JSONDecodeError:
        return None, text
    payload = data.get("payload", data) if isinstance(data, dict) else data
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = [flatten_item(i) for i in payload["data"] if isinstance(i, dict)]
        total = (payload.get("metadata") or {}).get("totalAvailableResults")
        return rows, total
    return None, json.dumps(payload, ensure_ascii=False)


async def run_operation(session, tool_names, candidates, args, log, tables):
    op = (args.get("operation") or "").strip()
    if op not in candidates:  # namespace はLLMに考えさせず、候補から引く
        return f"エラー: {op} は候補にありません。使えるのは {list(candidates)}"
    tool = f"{candidates[op]}_execute"
    if tool not in tool_names:
        return f"エラー: {tool} はこのPrism Centralでは使えません"
    params = fix_params(args.get("params"))
    if op.split("_")[-1].startswith("list"):
        params.setdefault("_limit", 100)  # 一覧は最大件数で取る（絞り込みはコード側で行う）
    call = {"operation": op, **params}
    log.write(f"🔧 `{tool}` {json.dumps(call, ensure_ascii=False)}")
    raw = text_of(await session.call_tool(tool, call))

    rows, info = parse_result(raw)
    if rows is None:  # 一覧以外（エラーや1件の詳細）はそのままLLMへ
        return info[:MAX_RESULT_CHARS]

    where = args.get("where") or {}
    matched = tidy_rows(apply_where(rows, where))
    log.write(f"📦 取得 {len(rows)}件" + (f" → 条件 {where} で {len(matched)}件" if where else ""))
    tables.append(matched)

    # LLMには表の全部ではなく、要約に必要な情報だけを渡す
    summary = {
        "取得件数": len(rows),
        "全体の件数": info,
        "絞り込み条件": where or "なし",
        "条件に合った件数": len(matched),
        "列": list(matched[0]) if matched else [],
    }
    # 数値の列は合計をコードで計算して渡す（LLMに足し算させない）
    for col in ("numSockets", "memoryGB"):
        vals = [r[col] for r in matched if isinstance(r.get(col), (int, float))]
        if vals:
            summary[f"{col} の合計"] = round(sum(vals), 1)
    if where and not matched:  # 項目名を間違えた可能性があるので、使える項目名を教える
        summary["where に使える項目名"] = sorted({k for r in rows for k in r})
    return json.dumps(summary, ensure_ascii=False, default=str)


async def ask(history, log, tables):
    question = history[-1]["content"]
    server = StdioServerParameters(command="nutanix-mcp", args=["serve-stdio"], cwd=MCP_DIR, env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_names = {t.name for t in (await session.list_tools()).tools}

            keywords, namespace = get_keywords(question)
            log.write(f"🗝️ キーワード: {keywords} / namespace: {namespace}")

            candidates, schemas = await search_candidates(session, keywords, namespace, log)
            if not candidates:
                return "該当しそうな操作が見つかりませんでした。言い方を変えて聞いてみてください。"

            past = [{"role": m["role"], "content": m["content"]} for m in history]
            messages = [{"role": "system", "content": ANSWER_PROMPT.format(schemas=schemas)}] + past
            executed = False  # 一度でも実際にAPIを実行したか
            nudged = False
            for _ in range(MAX_STEPS):
                msg = chat(messages, tools=[RUN_TOOL])
                if not msg.tool_calls:
                    if executed:
                        return clean(msg.content)
                    # データを取らずに答えようとした → 一度だけやり直させる
                    if not nudged:
                        nudged = True
                        log.write("⚠️ データを取得せずに答えようとしたので、やり直させます")
                        messages.append({"role": "assistant", "content": msg.content or ""})
                        messages.append({"role": "user", "content": "データを取得せずに答えてはいけません。必ず run_operation を呼んでください。"})
                        continue
                    # それでも取らない → LLMの作文は表示しない
                    log.write("⛔ データを取得しなかったため、回答を破棄しました")
                    return "Prism Centralからデータを取得できなかったため、回答できませんでした。聞き方を変えて試してください。"
                messages.append(msg.model_dump(exclude_none=True))
                for call in msg.tool_calls:
                    try:
                        args = json.loads(call.function.arguments or "{}")
                        result = await run_operation(session, tool_names, candidates, args, log, tables)
                    except Exception as e:  # 引数の形が崩れていてもLLMに伝えて直させる
                        result = f"エラー: {e}"
                    if not result.startswith("エラー"):
                        executed = True
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            return "実行回数の上限に達しました。質問を具体的にしてみてください。"


# ===== 画面 =====
TITLE = "Prism Central に聞いてみる"
SUBTITLE = "Nutanix MCP Serverを使ってみる"
st.set_page_config(page_title=f"{TITLE} - {SUBTITLE}", page_icon="💬", layout="wide")  # タブ名・横幅いっぱい


def show_table(rows):
    """表を全行表示する（内側のスクロールが出ない高さにする）。並べ替え・検索・CSV保存ができる"""
    st.dataframe(rows, use_container_width=True, hide_index=True,
                 height=min(35 * (len(rows) + 1) + 3, 2000))
st.title(TITLE)
st.markdown(f"**{SUBTITLE}**")
from datetime import datetime
from zoneinfo import ZoneInfo

# app.py自身の更新日時（ファイルを置き換えれば自動で変わる）
APP_UPDATED = datetime.fromtimestamp(os.path.getmtime(__file__), ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
st.caption(f"LLM: {LLM_MODEL} / {LLM_BASE_URL}　|　app.py 更新: {APP_UPDATED}")

if "history" not in st.session_state:
    st.session_state.history = []

for m in st.session_state.history:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for t in m.get("tables", []):
            show_table(t)

if question := st.chat_input("例：停止中のVMを表で出して"):
    st.chat_message("user").markdown(question)
    st.session_state.history.append({"role": "user", "content": question})

    with st.chat_message("assistant"):
        log = st.status("考え中...", expanded=True)
        tables = []
        try:
            answer = asyncio.run(ask(st.session_state.history, log, tables))
            log.update(label="完了", state="complete", expanded=False)
        except Exception as e:
            answer = f"エラーが発生しました: {e}"
            log.update(label="エラー", state="error")
        st.markdown(answer)
        for t in tables:  # 表はLLMに書かせず、取得したデータをそのまま表示する
            show_table(t)
    st.session_state.history.append({"role": "assistant", "content": answer, "tables": tables})
