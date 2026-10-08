package moj

import (
	"strings"
	"testing"
)

// TestParseFile_FilterAndNormalize 驗證類別過濾、廢止排除、章節歸屬與換行還原。
func TestParseFile_FilterAndNormalize(t *testing.T) {
	laws, err := ParseFile("testdata/sample.xml", Filter{CategoryPrefixes: []string{"立法"}, ExcludeRepealed: true})
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(laws) != 1 {
		t.Fatalf("want 1 law, got %d", len(laws))
	}
	law := laws[0]
	if law.Name != "立法院議事規則" || law.PCode != "A0020059" || law.Nature != NatureOrder || law.Repealed {
		t.Fatalf("unexpected law header: %+v", law)
	}
	if len(law.Articles) != 3 {
		t.Fatalf("want 3 articles, got %d", len(law.Articles))
	}
	a1, a8, a87 := law.Articles[0], law.Articles[1], law.Articles[2]
	if a1.No != "1" || a1.Label != "第 1 條" || a1.Chapter != "第一章 總則" {
		t.Errorf("article 1 normalize: %+v", a1)
	}
	if a8.Chapter != "第二章 委員提案" {
		t.Errorf("article 8 chapter: %q", a8.Chapter)
	}
	wantContent := "立法委員提出之法律案，應有十五人以上之連署；其他提案，除另有規定外，應有十人以上之連署。\n連署人不得發表反對原提案之意見；提案人撤回提案時，應先徵得連署人之同意。"
	if a8.Content != wantContent {
		t.Errorf("article 8 content:\n got %q\nwant %q", a8.Content, wantContent)
	}
	if a87.No != "87-1" || a87.Label != "第 87-1 條" || a87.Chapter != "第二章 委員提案" {
		t.Errorf("article 87-1 normalize: %+v", a87)
	}
}

// TestParse_NoFilterKeepsAll 為邊界案例：空過濾條件保留全部（含廢止）。
func TestParse_NoFilterKeepsAll(t *testing.T) {
	laws, err := ParseFile("testdata/sample.xml", Filter{})
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(laws) != 3 {
		t.Fatalf("want 3 laws, got %d", len(laws))
	}
	if !laws[1].Repealed || laws[1].Articles[0].No != "1" {
		t.Errorf("repealed law not parsed as expected: %+v", laws[1])
	}
}

// TestParse_MalformedXML 為錯誤案例：壞掉的 XML 必須回傳錯誤而非靜默略過。
func TestParse_MalformedXML(t *testing.T) {
	_, err := Parse(strings.NewReader("<LAWS><法規><法規名稱>x</法規名稱>"), Filter{})
	if err == nil {
		t.Fatal("expected error for truncated xml")
	}
}

// TestFilter_NamesMatch 驗證以名稱指定納入。
func TestFilter_NamesMatch(t *testing.T) {
	laws, err := ParseFile("testdata/sample.xml", Filter{Names: []string{"其他機關辦法"}})
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(laws) != 1 || laws[0].PCode != "B0000001" {
		t.Fatalf("names filter failed: %+v", laws)
	}
}
