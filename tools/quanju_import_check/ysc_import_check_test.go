package core

// 用全剧视界（guoappxxx/native/core）真实的导入逻辑校验 ysc-helper 导出的 JSON。
// 用法见 tools/quanju_import_check/README.md：把本文件复制到 native/core/ 后运行
//   YSC_EXPORT_FILE=/path/quanju.json YSC_RESULT_FILE=/tmp/r.json go test -run TestYscHelperExportImport ./core/

import (
	"encoding/json"
	"os"
	"testing"
)

type yscRecord struct {
	Name    string `json:"name"`
	Base    string `json:"base"`
	Type    string `json:"type"`
	HasRule bool   `json:"hasRule"`
	RuleOK  bool   `json:"ruleOk"`
}

type yscSummary struct {
	Added     int         `json:"added"`
	Updated   int         `json:"updated"`
	Skipped   []string    `json:"skipped"`
	Failed    []string    `json:"failed"`
	Records   []yscRecord `json:"records"`
	Persisted int         `json:"persisted"`
	ReExport  int         `json:"reExportCount"`
}

func TestYscHelperExportImport(t *testing.T) {
	path := os.Getenv("YSC_EXPORT_FILE")
	if path == "" {
		t.Skip("YSC_EXPORT_FILE 未设置")
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	engine := &nativeEngine{customRegistry: newCustomMaccmsRegistry(dir)}
	result, err := engine.importCustomMaccmsSources(string(body))
	if err != nil {
		t.Fatalf("导入失败: %v", err)
	}
	summary := yscSummary{Added: len(result.Added), Updated: len(result.Updated), Skipped: result.Skipped, Failed: result.Failed}
	for _, rec := range engine.customMaccmsSources() {
		ok := false
		if rec.Rule != "" {
			_, ok = parseXBPQRule(rec.Rule)
		}
		summary.Records = append(summary.Records, yscRecord{Name: rec.Name, Base: rec.Base, Type: rec.Type, HasRule: rec.Rule != "", RuleOK: ok})
	}
	// 重新从磁盘加载，确认注册表真的落盘（registry.save 超过上限时会静默不写）。
	reloaded := newCustomMaccmsRegistry(dir)
	reloaded.load()
	summary.Persisted = len(reloaded.all())
	exported, err := engine.exportCustomMaccmsSources()
	if err == nil {
		var env struct {
			Count int `json:"count"`
		}
		_ = json.Unmarshal([]byte(exported), &env)
		summary.ReExport = env.Count
	}
	out, _ := json.MarshalIndent(summary, "", "  ")
	if target := os.Getenv("YSC_RESULT_FILE"); target != "" {
		_ = os.WriteFile(target, out, 0o644)
	}
	t.Logf("%s", out)
}
