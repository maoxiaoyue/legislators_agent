package services

import (
	"strings"
	"testing"
)

// TestProceedingsSchema_DefinesEveryTable 確認清單中的每張表都有建表語句。
func TestProceedingsSchema_DefinesEveryTable(t *testing.T) {
	all := strings.Join(proceedingsSchemaStatements, "\n")
	for _, table := range ProceedingsTables {
		if !strings.Contains(all, "CREATE TABLE IF NOT EXISTS proceedings."+table+" (") {
			t.Errorf("table %s has no CREATE statement", table)
		}
	}
}

// TestProceedingsSchema_IsRepeatable 確認語句可重複執行，重跑不會失敗或產生重複資料。
func TestProceedingsSchema_IsRepeatable(t *testing.T) {
	for _, stmt := range proceedingsSchemaStatements {
		s := strings.TrimSpace(stmt)
		switch {
		case strings.HasPrefix(s, "CREATE"):
			if !strings.Contains(s, "IF NOT EXISTS") {
				t.Errorf("CREATE without IF NOT EXISTS: %.60s", s)
			}
		case strings.HasPrefix(s, "INSERT"):
			if !strings.Contains(s, "ON CONFLICT") {
				t.Errorf("INSERT without ON CONFLICT: %.60s", s)
			}
		default:
			t.Errorf("unexpected statement: %.60s", s)
		}
	}
}

// TestProceedingsSchema_HasNoVectors 確認這裡不存向量；向量由另一個服務寫入 Cassandra。
func TestProceedingsSchema_HasNoVectors(t *testing.T) {
	all := strings.ToLower(strings.Join(proceedingsSchemaStatements, "\n"))
	for _, word := range []string{"vector", "embedding"} {
		if strings.Contains(all, word) {
			t.Errorf("schema should not contain %q", word)
		}
	}
}
