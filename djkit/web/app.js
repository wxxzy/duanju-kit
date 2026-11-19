const $ = (id) => document.getElementById(id);
let S = null;
let step = 1;
let localScript = "";
let maxStep = 1;
let segKey = "";

// 客户端版没有控制台。前端一旦报错，用户只会看到「白屏 / 点了没反应」，
// 而 app.log 里干干净净。所以把错误甩回后端，让日志能查到。
let errSent = 0;
function reportErr(where, msg) {
  if (errSent++ > 8) return;      // 别让一个死循环把日志刷爆
  try {
    fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: where + "：" + msg }),
    });
  } catch (e) { /* 后端也挂了，那就真没办法了 */ }
}

window.addEventListener("error", (e) => {
  reportErr("脚本错误", `${e.message || "?"}（${e.filename || "?"}:${e.lineno || 0}）`);
});
window.addEventListener("unhandledrejection", (e) => {
  reportErr("未处理的 Promise", String((e.reason && e.reason.message) || e.reason || "?"));
});

const ACTIONS = {
  1: { label: "分析片段", fn: analyze },
  2: { label: "生成文案", fn: makeScript },
  3: { label: "配音并出片", fn: render },
  4: { label: "再做一集", fn: reset },
};

function showError(msg) {
  $("status").textContent = "✗ " + msg;
  $("status").style.color = "var(--err)";
  if (S) S.error = msg;
}

async function post(url, body) {
  let r, data = {};
  try {
    r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    data = await r.json();
  } catch (e) {
    showError("服务没响应，确认 gui 还在跑");
    return { error: "network" };
  }
  if (!r.ok || data.error) {
    showError(data.error || ("请求失败 " + r.status));
    return { error: data.error || r.status };
  }
  return data;
}

let bibleReady = false;

// ---------------------------------------------------------------- 客户端模式
// 只有 pywebview 外壳起来时，window.pywebview.api 才有东西。
// 浏览器版一切照旧：路径只能手粘，其它功能一样。
function inDesktop() {
  // 只看桥有没有起来，不认具体方法名：认方法名会让「少一个接口」直接退化成浏览器模式
  return !!(window.pywebview && window.pywebview.api);
}

async function pickVideo() {
  if (!inDesktop()) return;
  if (typeof window.pywebview.api.pick_video !== "function") {
    return showError("这个壳没提供文件选择，直接粘路径");
  }
  let p = "";
  try {
    p = await window.pywebview.api.pick_video();
  } catch (e) {
    return showError("打开文件选择框失败：" + e);
  }
  if (!p) return;
  $("src").value = p;
  $("src").dispatchEvent(new Event("input"));   // 触发路径即时校验
}

window.addEventListener("pywebviewready", () => {
  document.body.classList.add("desktop");
  $("pick").style.display = "";
  if (typeof window.pywebview.api.pick_folder === "function") {
    $("batch-pick").style.display = "";
  }
  if (typeof window.pywebview.api.pick_audio === "function") {
    $("bgm-pick").style.display = "";
  }
  // 告一声，Python 侧日志能看到「桥接就绪」，出问题时好判断是壳还是页面挂了
  try { window.pywebview.api.ping(); } catch (e) { /* 忽略 */ }
});

// 拖文件进窗口时别让 WebView2 直接导航走（会白屏且回不来）
["dragover", "drop"].forEach((t) =>
  window.addEventListener(t, (e) => { e.preventDefault(); e.stopPropagation(); }));

setTimeout(() => { if (!inDesktop()) $("pick").style.display = "none"; }, 1500);

async function refreshBible() {
  let d = {};
  try {
    d = await (await fetch("/api/bible")).json();
  } catch (e) { return; }
  const st = $("bible-state"), hint = $("bible-hint");
  if (!st) return;
  if (!d.exists) {
    st.textContent = "未生成";
    st.style.color = "var(--muted)";
    $("bible-remix").style.display = "none";
    return;
  }
  st.textContent = `✓ ${d.people} 人 · ${d.nouns} 名词 · ${d.terms} 译名 · 主线 ${d.main} 字`;
  st.style.color = "var(--ok)";
  hint.textContent = `${d.when || ""} 生成，之后每一集会自带这份设定`;
  $("bible-remix").style.display = "";
}

async function refreshCache() {
  let d = {};
  try {
    d = await (await fetch("/api/cache")).json();
  } catch (e) { return; }
  const st = $("cache-state");
  if (!st) return;
  st.textContent = `${d.total_mb} MB`;
  st.style.color = d.total_mb > 800 ? "var(--err)" : "var(--ok)";
  const parts = Object.values(d.parts || {});
  $("cache-hint").textContent =
    parts.map((v) => `${v.label.split("（")[0]} ${v.mb}MB`).join(" · ") +
    "。删了只是下次分析要重算，成片、整季、总纲都不会动。";
  $("cache-free").textContent = d.free_mb
    ? `磁盘剩余 ${(d.free_mb / 1024).toFixed(1)} GB` : "";
}

let clearArm = 0;
$("cache-clear").onclick = async () => {
  // 不用 confirm()：WebView2 外壳里 JS 对话框可能被拦，点一下就卡住
  if (Date.now() - clearArm > 4000) {
    clearArm = Date.now();
    $("cache-clear").textContent = "再点一次确认";
    $("cache-clear").classList.add("danger");
    setTimeout(() => {
      if (Date.now() - clearArm >= 3900) {
        clearArm = 0;
        $("cache-clear").textContent = "清理缓存";
        $("cache-clear").classList.remove("danger");
      }
    }, 4000);
    return;
  }
  clearArm = 0;
  $("cache-clear").textContent = "清理中…";
  $("cache-clear").classList.remove("danger");
  const d = await post("/api/cache", { what: "all" });
  $("cache-clear").textContent = "清理缓存";
  if (!d.error) {
    await refreshCache();
    $("cache-state").textContent = `已释放 ${d.freed_mb} MB`;
  }
};

async function pickFolder() {
  if (!inDesktop() || typeof window.pywebview.api.pick_folder !== "function") {
    return showError("这个壳没有目录选择，手动粘贴路径");
  }
  const p = await window.pywebview.api.pick_folder();
  if (p) $("batch-dir").value = p;
}

async function pickBgm() {
  if (!inDesktop() || typeof window.pywebview.api.pick_audio !== "function") {
    return showError("这个壳没有音频选择，手动粘贴路径");
  }
  let p = "";
  try {
    p = await window.pywebview.api.pick_audio();
  } catch (e) {
    return showError("打开音频选择框失败：" + e);
  }
  if (p) $("bgm").value = p;
}

function pickedSpecs() {
  const v = [...document.querySelectorAll('#specs input[name="spec"]:checked')]
    .map((i) => i.value);
  return v.length ? v : ["1080x1920"];
}

async function runBatch() {
  let dir = ($("batch-dir").value || "").trim();
  if (!dir && S && S.src) dir = S.src.replace(/[\\/][^\\/]+$/, "");
  if (!dir) return showError("先填剧集目录，或先选一个视频");
  const d = await post("/api/batch", {
    dir,
    top: +$("top").value || 4,
    len: +$("len").value || 12,
    window: +$("window").value || 150,
    seconds: +$("seconds").value || 40,
    model: $("model").value,
    lang: $("lang").value,
    voice: $("voice").value,
    rate: $("rate").value,
    src_vol: +$("srcvol").value || 0.12,
    specs: pickedSpecs(),
    title: $("title").value,
    background: $("bg").value,
    burn_sub: $("burn-sub").checked,
    sub_style: $("sub-style").value,
    bgm: ($("bgm").value || "").trim(),
    bgm_vol: +$("bgm-vol").value,
    duck: $("duck").checked,
  });
  if (!d.error) {
    $("status").textContent = "批量开始，进度看右侧日志…";
    $("status").style.color = "var(--muted)";
  }
}

async function runSeries(remix) {
  const path = $("src").value.trim();
  if (!path) return showError("先填视频路径（总纲按它所在的整季目录跑）");
  const d = await post("/api/series", {
    path,
    minutes: +$("bible-min").value || 7,
    eps: +$("bible-eps").value || 0,
    model: $("model").value,
    lang: $("lang").value,
    names: ($("bible-names").value || "").trim(),
    remix: !!remix,
  });
  if (!d.error) {
    $("status").textContent = remix ? "重汇总中…" : "建总纲中，进度见右侧日志…";
    $("status").style.color = "var(--muted)";
  }
}

function setStep(n) {
  step = n;
  if (n > maxStep) maxStep = n;
  document.querySelectorAll(".pane").forEach((p, i) => p.classList.toggle("active", i === n - 1));
  document.querySelectorAll("#steps button").forEach((b, i) => {
    b.classList.toggle("active", i === n - 1);
    b.classList.toggle("done", i < n - 1);
    b.disabled = i + 1 > maxStep;
  });
  renderAside();
}

function fmt(sec) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

function selected() {
  return (S?.segments || []).filter((s) => s.selected);
}

function actionEnabled() {
  if (!S || S.busy) return false;
  if (step === 1) return !!$("src").value.trim();
  if (step === 2) return selected().length > 0;
  if (step === 3) return collectSegScript().some((d) => d.lines.length > 0);
  return true;
}

function analyze() {
  post("/api/analyze", {
    path: $("src").value.trim(),
    top: +$("top").value,
    len: +$("len").value,
    window: +$("window").value,
    model: $("model").value,
    lang: $("lang").value,
  });
}

function makeScript() {
  localScript = "";
  post("/api/script", {
    variants: 3,
    seconds: +$("seconds").value,
    background: $("bg").value,
    style: "克制的叙述，不煽情，偶尔一句冷判断",
  });
}

function render() {
  localScript = "";
  // 后端也会拦，但在这里拦住能说清是哪一段没写，不用等一轮往返
  const data = collectSegScript();
  const empty = data.filter((d) => !d.lines.length).map((d) => d.i);
  if (empty.length) {
    return showError(`第 ${empty.join("、")} 段还是空的，补齐再出片（缺段会少一段画面）`);
  }
  const sel = selected().length;
  if (data.length !== sel) {
    return showError(`文案 ${data.length} 段，画面 ${sel} 段，数量对不上`);
  }
  post("/api/render", {
    voice: $("voice").value,
    rate: $("rate").value,
    src_vol: +$("srcvol").value,
    specs: pickedSpecs(),
    title: $("title").value,
    seg_scripts: data,
    burn_sub: $("burn-sub").checked,
    sub_style: $("sub-style").value,
    bgm: ($("bgm").value || "").trim(),
    bgm_vol: +$("bgm-vol").value,
    duck: $("duck").checked,
  });
}

function reset() {
  // 走按钮上那套完整清理，别维护两份
  $("reset").onclick();
}

function renderSegs() {
  const box = $("segs");
  const segs = S?.segments || [];
  const key = segs.map((s) => s.i + ":" + s.tc + ":" + s.len).join("|");

  if (key !== segKey) {
    segKey = key;
    selLocal = {};        // 换了一批片段，旧的勾选记录作废
    selDirty = 0;
    box.innerHTML = "";
    segs.forEach((s, idx) => {
      const el = document.createElement("div");
      el.className = "card";
      el.innerHTML =
        `<div class="tick">✓</div>` +
        `<img loading="lazy" alt="">` +
        `<div class="bar"><i></i></div>` +
        `<div class="meta"><span class="tc"></span><span class="sc"></span></div>`;
      el.onclick = () => {
        const cur = S.segments[idx];
        if (!cur) return;
        cur.selected = !cur.selected;
        selLocal[idx] = cur.selected;
        selDirty++;
        el.classList.toggle("on", cur.selected);
        renderAside();
        syncSegments();
      };
      box.appendChild(el);
    });
  }

  [...box.children].forEach((el, i) => {
    const s = segs[i];
    if (!s) return;
    el.classList.toggle("on", !!s.selected);
    const img = el.querySelector("img");
    if (img.getAttribute("src") !== s.thumb) img.src = s.thumb;
    el.querySelector(".tc").textContent = s.tc;
    el.querySelector(".sc").textContent = "分 " + (s.score || 0).toFixed(2);
    el.querySelector(".bar i").style.width = Math.round((s.score || 0) * 100) + "%";
  });
}

let syncTimer = null;
let syncSeq = 0;
// 点击后的勾选状态先记在本地，等服务端确认再交回去。
// 不这么做的话，轮询每 0.8 秒拉一次 state，而保存有 200ms 防抖，
// 中间任何一次轮询都会用旧数据把勾选翻回去——看起来就是「取消不掉」。
let selLocal = {};
let selDirty = 0;

function syncSegments() {
  const mySeq = ++syncSeq;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(async () => {
    const r = await post("/api/segments", { segments: S.segments });
    if (mySeq === syncSeq && !r.error) {
      selLocal = {};
      selDirty = 0;
    }
  }, 200);
}

function collectSegScript() {
  return [...document.querySelectorAll("#seg-editor .seg-block")].map((b, i) => ({
    i: i + 1,
    lines: b.querySelector("textarea").value
      .split("\n").map((x) => x.trim()).filter(Boolean),
  }));
}

// ---------------------------------------------------------------- 三版文案
// 后端一次写三版（悬念/人物/反常识），这里只负责「显示哪一版」。
// 用户改过的内容记在 varEdits 里：切走切回来还在，且不会污染另外两版。
let varEdits = {};
let varSig = "";
let outSig = "";

function variantOf(key) {
  return (S?.variants || []).find((v) => v.key === key) || null;
}

function currentLines() {
  const v = variantOf(S?.variant);
  if (v) return varEdits[v.key] || v.seg_scripts;
  return S?.seg_scripts || [];
}

function renderVarTabs() {
  const vs = S?.variants || [];
  const box = $("var-tabs");
  if (!box) return;
  const cmp = $("var-compare");
  if (vs.length < 2) {
    box.style.display = "none";   // 只有一版就别摆一排按钮占地方
    if (cmp) cmp.style.display = "none";
    const sc = $("var-score");
    if (sc) sc.style.display = "none";
    return;
  }
  if (cmp) {
    cmp.style.display = "";
    cmp.textContent = compareOn ? "收起并排" : "并排看";
  }
  // 只关心「哪几版被改过」，不关心改了多少——否则每敲一个字都重建按钮
  const editSig = Object.keys(varEdits).sort().join(",");
  const sig = vs.map((v) => v.key + v.chars + (v.score || "")).join("|") +
    "#" + S.variant + "#" + editSig;
  if (sig === varSig) return;
  varSig = sig;

  box.style.display = "flex";
  box.innerHTML = "";
  const want = (+$("seconds").value || 45) * 4.1;
  const need = selected().length;
  vs.forEach((v) => {
    const b = document.createElement("button");
    const on = v.key === S.variant;
    const off = Math.round(((v.chars - want) / want) * 100);
    const short = need && (v.full || 0) < need;
    b.className = "vtab" + (on ? " active" : "") + (short ? " short" : "") +
      (v.best ? " best" : "");
    // 分数是相对的，得带一句理由，否则「为什么这版好」说不清
    const why = (v.reasons || [])[0] || "";
    b.innerHTML =
      `<b>${v.key}</b><span>${v.name}</span>` +
      `<em>${v.chars} 字${on ? "，" + (off >= 0 ? "+" : "") + off + "%" : ""}</em>` +
      (v.score != null ? `<u class="sc" title="${esc(why || "各维度都合格")}">${Math.round(v.score)}</u>` : "") +
      (short ? `<u title="有段落没写完">缺段</u>` : "") +
      (varEdits[v.key] ? `<i title="改过">*</i>` : "");
    b.onclick = () => switchVariant(v.key);
    box.appendChild(b);
  });
  renderScoreTable();
}

// ---------------------------------------------------------------- 评分明细
// 不做黑箱：每个维度、得分、同一句话的理由都摊开，用户能自己判断要不要换版
let scoreSig = "";

function renderScoreTable() {
  const box = $("var-score");
  if (!box) return;
  const vs = S?.variants || [];
  if (vs.length < 2 || !vs[0].score_parts) {
    box.style.display = "none";
    return;
  }
  const sig = vs.map((v) => v.key + (v.score || 0)).join("|") + "@" + S.variant;
  if (sig === scoreSig) return;
  scoreSig = sig;
  box.style.display = "";
  box.innerHTML = "";

  vs.forEach((v) => {
    const on = v.key === S.variant;
    const col = document.createElement("div");
    col.className = "score-col" + (on ? " on" : "") + (v.best ? " best" : "");
    const rows = (v.score_parts || []).map((p) => {
      const lv = p.score >= 85 ? "ok" : p.score >= 60 ? "mid" : "bad";
      return `<div class="score-row"><span>${p.name}</span>` +
        `<i class="${lv}" style="width:${Math.max(3, p.score)}%"></i>` +
        `<em title="${esc(p.detail)}">${Math.round(p.score)}</em></div>`;
    }).join("");
    col.innerHTML =
      `<div class="score-h"><b>${v.key}</b>` +
      `<span>${Math.round(v.score || 0)} 分</span>` +
      (v.best ? `<u>推荐</u>` : `<u class="mu">${v.grade || ""}</u>`) + `</div>` +
      rows +
      `<div class="score-why">${esc((v.reasons || []).join("；") || "各维度都合格")}</div>`;
    col.onclick = () => switchVariant(v.key);
    box.appendChild(col);
  });
}

async function switchVariant(key) {
  if (!S || key === S.variant) return;
  // 先把当前这版改过的内容收下来，否则切一次就丢了
  if (S.variant) varEdits[S.variant] = collectSegScript();
  const d = await post("/api/variant", { key });
  if (d.error) return;
  S.variant = key;
  S.seg_scripts = variantOf(key)?.seg_scripts || S.seg_scripts;
  segScriptKey = "";        // 强制重绘编辑器
  renderSegScripts();
  renderVarTabs();
  syncHint();
}
let segScriptKey = "";

// ---------------------------------------------------------------- 并排看
// 三版的开头差别最大，光看字数选不出来。这里给一个只读的三列视图，
// 点哪列就切到那一版，不做第二套编辑逻辑。
let compareOn = false;
let cmpSig = "";

function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function renderCompare() {
  const box = $("vcompare");
  if (!box) return;
  const vs = S?.variants || [];
  if (!compareOn || vs.length < 2) {
    box.style.display = "none";
    return;
  }
  const sig = vs.map((v) => v.key + "#" +
    (varEdits[v.key] || v.seg_scripts).map((d) => d.lines.join("").length).join(",")
  ).join("|") + "@" + (S?.variant || "");
  if (sig === cmpSig) return;
  cmpSig = sig;

  box.style.display = "grid";
  box.innerHTML = "";
  vs.forEach((v) => {
    const lines = (varEdits[v.key] || v.seg_scripts)
      .map((d) => (d.lines.length ? d.lines.join("\n") : "（这一段空着）"))
      .join("\n\n");
    const col = document.createElement("div");
    col.className = "vcol" + (v.key === S.variant ? " on" : "");
    col.innerHTML =
      `<div class="vcol-h"><b>${v.key}</b><span>${esc(v.name)}</span>` +
      `<em>${v.chars} 字</em></div><div class="vcol-b">${esc(lines)}</div>`;
    col.onclick = () => switchVariant(v.key);
    box.appendChild(col);
  });
}

function renderSegScripts() {
  // 判重只能用「服务端版本」，不能用 varEdits：后者跟着键盘在变，
  // 每 0.8 秒的轮询就会重绘 textarea，把光标和正在输入的字打回去。
  const v = variantOf(S?.variant);
  const base = v ? v.seg_scripts : (S?.seg_scripts || []);
  const key = (S?.variant || "") + ":" +
    base.map((d) => d.i + ":" + d.lines.join("").length).join(";");

  if (key !== segScriptKey && base.length) {
    segScriptKey = key;
    const data = currentLines();     // 本地改过的优先
    const box = $("seg-editor");
    box.innerHTML = "";
    data.forEach((d, i) => {
      const el = document.createElement("div");
      el.className = "seg-block";
      el.innerHTML =
        `<div class="seg-head"><b>第 ${i + 1} 段</b><span></span></div>` +
        `<textarea rows="2" spellcheck="false"></textarea>`;
      const ta = el.querySelector("textarea");
      ta.value = d.lines.join("\n");
      ta.oninput = () => {
        if (S?.variant) varEdits[S.variant] = collectSegScript();
        syncHint();
      };
      box.appendChild(el);
    });
    syncHint();
  }

  [...document.querySelectorAll("#seg-editor .seg-block")].forEach((b) => {
    const n = b.querySelector("textarea").value.replace(/\s/g, "").length;
    b.querySelector(".seg-head span").textContent =
      `${n} 字 · 约 ${(n / 4.1).toFixed(1)}s`;
  });
}

function syncHint() {
  const all = collectSegScript();
  const n = all.reduce((a, d) => a + d.lines.join("").replace(/\s/g, "").length, 0);
  const target = +$("seconds").value || 45;
  const est = n / 4.1;
  const empty = all.filter((d) => !d.lines.length).length;
  const need = selected().length;
  let msg = `${all.length}/${need} 段 · ${n} 字 · 预计 ${est.toFixed(0)} 秒 · 目标 ${target} 秒`;
  if (empty) msg += ` · 有 ${empty} 段空着，出片前必须补上`;
  else if (Math.abs(est - target) > 8) msg += "（偏差较大，可增删句子）";
  $("script-hint").textContent = msg;
  renderAside();
}

function fileBase(p) {
  return String(p || "").split(/[\\/]/).pop();
}

function renderOutputs() {
  const box = $("outputs");
  if (!box) return;
  const list = S?.outputs || [];
  const sig = list.map((o) => o.path).join("|") + "#" + (S?.output || "");
  if (sig === outSig) return;
  outSig = sig;
  box.innerHTML = "";
  if (list.length < 2) return;     // 只出一版就不用列表
  list.forEach((o) => {
    const el = document.createElement("div");
    el.className = "out-item" + (o.path === S.output ? " main" : "");
    el.innerHTML = `<b>${o.spec}</b><span>${o.size_mb} MB</span>` +
                   `<em>${fileBase(o.path)}</em>`;
    el.onclick = () => playOutput(o.path);
    box.appendChild(el);
  });
}

function playOutput(p) {
  S.output = p;
  outSig = "";
  const v = $("video-wrap").querySelector("video");
  if (v) { v.src = "/video?p=" + encodeURIComponent(p); v.play().catch(() => {}); }
  $("out-hint").textContent = p;
  renderOutputs();
}

// ---------------------------------------------------------------- 整季汇总
// 整季跑完，成片散在目录里没法一眼看全。这里的目的是「一屏判断这季能不能发」：
// 出齐了没、哪集没音轨、哪集没封面、总体积多大。
let seasonSig = "";
let seasonData = null;
let seasonSpec = "";
let seasonChecked = 0;      // 启动后探过一次没？没跑过批量也要能看见历史成片

async function refreshSeason(force) {
  const box = $("season");
  if (!box) return;
  // 汇总会扫盘，别每 0.8 秒都问。只在三种时候拉：
  // 刚跑完批量、点进成片页、启动后探一次（上次跑完就退出的话 batch 是空的）
  if (!force && !shouldShowSeason() && seasonChecked) return;
  seasonChecked = 1;
  let d = {};
  try {
    d = await (await fetch("/api/season?spec=" + encodeURIComponent(seasonSpec))).json();
  } catch (e) { return; }
  seasonData = d;
  renderSeason();
}

function shouldShowSeason() {
  const b = S?.batch || {};
  return !!(b.count || (S && S.stage === "batch"));
}

function renderSeason() {
  const box = $("season");
  if (!box || !seasonData) return;
  const d = seasonData;
  if (!d.exists || !d.count) {
    box.style.display = "none";
    return;
  }
  box.style.display = "";
  const sig = d.count + "@" + d.spec + "@" + d.total_mb + "@" +
    (d.episodes || []).map((e) => e.name + e.size_mb).join(",");
  if (sig === seasonSig) return;
  seasonSig = sig;

  $("season-state").textContent = `✓ ${d.count} 集 · ${d.total_txt} · ${d.total_mb} MB`;
  $("season-state").style.color = "var(--ok)";

  // 画幅切换：整季通常一次出多档，只想看其中一档
  const sp = $("season-specs");
  sp.innerHTML = "";
  (d.specs || []).forEach((s) => {
    const b = document.createElement("button");
    b.className = "chip" + (s === d.spec ? " on" : "");
    b.textContent = s;
    b.onclick = () => {
      seasonSpec = s;
      seasonSig = "";
      refreshSeason(true);
    };
    sp.appendChild(b);
  });

  $("season-ledger").style.display = d.ledger ? "" : "none";
  $("season-ledger").onclick = () => post("/api/open", { path: d.ledger_path });

  // 自己从每一集数一遍，不只信后端给的 problems —— 界面显示什么，就该以眼前这张表为准
  const eps = d.episodes || [];
  const noAud = eps.filter((e) => !e.audio).length;
  const noCov = eps.filter((e) => !e.cover).length;
  const bad = [];
  if (noAud) bad.push(`${noAud} 集没音轨`);
  if (noCov) bad.push(`${noCov} 集没封面`);
  (d.problems || []).forEach((p) => { if (!bad.some((b) => b.includes(p.slice(0, 4)))) bad.push(p); });
  $("season-sum").innerHTML = bad.length
    ? `<span class="bad">⚠ ${esc(bad.join("；"))}</span>`
    : `<span class="ok">✓ ${eps.length} 集都有音轨和封面，可以直接发</span>`;

  const grid = $("season-grid");
  grid.innerHTML = "";
  (d.episodes || []).forEach((e) => {
    const el = document.createElement("div");
    el.className = "ep-card";
    const ep = e.ep != null ? "EP" + String(e.ep).padStart(2, "0") : "—";
    // 拼一串「·」分隔的副信息，空项不占位，免得行首多个点
    const bits = [];
    if (e.chars) bits.push(e.chars + " 字文案");
    if (!e.audio) bits.push('<i class="bad">无音轨</i>');
    if (e.w && e.h) bits.push(`${e.w}×${e.h}`);
    el.innerHTML =
      `<div class="ep-thumb">` +
      (e.cover && e.cover_path
        ? `<img loading="lazy" alt="" src="/cover?p=${encodeURIComponent(e.cover_path)}">`
        : `<span class="noimg">无封面</span>`) +
      `<u>${e.dur_txt}</u></div>` +
      `<div class="ep-meta"><b>${ep}</b><span>${e.size_mb} MB</span></div>` +
      `<div class="ep-sub">${bits.join(" · ")}</div>`;
    el.onclick = () => {
      // 点一集就在上面的播放器里放它，不跳页
      playOutput(e.path);
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
    grid.appendChild(el);
  });
}

function renderAside() {
  if (!S) return;
  $("k-src").textContent = S.src ? S.src.split(/[\\/]/).pop().slice(0, 18) + "…" : "—";
  $("k-dur").textContent = S.duration ? fmt(S.duration) : "—";
  $("k-title").textContent = S.title || "—";
  const sel = selected();
  $("k-sel").textContent = sel.length + " 段";
  const total = sel.reduce((a, b) => a + b.len, 0);
  $("k-total").textContent = fmt(total);
  $("k-target").textContent = $("seconds").value + "s";
  const hint = $("seg-hint");
  if (hint) {
    hint.textContent = sel.length
      ? `已选 ${sel.length} 段，画面 ${fmt(total)}；成片长度会自动贴合配音（配音短则裁尾，长则延长末段）`
      : "点缩略图选中或取消。建议 5–8 段，太少撑不住信息量，太多节奏会散。";
  }

  const btn = $("action");
  btn.textContent = S.busy ? "处理中…" : ACTIONS[step].label;
  btn.disabled = !actionEnabled() || !!S.busy;

  // 忙的时候：露出「取消」，禁掉「重置」（重置会强制把 busy 置 False，
  // 后端已经改成 409，这里提前拦住，别让用户对着报错点）
  $("cancel").style.display = S.busy ? "" : "none";
  $("reset").disabled = !!S.busy;

  $("pct").style.width = (S.pct || 0) + "%";
  $("status").textContent = S.error ? "✗ " + S.error : (S.msg || "就绪");
  $("status").style.color = S.error ? "var(--err)" : "var(--muted)";
  $("log").textContent = (S.log || []).slice(-18).join("\n");
  $("log").scrollTop = $("log").scrollHeight;

  // 总纲跑完（busy 由 true 变 false 且没出错）刷一次状态
  if (!S.busy && !S.error && S.stage === "idle" && !bibleReady) {
    bibleReady = true;
    refreshBible();
  }
  if (S.busy && S.stage === "series") {
    $("bible-run").disabled = true;
    $("bible-remix").disabled = true;
  } else {
    $("bible-run").disabled = false;
    $("bible-remix").disabled = false;
  }
  // 批量状态行
  const bs = $("batch-state");
  if (bs) {
    const b = S.batch || {};
    if (S.stage === "batch") {
      bs.textContent = `进行中 ${S.pct || 0}%`;
      bs.style.color = "var(--accent, #6aa9ff)";
    } else if (b.count) {
      bs.textContent = `✓ ${b.count} 个成片 · ${b.dir ? b.dir.split(/[\\/]/).pop() : ""}`;
      bs.style.color = "var(--ok)";
    } else if (!S.busy) {
      bs.textContent = "未开始";
      bs.style.color = "var(--muted)";
    }
  }
  const br = $("batch-run");
  if (br) br.disabled = !!S.busy;

  const rs = $("reshuffle");
  if (rs) rs.disabled = !!S.busy || !(S.segments || []).length;

  if (S.cover) {
    const row = $("cover-row");
    if (row.style.display === "none") row.style.display = "flex";
    if ($("cover").getAttribute("src") !== "/cover?" + S.cover) {
      $("cover").src = "/cover?" + encodeURIComponent(S.cover);
    }
  } else if ($("cover-row")) {
    $("cover-row").style.display = "none";
  }

  if (S.output && !$("video-wrap").innerHTML) {
    $("video-wrap").innerHTML = `<video src="/video" controls playsinline></video>`;
    $("out-hint").textContent = S.output;
    $("open").style.display = "block";
  }

  renderVarTabs();
  renderCompare();
  renderOutputs();

  // 批量刚跑完那一下拉一次汇总；平时不拉，免得每 0.8 秒扫一遍盘
  if (!S.busy && shouldShowSeason()) refreshSeason(false);
  // 第一次进成片页也拉一次：上次跑完就关窗的话，S.batch 是空的但片子还在
  if (step === 4 && !seasonChecked) refreshSeason(true);
}

let lastStage = "";
let lastBusy = false;

async function tick() {
  try {
    const r = await fetch("/api/state");
    const data = await r.json();
    // 本地还没确认的勾选，压住服务端返回的旧值
    if (selDirty && Array.isArray(data.segments)) {
      data.segments.forEach((s, i) => {
        if (i in selLocal) s.selected = selLocal[i];
      });
    }
    S = data;
    renderSegs();
    renderAside();

    // 只在「阶段变化」时跳步。原来写的是「stage 是 segments 且在第 1 步就跳」，
    // 而 stage 会一直是 segments，结果一回素材页就被弹回片段页，
    // 想改参数重新生成片段根本回不去。
    if (S.stage !== lastStage) {
      if (S.stage === "segments" && step === 1) setStep(2);
      if (S.stage === "scripts") {
        if (step <= 2) setStep(3);
        renderSegScripts();
      }
      if (S.stage === "done" && step <= 3) setStep(4);
      lastStage = S.stage;
    }
    if (S.stage === "scripts") renderSegScripts();
    // 任务跑完刷一下磁盘占用：缓存就是这时候涨起来的
    if (lastBusy && !S.busy) { refreshCache(); refreshSeason(true); }
    lastBusy = !!S.busy;
  } catch (e) {
    // 服务重启时会静默重试，但别一直哑着——同一个错误只报一次
    reportErr("轮询状态失败", String((e && e.message) || e));
    await new Promise((r) => setTimeout(r, 1500));
  }
  setTimeout(tick, 800);
}

$("action").onclick = () => ACTIONS[step].fn();
$("open").onclick = () => post("/api/open", { path: S?.output ? S.output.replace(/[^\\/]+$/, "") : "" });
let chkTimer = null;
$("src").addEventListener("input", () => {
  clearTimeout(chkTimer);
  const v = $("src").value.trim();
  const h = $("src-hint");
  if (!v) { h.textContent = ""; return; }
  h.textContent = "检查中…";
  h.style.color = "var(--muted)";
  chkTimer = setTimeout(async () => {
    try {
      const d = await (await fetch("/api/check?path=" + encodeURIComponent(v))).json();
      if (d.ok) {
        h.textContent = `✓ ${fmt(d.duration)} · ${d.width}×${d.height} · ${d.size_mb}MB`;
        h.style.color = "var(--ok)";
      } else {
        h.textContent = "✗ " + d.error;
        h.style.color = "var(--err)";
      }
    } catch (e) { h.textContent = ""; }
  }, 450);
});

// 换一批：把当前这些起点传给后端，让它避开这些位置重新挑
async function reshuffle() {
  if (!S || !S.segments || !S.segments.length) return;
  const avoid = S.segments.map((s) => s.start);
  const d = await post("/api/analyze", {
    path: S.src,
    top: +$("top").value || 4,
    len: +$("len").value || 12,
    window: +$("window").value || 150,
    model: $("model").value,
    lang: $("lang").value,
    avoid,
  });
  if (!d.error) {
    selLocal = {};
    selDirty = 0;
    $("status").textContent = "换一批中，进度见右侧日志…";
    $("status").style.color = "var(--muted)";
  }
}

$("cancel").onclick = async () => {
  const d = await post("/api/cancel", {});
  if (!d.error) {
    $("status").textContent = "正在取消，等当前步骤结束…";
    $("status").style.color = "var(--muted)";
  }
};

$("reshuffle").onclick = reshuffle;
$("pick").onclick = pickVideo;
$("var-compare").onclick = () => {
  compareOn = !compareOn;
  cmpSig = "";           // 强制重画
  renderCompare();
  renderVarTabs();       // 按钮文案要跟着变
};
$("batch-run").onclick = runBatch;
$("batch-pick").onclick = pickFolder;
$("bgm-pick").onclick = pickBgm;
$("bible-run").onclick = () => runSeries(false);
$("bible-remix").onclick = () => runSeries(true);
refreshBible();
refreshCache();

$("reset").onclick = async () => {
  const d = await post("/api/reset", {});
  if (d.error) return;   // 任务在跑，后端会拒绝，别清空界面
  maxStep = 1; segKey = ""; segScriptKey = "";
  varEdits = {}; varSig = ""; outSig = ""; lastStage = "";
  compareOn = false; cmpSig = ""; scoreSig = "";
  seasonSig = ""; seasonData = null; seasonChecked = 0;
  $("video-wrap").innerHTML = "";
  $("out-hint").textContent = "";
  $("outputs").innerHTML = "";
  $("open").style.display = "none";
  $("seg-editor").innerHTML = "";
  $("var-tabs").innerHTML = "";
  $("var-tabs").style.display = "none";
  $("var-score").innerHTML = "";
  $("var-score").style.display = "none";
  segScriptKey = "";
  $("src-hint").textContent = "";
  setStep(1);
};
$("src").oninput = () => renderAside();
document.querySelectorAll("#steps button").forEach((b) => {
  b.onclick = () => { if (!b.disabled) setStep(+b.dataset.step); };
});

fetch("/api/state").then((r) => r.json()).then((s) => {
  S = s;
  if (s.stage === "segments") setStep(2);
  else if (s.stage === "scripts") setStep(3);
  else if (s.stage === "done") setStep(4);
  renderAside();
});

tick();
