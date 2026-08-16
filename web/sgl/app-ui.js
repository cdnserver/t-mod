/* SGL internal console — motion layer. Vanilla, no deps.
   Progressive enhancement over app.js; never blocks functionality. */
(function () {
  "use strict";
  if (new URLSearchParams(location.search).get("public") === "1") return;
  var app = document.getElementById("app");
  if (!app) return;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var raf = window.requestAnimationFrame;

  function init() {
    if (reduce) return;
    app.classList.add("ui-anim");
    metrics();
    cardStagger();
    refreshSpin();
  }

  /* count-up on the desk metric numbers whenever app.js sets them */
  function metrics() {
    var box = document.querySelector(".metrics");
    if (!box) return;
    var nums = [].slice.call(box.querySelectorAll("strong"));
    var run = function (el) {
      var target = parseInt(el.textContent, 10);
      if (isNaN(target)) return;
      if (el._anim || el.dataset.shown === String(target)) return;
      el.dataset.shown = String(target);
      var art = el.closest("article");
      if (art) art.classList.add("in");
      el._anim = true;
      var dur = 1000, t0 = null;
      var step = function (ts) {
        if (t0 === null) t0 = ts;
        var p = Math.min(1, (ts - t0) / dur);
        var e = 1 - Math.pow(1 - p, 3);
        el.textContent = String(Math.round(target * e));
        if (p < 1) raf(step); else el._anim = false;
      };
      raf(step);
    };
    var sweep = function () { nums.forEach(run); };
    new MutationObserver(sweep).observe(box, { childList: true, characterData: true, subtree: true });
    sweep();
  }

  /* stagger newly rendered case / archive cards */
  function cardStagger() {
    ["recent-cases", "case-list", "archive-list"].forEach(function (id) {
      var c = document.getElementById(id);
      if (!c) return;
      new MutationObserver(function (muts) {
        var i = 0;
        muts.forEach(function (m) {
          [].forEach.call(m.addedNodes, function (n) {
            if (n.nodeType !== 1 || n.classList.contains("empty")) return;
            n.style.animationDelay = (i * 55) + "ms";
            i++;
            n.classList.remove("ui-pop");
            void n.offsetWidth;
            n.classList.add("ui-pop");
          });
        });
      }).observe(c, { childList: true });
    });
  }

  /* refresh button gets a full spin on click */
  function refreshSpin() {
    var r = document.getElementById("refresh");
    if (!r) return;
    r.addEventListener("click", function () {
      r.classList.remove("spin");
      void r.offsetWidth;
      r.classList.add("spin");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
