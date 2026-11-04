package services

import (
	"strings"
	"testing"
)

// TestLoadCouncilRules_CommittedFile 確認版控中的規則檔有效，且第 25 條的罰則確實標為失效。
func TestLoadCouncilRules_CommittedFile(t *testing.T) {
	set, err := LoadCouncilRules("../../data/council_rules/council_rules.json")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	byKey := map[string]string{}
	for _, r := range set.Rules {
		byKey[r.RuleKey] = r.Status + "/" + r.BindingLevel
		if r.ReviewedOn != set.ReviewedOn {
			t.Errorf("%s: reviewed_on not filled", r.RuleKey)
		}
	}
	for _, key := range []string{"A0020058-25-3", "A0020058-25-5-7", "A0020058-25-8", "A0020058-25-9"} {
		if byKey[key] != "失效/無" {
			t.Errorf("%s: got %q, want 失效/無", key, byKey[key])
		}
	}
	if byKey["A0020058-25-1"] != "有效但經限縮/中" {
		t.Errorf("反質詢: got %q", byKey["A0020058-25-1"])
	}
}

// TestLoadCouncilRules_Rejects 為錯誤與邊界案例：矛盾或缺依據的紀錄必須被擋下。
func TestLoadCouncilRules_Rejects(t *testing.T) {
	rule := func(key, level, status, basis string) string {
		return `{"rule_key":"` + key + `","law_name":"法","article_ref":"第 1 條","subject":"事項","applies_to":"委員",` +
			`"requirement":"內容","penalty":"無","enforcement":"無","binding_level":"` + level + `","binding_reason":"理由",` +
			`"status":"` + status + `","status_basis":"` + basis + `","source_url":"https://example.test"}`
	}
	wrap := func(rules ...string) string {
		return `{"reviewed_on":"2026-11-05","rules":[` + strings.Join(rules, ",") + `]}`
	}
	cases := map[string]string{
		"no rules":             wrap(),
		"bad date":             `{"reviewed_on":"昨天","rules":[` + rule("a", "強", "有效", "") + `]}`,
		"duplicate key":        wrap(rule("a", "強", "有效", ""), rule("a", "弱", "有效", "")),
		"bad level":            wrap(rule("a", "很強", "有效", "")),
		"bad status":           wrap(rule("a", "強", "可能有效", "")),
		"void keeps binding":   wrap(rule("a", "中", "失效", "判決")),
		"valid without force":  wrap(rule("a", "無", "有效", "")),
		"void without basis":   wrap(rule("a", "無", "失效", "")),
		"narrowed needs basis": wrap(rule("a", "弱", "有效但經限縮", "")),
		"missing field":        wrap(strings.Replace(rule("a", "強", "有效", ""), `"penalty":"無"`, `"penalty":""`, 1)),
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := LoadCouncilRules(writeRoster(t, content))
			assertAppErr(t, err, ErrCouncilRulesInvalid)
		})
	}

	_, err := LoadCouncilRules(writeRoster(t, "not json"))
	assertAppErr(t, err, ErrCouncilRulesFileUnreadable)

	if _, err := LoadCouncilRules(writeRoster(t, wrap(rule("ok", "無", "失效", "判決"), rule("ok2", "弱", "有效但經限縮", "判決")))); err != nil {
		t.Fatalf("valid rules rejected: %v", err)
	}
}
