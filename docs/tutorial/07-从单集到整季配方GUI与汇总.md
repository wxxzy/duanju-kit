# 07 · 从单集到整季：配方、GUI 与汇总

## 这一篇解决什么

单集跑通了，15 集怎么一次搞定？改了参数怎么只重跑需要的步骤？成片散在目录里怎么一屏验收？

**主代码位置**：`djkit/recipe.py`、`djkit/explainer.py`、`djkit/gui_server.py`、`djkit/season.py`、`djkit/web/`

---

## 1. 配方：把流水线写成 YAML

```bash
python -m djkit run 配方.yaml
```

配方是描述「这一批素材怎么出片」的 YAML。看 `recipe_plan()`：它根据配方里的字段决定跑哪些子命令——没有配音就走切片路线，有 `解说:` 块就走 `explainer` 路线。

一个解说配方长这样：

```yaml
输出: 产出/某剧_解说.mp4
解说:
  背景: 总纲.md
  秒数: 24
  音色: zh-CN-YunjianNeural
  三版: 3
  片段数: 6
  段长: 8
规格: [1080x1920]
字幕: auto
字幕样式: default
BGM: 音乐.mp3
BGM音量: 0.2
闪避: true
```

`run_job()` 按顺序执行，每步之间检查产物签名——改了提示词只重跑文案，不重转写。

**目录监听**：

```bash
python -m djkit run 配方.yaml --watch 60      # 每 60 秒检查一次新素材
python -m djkit run 配方.yaml --watch --cycles 3  # 最多跑 3 轮
```

适合剧方分批下发素材的场景：丢进去就自动处理。

---

## 2. `explainer.py`：解说片的一条龙

`run_explainer()` 是配方里 `解说:` 块的执行体。它把这些步骤串起来：

```
speech_windows (VAD 找有声音的区间)
  → auto_segments (自动挑片段，避开静音)
  → build_sample_audio (拼采样音频)
  → script (写文案)
  → tts (配音)
  → fit_segments (配音反推时长)
  → build_script_srt (按文案排字幕)
  → assemble (出片)
```

几个设计要点：

**`speech_windows()` 用 VAD 找语音区间**：避免在静音段选画面。`snap_to_speech()` 把选段起点吸附到最近的语音起点。

**`auto_segments()` 自动挑片段**：配方没写 `片段:` 时自动选。选完检查覆盖率，不够就补。

**缓存靠签名**（`_sig()` + `cached_ok()`）：每个中间产物配一个签名文件，输入没变就跳过。改了提示词只会让文案那一步的签名变，前面转写的产物沿用。

**字幕向后兼容**：

```python
if rg(job, "字幕", "burn_sub", default=None) is not None:
    do_sub = as_bool(rg(job, "字幕", "burn_sub"), True)
else:
    do_sub = False      # 配方没写就不烧，别改老配方的产出
```

老配方没写 `字幕` 字段时**不烧字幕**。这是向后兼容——不能因为升级工具就改变已有配方的产出。

**多规格配音只做一次**：画幅列表循环时，配音和音画对齐只跑前面，后面只重编码。第一个画幅用原名，后续加 `_1920x1080` 后缀。

---

## 3. GUI：四步走完

```bash
python -m djkit gui              # 浏览器界面
python -m djkit app              # 桌面客户端（pywebview）
```

浏览器打开 `http://127.0.0.1:8770/`，四步：**素材 → 片段 → 文案 → 出片**。

后端是**标准库 `ThreadingHTTPServer`**，不用 FastAPI/Flask：

```
浏览器 ──HTTP──> ThreadingHTTPServer
                    ├── 静态页（index.html / app.js / style.css）
                    ├── /video   支持 Range，能拖进度条
                    ├── /thumb   按需生成缩略图并缓存
                    └── /api/*   长任务丢进单工作线程，前端每 0.8 秒轮询
```

**为什么不用 SSE**：轮询断线更皮实。**为什么单工作线程**：避免 ffmpeg 和模型并发抢资源。

界面比命令行多做的事：

| 环节 | 界面做的 |
|---|---|
| 剧集总纲 | 素材页一键生成，状态栏显示 `✓ 14 人 · 16 名词 · 104 译名` |
| 挑片段 | 缩略图墙，点选实时算总时长；「换一批」跳过已选位置 |
| 文案 | 三版 A/B/C，六维评分明细，逐句改，切走切回不丢 |
| 字幕/BGM | 右侧参数区：样式下拉、烧制开关、原生选文件、音量、闪避 |
| 进度 | 进度条 + 滚动日志，可取消 |
| 成片 | 内嵌播放，多画幅点规格切换 |
| 整季 | 第 4 步下方的汇总页 |

**几个前端坑（都修过）**：

- **勾选被轮询翻回去**：未确认的勾选记在 `selLocal`，轮询合并时用它压住服务端旧值，等服务端确认后再交回。否则保存有 200ms 防抖、轮询每 0.8 秒一次，中间任何一次都会把刚点的覆盖掉。
- **跳步在阶段变化时发生**：`stage` 会长时间停在 `segments`，原来「stage 是 segments 且在第 1 步就跳」会导致回到素材页立刻被弹回。
- **忙时 `reset` 返回 409**：原来直接 `STATE.update({"busy":False})`，两个任务并行写同一份 STATE。
- **`confirm()` 在 WebView2 里可能被拦**：「清理缓存」要连点两次才生效，不能靠弹窗确认。
- **前端报错写日志**：`window.onerror` 和 `unhandledrejection` POST 到 `/api/log`，落到 `gui_out/app.log`。客户端没控制台，否则用户只会说「白屏了」。

**关窗不丢进度**：片段、三版文案、勾选、成片信息落在 `gui_out/_last.json`（临时文件 + 原子替换）。重开自动恢复。三条保护：素材不在了只提示不恢复；`rendering` 这类中间态不恢复；成片被手工删了退回文案页。

---

## 4. 整季汇总：一屏看完这一季

`season.py` 的 `scan_season()` 只读目录，不改任何文件：

```python
def scan_season(root: Path, spec: str = "") -> dict:
    """返回 {count, episodes[], specs[], total_mb, total_txt, problems[]}"""
```

成片散在 `整季/1080x1920/*.mp4` 里（也可能平铺在根目录）。它给出每集：

- 集号、时长、体积、分辨率
- 有没有音轨、有没有封面
- 文案字数（从 `_工作/<stem>/文案/选定.txt` 读）

跳过 `_工作` / `.djkit` / `封面` / `_thumbs`——那是中间产物。

**排序按数字不按字典**：

```python
eps.sort(key=lambda e: (e["ep"] is None, e["ep"] or 0, e["name"]))
```

第 10 集排在第 2 集后面。这是「按字典排序」bug 的高发区——字符串比较时 `"10" < "2"`。

**缓存策略**：`/api/season` 按「目录 mtime + mp4 数量 + 画幅」缓存。前端只在三个时机拉：跑完批量、点进成片页、启动探一次（上次跑完就退出的话 `batch` 是空的）。**不跟着 0.8 秒轮询扫盘**。

**两个真实修过的瑕疵**：

1. 目录不存在时只返回 4 个键，前端读 `specs`/`total_mb` 得到 `undefined`。现在 11 个键全给。
2. 前端原本用 `path.replace(".mp4",".jpg")` 猜封面路径，成片是 `.mov` 就猜错。现在后端返回 `cover_path`。

**越权校验**：`/cover?p=` 检查 `is_relative_to(OUT_ROOT)`，越权直接 403（实测拦住了 `C:/Windows/win.ini`）。

**汇总行自己数**：前端不信后端的 `problems`，从 episodes 里自己数 `noAud`/`noCov`——界面显示什么，就该以眼前这张表为准。后端报「有没有」，前端要报「几集」。

---

## 常见坑

**整季目录里成片平铺，没有画幅子目录**
正常。`specs` 为空时 `scan_season` 直接扫根目录，照样找得到。

**改了 total 但汇总页没变**
缓存没失效。汇总按目录 mtime 缓存，直接改文件内容而没改 mtime 的话，点「刷新」或重启界面。

**GUI 端口被占**
`app.py` 的 `free_port()` 会挑一个可用端口。手动 `gui --port 8771` 指定也行。

**批量跑到一半想停**
点取消。`/api/cancel` 置一个 `threading.Event`，各任务在步骤边界 `check_cancel()`。ffmpeg 一旦开始就得等它跑完——取消只在步骤之间生效。

---

## 下一步

改完代码怎么确认没弄坏、怎么交付给别人：[08 · 测试与交付：自检、打包与排错](08-测试与交付自检打包与排错.md)。
