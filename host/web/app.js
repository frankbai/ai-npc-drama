"use strict";
const $ = (s) => document.querySelector(s);
const S = { player: null, state: null, story: null, session: null, cur: null, busy: false, me: null, mindNpc: null };
const PH_COLORS = { 1: ["#5d6b78", "#1c2128"], 2: ["#6b7480", "#20252b"], 3: ["#7a5a3f", "#2b1f15"], 4: ["#5a4a6b", "#1d1824"],
  5: ["#7a3434", "#261212"], 6: ["#6b5a2f", "#24200f"], ending: ["#4a5a6b", "#141a20"] };

// 浏览器里记的东西加个前缀：同一个地址（比如 127.0.0.1:8800）以前跑过别的程序，也不会读到它留下的值
const LS = "ainpcdrama:";
function store(k, v) { try { v === undefined ? localStorage.removeItem(LS + k) : localStorage.setItem(LS + k, v); } catch (e) {} }
function load(k) { try { return localStorage.getItem(LS + k); } catch (e) { return null; } }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function fmt(t, vars) { return String(t || "").replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m)); }
function ui(k, fallback = "") { return ((S.story || {}).ui || {})[k] || fallback; }
function toast(msg, ms = 3600) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms); }
function busy(on, text) { $("#busy").hidden = !on; if (text) $("#busyText").textContent = text; }
function npcInfo(id) { return ((S.story || {}).npcs || []).find((n) => n.id === id) || { id, name: id, expressions: {} }; }
function person(id) { return ((S.state || {}).people || []).find((p) => p.id === id); }

// ================================================================ 头像
// 有 /avatars/<名字>.(png|jpg|webp) 就用图片，没有就用临时头像（代码画的）。有哪些图由 /api/state 的 assets 告诉页面
const AV_FOUND = new Set();
let ASSETS = { cg: new Map(), avatars: new Map() };   // 不带扩展名的文件名 → 实际文件名
const stem = (f) => String(f || "").replace(/\.[^.]+$/, "");
// 内容包里写的是 cg01.png，放进去的是 cg01.webp 也认
function assetFile(kind, name) { return ASSETS[kind].get(stem(name)) || null; }
const EXPR_STYLE = { calm: ["#5f7480", "静"], smile: ["#a8794f", "笑"], angry: ["#8c3a32", "怒"], stubborn: ["#6e5a3a", "倔"],
  stunned: ["#76689a", "怔"], sad: ["#56637a", "哀"], wary: ["#5a6e5a", "疑"], worried: ["#5f6b80", "忧"], stern: ["#3f4347", "肃"],
  cold: ["#2f3a44", "冷"], shy: ["#b06b6b", "羞"], serious: ["#4d4a47", "肃"] };
const ME_COLORS = ["#5d6b78", "#7a5a3f", "#5a6e5a", "#6e4a52", "#4a5a6b", "#7a6a4a"];
function svgAvatar(big, small, color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="${color}" stop-opacity=".95"/><stop offset="1" stop-color="#15181b" stop-opacity=".92"/></linearGradient></defs>
    <circle cx="50" cy="50" r="50" fill="url(#g)"/><circle cx="50" cy="50" r="44" fill="none" stroke="#eef2f4" stroke-opacity=".3"/>
    <text x="50" y="62" font-size="38" text-anchor="middle" fill="#f3f5f6" font-family="Songti SC, STSong, serif">${big}</text>
    ${small ? `<circle cx="79" cy="79" r="15" fill="#f3f5f6"/><text x="79" y="85" font-size="16" text-anchor="middle" fill="${color}" font-family="Songti SC, serif">${small}</text>` : ""}</svg>`;
  return `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}
function probeAvatars(names) {
  for (const n of names.filter((x) => assetFile("avatars", x))) { const i = new Image(); i.onload = () => { AV_FOUND.add(n); document.querySelectorAll(`[data-av="${n}"]`).forEach(paintAv); }; i.src = `/avatars/${assetFile("avatars", n)}`; }
}
function paintAv(el) { const n = el.dataset.av; el.style.backgroundImage = AV_FOUND.has(n) ? `url("/avatars/${assetFile("avatars", n)}")` : el.dataset.fallback; }
function npcAv(npcId, expr) {
  const n = npcInfo(npcId); const key = expr && (n.expressions || {})[expr] ? expr : Object.keys(n.expressions || {})[0] || "calm";
  const [color, mark] = EXPR_STYLE[key] || EXPR_STYLE.calm;
  return { name: `${npcId}_${key}`, fallback: svgAvatar((n.name || "?")[0], mark, color), title: (n.expressions || {})[key] || "" };
}
function meAv(id) {
  const list = ((S.story || {}).player || {}).avatars || [];
  const i = Math.max(0, list.findIndex((a) => a.id === id));
  const a = list[i] || { id: "me_1", mark: "我" };
  return { name: a.id, fallback: svgAvatar(a.mark, "", ME_COLORS[i % ME_COLORS.length]), title: a.label || "" };
}
function setAv(el, av, size = "") { el.className = `av ${size}`; el.dataset.av = av.name; el.dataset.fallback = av.fallback; el.title = av.title; paintAv(el); }
function makeAv(av, size = "") { const s = document.createElement("span"); setAv(s, av, size); return s; }

class NeedLogin extends Error {}
function needLogin(j) { $("#loginErr").textContent = ""; if (!$("#loginDlg").open) $("#loginDlg").showModal(); return new NeedLogin(j.error || "请先输入体验口令"); }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && j.login) throw needLogin(j);
  if (!r.ok) throw new Error(j.error || `请求失败 ${r.status}`);
  return j;
}

// 流式请求：服务器可能直接回 JSON（比如预算用完），也可能回事件流
async function stream(path, body, on) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const ct = r.headers.get("Content-Type") || "";
  if (!ct.includes("event-stream")) {
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && j.login) throw needLogin(j);
    if (j.closed) { on({ t: "closed", message: j.message }); return; }
    throw new Error(j.error || `请求失败 ${r.status}`);
  }
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      if (chunk.startsWith("data:")) on(JSON.parse(chunk.slice(5)));
    }
  }
}

// ================================================================ 状态与侧栏
async function refresh() {
  S.state = await api(`/api/state${S.player ? `?player=${S.player}` : ""}`);
  const st = S.state, me = st.me || {};
  S.me = me; S.story = st.story;
  if (st.assets) ASSETS = { cg: new Map((st.assets.cg || []).map((f) => [stem(f), f])), avatars: new Map((st.assets.avatars || []).map((f) => [stem(f), f])) };
  renderBrand();
  $("#settingsBtn").hidden = me.role !== "admin";
  $("#usageBtn").hidden = !(me.role === "admin" && me.online);
  $("#mindBtn").hidden = !me.show_mind;
  if (!me.show_mind) $("#mind").hidden = true;
  $("#modeDot").className = st.config.mode === "mock" ? "" : "live";
  const sel = $("#player");
  sel.innerHTML = st.players.map((p) => `<option value="${p.id}">${esc(p.name)}（${esc(p.shop)}）${p.mine === false ? " · 别人的" : ""}</option>`).join("") || `<option value="">还没有存档</option>`;
  if (S.player) sel.value = S.player;
  if (!S.player || !st.chapter) {
    $("#chLabel").textContent = ""; $("#chTitle").textContent = "";
    ["#nextChapter", "#accuseBtn", "#endingBtn", "#replayBtn"].forEach((s) => ($(s).hidden = true));
    ["#people", "#eventGroups", "#clues", "#journal"].forEach((s) => ($(s).innerHTML = ""));
    $("#lampText").textContent = ""; $("#lampBar").style.width = "0";
    scene();
    return;
  }
  $("#chLabel").textContent = st.label;
  $("#chTitle").textContent = `《${st.chapter_title}》`;
  $("#nextChapter").hidden = !st.can_advance;
  $("#nextChapter").textContent = ui("next", "进入下一段时日 →");
  $("#accuseBtn").hidden = !st.can_accuse;
  $("#accuseBtn").textContent = ui("accuse", "当众指认 →");
  $("#endingBtn").hidden = !st.ended;
  $("#replayBtn").hidden = !st.cg_done;
  $("#replayBtn").textContent = ui("replay", "重温本章剧情");
  renderLamp(st.budget);
  renderPeople();
  renderEvents(st.events.groups);
  renderClues(st.clues);
  $("#journal").innerHTML = st.journal.map((j) => `<li class="${j.kind === "剧情" ? "story" : ""}"><span class="d">第${j.stage}章${j.kind ? " · " + esc(j.kind) : ""}</span>${esc(j.text)}</li>`).join("") || `<li class="muted">还没有记下什么。</li>`;
  if (!S.session && !$("#log").children.length) scene();
}

function renderBrand() {
  const s = S.story || {};
  $("#seal").textContent = s.seal || "·";
  $("#brandTitle").textContent = s.title || "";
  $("#brandSub").textContent = s.subtitle || "";
  document.title = `${s.title || ""} · ${s.subtitle || "AI NPC Demo"}`;
  $("#peopleTitle").textContent = ui("people", "人物");
  $("#cluesTitle").textContent = ui("clues", "线索簿");
  $("#journalTitle").textContent = ui("journal", "我的手记");
  $("#loginTitle").textContent = s.title ? `${s.title} · ${s.subtitle || ""}` : "AI NPC Demo";
  if (s.login_hint) $("#loginHint").textContent = s.login_hint;
}

function renderLamp(b) {
  if (!b) return;
  const st = S.state;
  $("#lampBar").style.width = `${(b.left / b.per_chapter) * 100}%`;
  $("#lampText").textContent = st.ended ? "" : !st.cg_done ? "先看完这一章的剧情" : b.left ? `${ui("lamp", "今夜的灯")} · ${fmt(ui("lamp_left", "还能说 {n} 句"), { n: b.left })}` : ui("lamp_out", "灯油燃尽了");
}

function renderPeople() {
  const st = S.state, lock = !st.cg_done || st.ended;
  $("#people").innerHTML = st.people.map((p) => {
    const left = p.rounds ? p.rounds.left : 0;
    return `<button class="person ${S.cur === p.id && S.session ? "on" : ""}" data-npc="${p.id}" ${lock || !left ? "disabled" : ""}>
      <span class="person-av" data-pav="${p.id}"></span>
      <b>${esc(p.name)}</b><small class="place">${esc(p.place)}</small>
      <small class="card">${esc(p.card)}</small>
      <small class="left">${st.ended ? "" : (left ? fmt(ui("lamp_left", "还能说 {n} 句"), { n: left }) : "这段时日不能再说了") + (p.inner_visible ? " · 看得见心声" : "")}</small></button>`;
  }).join("");
  document.querySelectorAll("[data-pav]").forEach((el) => setAv(el, npcAv(el.dataset.pav, person(el.dataset.pav).expr), "md"));
}

function renderEvents(groups) {
  const lock = !S.state.cg_done || S.state.ended;
  $("#eventGroups").innerHTML = groups.map((g) => `<section><h3>${esc(g.id)} <small>${esc(g.hint)}</small></h3><div class="evs">${
    g.items.map((e) => `<button data-ev="${e.id}" class="${e.done ? "done" : ""}${e.choices ? " choice" : ""}" ${e.done || lock || g.left === 0 ? "disabled" : ""}>${esc(e.label)}${
      e.done && e.chosen ? `<span class="chosen">（${esc((e.choices.find((c) => c.id === e.chosen) || {}).label || "")}）</span>` : ""}</button>`).join("")
    || `<p class="hint">这段时日没有可做的。</p>`}</div></section>`).join("");
}

function renderClues(clues) {
  $("#clues").innerHTML = clues.map((c) => `<li><details><summary><span class="wt w${c.weight}">${esc(c.weight_name)}</span>${esc(c.title)}</summary>
    <p>${esc(c.text)}</p>${c.note ? `<p class="hint">${esc(c.note)}</p>` : ""}</details></li>`).join("") || `<li class="muted">${esc(ui("no_clues", "还没有线索。"))}</li>`;
}

function scene() {
  const st = S.state;
  $("#scene").innerHTML = st && st.chapter
    ? `<div class="empty"><p>${esc(st.label)} ·《${esc(st.chapter_title)}》</p><p class="hint">${st.ended ? esc(ui("the_end", "故事讲完了。")) : st.cg_done ? esc(ui("cg_done", "去找人说说话，或者做点别的事。")) : "这一章的剧情还没看。"}</p></div>`
    : `<div class="empty"><p>新建一个存档，开始你的故事。</p></div>`;
}

// ================================================================ 对话（主界面与 CG 场景共用）
const MAIN = { log: "#log", opts: "#options", gest: "#gestures", form: "#form", input: "#text" };
const SCENE = { log: "#cgLog", opts: "#cgOptions", gest: "#cgGestures", form: "#cgForm", input: "#cgInput" };

function addMsg(view, cls, text, who, expr, npcId) {
  const d = document.createElement("div");
  d.className = `msg ${cls}`;
  if (who) d.innerHTML = `<span class="who">${esc(who)}</span>`;
  const body = document.createElement("span"); body.textContent = text; d.appendChild(body);
  const isNpc = cls.startsWith("npc"), isMe = cls.startsWith("me");
  let av = null, row = d;
  if (isNpc || isMe) {
    row = document.createElement("div"); row.className = `row ${isNpc ? "npc" : "me"}`;
    av = makeAv(isNpc ? npcAv(npcId || view.npc, expr) : meAv(S.state.me_avatar));
    row.appendChild(av); row.appendChild(d);
  }
  $(view.log).appendChild(row); $(view.log).scrollTop = 1e9;
  return { box: d, body, av, row, remove: () => row.remove() };
}

function setControls(view, { options = [], gestures = [], loading = false, ended = false } = {}) {
  const o = $(view.opts);
  o.innerHTML = loading ? `<div class="loading">……</div>` : options.map((x, i) => `<button data-opt="${i}"><span class="k">${esc(x.kind)}</span>${esc(x.label)}</button>`).join("");
  o.style.gridTemplateColumns = `repeat(${Math.max(1, Math.min(options.length || 3, 4))}, 1fr)`;
  o._opts = options;
  $(view.gest).innerHTML = gestures.map((g) => `<button data-g="${esc(g)}">${esc(g)}</button>`).join("");
  $(view.form).hidden = ended;
  if (!loading) { view.lastOpts = options; view.lastGest = gestures; }
}

// 对方说话的时候，别的操作（切存档、指认、进下一章、新建存档）先锁住，免得半句话丢了
const TOP_ACTIONS = ["#player", "#newPlayer", "#accuseBtn", "#nextChapter", "#replayBtn", "#endingBtn"];
function lock(view, on) {
  S.busy = on;
  [view.opts, view.gest, view.form].forEach((s) => document.querySelectorAll(`${s} button, ${s} input`).forEach((b) => (b.disabled = on)));
  TOP_ACTIONS.forEach((s) => { const el = $(s); if (el) el.disabled = on; });
}

function renderBond(b, flash = {}) {
  const bar = $("#bondbar");
  if (!b) { bar.hidden = true; return; }
  const row = (key, label, cls) => `<div class="bb ${cls} ${flash[key] > 0 ? "up" : flash[key] < 0 ? "down" : ""}"><span class="lab">${label}</span><span class="track"><i style="width:${b[key]}%"></i></span><b>${b[key]}</b></div>`;
  const p = person(S.cur), left = p && p.rounds ? p.rounds.left : 0;
  bar.innerHTML = `<span class="bb-name">${esc(npcInfo(S.cur).name)}</span>${row("trust", "信任", "t")}${row("warmth", "亲近", "w")}<span class="wickline">${fmt(ui("lamp_left", "还能说 {n} 句"), { n: left })}</span>`;
  bar.hidden = false;
}

function feedback(box, a) {
  const bits = [];
  for (const [k, label] of [["trust", "信任"], ["warmth", "亲近"]])
    if (a[k]) bits.push(`<span class="chip ${a[k] > 0 ? "up" : "down"}">${label} ${a[k] > 0 ? "+" : "−"}${Math.abs(a[k])}</span>`);
  if (!bits.length) return;
  const s = document.createElement("div"); s.className = "fb";
  s.innerHTML = bits.join("") + (a.why ? `<span class="why">${esc(a.why)}</span>` : "");
  box.appendChild(s);
}

function innerLine(box, ev) {
  if (!ev.inner && !ev.inner_label) return;
  const s = document.createElement("div");
  s.className = ev.inner ? "inner" : "inner hidden";
  s.innerHTML = ev.inner ? `<span class="lab">${esc(ui("inner", "心声"))}</span>${esc(ev.inner)}` : `<span class="lab">${esc(ui("inner", "心声"))}</span>${esc(ev.inner_label)}`;
  box.appendChild(s);
}

async function runTurn(view, path, body, playerText, playerCls, onDone) {
  if (S.busy) return;
  lock(view, true);
  if (playerText) addMsg(view, playerCls || "me", playerText);
  const m = addMsg(view, "npc typing", "", npcInfo(view.npc).name, null, view.npc);
  setControls(view, { loading: true });
  try {
    await stream(path, body, (ev) => {
      if (ev.t === "session") { view.session = ev.session; if (view === MAIN) S.session = ev.session; }
      else if (ev.t === "expr") { if (m.av) setAv(m.av, npcAv(view.npc, ev.expr)); }
      else if (ev.t === "delta") { m.body.textContent += ev.text; $(view.log).scrollTop = 1e9; }
      else if (ev.t === "done") {
        m.box.classList.remove("typing");
        innerLine(m.box, ev);
        feedback(m.box, ev.applied || {});
        if (view === MAIN) {
          const p = person(S.cur);
          if (p && ev.rounds && ev.rounds.limit) p.rounds = { used: ev.rounds.used, left: Math.max(0, ev.rounds.limit - ev.rounds.used) };
          renderBond(ev.bond, { trust: (ev.applied || {}).trust || 0, warmth: (ev.applied || {}).warmth || 0 });
          if (!ev.leave) refresh().catch(() => {});      // 灯油、别人还能说几句，都跟着变
        }
        setControls(view, { options: ev.options, gestures: ev.gestures, ended: ev.leave });
        $(view.log).scrollTop = 1e9;
        lock(view, false);
        if (onDone) onDone(ev);
      } else if (ev.t === "closed") { m.remove(); throw Object.assign(new Error(ev.message), { closed: true }); }
      else if (ev.t === "error") { throw new Error(ev.message); }
    });
  } catch (e) {
    if (!(e instanceof NeedLogin)) {
      m.remove();
      if (e.closed) { if (onDone) onDone({ closed: true, message: e.message }); }
      else {
        addMsg(view, "err", `没能得到回复：${e.message}`);
        if (body.text && body.kind === "say") $(view.input).value = body.text;
        setControls(view, { options: view.lastOpts || [], gestures: view.lastGest || [] });
      }
    }
    lock(view, false);
    return;
  }
  m.box.classList.remove("typing");
  lock(view, false);
}

// ---------------------------------------------------------------- 找人说话
function sceneHead(p) {
  $("#scene").innerHTML = `<h2>${esc(p.place)} · ${esc(p.name)}</h2><p>${esc(p.place_desc || "")}</p>`;
}

async function visit(npcId) {
  if (!S.player || S.busy) return;
  if (S.session) await leave(true);
  const p = person(npcId); if (!p) return;
  S.session = null; S.cur = npcId; MAIN.npc = npcId;
  $("#log").innerHTML = "";
  sceneHead(p);
  $("#controls").hidden = false;
  renderBond(p.bond);
  renderPeople();
  addMsg(MAIN, "sys", fmt(ui("arrive", "你来到{place}。"), { place: p.place, name: p.name }));
  await runTurn(MAIN, "/api/session/start", { player: S.player, npc: npcId }, null, "me", onChatDone);
}

function onChatDone(ev) {
  if (ev.closed) {
    addMsg(MAIN, "sys", ev.message);
    $("#controls").hidden = true; S.session = null;
    endNotice();
    return;
  }
  if (ev.leave) {
    const sid = S.session;
    S.session = null;
    $("#controls").hidden = true;
    api("/api/session/end", { player: S.player, session: sid }).catch(() => {});
    endNotice();
  }
}

async function endNotice() {
  await refresh();
  const b = S.state.budget, p = person(S.cur);
  const d = addMsg(MAIN, "sys notice", "");
  const name = p ? p.name : "";
  const left = b ? b.left : 0;
  let html = left > 0 ? esc(fmt(ui("see_off", "{name}没有留你。这段时日还能再说 {n} 句。"), { name, n: left })) : esc(ui("lamp_out", "这段时日就到这里了。"));
  if (S.state.can_advance) html += `<br>做完想做的事，就 <button class="small" data-next="1">${esc(ui("next", "进入下一段时日 →"))}</button>`;
  if (S.state.can_accuse) html += `<br>准备好了，就 <button class="small danger" data-accuse="1">${esc(ui("accuse", "当众指认 →"))}</button>`;
  d.body.innerHTML = html;
}

async function say(kind, text, shown) {
  if (!S.session) return;
  await runTurn(MAIN, "/api/turn", { session: S.session, kind, text }, shown ?? text, kind === "gesture" ? "me gesture" : "me", onChatDone);
}

async function leave(silent = false) {
  if (!S.session) return;
  await api("/api/session/end", { player: S.player, session: S.session }).catch(() => {});
  if (!silent) addMsg(MAIN, "sys", fmt(ui("leave_note", "你起身告辞。"), { name: npcInfo(S.cur).name }));
  S.session = null; $("#controls").hidden = true;
  await refresh();
}

// ================================================================ CG
const C = { data: null, part: "cg", phase: 0, line: 0, replay: false, waiting: false, img: null, resolve: null };

function openCG(part = "cg", replay = false) {
  return new Promise(async (resolve) => {
    if (!S.player) return resolve(false);
    let d;
    try { d = await api(`/api/cg?player=${S.player}&part=${part}`); } catch (e) { toast(e.message); return resolve(false); }
    if (part === "cg" && d.done && !replay) return resolve(false);
    if (!d.phases.length) return resolve(true);
    C.data = d; C.part = part; C.replay = replay; C.phase = part === "cg" && !replay ? d.phase : 0; C.line = 0; C.resolve = resolve;
    const [c1, c2] = PH_COLORS[part === "ending" ? "ending" : d.chapter] || PH_COLORS[1];
    $("#cgPh").style.setProperty("--c1", c1); $("#cgPh").style.setProperty("--c2", c2);
    $("#cgPhLabel").textContent = part === "ending" ? "终章" : d.label;
    $("#cgPhTitle").textContent = d.title;
    setImage(d.image);
    $("#cgSkip").hidden = !replay;
    $("#cgSceneHead").textContent = ui("scene_head", "这一幕由你们自己演完");
    $("#cg").hidden = false;
    renderPhase();
  });
}

function setImage(name) {
  if (name === C.img) return;
  C.img = name;
  const img = $("#cgImg");
  img.hidden = true; $("#cgPh").hidden = false;
  const file = name && assetFile("cg", name);
  if (file) { img.onload = () => { if (C.img === name) { img.hidden = false; $("#cgPh").hidden = true; } }; img.onerror = () => { img.hidden = true; }; img.src = `/cg/${file}`; }
}

function renderPhase() {
  const ph = C.data.phases[C.phase];
  if (!ph) return closeCG(true);
  if (ph.image) setImage(ph.image);
  if (ph.type === "director") {
    if (C.replay || C.part !== "cg") { C.phase++; C.line = 0; return renderPhase(); }
    return startScene(ph);
  }
  $("#cgScene").hidden = true; $("#cgScript").hidden = false;
  renderLine();
}

async function renderLine() {
  const ph = C.data.phases[C.phase];
  const ln = ph.lines[C.line];
  if (!ln) return phaseEnd();
  if (ln.image) setImage(ln.image);
  const isNpc = !!npcInfo(ln.who).expressions && (S.story.npcs || []).some((n) => n.id === ln.who);
  const label = isNpc || ln.who === "me" || ln.who === "other" ? ln.name
    : ln.who === "card" ? `· ${ln.name || "剧情卡片"} ·` : ln.who === "memory" ? ui("flashback", "你想起") : ln.who === "tip" ? ui("tip", "提示") : "";
  $("#cgWho").textContent = label || "";
  $("#cgArt").classList.toggle("memory", ln.who === "memory");
  const cgAv = $("#cgAv");
  cgAv.hidden = !(isNpc || ln.who === "me");
  if (isNpc) setAv(cgAv, npcAv(ln.who, ln.expr), "lg");
  if (ln.who === "me") setAv(cgAv, meAv(C.data.me_avatar), "lg");
  const t = $("#cgText");
  t.className = `cg-text ${isNpc ? "npc" : ln.who}`;
  if (ln.slot && !ln.text) {
    t.textContent = "……"; C.waiting = true; $("#cgHint").textContent = "";
    try { const r = await api("/api/cg/slot", { player: S.player, slot: ln.slot, part: C.part }); ln.text = r.text; ln.expr = r.expr; if (r.expr && isNpc) setAv(cgAv, npcAv(ln.who, r.expr), "lg"); }
    catch (e) { if (e instanceof NeedLogin) { C.waiting = false; return; } ln.text = ""; }
    C.waiting = false;
    if (!ln.text) { C.line++; return renderLine(); }   // 生成失败就跳过这一句
  }
  t.textContent = ln.text;
  $("#cgHint").textContent = "点击继续 ▸";
}

async function phaseEnd() {
  if (C.waiting) return;                      // 正在等服务器：连点不会多跳一段
  if (C.replay || C.part !== "cg") { C.phase++; C.line = 0; return renderPhase(); }
  C.waiting = true; $("#cgSceneNext").disabled = true;
  try { C.data = await api("/api/cg/phase_done", { player: S.player, phase: C.phase }); }
  catch (e) { toast(e.message); return; }
  finally { C.waiting = false; $("#cgSceneNext").disabled = false; }
  if (C.data.new_clues && C.data.new_clues.length) toast(`记进了线索簿：${C.data.new_clues.map((c) => c.title).join("、")}`, 5000);
  if (C.data.done) return closeCG(true);
  C.phase = C.data.phase; C.line = 0;
  renderPhase();
}

async function startScene(ph) {
  $("#cgScript").hidden = true; $("#cgScene").hidden = false;
  $("#cgLog").innerHTML = ""; $("#cgSceneNext").hidden = true; $("#cgSceneRetry").hidden = true; $("#cgBeats").textContent = "";
  SCENE.session = null; SCENE.npc = ph.npc;
  // 刷新页面前已经演到一半：接着演，不重新开场
  if (C.data.scene) {
    const h = await api(`/api/history?player=${S.player}&session=${C.data.scene}`).catch(() => null);
    if (h && h.session && h.messages.some((m) => m.role === "npc")) return resumeScene(h);
  }
  await runTurn(SCENE, "/api/cg/scene", { player: S.player }, null, "me", onSceneDone);
  if (!$("#cgLog .row.npc")) {               // 没能开场（模型出错、限流……）：给一个重试的按钮，不让人卡在这里
    setControls(SCENE, { ended: true });
    $("#cgSceneRetry").hidden = false;
    $("#cgSceneRetry").onclick = () => { C.data.scene = null; startScene(ph); };
  }
}

function resumeScene(h) {
  SCENE.session = h.session;
  for (const m of h.messages) {
    if (m.kind === "arrive") continue;
    if (m.role === "npc") { const x = addMsg(SCENE, "npc", m.text, npcInfo(h.npc).name, m.expr, h.npc); innerLine(x.box, m); }
    else addMsg(SCENE, m.kind === "gesture" ? "me gesture" : "me", m.kind === "gesture" ? `（${m.text}）` : m.text);
  }
  setControls(SCENE, { options: h.options, gestures: h.gestures, ended: h.ended });
  onSceneDone({ scene: h.scene, rounds: h.rounds, leave: h.ended });
}

function onSceneDone(ev) {
  if (ev.scene) $("#cgBeats").textContent = `剧情进展 ${ev.scene.done.length}/${ev.scene.total}`;
  if (ev.rounds && ev.rounds.limit) $("#cgBeats").textContent += ` · 还可再说 ${Math.max(0, ev.rounds.limit - ev.rounds.used)} 句`;
  if (ev.leave) { setControls(SCENE, { ended: true }); $("#cgSceneNext").hidden = false; }
}

function closeCG(finished) {
  $("#cg").hidden = true;
  $("#cgArt").classList.remove("memory");
  C.img = null; $("#cgImg").hidden = true; $("#cgPh").hidden = false;   // 下次打开从场景卡开始，不残留上一张图
  const r = C.resolve; C.resolve = null;
  if (finished && C.part === "cg" && !C.replay) toast(ui("cg_done", "这一章的剧情结束了。"), 5000);
  refresh().then(() => { if (!$("#mind").hidden) loadMind(); });
  if (r) r(finished);
}

$("#cgScript").addEventListener("click", () => { if (C.waiting) return; C.line++; renderLine(); });
$("#cgSceneNext").addEventListener("click", phaseEnd);
$("#cgSkip").addEventListener("click", () => closeCG(false));
$("#cgForm").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#cgInput").value.trim(); if (!t || !SCENE.session) return; $("#cgInput").value = "";
  runTurn(SCENE, "/api/turn", { session: SCENE.session, kind: "say", text: t }, t, "me", onSceneDone); });

// ================================================================ 章节、指认、结局
async function nextChapter() {
  if (S.busy || !S.player) return;
  if (S.session) await leave(true);
  if (S.state.has_outro) await openCG("outro");
  busy(true, ui("next_busy", "时日流转……"));
  try {
    await api("/api/chapter/next", { player: S.player });
    S.session = null; $("#controls").hidden = true; $("#bondbar").hidden = true; $("#log").innerHTML = "";
    await refresh(); busy(false);
    await openCG("cg");
  } catch (err) { busy(false); toast(err.message); }
}

function openAccuse() {
  const acc = S.story.accuse; if (!acc) return;
  $("#accuseTitle").textContent = acc.title || "指认";
  $("#accusePrompt").textContent = acc.prompt || "";
  $("#pickN").textContent = acc.pick || 3;
  $("#accuseErr").textContent = "";
  $("#suspectPick").innerHTML = acc.suspects.map((s, i) => `<label><input type="radio" name="suspect" value="${s.id}" ${i === 0 ? "" : ""}><span>${esc(s.name)}</span></label>`).join("");
  $("#evidencePick").innerHTML = S.state.clues.map((c) => `<label title="${esc(c.text)}"><input type="checkbox" name="clue" value="${c.id}"><span class="wt w${c.weight}">${esc(c.weight_name)}</span><span>${esc(c.title)}<small>${esc(c.text)}</small></span></label>`).join("") || `<p class="hint">你手里没有任何证据。</p>`;
  $("#evidencePick").onchange = () => {
    const on = [...document.querySelectorAll("#evidencePick input:checked")].length;
    document.querySelectorAll("#evidencePick input").forEach((x) => (x.disabled = !x.checked && on >= (acc.pick || 3)));
  };
  $("#accuseDlg").showModal();
}

$("#accuseDlg").addEventListener("close", async () => {
  if ($("#accuseDlg").returnValue !== "ok") return;
  const suspect = (document.querySelector("#suspectPick input:checked") || {}).value;
  const clues = [...document.querySelectorAll("#evidencePick input:checked")].map((x) => x.value);
  if (!suspect || !clues.length) { toast("选一个人，再摆出至少一样证据。"); return openAccuse(); }
  try {
    if (S.session) await leave(true);
    await api("/api/accuse", { player: S.player, suspect, clues });
    await refresh();
    await openCG("ending");
    await showEpilogue();
  } catch (err) { toast(err.message); }
});

async function showEpilogue() {
  const e = await api(`/api/epilogue?player=${S.player}`).catch((err) => ({ error: err.message }));
  if (e.error) return toast(e.error);
  const names = Object.fromEntries((S.story.accuse ? S.story.accuse.suspects : []).map((s) => [s.id, s.name]));
  $("#epiTitle").textContent = e.ending ? `结局 · ${e.ending.title}` : "结局";
  const acc = e.accuse || {};
  const clueTitles = (acc.clues || []).map((id) => (S.state.clues.find((c) => c.id === id) || {}).title || id);
  $("#epiBody").innerHTML = `
    <p class="hint">你指认了 <b>${esc(names[acc.suspect] || acc.suspect || "")}</b>，说出了：${esc(clueTitles.join("、"))}。证据分量 ${acc.score ?? "—"}${e.bond_label ? `；${esc(e.bond_label)} ${acc.bond ?? "—"}` : ""}。</p>
    <h4>${esc(ui("epilogue", "他们眼里的你"))}</h4>
    ${e.people.map((p) => `<div class="mcard epi"><span class="epi-av" data-eav="${p.id}"></span><div><b>${esc(p.name)}</b><small class="muted"> · 信任 ${p.trust} · 亲近 ${p.warmth} · 相处 ${p.meetings} 次</small><p>${esc(p.impression || "（没有留下什么看法）")}</p>${(p.quotes || []).map((q) => `<p class="epi-quote">记得你说过：“${esc(q)}”</p>`).join("")}</div></div>`).join("")}
    <p class="hint">${esc(e.the_end || "")}</p>`;
  document.querySelectorAll("[data-eav]").forEach((el) => setAv(el, npcAv(el.dataset.eav, ""), "md"));
  $("#epilogueDlg").showModal();
}

// ================================================================ 心智面板
function list(items, f = (x) => esc(x), empty = "（还没有）") {
  return items && items.length ? `<ul>${items.map((x) => `<li>${f(x)}</li>`).join("")}</ul>` : `<p class="muted">${empty}</p>`;
}
async function loadMind() {
  if (!S.player || !S.state.people) { $("#mindBody").innerHTML = `<p class="muted">先选一个存档。</p>`; $("#mindTabs").innerHTML = ""; return; }
  const ids = S.state.people.map((p) => p.id);
  if (!ids.includes(S.mindNpc)) S.mindNpc = ids.includes(S.cur) ? S.cur : ids[0];
  $("#mindTabs").innerHTML = S.state.people.map((p) => `<button class="tab ${p.id === S.mindNpc ? "on" : ""}" data-mind="${p.id}">${esc(p.name)}</button>`).join("");
  const v = await api(`/api/mind?player=${S.player}&npc=${S.mindNpc}`).catch((e) => ({ error: e.message }));
  if (v.error) { $("#mindBody").innerHTML = `<p class="muted">${esc(v.error)}</p>`; return; }
  const l = v.life || {}, names = Object.fromEntries((S.story.npcs || []).map((n) => [n.id, n.name]));
  $("#mindBody").innerHTML = `
    <div class="mcard"><h4>这段时日的${esc(v.npc.name)}（第 ${v.stage} 章${l._fallback ? " · 推演失败，用的是剧本锚点" : ""}）</h4>
      <p><b>心情</b> ${esc(l.mood)}</p><p><b>在做</b> ${esc(l.doing)}</p><p><b>挂心</b> ${esc(l.on_mind)}</p></div>
    <div class="mcard"><h4>交情</h4><p>信任 ${v.bond.trust} · 亲近 ${v.bond.warmth} · 相处 ${v.bond.meetings} 次</p>
      <p class="muted">每章的最高点：${(v.bond_log || []).map((r) => `第${r.stage}章 ${r.max_sum}`).join(" · ")}</p>
      <p class="muted">心事按章节解锁：目前解锁到第 ${v.secret_stage} 章。心声：${{ always: "一直给玩家看", trust: "信任够了才给看", never: "从不给看" }[v.npc.inner_mode] || ""}${v.last_inner ? `；最近一句心声：${esc(v.last_inner)}` : ""}</p></div>
    <div class="mcard"><h4>对你的看法</h4><p>${esc(v.impression) || '<span class="muted">还没有形成看法。</span>'}</p></div>
    <div class="mcard"><h4>记得的关于你的事</h4>${list(v.facts)}</div>
    <div class="mcard"><h4>记下的你的原话</h4>${list(v.quotes, (q) => `<span class="muted">第${q.stage}章 · ${esc(q.topic)}</span><br>“${esc(q.text)}”`)}</div>
    <div class="mcard"><h4>日记（含 CG 里发生的事）</h4>${list(v.episodes, (e) => `<span class="muted">第${e.stage}章 · 重要度${e.salience}</span><br>${esc(e.text)}`)}</div>
    <div class="mcard"><h4>约定</h4>${list(v.commitments, (c) => `${c.status === "done" ? "✓ " : ""}${c.who === "npc" ? "NPC 答应你" : "你答应 NPC"}：${esc(c.text)}`)}</div>
    <div class="mcard"><h4>已经对你讲过的事</h4>${list(v.told)}</div>
    <div class="mcard"><h4>听说的关于你的事</h4>${list(v.heard, (h) => `<span class="muted">第${h.stage}章 · ${esc(h.source)} · 可靠程度${esc(h.certainty)}</span><br>${esc(h.text)}`)}</div>
    <div class="mcard"><h4>转告给别人的事</h4>${list(v.relayed, (h) => `<span class="muted">第${h.stage}章 · 告诉了${esc(names[h.npc] || h.npc)}</span><br>${esc(h.text)}`)}</div>
    <div class="mcard"><h4>近来的消息</h4>${list(v.news)}</div>
    <div class="mcard"><h4>为你生成的专属台词</h4>${list(v.lines, (x) => `<span class="muted">${esc(x.slot)}</span><br>${esc(x.text)}`)}</div>
    ${S.me && S.me.role === "admin" ? `<div class="mcard"><h4>调试</h4><button class="ghost small" id="showPrompt" ${S.session ? "" : "disabled"}>看最近一轮的完整提示词</button></div>` : ""}`;
}

// ================================================================ 事件绑定
function openChoice(evId) {
  const g = S.state.events.groups.flatMap((x) => x.items).find((e) => e.id === evId);
  if (!g) return;
  $("#choiceTitle").textContent = g.label;
  $("#choiceIntro").textContent = g.intro || "";
  $("#choiceList").innerHTML = g.choices.map((c) => `<button type="button" data-choose="${c.id}" data-for="${evId}">${esc(c.label)}</button>`).join("");
  $("#choiceDlg").showModal();
}

async function doEvent(evId, choice) {
  if (S.busy || S.evBusy) return;
  S.evBusy = true;
  document.querySelectorAll("#eventGroups button").forEach((b) => (b.disabled = true));
  try {
    const r = await api("/api/event", { player: S.player, event: evId, choice });
    toast(r.note + (r.new_clues && r.new_clues.length ? `\n（记进了线索簿：${r.new_clues.map((c) => c.title).join("、")}）` : ""), 7000);
    await refresh(); if (!$("#mind").hidden) loadMind();
  } catch (err) { toast(err.message); await refresh().catch(() => {}); }
  finally { S.evBusy = false; }
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const inScene = !!b.closest("#cgScene");
  const view = inScene ? SCENE : MAIN;
  if (b.dataset.opt !== undefined) {
    const o = $(view.opts)._opts[+b.dataset.opt];
    if (inScene) return runTurn(SCENE, "/api/turn", { session: SCENE.session, kind: "option", text: o.say }, o.say, "me", onSceneDone);
    return say("option", o.say);
  }
  if (b.dataset.g) {
    if (inScene) return runTurn(SCENE, "/api/turn", { session: SCENE.session, kind: "gesture", text: b.dataset.g }, `（${b.dataset.g}）`, "me gesture", onSceneDone);
    return say("gesture", b.dataset.g, `（${b.dataset.g}）`);
  }
  if (b.dataset.npc) return visit(b.dataset.npc);
  if (b.dataset.ev) {
    const ev = S.state.events.groups.flatMap((x) => x.items).find((x) => x.id === b.dataset.ev);
    return ev && ev.choices ? openChoice(ev.id) : doEvent(b.dataset.ev);
  }
  if (b.dataset.choose) { $("#choiceDlg").close(); return doEvent(b.dataset.for, b.dataset.choose); }
  if (b.dataset.mind) { S.mindNpc = b.dataset.mind; return loadMind(); }
  if (b.dataset.next) return nextChapter();
  if (b.dataset.accuse) return openAccuse();
  if (b.id === "showPrompt") {
    const p = await api(`/api/prompt?session=${S.session}`).catch(() => ({}));
    $("#promptBody").textContent = (p.prompt || []).map((m) => `【${m.role}】\n${m.content}`).join("\n\n") + (p.output ? `\n\n【模型输出 · ${p.ms}ms】\n${p.output}` : "");
    $("#promptDlg").showModal();
  }
});

$("#replayBtn").addEventListener("click", () => openCG("cg", true));
$("#nextChapter").addEventListener("click", nextChapter);
$("#accuseBtn").addEventListener("click", openAccuse);
$("#endingBtn").addEventListener("click", async () => { await openCG("ending", true); await showEpilogue(); });
$("#form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#text").value.trim(); if (!t) return; $("#text").value = ""; say("say", t); });
$("#leave").addEventListener("click", () => leave(false));
$("#player").addEventListener("change", async (e) => {
  if (S.session) await leave(true);
  S.player = e.target.value || null; store("player", S.player || undefined);
  S.cur = null; $("#log").innerHTML = ""; $("#bondbar").hidden = true; $("#controls").hidden = true;
  await refresh(); if (!$("#mind").hidden) loadMind();
  if (S.state.chapter && !S.state.cg_done) openCG("cg");
});

function openNewPlayer() {
  $("#playerForm").reset();
  const pc = (S.story || {}).player || {};
  $("#introText").textContent = (S.story || {}).intro || "";
  $("#nameLabel").textContent = (pc.name || {}).label || "你的名字";
  $("#shopLabel").textContent = (pc.shop || {}).label || "店名";
  const f = $("#playerForm");
  f.name.placeholder = (pc.name || {}).placeholder || ""; f.name.maxLength = (pc.name || {}).max || 8;
  f.shop.placeholder = (pc.shop || {}).placeholder || ""; f.shop.maxLength = (pc.shop || {}).max || 10;
  $("#genderPick").innerHTML = (pc.genders || []).map((g, i) => `<label><input type="radio" name="gender" value="${g.id}" ${i === 0 ? "checked" : ""}><span>${esc(g.label)}</span></label>`).join("");
  const avs = pc.avatars || [];
  $("#avPick").innerHTML = avs.map((a, i) => `<label><input type="radio" name="avatar" value="${a.id}" ${i === 0 ? "checked" : ""}><span class="av md" data-slot="${i}"></span>${esc(a.label)}</label>`).join("");
  $("#avPick").querySelectorAll("[data-slot]").forEach((el) => setAv(el, meAv(avs[+el.dataset.slot].id), "md"));
  $("#playerDlg").showModal();
}
$("#newPlayer").addEventListener("click", openNewPlayer);
$("#playerDlg").addEventListener("close", async () => {
  if ($("#playerDlg").returnValue !== "ok") return;
  const f = new FormData($("#playerForm"));
  try {
    if (S.session) await leave(true);
    const r = await api("/api/players", { name: f.get("name"), shop: f.get("shop"), gender: f.get("gender"), avatar: f.get("avatar") });
    S.player = r.id; S.cur = null; store("player", r.id); $("#log").innerHTML = ""; $("#bondbar").hidden = true; $("#controls").hidden = true;
    await refresh(); await openCG("cg");
  } catch (err) { toast(err.message); }
});
$("#mindBtn").addEventListener("click", () => { const m = $("#mind"); m.hidden = !m.hidden; store("mind", m.hidden ? "0" : "1"); if (!m.hidden) loadMind(); });
$("#refreshMind").addEventListener("click", loadMind);
$("#loginDlg").addEventListener("cancel", (e) => e.preventDefault());
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("/api/login", { code: new FormData($("#loginForm")).get("code") }); $("#loginDlg").close(); $("#loginForm").reset(); await init(); }
  catch (err) { $("#loginErr").textContent = "口令不对"; if (!$("#loginDlg").open) $("#loginDlg").showModal(); }
});
$("#usageBtn").addEventListener("click", async () => {
  try {
    const u = await api("/api/admin/usage");
    const rows = u.rows.map((r) => `<tr><td>${r.day}</td><td>${esc(r.model)}</td><td>${r.calls}</td><td>${r.input}</td><td>${r.cached}</td><td>${r.output}</td><td>$${r.cost_usd}</td></tr>`).join("");
    $("#usageBody").innerHTML = `<p>累计约 <b>$${u.total_usd}</b> · ${u.players} 个存档 · NPC 共说了 ${u.npc_lines} 句</p>
      <p><a href="/api/admin/export" download>导出全部数据</a></p>
      <p class="hint">按每百万 token 输入 $${u.prices_per_million.input}、缓存 $${u.prices_per_million.cached}、输出 $${u.prices_per_million.output} 估算，以账单为准。</p>
      <table class="usage"><tr><th>日期</th><th>模型</th><th>调用</th><th>输入</th><th>其中缓存</th><th>输出</th><th>费用</th></tr>${rows || '<tr><td colspan="7" class="muted">还没有用量</td></tr>'}</table>`;
    $("#usageDlg").showModal();
  } catch (err) { toast(err.message); }
});
$("#settingsBtn").addEventListener("click", () => {
  const c = S.state.config, f = $("#settingsForm");
  f.mode.value = c.mode === "mock" ? "mock" : "live";
  for (const k of ["base_url", "model", "fast_model", "reasoning_effort"]) f[k].value = c[k] || "";
  f.api_key.value = ""; f.clear_key.checked = false;
  $("#keyState").textContent = c.has_key ? "已保存密钥（或使用环境变量 OPENAI_API_KEY）。" : "尚未设置密钥。";
  $("#settingsDlg").showModal();
});
$("#settingsDlg").addEventListener("close", async () => {
  if ($("#settingsDlg").returnValue !== "ok") return;
  const f = $("#settingsForm");
  const body = { mode: f.mode.value, base_url: f.base_url.value.trim(), model: f.model.value.trim(), fast_model: f.fast_model.value.trim(),
    reasoning_effort: f.reasoning_effort.value.trim() || null, api_key: f.api_key.value, clear_key: f.clear_key.checked };
  try { await api("/api/config", body); await refresh(); toast(body.mode === "mock" ? "已切换到模拟模式" : `已切换到真实模型：${body.model}`); }
  catch (err) { toast(err.message); }
});

async function restore() {
  if (!S.player) return;
  const h = await api(`/api/history?player=${S.player}`).catch(() => null);
  if (!h || !h.session || !h.messages.length) return;
  const p = person(h.npc); if (!p) return;
  S.session = h.session; S.cur = h.npc; MAIN.npc = h.npc; MAIN.session = h.session;
  sceneHead(p);
  $("#log").innerHTML = "";
  for (const m of h.messages) {
    if (m.kind === "arrive") continue;
    if (m.role === "npc") { const x = addMsg(MAIN, "npc", m.text, p.name, m.expr, h.npc); innerLine(x.box, m); }
    else addMsg(MAIN, m.kind === "gesture" ? "me gesture" : "me", m.kind === "gesture" ? `（${m.text}）` : m.text);
  }
  $("#controls").hidden = false;
  renderBond(p.bond);
  renderPeople();
  setControls(MAIN, { options: h.options || [], gestures: h.gestures || S.story.gestures || [] });
}

async function init() {
  S.player = load("player");
  if (load("mind") === "1") $("#mind").hidden = false;
  await refresh();
  if (!S._probed) {
    S._probed = true;
    probeAvatars([...(S.story.npcs || []).flatMap((n) => Object.keys(n.expressions || {}).map((k) => `${n.id}_${k}`)),
      ...((S.story.player || {}).avatars || []).map((a) => a.id)]);
  }
  if (!S.state.players.some((p) => p.id === S.player)) {
    S.player = (S.state.players.find((p) => p.mine !== false) || {}).id || null;
    store("player", S.player || undefined); await refresh();
  }
  await restore();
  if (!$("#mind").hidden) loadMind();
  if (!S.player) openNewPlayer();
  else if (S.state.chapter && !S.state.cg_done) openCG("cg");
}
init().catch((e) => { if (!(e instanceof NeedLogin)) toast(e.message); });
