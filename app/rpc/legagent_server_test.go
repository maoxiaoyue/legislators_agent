package rpc

import (
	"context"
	"net"
	"path/filepath"
	"testing"

	"github.com/maoxiaoyue/hypgo/pkg/contract"
	"github.com/maoxiaoyue/hypgo/pkg/logger"
	"github.com/maoxiaoyue/hypgo/pkg/router"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	pb "legagent/app/proto/legagentpb"
	"legagent/app/services"
	"legagent/internal/embed"
	"legagent/internal/moj"
	"legagent/internal/vectorstore"
)

// TestContracts 驗證所有 schema 註冊的 gRPC 方法皆宣告 Input/Output 型別。
func TestContracts(t *testing.T) {
	RegisterSchemas()
	contract.TestAll(t, router.New())
}

func startServer(t *testing.T, ingest bool) pb.LegagentServiceClient {
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
	rules := services.NewRulesService(store, filepath.Join(t.TempDir(), "laws"), log, 2)
	if ingest {
		if _, err := rules.IngestXML(context.Background(), []string{"../../internal/moj/testdata/sample.xml"},
			moj.Filter{CategoryPrefixes: []string{"立法"}, ExcludeRepealed: true}, false); err != nil {
			t.Fatalf("ingest: %v", err)
		}
	}

	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	pb.RegisterLegagentServiceServer(srv, NewLegagentServer(rules, log))
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	conn, err := grpc.NewClient("passthrough:///bufconn",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return pb.NewLegagentServiceClient(conn)
}

// TestSearchRules_HappyPath 驗證 gRPC 端到端檢索回傳條號與來源。
func TestSearchRules_HappyPath(t *testing.T) {
	c := startServer(t, true)
	resp, err := c.SearchRules(context.Background(), &pb.SearchRulesRequest{Query: "法律案連署人數", TopK: 2})
	if err != nil {
		t.Fatalf("SearchRules: %v", err)
	}
	if resp.Total != 2 || resp.Hits[0].ArticleNo != "8" || resp.Hits[0].LawName != "立法院議事規則" || resp.Hits[0].SourceUrl == "" {
		t.Fatalf("unexpected hits: %+v", resp.Hits)
	}
	laws, err := c.ListRuleLaws(context.Background(), &pb.ListRuleLawsRequest{})
	if err != nil || laws.Total != 1 || laws.Laws[0].Pcode != "A0020059" {
		t.Fatalf("ListRuleLaws: %v %+v", err, laws)
	}
}

// TestSearchRules_Boundary 驗證 top_k=0 走預設且被文件數截斷。
func TestSearchRules_Boundary(t *testing.T) {
	c := startServer(t, true)
	resp, err := c.SearchRules(context.Background(), &pb.SearchRulesRequest{Query: "總則", TopK: 0})
	if err != nil || resp.Total != 3 {
		t.Fatalf("top_k=0: %v %+v", err, resp)
	}
}

// TestSearchRules_Errors 驗證錯誤轉譯：空查詢 → InvalidArgument、空索引 → Unavailable。
func TestSearchRules_Errors(t *testing.T) {
	c := startServer(t, false)
	_, err := c.SearchRules(context.Background(), &pb.SearchRulesRequest{Query: ""})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty query: want InvalidArgument, got %v", err)
	}
	_, err = c.SearchRules(context.Background(), &pb.SearchRulesRequest{Query: "任何問題"})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("empty index: want Unavailable, got %v", err)
	}
	_, err = c.ListRuleLaws(context.Background(), &pb.ListRuleLawsRequest{})
	if status.Code(err) != codes.Internal {
		t.Fatalf("missing index.json: want Internal, got %v", err)
	}
}
