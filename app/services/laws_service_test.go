package services

import (
	"context"
	"testing"

	"github.com/maoxiaoyue/hypgo/pkg/logger"

	"legagent/internal/moj"
)

// TestBuildLawRows_KeepsOrderAndMetadata 為 happy path：條文順序與法規中繼資料完整保留。
func TestBuildLawRows_KeepsOrderAndMetadata(t *testing.T) {
	laws, err := moj.ParseFile("../../internal/moj/testdata/sample.xml", moj.Filter{Names: []string{"立法院議事規則"}})
	if err != nil || len(laws) != 1 {
		t.Fatalf("parse: %v %d", err, len(laws))
	}
	catalog, rows, err := BuildLawRows(laws[0])
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if catalog.TableName != "laws.law_a0020059" || catalog.Name != "立法院議事規則" || catalog.ArticleCount != 3 || catalog.Repealed {
		t.Fatalf("catalog: %+v", catalog)
	}
	if len(rows) != 3 || rows[0].Seq != 1 || rows[2].Seq != 3 || rows[2].ArticleNo != "87-1" || rows[1].Chapter != "第二章 委員提案" {
		t.Fatalf("rows: %+v", rows)
	}
}

// TestBuildLawRows_NoArticles 為邊界案例：沒有條文的法規仍要有目錄列，表可以是空的。
func TestBuildLawRows_NoArticles(t *testing.T) {
	catalog, rows, err := BuildLawRows(moj.Law{PCode: "A0000099", Name: "只有附件的法規", Repealed: true})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if len(rows) != 0 || catalog.ArticleCount != 0 || !catalog.Repealed {
		t.Fatalf("catalog=%+v rows=%+v", catalog, rows)
	}
}

// TestLawTableName_RejectsUnsafeCodes 為錯誤案例：代碼會組進 DDL，不合格式必須拒絕。
func TestLawTableName_RejectsUnsafeCodes(t *testing.T) {
	if got, err := LawTableName("B0000001"); err != nil || got != "law_b0000001" {
		t.Fatalf("valid code: %q %v", got, err)
	}
	for _, code := range []string{"", "b0000001", "B000001", "B00000011", `B0"; DROP`, "民法民法民法民法"} {
		if _, err := LawTableName(code); err == nil {
			t.Errorf("code %q should be rejected", code)
		}
	}
}

// TestLawsImport_RejectsBadSource 為錯誤案例：來源有問題時，在碰資料庫之前就要拒絕。
func TestLawsImport_RejectsBadSource(t *testing.T) {
	log, _ := logger.New("error", "stderr", nil, false)
	s := NewLawsService(nil, log) // nil db：若驗證沒有先擋下，這裡會直接 panic

	cases := map[string][]moj.Law{
		"invalid code":   {{PCode: "bad", Name: "x"}},
		"duplicate code": {{PCode: "A0000001", Name: "x"}, {PCode: "A0000001", Name: "y"}},
	}
	for name, laws := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := s.Import(context.Background(), laws)
			assertAppErr(t, err, ErrLawsInvalidSource)
		})
	}
}
