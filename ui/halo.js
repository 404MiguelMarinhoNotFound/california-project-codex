// California's halo: a ring of embers with a core of light, drawn with three.js
// (ui/vendor/three.min.js, r149, the window.THREE global build).
//
// Inputs only. It never touches the network; ui/app.js turns the event stream
// into these calls. One factory on window, no module: the page has no bundler,
// and a global keeps the CSP and the static whitelist simple.
//
//   const halo = createHalo(canvas, { small, reducedMotion, palette, debug });
//   halo.setState("idle" | "listening" | "thinking" | "speaking" | "unknown")
//   halo.setVoice(0..1)   her voice, already timed by app.js's VoiceClock
//   halo.setMic(0..1)     the mic level; smoothed in here
//   halo.setMood("none" | "happy" | "smug" | "unimpressed" | "sheepish" | "roll")
//   halo.wake()           snap the idle cloud into a ring with a flash
//   halo.hush()           she was cut off: let the voice go at once, not over a release
//   halo.ok               false when WebGL is unavailable: show the pill instead
//
// Tool satellites (needs opts.overlay = { sats: <div> }). The satellites are
// DOM; their tethers are drawn here in WebGL, because an animated full-stage
// SVG cost a full repaint every frame (43 -> 81 ms a frame on swiftshader).
//   halo.toolStart(key, { dev, label, expected, t0 })   bud a satellite off the ring;
//                         t0 is performance.now() at the call's start, expected
//                         the usual ms (0 = no estimate: a sweeping arc)
//   halo.toolEnd(key, "ok" | "done" | "fail" | "ask" | "lost", note, ms)
//                         done: finished with nothing to claim (a status read)
//   halo.toolLive(key, note, t0)  keep it in orbit (a phone call) after the turn;
//                         t0 restarts its clock (the dial)
//   halo.toolPulse(key)        a call line arrived
//   halo.toolRemove(key)       fade it out now; halo.toolsClear() drops them all
//
// Every value here is from the approved draft (Dusk). The halo eases toward
// targets, so nothing ever jumps.
"use strict";

(() => {
  // ------------------------------------------------------------ palette
  // core: the light at the centre; a/b/c: the embers, cool to warm.
  const PALETTES = {
    dusk: { core: "#ffe1f0", a: "#b388ff", b: "#ff6fa8", c: "#ff9a6b" },
  };

  // ------------------------------------------------------------ smoothing
  // Attack/release time constants in seconds, from the draft.
  const VOICE_ATTACK = 0.06, VOICE_RELEASE = 0.2;
  // After a barge-in or "stop" the ring must settle visibly at once: ~0.3s from
  // full voice to still, rather than the ~0.6s the normal release takes.
  const VOICE_HUSH_RELEASE = 0.06;
  const MIC_ATTACK = 0.05, MIC_RELEASE = 0.25;
  const HEAT_ATTACK = 0.4, HEAT_RELEASE = 0.6;
  const PUSH_ATTACK = 0.08, PUSH_RELEASE = 0.3;
  const LOOSE_ATTACK = 0.18, LOOSE_RELEASE = 0.9;
  const MOOD_ATTACK = 0.25, MOOD_RELEASE = 0.4;

  // ------------------------------------------------------------ state shape
  const SPIN = { idle: 0.08, listening: 0.28, thinking: 1.6, speaking: 0.45 };   // rad/s
  const COMET_SPEED = 4.2;          // rad/s, the thinking comet
  const LISTEN_LEAN = 0.22;         // rad toward the viewer
  const LISTEN_PUSH = -0.12, LISTEN_MIC_PUSH = 0.12;
  const THINK_PUSH = -0.05;
  const VOICE_PUSH = 0.22;          // ring push per unit of voice
  const WAKE_POP = 0.12;            // scale pop on wake
  const WAKE_FLASH_DECAY = 1.6;     // per second; ~0.6s from 1 to 0
  const RIPPLE_LEVEL = 0.55, RIPPLE_COOLDOWN = 0.32, RIPPLE_LIFE = 1.5;
  const UNKNOWN_BRIGHTNESS = 0.4;   // a stale page must never look alive
  const WAKE_HOLD_S = 10;           // a wake with no state change lets go after this

  // ------------------------------------------------------------ tool satellites
  // One satellite per tool call, DOM over the canvas, placed by projecting a
  // point on an orbit just outside the tilted ring through the camera.
  const SAT_ORBIT = 4.75;           // ring radius is 3.0-3.9
  const SAT_BUD = 3.4;              // where it starts, on the ring
  const SAT_SLOTS = [-0.62, -2.52, -1.57, 0.32, -3.46, 1.25];   // rad, upper half first
  const SAT_MAX = 6;                // more than this and the oldest finished go
  const SAT_DOCK_S = 0.8, SAT_DOCK_FADE_S = 0.45;
  const SAT_FAIL_HOLD_S = 1.4, SAT_FAIL_FADE_S = 0.8;
  const SAT_LOST_FADE_S = 0.6;
  const SAT_ASK_MAX_S = 120;        // a read-back nobody answered lets go
  const SAT_RUN_MAX_S = 300;        // a call whose end never arrived lets go
  const PROG_C = 2 * Math.PI * 29;  // progress ring circumference (r = 29)
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TINT = { tv: "#ff6fa8", lights: "#ff9a6b", vacuum: "#b388ff", whatsapp: "#8fe3c8", phone: "#ffd08a", web: "#9ec8ff", tool: "#d9d3ea" };
  // Line-art glyphs as element specs: built with createElementNS, never markup.
  const GLYPHS = {
    tv: [["rect", { x: 3, y: 5, width: 18, height: 12, rx: 2 }], ["path", { d: "M8 20h8" }]],
    lights: [["path", { d: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z" }]],
    vacuum: [["circle", { cx: 12, cy: 12, r: 8 }], ["circle", { cx: 12, cy: 12, r: 2.5 }], ["path", { d: "M12 4v3" }]],
    whatsapp: [["path", { d: "M4.5 19.5l1.2-3.4A7.5 7.5 0 1 1 8 18.6z" }], ["path", { d: "M9 11h6M9 13.6h3.5" }]],
    phone: [["path", { d: "M6.5 3.5l3 .5 1.2 3.6-2 1.4a11 11 0 0 0 6.3 6.3l1.4-2 3.6 1.2.5 3a1.5 1.5 0 0 1-1.6 1.5A16 16 0 0 1 3 5.1a1.5 1.5 0 0 1 1.5-1.6z" }]],
    web: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18" }]],
    tool: [["circle", { cx: 12, cy: 12, r: 3 }], ["path", { d: "M12 3v4M12 17v4M3 12h4M17 12h4" }]],
  };
  const MARKS = { ok: [["path", { d: "M2.5 6.4l2.3 2.3 4.7-5" }]], fail: [["path", { d: "M3 3l6 6M9 3l-6 6" }]] };

  function svgEl(tag, attrs, cls) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (cls) n.setAttribute("class", cls);
    return n;
  }
  function svgShape(specs, viewBox, cls) {
    const g = svgEl("svg", { viewBox, "aria-hidden": "true", focusable: "false" }, cls);
    for (const [tag, attrs] of specs) g.append(svgEl(tag, attrs));
    return g;
  }
  window.haloGlyph = (dev, cls) => svgShape(GLYPHS[dev] || GLYPHS.tool, "0 0 24 24", cls);
  window.haloTint = (dev) => TINT[dev] || TINT.tool;
  const fmtSat = (ms) => ms < 1000 ? Math.max(0, Math.round(ms / 100) / 10).toFixed(1) + "s"
    : ms < 60000 ? (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s"
      : Math.floor(ms / 60000) + ":" + String(Math.floor(ms / 1000) % 60).padStart(2, "0");

  // ------------------------------------------------------------ moods
  const MOOD_REST = { tiltZ: 0, droop: 0, scale: 1, spinMul: 1, warm: 0, cool: 0, dim: 0, sparkle: 0, flat: 0 };
  const MOODS = {
    happy: { warm: 1, sparkle: 1, scale: 1.06, spinMul: 1.3 },
    smug: { tiltZ: 0.38, spinMul: 0.45, warm: 0.4 },
    unimpressed: { flat: 1, dim: 0.6, droop: -0.25, spinMul: 0.25 },
    sheepish: { scale: 0.82, cool: 1, dim: 0.35, droop: -0.35, spinMul: 0.5, tiltZ: -0.1 },
  };
  const ROLL_S = 1.3;               // the eye-roll: one full turn about x, eased

  // ------------------------------------------------------------ frame pacing
  const IDLE_THROTTLE_AFTER_S = 10; // then every other frame (~30 fps)
  const QUIET = 0.02;
  const REDUCED_SETTLE_FRAMES = 30;

  const smooth = (cur, target, dt, attack, release) =>
    cur + (target - cur) * (1 - Math.exp(-dt / (target > cur ? attack : release)));

  function radialTexture(THREE) {
    const c = document.createElement("canvas");
    c.width = c.height = 256;
    const x = c.getContext("2d");
    const g = x.createRadialGradient(128, 128, 0, 128, 128, 128);
    g.addColorStop(0, "rgba(255,255,255,1)");
    g.addColorStop(0.18, "rgba(255,255,255,0.75)");
    g.addColorStop(0.45, "rgba(255,255,255,0.18)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    x.fillStyle = g;
    x.fillRect(0, 0, 256, 256);
    return new THREE.CanvasTexture(c);
  }

  window.createHalo = function createHalo(canvas, opts) {
    opts = opts || {};
    const THREE = window.THREE;
    const small = !!opts.small;
    const reduced = !!opts.reducedMotion;
    const P = PALETTES[opts.palette] || PALETTES.dusk;
    const failed = {
      ok: false, setState() {}, setVoice() {}, setMic() {}, setMood() {}, wake() {}, hush() {}, resize() {}, dispose() {},
      toolStart() {}, toolEnd() {}, toolLive() {}, toolPulse() {}, toolRemove() {}, toolsClear() {},
    };
    if (!THREE || !canvas) return failed;

    // ---------------------------------------------------------- 1. renderer
    let renderer;
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    } catch (e) {
      return failed;
    }
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(1.75, window.devicePixelRatio || 1));
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
    camera.position.set(0, 0.1, small ? 12.5 : 10.5);

    const U = {
      uTime: { value: 0 }, uVoice: { value: 0 }, uMic: { value: 0 }, uHeat: { value: 0 },
      uA: { value: new THREE.Color(P.a) }, uB: { value: new THREE.Color(P.b) }, uC: { value: new THREE.Color(P.c) },
      uWarm: { value: 0 }, uCool: { value: 0 }, uDim: { value: 0 }, uSparkle: { value: 0 }, uFlash: { value: 0 },
      uLive: { value: 1 },
    };

    // ---------------------------------------------------------- 3. core and aura
    const glowTex = radialTexture(THREE);
    const coreColor = new THREE.Color(P.core);
    const core = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    const aura = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0.35,
    }));
    scene.add(aura);
    scene.add(core);

    // ---------------------------------------------------------- 2. embers
    // Each ember has a place on the ring and a place in a loose idle cloud.
    const N = small ? 900 : 1800;
    const geo = new THREE.BufferGeometry();
    const A = {
      aAngle: new Float32Array(N), aRad: new Float32Array(N), aSpeed: new Float32Array(N), aY: new Float32Array(N),
      aSize: new Float32Array(N), aPhase: new Float32Array(N), aCloud: new Float32Array(N * 3),
    };
    for (let i = 0; i < N; i++) {
      A.aAngle[i] = Math.random() * Math.PI * 2;
      A.aRad[i] = 3.0 + Math.pow(Math.random(), 2.4) * 0.9;
      A.aSpeed[i] = 0.55 + Math.random() * 0.9;
      A.aY[i] = (Math.random() - 0.5) * 0.2 * (A.aRad[i] - 2.8);
      A.aSize[i] = 0.5 + Math.random() * 1.7;
      A.aPhase[i] = Math.random() * 100;
      const u = Math.random(), v = Math.random(), r = 2.2 + Math.cbrt(Math.random()) * 2.8;
      const th = u * Math.PI * 2, ph = Math.acos(2 * v - 1);
      A.aCloud[i * 3] = r * Math.sin(ph) * Math.cos(th);
      A.aCloud[i * 3 + 1] = r * Math.cos(ph) * 0.75;
      A.aCloud[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
    }
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    for (const [name, arr] of Object.entries(A)) {
      geo.setAttribute(name, new THREE.BufferAttribute(arr, name === "aCloud" ? 3 : 1));
    }
    const UP = Object.assign({}, U, {
      uSpin: { value: 0 }, uPush: { value: 0 }, uLoose: { value: 1 }, uComet: { value: 0 },
      uPx: { value: renderer.getPixelRatio() },
    });
    const ringMat = new THREE.ShaderMaterial({
      uniforms: UP, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      vertexShader: `
        attribute float aAngle, aRad, aSpeed, aY, aSize, aPhase; attribute vec3 aCloud;
        uniform float uTime, uSpin, uPush, uVoice, uPx, uLoose, uComet, uHeat, uSparkle;
        varying float vA; varying float vT; varying float vBoost;
        void main(){
          float ang = aAngle + uSpin * aSpeed;
          float r = aRad * (1.0 + uPush) + sin(uTime * 1.3 + aPhase) * 0.05;
          vec3 ring = vec3(cos(ang) * r, aY * (1.0 + uVoice * 1.4) + sin(uTime * 0.9 + aPhase) * 0.04, sin(ang) * r);
          vec3 drift = vec3(sin(uTime * 0.21 + aPhase), cos(uTime * 0.17 + aPhase * 1.3), sin(uTime * 0.19 + aPhase * 0.7)) * 0.35;
          vec3 p = mix(ring, aCloud + drift, uLoose);
          vec4 mv = modelViewMatrix * vec4(p, 1.0);
          // The comet: a bright head running round the ring while she thinks.
          float rel = fract((ang - uComet) / 6.2831853);
          float comet = uHeat * pow(rel, 10.0) * 1.3;
          float twinkle = uSparkle * step(0.82, fract(sin(aPhase * 12.9898 + floor(uTime * 6.0)) * 43758.5453)) * 1.6;
          vBoost = comet + twinkle;
          gl_PointSize = aSize * (1.0 + uVoice * 1.3 + vBoost * 0.8) * uPx * (40.0 / -mv.z);
          vA = 0.35 + 0.65 * fract(aPhase * 0.37); vT = fract(aPhase * 0.13);
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: `
        uniform float uVoice, uHeat, uWarm, uCool, uDim, uFlash, uLoose, uLive; uniform vec3 uA, uB, uC;
        varying float vA; varying float vT; varying float vBoost;
        void main(){
          float d = length(gl_PointCoord - 0.5); if (d > 0.5) discard;
          float glow = pow(1.0 - 2.0 * d, 2.0);
          vec3 col = vT < 0.5 ? mix(uA, uB, vT * 2.0) : mix(uB, uC, (vT - 0.5) * 2.0);
          col = mix(col, uC, uWarm * 0.6);
          col = mix(col, uA, uCool * 0.7);
          col += vec3(1.0) * vBoost * 0.18;
          float a = glow * vA * (0.5 + 0.6 * uVoice + 0.25 * uHeat + uFlash) * (1.0 - uDim * 0.55) * (1.0 - uLoose * 0.45);
          gl_FragColor = vec4(col, a * (1.0 + vBoost) * uLive);
        }`,
    });
    const embers = new THREE.Points(geo, ringMat);
    const tiltGroup = new THREE.Group();     // mood tilt and the roll live here
    const baseTilt = new THREE.Group();      // the ring seen at an angle
    baseTilt.rotation.x = 0.9;
    baseTilt.add(embers);
    tiltGroup.add(baseTilt);
    scene.add(tiltGroup);

    // ---------------------------------------------------------- 4. ripples
    const ripples = Array.from({ length: 6 }, () => {
      const m = new THREE.Mesh(
        new THREE.RingGeometry(1, 1.012, 160),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      m.userData.age = -1;
      scene.add(m);
      return m;
    });

    // ---------------------------------------------------------- 8. tool satellites
    const overlay = opts.overlay && opts.overlay.sats ? opts.overlay : null;
    // A tether is a faint line plus a row of embers along the same curve that
    // run out to the satellite while it works and back in when it docks.
    const TETHER_N = 28;
    const tetherVert = `
      attribute float aT; uniform float uPx, uSize; varying float vT;
      void main(){ vT = aT; gl_PointSize = uSize * uPx; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
    const tetherFrag = `
      uniform vec3 uColor; uniform float uPhase, uAlpha, uFlow; varying float vT;
      void main(){
        float d = length(gl_PointCoord - 0.5); if (d > 0.5) discard;
        float glow = pow(1.0 - 2.0 * d, 1.6);
        float wave = fract(vT * 5.0 - uPhase);
        float lit = mix(0.35, smoothstep(0.55, 1.0, wave) * 1.3, uFlow);
        gl_FragColor = vec4(uColor, glow * lit * uAlpha * smoothstep(0.0, 0.12, vT));
      }`;
    const ndc = new THREE.Vector3();
    let coreNdcZ = 0;
    const sats = [];
    const v3 = new THREE.Vector3();
    const corePx = { x: 0, y: 0 };
    let toolTimer = 0;
    const nowS = () => performance.now() / 1000;
    // Write a style only when it changed: identical writes still cost a style recalc.
    const put = (x, slot, style, prop, value) => {
      if (x[slot] !== value) { x[slot] = value; style[prop] = value; }
    };

    function freeSlot() {
      const used = new Set(sats.filter((x) => !x.leaving).map((x) => x.slot));
      for (let i = 0; i < SAT_SLOTS.length; i++) if (!used.has(i)) return i;
      return sats.length % SAT_SLOTS.length;
    }

    function dropSat(x) {
      x.el.remove();
      scene.remove(x.line); scene.remove(x.dots);
      x.geo.dispose(); x.line.material.dispose(); x.dots.material.dispose();
      const i = sats.indexOf(x);
      if (i >= 0) sats.splice(i, 1);
    }

    function makeSat(key, o) {
      const dev = GLYPHS[o.dev] ? o.dev : "tool";
      const el = document.createElement("div");
      el.className = "sat";
      el.dataset.st = "run";
      el.dataset.dev = dev;
      el.style.setProperty("--tint", TINT[dev]);
      const bub = document.createElement("div");
      bub.className = "bub";
      const prog = svgEl("svg", { viewBox: "0 0 64 64", "aria-hidden": "true" }, "prog");
      prog.append(svgEl("circle", { cx: 32, cy: 32, r: 29 }, "trk"));
      const expected = Number(o.expected) > 0 ? Number(o.expected) : 0;
      const val = expected
        ? svgEl("circle", { cx: 32, cy: 32, r: 29, "stroke-dasharray": PROG_C.toFixed(2), "stroke-dashoffset": PROG_C.toFixed(2) }, "val")
        : svgEl("circle", { cx: 32, cy: 32, r: 29 }, "spin");
      prog.append(val);
      const mark = document.createElement("span");
      mark.className = "mark";
      const tag = document.createElement("span");
      tag.className = "tag";
      const b = document.createElement("b");
      b.textContent = o.label || "";
      b.hidden = !o.label;
      const t = document.createElement("span");
      t.className = "t";
      tag.append(b, t);
      el.append(bub, prog, svgShape(GLYPHS[dev], "0 0 24 24", "glyph"), mark, tag);
      overlay.sats.append(el);
      const geo = new THREE.BufferGeometry();
      const aT = new Float32Array(TETHER_N);
      for (let i = 0; i < TETHER_N; i++) aT[i] = i / (TETHER_N - 1);
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(TETHER_N * 3), 3));
      geo.setAttribute("aT", new THREE.BufferAttribute(aT, 1));
      const tint = new THREE.Color(TINT[dev]);
      const line = new THREE.Line(geo, new THREE.LineBasicMaterial({
        color: tint, transparent: true, opacity: 0, depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      const dots = new THREE.Points(geo, new THREE.ShaderMaterial({
        uniforms: {
          uColor: { value: tint }, uPhase: { value: 0 }, uAlpha: { value: 0 }, uFlow: { value: 1 },
          uPx: { value: renderer.getPixelRatio() }, uSize: { value: small ? 3.2 : 3.6 },
        },
        vertexShader: tetherVert, fragmentShader: tetherFrag,
        transparent: true, depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      line.frustumCulled = dots.frustumCulled = false;
      line.renderOrder = dots.renderOrder = 2;
      scene.add(line); scene.add(dots);
      const slot = freeSlot();
      return {
        key, dev, el, val, mark, tag, b, t, geo, line, dots, slot, expected, phase: 0, flowDir: 1,
        st: "run", t0: Number(o.t0) || performance.now(), endAt: null, endMs: null, note: "",
        ang: SAT_SLOTS[slot], r: SAT_BUD, y: 0, alpha: reduced ? 1 : 0, shake: 0, pulse: 0,
        sx: 0, sy: 0, leaving: false, born: nowS(), dirty: true, tw: 0, th: 0,
      };
    }

    function ensureToolTimer() {
      // Reduced motion has no frame loop: the timers and states still have to move.
      if (!reduced || toolTimer || disposed) return;
      toolTimer = setInterval(() => {
        if (!sats.length) { clearInterval(toolTimer); toolTimer = 0; }
        layoutTools(1);
        renderer.render(scene, camera);
      }, 500);
    }

    function setMark(x, st) {
      x.mark.replaceChildren();
      if (MARKS[st]) x.mark.append(svgShape(MARKS[st], "0 0 12 12"));
      else if (st === "ask") x.mark.textContent = "?";
    }

    // Place every satellite; called once per frame after the scene has moved.
    function layoutTools(dt) {
      if (!overlay || !sats.length) return null;
      const box = canvas.parentElement || canvas;
      const W = box.clientWidth, H = box.clientHeight;
      if (!W || !H) return null;
      const compact = W < 560;
      overlay.sats.classList.toggle("compact", compact);
      const size = compact ? 50 : 64;
      const k = (tau) => (reduced ? 1 : 1 - Math.exp(-dt / tau));
      // Read label sizes first, and only for labels whose text changed: a
      // read after this frame's style writes would force a layout every frame
      // (measured: 41 -> 68 ms a frame on swiftshader with four satellites).
      for (const x of sats) {
        if (x.dirty || x.compact !== compact) {
          x.tw = x.tag.offsetWidth; x.th = x.tag.offsetHeight;
          x.dirty = false; x.compact = compact;
        }
      }
      scene.updateMatrixWorld();
      v3.set(0, tiltGroup.position.y, 0).project(camera);
      corePx.x = (v3.x + 1) / 2 * W; corePx.y = (1 - v3.y) / 2 * H;
      coreNdcZ = v3.z;
      const now = nowS();
      let hot = null;
      const placed = [];
      for (const x of sats.slice()) {
        const since = x.endAt == null ? 0 : now - x.endAt;
        let rT = SAT_ORBIT, aT = 1, yT = 0, scale = 1;
        if (x.st === "ok" || x.st === "done") {
          const d = Math.min(1, since / SAT_DOCK_S);
          rT = SAT_ORBIT - (SAT_ORBIT - SAT_BUD) * d * d;
          aT = 1 - Math.max(0, (since - (SAT_DOCK_S - 0.3)) / SAT_DOCK_FADE_S);
          scale = 1 - 0.5 * d;
          if (reduced) { rT = SAT_ORBIT; scale = 1; aT = since < 1.2 ? 1 : 0; }
        } else if (x.st === "fail") {
          x.shake = reduced ? 0 : Math.max(0, 1 - since * 2.2);
          yT = reduced ? 0 : -Math.max(0, since - 0.5) * 1.6;
          aT = 1 - Math.max(0, (since - SAT_FAIL_HOLD_S) / SAT_FAIL_FADE_S);
        } else if (x.st === "lost" || x.leaving) {
          aT = 1 - since / SAT_LOST_FADE_S;
        } else if (x.st === "ask" && since > SAT_ASK_MAX_S) {
          x.leaving = true; x.endAt = now;
        } else if (x.st === "run" && now - x.born > SAT_RUN_MAX_S) {
          endSat(x, "lost", "no result");
        }
        if (aT <= 0.01 && x.endAt != null) { dropSat(x); continue; }
        const young = now - x.born < 0.06;
        x.r += ((young ? SAT_BUD : rT) - x.r) * k(0.28);
        x.alpha += (Math.max(0, aT) - x.alpha) * k(0.15);
        x.y += (yT - x.y) * k(0.2);
        x.pulse = Math.max(0, x.pulse - dt * 2.2);
        const sway = reduced ? 0 : Math.sin(now * 0.35 + x.slot * 1.7) * 0.05;
        const ang = SAT_SLOTS[x.slot] + sway;
        x.ang = ang;
        v3.set(Math.cos(ang) * x.r, 0, Math.sin(ang) * x.r);
        baseTilt.localToWorld(v3);
        v3.y += x.y;
        v3.project(camera);
        x.ndcZ = v3.z;
        let px = (v3.x + 1) / 2 * W, py = (1 - v3.y) / 2 * H;
        // Keep it, and its label, inside the stage and clear of the HUD and caption.
        const m = size / 2 + 6;
        px = Math.max(m, Math.min(W - m, px));
        py = Math.max(34 + size / 2, Math.min(H - 104 - size / 2, py));
        placed.push({ x, px, py, scale });
        if (x.st === "run" || x.st === "live") hot = x;
      }
      // Nothing sits on anything else: each satellite and its label is one box,
      // and overlapping boxes are pushed apart along their shallower overlap.
      const pad = 4;
      for (const p of placed) {
        p.w = Math.max(size, p.x.tw) + pad;
        p.h = size + p.x.th + pad;
      }
      for (let pass = 0; pass < 4; pass++) {
        for (let i = 0; i < placed.length; i++) {
          for (let j = i + 1; j < placed.length; j++) {
            const a = placed[i], c = placed[j];
            if (a.x.alpha < 0.05 || c.x.alpha < 0.05) continue;
            // Box centres: the label hangs below the bubble.
            const ay = a.py + (a.h - size) / 2, cy = c.py + (c.h - size) / 2;
            const ox = (a.w + c.w) / 2 - Math.abs(c.px - a.px);
            const oy = (a.h + c.h) / 2 - Math.abs(cy - ay);
            if (ox <= 0 || oy <= 0) continue;
            if (ox < oy) {
              const sgn = c.px >= a.px ? 1 : -1;
              a.px -= sgn * ox / 2; c.px += sgn * ox / 2;
            } else {
              const sgn = cy >= ay ? 1 : -1;
              a.py -= sgn * oy / 2; c.py += sgn * oy / 2;
            }
          }
        }
      }
      for (const { x, px: rawX, py: rawY, scale } of placed) {
        const px = Math.max(size / 2, Math.min(W - size / 2, rawX)) + Math.sin(now * 48) * x.shake * 6;
        const py = Math.max(size / 2, Math.min(H - size / 2, rawY));
        x.sx = px; x.sy = py;
        const tw = x.tw / 2;
        const shift = Math.min(0, W - 6 - (px + tw)) + Math.max(0, 6 - (px - tw));
        put(x, "tagT", x.tag.style, "transform", `translateX(calc(-50% + ${shift.toFixed(0)}px))`);
        put(x, "elT", x.el.style, "transform", `translate(${px.toFixed(1)}px, ${py.toFixed(1)}px) scale(${(scale * (1 + x.pulse * 0.1)).toFixed(3)})`);
        put(x, "elO", x.el.style, "opacity", x.alpha.toFixed(2));
        // The tether: a curve from the core to where the satellite was drawn,
        // in screen space, lifted back into the scene at matching depths.
        const mx = (px + corePx.x) / 2 + (py - corePx.y) * 0.18, my = (py + corePx.y) / 2 - (px - corePx.x) * 0.18;
        const key = `${px.toFixed(1)},${py.toFixed(1)},${corePx.x.toFixed(1)},${corePx.y.toFixed(1)}`;
        if (x.d !== key) {
          x.d = key;
          const pos = x.geo.attributes.position;
          for (let i = 0; i < TETHER_N; i++) {
            const u = i / (TETHER_N - 1), w = 1 - u;
            const sx = w * w * corePx.x + 2 * w * u * mx + u * u * px;
            const sy = w * w * corePx.y + 2 * w * u * my + u * u * py;
            ndc.set(sx / W * 2 - 1, 1 - sy / H * 2, coreNdcZ + (x.ndcZ - coreNdcZ) * u).unproject(camera);
            pos.setXYZ(i, ndc.x, ndc.y, ndc.z);
          }
          pos.needsUpdate = true;
        }
        const docking = x.st === "ok" || x.st === "done";
        const flowing = x.st === "run" || x.st === "live" || docking;
        if (!reduced && flowing) x.phase += dt * (docking ? -2.8 : 1.4);
        x.dots.material.uniforms.uPhase.value = x.phase;
        x.dots.material.uniforms.uFlow.value = flowing ? 1 : 0;
        x.dots.material.uniforms.uAlpha.value = (docking ? 1 : x.st === "ask" ? 0.55 : 0.85) * x.alpha;
        x.line.material.opacity = 0.16 * x.alpha;
        // Elapsed in real time, against what this action usually takes.
        const ms = x.endMs != null ? x.endMs : performance.now() - x.t0;
        if (x.expected && x.st !== "live") {
          const frac = Math.min(1, Math.max(0, ms / x.expected));
          const off = (PROG_C * (1 - frac)).toFixed(1);
          if (x.off !== off) { x.off = off; x.val.setAttribute("stroke-dashoffset", off); }
          x.el.classList.toggle("late", x.st === "run" && ms > x.expected * 1.5);
        }
        const timer = x.st === "run" ? fmtSat(ms) + (x.expected ? " / ~" + fmtSat(x.expected) : "")
          : x.st === "live" ? (x.note ? x.note + " \u00b7 " : "") + fmtSat(ms)
            : x.note;
        if (x.t.textContent !== timer) { x.t.textContent = timer; x.dirty = true; }
      }
      if (hot) {
        UP.uComet.value = hot.ang + 0.25;
        U.uHeat.value = Math.max(U.uHeat.value, reduced ? 0.5 : 0.75);
      }
      return hot;
    }

    function endSat(x, st, note, ms) {
      x.st = st;
      x.el.dataset.st = st;
      x.endAt = nowS();
      x.endMs = Number.isFinite(ms) ? ms : performance.now() - x.t0;
      x.note = note || "";
      setMark(x, st);
      if (st === "ok" && !reduced) sim.wakeFlash = Math.max(sim.wakeFlash, 0.35);
      if (st === "ask") x.endAt = nowS();   // parked: the ask clock starts now
    }

    const find = (key) => sats.find((x) => x.key === key && !x.leaving);

    // ---------------------------------------------------------- inputs
    const sim = {
      state: "unknown", mood: "none", moodStart: 0,
      voice: 0, voiceTarget: 0, mic: 0, micTarget: 0, micAt: 0,
      wakeFlash: 0, awake: false, awakeAt: 0, hushed: false,
    };
    let clock = 0;          // seconds since start, the frame loop's time base
    let lastActive = 0;     // last time anything moved, for the idle throttle

    let settleTimer = 0, disposed = false;

    // ---------------------------------------------------------- 6. drag to turn
    const drag = { on: false, x: 0, y: 0, rx: 0, ry: 0 };
    const onDown = (e) => {
      drag.on = true; drag.x = e.clientX; drag.y = e.clientY;
      try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
      poke();
    };
    const onMove = (e) => {
      if (!drag.on) return;
      drag.ry += (e.clientX - drag.x) * 0.006; drag.rx += (e.clientY - drag.y) * 0.006;
      drag.x = e.clientX; drag.y = e.clientY;
      poke();
    };
    const onUp = () => { drag.on = false; };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onUp);

    function resize() {
      const box = canvas.parentElement || canvas;
      const w = Math.max(1, box.clientWidth), h = Math.max(1, box.clientHeight);
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      poke();
    }
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
    if (observer) observer.observe(canvas.parentElement || canvas);
    resize();

    // ---------------------------------------------------------- 5 + 7. the frame
    const M = Object.assign({}, MOOD_REST);
    let last = performance.now(), t0 = last, spin = 0, comet = 0, rippleCool = 0, lastVoice = 0, roll = 0;
    let frameNo = 0, raf = 0;

    function step(now) {
      const dt = Math.min(0.05, Math.max(0, (now - last) / 1000));
      last = now;
      const t = (now - t0) / 1000;
      clock = t;
      const S = sim.state === "unknown" ? "idle" : sim.state;
      const since = t - sim.moodStart;

      // A mic that stopped reporting (link down) fades rather than freezing.
      if (t - sim.micAt > 0.3) sim.micTarget = 0;
      // A wake whose turn never arrived lets go.
      if (sim.awake && S === "idle" && t - sim.awakeAt > WAKE_HOLD_S) sim.awake = false;

      if (sim.hushed && (sim.voiceTarget > QUIET || sim.voice <= QUIET)) sim.hushed = false;
      sim.voice = smooth(sim.voice, sim.voiceTarget, dt, VOICE_ATTACK, sim.hushed ? VOICE_HUSH_RELEASE : VOICE_RELEASE);
      sim.mic = smooth(sim.mic, sim.micTarget, dt, MIC_ATTACK, MIC_RELEASE);
      U.uHeat.value = smooth(U.uHeat.value, S === "thinking" ? 1 : 0, dt, HEAT_ATTACK, HEAT_RELEASE);
      sim.wakeFlash = Math.max(0, sim.wakeFlash - dt * WAKE_FLASH_DECAY);
      U.uLive.value = smooth(U.uLive.value, sim.state === "unknown" ? UNKNOWN_BRIGHTNESS : 1, dt, 0.3, 0.3);

      // Mood targets.
      const T = Object.assign({}, MOOD_REST, MOODS[sim.mood] || {});
      if (sim.mood === "roll" && since > ROLL_S) Object.assign(T, MOODS.unimpressed);
      for (const k in M) M[k] = smooth(M[k], T[k], dt, MOOD_ATTACK, MOOD_RELEASE);
      if (sim.mood === "roll" && since < ROLL_S) {
        const k = since / ROLL_S;
        roll = (1 - Math.cos(k * Math.PI)) / 2 * Math.PI * 2;
      } else {
        roll = 0;
      }

      // Shape from state. Idle is a loose cloud unless a wake is holding it.
      const loose = S === "idle" && !sim.awake ? 1 : 0;
      UP.uLoose.value = smooth(UP.uLoose.value, loose, dt, LOOSE_ATTACK, LOOSE_RELEASE);
      // Her voice pushes the ring in any state: the acknowledgement and the
      // bootup line both play while the published state is still idle.
      let push = sim.voice * VOICE_PUSH;
      if (S === "listening") push = LISTEN_PUSH - sim.mic * LISTEN_MIC_PUSH + sim.voice * VOICE_PUSH;
      else if (S === "thinking") push = THINK_PUSH + sim.voice * VOICE_PUSH;
      UP.uPush.value = smooth(UP.uPush.value, push, dt, PUSH_ATTACK, PUSH_RELEASE);
      spin += dt * ((SPIN[S] || SPIN.idle) + sim.voice * 0.6) * M.spinMul;
      UP.uSpin.value = spin;
      comet += dt * COMET_SPEED;
      UP.uComet.value = comet;
      U.uTime.value = t; U.uVoice.value = sim.voice; U.uMic.value = sim.mic;
      U.uWarm.value = M.warm; U.uCool.value = M.cool; U.uDim.value = M.dim; U.uSparkle.value = M.sparkle;
      U.uFlash.value = sim.wakeFlash * 1.5;

      // Listening leans toward you; moods tilt, droop, flatten and roll.
      const lean = S === "listening" ? LISTEN_LEAN : 0;
      tiltGroup.rotation.set(-lean + drag.rx * 0.6 + roll, drag.ry * 0.6, M.tiltZ);
      baseTilt.rotation.x = 0.9 - M.flat * 0.5;
      tiltGroup.position.y = M.droop + Math.sin(t * 0.6) * 0.04;
      tiltGroup.scale.setScalar(M.scale * (1 + sim.wakeFlash * WAKE_POP));
      if (!drag.on) { drag.rx *= 0.94; drag.ry *= 0.94; }

      // Core: breathes when idle, flares with her voice and on wake, dims with mood.
      const idle = S === "idle" && !sim.awake;
      const idleBreath = idle ? 0.12 * (0.5 + 0.5 * Math.sin(t * 1.1)) : 0;
      const c = (0.55 + sim.voice * 0.9 + sim.mic * 0.3 + U.uHeat.value * 0.2 + sim.wakeFlash + idleBreath)
        * (1 - M.dim * 0.5) * (idle ? 0.55 : 1) * U.uLive.value;
      core.material.color.copy(coreColor);
      core.material.opacity = Math.min(1, 0.35 + c * 0.6) * U.uLive.value;
      core.scale.setScalar((1.6 + sim.voice * 1.1 + sim.wakeFlash * 0.8) * M.scale);
      aura.material.color.copy(U.uB.value);
      aura.material.opacity = Math.min(0.6, 0.12 + c * 0.25) * U.uLive.value;
      aura.scale.setScalar((5.5 + sim.voice * 2.2) * M.scale);
      core.position.y = aura.position.y = tiltGroup.position.y;

      // Ripples on rising syllables.
      rippleCool -= dt;
      if (sim.voice > RIPPLE_LEVEL && lastVoice <= RIPPLE_LEVEL && rippleCool <= 0) {
        const r = ripples.find((x) => x.userData.age < 0) || ripples[0];
        r.userData.age = 0;
        rippleCool = RIPPLE_COOLDOWN;
      }
      lastVoice = sim.voice;
      for (const r of ripples) {
        if (r.userData.age < 0) continue;
        r.userData.age += dt;
        const k = r.userData.age / RIPPLE_LIFE;
        if (k >= 1) { r.userData.age = -1; r.material.opacity = 0; continue; }
        const sc = 1.6 + k * 3.4;
        r.scale.set(sc, sc, 1);
        r.position.y = tiltGroup.position.y;
        r.material.color.copy(U.uB.value);
        r.material.opacity = 0.4 * (1 - k) ** 2 * U.uLive.value;
        r.quaternion.copy(camera.quaternion);
      }

      if (layoutTools(dt) || sats.length) lastActive = t;   // a satellite keeps the full rate
      if (sim.voice > QUIET || sim.mic > QUIET || S !== "idle" || sim.awake || sim.wakeFlash > 0 || drag.on) {
        lastActive = t;
      }
    }

    function frame(now) {
      if (disposed) return;
      raf = requestAnimationFrame(frame);
      frameNo++;
      const quiet = clock - lastActive > IDLE_THROTTLE_AFTER_S;
      if (quiet && frameNo % 2 === 1) return;   // ~30 fps while nothing moves
      step(now);
      renderer.render(scene, camera);
    }

    // Reduced motion: no loop. Each change renders a settled picture once.
    // (settleTimer is declared with the other frame state, above resize():
    // resize() pokes, and under reduced motion a poke settles.)
    function settle() {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        if (disposed) return;
        for (let i = 0; i < REDUCED_SETTLE_FRAMES; i++) {
          last = performance.now() - 50;
          step(performance.now());
        }
        renderer.render(scene, camera);
      }, 16);
    }

    function poke() {
      lastActive = clock;
      if (reduced) settle();
    }

    if (reduced) settle();
    else raf = requestAnimationFrame(frame);

    const api = {
      get ok() { return true; },
      setState(state) {
        const next = state || "unknown";
        if (next === sim.state) return;
        // Back to idle after a turn: the wake hold is over.
        if (next === "idle" && sim.state !== "idle" && sim.state !== "unknown") sim.awake = false;
        if (next !== "idle" && next !== "unknown") {
          // Leaving idle with no wake before it (a phone report's turn) still arrives.
          if (!sim.awake) sim.wakeFlash = 1;
          sim.awake = true;
          sim.awakeAt = clock;
        }
        sim.state = next;
        poke();
      },
      setVoice(level) {
        const v = Math.max(0, Math.min(1, Number(level) || 0));
        sim.voiceTarget = v;
        if (v > QUIET) lastActive = clock;   // called every frame: only real sound wakes the rate
      },
      hush() {
        sim.voiceTarget = 0;
        sim.hushed = true;
        poke();
      },
      setMic(level) {
        sim.micTarget = Math.max(0, Math.min(1, Number(level) || 0));
        sim.micAt = clock;
        if (sim.micTarget > QUIET) lastActive = clock;
      },
      setMood(mood) {
        const next = mood || "none";
        if (next === sim.mood && next !== "roll") return;
        sim.mood = next;
        sim.moodStart = clock;
        poke();
      },
      wake() {
        // Idempotent: a second wake (the hit, then the ack clip) only refreshes the hold.
        sim.awakeAt = clock;
        if (sim.awake && (UP.uLoose.value < 0.5 || sim.wakeFlash > 0.5)) return;
        sim.awake = true;
        sim.wakeFlash = 1;
        poke();
      },
      toolStart(key, o) {
        if (!overlay || !key) return;
        const existing = find(key);
        if (existing) {   // a repeat start updates the label (web search learns its query late)
          if (o && o.label) { existing.b.textContent = o.label; existing.b.hidden = false; existing.dirty = true; }
          return;
        }
        // Crowded: the oldest finished satellite gives way, then the oldest parked one.
        while (sats.filter((x) => !x.leaving).length >= SAT_MAX) {
          const victim = sats.find((x) => x.endAt != null && x.st !== "live") || sats.find((x) => x.st !== "live") || sats[0];
          dropSat(victim);
        }
        sats.push(makeSat(key, o || {}));
        poke();
        ensureToolTimer();
      },
      toolEnd(key, st, note, ms) {
        const x = find(key);
        if (!x || (x.endAt != null && x.st !== "live")) return;
        endSat(x, st, note, ms);
        poke();
      },
      toolLive(key, note, t0) {
        const x = find(key);
        if (!x) return;
        if (Number.isFinite(t0)) x.t0 = t0;   // the call's clock starts at the dial
        x.st = "live";
        x.el.dataset.st = "live";
        x.endAt = null;
        x.endMs = null;
        x.note = note || "";
        x.mark.replaceChildren();
        poke();
      },
      toolPulse(key) { const x = find(key); if (x) { x.pulse = 1; poke(); } },
      toolRemove(key) {
        const x = find(key);
        if (!x) return;
        x.leaving = true;
        if (x.endAt == null || x.st === "ask") x.endAt = nowS();
        if (x.st === "run" || x.st === "live" || x.st === "ask") { x.st = "lost"; x.el.dataset.st = "lost"; }
        poke();
      },
      toolsClear() { for (const x of sats.slice()) dropSat(x); },
      resize,
      dispose() {
        disposed = true;
        cancelAnimationFrame(raf);
        clearTimeout(settleTimer);
        clearInterval(toolTimer);
        for (const x of sats.slice()) dropSat(x);
        if (observer) observer.disconnect();
        canvas.removeEventListener("pointerdown", onDown);
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointercancel", onUp);
        geo.dispose(); ringMat.dispose(); glowTex.dispose();
        core.material.dispose(); aura.material.dispose();
        for (const r of ripples) { r.geometry.dispose(); r.material.dispose(); }
        renderer.dispose();
      },
    };

    // Read by tools/demo_live_ui.py --check, only with ?debug in the URL.
    if (opts.debug) {
      window.__halo = {
        get voice() { return sim.voice; },
        get state() { return sim.state; },
        get mood() { return sim.mood; },
        get loose() { return UP.uLoose.value; },
        get awake() { return sim.awake; },
        get sats() { return sats.map((x) => ({ key: x.key, st: x.st, dev: x.dev, x: x.sx, y: x.sy, alpha: x.alpha, label: x.b.textContent, note: x.t.textContent })); },
        get satCount() { return sats.length; },
        get tethers() { return scene.children.filter((c) => c.isLine || (c.isPoints && c !== embers)).length; },
      };
    }
    return api;
  };
})();
