(() => {
  "use strict";

  const two = (value) => String(value).padStart(2, "0");
  const time = (date, zone) => {
    const parts = new Intl.DateTimeFormat("ru-RU", {
      timeZone: zone,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).formatToParts(date);
    const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${two(value.hour)}:${two(value.minute)}:${two(value.second)}`;
  };

  const updateTime = () => {
    const now = new Date();
    const moscow = document.querySelector("#moscow-time");
    const local = document.querySelector("#local-time");
    if (moscow) moscow.textContent = time(now, "Europe/Moscow");
    if (local) local.textContent = time(now, Intl.DateTimeFormat().resolvedOptions().timeZone);
  };

  const updateStatus = async () => {
    const status = document.querySelector("#system-status");
    if (!status) return;
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (!response.ok) throw new Error("unavailable");
      status.classList.add("online");
      status.lastChild.textContent = " Системы доступны";
    } catch (_) {
      status.classList.remove("online");
      status.lastChild.textContent = " Проверяем связь";
    }
  };

  const reveal = () => {
    const items = [...document.querySelectorAll(".reveal")];
    if (!("IntersectionObserver" in window)) {
      items.forEach((item) => item.classList.add("visible"));
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("visible");
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8%", threshold: 0.08 });
    items.forEach((item) => observer.observe(item));
  };

  document.querySelectorAll("[data-current-year]").forEach((item) => {
    item.textContent = String(new Date().getFullYear());
  });
  updateTime();
  window.setInterval(updateTime, 1000);
  updateStatus();
  window.setInterval(updateStatus, 60000);
  reveal();
})();
