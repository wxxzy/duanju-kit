"""djkit.script

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .assemble import tc_to_sec
from .core import yaml


LLM_ENV = {
    "base_url": "DJKIT_LLM_BASE",
    "api_key": "DJKIT_LLM_KEY",
    "model": "DJKIT_LLM_MODEL",
}


SCRIPT_ANGLES = [
    "用悬念开场：先抛出一个让人想往下看的问题或反常事实",
    "用人物处境开场：直接把这个人的困境摆出来",
    "用一句反常识的判断开场：说出一个和直觉相反的结论",
]

# 和 SCRIPT_ANGLES 一一对应，给界面上 A/B/C 三版起名字用
SCRIPT_ANGLE_NAMES = ["悬念开场", "人物处境", "反常识判断"]


SCRIPT_SYSTEM = """你是短剧和影视解说文案写手，作品投放在抖音/快手竖屏信息流。

硬性要求：
1. 只能使用我给你的台词内容和背景设定，不许编造台词里没有的具体情节、人名、数字。
   可以用台词支撑的推断，但不能凭空加戏。
2. 中文口语，短句，一句一行，每行 10–18 字。
3. **必须写满 {min_lines}–{max_lines} 行，总字数 {lo}–{hi} 字。**
   目标是念出来 {seconds} 秒。字数不够就是不合格，宁可把台词里的细节展开写，也不要凑空话。
4. 第一句必须是钩子，三秒内抓住人。不要开场白、不要"大家好"、不要自我介绍。
5. 结尾停在悬念上，不要总结、不要升华、不要说教。
6. 不要出现"本期""视频""我们来看看""接下来"这类出戏的词。
7. **台词是按画面顺序给的，每段前面有【第 N 段】标记。文案必须按同样顺序推进，
   不许把后面段落的内容提前说。开头两句只能用【第 1 段】里有的内容。**
8. 纯文本输出，不要 markdown、不要编号、不要时间码、不要加引号包裹整段。

风格：{style}
{angle}"""


def load_llm_config(path: str | None = None) -> dict:
    """配置优先级：--config 文件 > 环境变量 > ~/.djkit/llm.yaml"""
    cfg = {"base_url": "", "api_key": "", "model": "qwen3_6"}
    for f in ([Path(path).expanduser()] if path else []) + [Path.home() / ".djkit" / "llm.yaml"]:
        if f and f.exists():
            try:
                d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                for k in ("base_url", "api_key", "model"):
                    if d.get(k):
                        cfg[k] = str(d[k]).strip()
                break
            except Exception:
                continue
    for k, env in LLM_ENV.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env].strip()
    return cfg


def llm_chat(cfg: dict, messages: list[dict], temperature: float = 0.85,
             max_tokens: int = 4000, timeout: int = 240) -> str:
    """调 OpenAI 兼容的 /chat/completions。只用标准库，不引入额外依赖。

    注意：qwen3 这类推理模型会先消耗大量 reasoning tokens 再输出正文，
    max_tokens 给小了会出现 finish_reason=length 且 content 为空。
    """
    import urllib.error
    import urllib.request

    base = cfg["base_url"].rstrip("/")
    if not base or not cfg["api_key"]:
        raise SystemExit("未配置 LLM：请设置 DJKIT_LLM_BASE / DJKIT_LLM_KEY，"
                         "或写入 ~/.djkit/llm.yaml")
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # qwen3 默认会先长篇思考，约束一多就在思考里反复复述要求、把额度耗光。
        # vLLM 系服务接受这个开关；不认的服务会返回 400，下面会自动回退重试。
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 422):
            # 服务端不认识 enable_thinking，去掉重试一次
            payload = json.loads(body.decode("utf-8"))
            payload.pop("chat_template_kwargs", None)
            req2 = urllib.request.Request(
                base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {cfg['api_key']}",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req2, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception as e2:
                raise SystemExit(f"请求失败：{type(e2).__name__} {e2}")
        else:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise SystemExit(f"接口返回 {e.code}：{detail}")
    except Exception as e:
        raise SystemExit(f"请求失败：{type(e).__name__} {e}")

    msg = (d.get("choices") or [{}])[0].get("message") or {}
    finish = (d.get("choices") or [{}])[0].get("finish_reason")
    text = (msg.get("content") or "").strip()
    if not text:
        hint = "（思考过程把额度用完了，调大 max_tokens）" if finish == "length" else ""
        raise SystemExit(f"返回内容为空{hint}。片段：{json.dumps(d, ensure_ascii=False)[:240]}")
    return text


def clean_script(text: str) -> list[str]:
    """去掉模型爱加的编号、markdown、引号，按行切"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    lines = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^(\d+[\.、\)]|[-*·•])\s*", "", s)
        s = s.strip("「」『』\"'“”")
        s = s.replace("**", "").replace("#", "")
        if s:
            lines.append(s)
    return lines


_LINE_TC = re.compile(r"^\[(\d{2}):(\d{2})-")


def mark_segments(txt_path: Path, durs: list[float], dst: Path) -> Path:
    """按片段时长给台词加【第 N 段】标记。

    为什么必须加：台词是一串连续对白，模型分不清哪句属于哪段画面，
    就会按「叙事逻辑」重排，导致解说比画面慢一个片段。标上边界之后，
    它可以照着顺序写。
    """
    lines = txt_path.read_text(encoding="utf-8-sig").splitlines()
    bounds: list[float] = []
    acc = 0.0
    for d in durs:
        acc += float(d)
        bounds.append(acc)

    out: list[str] = []
    cur = 0          # 当前台词所属片段
    written = 0      # 已经写过几段标题
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _LINE_TC.match(line)
        if m and bounds:
            t = int(m.group(1)) * 60 + int(m.group(2))
            while cur < len(bounds) - 1 and t >= bounds[cur]:
                cur += 1
        while written <= cur:
            out.append(f"【第{written + 1}段】")
            written += 1
        out.append(line)

    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    return dst


def read_transcript(path: Path, t_from: float = 0.0, t_to: float = 1e9) -> str:
    """读 asr 产物（.txt 带时间码 / .json），按时间范围裁"""
    if path.suffix.lower() == ".json":
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            picked = [r for r in rows if t_from <= float(r.get("start", 0)) <= t_to]
            return "\n".join(str(r.get("text", "")).strip() for r in picked if r.get("text"))
        except Exception:
            pass
    out = []
    pat = re.compile(r"^\[(\d{2}):(\d{2})-(\d{2}):(\d{2})\]\s*(.+)$")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = pat.match(line.strip())
        if m:
            sec = int(m.group(1)) * 60 + int(m.group(2))
            if t_from <= sec <= t_to:
                out.append(m.group(5).strip())
        elif line.strip() and not line.strip().startswith("["):
            out.append(line.strip())
    return "\n".join(out)


def tc_sec(s: str) -> float:
    if not s:
        return 0.0
    return tc_to_sec(s)


def cmd_script(args) -> int:
    src = Path(args.transcript).expanduser().resolve()
    if not src.exists():
        print(f"转写文件不存在：{src}")
        return 2

    cfg = load_llm_config(getattr(args, "config", None))
    text = read_transcript(src, tc_sec(args.start), tc_sec(args.end) or 1e9)
    if not text.strip():
        print("选定区间内没有台词。")
        return 2
    if len(text) > 7000:
        text = text[:7000]
        print("⚠ 台词过长，已截断到 7000 字，建议用 --start/--end 缩小区间")

    seconds = float(args.seconds)
    # 4.1 字/秒：实测 edge-tts 中文含标点停顿约 4.2–4.3 字/秒，留一点余量避免配完超长
    chars = int(seconds * 4.1)
    lo, hi = int(chars * 0.85), int(chars * 1.15)
    min_lines = max(3, round(lo / 18))
    max_lines = max(min_lines + 1, round(hi / 13))
    n = max(1, int(args.variants))
    out_dir = Path(args.out).expanduser().resolve() if args.out else src.parent / "文案"
    print(f"台词 {len(text)} 字 → 目标 {seconds:g}s / {lo}–{hi} 字 / "
          f"{min_lines}–{max_lines} 行 / {n} 个版本（模型 {cfg['model']}）")

    def build_messages(angle: str) -> list[dict]:
        sys_prompt = SCRIPT_SYSTEM.format(
            seconds=seconds, chars=chars, lo=lo, hi=hi,
            min_lines=min_lines, max_lines=max_lines,
            style=args.style, angle=angle,
        )
        user = ""
        if args.context:
            user += "背景设定（可以引用，不要扩写）：\n" + args.context.strip() + "\n\n"
        user += "台词如下：\n" + text
        return [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user}]

    if args.dry_run:
        print("\n—— 台词 ——\n" + text[:600])
        msgs = build_messages(SCRIPT_ANGLES[0])
        print(f"\n—— system ——\n{msgs[0]['content']}")
        if args.context:
            print(f"\n—— 背景 ——\n{args.context}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, list[str]]] = []
    for i in range(n):
        angle = SCRIPT_ANGLES[i % len(SCRIPT_ANGLES)]
        try:
            raw = llm_chat(cfg, build_messages(angle), temperature=0.85 + 0.05 * i)
        except SystemExit as e:
            print(f"  第 {i+1} 版失败：{e}")
            continue
        lines = clean_script(raw)
        if not lines:
            continue
        body = "\n".join(lines) + "\n"
        path = out_dir / f"文案_{chr(65 + i)}.txt"
        path.write_text(body, encoding="utf-8")
        est = len("".join(lines)) / 4.3
        results.append((path.name, lines))
        print(f"  {path.name}  {len(''.join(lines))} 字  预计 {est:.1f}s"
              + ("  ⚠ 偏长" if est > seconds * 1.2 else "")
              + ("  ⚠ 偏短" if est < seconds * 0.8 else ""))
        print("    " + " / ".join(lines[:3]) + (" …" if len(lines) > 3 else ""))

    if not results:
        print("没有生成任何版本。")
        return 1
    print(f"\n文案已写入 {out_dir}")
    print(f"下一步：python -m djkit tts {out_dir / results[0][0]} --voice zh-CN-YunjianNeural")
    return 0
