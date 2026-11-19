"""djkit.score —— 逐段文案的本地打分（不调模型、不花钱、可解释）

为什么需要它：
    一次生成三版（悬念 / 人物处境 / 反常识），原来只按「字数接近目标」挑一版。
    但字数对得上不代表能用 —— 可能少写一段、可能全是 8 字短句（配音念出来像念标签）、
    可能两句完全重复、也可能混进「本期视频」这种出戏词。

设计原则：
    * 每个维度都输出 0~100 和一句人话理由，界面上直接显示，不做黑箱
    * 只在「同一批候选之间」比较，不做绝对好坏判断 —— 分数是相对的
    * 缺段是一票否决：少一段就得丢一段画面，别指望字数补回来
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# 提示词里明令禁止、但模型仍然会犯的词
OFF_WORDS = ("本期", "这期", "视频", "我们来看看", "接下来", "下面", "综上",
             "大家好", "欢迎收看", "小编", "点赞", "关注", "评论区")

# 单句长度的合理区间。提示词要求 10–18 字，这里放宽一点，
# 因为标点、数字、人名会把实际长度拉开。
LINE_MIN, LINE_MAX = 8, 24


@dataclass
class Part:
    """一个维度的得分"""

    name: str
    score: float          # 0~100
    weight: float
    detail: str = ""


@dataclass
class Result:
    score: float                    # 加权总分 0~100
    parts: list[Part] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)   # 扣分理由，按扣得多的在前

    @property
    def grade(self) -> str:
        """给界面用的短标签。「推荐」只留给真正选中的那一版，这里不带它"""
        if self.score >= 85:
            return "扎实"
        if self.score >= 70:
            return "可用"
        return "凑合"


def _chars(s: str) -> int:
    return len(re.sub(r"\s", "", s))


def _all_lines(data: list[dict]) -> list[str]:
    return [str(x).strip() for d in data for x in (d.get("lines") or []) if str(x).strip()]


def _coverage(data: list[dict], n_seg: int) -> Part:
    filled = sum(1 for d in data if any(str(x).strip() for x in (d.get("lines") or [])))
    n_seg = max(1, n_seg)
    ratio = min(1.0, filled / n_seg)
    # 缺段不是线性扣分：少一段那一段画面就废了，直接砍到 0.4 以下
    score = ratio * 100 if ratio >= 1 else ratio * 40
    miss = n_seg - filled
    detail = "段落齐全" if miss <= 0 else f"缺 {miss} 段（那几段画面会没词）"
    return Part("覆盖", score, 0.30, detail)


def _fit(data: list[dict], want: float) -> Part:
    chars = sum(_chars(x) for x in _all_lines(data))
    if want <= 0:
        return Part("字数", 80, 0.24, f"{chars} 字（没设目标）")
    diff = abs(chars - want) / want
    # 偏差 0 → 100；偏差 30% → 40；偏差 50% 以上 → 0
    score = max(0.0, 1.0 - diff / 0.5) * 100
    pct = (chars - want) / want * 100
    if abs(pct) < 8:
        detail = f"{chars} 字，正好（目标 {want:.0f}）"
    else:
        detail = f"{chars} 字，目标 {want:.0f}（{pct:+.0f}%）"
    return Part("字数", score, 0.24, detail)


def _line_len(data: list[dict]) -> Part:
    lines = _all_lines(data)
    if not lines:
        return Part("句长", 0, 0.14, "没有句子")
    good = sum(1 for x in lines if LINE_MIN <= _chars(x) <= LINE_MAX)
    ratio = good / len(lines)
    score = ratio * 100
    lens = [_chars(x) for x in lines]
    short = sum(1 for n in lens if n < LINE_MIN)
    long_ = sum(1 for n in lens if n > LINE_MAX)
    if short == 0 and long_ == 0:
        detail = f"{len(lines)} 句都在 {LINE_MIN}–{LINE_MAX} 字"
    else:
        bits = []
        if short:
            bits.append(f"{short} 句偏短")
        if long_:
            bits.append(f"{long_} 句偏长")
        detail = "、".join(bits) + f"（均值 {sum(lens) / len(lens):.0f} 字）"
    return Part("句长", score, 0.14, detail)


def _dup(data: list[dict]) -> Part:
    lines = [_chars(x) and x for x in _all_lines(data)]
    lines = [_norm(x) for x in lines if x]
    if len(lines) < 2:
        return Part("重复", 100, 0.14, "句数太少，不算重复")
    from collections import Counter

    cnt = Counter(lines)
    dup_n = sum(v - 1 for v in cnt.values() if v > 1)
    ratio = dup_n / len(lines)
    # 重复一句扣一点，但重复到 30% 就已经不能用了
    score = max(0.0, 1.0 - ratio / 0.3) * 100
    if dup_n == 0:
        detail = "没有重复句"
    else:
        samples = [k for k, v in cnt.items() if v > 1][:2]
        detail = f"{dup_n} 句重复（如「{samples[0][:12]}」）"
    return Part("重复", score, 0.14, detail)


def _norm(s: str) -> str:
    """去标点再比，避免「他走了。」和「他走了！」被当成两句"""
    return re.sub(r"[，。！？、；：,.!?;:\s\"'“”‘’]", "", s)


def _off_words(data: list[dict]) -> Part:
    lines = _all_lines(data)
    if not lines:
        return Part("出戏词", 0, 0.06, "没有句子")
    hits = [w for w in OFF_WORDS if any(w in x for x in lines)]
    score = max(0.0, 1.0 - len(hits) / 3) * 100
    detail = "干净" if not hits else "出现「" + "、".join(hits[:3]) + "」"
    return Part("出戏词", score, 0.06, detail)


# 中文字、中文标点、数字都算「念得出来」；英文单词、括号、符号要扣
_SPEAK_OK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f0-9]")
_SPEAK_BAD = re.compile(r"[A-Za-z]{3,}|[\[\]{}<>*_#`|\\]")


def _speakable(data: list[dict]) -> Part:
    """配音能不能顺口念出来。英文长词和 markdown 残渣都会被念成怪音。"""
    lines = _all_lines(data)
    if not lines:
        return Part("可念", 0, 0.12, "没有句子")
    bad = sum(1 for x in lines if _SPEAK_BAD.search(x))
    ratio = bad / len(lines)
    score = max(0.0, 1.0 - ratio / 0.25) * 100
    if bad == 0:
        detail = "全是中文口语句"
    else:
        detail = f"{bad} 句带英文/符号，配音会念怪"
    return Part("可念", score, 0.12, detail)


def score_script(data: list[dict], *, want_chars: float = 0.0,
                 n_seg: int = 0) -> Result:
    """给一版逐段文案打分。data 形如 [{i, lines}]，want_chars 是目标字数。"""
    n_seg = n_seg or len(data)
    parts = [
        _coverage(data, n_seg),
        _fit(data, want_chars),
        _line_len(data),
        _dup(data),
        _off_words(data),
        _speakable(data),
    ]
    total_w = sum(p.weight for p in parts) or 1.0
    score = sum(p.score * p.weight for p in parts) / total_w

    # 扣分理由：低于 75 分的维度才说，按「扣的分 × 权重」排序
    losers = [p for p in parts if p.score < 75]
    losers.sort(key=lambda p: (100 - p.score) * p.weight, reverse=True)
    reasons = [f"{p.name}：{p.detail}" for p in losers]
    return Result(round(score, 1), parts, reasons)


def rank(versions: list[dict], *, want_chars: float, n_seg: int) -> str:
    """给一版列表打分并写回 rank/score/reasons，返回推荐的那一版 key。

    versions 每项至少要有 {key, seg_scripts}。就地修改，返回被选中项的 key。
    """
    if not versions:
        return ""
    for v in versions:
        r = score_script(v.get("seg_scripts") or [], want_chars=want_chars, n_seg=n_seg)
        v["score"] = r.score
        v["grade"] = r.grade
        v["score_parts"] = [{"name": p.name, "score": round(p.score),
                             "detail": p.detail} for p in r.parts]
        v["reasons"] = r.reasons
    best = max(versions, key=lambda v: (v.get("score") or 0,
                                        v.get("full") or 0))
    for v in versions:
        v["best"] = v is best
    return best["key"]
