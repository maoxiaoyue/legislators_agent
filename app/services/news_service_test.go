package services

import (
	"strings"
	"testing"
)

// TestNewsSchema_DefinesEveryTable 確認清單中的每張表都有建表語句。
func TestNewsSchema_DefinesEveryTable(t *testing.T) {
	all := strings.Join(newsSchemaStatements, "\n")
	for _, table := range NewsTables {
		if !strings.Contains(all, "CREATE TABLE IF NOT EXISTS news."+table+" (") {
			t.Errorf("table %s has no CREATE statement", table)
		}
	}
}

// TestNewsSchema_IsRepeatable 確認每個語句都能重跑。
func TestNewsSchema_IsRepeatable(t *testing.T) {
	for _, stmt := range newsSchemaStatements {
		s := strings.TrimSpace(stmt)
		if !strings.HasPrefix(s, "CREATE") || !strings.Contains(s, "IF NOT EXISTS") {
			t.Errorf("statement is not repeatable: %.60s", s)
		}
	}
}

// TestNewsSchema_ExcludesMainlandRegion 確認地區欄位不允許 CN，因為大陸網域在匯入時就排除。
func TestNewsSchema_ExcludesMainlandRegion(t *testing.T) {
	all := strings.Join(newsSchemaStatements, "\n")
	if !strings.Contains(all, "source_region IN ('TW', 'HK', 'foreign')") {
		t.Error("source_region check must list exactly TW, HK and foreign")
	}
	if strings.Contains(all, "'CN'") {
		t.Error("source_region must not allow CN")
	}
}

// TestNewsSchema_HasNoVectors 確認這裡不存向量；向量由另一個服務寫入 Cassandra。
func TestNewsSchema_HasNoVectors(t *testing.T) {
	all := strings.ToLower(strings.Join(newsSchemaStatements, "\n"))
	for _, word := range []string{"vector", "embedding"} {
		if strings.Contains(all, word) {
			t.Errorf("schema should not contain %q", word)
		}
	}
}

// TestNewsSchema_SourcesRegionMatchesArticles 確認來源清單與文章用同一組地區值，兩表才能對照。
func TestNewsSchema_SourcesRegionMatchesArticles(t *testing.T) {
	all := strings.Join(newsSchemaStatements, "\n")
	check := "source_region text NOT NULL CHECK (source_region IN ('TW', 'HK', 'foreign'))"
	if n := strings.Count(all, check); n != 2 {
		t.Errorf("expected the same region check on articles and sources, found %d", n)
	}
}

// TestNewsSchema_SourcesHasNoForeignKey 確認文章不綁外鍵到來源，新網域匯入時不會被擋下。
func TestNewsSchema_SourcesHasNoForeignKey(t *testing.T) {
	all := strings.Join(newsSchemaStatements, "\n")
	if strings.Contains(all, "REFERENCES news.sources") {
		t.Error("articles must not reference news.sources")
	}
}
