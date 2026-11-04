package services

import (
	"os"
	"path/filepath"
	"testing"

	"legagent/app/models"
)

func writeRoster(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "legislators.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write roster: %v", err)
	}
	return path
}

// TestLoadLegislatorRoster_CommittedFile 確認版控中的名單檔通過驗證，並與來源 PDF 的統計一致。
func TestLoadLegislatorRoster_CommittedFile(t *testing.T) {
	roster, err := LoadLegislatorRoster("../../data/legislators/legislators.json")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	legislators, terms := BuildLegislatorRows(roster)
	if len(legislators) != 774 || len(terms) != 1648 || len(roster.Terms) != 10 {
		t.Fatalf("got %d legislators, %d records, %d terms", len(legislators), len(terms), len(roster.Terms))
	}
}

// TestBuildLegislatorRows_UsesLatestName 為邊界案例：屆次亂序時，主檔仍取最近一屆的姓名寫法。
func TestBuildLegislatorRows_UsesLatestName(t *testing.T) {
	roster := &models.LegislatorRoster{Terms: []models.LegislatorTermSet{
		{Term: 10, Members: []models.LegislatorEntry{{No: "0964", Name: "新寫法"}}},
		{Term: 9, Members: []models.LegislatorEntry{{No: "0964", Name: "舊寫法", Note: "附註"}, {No: "0022", Name: "王金平"}}},
	}}
	legislators, terms := BuildLegislatorRows(roster)
	if len(legislators) != 2 || legislators[0].No != "0022" || legislators[1].Name != "新寫法" {
		t.Fatalf("legislators: %+v", legislators)
	}
	if len(terms) != 3 || terms[0].Term != 9 || terms[0].Note != "附註" || terms[2].Term != 10 {
		t.Fatalf("terms: %+v", terms)
	}
}

// TestLoadLegislatorRoster_Rejects 為錯誤案例：不一致的名單必須被擋下，不能寫進資料庫。
func TestLoadLegislatorRoster_Rejects(t *testing.T) {
	member := `{"no":"0022","name":"王金平"}`
	cases := map[string]string{
		"empty":        `{"total_records":0,"unique_legislators":0,"terms":[]}`,
		"count":        `{"total_records":1,"unique_legislators":1,"terms":[{"term":2,"count":2,"members":[` + member + `]}]}`,
		"total":        `{"total_records":9,"unique_legislators":1,"terms":[{"term":2,"count":1,"members":[` + member + `]}]}`,
		"duplicate":    `{"total_records":2,"unique_legislators":1,"terms":[{"term":2,"count":2,"members":[` + member + `,` + member + `]}]}`,
		"bad number":   `{"total_records":1,"unique_legislators":1,"terms":[{"term":2,"count":1,"members":[{"no":"22","name":"王金平"}]}]}`,
		"missing name": `{"total_records":1,"unique_legislators":1,"terms":[{"term":2,"count":1,"members":[{"no":"0022","name":""}]}]}`,
		"bad term":     `{"total_records":1,"unique_legislators":1,"terms":[{"term":0,"count":1,"members":[` + member + `]}]}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := LoadLegislatorRoster(writeRoster(t, content))
			assertAppErr(t, err, ErrLegislatorsInvalidRoster)
		})
	}

	_, err := LoadLegislatorRoster(writeRoster(t, `not json`))
	assertAppErr(t, err, ErrLegislatorsFileUnreadable)
	_, err = LoadLegislatorRoster("does-not-exist.json")
	assertAppErr(t, err, ErrLegislatorsFileUnreadable)
}
