// Reactive background: soft colour blobs that drift, follow the cursor,
// speed up while the assistant is thinking, plus a cursor glow and
// orb mascots whose eyes track the pointer.
(() => {
  const blobs = [...document.querySelectorAll(".blob")];
  const glow = document.getElementById("glow");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const cfg = [
    { parallax: 70, amp: 70, speed: 0.00022, phase: 0 },
    { parallax: -50, amp: 90, speed: 0.00017, phase: 2 },
    { parallax: 40, amp: 60, speed: 0.00026, phase: 4 },
  ];

  const target = { x: 0.5, y: 0.5 };
  const smooth = { x: 0.5, y: 0.5 };
  let mode = "idle";
  let energy = 0;
  let t = 0;
  let last = performance.now();
  let lookQueued = false;
  let lastEvent = null;

  function lookAtPointer() {
    lookQueued = false;
    if (!lastEvent) return;
    const { clientX, clientY } = lastEvent;
    document.querySelectorAll(".orb").forEach((orb) => {
      const r = orb.getBoundingClientRect();
      if (!r.width) return;
      const dx = clientX - (r.left + r.width / 2);
      const dy = clientY - (r.top + r.height / 2);
      const d = Math.hypot(dx, dy) || 1;
      const k = Math.min(1, d / 180);
      orb.style.setProperty("--dx", ((dx / d) * k).toFixed(3));
      orb.style.setProperty("--dy", ((dy / d) * k).toFixed(3));
    });
  }

  function frame(now) {
    const dt = Math.min(48, now - last);
    last = now;
    energy += ((mode === "think" ? 1 : 0) - energy) * 0.05;
    smooth.x += (target.x - smooth.x) * 0.05;
    smooth.y += (target.y - smooth.y) * 0.05;
    t += dt * (1 + energy * 3.5);

    blobs.forEach((b, i) => {
      const c = cfg[i % cfg.length];
      const x = (smooth.x - 0.5) * 2 * c.parallax + Math.sin(t * c.speed + c.phase) * c.amp * (1 + energy);
      const y = (smooth.y - 0.5) * 2 * c.parallax + Math.cos(t * c.speed * 1.3 + c.phase) * c.amp * (1 + energy);
      b.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0) scale(${(1 + energy * 0.18).toFixed(3)})`;
    });

    if (!reduceMotion) requestAnimationFrame(frame);
  }

  window.addEventListener("pointermove", (e) => {
    target.x = e.clientX / window.innerWidth;
    target.y = e.clientY / window.innerHeight;
    lastEvent = e;
    if (glow) {
      glow.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
      glow.classList.add("on");
    }
    if (!lookQueued) {
      lookQueued = true;
      requestAnimationFrame(lookAtPointer);
    }
  });

  document.addEventListener("pointerleave", () => glow && glow.classList.remove("on"));

  window.field = { setMode: (m) => (mode = m) };

  requestAnimationFrame(frame);
})();