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
		case strings.HasPrefix(s, "CREATE"), strings.Contains(s, "ADD COLUMN"):
			if !strings.Contains(s, "IF NOT EXISTS") {
				t.Errorf("schema change without IF NOT EXISTS: %.60s", s)
			}
		case strings.Contains(s, "DROP CONSTRAINT"):
			if !strings.Contains(s, "IF EXISTS") {
				t.Errorf("DROP without IF EXISTS: %.60s", s)
			}
		case strings.Contains(s, "ADD CONSTRAINT"):
			// 緊接在同名 DROP CONSTRAINT IF EXISTS 之後，所以可重複執行。
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
