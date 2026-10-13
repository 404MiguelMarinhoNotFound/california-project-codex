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
    if (h.threshold != null) $("tick-idle").style.left = (h.threshold * 100) + "%";
    const barge = $("tick-barge");
    if (h.barge_in_threshold != null && h.barge_in_threshold !== h.threshold) {
      barge.hidden = false;
      barge.style.left = (h.barge_in_threshold * 100) + "%";
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
    clearInterval(clockTimer);
    const tick = () => { if (turn) $("now-clock").textContent = fmtMs(Date.now() - turn.startedAt); };
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

  function onToolStart(d) {
    if (!turn || turn.id !== d.turn) return;
    const li = el("li", "running");
    li.dataset.key = `${d.name}:${d.start_ms}`;
    li.append(
      el("span", "tool-name", d.action ? `${d.name}.${d.action}` : d.name),
      el("span", "tool-ms", "running"),
    );
    $("tools").append(li);
  }

  function onTool(d) {
    if (!turn || turn.id !== d.turn) return;
    let li = $("tools").querySelector(`[data-key="${CSS.escape(`${d.name}:${d.start_ms}`)}"]`);
    if (!li) {
      li = el("li");
      li.append(el("span", "tool-name", d.action ? `${d.name}.${d.action}` : d.name), el("span", "tool-ms"));
      $("tools").append(li);
    }
    li.classList.remove("running");
    li.querySelector(".tool-ms").textContent = fmtMs(d.ms);
    if (d.result) li.append(el("span", "tool-result", d.result));
  }

  function onTurnEnd(d) {
    addTurnToFeed(d.record || {}, d.ts);
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
      if (reducedMotion) this.render(this.words.length);
    },
    // She was cut off: leave the words she got to, reveal no more.
    freeze() { this.frozen = true; },
    clear() { this.words = []; this.shown = -1; this.clip = null; this.frozen = false; $("halo-caption").replaceChildren(); },
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
    if (halo) halo.setVoice(VoiceClock.level(now));
    caption.tick(now);
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
    halo = window.createHalo($("halo-canvas"), {
      small: window.matchMedia("(max-width: 700px)").matches,
      reducedMotion,
      palette: "dusk",
      debug,
    });
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
  }

  // A server with `ui.halo: false` gets the page exactly as it was before.
  function haloAllowed(h) {
    if (h.halo !== false || !halo) return;
    halo.dispose();
    halo = null;
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

  function addTurnToFeed(r, ts) {
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
      detail.append(el("div", "mono", `${t.name}.${t.action || ""} ${fmtMs(t.ms)}: ${t.result || ""}`));
    }
    if (r.reply) detail.append(el("div", "", r.reply));
    if (r.error) detail.append(el("div", "", r.error));
    btn.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
      btn.setAttribute("aria-expanded", String(!detail.hidden));
    });
    li.append(btn, detail);
    const list = $("turns");
    list.prepend(li);
    while (list.children.length > 200) list.lastChild.remove();
  }

  // ----------------------------------------------------------------- call
  let callTimer = null;
  let callStarted = null;

  function onCallState(d) {
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
      setThresholds(h);
      haloAllowed(h);
      lastEventAt = Date.now();
    });
    for (const [kind, fn] of Object.entries(HANDLERS)) {
      source.addEventListener(kind, (e) => {
        markAlive();
        try {
          const d = JSON.parse(e.data);
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
  }, 1000);

  window.addEventListener("resize", drawSpark);
  setupLog();
  resetNow();
  setupHalo();
  connect();
})();
