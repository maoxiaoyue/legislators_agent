package services

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	hyperrors "github.com/maoxiaoyue/hypgo/pkg/errors"
	"github.com/maoxiaoyue/hypgo/pkg/logger"

	"legagent/app/models"
	"legagent/internal/embed"
	"legagent/internal/moj"
	"legagent/internal/vectorstore"
)

func newTestService(t *testing.T) *RulesService {
	t.Helper()
	fn, desc, err := embed.New(embed.Config{Provider: embed.ProviderLexical})
	if err != nil {
		t.Fatalf("embedder: %v", err)
	}
	store, err := vectorstore.NewChromem(vectorstore.Options{Collection: "test", Metadata: map[string]string{"embedding": desc}}, fn)
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	log, _ := logger.New("error", "stderr", nil, false)
	return NewRulesService(store, filepath.Join(t.TempDir(), "laws"), log, 2)
}

func ingestSample(t *testing.T, s *RulesService) *models.IngestStats {
	t.Helper()
	stats, err := s.IngestXML(context.Background(), []string{"../../internal/moj/testdata/sample.xml"},
		moj.Filter{CategoryPrefixes: []string{"立法"}, ExcludeRepealed: true}, false)
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	return stats
}

// TestRulesService_IngestAndSearch 為 happy path：ingest 後查詢能命中正確條文並帶出處。
func TestRulesService_IngestAndSearch(t *testing.T) {
	s := newTestService(t)
	stats := ingestSample(t, s)
	if stats.Laws != 1 || stats.Articles != 3 || stats.Chunks != 3 {
		t.Fatalf("unexpected stats: %+v", stats)
	}

	resp, err := s.Search(context.Background(), models.SearchRulesReq{Query: "法律案連署人數"})
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if resp.Total == 0 || resp.Hits[0].ArticleNo != "8" {
		t.Fatalf("expected article 8 first, got %+v", resp.Hits)
	}
	h := resp.Hits[0]
	if h.LawName != "立法院議事規則" || h.PCode != "A0020059" || h.Label != "第 8 條" || h.SourceURL == "" {
		t.Errorf("hit metadata incomplete: %+v", h)
	}
	if h.Content == "" || h.Content[0] == '\n' {
		t.Errorf("content should be bare article text, got %q", h.Content)
	}

	laws, err := s.ListLaws(context.Background())
	if err != nil || laws.Total != 1 || laws.Laws[0].ArticleCount != 3 {
		t.Fatalf("list laws: %v %+v", err, laws)
	}
}

// TestRulesService_SearchBoundaries 為邊界案例：TopK=0 用預設、TopK 超過文件數被截斷、法規過濾生效。
func TestRulesService_SearchBoundaries(t *testing.T) {
	s := newTestService(t)
	ingestSample(t, s)
	ctx := context.Background()

	resp, err := s.Search(ctx, models.SearchRulesReq{Query: "總則", TopK: 0})
	if err != nil || resp.Total != 3 {
		t.Fatalf("topk=0 should return all 3 (default 5 clamped to count): %v %+v", err, resp)
	}
	resp, err = s.Search(ctx, models.SearchRulesReq{Query: "總則", TopK: 50})
	if err != nil || resp.Total != 3 {
		t.Fatalf("topk=50 should clamp to 3: %v %+v", err, resp)
	}
	resp, err = s.Search(ctx, models.SearchRulesReq{Query: "總則", TopK: 1, LawName: "不存在的法規"})
	if err != nil || resp.Total != 0 {
		t.Fatalf("unknown law filter should yield 0 hits: %v %+v", err, resp)
	}
	resp, err = s.Search(ctx, models.SearchRulesReq{Query: "總則", TopK: 1, PCode: "A0020059"})
	if err != nil || resp.Total != 1 {
		t.Fatalf("pcode filter: %v %+v", err, resp)
	}
}

// TestRulesService_SearchErrors 為錯誤案例：空查詢、TopK 超界、空索引。
func TestRulesService_SearchErrors(t *testing.T) {
	s := newTestService(t)
	ctx := context.Background()

	_, err := s.Search(ctx, models.SearchRulesReq{Query: "   "})
	assertAppErr(t, err, ErrRulesInvalidRequest)
	_, err = s.Search(ctx, models.SearchRulesReq{Query: "x", TopK: 51})
	assertAppErr(t, err, ErrRulesInvalidRequest)
	_, err = s.Search(ctx, models.SearchRulesReq{Query: "尚未建立索引"})
	assertAppErr(t, err, ErrRulesIndexEmpty)

	_, err = s.IngestXML(ctx, []string{"does-not-exist.xml"}, moj.Filter{}, false)
	assertAppErr(t, err, ErrRulesIngestFailed)
}

// TestRulesService_ReindexFromJSON 驗證離線 reindex 走 data_dir JSON 可重建相同索引。
func TestRulesService_ReindexFromJSON(t *testing.T) {
	s := newTestService(t)
	ingestSample(t, s)
	stats, err := s.Reindex(context.Background(), true)
	if err != nil || stats.Chunks != 3 || s.store.Count() != 3 {
		t.Fatalf("reindex: %v %+v count=%d", err, stats, s.store.Count())
	}
}

func assertAppErr(t *testing.T, err error, want *hyperrors.AppError) {
	t.Helper()
	var got *hyperrors.AppError
	if !errors.As(err, &got) || got.Code != want.Code {
		t.Fatalf("want %s, got %v", want.Code, err)
	}
}
