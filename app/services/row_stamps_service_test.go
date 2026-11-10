package services

import (
	"strings"
	"testing"
)

// TestStampTables_CoverEverySchemaTable 確認建表清單裡的每張表都在遷移清單中，新增表時不會漏掉時間欄位。
func TestStampTables_CoverEverySchemaTable(t *testing.T) {
	listed := map[string]bool{}
	for _, s := range StampTables {
		key := s.Schema + "." + s.Table
		if listed[key] {
			t.Errorf("%s listed twice", key)
		}
		listed[key] = true
	}
	want := []string{"public.legislators", "public.legislator_terms", "public.council_rules", "laws.catalog"}
	for _, table := range ProceedingsTables {
		want = append(want, "proceedings."+table)
	}
	for _, table := range NewsTables {
		want = append(want, "news."+table)
	}
	for _, key := range want {
		if !listed[key] {
			t.Errorf("%s has no row stamp entry", key)
		}
	}
}

// TestStampTables_BackfillTargetsOwnTable 確認回填語句改的是自己那張表，避免複製貼上時寫錯表。
func TestStampTables_BackfillTargetsOwnTable(t *testing.T) {
	for _, s := range StampTables {
		if s.Backfill == "" {
			continue
		}
		if !strings.HasPrefix(s.Backfill, "UPDATE "+s.Schema+"."+s.Table+" ") {
			t.Errorf("%s.%s backfill updates another table: %.60s", s.Schema, s.Table, s.Backfill)
		}
		if !strings.Contains(s.Backfill, "created_at =") {
			t.Errorf("%s.%s backfill does not set created_at", s.Schema, s.Table)
		}
	}
}

// TestStampFunction_KeepsCreatedAtAndSkipsNoOps 確認觸發器不讓 created_at 被改寫，且內容沒變時不動 updated_at。
func TestStampFunction_KeepsCreatedAtAndSkipsNoOps(t *testing.T) {
	for _, part := range []string{
		"NEW.created_at := OLD.created_at",
		"NEW.updated_at := OLD.updated_at",
		"IF NEW IS DISTINCT FROM OLD THEN",
	} {
		if !strings.Contains(stampFunctionSQL, part) {
			t.Errorf("stamp function is missing %q", part)
		}
	}
}
