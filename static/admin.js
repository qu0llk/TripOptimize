// Клиентская логика админ-панели TripOptimizer (Модуль 7C) — чистая ваниль.
// HTTP-поллинг сводки каждые 5 секунд + кнопка очистки устаревшего кэша.

"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;

// Цветовые бейджи статусов в палитре «Mossy Hollow» / семафор.
const STATUS_BADGE = {
  PENDING: "bg-moss-sage text-moss-dark",
  SCRAPING: "bg-moss-soft text-moss-dark",
  OPTIMIZING: "bg-moss-olive text-moss-soft",
  COMPLETED: "bg-green-200 text-green-900",
  FAILED: "bg-red-200 text-red-900",
};

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) {
    return iso;
  }
}

// --- Отрисовка карточек метрик ---
function renderMetrics(m) {
  $("m-total").textContent = m.total_tasks;
  $("m-active").textContent = m.active_tasks;
  $("m-hits").textContent = m.cache_hits;
  $("m-cache").textContent = m.cache_rows;
}

// --- Отрисовка трекера здоровья скраперов ---
const SCRAPER_TITLE = { aviasales: "Aviasales ✈", rzd: "РЖД 🚆" };
function renderScrapers(scrapers) {
  const box = $("scrapers");
  const sources = ["aviasales", "rzd"];
  box.innerHTML = "";
  sources.forEach((src) => {
    const s = scrapers[src];
    const warn = s && s.status === "warn";
    const known = Boolean(s);
    const label = warn ? "⚠ ВНИМАНИЕ / БЛОКИРОВКА" : known ? "✅ В норме" : "— нет данных";
    const cls = warn ? "bg-red-100 border-red-400 text-red-700"
                     : "bg-moss-soft/40 border-moss-sage text-moss-dark";
    const err = warn && s.last_error
      ? `<div class="mt-1 text-xs">${s.last_error} · ${fmtTime(s.last_error_at)}</div>`
      : "";
    const card = document.createElement("div");
    card.className = `border rounded-xl px-4 py-3 ${cls}`;
    card.innerHTML = `<b>${SCRAPER_TITLE[src]}</b> — ${label}${err}`;
    box.appendChild(card);
  });
}

// --- Отрисовка таблицы задач ---
function renderTasks(tasks) {
  const body = $("tasks-body");
  body.innerHTML = "";
  tasks.forEach((t) => {
    const tr = document.createElement("tr");
    tr.className = "border-b border-moss-sage/50";
    const badge = STATUS_BADGE[t.status] || "bg-gray-200 text-gray-800";
    const err = t.status === "FAILED" && t.error_message
      ? t.error_message.slice(0, 60) : "";
    tr.innerHTML = `
      <td class="py-2 pr-3 font-mono text-xs">${t.id.slice(0, 8)}</td>
      <td class="py-2 pr-3">${fmtTime(t.created_at)}</td>
      <td class="py-2 pr-3">
        <span class="px-2 py-1 rounded-lg text-xs font-semibold ${badge}">${t.status}</span>
      </td>
      <td class="py-2 pr-3">${t.origin} → ${t.destination}</td>
      <td class="py-2 pr-3">${t.metric === "time" ? "⏱ Время" : "💰 Деньги"}</td>
      <td class="py-2 text-red-700 text-xs">${err}</td>`;
    body.appendChild(tr);
  });
}

// --- Поллинг сводки ---
async function refresh() {
  try {
    const resp = await fetch("/api/admin/overview");
    if (!resp.ok) return;
    const data = await resp.json();
    renderMetrics(data.metrics);
    renderScrapers(data.scrapers || {});
    renderTasks(data.tasks || []);
  } catch (e) {
    // молча игнорируем сетевые сбои — следующий тик повторит запрос
  }
}

// --- Очистка устаревшего кэша ---
$("purge-btn").addEventListener("click", async () => {
  $("purge-btn").disabled = true;
  $("purge-result").textContent = "Очистка…";
  try {
    const resp = await fetch("/api/admin/cache/purge", { method: "POST" });
    const data = await resp.json();
    $("purge-result").textContent = `Удалено записей: ${data.deleted}`;
    await refresh();
  } catch (e) {
    $("purge-result").textContent = "Ошибка очистки";
  } finally {
    $("purge-btn").disabled = false;
  }
});

document.addEventListener("DOMContentLoaded", () => {
  refresh();
  setInterval(refresh, POLL_MS);
});
