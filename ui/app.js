// California Live. Watches the event stream from core/live_ui.py; never sends
// anything back. All text arrives from the house (what he said, call
// transcripts, other people's WhatsApp messages inside log lines), so it is
// only ever written with textContent, never as HTML.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const fmtMs = (ms) => (ms === undefined || ms === null) ? "-" :
    ms >= 10000 ? (ms / 1000).toFixed(1) + "s" : ms >= 1000 ? (ms / 1000).toFixed(2) + "s" : ms + "ms";
  const TIME = { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], TIME);
  const mss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const STATE_LABELS = {
    idle: "Idle", listening: "Listening", thinking: "Thinking",
    speaking: "Speaking", error: "Error", unknown: "Connecting",
  };
  const STAGES = [
    ["speech_end", "speech end"], ["stt_done", "heard"], ["llm_first_token", "first token"],
    ["first_sentence", "first sentence"], ["first_audio", "first audio"], ["reply_done", "done"],
  ];
  const DROPPED = new Set(["no_speech", "short", "empty_transcript"]);

  let hello = null;
  let serverStarted = null;
  let lastEventAt = Date.now();

  // ---------------------------------------------------------------- state
  let lastState = "unknown";

  function setState(state, remember = true) {
    if (remember) lastState = state;
    const node = $("state");
    node.dataset.state = state;
    $("state-label").textContent = STATE_LABELS[state] || state;
    haloState(state);
  }

  function setLink(kind, label) {
    $("link").dataset.link = kind;
    $("link-label").textContent = label;
    // A page that has lost the stream must not keep showing the last state
    // as if it were current.
    setState(kind === "live" ? lastState : "unknown", false);
  }

  // ----------------------------------------------------------------- wake
  const spark = { points: [], windowMs: 30000 };
  let threshold = { idle: null, reply: null, current: null };

  function setThresholds(h) {
    threshold.idle = h.threshold;
    threshold.reply = h.barge_in_threshold;
    threshold.current = h.threshold;
    $("wake-threshold").textContent = h.threshold != null ? Number(h.threshold).toFixed(2) : "-";
    if (h.threshold != null) {
      $("tick-idle").style.left = (h.threshold * 100) + "%";
      $("tick-idle").dataset.label = `wake ${Number(h.threshold).toFixed(2)}`;
    }
    const barge = $("tick-barge");
    if (h.barge_in_threshold != null && h.barge_in_threshold !== h.threshold) {
      barge.hidden = false;
      barge.style.left = (h.barge_in_threshold * 100) + "%";
      barge.dataset.label = `barge ${Number(h.barge_in_threshold).toFixed(2)}`;
    } else {
      barge.hidden = true;
    }
  }

  function onWake(d) {
    const score = Number(d.score) || 0;
    const t = d.threshold != null ? d.threshold : threshold.idle;
    threshold.current = t;
    $("wake-score").textContent = score.toFixed(3);
    $("wake-phase").textContent = d.phase === "reply" ? "listening over her reply" : "idle";
    $("meter-fill").style.width = Math.min(100, score * 100) + "%";
    $("meter").classList.toggle("over", t != null && score >= t);
    spark.points.push({ t: Date.now(), s: score, th: t });
    trimSpark();
    const peak = spark.points.reduce((m, p) => Math.max(m, p.s), 0);
    $("wake-peak").textContent = peak.toFixed(3);
    drawSpark();
  }

  function trimSpark() {
    const cutoff = Date.now() - spark.windowMs;
    while (spark.points.length && spark.points[0].t < cutoff) spark.points.shift();
  }

  function drawSpark() {
    const canvas = $("spark");
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const now = Date.now();
    const x = (t) => w - ((now - t) / spark.windowMs) * w;
    const y = (s) => h - 2 - Math.min(1, s) * (h - 4);
    // threshold line
    if (threshold.current != null) {
      ctx.strokeStyle = css.getPropertyValue("--muted");
      ctx.setLineDash([3, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y(threshold.current)); ctx.lineTo(w, y(threshold.current)); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (!spark.points.length) return;
    ctx.strokeStyle = css.getPropertyValue("--accent");
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    spark.points.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.s)) : ctx.moveTo(x(p.t), y(p.s))));
    ctx.stroke();
  }

  function onWakeHit(d) {
    // The earliest sign of a wake: the acknowledgement line plays before the
    // published state leaves idle, so the halo cannot wait for `state`.
    if (d.phase === "idle" && !d.ignored && halo) halo.wake();
    const li = el("li", d.ignored ? "ignored" : "");
    li.append(el("span", "", new Date().toLocaleTimeString([], TIME) + " "));
    li.append(el("b", "", Number(d.score).toFixed(3)));
    li.append(el("span", "", d.phase === "reply" ? (d.ignored ? " own name" : " barge-in") : ""));
    const hits = $("hits");
    hits.prepend(li);
    while (hits.children.length > 6) hits.lastChild.remove();
    if (!reducedMotion) {
      const m = $("meter");
      m.classList.remove("hit"); void m.offsetWidth; m.classList.add("hit");
    }
  }

  // ------------------------------------------------------------------ now
  let turn = null; // the turn being built
  let clockTimer = null;

  function resetNow() {
    $("now-empty").hidden = false;
    $("now-body").hidden = true;
    $("now-clock").textContent = "";
    clearInterval(clockTimer);
  }

  function startTurn(d) {
    latestTurn = Math.max(latestTurn, Number(d.turn) || 0);
    setMood("none");
    caption.clear();
    turn = { id: d.turn, startedAt: Date.now(), marks: {}, fields: {}, tools: [], sentences: [] };
    $("now-empty").hidden = true;
    $("now-body").hidden = false;
    $("said").hidden = true;
    $("reply").hidden = true;
    $("reply-text").replaceChildren();
    $("tools").replaceChildren();
    const stages = $("stages");
    stages.replaceChildren(...STAGES.map(([key, name]) => {
      const li = el("li", "stage");
      li.dataset.key = key;
      li.append(el("div", "name", name), el("div", "ms", "-"));
      return li;
    }));
    $("tl-dots").replaceChildren();
    $("tl-run").style.width = "0";
    $("tl-end").textContent = "6s";
    clearInterval(clockTimer);
    const tick = () => {
      if (!turn) return;
      $("now-clock").textContent = fmtMs(Date.now() - turn.startedAt);
      tickRail();
    };
    tick();
    clockTimer = setInterval(tick, 100);
  }

  function onMark(d) {
    if (!turn || turn.id !== d.turn) return;
    turn.marks[d.name] = d.ms;
    const stages = $("stages");
    stages.querySelectorAll(".stage.latest").forEach((n) => n.classList.remove("latest"));
    const node = stages.querySelector(`[data-key="${d.name}"]`);
    if (node) {
      node.classList.add("done", "latest");
      node.querySelector(".ms").textContent = fmtMs(d.ms);
    }
    drawTimeline();
  }

  // The turn on one line: a dot per stage at its time, the run up to the
  // latest one. The scale grows with the turn so a long reply still fits.
  function drawTimeline() {
    const marks = STAGES.filter(([k]) => turn.marks[k] != null).map(([k]) => [k, turn.marks[k]]);
    if (!marks.length) return;
    const latest = Math.max(...marks.map(([, ms]) => ms));
    const scale = Math.max(6000, latest * 1.15);
    const dots = $("tl-dots");
    for (const [k, ms] of marks) {
      let dot = dots.querySelector(`[data-key="${k}"]`);
      if (!dot) {
        dot = el("span", "tl-dot");
        dot.dataset.key = k;
        dots.append(dot);
      }
      dot.classList.toggle("end", k === "reply_done");
      dot.style.left = ((100 * ms) / scale).toFixed(2) + "%";
    }
    $("tl-run").style.width = ((100 * latest) / scale).toFixed(2) + "%";
    $("tl-end").textContent = Math.round(scale / 1000) + "s";
    $("timeline").setAttribute("aria-label", marks.map(([k, ms]) => `${STAGES.find(([s]) => s === k)[1]} ${fmtMs(ms)}`).join(", "));
  }

  function onSet(d) {
    if (!turn || turn.id !== d.turn) return;
    Object.assign(turn.fields, d.fields || {});
    if (d.fields && d.fields.transcript) {
      $("said").hidden = false;
      $("said-text").textContent = d.fields.transcript;
    }
  }

  function onSentence(d) {
    if (!turn) return;
    $("reply").hidden = false;
    const p = $("reply-text");
    p.querySelectorAll(".current").forEach((n) => n.classList.remove("current"));
    if (p.childNodes.length) p.append(" ");
    p.append(el("span", "current", d.text));
  }

  // ---------------------------------------------------------------- tools
  // Every tool call is a row in the Now panel's rail and, while the halo is
  // drawing, a satellite off the ring (ui/halo.js). Both read the same entry.
  const DEVICE = {
    control_tv: "tv", control_lights: "lights", control_vacuum: "vacuum",
    control_whatsapp: "whatsapp", control_phone: "phone", web_search: "web",
  };
  const DEVICE_WORD = { tv: "TV", lights: "Lights", vacuum: "Vacuum", whatsapp: "WhatsApp", phone: "Phone", web: "Web", tool: "Tool" };
  // What each action usually takes, in ms, from the bench numbers in CLAUDE.md.
  // The ring fills against it, so a 33s deep wake reads as normal, not stuck.
  // Absent: no honest estimate (a web search, a call), and the ring sweeps.
  const USUAL = {
    control_tv: {
      turn_on: 33000, turn_off: 4000, stremio_play: 20000, stremio_continue: 20000,
      stremio_get_progress: 400, stremio_sync_library: 1000, youtube_search: 6000,
      youtube_playlist: 5000, get_status: 1000, switch_hdmi: 3000, launch_app: 3000,
      volume_up: 300, volume_down: 300, volume_set: 300, mute: 300, unmute: 300,
      play_pause: 600, stop: 600, next: 600, prev: 600, fast_forward: 600, rewind: 600,
      go_home: 600, go_back: 600,
    },
    control_lights: { light_on: 1000, light_off: 1000, light_brightness: 1000, light_color: 1000, light_status: 600 },
    control_vacuum: { vacuum_status: 2000, vacuum_clean_rooms: 5000, vacuum_clean_all: 5000, vacuum_stop: 1000, vacuum_dock: 1000 },
    control_whatsapp: { whatsapp_send: 2000, whatsapp_unread: 3000, whatsapp_read: 2500, whatsapp_find_contact: 200 },
    control_phone: { phone_find: 1500, phone_status: 200, phone_log: 200 },
  };
  const usual = (name, action) => (USUAL[name] && USUAL[name][action]) || 0;
  const deviceOf = (name) => DEVICE[name] || "tool";
  const actionWords = (d) => (d.action || d.name || "").replace(/^(whatsapp|vacuum|light|stremio|phone)_/, "").replace(/_/g, " ");
  const toolKey = (d) => `${d.turn}:${d.name}:${d.start_ms}`;
  const toolEntries = new Map();      // key -> entry; the live state of every call shown
  let replaying = true;               // the backlog before the server's `replayed`
  const endedTurns = new Set();
  let latestTurn = 0;

  // The server's clock, seen from here: the smallest (local - server) gap over
  // the events received is the best guess at skew plus latency. Only used to
  // age a call that started before this page saw it (a replay).
  let skewMs = null;
  function noteServerTs(ts) {
    if (typeof ts !== "number") return;
    const gap = Date.now() - ts * 1000;
    if (skewMs === null || gap < skewMs) skewMs = gap;
  }
  const perfAt = (ts) => {
    if (typeof ts !== "number" || skewMs === null) return performance.now();
    const ageMs = Date.now() - skewMs - ts * 1000;
    return performance.now() - Math.max(0, ageMs);
  };

  function railRow(key, d, dev) {
    let li = $("tools").querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (li) return li;
    li = el("li");
    li.dataset.key = key;
    li.dataset.st = "run";
    li.style.setProperty("--tint", window.haloTint ? window.haloTint(dev) : "var(--accent)");
    const glyph = window.haloGlyph ? window.haloGlyph(dev, "g") : el("span", "g");
    const name = el("span", "tool-name", actionWords(d));
    name.append(el("small", "", DEVICE_WORD[dev]));
    const trk = el("span", "trk");
    trk.append(el("span", "fill"));
    if (usual(d.name, d.action)) trk.append(el("span", "exp"));
    const ms = el("span", "tool-ms", "0ms");
    ms.append(el("small", "", "running"));
    li.append(glyph, name, trk, ms);
    $("tools").append(li);
    return li;
  }

  function railLabel(li, dev, target) {
    li.querySelector(".tool-name small").textContent = target ? `${DEVICE_WORD[dev]} \u00b7 ${target}` : DEVICE_WORD[dev];
  }

  function railBar(li, ms, expected) {
    const scale = Math.max(expected || 0, ms, expected ? 0 : 1000) * 1.1;
    li.querySelector(".fill").style.width = Math.min(100, (100 * ms) / scale).toFixed(1) + "%";
    const exp = li.querySelector(".exp");
    if (exp) exp.style.left = ((100 * expected) / scale).toFixed(1) + "%";
    li.querySelector(".tool-ms").firstChild.textContent = fmtMs(Math.round(ms));
  }

  // Tick the running rows; called by the turn clock.
  function tickRail() {
    for (const li of $("tools").querySelectorAll('li[data-st="run"]')) {
      const e = toolEntries.get(li.dataset.key);
      if (e) railBar(li, performance.now() - e.t0, e.expected);
    }
  }

  const NOTE = { ok: "", done: "", fail: "failed", ask: "waiting for you", lost: "no result" };
  const RAIL_WORD = { ok: "worked", done: "done", fail: "failed", ask: "waiting for you", lost: "no result", live: "on a call" };

  function satStart(e) {
    if (halo && !replaying) halo.toolStart(e.key, { dev: e.dev, label: e.label, expected: e.expected, t0: e.t0 });
  }

  function onToolStart(d) {
    const key = toolKey(d);
    const dev = deviceOf(d.name);
    const label = hello && hello.transcripts === false ? "" : (d.target || "");
    let e = toolEntries.get(key);
    if (e) {
      // A repeat start is a label update: web search learns its query late.
      if (label) e.label = label;
      if (halo && !replaying && label) halo.toolStart(key, { label });
    } else {
      e = { key, turn: d.turn, name: d.name, action: d.action, dev, label, expected: usual(d.name, d.action),
            t0: perfAt(d.ts), ts: d.ts, st: "run", endTs: null };
      toolEntries.set(key, e);
      // He answered: a read-back from an earlier turn has done its job.
      for (const other of toolEntries.values()) {
        if (other.st === "ask" && other.turn !== d.turn) retire(other);
      }
      satStart(e);
    }
    if (turn && turn.id === d.turn) {
      const li = railRow(key, d, dev);
      railLabel(li, dev, label);
    }
  }

  function onTool(d) {
    const key = toolKey(d);
    const dev = deviceOf(d.name);
    let e = toolEntries.get(key);
    if (!e) {
      // The start was lost (a dropped event, a reconnect): show the end anyway.
      onToolStart({ ...d });
      e = toolEntries.get(key);
    }
    const label = hello && hello.transcripts === false ? "" : (d.target || e.label);
    e.label = label;
    const st = d.ok === true ? "ok" : d.ok === false ? "fail" : d.awaiting === true ? "ask" : "done";
    // A placed call carries on after the turn: it orbits until the call ends.
    const live = d.name === "control_phone" && d.action === "phone_call" && d.ok === true;
    e.st = live ? "live" : st;
    e.endTs = d.ts;
    e.ms = d.ms;
    if (halo && !replaying) {
      if (live) halo.toolLive(key, "ringing");
      else halo.toolEnd(key, st, st === "ok" || st === "done" ? fmtMs(d.ms) : NOTE[st], d.ms);
    }
    if (!live && st !== "ask") toolEntries.delete(key);
    if (turn && turn.id === d.turn) {
      const li = railRow(key, d, dev);
      railLabel(li, dev, label);
      li.dataset.st = live ? "ask" : st;
      railBar(li, d.ms, e.expected);
      li.querySelector(".tool-ms small").textContent = RAIL_WORD[live ? "live" : st];
      if (d.result && hello && hello.transcripts !== false) {
        li.querySelector(".tool-result")?.remove();
        li.append(el("span", "tool-result", d.result));
      }
      railDetail(li, d);
    }
  }

  // Tapping a finished row opens what the tool was asked to do (the message
  // that went out, the call brief); its answer is already on the row. Text only, and only
  // with transcripts on: the server sends no `input` otherwise.
  function railDetail(li, d) {
    if (!hello || hello.transcripts === false) return;
    const rows = Object.entries(d.input || {});
    if (!rows.length) return;
    const box = el("div", "tool-detail");
    box.hidden = li.getAttribute("aria-expanded") !== "true";
    const dl = el("dl");
    for (const [k, v] of rows) dl.append(el("dt", "", k.replace(/_/g, " ")), el("dd", "", String(v)));
    box.append(dl);
    li.querySelector(".tool-detail")?.remove();
    li.append(box);
    if (li.classList.contains("openable")) return;
    li.classList.add("openable");
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-expanded", "false");
    const flip = (ev) => {
      // Selecting text inside the opened detail is not a tap.
      if (ev && ev.target && ev.target.closest && ev.target.closest(".tool-detail")) return;
      const b = li.querySelector(".tool-detail");
      if (!b) return;
      b.hidden = !b.hidden;
      li.setAttribute("aria-expanded", String(!b.hidden));
    };
    li.addEventListener("click", flip);
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); flip(); }
    });
  }

  function retire(e) {
    toolEntries.delete(e.key);
    if (halo && !replaying) halo.toolRemove(e.key);
  }

  // A turn is over: whatever never reported back is shown as lost, not left
  // spinning forever, and a parked read-back from before it lets go.
  function toolsAtTurnEnd(turnId) {
    endedTurns.add(turnId);
    for (const e of [...toolEntries.values()]) {
      if (e.turn === turnId && e.st === "run") {
        toolEntries.delete(e.key);
        if (halo && !replaying) halo.toolEnd(e.key, "lost", NOTE.lost);
      } else if (e.st === "ask" && e.turn < turnId) {
        retire(e);
      }
    }
    for (const li of $("tools").querySelectorAll('li[data-st="run"]')) {
      li.dataset.st = "lost";
      li.querySelector(".tool-ms small").textContent = RAIL_WORD.lost;
    }
  }

  // The phone call: its satellite stays up while the call runs, pulses on each
  // line, and goes when the call ends. A page opened mid-call (or a call whose
  // tool event is out of the ring) still gets one from call_state.
  function liveCall() {
    return [...toolEntries.values()].reverse().find((e) => e.dev === "phone" && e.st === "live");
  }

  function callSatellite(d) {
    if (d.state === "ended") {
      const e = liveCall();
      if (!e) return;
      toolEntries.delete(e.key);
      const failed = d.ended_by === "error" || d.ended_by === "dial_failed";
      if (halo && !replaying) halo.toolEnd(e.key, failed ? "fail" : "done", failed ? "call failed" : "call ended");
      return;
    }
    let e = liveCall();
    if (!e) {
      const key = `call:${d.started_at || d.ts}`;
      const label = hello && hello.transcripts === false ? "" : (d.label || "");
      e = { key, turn: 0, name: "control_phone", action: "phone_call", dev: "phone", label, expected: 0,
            t0: perfAt(d.ts), ts: d.ts, st: "live", endTs: null };
      toolEntries.set(key, e);
      if (halo && !replaying) { satStart(e); halo.toolLive(key, ""); }
    }
    e.note = d.state === "dialing" ? "on the call" : "starting";
    // The timer reads the call's length, like the Call panel: from the dial.
    if (d.state === "dialing") { e.t0 = perfAt(d.ts); e.ts = d.ts; }
    if (halo && !replaying) halo.toolLive(e.key, e.note, d.state === "dialing" ? e.t0 : undefined);
  }

  function onCallLinePulse() {
    const e = liveCall();
    if (e && halo && !replaying) halo.toolPulse(e.key);
  }

  // The backlog is in. Draw only what is still happening: a call still
  // running in a turn that has not ended, a read-back from the last turn
  // still inside its window, a phone call in progress. Finished calls from
  // the replay are history; animating them would replay ten docks at once.
  function onReplayed(d) {
    noteServerTs(d.ts);
    replaying = false;
    document.body.classList.remove("loading");
    loadCalls();
    if (!halo) return;
    halo.toolsClear();
    for (const e of [...toolEntries.values()]) {
      e.t0 = perfAt(e.ts);
      const askFresh = e.st === "ask" && e.turn === latestTurn && typeof e.endTs === "number"
        && (Date.now() - (skewMs || 0)) / 1000 - e.endTs < 120;
      if (e.st === "run" && !endedTurns.has(e.turn)) {
        satStart(e);
      } else if (askFresh) {
        satStart(e);
        halo.toolEnd(e.key, "ask", NOTE.ask, e.ms);
      } else if (e.st === "live") {
        satStart(e);
        halo.toolLive(e.key, e.note || "");
      } else {
        toolEntries.delete(e.key);
      }
    }
  }

  function onTurnEnd(d) {
    toolsAtTurnEnd(d.turn);
    addTurnToFeed(d.record || {}, d.ts, d.turn);
    if (turn && turn.id === d.turn) {
      turn = null;
      clearInterval(clockTimer);
      setTimeout(() => { if (!turn) resetNow(); }, 4000);
    }
  }

  // ----------------------------------------------------------------- halo
  // ui/halo.js draws; this section turns events into its inputs. Her voice
  // never plays in the browser, so each clip_start carries its loudness
  // envelope and VoiceClock replays it against this page's own clock.
  const MOOD_WORDS = { none: "", happy: "happy", smug: "smug", unimpressed: "unimpressed", sheepish: "sheepish", roll: "eye-roll" };
  const debug = new URLSearchParams(location.search).has("debug");
  let halo = null;
  let haloShown = null;      // last state handed to the halo, to spot transitions
  let mood = "none";
  let moodTimer = null;

  const VoiceClock = {
    clip: null,                                   // {id, start, hz, env, ms}
    begin(d) {
      const offset = hello && hello.voice_offset_ms != null ? Number(hello.voice_offset_ms) : 120;
      this.clip = { id: d.clip, start: performance.now() + offset, hz: Number(d.hz) || 25, env: d.env || [], ms: Number(d.ms) || 0 };
      return this.clip;
    },
    // A stopped clip (barge-in, "stop") drops at once so the ring settles; a
    // clip that ran out ends by its duration, which the offset already shifts.
    end(d) { if (this.clip && this.clip.id === d.clip && d.stopped) this.clip = null; },
    level(now) {
      const c = this.clip;
      if (!c) return 0;
      const t = (now - c.start) / 1000;
      if (t < 0) return 0;                        // waiting out voice_offset_ms
      if (t * 1000 > c.ms + 150) { this.clip = null; return 0; }
      const i = Math.floor(t * c.hz);
      return c.env[Math.min(i, c.env.length - 1)] || 0;
    },
  };

  // The sentence she is saying, revealed word by word over the clip.
  const caption = {
    words: [], start: 0, ms: 0, shown: -1, clip: null, frozen: false,
    show(text, clip) {
      this.words = String(text).split(/\s+/).filter(Boolean);
      this.start = clip.start; this.ms = clip.ms; this.clip = clip.id; this.shown = -1;
      this.frozen = false;
      $("pin-line").textContent = this.words.join(" ");
      if (reducedMotion) this.render(this.words.length);
    },
    // She was cut off: leave the words she got to, reveal no more.
    freeze() { this.frozen = true; },
    clear() {
      this.words = []; this.shown = -1; this.clip = null; this.frozen = false;
      $("halo-caption").replaceChildren();
      $("pin-line").textContent = "";
    },
    tick(now) {
      if (!this.words.length || reducedMotion || this.frozen) return;
      const elapsed = Math.max(0, now - this.start);
      const n = this.ms > 0 ? Math.ceil(this.words.length * Math.min(1, elapsed / this.ms)) : this.words.length;
      if (n !== this.shown) this.render(n);
    },
    render(n) {
      this.shown = n;
      const w = this.words;
      if (n <= 0) {
        $("halo-caption").replaceChildren(el("span", "rest", w.join(" ")));
        return;
      }
      $("halo-caption").replaceChildren(
        el("span", "said", w.slice(0, n - 1).join(" ") + (n > 1 ? " " : "")),
        el("span", "now", w[n - 1]),
        el("span", "rest", n < w.length ? " " + w.slice(n).join(" ") : ""),
      );
    },
  };

  function haloState(state) {
    const prev = haloShown;
    haloShown = state;
    $("halo-state").textContent = (STATE_LABELS[state] || state).toLowerCase();
    pinText();
    if (halo) halo.setState(state);
    if (state === prev) return;
    if (state === "listening") caption.clear();
    // Back to idle: let the mood linger, then clear it.
    if (state === "idle") holdMood();
  }

  function setMood(m) {
    mood = MOOD_WORDS[m] !== undefined ? m : "none";
    if (halo) halo.setMood(mood);
    $("halo-mood").textContent = MOOD_WORDS[mood];
    $("halo-mood-sep").hidden = mood === "none";
    pinText();
    clearTimeout(moodTimer);
    if (haloShown === "idle") holdMood();
  }

  function holdMood() {
    clearTimeout(moodTimer);
    if (mood === "none") return;
    const hold = hello && hello.mood_hold_ms != null ? Number(hello.mood_hold_ms) : 6000;
    moodTimer = setTimeout(() => { if (haloShown === "idle") setMood("none"); }, hold);
  }

  function onClipStart(d) {
    if (d.kind === "ack" && halo) halo.wake();
    const clip = VoiceClock.begin(d);
    if (d.text) caption.show(d.text, clip);
  }

  function onClipEnd(d) {
    const playing = VoiceClock.clip && VoiceClock.clip.id === d.clip;
    VoiceClock.end(d);
    if (d.stopped && playing && halo) halo.hush();
    if (d.stopped && caption.clip === d.clip) caption.freeze();
  }

  function onMic(d) { if (halo) halo.setMic(Number(d.level) || 0); }

  function onMood(d) { setMood(d.mood || "none"); }

  function haloFrame(now) {
    requestAnimationFrame(haloFrame);
    const level = VoiceClock.level(now);
    if (halo) halo.setVoice(level);
    caption.tick(now);
    if (pinned && !reducedMotion) miniRing.draw(now, level);
  }

  // ------------------------------------------------------------ pinned strip
  // Once the halo scrolls away the header turns into a strip that keeps her in
  // view: a small ring, her state, the sentence she is saying. The ring is a
  // 2D canvas, not a second WebGL halo: at 40px the halo's embers would be
  // blobs, and a second render loop would cost the frame budget.
  let pinned = false;

  function pinText() {
    const word = STATE_LABELS[haloShown] || "Connecting";
    $("pin-state").textContent = mood !== "none" ? `${word}, ${MOOD_WORDS[mood]}` : word;
    if (pinned && reducedMotion) miniRing.draw(performance.now(), 0);
  }

  const miniRing = {
    ps: Array.from({ length: 56 }, (_, i) => ({
      a: (i / 56) * Math.PI * 2 + Math.random() * 0.2, r: 0.84 + Math.random() * 0.3,
      sp: 0.1 + Math.random() * 0.25, sz: 0.5 + Math.random() * 1.3, ph: Math.random() * 6.3,
      c: i % 7 === 0 ? "255,154,107" : i % 3 === 0 ? "179,136,255" : "255,111,168",
    })),
    draw(now, level) {
      const cv = $("pin-ring");
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = cv.clientWidth || 40, h = cv.clientHeight || 40;
      if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
      const c = cv.getContext("2d");
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);
      const t = now / 1000;
      const st = haloShown;
      const live = st === "speaking" || st === "listening" || st === "thinking";
      const env = st === "speaking" ? 0.2 + level * 0.8 : st === "listening" ? 0.35 : st === "thinking" ? 0.25 : 0.1;
      const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.32 * (1 + env * 0.08);
      const g = c.createRadialGradient(cx, cy, 0, cx, cy, R);
      g.addColorStop(0, `rgba(255,190,215,${0.45 + env * 0.4})`);
      g.addColorStop(0.45, `rgba(255,111,168,${0.18 + env * 0.25})`);
      g.addColorStop(1, "rgba(179,136,255,0)");
      c.fillStyle = g;
      c.beginPath(); c.arc(cx, cy, R, 0, 6.283); c.fill();
      c.globalCompositeOperation = "lighter";
      const spin = st === "thinking" ? 1.8 : live ? 1 : 0.4;
      for (const p of this.ps) {
        const a = p.a + t * p.sp * spin;
        const rr = R * (p.r + Math.sin(t * 2 + p.ph) * 0.04 + env * 0.08 * Math.sin(p.ph * 3 + t * 6));
        const al = (0.3 + 0.5 * (0.5 + 0.5 * Math.sin(t * 3 + p.ph))) * (haloShown === "unknown" ? 0.45 : 1);
        c.fillStyle = `rgba(${p.c},${al.toFixed(3)})`;
        c.beginPath(); c.arc(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr, p.sz * (1 + env * 0.3), 0, 6.283); c.fill();
      }
      c.globalCompositeOperation = "source-over";
    },
  };

  function setPinned(on) {
    on = on && !!halo && !document.body.classList.contains("ambient");
    if (on === pinned) return;
    pinned = on;
    document.body.classList.toggle("pinned", on);
    if (on) miniRing.draw(performance.now(), 0);
  }

  function setupPin() {
    if (!("IntersectionObserver" in window)) return;
    // Pinned once the bottom of the halo (less the caption's room) has gone
    // up behind the header, not merely touched the top of the window.
    const io = new IntersectionObserver(([e]) => {
      setPinned(!e.isIntersecting && e.boundingClientRect.top < (e.rootBounds ? e.rootBounds.top + 60 : 60));
    }, { rootMargin: "-56px 0px 0px 0px" });
    io.observe($("halo-end"));
  }

  function showPill() {
    document.body.classList.remove("has-halo");
  }

  function setupHalo() {
    const section = $("halo");
    if (typeof window.createHalo !== "function" || !window.THREE) {
      section.hidden = false;
      section.classList.add("nogl");
      $("halo-nogl").hidden = false;
      return;
    }
    try {
      halo = window.createHalo($("halo-canvas"), {
        small: window.matchMedia("(max-width: 700px)").matches,
        reducedMotion,
        palette: "dusk",
        debug,
        overlay: { sats: $("halo-sats") },
      });
    } catch (err) {
      // A halo that cannot start must never take the page down with it: this
      // runs before connect(), so a throw here used to leave a dead page.
      console.error("halo", err);
      halo = null;
    }
    if (!halo) {
      section.hidden = false;
      section.classList.add("nogl");
      $("halo-nogl").hidden = false;
      return;
    }
    section.hidden = false;
    if (!halo.ok) {
      halo = null;
      section.classList.add("nogl");
      $("halo-nogl").hidden = false;
      return;
    }
    document.body.classList.add("has-halo");
    halo.setState(lastState);
    requestAnimationFrame(haloFrame);
    setupAmbient();
    setupPin();
  }

  // A server with `ui.halo: false` gets the page exactly as it was before.
  function haloAllowed(h) {
    if (h.halo !== false || !halo) return;
    halo.dispose();
    halo = null;
    setPinned(false);
    $("halo").hidden = true;
    setAmbient(false);
    showPill();
  }

  // Ambient mode: only the halo, full screen, the screen kept awake. Every
  // browser API here is optional; a refusal just leaves the overlay.
  let wakeLock = null;
  let ownFullscreen = false;

  async function keepAwake() {
    try {
      if (navigator.wakeLock && document.visibilityState === "visible") {
        wakeLock = await navigator.wakeLock.request("screen");
      }
    } catch (err) { wakeLock = null; }
  }

  function setAmbient(on) {
    if (on) setPinned(false);
    document.body.classList.toggle("ambient", on);
    const btn = $("halo-full");
    btn.setAttribute("aria-pressed", String(on));
    btn.textContent = on ? "Exit full screen" : "Full screen";
    if (on) {
      const root = document.documentElement;
      if (root.requestFullscreen && !document.fullscreenElement) {
        root.requestFullscreen().then(() => { ownFullscreen = true; }).catch(() => {});
      }
      keepAwake();
    } else {
      if (document.fullscreenElement && ownFullscreen && document.exitFullscreen) {
        document.exitFullscreen().catch(() => {});
      }
      ownFullscreen = false;
      if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
    }
    if (halo) halo.resize();
  }

  function setupAmbient() {
    $("halo-full").addEventListener("click", () => setAmbient(!document.body.classList.contains("ambient")));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("ambient")) setAmbient(false);
    });
    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement && ownFullscreen) setAmbient(false);
    });
    // The spec releases a wake lock whenever the page is hidden; take it back.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && document.body.classList.contains("ambient")) keepAwake();
    });
    if (location.hash === "#ambient") setAmbient(true);
  }

  // ----------------------------------------------------------------- feed
  function segments(m, total) {
    const order = [
      ["seg-listen", 0, m.speech_end],
      ["seg-stt", m.speech_end, m.stt_done],
      ["seg-llm", m.stt_done, m.first_sentence ?? m.llm_first_token],
      ["seg-tts", m.first_sentence ?? m.llm_first_token, m.first_audio],
      ["seg-speak", m.first_audio, m.reply_done],
    ];
    const out = [];
    for (const [cls, a, b] of order) {
      if (a === undefined || b === undefined || b <= a) continue;
      out.push([cls, ((b - a) / total) * 100]);
    }
    return out;
  }

  function addTurnToFeed(r, ts, turnId) {
    $("feed-empty").hidden = true;
    const marks = r.marks || {};
    const total = r.total_ms || 1;
    const outcome = r.outcome || "unknown";
    const li = el("li", "turn");
    if (DROPPED.has(outcome)) li.classList.add("dropped");
    if (outcome === "error") li.classList.add("error");

    const btn = el("button");
    btn.setAttribute("aria-expanded", "false");
    const mid = el("div", "mid");
    const what = el("div", "what");
    what.append(el("span", "outcome", outcome.replace(/_/g, " ")));
    const tools = (r.tools || []).map((t) => t.action || t.name);
    what.append(document.createTextNode(r.transcript || (tools.length ? tools.join(", ") : DROPPED.has(outcome) ? "nothing said" : "")));
    const bar = el("div", "bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", STAGES.filter(([k]) => k in marks).map(([k, n]) => `${n} ${fmtMs(marks[k])}`).join(", ") || "no timings");
    for (const [cls, pct] of segments(marks, total)) {
      const s = el("span", cls);
      s.style.width = pct + "%";
      s.style.background = `var(--${cls})`;
      bar.append(s);
    }
    mid.append(what, bar);
    btn.append(el("span", "when", clock(ts)), mid, el("span", "total", fmtMs(r.total_ms)));

    const detail = el("div", "detail");
    detail.hidden = true;
    const marksLine = STAGES.filter(([k]) => k in marks).map(([k, n]) => `${n} ${fmtMs(marks[k])}`).join("  ");
    if (marksLine) detail.append(el("div", "mono", marksLine));
    for (const t of r.tools || []) {
      const how = t.ok === true ? "worked" : t.ok === false ? "failed" : t.awaiting ? "asked" : "";
      detail.append(el("div", "mono", `${t.name}.${t.action || ""}${t.target ? " (" + t.target + ")" : ""} ${fmtMs(t.ms)}${how ? " " + how : ""}: ${t.result || ""}`));
      for (const [k, v] of Object.entries(t.input || {})) detail.append(el("div", "tool-in", `${k.replace(/_/g, " ")}: ${v}`));
    }
    if (r.reply) detail.append(el("div", "", r.reply));
    if (r.error) detail.append(el("div", "", r.error));
    btn.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      btn.setAttribute("aria-expanded", String(!detail.hidden));
    });
    li.append(btn, detail);
    if (turnId != null) {
      li.dataset.turn = turnId;
      feedByTurn.set(turnId, li);
      for (const pending of subsByTurn.get(turnId) || []) li.append(pending.root);
      subsByTurn.delete(turnId);
    }
    const list = $("turns");
    list.prepend(li);
    while (list.children.length > 200) list.lastChild.remove();
  }

  // ----------------------------------------------------------------- call
  let callTimer = null;
  let callStarted = null;

  function onCallState(d) {
    callSatellite(d);
    callSubState(d);
    const panel = $("call");
    if (d.state === "starting") {
      panel.hidden = false;
      $("call-h").textContent = "Starting a call";
      $("call-who").textContent = d.label || "";
      $("call-lines").replaceChildren();
      callStarted = null;
      $("call-clock").textContent = "0:00";
    } else if (d.state === "dialing") {
      panel.hidden = false;
      $("call-h").textContent = "On a call";
      callStarted = Date.now();
      clearInterval(callTimer);
      callTimer = setInterval(() => { $("call-clock").textContent = mss((Date.now() - callStarted) / 1000); }, 500);
    } else if (d.state === "ended") {
      clearInterval(callTimer);
      $("call-h").textContent = `Call ended${d.ended_by ? ", " + d.ended_by.replace(/_/g, " ") : ""}`;
      if (d.error) $("call-who").textContent = `${d.label || ""}: ${d.error}`;
      setTimeout(() => { $("call").hidden = true; }, 60000);
    }
  }

  function onCallLine(d) {
    onCallLinePulse();
    $("call").hidden = false;
    const list = $("call-lines");
    let li = list.querySelector(`[data-index="${d.index}"]`);
    if (!li) {
      li = el("li", d.who === "them" ? "them" : "her");
      li.dataset.index = d.index;
      const body = el("div");
      body.append(el("div", "who", d.who === "them" ? "them" : "California"), el("div", "text", ""));
      li.append(el("span", "at", mss(d.at || 0)), body);
      list.append(li);
    }
    li.querySelector(".text").textContent += d.text;
    list.scrollTop = list.scrollHeight;
    if (liveSub) {
      fillLine(liveSub.lines, d);
      liveSub.lines.scrollTop = liveSub.lines.scrollHeight;
    }
  }

  // ---------------------------------------------------------- call history
  // A call is a sub-turn: nested under the turn that placed it in the feed,
  // filling in line by line while it runs, and listed in the Calls panel
  // afterwards. Past calls are fetched from the server (logs/calls.jsonl) on
  // demand, so the live event ring never has to hold them.
  const feedByTurn = new Map();
  const subsByTurn = new Map();
  const subs = new Map();
  let liveSub = null;

  const GOOD = /book|deliver|confirm|success|answered|agreed|done/;
  const BAD = /fail|error|no_answer|declin|refus|not_placed|busy/;
  const tone = (status) => {
    const st = String(status || "");
    return BAD.test(st) ? "bad" : GOOD.test(st) ? "good" : "";
  };
  const dur = (sec) => (sec == null ? "" : sec >= 60 ? `${Math.floor(sec / 60)}m ${String(Math.round(sec % 60)).padStart(2, "0")}s` : `${Math.round(sec)}s`);

  function fillLine(list, d) {
    let li = list.querySelector(`[data-index="${d.index}"]`);
    if (!li) {
      li = el("li", d.who === "them" ? "them" : "her");
      li.dataset.index = d.index;
      const body = el("div");
      body.append(el("div", "who", d.who === "them" ? "them" : "California"), el("div", "text", ""));
      li.append(el("span", "at", mss(d.at || 0)), body);
      list.append(li);
    }
    li.querySelector(".text").textContent += d.text;
  }

  function chipsFor(rec) {
    const m = rec.meta || {};
    const row = el("div", "call-chips");
    const add = (k, v) => { if (v !== null && v !== undefined && v !== "") row.append(el("span", "call-chip", `${k} ${v}`)); };
    add("rang", m.ring_s != null ? dur(m.ring_s) : "");
    add("talked", m.talk_s != null ? dur(m.talk_s) : "");
    add("turns", m.her_turns != null ? `${m.her_turns} / ${m.their_turns}` : "");
    add("interruptions", m.interruptions);
    add("ended", m.ended_how);
    add("number", m.number_source);
    return row;
  }

  // The body of one call: chips, the whole timed transcript, copy.
  function renderCall(host, rec) {
    host.replaceChildren();
    const summary = rec.outcome && rec.outcome.summary;
    if (summary && hello && hello.transcripts !== false) host.append(el("p", "call-sum", summary));
    host.append(chipsFor(rec));
    const lines = el("ol", "call-lines call-full");
    (rec.transcript || []).forEach((l, i) => fillLine(lines, { index: i, who: l.who, text: l.text, at: l.at }));
    if (!lines.children.length) {
      host.append(el("p", "muted", hello && hello.transcripts === false ? "Transcripts are switched off." : "No transcript was kept for this call."));
    } else {
      host.append(lines);
      const copy = el("button", "call-btn", "Copy transcript");
      copy.type = "button";
      copy.addEventListener("click", () => copyText((rec.transcript || []).map((l) => `[${mss(l.at || 0)}] ${l.who === "them" ? "Them" : "California"}: ${l.text}`).join("\n"), copy));
      host.append(copy);
    }
    return lines;
  }

  function copyText(text, btn) {
    const done = () => { const was = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = was; }, 1500); };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done, () => {}); return; }
    const ta = el("textarea");
    ta.value = text;
    ta.style.position = "absolute"; ta.style.left = "-9999px";
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (err) { /* a locked-down browser: nothing to do */ }
    ta.remove();
  }

  async function fetchCall(id) {
    try {
      const res = await fetch(`calls/${encodeURIComponent(id)}`, { cache: "no-store" });
      return res.ok ? await res.json() : null;
    } catch (err) {
      return null;
    }
  }

  function callHead(label, status, seconds) {
    const head = el("button", "subhead");
    head.type = "button";
    head.setAttribute("aria-expanded", "false");
    const pill = el("span", "call-pill", String(status || "calling").replace(/_/g, " "));
    pill.dataset.tone = tone(status);
    const d = el("span", "muted mono", seconds == null ? "" : dur(seconds));
    if (window.haloGlyph) head.append(window.haloGlyph("phone", "g"));
    head.append(el("span", "sub-who", label), pill, d, el("span", "chev", "›"));
    return { head, pill, d };
  }

  function callSub(id, turnId, label) {
    let sub = subs.get(id);
    if (sub) return sub;
    const root = el("div", "sub");
    const { head, pill, d } = callHead(label ? `Call to ${label}` : "Call", "calling", null);
    const body = el("div", "sub-body");
    body.hidden = true;
    const lines = el("ol", "call-lines call-full");
    body.append(lines);
    root.append(head, body);
    sub = { id, turn: turnId, root, head, pill, d, body, lines, loaded: false, done: false };
    head.addEventListener("click", async () => {
      body.hidden = !body.hidden;
      head.setAttribute("aria-expanded", String(!body.hidden));
      if (!body.hidden && !sub.loaded && sub.done) await loadSub(sub);
    });
    subs.set(id, sub);
    return sub;
  }

  async function loadSub(sub) {
    const rec = await fetchCall(sub.id);
    if (!rec) return;
    sub.loaded = true;
    sub.lines = renderCall(sub.body, rec);
    sub.pill.textContent = String(rec.status || "").replace(/_/g, " ");
    sub.pill.dataset.tone = tone(rec.status);
    sub.d.textContent = dur(rec.duration_s);
  }

  function placeSub(sub) {
    const li = sub.turn != null ? feedByTurn.get(sub.turn) : null;
    if (li) { li.append(sub.root); return; }
    if (sub.turn != null && !endedTurns.has(sub.turn)) {
      // The turn that placed it is still running: nest it when it lands.
      if (!subsByTurn.has(sub.turn)) subsByTurn.set(sub.turn, []);
      subsByTurn.get(sub.turn).push(sub);
      return;
    }
    $("feed-empty").hidden = true;
    const solo = el("li", "turn call-solo");
    solo.append(sub.root);
    $("turns").prepend(solo);
  }

  function callSubState(d) {
    if (!d.call_id) return;
    if (d.state === "starting") {
      liveSub = callSub(d.call_id, d.turn, hello && hello.transcripts === false ? "" : d.label);
      placeSub(liveSub);
      liveSub.body.hidden = false;
      liveSub.head.setAttribute("aria-expanded", "true");
    } else if (d.state === "dialing" && liveSub) {
      liveSub.pill.textContent = "on the call";
    } else if (d.state === "ended") {
      const sub = subs.get(d.call_id);
      if (!sub) return;
      sub.done = true;
      sub.pill.textContent = String(d.ended_by || "ended").replace(/_/g, " ");
      if (liveSub === sub) liveSub = null;
      if (!replaying) {
        // The server has the final status, outcome and timings once the log write lands.
        setTimeout(async () => { await loadSub(sub); loadCalls(); }, 1500);
      }
    }
  }

  // The Calls panel: every logged call, newest first.
  async function loadCalls() {
    let list = null;
    try {
      const res = await fetch("calls", { cache: "no-store" });
      list = res.ok ? await res.json() : null;
    } catch (err) {
      list = null;
    }
    if (!Array.isArray(list)) return;
    const ol = $("calls-list");
    ol.replaceChildren();
    $("calls-empty").hidden = list.length > 0;
    for (const c of list) {
      const li = el("li", "call-row");
      const { head } = callHead(c.to || c.kind || "Call", c.status, c.duration_s);
      head.prepend(el("span", "muted mono", c.ts ? new Date(c.ts).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : ""));
      const body = el("div", "sub-body");
      body.hidden = true;
      let loaded = false;
      const load = async () => {
        const rec = await fetchCall(c.id);
        if (!rec) {
          const retry = el("button", "call-btn", "Try again");
          retry.type = "button";
          retry.addEventListener("click", load);
          body.replaceChildren(el("p", "muted", "Couldn't load this call."), retry);
          return;
        }
        loaded = true;
        renderCall(body, rec);
        const turnLi = c.turn != null ? feedByTurn.get(c.turn) : null;
        if (turnLi) {
          const jump = el("button", "call-btn", "Show its turn");
          jump.type = "button";
          jump.addEventListener("click", () => {
            const turnBtn = turnLi.querySelector(":scope > button");
            if (turnBtn && turnBtn.getAttribute("aria-expanded") === "false") turnBtn.click();
            turnLi.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
            turnLi.classList.remove("flash"); void turnLi.offsetWidth;
            turnLi.classList.add("flash");
            setTimeout(() => turnLi.classList.remove("flash"), 1500);
          });
          body.append(jump);
        }
      };
      head.addEventListener("click", async () => {
        body.hidden = !body.hidden;
        head.setAttribute("aria-expanded", String(!body.hidden));
        if (body.hidden || loaded) return;
        await load();
      });
      li.append(head, body);
      ol.append(li);
    }
  }

  // ------------------------------------------------------------------ log
  const LEVELS = { INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  let minLevel = 20;
  let follow = true;

  function onLog(d, ts) {
    const li = el("li", d.level);
    li.dataset.level = LEVELS[d.level] || 20;
    li.hidden = (LEVELS[d.level] || 20) < minLevel;
    const m = el("span", "m");
    m.append(el("span", "src", (d.logger || "").split(".").pop() + " "), document.createTextNode(d.message));
    li.append(el("span", "t", clock(ts)), m);
    const list = $("log-lines");
    list.append(li);
    while (list.children.length > 300) list.firstChild.remove();
    if (follow) list.scrollTop = list.scrollHeight;
  }

  function setupLog() {
    const list = $("log-lines");
    list.addEventListener("scroll", () => {
      follow = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
      $("follow").hidden = follow;
    });
    $("follow").addEventListener("click", () => {
      follow = true; list.scrollTop = list.scrollHeight; $("follow").hidden = true;
    });
    document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
      document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
      minLevel = LEVELS[chip.dataset.level];
      list.querySelectorAll("li").forEach((li) => { li.hidden = Number(li.dataset.level) < minLevel; });
    }));
  }

  // --------------------------------------------------------------- stream
  function resetAll() {
    turn = null;
    resetNow();
    $("turns").replaceChildren();
    $("feed-empty").hidden = false;
    $("log-lines").replaceChildren();
    $("hits").replaceChildren();
    spark.points = [];
    $("call").hidden = true;
    VoiceClock.clip = null;
    caption.clear();
    setMood("none");
    toolEntries.clear();
    endedTurns.clear();
    feedByTurn.clear();
    subsByTurn.clear();
    subs.clear();
    liveSub = null;
    latestTurn = 0;
    if (halo) halo.toolsClear();
  }

  const HANDLERS = {
    state: (d) => setState(d.state),
    wake: onWake,
    wake_hit: onWakeHit,
    turn_start: startTurn,
    turn_mark: onMark,
    turn_set: onSet,
    turn_tool_start: onToolStart,
    turn_tool: onTool,
    turn_end: onTurnEnd,
    sentence: onSentence,
    call_state: onCallState,
    call_line: onCallLine,
    log: onLog,
    clip_start: onClipStart,
    clip_end: onClipEnd,
    mic: onMic,
    mood: onMood,
    replayed: onReplayed,
  };

  function markAlive() {
    lastEventAt = Date.now();
    if ($("link").dataset.link !== "live") setLink("live", "live");
  }

  function connect() {
    const source = new EventSource("events");
    source.addEventListener("open", () => {
      setLink("live", "live");
      $("banner").hidden = true;
    });
    source.addEventListener("error", () => {
      setLink("connecting", "reconnecting");
      // EventSource retries on its own; a closed one (404: bad token) does not.
      if (source.readyState === EventSource.CLOSED) {
        setLink("down", "offline");
        document.body.classList.remove("loading");
        const b = $("banner");
        b.textContent = "Cannot reach California. Check she is running and that this is the URL from her boot log, token included.";
        b.hidden = false;
        setTimeout(connect, 5000);
      }
    });
    source.addEventListener("ping", markAlive);
    source.addEventListener("hello", (e) => {
      const h = JSON.parse(e.data);
      // A new `started` means California restarted: this page's history is
      // from the old run, so drop it rather than mix two runs.
      if (serverStarted !== null && h.started !== serverStarted) resetAll();
      serverStarted = h.started;
      hello = h;
      // Everything until the server's `replayed` marker is backlog.
      replaying = true;
      skewMs = null;
      setThresholds(h);
      haloAllowed(h);
      lastEventAt = Date.now();
    });
    for (const [kind, fn] of Object.entries(HANDLERS)) {
      source.addEventListener(kind, (e) => {
        markAlive();
        try {
          const d = JSON.parse(e.data);
          noteServerTs(d.ts);
          fn(d, d.ts);
        } catch (err) {
          console.error(kind, err);
        }
      });
    }
  }

  // The server sends something at least every 15s (wake scores ~10 a second
  // while idle, a ping otherwise), so 40s of nothing means the stream is stuck
  // even if the socket looks open. Say so rather than let a frozen page pass
  // for a quiet room.
  setInterval(() => {
    if ($("link").dataset.link === "live" && Date.now() - lastEventAt > 40000) {
      setLink("connecting", "stalled");
    }
    if (spark.points.length) { trimSpark(); drawSpark(); }
    pruneTools();
  }, 1000);

  // Belt and braces for a lost turn_end: nothing in toolEntries lives forever.
  function pruneTools() {
    const now = performance.now();
    for (const e of [...toolEntries.values()]) {
      const age = (now - e.t0) / 1000;
      if ((e.st === "run" && age > 300) || (e.st === "ask" && age > 180) || (e.st === "live" && age > 3600)) retire(e);
    }
  }

  // Read by tools/demo_live_ui.py --check, only with ?debug in the URL.
  if (debug) {
    window.__tools = {
      get entries() { return toolEntries.size; },
      get rows() { return $("tools").children.length; },
    };
    // How many satellite labels poke outside the halo stage.
    window.__labelBounds = () => {
      const stage = $("halo").getBoundingClientRect();
      let out = 0, n = 0;
      for (const tag of document.querySelectorAll("#halo-sats .sat .tag")) {
        const r = tag.getBoundingClientRect();
        if (!r.width || Number(getComputedStyle(tag.parentElement).opacity) < 0.3) continue;
        n++;
        if (r.left < stage.left - 1 || r.right > stage.right + 1 || r.top < stage.top - 1 || r.bottom > stage.bottom + 1) out++;
      }
      return { n, out };
    };
  }

  window.addEventListener("resize", drawSpark);
  setupLog();
  resetNow();
  setupHalo();
  connect();
})();
