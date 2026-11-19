"""djkit.report

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

from pathlib import Path

from .core import fmt_sec
from .media import collect_args


def cmd_report(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    clips = collect_args(args, root, need_hash=True)
    if not clips:
        print("没有找到视频文件。")
        return 1

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("缺少 openpyxl，请先 pip install openpyxl")
        return 2

    wb = Workbook()
    ws = wb.active
    ws.title = "素材台账"

    headers = ["序号", "剧名", "集数", "素材文件", "时长", "分辨率", "大小(MB)", "指纹",
               "投放平台", "发布日期", "作品链接", "状态", "备注"]
    ws.append(headers)

    head_fill = PatternFill("solid", fgColor="C00000")
    head_font = Font(color="FFFFFF", bold=True, size=11)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for i, c in enumerate(clips, 1):
        ws.append([
            i, c.title, c.ep if c.ep is not None else "", c.path.name,
            fmt_sec(c.duration), c.res, round(c.size / 1048576, 2), c.fp,
            "", "", "", "待投放", "",
        ])

    for cell in ws[1]:
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    widths = [6, 24, 8, 40, 10, 12, 12, 14, 14, 14, 30, 10, 20]
    for i, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=len(headers)):
        for cell in row:
            cell.border = border
            if cell.column != 1:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    # 说明页
    note = wb.create_sheet("使用说明")
    lines = [
        ["短剧推广素材台账", ""],
        ["", ""],
        ["怎么用", "在「投放平台 / 发布日期 / 作品链接 / 状态」里填自己的数据，状态可写：待投放 / 已发布 / 已下架"],
        ["自动重算", "本表由 djkit report 生成，重新跑会覆盖本文件；建议另存为「投放跟踪_日期.xlsx」再手工填"],
        ["", ""],
        ["授权提醒", "素材须来自剧方或推广平台下发的授权素材包；录屏、解析、搬运成片切片不属于授权范围"],
        ["二创要求", "各平台普遍要求加解说、字幕、混剪等再创作，纯搬运原片大概率被判搬运/限流"],
        ["平台规则", "各平台推广政策与素材规范会变，投放前以当期官方文档为准"],
    ]
    for ln in lines:
        note.append(ln)
    note.column_dimensions["A"].width = 14
    note.column_dimensions["B"].width = 92
    note["A1"].font = Font(bold=True, size=14, color="C00000")
    for r in range(3, 9):
        note.cell(row=r, column=1).font = Font(bold=True)

    out = Path(args.out) if args.out else root / "_投放台账.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"台账已生成：{out}")
    print(f"共 {len(clips)} 条素材，{len({c.title for c in clips})} 部剧。")
    return 0
