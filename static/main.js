// Клиентская логика TripOptimizer (Модуль 7B) — чистый ванильный JavaScript.
// Отвечает за: динамическую таблицу городов, валидацию лимита дней, отправку
// задачи в API, отрисовку гибридного прогресса (бар + проценты) через SSE и
// вывод кликабельных карточек билетов с deep-link на покупку.

"use strict";

const MAX_CITIES = 8;   // максимум промежуточных городов
const MAX_DAYS = 31;    // максимум суммарной длительности поездки

// --- Короткие хелперы доступа к DOM ---------------------------------------
const $ = (id) => document.getElementById(id);
const el = (tag, cls) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  return node;
};

// ==========================================================================
// Автодополнение городов
// ==========================================================================
let _acTimer = null;

function setupCityAutocomplete(inputEl) {
  inputEl.setAttribute("autocomplete", "off");
  inputEl.addEventListener("input", () => {
    clearTimeout(_acTimer);
    const q = inputEl.value.trim();
    if (q.length < 2) { _closeDropdowns(); return; }
    _acTimer = setTimeout(() => _fetchSuggestions(inputEl, q), 180);
  });
  inputEl.addEventListener("blur", () => {
    setTimeout(_closeDropdowns, 160);
  });
  inputEl.addEventListener("keydown", (e) => {
    const drop = inputEl._acDrop;
    if (!drop) return;
    const items = [...drop.querySelectorAll("[data-ac-item]")];
    const active = drop.querySelector("[data-ac-active]");
    let idx = items.indexOf(active);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      idx = Math.min(idx + 1, items.length - 1);
      items.forEach((it, i) => i === idx ? it.setAttribute("data-ac-active", "") : it.removeAttribute("data-ac-active"));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      idx = Math.max(idx - 1, 0);
      items.forEach((it, i) => i === idx ? it.setAttribute("data-ac-active", "") : it.removeAttribute("data-ac-active"));
    } else if (e.key === "Enter" && active) {
      e.preventDefault();
      inputEl.value = active.textContent;
      _closeDropdowns();
    } else if (e.key === "Escape") {
      _closeDropdowns();
    }
  });
}

function _closeDropdowns() {
  document.querySelectorAll("[data-ac-drop]").forEach((d) => d.remove());
}

async function _fetchSuggestions(inputEl, q) {
  try {
    const resp = await fetch(`/api/cities?q=${encodeURIComponent(q)}`);
    if (!resp.ok) return;
    const cities = await resp.json();
    _showDropdown(inputEl, cities);
  } catch (_) { /* игнорируем ошибки сети */ }
}

function _showDropdown(inputEl, cities) {
  _closeDropdowns();
  if (!cities.length) return;

  const rect = inputEl.getBoundingClientRect();
  const drop = document.createElement("div");
  drop.setAttribute("data-ac-drop", "");
  drop.style.cssText = `position:fixed;z-index:9999;top:${rect.bottom + window.scrollY}px;`
    + `left:${rect.left + window.scrollX}px;width:${rect.width}px;`
    + `background:#fff;border:1px solid #BAC095;border-radius:8px;`
    + `box-shadow:0 4px 16px rgba(0,0,0,.12);overflow:hidden;`;

  cities.forEach((city) => {
    const item = document.createElement("div");
    item.setAttribute("data-ac-item", "");
    item.textContent = city;
    item.style.cssText = "padding:8px 12px;cursor:pointer;font-size:.9rem;";
    item.addEventListener("mouseenter", () => {
      drop.querySelectorAll("[data-ac-item]").forEach((i) => i.removeAttribute("data-ac-active"));
      item.setAttribute("data-ac-active", "");
    });
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      inputEl.value = city;
      _closeDropdowns();
    });
    drop.appendChild(item);
  });

  // Подсветка активного пункта
  const style = document.createElement("style");
  style.textContent = "[data-ac-item][data-ac-active]{background:#D4DE95;}";
  drop.appendChild(style);

  inputEl._acDrop = drop;
  document.body.appendChild(drop);
}

// ==========================================================================
// Таблица промежуточных городов
// ==========================================================================
function addCityRow() {
  const body = $("cities-body");
  if (body.children.length >= MAX_CITIES) return;

  const row = el("div", "grid grid-cols-[1fr_220px_44px] gap-3 items-center");
  row.innerHTML = `
    <input type="text" class="field-input city-name" placeholder="Город" />
    <input type="number" min="0" value="1" class="field-input city-days" />
    <button type="button" class="remove-city text-red-600 text-xl font-bold">✕</button>`;
  row.querySelector(".remove-city").addEventListener("click", () => {
    row.remove();
    recomputeDays();
    updateAddButton();
  });
  row.querySelector(".city-days").addEventListener("input", recomputeDays);
  setupCityAutocomplete(row.querySelector(".city-name"));
  body.appendChild(row);
  updateAddButton();
  recomputeDays();
}

function updateAddButton() {
  $("add-city").disabled = $("cities-body").children.length >= MAX_CITIES;
  $("add-city").classList.toggle("opacity-50", $("add-city").disabled);
}

// Сумма дней пребывания + запас дней; подсветка предупреждения при превышении.
function recomputeDays() {
  const stay = [...document.querySelectorAll(".city-days")]
    .reduce((sum, i) => sum + (parseInt(i.value, 10) || 0), 0);
  const surplus = parseInt($("surplus_days").value, 10) || 0;
  const total = stay + surplus;

  const exceeded = total > MAX_DAYS;
  $("days-warning").classList.toggle("hidden", !exceeded);
  $("submit-btn").disabled = exceeded;
  $("submit-btn").classList.toggle("opacity-50", exceeded);
  return total;
}

// ==========================================================================
// Сбор полезной нагрузки в формат Pydantic-схемы SearchRequest
// ==========================================================================
function buildPayload() {
  const intermediate = [...$("cities-body").children]
    .map((row) => ({
      city: row.querySelector(".city-name").value.trim(),
      days_to_stay: parseInt(row.querySelector(".city-days").value, 10) || 0,
    }))
    .filter((c) => c.city.length > 0);

  const maxBudgetRaw = $("max_budget").value.trim();
  return {
    origin_city: $("origin_city").value.trim(),
    destination_city: $("destination_city").value.trim(),
    start_date: $("start_date").value,
    surplus_days: parseInt($("surplus_days").value, 10) || 0,
    filters: {
      transport_type: document.querySelector('input[name="transport"]:checked').value,
      require_baggage: $("require_baggage").checked,
      max_budget: maxBudgetRaw ? parseInt(maxBudgetRaw, 10) : null,
      optimization_metric: document.querySelector('input[name="metric"]:checked').value,
    },
    intermediate_cities: intermediate,
  };
}

// ==========================================================================
// Экраны: форма → загрузка → результаты
// ==========================================================================
function showScreen(name) {
  $("form-screen").classList.toggle("hidden", name !== "form");
  $("loading-screen").classList.toggle("hidden", name !== "loading");
  $("results-screen").classList.toggle("hidden", name !== "results");
}

// Рисует N серых скелетонов — ровно столько, сколько ожидается плеч маршрута.
function renderSkeletons(count) {
  const box = $("skeletons");
  box.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const card = el("div", "bg-white rounded-2xl p-5 shadow flex gap-4 items-center");
    card.innerHTML = `
      <div class="skeleton w-16 h-16 rounded-xl"></div>
      <div class="flex-1 space-y-2">
        <div class="skeleton h-4 w-1/3 rounded"></div>
        <div class="skeleton h-4 w-2/3 rounded"></div>
      </div>
      <div class="skeleton h-8 w-24 rounded"></div>`;
    box.appendChild(card);
  }
}

// ==========================================================================
// Форматирование
// ==========================================================================
function fmtTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) {
    return iso;
  }
}
function fmtDuration(min) {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h} ч ${m} мин`;
}
const SOURCE_LABEL = { aviasales: "Aviasales ✈", rzd: "РЖД 🚆" };

// ==========================================================================
// Отрисовка карточек билетов
// ==========================================================================
function renderResults(result) {
  showScreen("results");
  $("error-block").classList.add("hidden");

  const order = (result.order || []).join(" → ");
  $("route-summary").innerHTML = `
    <div class="bg-moss-soft/50 rounded-xl px-4 py-3">
      <b>${order}</b><br/>
      Итого: <b>${result.total_price} ₽</b> ·
      ${fmtDuration(result.total_duration_minutes || 0)} ·
      оптимизация: ${result.optimization_metric === "time" ? "по времени" : "по деньгам"}
    </div>`;
  // Если оптимизатор не смог собрать маршрут — выводим общую причину ОТДЕЛЬНО
  // от карточек плеч, чтобы пользователь сразу видел, что дело не в одной паре.
  const globalBlock = $("global-reason");
  if (result.global_reason) {
    globalBlock.textContent = result.global_reason;
    globalBlock.classList.remove("hidden");
  } else {
    globalBlock.textContent = "";
    globalBlock.classList.add("hidden");
  }

  const box = $("tickets");
  box.innerHTML = "";
  (result.legs || []).forEach((leg, idx) => {
    if (leg && leg.__empty__) {
      box.appendChild(emptyLegCard(idx, result.order, leg.reason));
    } else {
      box.appendChild(leg ? ticketCard(leg) : emptyLegCard(idx, result.order));
    }
  });

  // Дерево перестановок (если оптимизатор его вернул).
  renderTree(result.tree);
}

function ticketCard(t) {
  const url = t.booking_url || "#";
  // Вся карточка — кликабельный deep-link на покупку (Aviasales/РЖД).
  const card = el("a", "ticket-card block bg-white rounded-2xl p-5 shadow cursor-pointer");
  card.href = url;
  card.target = "_blank";
  card.rel = "noopener";
  card.innerHTML = `
    <div class="flex justify-between items-start gap-4">
      <div>
        <div class="text-sm text-moss-olive font-semibold">${SOURCE_LABEL[t.source] || t.source}</div>
        <div class="text-lg font-bold">${t.departure_city} → ${t.arrival_city}</div>
        <div class="text-sm text-moss-dark/80 mt-1">
          ${fmtTime(t.departure_time)} → ${fmtTime(t.arrival_time)}
        </div>
        <div class="text-sm text-moss-dark/70">${fmtDuration(t.duration_minutes)}</div>
        <div class="mt-1 text-sm">
          ${t.has_baggage ? "🧳 Багаж включён" : "Без багажа"}
        </div>
      </div>
      <div class="text-right">
        <div class="text-2xl font-extrabold text-moss-dark">${t.price} ₽</div>
        <div class="mt-2 inline-block bg-moss-olive text-moss-soft px-3 py-1 rounded-lg text-sm">
          Купить →
        </div>
      </div>
    </div>`;
  return card;
}

function emptyLegCard(idx, order, reason) {
  const card = el("div", "bg-white rounded-2xl p-5 shadow text-moss-dark/70");
  const from = order && order[idx] ? order[idx] : "";
  const to = order && order[idx + 1] ? order[idx + 1] : "";
  const title = el("div", "font-semibold text-moss-dark");
  title.textContent = `Билеты не найдены: ${from} → ${to}`;
  card.appendChild(title);
  if (reason) {
    const sub = el("div", "text-sm text-moss-dark/60 mt-1");
    sub.textContent = reason;
    card.appendChild(sub);
  }
  return card;
}

// ==========================================================================
// Дерево перестановок маршрута
// ==========================================================================
// Простая визуализация в виде отступа с цветными «бэйджами» — никаких
// сторонних либ (d3, cytoscape и т.п.) не нужно. Узлы, ведущие к выбранному
// маршруту, подсвечены; неполные пути — пунктиром.
function renderTree(tree) {
  const section = $("tree-section");
  const root = $("tree-root");
  root.innerHTML = "";
  if (!tree || !tree.root) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  const metric = tree.metric || "money";
  const isMoney = metric === "money";

  // Верхняя «легенда»: сколько всего вариантов рассмотрено.
  const totalLine = el("div", "text-xs text-moss-dark/60 mb-2");
  totalLine.textContent = `Всего вариантов: ${tree.total}`;
  root.appendChild(totalLine);

  // Контейнер-дерево. Уровни вложенности даются отступами.
  const list = el("ul", "tree-list");
  list.style.cssText = "list-style:none;padding-left:0;margin:0;";
  list.appendChild(buildTreeNode(tree.root, 0, isMoney, true));
  root.appendChild(list);

  // Кнопка «развернуть/свернуть всё».
  const btn = $("tree-toggle");
  let collapsed = false;
  btn.textContent = "Свернуть все";
  btn.onclick = () => {
    collapsed = !collapsed;
    list.querySelectorAll(".tree-children").forEach((sub) => {
      sub.style.display = collapsed ? "none" : "";
    });
    btn.textContent = collapsed ? "Развернуть все" : "Свернуть все";
  };
}

function buildTreeNode(node, depth, isMoney, isRoot) {
  const li = el("li");
  li.className = "tree-node";

  // Обёртка узла с отступом по уровню.
  const wrap = el("div", "flex items-center gap-2 py-1");
  wrap.style.paddingLeft = `${depth * 22}px`;

  // Линия-коннектор (кроме корня) — вертикальная «рельса» слева от карточки.
  if (!isRoot) {
    const connector = el("span");
    connector.style.cssText =
      "display:inline-block;width:14px;height:1px;background:#BAC095;";
    wrap.appendChild(connector);
  }

  // Бэйдж с названием города.
  const badge = el("span", "px-2 py-1 rounded-md font-semibold text-sm");
  if (node.is_chosen) {
    badge.className += " bg-moss-dark text-moss-soft";
  } else if (!node.is_complete) {
    badge.className += " border border-dashed border-moss-olive/50 text-moss-olive/70";
  } else {
    badge.className += " bg-moss-sage/60 text-moss-dark";
  }
  badge.textContent = node.city;
  wrap.appendChild(badge);

  // Метрика (цена/длительность лучшего потомка).
  if (node.preview) {
    const metricLine = el("span", "text-xs text-moss-dark/70");
    if (isMoney) {
      metricLine.textContent = `${node.preview.total_price} ₽`;
    } else {
      metricLine.textContent = fmtDuration(node.preview.total_duration_minutes);
    }
    if (!node.is_complete) {
      metricLine.textContent += " · неполный";
      metricLine.className += " italic";
    }
    wrap.appendChild(metricLine);
  }

  // Tooltip с полным маршрутом.
  if (node.preview && node.preview.sequence) {
    badge.title = node.preview.sequence.join(" → ");
    if (node.preview) {
      const t = el("span", "text-xs text-moss-olive/70");
      t.textContent = ` (${node.preview.sequence.join(" → ")})`;
      wrap.appendChild(t);
    }
  }

  li.appendChild(wrap);

  // Потомки (если есть) — рекурсивно.
  if (node.children && node.children.length) {
    const sub = el("ul");
    sub.style.cssText = "list-style:none;padding-left:0;margin:0;";
    sub.classList.add("tree-children");
    node.children.forEach((c) => {
      sub.appendChild(buildTreeNode(c, depth + 1, isMoney, false));
    });
    li.appendChild(sub);
  }
  return li;
}

// ==========================================================================
// Подписка на прогресс задачи через Server-Sent Events
// ==========================================================================
function trackProgress(taskId) {
  const source = new EventSource(`/api/tasks/${taskId}/stream`);

  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const pct = data.progress_percentage || 0;

    $("progress-bar").style.width = `${pct}%`;
    $("progress-text").textContent = `Парсинг билетов: ${pct}%`;
    $("progress-status").textContent = data.status;

    if (data.total_legs && $("skeletons").children.length !== data.total_legs) {
      renderSkeletons(data.total_legs);
    }

    if (data.status === "COMPLETED") {
      source.close();
      renderResults(data.result || {});
    } else if (data.status === "FAILED") {
      source.close();
      showScreen("results");
      $("tickets").innerHTML = "";
      $("route-summary").innerHTML = "";
      const err = $("error-block");
      err.classList.remove("hidden");
      err.textContent = "Не удалось подобрать маршрут. Попробуйте ещё раз.";
    }
  };

  source.onerror = () => {
    // Браузер сам переподключается; ничего не делаем, чтобы не плодить ошибки.
  };
}

// ==========================================================================
// Инициализация
// ==========================================================================
document.addEventListener("DOMContentLoaded", () => {
  setupCityAutocomplete($("origin_city"));
  setupCityAutocomplete($("destination_city"));

  addCityRow(); // одна строка по умолчанию
  $("add-city").addEventListener("click", addCityRow);
  $("surplus_days").addEventListener("input", recomputeDays);

  // Поповер фильтров: открытие/закрытие и закрытие по клику снаружи.
  $("filters-toggle").addEventListener("click", (e) => {
    e.stopPropagation();
    $("filters-popover").classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    const pop = $("filters-popover");
    if (!pop.contains(e.target) && e.target !== $("filters-toggle")) {
      pop.classList.add("hidden");
    }
  });

  $("restart-btn").addEventListener("click", () => showScreen("form"));

  // Отправка формы: без перезагрузки страницы.
  $("search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (recomputeDays() > MAX_DAYS) return;

    const payload = buildPayload();
    if (!payload.origin_city || !payload.destination_city || !payload.start_date) {
      return;
    }

    // Ожидаемое число плеч = промежуточные города + 1.
    showScreen("loading");
    renderSkeletons(payload.intermediate_cities.length + 1);
    $("progress-bar").style.width = "0%";
    $("progress-text").textContent = "Парсинг билетов: 0%";

    try {
      const resp = await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const { task_id } = await resp.json();
      trackProgress(task_id);
    } catch (err) {
      showScreen("results");
      const block = $("error-block");
      block.classList.remove("hidden");
      block.textContent = `Ошибка отправки запроса: ${err.message}`;
    }
  });
});
