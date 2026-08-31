/* SGL public site — motion layer. Vanilla, no deps. Progressive enhancement:
   nothing here is required to read the page; it only adds motion. */
(function () {
  "use strict";
  var isPublic = new URLSearchParams(location.search).get("public") === "1";
  var root = document.getElementById("public-site");
  if (!isPublic || !root) return;

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var raf = window.requestAnimationFrame;

  function init() {
    mobileNav(); // navigation must work regardless of motion preference
    if (reduce) return; // leave everything else static & visible
    root.classList.add("sx-anim");

    prepTitle();
    revealHero();
    observeReveals();
    counters();
    progressBar();
    navPin();
    marquee();
    magnetic();
    cardGlow();
    spotlight();
    processLine();
  }

  /* burger menu — toggle the fullscreen overlay */
  function mobileNav() {
    var burger = root.querySelector(".sx-burger");
    var menu = root.querySelector("#sx-mobile-nav");
    if (!burger || !menu) return;
    var set = function (open) {
      menu.classList.toggle("open", open);
      menu.setAttribute("aria-hidden", open ? "false" : "true");
      burger.setAttribute("aria-expanded", open ? "true" : "false");
      burger.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
      document.body.style.overflow = open ? "hidden" : "";
    };
    burger.addEventListener("click", function () {
      set(burger.getAttribute("aria-expanded") !== "true");
    });
    menu.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () { set(false); });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.classList.contains("open")) set(false);
    });
  }

  /* split hero title words for staggered reveal */
  function prepTitle() {
    var words = root.querySelectorAll(".sx-title .w");
    words.forEach(function (w, i) { w.style.setProperty("--wi", i); });
  }
  function revealHero() {
    raf(function () {
      raf(function () {
        var t = root.querySelector(".sx-title");
        if (t) t.classList.add("in");
        // reveal the hero-side items immediately (they're above the fold)
        root.querySelectorAll(".sx-hero [data-reveal]").forEach(function (n, i) {
          n.style.setProperty("--d", 220 + i * 90);
          n.classList.add("in");
        });
      });
    });
  }

  /* generic scroll reveals */
  function observeReveals() {
    var items = [].slice.call(root.querySelectorAll("[data-reveal]"))
      .filter(function (n) { return !n.closest(".sx-hero"); });
    if (!("IntersectionObserver" in window)) {
      items.forEach(function (n) { n.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var group = e.target.parentElement
          ? [].slice.call(e.target.parentElement.querySelectorAll(":scope > [data-reveal]"))
          : [e.target];
        var idx = Math.max(0, group.indexOf(e.target));
        e.target.style.setProperty("--d", idx * 90);
        e.target.classList.add("in");
        io.unobserve(e.target);
      });
    }, { threshold: 0.16, rootMargin: "0px 0px -8% 0px" });
    items.forEach(function (n) { io.observe(n); });
  }

  /* count-up numbers in the hero stat row */
  function counters() {
    var nums = root.querySelectorAll("[data-count]");
    var fire = function (el) {
      var target = parseFloat(el.getAttribute("data-count")) || 0;
      var suffix = el.getAttribute("data-suffix") || "";
      var dur = 1300, start = 0;
      var t0 = null;
      var step = function (ts) {
        if (t0 === null) t0 = ts;
        var p = Math.min(1, (ts - t0) / dur);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(start + (target - start) * eased) + suffix;
        if (p < 1) raf(step);
      };
      raf(step);
    };
    if (!("IntersectionObserver" in window)) { nums.forEach(fire); return; }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { fire(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.6 });
    nums.forEach(function (n) { io.observe(n); });
  }

  /* top scroll-progress bar */
  function progressBar() {
    var bar = root.querySelector(".sx-progress i");
    if (!bar) return;
    var tick = function () {
      var h = document.documentElement;
      var max = h.scrollHeight - h.clientHeight;
      bar.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + "%";
    };
    document.addEventListener("scroll", function () { raf(tick); }, { passive: true });
    tick();
  }

  /* nav gets a border once scrolled */
  function navPin() {
    var nav = root.querySelector(".sx-nav");
    if (!nav) return;
    var tick = function () { nav.classList.toggle("pinned", window.scrollY > 24); };
    document.addEventListener("scroll", function () { raf(tick); }, { passive: true });
    tick();
  }

  /* duplicate marquee content so the loop is seamless */
  function marquee() {
    var row = root.querySelector(".sx-marquee-row");
    if (!row) return;
    row.innerHTML = row.innerHTML + row.innerHTML;
  }

  /* magnetic pull on gold buttons */
  function magnetic() {
    if (window.matchMedia("(pointer: coarse)").matches) return;
    root.querySelectorAll("[data-magnetic]").forEach(function (btn) {
      btn.addEventListener("pointermove", function (e) {
        var r = btn.getBoundingClientRect();
        var x = (e.clientX - r.left - r.width / 2) * 0.28;
        var y = (e.clientY - r.top - r.height / 2) * 0.4;
        btn.style.transform = "translate(" + x + "px," + (y - 2) + "px)";
      });
      btn.addEventListener("pointerleave", function () { btn.style.transform = ""; });
    });
  }

  /* pointer-follow glow inside service cards */
  function cardGlow() {
    if (window.matchMedia("(pointer: coarse)").matches) return;
    root.querySelectorAll(".sx-services article").forEach(function (card) {
      var mx = 0, my = 0, queued = false;
      var apply = function () {
        queued = false;
        card.style.setProperty("--mx", mx + "px");
        card.style.setProperty("--my", my + "px");
      };
      card.addEventListener("pointermove", function (e) {
        var r = card.getBoundingClientRect();
        mx = e.clientX - r.left; my = e.clientY - r.top;
        if (!queued) { queued = true; raf(apply); }
      }, { passive: true });
    });
  }

  /* soft spotlight following the cursor */
  function spotlight() {
    if (window.matchMedia("(pointer: coarse)").matches) return;
    var spot = document.createElement("div");
    spot.className = "sx-spot";
    root.appendChild(spot);
    var x = 0, y = 0, shown = false, queued = false;
    var apply = function () {
      queued = false;
      spot.style.transform = "translate(" + x + "px," + y + "px)";
      if (!shown) { shown = true; spot.style.opacity = "1"; }
    };
    document.addEventListener("pointermove", function (e) {
      x = e.clientX; y = e.clientY;
      if (!queued) { queued = true; raf(apply); }
    }, { passive: true });
  }

  /* draw the process connector line when it scrolls into view */
  function processLine() {
    var proc = root.querySelector(".sx-process");
    if (!proc || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) { proc.style.setProperty("--line-p", "1"); io.unobserve(proc); }
      });
    }, { threshold: 0.3 });
    io.observe(proc);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
