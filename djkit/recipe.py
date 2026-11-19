"""djkit.recipe

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from .compose import cmd_fit
from .core import abs_path, as_bool, as_list, rg, yaml
from .explainer import run_explainer
from .media import cmd_scan
from .report import cmd_report
from .slicer import cmd_slice


def is_explainer(job: dict) -> bool:
    """判断配方是「解说」还是「切片」流水线"""
    mode = str(rg(job, "模式", "mode", default="")).strip().lower()
    if mode in ("解说", "explainer", "narration"):
        return True
    if mode in ("切片", "slice", "clip"):
        return False
    return isinstance(job.get("解说"), dict) or "解说" in job


def recipe_plan(job: dict) -> list[str]:
    """把配方翻译成人话，跑之前先让人看清楚要干什么"""
    if is_explainer(job):
        ex = job.get("解说") if isinstance(job.get("解说"), dict) else {}
        steps = [
            f"高光检测（{rg(ex, '段数', 'top', default=6)} 段 × "
            f"{rg(ex, '段长', 'len', default=8)}s）",
            "转写台词",
            "生成文案",
            "配音",
        ]
        if rg(job, "花字", "title"):
            steps.append("加花字")
        if rg(job, "BGM", "bgm"):
            steps.append("混 BGM")
        steps.append("拼接出片")
        if rg(job, "封面", "cover") is not None:
            steps.append("出封面")
        return steps
    steps = []
    if as_bool(rg(job, "扫描", "scan"), True):
        steps.append("扫描素材并导出清单")
    if job.get("切片"):
        sl = job["切片"]
        mode = rg(sl, "模式", "mode", default="head")
        steps.append(f"切片（{mode}）")
    if rg(job, "字幕", "sub"):
        steps.append("烧字幕")
    if job.get("花字") or rg(job, "标题", "title"):
        steps.append("加花字")
    if rg(job, "变速", "speed", default=1) not in (1, 1.0, "1"):
        steps.append(f"变速 {rg(job, '变速', 'speed')}x")
    if rg(job, "BGM", "bgm"):
        steps.append("混 BGM")
    if rg(job, "片头", "intro") or rg(job, "片尾", "outro"):
        steps.append("拼片头片尾")
    if rg(job, "封面", "cover"):
        steps.append("出封面")
    specs = as_list(rg(job, "规格", "specs", "尺寸", "size", default="1080x1920"))
    steps.append("出 " + "、".join(specs))
    rep = rg(job, "台账", "report", default=True)
    if isinstance(rep, str):
        if rep.strip().lower() not in ("false", "no", "0", "否", "关"):
            steps.append("生成投放台账")
    elif as_bool(rep, True):
        steps.append("生成投放台账")
    if as_bool(rg(job, "清理", "cleanup"), False):
        steps.append("清理中间切片")
    return steps


def run_job(job: dict, base: Path, dry_run: bool, no_cache: bool, workers: int) -> int:
    """执行一份配方，返回 0 表示全部成功"""
    src = abs_path(base, rg(job, "素材", "输入", "src", "input"))
    if not src or not Path(src).exists():
        print(f"✗ 素材目录不存在：{src}")
        return 2
    out = abs_path(base, rg(job, "输出", "out", "output")) or str(Path(src) / "_成片")

    print(f"\n{'=' * 62}")
    print(f"素材  {src}")
    print(f"输出  {out}")
    print("步骤  " + " → ".join(recipe_plan(job)))
    print("=" * 62)

    # 解说模式和切片模式是两条完全不同的流水线，这里分流
    if is_explainer(job):
        return run_explainer(job, base, Path(out), workers, dry_run)

    # ---- 1. 扫描 ----
    if as_bool(rg(job, "扫描", "scan"), True):
        cmd_scan(argparse.Namespace(dir=src, out=None, jobs=workers, no_cache=no_cache))

    # ---- 2. 切片 ----
    fit_src = src
    slice_dir = None
    sl = job.get("切片")
    if sl:
        sl = sl if isinstance(sl, dict) else {}
        # 中间切片默认放在成片目录**外面**，否则会被 report/fit 当成成品扫进去
        slice_dir = (
            abs_path(base, rg(job, "切片输出", default=None))
            or str(Path(out).parent / f"_切片_{Path(out).name}")
        )
        cmd_slice(argparse.Namespace(
            dir=src, out=slice_dir, jobs=workers,
            mode=rg(sl, "模式", "mode", default="head"),
            head=float(rg(sl, "时长", "head", default=25)),
            seg=float(rg(sl, "段长", "seg", default=30)),
            ranges=str(rg(sl, "区间", "ranges", default="0-15")),
            copy=as_bool(rg(sl, "流拷贝", "copy")),
            crf=int(rg(job, "CRF", "crf", default=20)),
            dry_run=dry_run, no_cache=no_cache,
            burn_sub=False, sub_dir=None, sub_style="default", sub_size=None, sub_font=None,
        ))
        fit_src = slice_dir

    if dry_run and slice_dir and not Path(slice_dir).exists():
        print("\n（切片产物还没生成，成片预演跳过；真跑时会先切再合成）")
        return 0

    # ---- 3. 成片合成 ----
    subs = [abs_path(base, x) for x in as_list(rg(job, "字幕", "sub"))]
    subs = [x for x in subs if x]
    specs = as_list(rg(job, "规格", "specs", "尺寸", "size", default="1080x1920"))
    covers = as_list(rg(job, "封面尺寸", "cover_sizes"))

    t = job.get("花字") if isinstance(job.get("花字"), dict) else {}
    title_fixed = rg(t, "文字", "title") or rg(job, "标题", "title")
    title_tpl = rg(t, "模板", "tpl")
    title_file = abs_path(base, rg(t, "表", "file") or rg(job, "标题表", "title_file"))
    has_title = bool(title_fixed or title_tpl or title_file)

    fit_ns = argparse.Namespace(
        dir=fit_src, out=out, jobs=workers,
        size=specs[0] if specs else "1080x1920",
        specs=",".join(specs) if specs else None,
        mode=rg(job, "缩放", "mode", default="blur"),
        crf=int(rg(job, "CRF", "crf", default=20)),
        fps=int(rg(job, "帧率", "fps", default=25)),
        intro=abs_path(base, rg(job, "片头", "intro")),
        outro=abs_path(base, rg(job, "片尾", "outro")),
        speed=float(rg(job, "变速", "speed", default=1.0)),
        bgm=abs_path(base, rg(job, "BGM", "bgm")),
        bgm_vol=float(rg(job, "BGM音量", "bgm_vol", default=0.25)),
        bgm_fade=float(rg(job, "BGM淡出", "bgm_fade", default=2.0)),
        duck=as_bool(rg(job, "闪避", "duck")),
        cover=str(rg(job, "封面", "cover")) if rg(job, "封面", "cover") is not None else None,
        cover_sizes=",".join(covers) if covers else None,
        burn_sub=bool(subs),
        sub_dir=subs or None,
        sub_style=rg(job, "字幕样式", "sub_style", default="default"),
        sub_size=rg(job, "字幕字号", "sub_size"),
        sub_font=rg(job, "字幕字体", "sub_font"),
        title=title_fixed,
        title_tpl=title_tpl,
        title_file=title_file,
        title_style=rg(t, "样式", "style") or rg(job, "花字样式", default="bar"),
        title_start=float(rg(t, "开始", "start", default=0.3)),
        title_dur=float(rg(t, "时长", "dur", default=3.0)),
        title_size=float(rg(t, "字号", "size", default=96)),
        title_pos=rg(t, "位置", "pos", default="top"),
        title_y=rg(t, "纵向", "y"),
        title_color=rg(t, "颜色", "color"),
        title_border=rg(t, "描边", "border"),
        title_fade=float(rg(t, "淡入", "fade", default=0.3)),
        title_font=rg(t, "字体", "font"),
        dry_run=dry_run, no_cache=no_cache,
    )
    if has_title or subs or fit_ns.intro or fit_ns.outro or fit_ns.bgm or fit_ns.speed != 1.0:
        rc = cmd_fit(fit_ns)
    else:
        # 纯转规格也走同一条路径，保证行为一致
        rc = cmd_fit(fit_ns)
    if rc:
        return rc

    # ---- 4. 台账 ----
    rep = rg(job, "台账", "report", default=True)
    report_out = None
    if isinstance(rep, str):
        if rep.strip().lower() in ("false", "no", "0", "否", "关"):
            report_out = None
        else:
            report_out = abs_path(base, rep) or str(Path(out) / "_投放台账.xlsx")
    elif as_bool(rep, True):
        report_out = str(Path(out) / "_投放台账.xlsx")
    if report_out and not dry_run:
        cmd_report(argparse.Namespace(dir=out, out=report_out, jobs=workers, no_cache=no_cache))

    # ---- 5. 清理中间产物 ----
    if as_bool(rg(job, "清理", "cleanup"), False) and slice_dir and Path(slice_dir).is_dir():
        if Path(slice_dir).resolve() != Path(src).resolve():
            shutil.rmtree(slice_dir, ignore_errors=True)
            print(f"已清理中间切片：{slice_dir}")

    return 0


def cmd_run(args) -> int:
    if yaml is None:
        print("缺少 PyYAML，请先 pip install pyyaml")
        return 2
    rp = Path(args.recipe).expanduser().resolve()
    if not rp.exists():
        print(f"配方不存在：{rp}")
        return 2
    try:
        data = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"配方解析失败：{e}")
        return 2

    base = rp.parent
    jobs = data if isinstance(data, list) else [data]
    jobs = [j for j in jobs if isinstance(j, dict)]
    if not jobs:
        print("配方里没有可执行的内容。")
        return 2

    print(f"配方 {rp.name}：{len(jobs)} 个项目"
          + (f"，监听间隔 {args.watch}s" if args.watch else ""))

    cycle = 0
    while True:
        cycle += 1
        if args.watch:
            print(f"\n—— 第 {cycle} 轮 ——————————————")

        rc = 0
        for i, job in enumerate(jobs, 1):
            if len(jobs) > 1:
                print(f"\n[{i}/{len(jobs)}] {rg(job, '名称', 'name', default='未命名项目')}")
            rc |= run_job(job, base, args.dry_run, args.no_cache, args.jobs)

        if not args.watch:
            return rc
        if args.cycles and cycle >= args.cycles:
            print(f"\n已跑满 {cycle} 轮，退出。")
            return rc
        time.sleep(max(1, int(args.watch)))
