"""剧集总纲：一次性读完一整季，建一份可复用的「剧集记忆」。

为什么需要：现在每集完全独立生成，模型不知道上一集发生了什么。结果是
第 8 集还在重新介绍「汤姆是历史老师」，10 条视频像 10 个孤立片段。

四段式（顺序不能换）：

  1. 每集采样若干段音频（默认 7 分钟，均匀分布）→ 转写
  2. **先从全季台词里抽一张译名表**，同一人只能有一个中文名
  3. 用译名表约束每集摘要 → 150 字
  4. 汇总成主线 + 人物 + 名词，写成 总纲.md（给人改）和 总纲.json（给程序用）

第 2 步是踩出来的：不做译名表时，同一部剧的主角在第一集叫「迈克尔·伯纳姆」、
第二集叫「米歇尔·伯恩汉姆」，主线段落里两种叫法混着用。跨集一致性
必须由一张表来保证，不能指望每次调用都译得一样。

两条纪律：
  * **不转写全季**。10 集全量约 50 分钟，采样后约 8 分钟，而总纲要的是主线不是每句台词。
  * **总纲写错一个设定，后面每集都会跟着错**。所以 总纲.md 一旦人工改过，
    程序不再覆盖（除非 --force）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from .asr import cmd_asr
from .core import CACHE_DIR, ffmpeg_exe, fmt_sec, probe_cached, run
from .media import iter_videos, parse_episode

# ---------------------------------------------------------------- 采样

def sample_windows(dur: float, per_ep_min: float = 7.0, slice_sec: float = 84.0,
                   head: float = 100.0, tail: float = 60.0) -> list[tuple[float, float]]:
    """在正片范围内均匀取几段。

    跳过头 100 秒（片头 + 前情回顾）和末尾 60 秒（片尾字幕），
    这些位置几乎没信息量，占了采样额度反而漏掉剧情。
    前情回顾尤其要跳：上一集的剧情混进来，会让这一集的摘要写错集。
    """
    if dur <= head + tail + 40:
        return [(0.0, dur)]
    lo, hi = head, dur - tail
    span = hi - lo
    n = max(1, int(round(per_ep_min * 60 / slice_sec)))
    n = min(n, max(1, int(span // (slice_sec * 0.8))))
    if n <= 1:
        return [(lo, min(hi, lo + slice_sec))]
    step = (span - slice_sec) / (n - 1)
    return [(lo + i * step, lo + i * step + slice_sec) for i in range(n)]


def build_sample_audio(src: Path, dst: Path, windows: list[tuple[float, float]],
                       cache: Path) -> bool:
    """把采样段拼成一条 16k 单声道，转写只跑这一条"""
    meta = probe_cached(src, cache)
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    parts, labels = [], []
    for i, (a, b) in enumerate(windows):
        d = max(0.3, b - a)
        if not meta["has_audio"]:
            cmd += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        else:
            cmd += ["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(src)]
        parts.append(f"[{i}:a]aresample=16000,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]")
        labels.append(f"[a{i}]")
    if not parts:
        return False
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[aout]",
            "-ar", "16000", "-ac", "1", str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = run(cmd, timeout=1800)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000


# ---------------------------------------------------------------- 提示词

GLOSSARY_SYSTEM = """你在为一部剧建立「译名表」，让整季的解说文案称呼统一。

任务：从给出的分集台词里，找出反复出现的人名、地名、组织名、专有名词，
给出统一的简体中文译名。

规则：
1. 只收台词里出现过的，不要补充外部知识。
2. **同一个人只能有一条。** 台词里同时出现名、姓、全称、军衔称呼时
   （如 Burnham / Michael / Michael Burnham / Commander Burnham），
   必须合并成一条：用**全名**作译名，其余写法放进「别名」。
   这一步做错，后面每一集都会把一个人写成两个人 —— 这是最严重的错误。
   台词是语音转写，可能有错拼，按出现最多的拼法归并。
3. 「类型」只能是：人物 / 地点 / 组织 / 设定。
4. 中文译名优先用通行译法；拿不准就音译，并在「备注」写「存疑」。
   「备注」只写一句、20 字以内（如「存疑」或「舰名」）。
   **不要在任何字段里写你的推理过程**，那些话写进「不确定」。
5. 只输出 JSON，不要解释、不要 markdown 代码块。

JSON 结构：
{"术语": [{"原名": "Michael Burnham", "译名": "迈克尔·伯纳姆", "类型": "人物",
          "别名": ["Burnham", "Michael", "Commander Burnham"], "备注": ""}]}"""

EP_SYSTEM = """你在为一部剧的某一集建立「剧情记录」，供之后写这一集的解说文案时当背景。

你只能看到这一集的若干采样片段，不是全片。

硬性规则：
1. **只写台词里明确出现的内容。** 没听到的写「未交代」，绝对不要推测、不要补前因后果、
   不要用你对这部剧已有的了解填空。
2. 人物称呼必须严格用给定译名表里的译名。表里没有的人名，用台词原文并标「（未入表）」。
   **译名表是这一集的出场表，不是全剧人物表。** 表里有的人如果本集台词里没出现，
   就不要写他，更不要因为他「在别集当过舰长」就把本集的行动安到他头上。
   台词只说「舰长」而没给名字时，就写「舰长」，不要填名字。
3. 台词是语音转写，会有错词。遇到明显不通的地方，宁可写「台词不清」。
4. 「事件」按采样里出现的先后顺序写，3~5 条，只写动作和决定，不写动机。
   同一件事不要写两遍：台词里重复出现的命令，合并成一条。
5. 拿不准的疑问放进「不确定」，不要塞进「事件」。
6. 「新增术语」只收本集台词里确实出现、且不在译名表里的名字，供后面几集沿用。
   **「译名」必须填**（音译即可，不要留空），否则后面几集没法沿用这个称呼；
   「原名」没听清可以留空。不要为了凑数编名字。
6. 只输出 JSON，不要解释、不要 markdown 代码块。

JSON 结构：
{{"一句": "这一集发生了什么，40 字以内",
 "人物": [{{"名": "译名表里的译名", "事": "这一集里他做了什么，20 字以内"}}],
 "事件": ["按先后顺序，3~5 条，每条 25 字以内"],
 "设定": ["出现的新名词或设定"],
 "新增术语": [{"原名": "本集出现但译名表里没有的人名/名词", "译名": "", "类型": "",
             "别名": []}],
 "不确定": ["拿不准的地方"]}}"""

BIBLE_SYSTEM = """你在整理一部剧的「剧集总纲」，供之后给每一集写解说文案时当统一背景。

硬性规则：
1. 只基于给定的分集记录，**不要引入外部知识，不要补剧情**。记录里没有的，不要写。
2. 人物称呼必须严格用译名表里的译名，全篇一致；表里没有的沿用分集记录里的写法。
   **同一个人只能出现在「人物」里一次**：如果一个名字是另一个的别名（译名表的「别名」列），
   合并成一个人，不要把名和姓当成两个人。
3. 「主线」只写贯穿全季的那条线：核心冲突是什么、主角处在什么位置、局势怎么变。
   挑 6~8 个真正的转折点，写成 5~8 句连贯的段落，**450~650 字**。
   字数明显不够就是不合格——那是把分集事件抄了一遍，不是主线。
   **不要逐集复述事件**，不要出现「第 N 集：」这类标签；单集任务、支线、
   一次性的怪和地点都不要写——分集剧情另有表格，不用你重复。
   不写评价、不写升华。
4. 「关系」只写**最要紧的一两个**，用具体称谓或身份，20 字以内，
   例：「萨鲁的上级，泰勒的操控者」。
   **只能写分集记录里明确写到的关系，不要推测。**
   看不出来就留空——**留空比写错强**，写错会让后面每一集的文案都跟着错
   （实测出现过把联邦舰长写成「克林贡囚犯」）。
   **绝对不要罗列人名**——写成「与 A、B、C…有互动」等于没写，
   还会把每一条都撑成同样的一长串，整张人物表就废了。
5. 身份和立场都只能写「未明确」的人物，**不要收进「人物」**。
   那多半是语音识别把路人听成了名字，收进来只会污染后面每一集的提示词。
6. 「名词」只收剧情里承重的设定（会反复被提到的），不要收一次性道具。
   **解释要具体**：一句话说清它是什么、在剧情里起什么作用。
   只写得成「剧情中提及的设定」这类空话的，宁可不要收。
   分集记录里标了存疑的人名/名词，除非在别处得到印证，否则不要收进「人物」「名词」。
7. 只输出 JSON，不要解释、不要 markdown 代码块。

JSON 结构：
{{"剧名": "",
 "主线": "",
 "人物": [{{"名": "译名", "原名": "台词原文里的拼法", "身份": "", "立场": "", "关系": ""}}],
 "名词": [{{"词": "", "解释": ""}}]}}"""


NORMALIZE_SYSTEM = """你在整理一份剧集的译名表。同一个人可能被拆成了好几条
（名、姓、全称、军衔称呼各一条），需要合并成一条。

规则：
1. 同一个人只留一条：用全名作译名，其余写法全部放进「别名」。
2. 不同类型之间不合并（组织、地点、设定各自独立，哪怕名字相近）。
3. 只合并能确定是同一人的。拿不准就保留两条，**不要硬并**。
4. 保留每条的「原名」「类型」「备注」，不要新增你没见过的条目。
5. 只输出 JSON，不要解释：{"术语": [{"原名","译名","别名","类型","备注"}]}"""


FIXNAMES_SYSTEM = """你在校对一份影视剧译名表里的「英文原名」。
这些原名来自语音识别，会出现音近错拼——同一个名字被听成了别的词，
或者一个人被拆成了两条不相干的写法。

你要做的是**用你对这部作品的了解把拼写改回来**。只输出补丁，不要复述整张表。

规则：
1.「校正」：认得出是误听的，给出正确拼写。认不出就不要写进来——宁可漏，不可瞎改。
2.「合并」：确认是同一个人却被拆成两条的，指出哪几条并进哪一条，
   并给出合并后的英文全名和中文译名。
3. **绝不新增台词里没出现过的人物**。这一步只校正拼写和合并，不是补人物表。
4. 中文译名只在合并时给新全名的译法，其余不要动。
5. 「理由」一句话，**不超过 15 个字**——输出太长会被截断，JSON 就坏了。
6. 一条都不用改就输出 {"校正":[],"合并":[]}。

输出 JSON，不要解释、不要代码块：
{"校正":[{"原名":"Sanjo","正确":"Shenzhou","理由":"薛定谔级星舰，主角服役舰"}],
 "合并":[{"保留":"Georgiou","并入":["Philippa"],"全名":"Philippa Georgiou",
          "译名":"菲利帕·乔治乌","理由":"同一人"}]}

（上面的例子只示范格式和力度，不要照抄到别的剧里。）"""


def _fix_sig() -> str:
    return _sig(FIXNAMES_SYSTEM)


def apply_name_fixes(terms: list[dict], patch: dict) -> tuple[list[dict], list[dict]]:
    """把「校正 / 合并」补丁应用到译名表。纯本地逻辑，不调模型，方便断言。

    顺序有讲究：先改拼写（改名可能撞车，撞了就合并），再按模型给的合并关系并。
    反过来做的话，'并入' 里的名字可能已经被改掉，就找不到了。
    """
    applied: list[dict] = []
    pool = [dict(t) for t in terms]        # 不改调用方传进来的对象

    def find(name) -> dict | None:
        key = str(name or "").strip().lower()
        if not key:
            return None
        return next((t for t in pool
                     if str(t.get("原名") or "").strip().lower() == key), None)

    def absorb(dst: dict, src: dict) -> None:
        """src 并进 dst：对方的原名和别名全部留成别名，一个都不丢"""
        full = str(dst.get("原名") or "")
        alias = {str(a) for a in (dst.get("别名") or []) if a}
        alias.add(str(src.get("原名") or ""))
        alias.update(str(a) for a in (src.get("别名") or []) if a)
        alias.discard(full)
        dst["别名"] = [a for a in alias if a][:8]
        if src in pool:
            pool.remove(src)

    # 1. 改拼写
    for c in patch.get("校正") or []:
        if not isinstance(c, dict):
            continue
        old = str(c.get("原名") or "").strip()
        new = str(c.get("正确") or "").strip()
        if not old or not new or old.lower() == new.lower():
            continue                       # 「原样返回」不是改动，丢掉
        hit = find(old)
        if hit is None:
            continue
        twin = find(new)
        if twin is not None and twin is not hit:
            # 改完和已有条目撞名，说明本来就是同一条
            absorb(twin, hit)
            applied.append({"类型": "合并", "从": [old], "到": twin.get("原名"),
                            "理由": str(c.get("理由") or "改名后与已有条目撞车")[:40]})
            continue
        hit["原名"] = new
        applied.append({"类型": "校正", "从": [old], "到": new,
                        "理由": str(c.get("理由") or "")[:40]})

    # 2. 按模型给的合并关系并
    for c in patch.get("合并") or []:
        if not isinstance(c, dict):
            continue
        keep = find(c.get("保留") or "")
        if keep is None:
            continue
        gone: list[str] = []
        for name in c.get("并入") or []:
            other = find(name)
            if other is not None and other is not keep:
                gone.append(str(other.get("原名") or ""))
                absorb(keep, other)
        if not gone:
            continue
        full = str(c.get("全名") or "").strip()
        cn = str(c.get("译名") or "").strip()
        if full:
            keep["原名"] = full
            keep["别名"] = [a for a in (keep.get("别名") or []) if a != full]
        if cn:
            keep["译名"] = cn
        applied.append({"类型": "合并", "从": gone, "到": keep.get("原名"),
                        "理由": str(c.get("理由") or "")[:40]})
    return pool, applied


def fix_names(cfg: dict, title: str, terms: list[dict]) -> tuple[list[dict], list[dict]]:
    """让模型凭对作品的了解，把 ASR 造成的音近错拼改回来。

    这是唯一允许引入「外部知识」的一步，所以边界卡得很死：
    只改拼写、只合并，不新增条目。改错了顶多一条名字不对，
    但能救回 Sanjo→Shenzhou、Takuvma→T'Kuvma 这类每条都吃亏的错误。
    """
    if not terms or cfg is None:
        return terms, []
    slim = [{"原名": t.get("原名") or "", "译名": t.get("译名") or "",
             "别名": (t.get("别名") or [])[:4], "类型": t.get("类型") or ""}
            for t in terms]
    user = f"剧名：{title}\n\n译名表：\n" + json.dumps({"术语": slim}, ensure_ascii=False)
    try:
        # 2000 会截断（实测 104 条的补丁在 3800 字符左右被封顶），JSON 直接坏掉
        d = _llm_json(cfg, FIXNAMES_SYSTEM, user, temperature=0.1, max_tokens=3200)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        print(f"    专名校正失败（{e}），保留原表")
        return terms, []
    fixed, applied = apply_name_fixes(terms, d)
    if len(fixed) < len(terms) // 2:
        print("    专名校正结果异常（条数掉太多），保留原表")
        return terms, []
    return fixed, applied


# ---------------------------------------------------------------- 工具

def _sig(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _gloss_sig() -> str:
    return _sig(GLOSSARY_SYSTEM)


def _ep_sig() -> str:
    """分集摘要的提示词指纹。

    必须含提示词：不含的话改完规则重跑会「复用」旧结果，
    实测出现过日志写「共 0s」、一个字都没重算的情况。
    三种提示词分开签名，改汇总规则时不必连带重算 15 集摘要。
    """
    return _sig(EP_SYSTEM)


def _bible_sig() -> str:
    return _sig(BIBLE_SYSTEM, NORMALIZE_SYSTEM)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def _json_block(text: str) -> dict:
    """从模型回复里抠出 JSON。模型偶尔会包代码块或加一句废话。"""
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()
    if "{" not in raw:
        raise ValueError("回复里没有 JSON")
    body = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        return json.loads(body)
    except Exception:
        fixed = re.sub(r",\s*([}\]])", r"\1", body)
        return json.loads(fixed)


def _llm_json(cfg: dict, system: str, user: str, temperature: float = 0.3,
              tries: int = 3, max_tokens: int = 4000) -> dict:
    """要 JSON：解析失败就重试。

    模型偶尔会输出带语法错误的 JSON（漏逗号、字符串里夹引号）。
    以前直接把异常抛出去，一集摘要就废了——实测有一次整轮 15 集里有 1 集失败。
    """
    from .script import llm_chat

    last: BaseException | None = None
    for i in range(tries):
        try:
            raw = llm_chat(cfg, [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                           temperature=temperature + 0.05 * i, max_tokens=max_tokens)
            return _json_block(raw)
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001
            last = e
            print(f"    解析失败（第 {i + 1}/{tries} 次）：{str(e)[:70]}")
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"JSON 生成失败：{last}")


def _llm(cfg: dict, system: str, user: str, temperature: float = 0.3,
         tries: int = 3) -> str:
    """调 LLM，失败重试。

    接口会偶发 RemoteDisconnected，而 llm_chat 失败时抛的是 SystemExit
    （不是 Exception），不专门接住就会让整季跑到一半整体退出。
    """
    from .script import llm_chat

    last: BaseException | None = None
    for i in range(tries):
        try:
            return llm_chat(cfg, [{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                            temperature=temperature, max_tokens=4000)
        except BaseException as e:  # noqa: BLE001
            last = e
            if isinstance(e, KeyboardInterrupt):
                raise
            print(f"    接口失败（第 {i + 1}/{tries} 次）：{e}")
            if i < tries - 1:
                time.sleep(4 * (i + 1))
    raise RuntimeError(f"LLM 调用失败：{last}")


def even_excerpt(text: str, budget: int, chunks: int = 3) -> str:
    """从全文里均匀摘几段，而不是只取开头。

    只取开头会漏掉后半集出场的人物：实测 Saru、Philippa 这些主要角色
    都没进译名表，因为他们在采样台词的后半段才出现。
    """
    body = text.strip()
    if len(body) <= budget:
        return body
    lines = body.splitlines()
    if len(lines) < chunks * 4:
        return body[:budget]
    per = max(1, budget // chunks)
    step = max(1, (len(lines) - 1) // max(1, chunks - 1))
    out, used = [], set()
    for i in range(chunks):
        start = min(i * step, max(0, len(lines) - 1))
        acc = 0
        for j in range(start, len(lines)):
            if j in used:
                continue
            out.append(lines[j])
            used.add(j)
            acc += len(lines[j]) + 1
            if acc >= per:
                break
    return "\n".join(out)


def merge_aliases(terms: list[dict]) -> list[dict]:
    """把「别名」命中的独立条目并回主条目。

    为什么需要代码兜底：提示词说了「同一个人只能有一条」，模型仍会把
    Burnham 和 Michael 拆成两行（它在备注里都写明了是同一人）。
    这种错误一旦进总纲，每一集文案都会把一个人写成两个人。
    """
    # 别名 → 主条目（对象引用，改它等于改原表里的那条）。
    # 只处理「人物」：组织/地点之间常有包含关系（Starfleet 属于 Federation），
    # 一合并就会把两个不同的东西并成一个。
    host_of: dict[str, dict] = {}
    for t in terms:
        if str(t.get("类型") or "") != "人物":
            continue
        for a in (t.get("别名") or []):
            k = str(a).strip().lower()
            if k:
                host_of[k] = t

    out: list[dict] = []
    for t in terms:
        if str(t.get("类型") or "") != "人物":
            out.append(t)
            continue
        host = None
        for name in (t.get("原名"), t.get("译名")):
            k = str(name or "").strip().lower()
            if k and k in host_of and host_of[k] is not t:
                host = host_of[k]
                break
        if host is None:
            out.append(t)
            continue
        # 并进主条目：把这条的名字补进别名，本条不再单独出现
        host.setdefault("别名", [])
        for x in (t.get("原名"), t.get("译名")):
            x = str(x or "").strip()
            if x and x not in host["别名"]:
                host["别名"].append(x)
    return out


def slice_terms(terms: list[dict], text: str) -> list[dict]:
    """只保留在本集台词里出现过的术语。

    这是防「判断污染」的关键：全季译名表写着「洛卡＝发现号舰长」，
    第一集台词里根本没这个人，模型却把「Captain」全安给了洛卡
    （实测 E01 台词 0 次提到 Lorca，摘要却写「洛卡擅自下令开火」）。
    """
    low = text.lower()
    out = []
    for t in terms:
        names = [t.get("原名")] + list(t.get("别名") or [])
        if any(str(n).strip() and str(n).strip().lower() in low for n in names):
            out.append(t)
    return out


def add_terms(terms: list[dict], new: list[dict]) -> list[dict]:
    """把新发现的术语并进表，按原名/译名去重"""
    seen_orig = {str(t.get("原名", "")).strip().lower() for t in terms}
    seen_cn = {str(t.get("译名", "")).strip() for t in terms}
    for t in new:
        if not isinstance(t, dict):
            continue
        orig = str(t.get("原名") or "").strip()
        cn = str(t.get("译名") or "").strip()
        if not cn or (orig and orig.lower() in seen_orig) or cn in seen_cn:
            continue
        seen_orig.add(orig.lower())
        seen_cn.add(cn)
        terms.append({"原名": orig, "译名": cn[:24], "类型": str(t.get("类型") or "人物"),
                      "别名": [str(a).strip()[:40] for a in (t.get("别名") or [])][:6],
                      "备注": ""})
    return merge_aliases(terms)


def _glossary_hint(terms: list[dict], limit: int = 40) -> str:
    if not terms:
        return ""
    rows = []
    for t in terms[:limit]:
        if not t.get("译名"):
            continue
        alias = [str(a) for a in (t.get("别名") or []) if a]
        note = f"；又称 {'/'.join(alias)}" if alias else ""
        rows.append(f"{t.get('译名')}（{t.get('原名')}{note}）{t.get('类型', '')}")
    return "译名表（必须照此称呼）：\n" + "\n".join("- " + r for r in rows)


# ---------------------------------------------------------------- 1. 转写

def transcribe_episode(src: Path, work: Path, cache: Path, *, model: str, lang: str,
                       per_ep_min: float, force: bool) -> tuple[Path, str]:
    """采样 + 转写。返回 (txt 路径, 缓存键)"""
    meta = probe_cached(src, cache)
    wins = sample_windows(meta["duration"], per_ep_min)
    sampled = sum(b - a for a, b in wins)
    key = _sig(int(src.stat().st_mtime), model, lang, per_ep_min, len(wins))

    txt = work / "采样.txt"
    if not force and txt.exists():
        print(f"    复用转写（采样 {len(wins)} 段 / {fmt_sec(sampled)}）")
        (work / "采样.m4a").unlink(missing_ok=True)   # 转写完就没用了，3.5MB/集
        return txt, key

    print(f"    采样 {len(wins)} 段 / {fmt_sec(sampled)}（片长 {fmt_sec(meta['duration'])}）")
    sample = work / "采样.m4a"
    if not build_sample_audio(src, sample, wins, cache):
        raise RuntimeError("采样音频生成失败")

    t0 = time.perf_counter()
    rc = cmd_asr(argparse.Namespace(
        input=str(sample), out=str(work / "采样.srt"), model=model,
        lang=lang, device="cpu", compute="int8", beam=5, vad=True))
    if rc != 0:
        raise RuntimeError("转写失败")
    print(f"    转写 {time.perf_counter() - t0:.0f}s")
    # 采样音频转写完就没用了，15 集能省 50MB 左右
    try:
        sample.unlink(missing_ok=True)
    except OSError:
        pass
    return txt, key


# ---------------------------------------------------------------- 2. 译名表

def build_glossary(cfg: dict, eps: list[dict], per_ep_chars: int = 3000) -> list[dict]:
    """从全季台词里抽一张统一译名表。

    这是跨集一致性的唯一保障：不做这一步，同一人会在不同集译成不同的中文名
    （实测出现过「迈克尔·伯纳姆」和「米歇尔·伯恩汉姆」并存）。
    """
    blocks = []
    for e in eps:
        body = Path(e["txt"]).read_text(encoding="utf-8-sig")
        blocks.append(f"【第 {e.get('集') or '?'} 集】\n"
                      + even_excerpt(body, per_ep_chars))
    user = "\n\n".join(blocks)[:20000]
    try:
        d = _llm_json(cfg, GLOSSARY_SYSTEM, user, temperature=0.2)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        print(f"    译名表生成失败（{e}），改用分集自己的写法")
        return []
    terms = [t for t in (d.get("术语") or []) if isinstance(t, dict) and t.get("译名")]
    for t in terms:
        note = re.sub(r"\s+", " ", str(t.get("备注") or "")).strip()
        t["备注"] = note[:24] + ("…" if len(note) > 24 else "")
        t["别名"] = [str(a).strip()[:40] for a in (t.get("别名") or []) if str(a).strip()][:6]
    # 同一译名只留一条
    seen, out = set(), []
    for t in terms:
        k = str(t.get("译名")).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return merge_aliases(out)


# ---------------------------------------------------------------- 3. 分集摘要

def summarize_episode(cfg: dict, txt: Path, *, ep: int | None, key: str,
                      work: Path, terms: list[dict], force: bool,
                      gloss_key: str = "") -> dict:
    ep_json = work / "摘要.json"
    # 签名只认「转写 + 提示词 + 基础译名表」，**不含沿途增长的那部分**。
    # 沿途补充的名字每次跑都会略有出入，把它算进签名会让 15 集每跑一次就全量重算。
    full_key = _sig(key, _ep_sig(), gloss_key)
    if not force and ep_json.exists():
        try:
            d = json.loads(ep_json.read_text(encoding="utf-8"))
            if d.get("_key") == full_key:
                print(f"    复用摘要（{len(d.get('事件') or [])} 条事件）")
                return d
        except Exception:
            pass

    body = txt.read_text(encoding="utf-8-sig")
    if len(body) < 120:
        raise RuntimeError("采样里几乎没有台词，无法建记录")

    user = (f"这是第 {ep} 集。\n\n" if ep else "这是其中一集。\n\n")
    hint = _glossary_hint(terms)
    if hint:
        user += hint + "\n\n"
    user += "台词如下：\n" + body[:12000]

    d = _llm_json(cfg, EP_SYSTEM, user)
    d["_key"] = full_key
    d["集"] = ep
    ep_json.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


# ---------------------------------------------------------------- 4. 汇总

def normalize_glossary(cfg: dict, terms: list[dict]) -> list[dict]:
    """让模型把「名/姓/全称」被拆开的人物合并。

    出现在后期补名之后：新补进来的「菲利帕」和「乔治乌」各自独立，
    代码没法判断它们是同一个人（也可能真是两个人），交给模型看一遍。
    """
    if len(terms) < 3:
        return terms
    body = json.dumps({"术语": terms}, ensure_ascii=False)
    try:
        # 88 条 × 每条几十字，4000 token 会被截断，解析必败
        d = _llm_json(cfg, NORMALIZE_SYSTEM, body, temperature=0.1, max_tokens=9000)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        print(f"    译名合并失败（{e}），保留原表")
        return terms
    out = [t for t in (d.get("术语") or []) if isinstance(t, dict) and t.get("译名")]
    if len(out) < len(terms) // 2:
        print("    译名合并结果异常（条数掉太多），保留原表")
        return terms
    for t in out:
        note = re.sub(r"\s+", " ", str(t.get("备注") or "")).strip()
        t["备注"] = note[:24] + ("…" if len(note) > 24 else "")
    return merge_aliases(out)


def parse_names(raw: str) -> list[dict]:
    """解析 --names："Michael Burnham=迈克尔·伯纳姆, Saru=萨鲁"

    中英文冒号也认，因为很多人习惯写"原名：译名"。
    中文逗号/分号也认——这行字是人手打的，不该因为标点被打回去重打。
    """
    out: list[dict] = []
    for part in re.split(r"[,，;；\n]", raw or ""):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            a, b = part.split("=", 1)
        elif re.search(r"[:：]", part):
            a, b = re.split(r"[:：]", part, 1)
        else:
            continue
        a, b = a.strip(), b.strip()
        if a and b:
            out.append({"原名": a, "译名": b})
    return out


def apply_user_names(terms: list[dict], names: list[dict]) -> tuple[list[dict], int, int]:
    """用户给的译名表优先级最高：同名覆盖中文译名，表里没有的补进去。

    这是给「模型怎么改都不对」留的最后一手。现场做解说的人本来就认识角色，
    让他一次性把名字钉死，比反复调提示词靠谱。
    """
    added = changed = 0
    for n in names:
        key = n["原名"].strip().lower()
        hit = next((t for t in terms
                    if str(t.get("原名") or "").strip().lower() == key), None)
        if hit is not None:
            if str(hit.get("译名") or "") != n["译名"]:
                hit["译名"] = n["译名"][:24]
                changed += 1
            continue
        terms.append({"原名": n["原名"], "译名": n["译名"][:24], "类型": "人物",
                      "别名": [], "备注": "手工指定"})
        added += 1
    return terms, added, changed


# 光杆职务不是人名，进人物表只会制造「舰长」这种条目
TITLES = {"舰长", "船长", "上校", "中校", "少校", "上尉", "少尉", "指挥官",
          "将军", "上将", "大使", "医生", "博士", "教授", "皇帝", "长官",
          "舰桥", "轮机长", "通讯官", "科学官", "安全官", "船长"} 


def people_in_eps(eps: list[dict], terms: list[dict] | None = None) -> list[tuple[str, int]]:
    """统计分集记录里出现过的人物和出场集数，按出场多少排。

    用来给汇总那一步一份「必须写全」的名单。不这么干的话，
    模型每次吐出来的人数在 8~19 之间跳，少掉的那个后面几集就没人认得。

    给了 terms 就只算译名表里的「人物」：台词里的「舰长」「皇帝」这类职务称呼
    也会被摘要当成人物记下来（12 集都有「舰长」），拿它当名单会把人搞乱。
    """
    cnt: dict[str, int] = {}
    for e in eps:
        seen = {str(p.get("名") or "").strip() for p in (e.get("人物") or [])}
        for n in seen:
            if n:
                cnt[n] = cnt.get(n, 0) + 1
    if terms:
        known = {str(t.get("译名") or "").strip() for t in terms
                 if "人物" in str(t.get("类型") or "")}
        cnt = {n: c for n, c in cnt.items() if n in known}
    # 光杆职务不是人名。摘要里「舰长下令开火」这种会被记成人物「舰长」，
    # 12 集都有——真拿它当名单，模型就会写一条叫「舰长」的人物出来。
    cnt = {n: c for n, c in cnt.items() if n not in TITLES}
    return [(n, c) for n, c in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))]


def merge_bible(cfg: dict, title: str, eps: list[dict], terms: list[dict]) -> dict:
    blocks = []
    for e in eps:
        lines = [f"第 {e.get('集') or '?'} 集：{e.get('一句', '')}"]
        if e.get("人物"):
            lines.append("  人物：" + "；".join(
                f"{x.get('名')}（{x.get('事')}）" for x in e["人物"] if x.get("名")))
        if e.get("事件"):
            lines.append("  事件：" + " / ".join(str(x) for x in e["事件"][:6]))
        if e.get("设定"):
            lines.append("  设定：" + "、".join(str(x) for x in e["设定"][:6]))
        if e.get("不确定"):
            lines.append("  存疑：" + "；".join(str(x) for x in e["不确定"][:4]))
        blocks.append("\n".join(lines))

    hint = _glossary_hint(terms)
    roster = people_in_eps(eps, terms)
    # 名单只放「出场两集以上」的：只露一面的路人写进表里纯属噪音，
    # 但一个主要人物都不能少。整季都只有一集出场时（比如只跑了 1 集），才退回全量。
    must = [(n, c) for n, c in roster if c >= 2] or roster[:12]
    user = f"剧名：{title}\n共 {len(eps)} 集。\n\n"
    if hint:
        user += hint + "\n\n"
    if must:
        user += ("「人物」必须收下面这些（按出场集数排列，一个都不能漏）。"
                 "确实没线索的字段留空，但**不要整条删掉，也不要用「未明确」占位**：\n"
                 + "、".join(f"{n}（{c} 集）" for n, c in must[:24]) + "\n\n")
    user += "分集记录：\n\n" + "\n\n".join(blocks)
    d = _llm_json(cfg, BIBLE_SYSTEM, user, temperature=0.4, max_tokens=6000)
    d.setdefault("剧名", title)
    # 人物：三项都是「未明确 / 未知」的直接丢掉，留着只会稀释人物表
    empty = re.compile(r"^\s*(未明确|未知|未交代|不详|无)?\s*$")
    people = []
    for p_ in (d.get("人物") or []):
        if not isinstance(p_, dict) or not str(p_.get("名") or "").strip():
            continue
        bits = [str(p_.get(k) or "").strip() for k in ("身份", "立场", "关系")]
        # 用「包含」而不是「等于」：模型会写成「未明确（提及在己方一侧）」绕过去
        if all((not b) or re.search(r"未明确|未知|未交代|不详", b) for b in bits):
            continue
        people.append(p_)
    if len(people) < len(d.get("人物") or []):
        print(f"  丢掉 {len(d.get('人物') or []) - len(people)} 个信息为空的人物")

    # 出场两集以上还被丢掉的，补回来（信息留空）：宁可这条信息少，
    # 也不能让主要人物从表里消失——他一消失，后面几集文案就开始瞎猜
    have = {str(p.get("名") or "").strip() for p in people}
    added = 0
    for name, cnt in roster:
        if cnt < 2 or name in have:
            continue
        raw = next((t.get("原名") for t in terms
                    if str(t.get("译名") or "").strip() == name), "")
        people.append({"名": name, "原名": raw or "", "身份": "", "立场": "", "关系": ""})
        added += 1
    if added:
        print(f"  补回 {added} 个出场多集但被漏掉的人物")
    d["人物"] = people

    # 剔除「剧情中提及的设定」这类空话：说明模型没听明白，留着只会误导文案
    nouns = [n for n in (d.get("名词") or []) if isinstance(n, dict)]
    vague = re.compile(r"剧情中提及|剧情中出现|涉及.*背景|相关设定|^\s*$")
    keep = [n for n in nouns if not vague.search(str(n.get("解释") or ""))]
    if len(keep) < len(nouns):
        print(f"  丢掉 {len(nouns) - len(keep)} 个解释说不清的名词")
    d["名词"] = keep
    if not str(d.get("主线") or "").strip():
        raise RuntimeError("主线段落是空的")
    return d


def render_md(bible: dict, eps: list[dict], terms: list[dict], meta: dict) -> str:
    out = [f"# {bible.get('剧名') or meta.get('剧名', '')} · 剧集总纲", ""]
    out.append(f"> 自动生成：{time.strftime('%Y-%m-%d %H:%M')} · "
               f"采样 {meta.get('每集分钟', 7):g} 分钟/集 · {len(eps)} 集 · "
               f"模型 {meta.get('模型', '')}")
    out.append("> **给人看也给人改。** 改过之后程序不再覆盖它（除非 `series --force`）。"
               "写错一个设定，后面每一集的文案都会跟着错。")
    out.append("")
    out.append("## 主线")
    out.append("")
    out.append(str(bible.get("主线") or "").strip())
    out.append("")
    if bible.get("人物"):
        out.append("## 人物")
        out.append("")
        for p in bible["人物"]:
            bits = [str(p.get(k) or "").strip() for k in ("身份", "立场", "关系")]
            bits = [b for b in bits if b]
            raw = str(p.get("原名") or "").strip()
            name = f"**{p.get('名', '')}**" + (f"（{raw}）" if raw and raw != p.get("名") else "")
            out.append(f"- {name}" + ("　" + "｜".join(bits) if bits else ""))
        out.append("")
    if bible.get("名词"):
        out.append("## 名词与设定")
        out.append("")
        for n in bible["名词"]:
            out.append(f"- **{n.get('词', '')}**：{n.get('解释', '')}")
        out.append("")
    if terms:
        out.append("## 译名表")
        out.append("")
        out.append("| 原名 | 译名 | 别名 | 类型 | 备注 |")
        out.append("|---|---|---|---|---|")
        for t in terms:
            alias = "/".join(str(a) for a in (t.get("别名") or []) if a)
            out.append(f"| {t.get('原名', '')} | {t.get('译名', '')} | {alias} | "
                       f"{t.get('类型', '')} | {t.get('备注', '')} |")
        out.append("")
    out.append("## 分集")
    out.append("")
    out.append("| 集 | 一句话 |")
    out.append("|---|---|")
    for e in sorted(eps, key=lambda x: (x.get("集") or 0)):
        one = str(e.get("一句") or "").replace("|", "／")
        out.append(f"| {e.get('集') or '?'} | {one} |")
    out.append("")
    fixes = meta.get("校正") or []
    if fixes:
        out.append("## 已自动校正的专名")
        out.append("")
        out.append("台词是语音识别来的，以下是模型根据剧集常识改回来的拼写。**对不上就改回去**，"
                   "改完这个文件程序不会再覆盖。")
        out.append("")
        for f in fixes:
            src = "、".join(str(x) for x in (f.get("从") or []))
            why = f"　—　{f['理由']}" if f.get("理由") else ""
            out.append(f"- `{src}` → `{f.get('到', '')}`{why}")
        out.append("")
    doubt = [(e.get("集"), e["不确定"]) for e in eps if e.get("不确定")]
    if doubt:
        out.append("## 存疑（生成时拿不准的地方）")
        out.append("")
        for ep, items in sorted(doubt, key=lambda x: (x[0] or 0)):
            out.append(f"- 第 {ep} 集：" + "；".join(str(x) for x in items[:5]))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 读取侧

def _sections(md: str) -> dict[str, str]:
    """把总纲 Markdown 拆成 {小节名: 正文}"""
    out: dict[str, str] = {}
    cur = ""
    for line in md.splitlines():
        m = re.match(r"^#{2,3}\s*(.+?)\s*$", line)
        if m:
            cur = m.group(1).strip()
            out.setdefault(cur, "")
            continue
        if cur:
            out[cur] = out.get(cur, "") + line + "\n"
    return {k: v.strip() for k, v in out.items()}


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def load_context(path: Path, ep: int | None = None, limit: int = 2600) -> str:
    """把总纲转成可以塞进文案提示词的一段文字。

    按小节分配预算，而不是整篇截断：15 集时主线就有一千多字，
    整篇截断会把「人物」「名词」整个切掉，而那恰恰是最影响文案的部分。
    译名表和存疑是给人看的，不进提示词。
    """
    if not path.exists():
        return ""
    try:
        md = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""

    sec = _sections(md)
    main = _clip(sec.get("主线", ""), 1100)
    people = _clip(sec.get("人物", ""), 700)
    nouns = _clip(sec.get("名词与设定", "") or sec.get("名词", ""), 450)

    eps_txt = ""
    rows = []
    for line in (sec.get("分集", "") or "").splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|", line)
        if not m:
            continue
        if ep and int(m.group(1)) >= ep:
            continue          # 不把本集及以后的剧情塞进提示词
        rows.append(line.strip())
    if rows:
        eps_txt = "\n".join(rows[-8:])   # 只带最近几集，够接上前情

    parts = []
    if main:
        parts.append("主线：\n" + main)
    if people:
        parts.append("人物：\n" + people)
    if nouns:
        parts.append("名词：\n" + nouns)
    if eps_txt:
        parts.append("已播分集：\n" + eps_txt)

    head = ("【剧集总纲】以下是同一部剧的统一设定，用于统一称呼，不许另起叫法。\n"
            "**但总纲是整季视角，不是本集事实**：本集具体是谁在场、谁当舰长，以台词为准。\n"
            "台词只说「舰长」「指挥官」而没给名字时，就写职务，"
            "不要从下面的人物表里挑一个名字填进去。\n")
    if ep:
        head += (f"当前要做的是第 {ep} 集；下面只有本集之前的剧情，"
                 "不要提前讲后面的结局。\n")
    out = head + "\n".join(parts)
    return out[:limit]


# ---------------------------------------------------------------- 主流程

def build(src_dir: Path, out_dir: Path, *, model: str = "small", lang: str = "",
          per_ep_min: float = 7.0, episodes: int = 0, force: bool = False,
          dry_run: bool = False, remix: bool = False, names: str = "",
          no_fix: bool = False, should_stop=None) -> int:
    from .script import load_llm_config

    names_raw = str(names or "").strip()
    files = iter_videos(src_dir) if src_dir.is_dir() else [src_dir]
    if not files:
        print(f"没有找到视频文件：{src_dir}")
        return 1
    parsed = [(f, *parse_episode(f)) for f in files]
    parsed.sort(key=lambda x: (x[2] if x[2] is not None else 0, x[0].name))
    if episodes:
        parsed = parsed[:episodes]

    title = parsed[0][1]
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / CACHE_DIR
    state_file = out_dir / "总纲.json"
    md_file = out_dir / "总纲.md"

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    old_eps: dict = state.get("分集") or {}
    old_md_hash = state.get("md_hash", "")

    print(f"剧集总纲：{title} · {len(parsed)} 集 · 每集采样 {per_ep_min:g} 分钟 · 模型 {model}")
    print(f"输出  {md_file}")

    cfg = None if dry_run else load_llm_config(None)

    # ---- 只重汇总：调提示词时用，跳过转写和摘要 ----
    if remix:
        eps = list(old_eps.values())
        terms = state.get("译名表") or []
        if not eps:
            print("没有可复用的分集记录，先完整跑一次。")
            return 2
        print(f"重汇总（{len(eps)} 集 · 译名 {len(terms)} 条）…")
        manual = parse_names(names_raw)
        if manual:
            terms, added, changed = apply_user_names(list(terms), manual)
            print(f"  手工译名：新增 {added} 条、改译 {changed} 条")
        bible = merge_bible(cfg, title, eps, terms)
        md = render_md(bible, eps, terms,
                       {"剧名": title, "每集分钟": per_ep_min, "模型": model,
                        "校正": state.get("校正") or []})
        md_file.write_text(md, encoding="utf-8")
        state.update({"总纲": bible, "md_hash": _md5(md),
                      "生成时间": time.strftime("%Y-%m-%d %H:%M")})
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        print(f"\n✓ {md_file}（已覆盖）")
        print(f"  人物 {len(bible.get('人物') or [])} 个 · "
              f"名词 {len(bible.get('名词') or [])} 个 · "
              f"主线 {len(str(bible.get('主线') or ''))} 字")
        return 0

    def stopped() -> bool:
        """界面点取消时，在集与集之间退出。不中断正在跑的转写，但不会继续下一集。"""
        return bool(should_stop and should_stop())

    # ---- 1. 逐集转写 ----
    t_all = time.perf_counter()
    done: list[dict] = []
    for i, (f, t, ep) in enumerate(parsed, 1):
        if stopped():
            print(f"\n已取消（完成 {len(done)}/{len(parsed)} 集转写）")
            return 2
        print(f"\n[{i}/{len(parsed)}] 转写 · {f.name}")
        work = out_dir / "_工作" / f.stem
        work.mkdir(parents=True, exist_ok=True)
        if dry_run:
            meta = probe_cached(f, cache)
            wins = sample_windows(meta["duration"], per_ep_min)
            total = sum(b - a for a, b in wins)
            print(f"    采样 {len(wins)} 段 / {fmt_sec(total)}："
                  + "、".join(fmt_sec(a) for a, _ in wins))
            continue
        try:
            txt, key = transcribe_episode(f, work, cache, model=model, lang=lang,
                                          per_ep_min=per_ep_min, force=force)
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            print(f"    ✗ {e}")
            continue
        done.append({"文件": f.name, "集": ep, "txt": str(txt), "work": work, "key": key})

    if dry_run:
        return 0
    if not done:
        print("\n一集都没转写成功。")
        return 1

    # ---- 2. 译名表（一次，全季共享）----
    # g_key 只跟台词和提示词有关：用户改了 --names 不该触发重抽整张表（那要一次大调用）
    g_key = _sig(sorted(e["key"] for e in done), _gloss_sig())
    terms: list[dict] = []
    reused = False
    if not force and state.get("_gloss_key") == g_key and state.get("译名表"):
        terms = state["译名表"]
        reused = True
        print(f"\n复用译名表（{len(terms)} 条）")
    else:
        print(f"\n抽译名表（{len(done)} 集台词）…")
        terms = build_glossary(cfg, done)
        print(f"  译名 {len(terms)} 条" + (f"：{', '.join(t['译名'] for t in terms[:6])}…"
                                        if terms else ""))

    # ---- 2.5 校正拼写 + 手工覆盖（必须在逐集摘要之前，摘要才用得上对的称呼）----
    fixes: list[dict] = []
    if terms:
        if reused and state.get("校正") is not None and not force:
            fixes = state["校正"]          # 表是上次校正过的，直接沿用结论
            print(f"\n复用专名校正（{len(fixes)} 处）")
        elif no_fix:
            print("\n跳过专名校正（--no-fix）")
        else:
            before = len(terms)
            print(f"\n校正专名拼写（{len(terms)} 条，模型过一遍）…")
            terms, fixes = fix_names(cfg, title, terms)
            if fixes:
                terms = merge_aliases(terms)   # 合并后可能又出现「名 + 全名」
            print(f"  校正 {len(fixes)} 处，{before} 条 → {len(terms)} 条")
            for f in fixes[:8]:
                print(f"    {f['类型']}　{'、'.join(f['从'])} → {f['到']}")
        # 手工表永远重新应用：它是最外层的覆盖，改了就必须生效
        manual = parse_names(names_raw)
        if manual:
            terms, added, changed = apply_user_names(terms, manual)
            if added or changed:
                print(f"  手工译名：新增 {added} 条、改译 {changed} 条")

    # ---- 3. 逐集摘要（译名表按出场裁剪，并沿途补充新名字）----
    # 摘要缓存键要带上「校正后的译名表」：fix 把 Sanjo 改成 Shenzhou 之后，
    # 旧摘要里还写着「桑乔号」，不重算就会和新译名表打架。
    fix_key = _sig(json.dumps(fixes, ensure_ascii=False, sort_keys=True), names_raw)
    ep_gloss_key = _sig(g_key, fix_key)
    eps: list[dict] = []
    for e in done:
        if stopped():
            print(f"\n已取消（完成 {len(eps)}/{len(done)} 集摘要）")
            return 2
        print(f"\n摘要 · 第 {e['集']} 集")
        body = Path(e["txt"]).read_text(encoding="utf-8-sig")
        local = slice_terms(terms, body)
        print(f"    本集出场术语 {len(local)}/{len(terms)} 条")
        try:
            d = summarize_episode(cfg, Path(e["txt"]), ep=e["集"], key=e["key"],
                                  work=e["work"], terms=local, force=force,
                                  gloss_key=ep_gloss_key)
            d["文件"] = e["文件"]
            eps.append(d)
            new = d.get("新增术语") or []
            if new:
                before = len(terms)
                terms = add_terms(terms, new)
                if len(terms) > before:
                    print(f"    补充译名 {len(terms) - before} 条："
                          + "、".join(t["译名"] for t in terms[before:][:6]))
        except BaseException as ex:  # noqa: BLE001
            if isinstance(ex, KeyboardInterrupt):
                raise
            print(f"    ✗ {ex}")
            if e["文件"] in old_eps:
                print("    沿用上一次的记录")
                eps.append(old_eps[e["文件"]])

    if not eps:
        print("\n没有拿到任何一集的记录，无法汇总。")
        return 1

    # ---- 4. 译名表收尾：合并被拆开的人 ----
    if terms:
        before = len(terms)
        terms = normalize_glossary(cfg, terms)
        print(f"\n译名表收尾：{before} 条 → {len(terms)} 条")

    # ---- 5. 汇总 ----
    # fix_key 上面已经算过（含校正记录和手工表指纹），这里直接复用
    keys = _sig(sorted(str(e.get("_key", "")) for e in eps), _bible_sig(), fix_key)
    bible = state.get("总纲") or {}
    if force or not bible or state.get("_key") != keys or state.get("_gloss_key") != g_key:
        print(f"\n汇总总纲（{len(eps)} 集）…")
        bible = merge_bible(cfg, title, eps, terms)
    else:
        print("\n分集记录没变，复用已有总纲")

    md = render_md(bible, eps, terms, {"剧名": title, "每集分钟": per_ep_min,
                                       "模型": model, "校正": fixes})
    new_hash = _md5(md)
    edited = (md_file.exists() and old_md_hash
              and _md5(md_file.read_text(encoding="utf-8-sig")) != old_md_hash)
    if edited and not force:
        print("总纲.md 被人工改过，保留你的版本（要覆盖加 --force）")
    else:
        md_file.write_text(md, encoding="utf-8")
        old_md_hash = new_hash

    state = {
        "_key": keys,
        "_gloss_key": g_key,
        "剧名": title,
        "生成时间": time.strftime("%Y-%m-%d %H:%M"),
        "md_hash": old_md_hash,
        "采样": {"每集分钟": per_ep_min, "模型": model, "语言": lang},
        "译名表": terms,
        "校正": fixes,
        "分集": {e.get("文件", ""): e for e in eps},
        "总纲": bible,
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n✓ {md_file}")
    print(f"✓ {state_file}")
    print(f"  人物 {len(bible.get('人物') or [])} 个 · "
          f"名词 {len(bible.get('名词') or [])} 个 · "
          f"译名 {len(terms)} 条 · 主线 {len(str(bible.get('主线') or ''))} 字"
          f" · 共 {time.perf_counter() - t_all:.0f}s")
    print("  下一步：看一眼 总纲.md，改掉明显错的地方，再跑各集的解说。")
    return 0


# ---------------------------------------------------------------- 命令行

def cmd_series(args) -> int:
    src = Path(args.dir).expanduser()
    if not src.exists():
        print(f"目录不存在：{src}")
        return 2
    src_dir = src if src.is_dir() else src.parent
    out = Path(args.out).expanduser() if args.out else src_dir
    return build(src_dir, out, model=args.model, lang=args.lang,
                 per_ep_min=args.minutes, episodes=args.eps,
                 force=args.force, dry_run=args.dry_run, remix=args.remix,
                 names=getattr(args, "names", "") or "",
                 no_fix=bool(getattr(args, "no_fix", False)))
