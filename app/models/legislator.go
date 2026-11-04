package models

import "github.com/uptrace/bun"

// Legislator 為一位立委；編號跨屆不變，Name 取其最近一屆的寫法。
type Legislator struct {
	bun.BaseModel `bun:"table:legislators"`

	No   string `bun:"legislator_no,pk,type:varchar(4)" json:"no"`
	Name string `bun:"name,notnull" json:"name"`
}

// LegislatorTerm 為某位立委在某一屆的名單紀錄；同一人各屆的姓名寫法可能不同，故逐屆保存。
type LegislatorTerm struct {
	bun.BaseModel `bun:"table:legislator_terms"`

	Term int    `bun:"term,pk" json:"term"`
	No   string `bun:"legislator_no,pk,type:varchar(4)" json:"no"`
	Name string `bun:"name,notnull" json:"name"`
	Note string `bun:"note,nullzero" json:"note,omitempty"` // 來源資料的差異說明
}

// LegislatorRoster 對應 data/legislators/legislators.json。
type LegislatorRoster struct {
	Source            string              `json:"source"`
	TotalRecords      int                 `json:"total_records"`
	UniqueLegislators int                 `json:"unique_legislators"`
	Terms             []LegislatorTermSet `json:"terms"`
}

// LegislatorTermSet 為一屆的完整名單。
type LegislatorTermSet struct {
	Term    int               `json:"term"`
	Count   int               `json:"count"`
	Members []LegislatorEntry `json:"members"`
}

// LegislatorEntry 為名單檔中的一筆委員資料。
type LegislatorEntry struct {
	No   string `json:"no"`
	Name string `json:"name"`
	Note string `json:"note,omitempty"`
}

// LegislatorImportStats 為一次匯入後資料庫內的實際筆數。
type LegislatorImportStats struct {
	Legislators int `json:"legislators"`
	TermRecords int `json:"term_records"`
	Terms       int `json:"terms"`
}
