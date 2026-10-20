# legislators_agent

建立一個依照台灣議事規則、行政程序等法律，可提供立法委員問政、服務、質詢等專門服務的 Agent。
法律複雜繁多，但這正是大語言模型擅長的區域。調用現行法律、定義文字其實不難，難在普及以及人與人的接觸也要計算在內。
台灣是有人情味的地方，所以這個 Agent 不只提供建議，還要有溫度。

## 目前進度：議事規則資料 + 向量資料庫

第一階段先把「立法院議事規則」及其相關法規建成可語意檢索的向量索引。

### 資料來源（唯一權威）

法務部「全國法規資料庫」公開資料，政府資料開放授權條款第 1 版，每月更新：

| 資料集 | 內容 | 下載 |
|---|---|---|
| 中文法規_法律資料檔（[data.gov.tw/dataset/18289](https://data.gov.tw/dataset/18289)） | 立法院組織法、職權行使法、各委員會組織法、立法委員行為法… | `FalV.zip`（約 6 MB） |
| 中文法規_命令資料檔（[data.gov.tw/dataset/18290](https://data.gov.tw/dataset/18290)） | **立法院議事規則**、程序委員會組織規程、議場規則、秘密會議規則、點名表決辦法… | `MingLing.zip`（約 25 MB） |

預設以「法規類別」前綴 `立法`（立法院主管）篩選並排除廢止法規，共約 40 部法規；可在 `config/config.yaml` 的 `rules` 區段調整。

### 資料流

```
官方 XML (data/raw, 快取)
  → internal/moj   串流解析、章節歸屬、條號正規化（第 87-1 條 → 87-1）、換行還原
  → data/laws/<pcode>.json + index.json   結構化資料（入版控，可離線重建索引）
  → app/services.RulesService.Index   一條一 chunk：「法規名 第N條（章節）--- 本文」
  → internal/embed   OpenAI 相容 / Ollama / lexical 備援
  → internal/vectorstore (chromem-go, data/vectors)   內嵌式向量庫，cosine 相似度
  → gRPC LegagentService/SearchRules、ListRuleLaws
```

### 建置索引

```bash
# 1. 選擇 embedding 供應者（擇一）
#    本地 Ollama（建議，繁中效果佳）：ollama pull bge-m3
export EMBEDDING_PROVIDER=ollama EMBEDDING_MODEL=bge-m3
#    或 OpenAI 相容 API（OpenAI / Voyage / LM Studio / vLLM）
export EMBEDDING_PROVIDER=openai EMBEDDING_BASE_URL=https://api.openai.com/v1 EMBEDDING_MODEL=text-embedding-3-small EMBEDDING_API_KEY=...
#    或離線備援（只有字面相似，無語意；供測試）
export EMBEDDING_PROVIDER=lexical

# 2. 下載（30 天內快取）→ 解析 → 寫 data/laws → 建索引，並試查一次
make ingest
go run . ingest -query "法律案要幾個人連署"

# 3. 更換 embedding 模型後，不必重新下載，直接以 data/laws 重建
make reindex
```

索引會記住建立時的 provider/model（`data/vectors/ly_rules.meta.json`）；設定不一致時啟動會直接報錯，避免混用向量空間。

### 啟動與查詢

```bash
make proto   # 修改 .proto 後重新產生（使用 buf + protoc-gen-go，不需安裝 protoc）
make run
grpcurl -plaintext -d '{"query":"法律案要幾個人連署","top_k":3}' localhost:9090 legagentpb.LegagentService/SearchRules
```

### 驗證

```bash
go build ./... && go vet ./... && go test ./...
```

## 架構

HypGo gRPC 專案（MVC）：`app/models` DTO、`app/services` 業務邏輯 + Error Catalog（E3xxx = rules）、`app/rpc` 薄介面層 + Schema-first 註冊、`internal/` 基礎設施（moj / embed / vectorstore）、根目錄 `ingest.go` 為資料建置子命令（`go run . ingest`）。
