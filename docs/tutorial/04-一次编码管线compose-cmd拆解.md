# 04 · 一次编码管线：compose_cmd 拆解

## 这一篇解决什么

这是整个项目的地基。看懂 `compose_cmd()` 怎么拼 ffmpeg 命令，你就能自己加滤镜、调顺序、改混音，而不是在参数里瞎试。

**主代码位置**：`djkit/compose.py` 的 `compose_cmd()`（约 126–232 行）

> 这篇最重要。前面三篇是「怎么用」，这篇开始是「怎么造」。

---

## 1. 目标：一个 filter_complex，一次编码

先看函数签名和骨架：

```python
def compose_cmd(c: Clip, dst: Path, opts: ComposeOpts) -> list[str]:
    """把一条素材组装成最终成片命令（单次编码）"""
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    inputs = []
    n_in = 0

    def add(argv):
        nonlocal n_in
        i = n_in
        inputs.extend(argv)
        n_in += 1
        return i
```

`add()` 是个小技巧：每加一个 `-i` 输入就返回它的**输入序号**。后面滤镜里写 `[0:v]`、`[2:a]` 全靠这个序号，手写很容易数错。

整个函数分三段：
1. 攒输入（`inputs`）
2. 攒滤镜（`parts`）
3. 拼成 `ffmpeg ... -filter_complex "..." -map [v] -map [a] out.mp4`

---

## 2. 滤镜顺序：为什么字幕必须在变速之前

看主视频链：

```python
vf = [build_filter(opts.mode, opts.w, opts.h)]   # 1. 规格
if sub is not None:
    vf.append(sub_filter_arg(sub, opts.sub_style))  # 2. 字幕
if opts.title is not None:
    vf.append(build_title_filter(...))              # 3. 花字
if opts.speed_on:
    vf.append(f"setpts=PTS/{opts.speed:.6f}")      # 4. 变速
vf.append(f"fps={opts.fps}")                        # 5. 统一帧率
vf.append("setsar=1")
```

**顺序不是随便排的**：

- **字幕在 `setpts` 之前**：字幕时间轴是原始时间。如果先变速，画面时间被压缩，字幕还按原时间出现，就对不上了。先烧字幕再变速，文字跟着画面一起被压缩，天然同步。
- **花字也在 `setpts` 之前**：理由相同。而且花字「跟着内容走」，加速后它在墙钟时间上确实变短了，这是对的。
- **`fps` 放最后**：前面所有操作可能产生非整数帧率，最后统一到 25fps，保证输出规格一致。
- **规格放最前**：字幕样式的字号换算依赖输出高度，先确定分辨率再烧字幕。

`build_filter()` 处理三种填充模式：

```python
if mode == "crop":     # 居中裁切，铺满
    scale=W:H:force_original_aspect_ratio=increase,crop=W:H
elif mode == "pad":    # 黑边
    scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2
else:                  # blur（默认）
    # 背景放大模糊 + 前景缩放叠加，见源码
```

---

## 3. 片头片尾：把它们也变成统一的段

```python
segs = []
if opts.intro is not None:
    segs.append((i, i, "intro"))
segs.append((main_i, main_a, "main"))
if opts.outro is not None:
    segs.append((i, i, "outro"))
```

每个段是一个 `(视频输入序号, 音频输入序号, 标签)`。片头片尾走 `pad_v`（黑边规整），正片走 `main_v`（完整滤镜链）。

**无音轨自动补静音**：

```python
if not c.has_audio:
    main_a = add(["-f", "lavfi", "-t", f"{c.duration:.2f}",
                  "-i", "anullsrc=r=48000:cl=stereo"])
```

不是为了输出有声音，是为了让 `concat` 的音视频结构统一——否则 concat 会失败。

最后：

```python
parts.append(f"{labels}concat=n={len(segs)}:v=1:a=1[vcat][acat]")
```

只有一段时不加 concat，直接用 `[v_main]`。

**变速只作用于正片**：片头片尾的音频链里不 append `atempo_chain`。看 `if tag == "main" and opts.speed_on` 这个条件。

`atempo_chain()` 处理超出 0.5–2.0 的倍率：

```python
while s > 2.0:
    parts.append("atempo=2.0")
    s /= 2.0
while s < 0.5:
    parts.append("atempo=0.5")
    s /= 0.5
parts.append(f"atempo={s:.6f}")
```

ffmpeg 的 atempo 单个只接受 0.5–2.0，超出就串联多级。超过 1.5x 音质有可闻损失，工具会提醒。

---

## 4. BGM 混音：闪避是怎么做到的

这是全函数最绕的一段：

```python
bi = add(["-stream_loop", "-1", "-t", f"{total:.2f}", "-i", str(opts.bgm)])
parts.append(f"[{bi}:a]{A_FMT},volume={opts.bgm_vol:.3f}[bgmv]")

if opts.duck:
    parts.append(f"{atail}asplit[a_voice][a_key]")
    parts.append("[bgmv][a_key]sidechaincompress="
                 "threshold=0.05:ratio=6:attack=15:release=300:makeup=1[bgmd]")
    parts.append(f"[a_voice][bgmd]amix=inputs=2:duration=first:"
                 f"dropout_transition=0:normalize=0{fade}[aout]")
```

逐步解释：

1. `-stream_loop -1 -t <总时长>`：BGM 无限循环并截断到成片长度。给 3 秒的音频也能铺满 90 秒成片。
2. `volume=0.25`：先把 BGM 压到人声之下的量级。
3. **`asplit`**：人声流要用两次——一次作为最终输出，一次作为「控制信号」告诉 sidechaincompress 什么时候有人声。同一个流不能直接用两次，必须先 split。
4. **`sidechaincompress`**：这就是「闪避」。检测到 `a_key`（人声）超过阈值时，把 `bgmv`（BGM）压低 6 倍，attack 15ms、release 300ms。人声停了 300ms 后恢复。
5. `amix ... normalize=0`：混合两路。**`normalize=0` 很重要**——默认会归一化，把整体音量拉低，导致你设的 `--bgm-vol` 失去意义。
6. `afade=t=out:st=...:d=...`：结尾淡出，位置按总时长算。

不 duck 时直接 `amix`，少了 split 和 sidechain 两步。

---

## 5. 收尾：一次编码

```python
cmd += inputs
cmd += ["-filter_complex", ";".join(parts), "-map", vtail, "-map", atail]
cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts.crf),
        "-pix_fmt", "yuv420p"]
cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
cmd.append(str(dst))
```

`parts` 里每一条都是 `滤镜链[输入标签][输出标签]` 的形式，用分号连起来就是一个完整的 filter_complex。

**`-preset veryfast`**：速度优先。crf 20 已经保证了视觉质量，preset 再快也对画质影响不大。批量出片时这个选择能省大量时间。

**`-pix_fmt yuv420p`**：不加的话某些播放器（尤其手机）播不了。别删。

---

## 6. `ComposeOpts`：配置的归一化

`ComposeOpts` 把命令行参数转成滤镜需要的结构。`parse_specs()` 支持 `1080x1920,1080x1440` 和 `douyin,xiaohongshu` 两种写法，后者映射到内置规格表。

多规格出片时 `cmd_fit` 会为每个规格调一次 `compose_cmd`，**但 `Clip` 的探测只做一次**——这是「多规格不慢得离谱」的关键。

---

## 怎么验证滤镜链对了

最直接的办法：把命令打印出来。在 `compose_cmd` 末尾加一行：

```python
print(" ".join(cmd))
```

或者用 `--dry-run`（部分命令支持）看会做什么。

真正可靠的验证是**看产物**：
- 字幕：抽帧数像素（自检里用黑底黄字，有字幕 4597px / 无字幕 0px）
- BGM：用 `volumedetect` 比响度（−91dB → −35dB）
- 时长：`probe()['duration']` 应该 ≈ 片头 + 正片/变速 + 片尾

第 8 篇会详细讲这些验证手法。

---

## 常见坑

**滤镜顺序改了以后字幕错位**
九成是把字幕挪到 `setpts` 之后了。回到第 2 节的顺序。

**`amix` 后整体音量变小**
没写 `normalize=0`。这是 ffmpeg 的默认归一化在作怪。

**concat 报「流参数不匹配」**
某一段的音轨参数没统一。所有段都必须过 `A_FMT`（统一的采样率/声道）。无音轨的段必须补静音。

**加了 BGM 后报错「Filter asplit has an unconnected output」**
`asplit` 出来的两路 `[a_voice]` 和 `[a_key]` 都要用掉。少用一路就报这个。

---

## 下一步

成片的地基打好了。接下来看素材里「哪段值得剪」：[05 · AI 三件套：找片段、转写、配音](05-AI三件套找片段转写配音.md)。
