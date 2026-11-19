"""djkit.selftest — 端到端自检

跑一遍真实流水线并**断言产物**，不是只打印日志。
默认离线：解说链用本地桩替代 LLM 和 TTS，不花钱、不依赖网络。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

from .compose import cmd_fit
from .core import (CACHE_DIR, child_kwargs, ff_path_quoted, ffmpeg_exe, fmt_sec,
                   probe, run)
from .media import cmd_rename, cmd_scan
from .report import cmd_report
from .slicer import cmd_slice

SUBCOMMANDS = ("scan", "rename", "slice", "fit", "report", "script",
               "asr", "tts", "hot", "assemble", "run", "doctor", "selftest")


class Checker:
    """把日志变成断言：失败计数，最后统一给结论"""

    def __init__(self) -> None:
        self.total = 0
        self.failed = 0

    def ok(self, cond: bool, name: str, detail: str = "") -> bool:
        self.total += 1
        if cond:
            print(f"   ✓ {name}" + (f"  {detail}" if detail else ""))
        else:
            self.failed += 1
            print(f"   ✗ {name}  {detail}")
        return bool(cond)

    def near(self, got: float, want: float, tol: float, name: str) -> bool:
        return self.ok(abs(got - want) <= tol, name, f"实测 {got:.2f}，期望 {want:.2f}±{tol}")

    def report(self) -> int:
        print()
        if self.failed:
            print(f"自检失败：{self.failed}/{self.total} 项不通过")
            return 1
        print(f"自检通过：{self.total}/{self.total} 项")
        return 0


def cmd_doctor(args) -> int:
    print("环境自检")
    print("-" * 46)
    try:
        exe = ffmpeg_exe()
        r = run([exe, "-version"], timeout=30)
        ver = (r.stdout or "").splitlines()[0] if r.stdout else "?"
        print(f"ffmpeg    OK   {ver[:60]}")
        print(f"          路径 {exe}")
    except SystemExit as e:
        print(f"ffmpeg    缺失 {e}")
        return 2

    for mod in ("openpyxl", "tabulate", "imageio_ffmpeg", "yaml", "numpy"):
        try:
            __import__(mod)
            print(f"{mod:<10}OK")
        except ImportError:
            print(f"{mod:<10}缺失（pip install {mod.replace('_', '-')}）")

    for mod, extra in (("faster_whisper", "asr"), ("edge_tts", "tts")):
        try:
            __import__(mod)
            print(f"{mod:<10}OK")
        except ImportError:
            print(f"{mod:<10}缺失（{extra} 不可用）")

    print(f"Python    {sys.version.split()[0]}")
    print(f"工作目录  {Path.cwd()}")
    print("文件编码  utf-8-sig（兼容 Excel 中文）")
    return 0


def _install_stubs() -> None:
    """把联网环节换成本地实现：LLM 返回固定文案，TTS 生成等长静音。"""
    from . import script as script_mod

    def fake_llm(cfg, messages, temperature=0.85, max_tokens=4000, timeout=240):
        import json as _json

        sysmsg = messages[0]["content"] if messages else ""
        m = re.search(r"分成\s*(\d+)\s*段", sysmsg)
        if m or "segments" in sysmsg:
            # 逐段模式：按段数返回合法 JSON，每段两句占位文案
            n = int(m.group(1)) if m else 3
            return _json.dumps({"segments": [
                {"i": i, "lines": [f"这是第{i}段的第一句", f"这是第{i}段的第二句"]}
                for i in range(1, n + 1)]}, ensure_ascii=False)
        return "\n".join([
            "第一行钩子，这句话用来占位",
            "第二行继续推进情节",
            "第三行给出一个反差",
            "第四行收在悬念上",
        ])

    script_mod.llm_chat = fake_llm

    class _FakeComm:
        def __init__(self, text, voice, rate=None, volume=None):
            self.text = text

        async def save(self, path):
            secs = max(2.0, len(self.text) / 4.1)
            run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                 "-t", f"{secs:.2f}", "-c:a", "libmp3lame", "-b:a", "64k", str(path)], 120)

    sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=_FakeComm)


def _make_assets(base: Path) -> dict:
    exe = ffmpeg_exe()
    tmp = base / "_自检素材"
    tmp.mkdir(parents=True, exist_ok=True)

    for i, (w, h) in enumerate([(720, 1280), (1280, 720), (1080, 1920)], 1):
        dst = tmp / f"测试短剧_第{i}集.mp4"
        run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc=size={w}x{h}:rate=25:duration=12",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(dst)])
        print(f"   {dst.name}  {w}x{h}")

    def _font_opt() -> str:
        for cand in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"):
            if Path(cand).exists():
                return f":fontfile={ff_path_quoted(cand)}"
        return ""

    intro = base / "_自检片头.mp4"
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x1a1a2e:size=1080x1920:rate=25:duration=2",
         "-vf", f"drawtext=text='片头'{_font_opt()}:fontsize=110:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(intro)])
    outro = base / "_自检片尾.mp4"
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x2e1a1a:size=1080x1920:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
         "-vf", f"drawtext=text='片尾'{_font_opt()}:fontsize=110:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(outro)])
    bgm = base / "_自检BGM.m4a"
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=523:duration=4", "-c:a", "aac", "-b:a", "128k", str(bgm)])

    # 解说链需要长一点的源：12 秒素材切两三段根本装不下。
    # 放在 tmp 外面，避免被 scan/fit/slice 当成第 4 集
    long_clip = base / "_自检长版.mp4"
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=1080x1920:rate=25:duration=45",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=45",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(long_clip)])
    print("   长版素材 45s（供解说链使用）")

    # 字幕故意用 GBK 编码，验证编码自动识别
    srt = tmp / "测试短剧_第1集.srt"
    srt.write_bytes(
        "1\n00:00:01,000 --> 00:00:04,000\n第一句台词\n\n"
        "2\n00:00:06,000 --> 00:00:09,000\n第二句台词\n".encode("gbk")
    )
    print("   片头（无音轨）2s、片尾 2s、BGM 4s、GBK 字幕 2 条")
    return {"tmp": tmp, "intro": intro, "outro": outro, "bgm": bgm, "long": long_clip}


def cmd_selftest(args) -> int:
    """生成合成素材，跑通全部流水线并对产物做断言"""
    base = Path(args.dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    c = Checker()
    live = bool(getattr(args, "live", False))
    if not live:
        _install_stubs()

    print("1) 生成测试素材")
    a = _make_assets(base)
    tmp, intro, outro, bgm = a["tmp"], a["intro"], a["outro"], a["bgm"]

    # ---------------------------------------------------------------- scan
    print("\n2) scan")
    cmd_scan(argparse.Namespace(dir=str(tmp), out=None, jobs=4, no_cache=False))
    from .media import parse_episode
    c.ok(parse_episode(tmp / "测试短剧_第2集.mp4") == ("测试短剧", 2),
         "剧名+集数解析", str(parse_episode(tmp / "测试短剧_第2集.mp4")))

    # ----------------------------------------------------------------- fit
    print("\n3) fit：双规格 + 花字 + 烧字幕 + 变速 1.1x + BGM + 片头尾 + 封面")
    out_fit = base / "_自检成片"
    cmd_fit(argparse.Namespace(
        dir=str(tmp), out=str(out_fit), jobs=4, size="1080x1920",
        specs="1080x1920,1080x1440", mode="blur", crf=25, fps=25,
        intro=str(intro), outro=str(outro),
        speed=1.1, bgm=str(bgm), bgm_vol=0.3, bgm_fade=1.0, duck=True,
        cover="auto", cover_sizes="720x1280",
        title="全家都跪了 这场面太解气", title_tpl=None, title_file=None,
        title_style="bar", title_start=0.3, title_dur=3.0, title_size=88,
        title_pos="top", title_y=None, title_color=None, title_border=None,
        title_fade=0.3, title_font=None,
        burn_sub=True, sub_dir=None, sub_style="default", sub_size=None, sub_font=None,
        dry_run=False, no_cache=False,
    ))
    finals = [p for p in sorted(out_fit.rglob("*.mp4")) if CACHE_DIR not in p.parts]
    c.ok(len(finals) == 6, "成片数量 = 2 规格 × 3 集", f"实测 {len(finals)}")
    expect = 2 + 12 / 1.1 + 2
    if finals:
        m = probe(finals[0])
        c.near(m["duration"], expect, 0.4, "成片时长 = 片头2 + 正片12/1.1 + 片尾2")
        c.ok(m["has_audio"], "成片有音轨")
        specs = {probe(p)["width"] for p in finals}
        c.ok(specs == {1080}, "宽 1080", str(sorted(specs)))
        c.ok(len({(probe(p)["width"], probe(p)["height"]) for p in finals}) == 2,
             "出了 2 种规格")
    covers = list(out_fit.rglob("*.jpg"))
    c.ok(len(covers) == 12, "封面 12 张（6 主图 + 6 额外尺寸）", f"实测 {len(covers)}")

    # --------------------------------------------------------------- slice
    print("\n4) slice：切片同时烧字幕（验证时间轴平移）")
    out_slice = base / "_自检切片"
    cmd_slice(argparse.Namespace(
        dir=str(tmp), out=str(out_slice), jobs=4, mode="ranges", head=5, seg=30,
        ranges="5-11", copy=False, crf=25, dry_run=False,
        burn_sub=True, sub_dir=None, sub_style="yellow", sub_size=None, sub_font=None,
        no_cache=False,
    ))
    slices = [p for p in out_slice.rglob("*.mp4") if CACHE_DIR not in p.parts]
    c.ok(len(slices) == 3, "切片 3 段", f"实测 {len(slices)}")
    if slices:
        c.near(probe(slices[0])["duration"], 6.0, 0.3, "切片时长 5s-11s = 6.0s")

    # -------------------------------------------------------------- report
    print("\n5) report")
    xlsx = base / "_自检台账.xlsx"
    cmd_report(argparse.Namespace(dir=str(out_fit), out=str(xlsx), jobs=4, no_cache=False))
    c.ok(xlsx.exists() and xlsx.stat().st_size > 3000, "台账已生成", f"{xlsx.stat().st_size // 1024}KB"
         if xlsx.exists() else "缺失")

    # -------------------------------------------------------------- rename
    print("\n6) rename：规范命名（在副本上做）")
    rn_dir = base / "_自检改名"
    if rn_dir.exists():
        shutil.rmtree(rn_dir)
    rn_dir.mkdir(parents=True)
    for src in sorted(tmp.glob("*.mp4"))[:2]:
        shutil.copy2(src, rn_dir / f"{src.stem}-1080p.mp4")
    cmd_rename(argparse.Namespace(dir=str(rn_dir), apply=True, jobs=4, no_cache=False))
    names = sorted(p.name for p in rn_dir.glob("*.mp4"))
    c.ok(all("第" not in n and "-1080p" not in n for n in names), "文件名已规范化", "、".join(names))

    # ------------------------------------------------------------ assemble
    print("\n7) assemble：多段拼接")
    plan = base / "_自检拼接.yaml"
    one = tmp / "测试短剧_第1集.mp4"
    plan.write_text(
        f"输出: {base / '_自检拼接.mp4'}\n规格: 1080x1920\n"
        f"片段:\n"
        f"  - {{源: '{one}', 起: '1', 长: 3, 花字: '第一段'}}\n"
        f"  - {{源: '{one}', 起: '6', 长: 3}}\n",
        encoding="utf-8",
    )
    from .assemble import cmd_assemble
    cmd_assemble(argparse.Namespace(
        plan=str(plan), out=None, size="1080x1920", mode="blur", fps=25, crf=25,
        bgm=None, cover=None, sub_style="default", title_style="bar",
        title_start=0.2, title_dur=3.0, title_size=88, title_pos="top",
        dry_run=False,
    ))
    joined = base / "_自检拼接.mp4"
    c.ok(joined.exists(), "拼接成片存在")
    if joined.exists():
        c.near(probe(joined)["duration"], 6.0, 0.3, "拼接时长 3+3 = 6.0s")

    # ----------------------------------------------------------------- hot
    print("\n8) hot：高光检测")
    from .hot import cmd_hot
    csv_out = base / "_自检高光.csv"
    cmd_hot(argparse.Namespace(
        dir=str(one), top=2, len=3.0, min_gap=0, out=str(csv_out),
        work=None, dry_run=False,
    ))
    rows = csv_out.read_text(encoding="utf-8-sig").splitlines() if csv_out.exists() else []
    c.ok(len(rows) == 3, "候选 CSV 有表头 + 2 行", f"实测 {len(rows)} 行")

    # ------------------------------------------------------ 解说链（离线）
    print(f"\n9) 解说链（{'真实 LLM/TTS' if live else '离线桩：不花钱'}）")
    ex_root = base / "_自检解说"
    if ex_root.exists():
        shutil.rmtree(ex_root)
    (ex_root / "素材").mkdir(parents=True)
    shutil.copy2(a["long"], ex_root / "素材" / "测试短剧_长版.mp4")
    (ex_root / "台词.txt").write_text(
        "汤姆说，我儿子还活着。\n指挥官回答，我们不回去。\n任务是活命，孩子往后排。\n"
        "他读过人类所有输掉的战争。\n每一次都不是输在武器上。\n",
        encoding="utf-8",
    )
    recipe = ex_root / "配方.yaml"
    recipe.write_text(
        "模式: 解说\n"
        "素材: 素材\n"
        "输出: 成片\n"
        "解说:\n"
        "  段数: 3\n"
        "  段长: 5\n"
        "  台词: 台词.txt\n"
        "  解说时长: 12\n"
        "  背景: 测试用背景\n"
        "规格: [720x1280]\n"
        "花字: {模板: '{剧名} {集}', 样式: bar}\n"
        "台账: false\n",
        encoding="utf-8",
    )
    from .recipe import cmd_run
    cmd_run(argparse.Namespace(recipe=str(recipe), watch=None, cycles=0,
                               dry_run=False, jobs=4, no_cache=True))
    ex_out = list((ex_root / "成片").glob("*.mp4")) if (ex_root / "成片").exists() else []
    c.ok(len(ex_out) == 1, "解说成片 1 个", f"实测 {len(ex_out)}")
    if ex_out:
        m = probe(ex_out[0])
        c.ok((m["width"], m["height"]) == (720, 1280), "规格 720x1280",
             f"{m['width']}x{m['height']}")
        c.ok(m["has_audio"], "解说成片有音轨（配音已混入）")
    work_dirs = list((ex_root / "成片" / "_工作").glob("*/"))
    c.ok(any((d / "文案" / "文案_A.txt").exists() for d in work_dirs), "文案已生成")
    voices = [q for d in work_dirs for q in (d / "配音.mp3", d / "配音_对齐.m4a") if q.exists()]
    c.ok(bool(voices), "配音已生成")
    if ex_out and voices:
        # 关键不变量：画面跟着配音走，成片长度应贴着配音长度（最多多一个尾巴）
        vd = probe(voices[0])["duration"]
        c.ok(vd - 0.5 <= m["duration"] <= vd + 3.0,
             "成片长度 ≈ 配音长度", f"成片 {m['duration']:.1f}s / 配音 {vd:.1f}s")

    # --------------------------------------------------------------- 冒烟
    print("\n10) 命令行冒烟：13 条子命令的 --help")
    bad = []
    for cmd in SUBCOMMANDS:
        r = subprocess.run([sys.executable, "-m", "djkit", cmd, "--help"],
                           **child_kwargs(),
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(cmd)
    c.ok(not bad, "全部子命令 --help 正常", "失败：" + "、".join(bad) if bad else "")

    # ------------------------------------------------------- 剧集总纲（纯函数）
    print("\n11) 剧集总纲：采样 / 别名合并 / 裁剪")
    from .series import (apply_name_fixes, apply_user_names, even_excerpt,
                         load_context, merge_aliases, parse_names, render_md,
                         sample_windows)

    wins = sample_windows(2561.0, 7.0)
    c.ok(len(wins) >= 4, "长片采样分成多段", f"{len(wins)} 段")
    c.ok(all(95 <= a and b <= 2505 for a, b in wins), "采样落在正片区间（跳过片头片尾）",
         f"首段 {wins[0][0]:.0f}s 末段 {wins[-1][1]:.0f}s")
    gaps = [wins[i + 1][0] - wins[i][1] for i in range(len(wins) - 1)]
    c.ok(all(g >= -0.01 for g in gaps), "采样段互不重叠",
         f"最小间隔 {min(gaps):.0f}s" if gaps else "单段")
    total = sum(b - a for a, b in wins)
    c.ok(abs(total - 420) <= 90, "采样总长约等于设定值（7 分钟）", f"{total:.0f}s")
    c.ok(sample_windows(120.0, 7.0)[0][0] == 0.0, "短素材不跳片头")

    terms = merge_aliases([
        {"原名": "Michael Burnham", "译名": "迈克尔·伯纳姆", "类型": "人物",
         "别名": ["Burnham", "Michael"]},
        {"原名": "Burnham", "译名": "伯纳姆", "类型": "人物"},
        {"原名": "Tubal Burnham", "译名": "图巴尔", "类型": "人物"},
        {"原名": "Federation", "译名": "联邦", "类型": "组织", "别名": ["Starfleet"]},
        {"原名": "Starfleet", "译名": "星际舰队", "类型": "组织"},
    ])
    names = [t["译名"] for t in terms]
    c.ok("迈克尔·伯纳姆" in names and "伯纳姆" not in names,
         "同一人物的名/姓并进主条目", "、".join(names))
    c.ok("图巴尔" in names, "同姓不同人不会被误并", "、".join(names))
    c.ok("联邦" in names and "星际舰队" in names, "组织之间不合并（Starfleet≠Federation）")

    # ---- 专名校正：模型只出「补丁」，本地应用 ----
    base_terms = [
        {"原名": "Sanjo", "译名": "桑乔号", "类型": "地点", "别名": [], "备注": ""},
        {"原名": "Shenzhou", "译名": "神舟号", "类型": "地点",
         "别名": ["USS Shenzhou"], "备注": ""},
        {"原名": "Georgiou", "译名": "乔治乌", "类型": "人物", "别名": [], "备注": ""},
        {"原名": "Philippa", "译名": "菲利帕", "类型": "人物", "别名": [], "备注": ""},
        {"原名": "Takuvma", "译名": "塔库瓦玛", "类型": "人物", "别名": [], "备注": ""},
    ]
    fixed, applied = apply_name_fixes(base_terms, {
        "校正": [
            {"原名": "Sanjo", "正确": "Shenzhou", "理由": "同一艘船"},   # 撞名 → 合并
            {"原名": "Takuvma", "正确": "T'Kuvma", "理由": "官方拼写"},
            {"原名": "Apphanger", "正确": "Apphanger Bay"},              # 表里没有，忽略
            {"原名": "Georgiou", "正确": "georgiou"},                    # 只差大小写，忽略
        ],
        "合并": [{"保留": "Georgiou", "并入": ["Philippa"],
                  "全名": "Philippa Georgiou", "译名": "菲利帕·乔治乌", "理由": "同一人"}],
    })
    got_names = [t["原名"] for t in fixed]
    c.ok(len(fixed) == 3, "校正 + 合并后条数对得上", f"{len(fixed)} 条：{'、'.join(got_names)}")
    c.ok("T'Kuvma" in got_names and "Sanjo" not in got_names,
         "音近错拼改成正确拼写", "、".join(got_names))
    c.ok(any("Sanjo" in (t.get("别名") or []) for t in fixed),
         "被合并进去的原名留在别名里", "别名不丢")
    full = next((t for t in fixed if t["原名"] == "Philippa Georgiou"), None)
    c.ok(bool(full) and full["译名"] == "菲利帕·乔治乌",
         "同一人的名+姓合成全名", str(full and full.get("译名")))
    c.ok(len(applied) == 3, "每处改动都留了记录", f"{len(applied)} 处")
    c.ok(base_terms[0]["原名"] == "Sanjo", "不改调用方传进来的表")

    users = parse_names("Michael Burnham=迈克尔·伯纳姆，Saru：萨鲁; USS Shenzhou=神舟号")
    c.ok(len(users) == 3, "手工译名：中英文标点都认", f"解析出 {len(users)} 条")
    t2, added, changed = apply_user_names(
        [{"原名": "Saru", "译名": "萨鲁", "类型": "人物", "别名": [], "备注": ""}],
        [{"原名": "saru", "译名": "萨鲁（改）"}, {"原名": "USS Shenzhou", "译名": "神舟号"}])
    c.ok(changed == 1 and added == 1, "手工译名：同名改译 + 缺的就补",
         f"改 {changed} 增 {added}")
    c.ok(t2[0]["译名"] == "萨鲁（改）", "手工译名不分大小写")

    long_txt = "\n".join(f"line{i:03d}" for i in range(400))
    ex = even_excerpt(long_txt, 300, 3)
    c.ok("line000" in ex and "line399" in ex, "均匀摘录覆盖首尾", f"{len(ex)} 字")
    c.ok(len(ex) <= 420, "摘录不超过预算太多", f"{len(ex)} 字")

    bible = {"剧名": "测试剧", "主线": "一段主线。" * 20,
             "人物": [{"名": "甲", "原名": "A", "身份": "主角"}],
             "名词": [{"词": "控制环", "解释": "外星装置"}]}
    eps = [{"集": 1, "一句": "第一集的事"}, {"集": 2, "一句": "第二集的事"}]
    md = render_md(bible, eps, terms, {"剧名": "测试剧", "每集分钟": 7, "模型": "small"})
    c.ok("## 主线" in md and "译名表" in md and "| 2 |" in md, "总纲 Markdown 结构完整")
    bible_md = base / "_自检总纲.md"
    bible_md.write_text(md, encoding="utf-8")

    ctx = load_context(bible_md, None)
    c.ok("剧集总纲" in ctx and "控制环" in ctx, "总纲可转成提示词上下文", f"{len(ctx)} 字")
    ctx2 = load_context(bible_md, 2)
    c.ok("第二集的事" not in ctx2 and "第一集的事" in ctx2,
         "按集裁剪：不含本集及以后的剧情")
    c.ok(len(ctx2) <= 2600, "上下文有长度上限", f"{len(ctx2)} 字")

    # ------------------------------------------------------- 界面（不开窗口）
    print("\n12) 界面：规格解析 / 进度恢复 / 缓存白名单")
    import json as _json

    from . import gui_server as gs

    got = gs.parse_specs(["1080x1920", "1080X1920", "1920*1080"])
    c.ok(got == ["1080x1920", "1920x1080"], "规格归一化并去重", str(got))
    c.ok(gs.parse_specs("1001x777") == ["1000x776"], "奇数宽高自动抹成偶数",
         str(gs.parse_specs("1001x777")))
    c.ok(gs.parse_specs("720x1280") == ["720x1280"], "单值字符串也认")
    c.ok(len(gs.parse_specs(["1080x1920"] * 6)) == 1, "重复值只留一个")
    for bad_spec in ("abc", "100x100", "0x0"):
        try:
            gs.parse_specs([bad_spec])
            c.ok(False, f"乱填规格要报错（{bad_spec}）")
        except RuntimeError:
            c.ok(True, f"乱填规格要报错（{bad_spec}）")

    # 进度恢复：拿一个真实存在的文件当素材，否则 load_state 会判定「素材没了」
    ok_src = str(next(iter(sorted(tmp.glob("*.mp4")))).resolve())
    backup = gs.LAST.read_bytes() if gs.LAST.exists() else None
    try:
        gs.STATE.update({
            "stage": "scripts", "src": ok_src, "title": "测试短剧", "ep": 1,
            "segments": [{"i": 0, "start": 1.0, "len": 12, "selected": True}],
            "seg_ids": [0],
            "variants": [{"key": "A", "name": "悬念开场", "chars": 30,
                          "seg_scripts": [{"i": 1, "lines": ["第一句"]}]}],
            "variant": "A", "seg_scripts": [{"i": 1, "lines": ["第一句"]}],
        })
        gs.save_state()
        c.ok(gs.LAST.exists(), "进度已落盘")

        # 模拟重开：清空内存态再读盘
        gs.STATE.update({"stage": "idle", "src": "", "variants": [], "variant": "",
                         "segments": [], "seg_scripts": []})
        note = gs.load_state()
        c.ok(gs.STATE["stage"] == "scripts" and len(gs.STATE["variants"]) == 1
             and gs.STATE["variant"] == "A", "重开窗口能恢复文案进度", note)

        # 素材文件被删 / 换了机器 → 只提示，不恢复
        data = _json.loads(gs.LAST.read_text(encoding="utf-8"))
        data["src"] = str(base / "没有这个文件.mp4")
        gs.LAST.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        gs.STATE.update({"stage": "idle", "variants": []})
        note = gs.load_state()
        c.ok(note and gs.STATE["stage"] == "idle", "素材没了只提示不恢复", note)

        # 中间态不恢复，否则重开会看到一个卡住的「渲染中」
        data["src"] = ok_src
        data["stage"] = "rendering"
        gs.LAST.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        gs.STATE["stage"] = "idle"
        c.ok(gs.load_state() == "" and gs.STATE["stage"] == "idle", "中间态不恢复")
    finally:
        if backup is None:
            gs.LAST.unlink(missing_ok=True)
        else:
            gs.LAST.write_bytes(backup)   # 别把用户真实的进度冲掉

    info = gs.cache_info()
    c.ok(set(info["parts"]) == {"cache", "thumbs", "work"}, "缓存分了 3 类",
         "、".join(info["parts"]))
    c.ok(info["total_mb"] >= 0 and "free_mb" in info, "缓存占用和磁盘余量都拿得到")
    try:
        gs.cache_clear("../../..")
        c.ok(False, "缓存清理拒绝白名单外的目标")
    except RuntimeError:
        c.ok(True, "缓存清理拒绝白名单外的目标")

    # --------------------------------------------- 字幕 + BGM（界面那条链）
    print("\n13) 文案择优：缺段一票否决，六个维度各给理由")
    from .score import rank, score_script

    good = [{"i": 1, "lines": ["他被困在这艘船上整整七年", "所有人都以为他早就死了"]},
            {"i": 2, "lines": ["直到那天舰桥收到一段陌生的求救信号"]},
            {"i": 3, "lines": ["而信号里的声音正是他自己"]}]
    short = [{"i": 1, "lines": ["他叫迈克尔", "他是大副"]},
             {"i": 2, "lines": ["他犯了个错"]},
             {"i": 3, "lines": ["于是所有人都死了"]}]
    missing = [{"i": 1, "lines": ["死掉的人往往不是最惨的"]},
               {"i": 3, "lines": ["这是本期视频的第三个观点"]}]
    vs = [{"key": "A", "name": "悬念", "full": 3, "seg_scripts": good},
          {"key": "B", "name": "人物", "full": 3, "seg_scripts": short},
          {"key": "C", "name": "反常识", "full": 2, "seg_scripts": missing}]
    best_key = rank(vs, want_chars=100, n_seg=3)
    by = {v["key"]: v for v in vs}
    c.ok(best_key == "A", "正常长度、段落齐全的那版被选中", f"推荐 {best_key}")
    c.ok(by["A"]["score"] > by["B"]["score"] > by["C"]["score"], "分数拉开档次",
         f"A {by['A']['score']} > B {by['B']['score']} > C {by['C']['score']}")
    c.ok(sum(1 for v in vs if v.get("best")) == 1, "只有一版被标为推荐")
    c.ok(any("缺" in r for r in by["C"]["reasons"]), "缺段会明说缺了几段",
         next((r for r in by["C"]["reasons"] if "缺" in r), "未检出"))
    c.ok(any("本期" in r or "出戏" in r for r in by["C"]["reasons"]), "出戏词会被点出来",
         next((r for r in by["C"]["reasons"] if "本期" in r), "未检出"))
    c.ok(any("句长" in r or "偏短" in r for r in by["B"]["reasons"]), "短句过多会被点出来",
         next((r for r in by["B"]["reasons"] if "句长" in r), "未检出"))
    c.ok(len(by["A"].get("score_parts") or []) == 6, "六个维度都返回了分数",
         "、".join(p["name"] for p in by["A"]["score_parts"]))

    dup = [{"i": 1, "lines": ["同一句话", "同一句话", "换个说法"]}]
    c.ok(score_script(dup, want_chars=30, n_seg=1).score <
         score_script([{"i": 1, "lines": ["第一句不同", "第二句也不同", "第三句还不同"]}],
                      want_chars=30, n_seg=1).score, "重复句会拉低分数")
    eng = [{"i": 1, "lines": ["Signal from the ship", "他等了很多年"]}]
    c.ok(any("英文" in r or "可念" in r
             for r in score_script(eng, want_chars=30, n_seg=1).reasons),
         "英文长句会被标成念不顺")
    c.ok(score_script([], want_chars=30, n_seg=2).score < 30, "空文案分很低")

    print("\n14) 字幕 / BGM：文案直接排时间轴，不重新转写")
    from .subtitle import build_script_srt, cues_from_scripts, wrap_cjk

    demo = [{"i": 1, "lines": ["第一段第一句", "第一段第二句"]},
            {"i": 2, "lines": ["第二段只有一句"]}]
    cues = cues_from_scripts(demo, [4.0, 4.0])
    c.ok(len(cues) == 3, "一句一条字幕", f"{len(cues)} 条")
    c.ok(cues[0][0] >= 0 and abs(cues[-1][1] - 8.0) < 0.3,
         "字幕铺满整条时间轴", f"{cues[0][0]:.2f}s → {cues[-1][1]:.2f}s")
    c.ok(all(b[0] >= a[1] - 0.01 for a, b in zip(cues, cues[1:])), "条目不重叠")
    # 字多的句子该分到更多时间（4.1 比 6.7 长就该更长）
    long_txt = [{"i": 1, "lines": ["短", "这是一句明显长很多的台词内容"]}]
    cu = cues_from_scripts(long_txt, [6.0])
    c.ok(cu[-1][1] - cu[-1][0] > cu[0][1] - cu[0][0], "按字数分配时长，长句占更多时间",
         f"{cu[0][1] - cu[0][0]:.2f}s vs {cu[-1][1] - cu[-1][0]:.2f}s")
    c.ok("\n" in wrap_cjk("一" * 40), "长句自动折行")
    c.ok(wrap_cjk("短句") == "短句", "短句不折行")

    srt_demo = base / "_自检字幕.srt"
    _p, n_cue = build_script_srt(demo, [4.0, 4.0], srt_demo)
    txt = srt_demo.read_text(encoding="utf-8")
    c.ok(n_cue == 3 and txt.count("-->") == 3, "SRT 条目数对得上",
         f"{txt.count('-->')} 条时间码")
    c.ok(txt.rstrip().endswith("第二段只有一句") and "\n\n\n" not in txt,
         "SRT 格式合规（条目间空行、末尾空行）")

    # 真的烧进画面：黑底 + 黄字，数黄色像素；同一段不烧字幕做对照
    exe = ffmpeg_exe()
    sub_dir = base / "_自检字幕"
    sub_dir.mkdir(exist_ok=True)
    black = sub_dir / "black.mp4"
    run([exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:size=720x1280:rate=25:duration=8",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(black)], 180)
    bsegs = [{"源": str(black), "起": "0", "长": 4, "_start": 0.0},
             {"源": str(black), "起": "4", "长": 4, "_start": 4.0}]
    from .assemble import assemble_cmd

    def _burn(tag: str, extra: dict) -> Path:
        dst = sub_dir / f"{tag}.mp4"
        cfg = {"宽": 720, "高": 1280, "缩放": "blur", "帧率": 25, "CRF": 18,
               "原声音量": 0.0}
        cfg.update(extra)
        cmd, _ = assemble_cmd(bsegs, cfg, sub_dir, dst, sub_dir / CACHE_DIR)
        r = run(cmd, 600)
        c.ok(r.returncode == 0 and dst.exists(), f"{tag} 编码成功",
             (r.stderr or "")[-120:] if r.returncode else "")
        return dst

    def _yellow_px(mp4: Path, t: float) -> int:
        raw = sub_dir / "_t.rgb"
        run([exe, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t}",
             "-i", str(mp4), "-frames:v", "1", "-f", "rawvideo",
             "-pix_fmt", "rgb24", str(raw)], 180)
        d = raw.read_bytes()
        raw.unlink(missing_ok=True)
        return sum(1 for i in range(0, len(d), 3)
                   if d[i] > 190 and d[i + 1] > 190 and d[i + 2] < 100)

    with_sub = _burn("_烧字幕", {"字幕": str(srt_demo), "字幕样式": "yellow"})
    no_sub = _burn("_无字幕", {})
    px_on = _yellow_px(with_sub, 1.0)
    px_off = _yellow_px(no_sub, 1.0)
    c.ok(px_on > 1000 and px_off < 50, "字幕真的烧进了画面（黑底数黄字像素）",
         f"有字幕 {px_on} px / 无字幕 {px_off} px")

    # BGM：纯静音源 + BGM，响度必须从「无声」涨上来
    import re as _re

    def _mean_db(p: Path) -> float:
        r = run([exe, "-hide_banner", "-i", str(p), "-af", "volumedetect",
                 "-f", "null", "-"], 180)
        m = _re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr or "")
        return float(m.group(1)) if m else 0.0

    with_bgm = _burn("_加BGM", {"BGM": str(bgm), "BGM音量": 0.3, "闪避": True})
    db_off, db_on = _mean_db(no_sub), _mean_db(with_bgm)
    c.ok(db_on > db_off + 20, "BGM 混进了音轨",
         f"无 BGM {db_off:.0f}dB → 有 BGM {db_on:.0f}dB")
    c.ok(probe(with_bgm)["has_audio"], "加 BGM 后仍有音轨")

    print("\n15) 整季汇总：一屏看清这季出了什么")
    from .season import scan_season

    season = base / "_自检整季"
    if season.exists():
        shutil.rmtree(season)
    for spec_dir, name in (("1080x1920", "测试短剧_第1集_解说.mp4"),
                           ("1080x1920", "测试短剧_第2集_解说.mp4")):
        d = season / spec_dir
        d.mkdir(parents=True, exist_ok=True)
        # 直接复制成片当「已出片」，避免自检再编码两遍
        src_pick = next((p for p in sorted(out_fit.rglob("*.mp4"))
                         if CACHE_DIR not in p.parts), None)
        if src_pick:
            shutil.copy2(src_pick, d / name)
    (season / "1080x1920" / "测试短剧_第1集_解说.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (season / "_投放台账.xlsx").write_bytes(b"x")

    info = scan_season(season)
    c.ok(info["count"] == 2, "扫到 2 集", f"{info['count']} 集")
    c.ok(info["specs"] == ["1080x1920"], "识别出画幅目录", str(info["specs"]))
    c.ok([e["ep"] for e in info["episodes"]] == [1, 2], "按集数排序",
         str([e["ep"] for e in info["episodes"]]))
    c.ok(info["episodes"][0]["cover"] and not info["episodes"][1]["cover"],
         "有没有封面分得清")
    # 封面路径由后端给，前端不拿 .mp4 去 replace 猜 —— 成片是 .mov 时会猜错
    c.ok(info["episodes"][0].get("cover_path", "").endswith("_解说.jpg")
         and info["episodes"][1].get("cover_path") == "",
         "封面路径由后端给，前端不用猜",
         f'A={info["episodes"][0].get("cover_path", "")[-14:] or "空"} '
         f'B={info["episodes"][1].get("cover_path") or "空"}')
    c.ok(any("封面" in p for p in info["problems"]), "缺封面会报出来",
         str(info["problems"]))
    c.ok(info["total_mb"] > 0 and ":" in info["total_txt"], "总体积和总时长都有",
         f"{info['total_mb']}MB / {info['total_txt']}")
    c.ok(info["ledger"], "认出投放台账")
    c.ok(scan_season(base / "没有这个目录")["count"] == 0, "目录不存在时不报错")
    # 目录不存在时字段也要给全，否则前端每处都得判空
    empty_keys = {"exists", "root", "specs", "spec", "count", "episodes",
                  "total_mb", "total_txt", "problems", "ledger", "ledger_path"}
    missing = empty_keys - set(scan_season(base / "没有这个目录"))
    c.ok(not missing, "目录不存在时字段也给全", "缺 " + ",".join(sorted(missing)) if missing else "11 个键齐全")

    # 再纠一遍「按集数排」：文件名里第 10 集不能排在第 2 集前面
    d10 = season / "1080x1920"
    for n in (10, 2, 1):
        p = d10 / f"测试短剧_第{n}集_解说.mp4"
        if not p.exists() and src_pick:
            shutil.copy2(src_pick, p)
    eps10 = [e["ep"] for e in scan_season(season)["episodes"]]
    c.ok(eps10 == sorted(eps10), "第 10 集排在第 2 集后面（按数字不按字典）", str(eps10))

    # --------------------------------------------------------------- 截图
    print("\n16) 抽帧留证")
    shot_dir = base / "_自检截图"
    shot_dir.mkdir(exist_ok=True)
    target = next((p for p in sorted(out_fit.rglob("*.mp4"))
                   if CACHE_DIR not in p.parts and "1080x1920" in str(p)), None)
    shots = []
    if target:
        shots += [(target, 0.5, "1_片头"), (target, 3.5, "2_花字字幕"), (target, 13.8, "3_片尾")]
    if slices:
        shots.append((slices[0], 2.0, "4_slice字幕"))
    if ex_out:
        shots.append((ex_out[0], 1.2, "5_解说成片"))
    for src_file, t, tag in shots:
        run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
             "-ss", str(t), "-i", str(src_file), "-frames:v", "1",
             str(shot_dir / f"{tag}_{t}s.png")])
    c.ok(len(list(shot_dir.glob("*.png"))) >= 4, "截图已生成", f"{len(shots)} 张")
    print(f"   {shot_dir}")

    rc = c.report()
    print(f"产物在 {base}")
    return rc
