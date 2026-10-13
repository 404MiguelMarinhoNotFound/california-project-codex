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
    const failed = { ok: false, setState() {}, setVoice() {}, setMic() {}, setMood() {}, wake() {}, hush() {}, resize() {}, dispose() {} };
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

    // ---------------------------------------------------------- inputs
    const sim = {
      state: "unknown", mood: "none", moodStart: 0,
      voice: 0, voiceTarget: 0, mic: 0, micTarget: 0, micAt: 0,
      wakeFlash: 0, awake: false, awakeAt: 0, hushed: false,
    };
    let clock = 0;          // seconds since start, the frame loop's time base
    let lastActive = 0;     // last time anything moved, for the idle throttle

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
    let frameNo = 0, raf = 0, disposed = false;

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
    let settleTimer = 0;
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
      resize,
      dispose() {
        disposed = true;
        cancelAnimationFrame(raf);
        clearTimeout(settleTimer);
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
      };
    }
    return api;
  };
})();
