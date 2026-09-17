# Cursor Usage Tracker

純本機、metadata-only 的 Cursor workload 與 token 估算器。它適合觀察
requests、模型分布、repo 分布與成本趨勢；它不是 Cursor 官方帳單的替代品。

## 快速開始

在專案目錄安裝 CLI：

```bash
cd /path/to/cursor-usage-tracker
uv tool install .
cursor-usage init
cursor-usage install-hook
cursor-usage doctor
```

安裝完成後正常使用 Cursor。Tracker 只會收集安裝 hook 之後的新 requests；
不需要常駐 daemon，也不需要保持此專案開啟。

日常查看方式：

```bash
# 補強 Cursor 本機有提供的模型資訊
cursor-usage sync

# 查看今天的摘要
cursor-usage report

# 查看最完整的每日表格
cursor-usage report --daily
```

如果 Auto 只留下 `default`，先設定「僅供參考」的模型假設：

```bash
cursor-usage model
```

選單會要求輸入編號或 model slug。設定只對執行指令後的新 requests 生效，
不會改寫先前紀錄。

## 能與不能觀察的資料

Tracker 透過 Cursor hooks 記錄每個 generation 的 selected model、mode、
thinking/model parameters、可見文字長度、附件數、工具輸出長度、subagent
模型與執行狀態。原始 prompt、assistant response、tool payload、附件內容、
email 與完整檔案路徑不會寫入 tracker database。

Conversation-aware estimator 會把同一 conversation 先前可見的 prompt、附件、
tool result、assistant response 與 visible thinking token 數帶入下一個 turn，
並在每次 tool result 後估算下一次 model call 的累積 input。升級後第一次遇到
既有 conversation 時，會從 metadata 重建可觀察 context；`preCompact` 會重設
ledger，因為 compact 後的 server summary 不可見。整個 ledger 只保存 token
聚合值，不保存原文。`afterAgentThought` 只保存 visible thinking token
estimate、duration 與 block count，也無法觀察 hidden reasoning。

`sync` 會以唯讀方式讀取 Cursor 的 `state.vscdb`，以 bubble 的 `requestId`
對應 hook `generation_id`。若 `modelInfo.modelName` 有明確模型，會補上
resolved model；`default`、`auto` 與缺值維持 unresolved。這個未公開 schema
可能隨 Cursor 版本改變，而且部分版本不會在 Auto request 留下實際模型。

無法從本機可靠取得：

- Cursor server 組合的完整 context 與 hidden instructions
- server-side trimming、summarization 與 cache read/write tokens
- 沒有出現在 hook 或本機資料庫內的模型 routing
- billing-grade token 與費用

未校準時會顯示 low-confidence loop-aware point estimate 與 observable lower
bound，但不提供有界的 high estimate。Screenshot 與其他明確的非文字工具
結果會另外計數，不套用文字 tokenizer heuristic。校準後才會顯示
p10/p50/p90 型區間，但不保證固定誤差範圍。

## 詳細安裝與資料位置

需要 Python 3.11+ 與 [`uv`](https://docs.astral.sh/uv/)：

```bash
uv tool install .
cursor-usage init
cursor-usage doctor
```

全域 hook 是 opt-in。以下指令會先備份既有 `~/.cursor/hooks.json`，再逐項
合併 tracker hook；它不會移除既有的 Orca 或其他 hooks：

```bash
cursor-usage install-hook
```

重複執行不會加入重複項目。Cursor 會自動重新載入 hook 設定。

預設資料庫位於：

```text
~/Library/Application Support/cursor-usage-tracker/usage.db
```

可用 `CURSOR_USAGE_DB` 與 `CURSOR_STATE_DB` 覆寫 tracker 與 Cursor database
路徑。兩者都應使用絕對路徑。

## 報表使用方式

同步本機模型資訊並查看今天：

```bash
cursor-usage sync
cursor-usage report
cursor-usage report --daily
cursor-usage report --date 2026-09-17
cursor-usage report --start 2026-09-01 --end 2026-10-01 --json
cursor-usage report --start 2026-09-01 --end 2026-10-01 --daily
```

`--start` 包含指定日期，`--end` 不包含指定日期；日期使用本機時區，資料庫
內一律保存帶時區的 UTC timestamp。`--daily` 會以 Date、Repository、Model、
Requests、Observable Input/Output、Excluded Non-text、Estimated Total 與 Cost
欄位輸出終端表格，也可以和 `--json` 一起使用。

每日表格欄位：

- `Requests`：使用者送出的 prompt 次數。
- `Calls`：估算的 model-call 次數；初始 request 為一次，每個 tool result
  會觸發下一次估算 call。
- `Input Est.*`：考慮同一 conversation 的跨 turn context 與 tool loop
  重複輸入後的 point estimate。
- `Output*`：hook 可觀察到的 assistant response 文字估算。
- `Think*`：`afterAgentThought` 可觀察到的 thinking 文字估算，不包含 hidden
  reasoning。
- `Excluded`：Screenshot 等未使用文字 heuristic 的非文字 tool events。
- `Total Est.`：input、output 與可見 thinking 的估算；沒有 calibration 時
  屬於 low confidence。
- `Ref Cost`：以 conversation-aware input estimate 與參考費率計算，不是
  Cursor 帳單；無法反映實際 cache 折扣。

`*` 欄位都不是官方 token telemetry。若要提供給其他程式使用：

```bash
cursor-usage report --daily --json
cursor-usage report --accuracy --json
```

### 預設模型參考

當 Cursor 只留下 `default` 而沒有 Auto resolved model 時，可以手動設定報表
使用的參考模型：

```bash
cursor-usage model
cursor-usage model list
cursor-usage model set cursor-grok-4.6-high-fast
cursor-usage model set gpt-5.6-sol-medium --repo teams-agent
cursor-usage model set claude-opus-5-thinking-high --session CONVERSATION_ID
cursor-usage model show
```

互動選單涵蓋 Cursor Grok 4.6 High Fast、Claude Opus 5 High、GPT-5.6 Sol
Medium、Claude Fable 5 High 與 Gemini 3.8 Flash High。費率來源是
[Cursor Models & Pricing](https://cursor.com/docs/models-and-pricing)，effective
date 為 2026-09-17。

這是手動 configured assumption 與價格參考，不代表 tracker 已證明 Cursor
Auto 實際 routing，也不是帳單 telemetry。Input/output 倍率分別以 Grok 4.6
Standard 的 `$2/M input`、`$6/M output` 為基準；High effort 沒有固定價格
乘數，Fast 與 long-context surcharge 才可能改變單價。

模型設定預設從執行指令的時間開始生效，不會回溯套用歷史 requests。
`--repo` 的優先級高於 global，`--session` 又高於 repo。需要匯入歷史假設時，
可以明確提供帶時區的 `--effective-from`。

### Telemetry 校準與誤差

匯入非 Auto 或其他具有真實 token telemetry 的 CSV/JSONL：

```bash
cursor-usage telemetry import telemetry.jsonl
cursor-usage telemetry profiles
cursor-usage report --accuracy
```

每筆至少需要 `timestamp`、`model`、`input_tokens`、`output_tokens`。可選欄位
為 `request_id`、`effort`、`tool_count`、`attachment_count`、
`cache_read_tokens`、`cache_write_tokens`。例如：

```json
{"request_id":"generation-id","timestamp":"2026-09-17T08:00:00Z","model":"grok-4.6","effort":"high","tool_count":3,"attachment_count":1,"input_tokens":120000,"output_tokens":5000,"cache_read_tokens":90000,"cache_write_tokens":0}
```

CSV 使用相同欄位名稱：

```csv
request_id,timestamp,model,effort,tool_count,attachment_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens
generation-id,2026-09-17T08:00:00Z,grok-4.6,high,3,1,120000,5000,90000,0
```

Tracker 依 model、effort、tool-count bucket 與 attachment-count bucket 建立
p10/p50/p90 calibration profile。`report --accuracy` 只比較可由 `request_id`
配對的資料，輸出每日 input/output WAPE、bias、sample size 與 confidence；
少於 30 筆為 low、30–99 為 medium、100 筆以上為 high。

如果 Accuracy 顯示 `Samples: 0`，代表匯入資料沒有可與 tracker
`generation_id` 配對的 `request_id`，或指定日期範圍內沒有 matched records。

手動保存 Cursor Dashboard snapshot：

```bash
cursor-usage snapshot add \
  --total-events 1224 \
  --events-with-token 125 \
  --events-without-token 1099 \
  --known-input 1000000 \
  --known-output 200000 \
  --known-cache-read 500000 \
  --known-cache-write 10000
```

加入從已知 telemetry 建立的每 request calibration。`low`、`point`、`high`
應來自同一模型的實際分布，並滿足 `low <= point <= high`：

```bash
cursor-usage calibration add grok-4.6 \
  --input-low 80000 --input-point 110000 --input-high 150000 \
  --output-low 3000 --output-point 6000 --output-high 10000 \
  --sample-size 50
```

`cursor-usage model` 的五個 presets 會安裝 effective-dated 官方參考費率。
其他模型或更新後的價格可以用 `rate set` 覆寫／新增每百萬 token 美元費率：

```bash
cursor-usage rate set grok-4.6 \
  --effective-at 2026-09-01T00:00:00Z \
  --input-per-million 1.00 \
  --output-per-million 3.00
```

只有具備 resolved／configured model 與有效費率的 requests 才能顯示
reference cost；具有 calibration 時才會使用校準後的 token range。報表會
另外列出 `uncosted_requests`。

## 指令速查

```text
cursor-usage init                  初始化／升級資料庫 schema
cursor-usage install-hook          備份並合併全域 Cursor hooks
cursor-usage doctor                檢查 tracker DB、Cursor DB 與 hooks
cursor-usage sync                  唯讀同步 Cursor bubble model metadata
cursor-usage report                查看今天摘要
cursor-usage report --daily        查看每日 repo/model 表格
cursor-usage report --accuracy     查看每日 WAPE、bias 與 confidence
cursor-usage model                 互動選擇參考模型
cursor-usage model list            顯示 models、費率與相對倍率
cursor-usage model show            顯示目前 global 參考模型
cursor-usage telemetry import FILE 匯入 CSV/JSONL 真實 telemetry
cursor-usage telemetry profiles    重建並顯示 calibration profiles
```

## 常見狀況

- `Model: unresolved`：Cursor 本機只留下 `default`／`auto`。可使用
  `cursor-usage model` 設定未來 requests 的參考模型，但它不代表實際 routing。
- `Calls` 與 `Requests` 相同：舊 requests 建立時尚未啟用 loop metrics，或該
  request 沒有 tool loop；新 requests 才會有完整資料。
- `Think: 0`：模型沒有提供 visible thought、Cursor 沒觸發
  `afterAgentThought`，或該 request 發生在 hook 更新前。
- `Ref Cost: —`：模型 unresolved，或尚未設定該模型費率。
- `Accuracy Samples: 0`：尚未匯入 telemetry，或 `request_id` 無法配對。

## 開發

```bash
uv sync --extra dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

刪除 tracker database 即可清除所有紀錄。這不會修改 Cursor 的
`state.vscdb`。
