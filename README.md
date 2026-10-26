# llvm-cov.py

獨立的 Python LLVM coverage 研究套件：使用 ijson 增量解析 JSON，轉成 typed
dataclasses，逐檔寫入 pickle。Python import 名稱是 `llvm_cov`，CLI 是
`llvm-cov-py`。不依賴 OrionDB、SQLite、Git 或原始碼檔案；filename 保持輸入值，
不排除 generated files、系統路徑或特定程式語言。

## 安裝與測試

需要 Python 3.11+。只有直接執行 export 時需要安裝 LLVM 工具。

```sh
task setup
task test
task example
```

或者：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## CLI

從既有 JSON 或 stdin 增量解析：

```sh
.venv/bin/llvm-cov-py parse coverage.json -o coverage.pkl --test-name my-test
llvm-cov export ./program -instr-profile=coverage.profdata \
  -skip-expansions -skip-functions | \
  .venv/bin/llvm-cov-py parse - -o coverage.pkl
```

需要確認 exporter exit code 時，使用內建 subprocess wrapper；它會直接讀取
stdout，不保存原始 JSON，並將 stderr 寫入獨立 log：

```sh
.venv/bin/llvm-cov-py export ./program --profile coverage.profdata \
  --tool llvm-cov-18 -o coverage.pkl --timeout 120 --test-name my-test
```

預設 exporter 使用 `-skip-expansions -skip-functions`。工具的 mismatch 警告等
診斷保留在 log；本套件不修改 mapping 或 profile，也不把工具警告當成解析錯誤。
JSON 無效、exporter exit code 非零或逾時時，不發布半成品 pickle。
預設不覆寫已存在的輸出，需要時明確使用 `--overwrite`。

檢查本機可信的 pickle：

```sh
.venv/bin/llvm-cov-py inspect coverage.pkl
.venv/bin/llvm-cov-py inspect coverage.pkl --files
```

## Dataclasses 與 Python API

| Model | 保存內容 |
| --- | --- |
| `Segment` | line、column、count、has_count、is_region_entry、is_gap_region |
| `LineCoverage` | 衍生的 line/count，並提供 `hit` property |
| `MetricSummary` | count、covered、percent、可選 notcovered |
| `FileSummary` | lines、regions、functions、instantiations 的 summary |
| `FileCoverage` | filename、segments、summary、data_index |
| `UnitSummary` | 一個 data unit 的 totals |
| `CoverageMetadata` | JSON type/version 與 unit summaries |
| `ParseStats` | 檔案／segment 數、輸入 SHA-256、bytes、解析時間 |
| `PickleHeader` | 格式版本、來源、test name、producer command |
| `PickleFooter` | 完成後的 metadata、stats 與 payload checksum |
| `ArchiveResult` | 已發布 archive 的路徑、大小、header/footer |

這些 dataclasses 都使用 `frozen=True, slots=True`。第一版保存 file-level
segments 與上述 summaries；branch、MC/DC、expansion 與完整 function-record
payloads 會被消耗但不保留。支援 LLVM JSON v2 和 v3 的六欄 segment 格式。
Line coverage 是衍生資料，不額外在 pickle 中存一份：

```python
from llvm_cov import CoverageReader, PickleReader, write_pickle

with open("coverage.json", "rb") as source:
    reader = CoverageReader(source, buffer_size=64 * 1024)
    result = write_pickle(reader, "coverage.pkl", test_name="my-test")

archive = PickleReader("coverage.pkl")
for file in archive:
    print(file.filename, len(file.segments))
    for line in file.iter_lines():
        if line.line == 320:
            print("count:", line.count, "hit:", line.hit)

assert archive.completed
print(archive.footer.metadata.json_version)
```

未映射的行不出現在 `iter_lines()` 中，不等同於已知未覆蓋。Line projection
遵循 LLVM 18/21 的 wrapped segment、gap 與 skipped region 語意。

`CoverageReader` 也可以單獨迭代，交給其他 consumer。root type/version 通常在
data 之後，必須完整消耗 iterator，才能使 `completed` 為 True、取得已驗證的
`metadata`。`PickleReader` 同樣須讀到 footer 才會完成整體驗證。

## Pickle 格式與記憶體

一份 archive 的結構：

```text
magic / format version
PickleHeader
FileCoverage
FileCoverage
...
PickleFooter
SHA-256 trailer
```

每個記錄使用新的 protocol-5 Pickler，避免 memo 把全部檔案留在記憶體中。
Reader 也逐筆載入，並檢查 payload checksum、archive checksum、記錄數與結尾。
Writer 先寫入同目錄的暫存檔，完成 JSON 與 producer 驗證後才原子發布。

記憶體取決於目前檔案的 segments／序列化狀態及少量 metadata，不會隨全部
檔案累積；單一超大來源檔仍可能需要較多記憶體。Exporter 自己的計算與記憶體
成本不受此 parser 控制，第一批 stdout bytes 出現前仍需要等待。

Pickle 僅適合讀取可信來源；反序列化 pickle 可以執行 Python 程式碼。格式中的
SHA-256 用於完整性檢查，不是來源認證。未來改動持久化 model 時需升級格式版本。
