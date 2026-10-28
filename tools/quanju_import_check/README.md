# 用全剧视界真实导入逻辑校验导出文件

`ysc_import_check_test.go` 是一个放进全剧视界 Go 核心包（`package core`）里运行的测试桩：
它调用 `importCustomMaccmsSources` 导入 ysc-helper 生成的 JSON，输出新增/失败/跳过统计、落盘条数与再导出条数。

```bash
git clone --depth 1 https://github.com/wokaotianshi123/guoappxxx /tmp/guoappxxx
cp ysc_import_check_test.go /tmp/guoappxxx/native/core/
cd /tmp/guoappxxx/native
YSC_EXPORT_FILE=/path/to/duanju-custom-sources-2026-10-28.json YSC_RESULT_FILE=/tmp/result.json \
  go test -count=1 -run TestYscHelperExportImport -v ./core/
cat /tmp/result.json
```

`tests/test_quanju_export.py::test_real_quanju_go_importer_accepts_export` 在设置 `QUANJU_CORE_DIR` 时会自动完成以上步骤。
