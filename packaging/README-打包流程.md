# 打包流程（开发机内部使用，不进分发包）

本目录里只有 `install.bat` 与 `安装说明.md` 会被 `tools\pack.py` 取走放进安装包，
其余文件（本说明 + 三个 `qa_*.py`）只服务于开发机的打包与验收。

---

## 全流程

在 `answer-assistant\` 目录下依次执行：

```
:: 0. 备份（只在改大件之前需要）
robocopy . ..\_backup\answer-assistant_v1_<时间戳> /E /MT:16

:: 1. 瘦身：删字节码、调试符号、编译期头文件、用不到的视频编解码库…
.venv\Scripts\python.exe tools\slim.py --report     :: 先看体积构成
.venv\Scripts\python.exe tools\slim.py              :: 执行

:: 2. 打包
.venv\Scripts\python.exe tools\pack.py
::    -> dist_package\AnswerAssistant-1.0.0-win64.zip

:: 3. 解压 + 环境重指向 + 端到端验收（换目录，模拟目标机）
::    3a. 解压到别的盘/目录，双击包内 install.bat
::    3b. 或者用下面的 qa 脚本（更快、结论更强）
```

---

## 三个 qa 脚本

### `qa_sync_extracted.py` —— 让解压目录与 zip 逐字节一致

重新解压 1.5 万个文件要十几分钟（实时防病毒扫描每个新文件）。
这个脚本改为「只补差、再全量校验 CRC32」：把目标目录同步成 zip 的内容，
最后逐文件比对 `size + CRC32`，并检查没有多余文件。

```
python packaging\qa_sync_extracted.py <zip> <解压目录> <zip内顶层目录名>
```

只要最后打印「与 zip 内记录逐字节一致」，这个目录就等价于（严格来说强于）
「重新解压一遍」的结果——之后在它里面跑的验收结论对 zip 同样成立。

### `qa_verify_install.py` —— 安装后的端到端验收

用它自己 `.venv` 里的 python 跑，检查 6 件事：

1. 包内结构完整、没有开发残留（`_backup/ dist_package/ build/ __pycache__` 等）
2. `sys.base_prefix` 指向**本目录**的 `runtime\python`（可迁移性的核心）
3. 核心依赖齐全（tkinter / numpy / cv2 / openpyxl / docx / olefile / mss / keyboard / rapidfuzz / PIL / paddle / paddlex）
4. 模型路径落在包内：`PADDLE_PDX_CACHE_HOME` 与 `paddlex.utils.cache.CACHE_DIR`
5. 题库：5 种格式题目数一致、选项正文不退化、匹配器命中
6. 真·全链路：渲染题目图 → OCR 识别 → 模糊匹配 → 命中正确题目

```
<解压目录>\.venv\Scripts\python.exe packaging\qa_verify_install.py <解压目录>
```

> 第 4 项最容易假通过：**脚本自己必须在 import paddle/paddlex 之前**
> 调一次 `core.config.ensure_bundled_models()`。`paddlex` 在导入期就把
> `CACHE_DIR` 固化了，晚一步设置环境变量就无效，而开发机上用户目录
> 恰好有模型，于是「看起来能用」——到没网的机器上才会崩。

### `qa_test_offline.py` —— 假装这是一台干净机器

把 `USERPROFILE / HOME` 指向一个空目录，于是 `Path.home()` 变成空目录、
`%USERPROFILE%\.paddlex` 不存在，等价于从没跑过 PaddleX 的目标机。
此时仍能识别 + 匹配，才真正证明「识别只依赖包内 `models\`，与用户目录和网络无关」。

```
cd <解压目录>
set USERPROFILE=<空目录> & set HOME=<空目录>
.venv\Scripts\python.exe qa_test_offline.py
```

---

## 验收标准（缺一不可）

| 检查 | 期望 |
| --- | --- |
| `qa_sync_extracted.py` 终检 | 「与 zip 内记录逐字节一致」 |
| 包内 `install.bat` | 4 步全绿，输出「安装完成」 |
| `.venv\Scripts\python.exe tools\selfcheck.py --quick` | 失败 0（警告可忽略） |
| `qa_verify_install.py` | 「验证全部通过」 |
| `qa_test_offline.py` | 「全部通过：不依赖用户目录、不联网」 |

---

## 踩过的坑（改打包脚本前必读）

1. **不要用 `opencv-python-headless` 替换 `opencv-contrib-python`。**
   `paddlex/utils/deps.py` 的 `is_dep_available()` 是按**发行包名**查
   `importlib.metadata.version()`，OCR 流水线走
   `pipeline_requires_extra("ocr", alt="ocr-core")`，清单里就是
   `opencv-contrib-python`。headless 版模块名叫 `cv2` 能 import，但 dist 名字对不上，
   直接抛 `DependencyError` / `RuntimeError: A dependency error occurred during
   pipeline creation`，**报错完全不提 opencv**。

2. **不要删 `paddle/utils/cpp_extension`。** `paddle/utils/__init__.py` 在 import
   阶段就 `from . import cpp_extension`，删掉后 `import paddle` 直接失败。

3. **`.venv\pyvenv.cfg` 里的路径是绝对写死的**，换目录不跑 `install.bat`
   会静默沿用打包机的原路径。`tools/setup_env.py` 用 `mbcs`（本地代码页）写这个文件，
   因为 CPython 启动阶段就是按本地代码页解析它——路径含中文时用 UTF-8 写会变乱码。

4. **`.bat` 必须 CRLF + 纯 ASCII**，否则 cmd 解析错乱。

5. **`tools/pack.py` 按包内路径去重**：`FILES` 里显式列的文件同时也在 `DIRS`
   的扫描范围内，不去重会写进 zip 两遍。

6. **不要把 `pack.py` / `slim.py` 打进包**：它们依赖本目录的 `packaging\`，
   而包里只保留 `install.bat` 与安装说明，带进去会让人误跑出一个残缺的包。
