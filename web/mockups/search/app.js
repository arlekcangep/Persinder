/* Эскиз поиска: данные демонстрационные, API не вызывается. */

const DOCS = [
  {
    id: "kuku",
    title: "куку1.lic",
    path: "D:\\Obsidian\\куку1.lic",
    area: "personal",
    areaLabel: "Личное",
    chunks: 1,
    sem: 0.8494,
    morph: 14.4987,
    snippet: "жил был медведь и серый волчок ффф",
  },
  {
    id: "lebon",
    title: "Lebon_G_-_Psikhologia_narodov_i_mass_1995.pdf",
    path: "D:\\Library\\Books\\Lebon_G_-_Psikhologia_narodov_i_mass_1995.pdf",
    area: "foreign",
    areaLabel: "Чужое",
    chunks: 544,
    sem: 0.8257,
    morph: 7.7539,
    snippet:
      "Толпа и её вожак: психология масс, коллективное поведение и механизмы подчинения.",
  },
  {
    id: "karamurza",
    title: "С.Г.Кара-Мурза-manipulation.pdf",
    path: "D:\\Library\\Books\\С.Г.Кара-Мурза-manipulation.pdf",
    area: "foreign",
    areaLabel: "Чужое",
    chunks: 2309,
    sem: 0.8185,
    morph: null,
    snippet:
      "Манипуляция сознанием, массы и механизмы влияния: как формируется образ врага.",
  },
  {
    id: "animals",
    title: "Заметки о поведении животных.md",
    path: "D:\\Obsidian\\Personal\\Заметки о поведении животных.md",
    area: "personal",
    areaLabel: "Личное",
    chunks: 12,
    sem: 0.8122,
    morph: 6.1044,
    snippet:
      "Наблюдения за стаей: вожак, волчата и сезонные миграции; серый волчок держится отдельно, медведь — у реки.",
  },
  {
    id: "web-volk",
    title: "web-archive-volk.html",
    path: "D:\\WebArchive\\web-archive-volk.html",
    area: "web",
    areaLabel: "Web",
    chunks: 8,
    sem: 0.8031,
    morph: null,
    snippet:
      "Серый волк в мифологии и фольклоре: от тотемного предка до сказочного антагониста.",
  },
  {
    id: "forest",
    title: "forest-notes.md",
    path: "D:\\Obsidian\\Personal\\forest-notes.md",
    area: "personal",
    areaLabel: "Личное",
    chunks: 4,
    sem: 0.7948,
    morph: 4.2210,
    snippet: "Лесные заметки: следы, повадки, наблюдения у кормушки.",
  },
];

const DEFAULTS = {
  query: "волчок",
  layers: "both",
  minScore: 0.8,
  morphLimit: 20,
  semanticLimit: 20,
};

const state = {
  ...DEFAULTS,
  loading: false,
  error: false,
  requestId: 0,
  inputTimer: 0,
  loadTimer: 0,
};

const els = {
  root: document.documentElement,
  form: document.querySelector("#search-form"),
  query: document.querySelector("#query"),
  clear: document.querySelector("#clear-btn"),
  results: document.querySelector("#results"),
  summaryText: document.querySelector("#summary-text"),
  summaryQuery: document.querySelector("#summary-query"),
  empty: document.querySelector("#empty-state"),
  notFound: document.querySelector("#not-found-state"),
  notFoundText: document.querySelector("#not-found-text"),
  error: document.querySelector("#error-state"),
  retry: document.querySelector("#retry-btn"),
  theme: document.querySelector("#theme-toggle"),
  filterToggle: document.querySelector("#filter-toggle"),
  filterPanel: document.querySelector("#filters-panel"),
  filterCount: document.querySelector("#filter-count"),
  minScore: document.querySelector("#min-score"),
  minScoreValue: document.querySelector("#min-score-value"),
  morphLimit: document.querySelector("#morph-limit"),
  semanticLimit: document.querySelector("#semantic-limit"),
  filterApply: document.querySelector("#filter-apply"),
  filterReset: document.querySelector("#filter-reset"),
  toast: document.querySelector("#toast"),
};

/* ---------- утилиты ---------- */

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch],
  );
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlight(text, query) {
  const safe = escapeHtml(text);
  const needle = query.trim();
  if (!needle) return safe;
  const pattern = new RegExp(`(${escapeRegExp(escapeHtml(needle))})`, "gi");
  return safe.replace(pattern, "<mark>$1</mark>");
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("show");
  }, 2600);
}

/* ---------- данные для текущего состояния ---------- */

function matchesQuery(doc, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return false;
  return [doc.title, doc.path, doc.snippet].some((value) =>
    value.toLowerCase().includes(needle),
  );
}

function semanticDocs() {
  // Семантика в эскизе «расширяет» запрос: если хоть один документ содержит
  // его текстом, показываем все близкие по демо-оценке. Иначе — пусто.
  if (!DOCS.some((doc) => matchesQuery(doc, state.query))) return [];
  return DOCS.filter((doc) => doc.sem >= state.minScore)
    .sort((a, b) => b.sem - a.sem)
    .slice(0, state.semanticLimit);
}

function morphDocs() {
  return DOCS.filter(
    (doc) => doc.morph !== null && matchesQuery(doc, state.query),
  )
    .sort((a, b) => b.morph - a.morph)
    .slice(0, state.morphLimit);
}

function visibleCounts() {
  const sem = state.layers === "morph" ? [] : semanticDocs();
  const morph = state.layers === "semantic" ? [] : morphDocs();
  return { sem, morph, total: sem.length + morph.length };
}

/* ---------- разметка ---------- */

function cardHtml(doc, layer) {
  const isSemantic = layer === "semantic";
  const score = isSemantic ? doc.sem.toFixed(4) : doc.morph.toFixed(4);
  const label = isSemantic ? "cos" : "bm25";
  const areaClass = `area-${escapeHtml(doc.area)}`;
  const chunkWord = plural(doc.chunks, "чанк", "чанка", "чанков");

  return `
    <article class="card" data-doc="${escapeHtml(doc.id)}">
      <div class="card-top">
        <span class="file-icon"><svg class="icon"><use href="#i-file"></use></svg></span>
        <div class="card-title">
          <h3 title="${escapeHtml(doc.title)}">${escapeHtml(doc.title)}</h3>
          <p class="path" title="${escapeHtml(doc.path)}">${escapeHtml(doc.path)}</p>
        </div>
        <span class="score ${layer}" title="${isSemantic ? "косинус" : "bm25"}">
          <span class="score-value">${score}</span>
          <span class="score-label">${label}</span>
        </span>
      </div>
      <p class="snippet">…${highlight(doc.snippet, state.query)}…</p>
      <div class="card-foot">
        <div class="tags">
          <span class="tag ${areaClass}">${escapeHtml(doc.areaLabel)}</span>
          <span class="tag">${doc.chunks} ${chunkWord}</span>
        </div>
        <div class="actions">
          <button class="action-btn" type="button" data-action="open" data-id="${escapeHtml(doc.id)}" title="Открыть файл">
            <svg class="icon"><use href="#i-external"></use></svg>
            <span class="action-text">Открыть</span>
          </button>
          <button class="action-btn icon-only" type="button" data-action="reveal" data-id="${escapeHtml(doc.id)}" title="Показать в папке" aria-label="Показать в папке">
            <svg class="icon"><use href="#i-folder"></use></svg>
          </button>
        </div>
      </div>
    </article>`;
}

function groupHtml(layer, docs) {
  const isSemantic = layer === "semantic";
  const title = isSemantic ? "Семантика" : "Морфология";
  const note = isSemantic ? "косинус · e5-small-int8-v1" : "bm25 · FTS5";

  return `
    <section class="group" data-layer="${layer}">
      <header class="group-head">
        <span class="layer-dot ${layer}"></span>
        <h2 class="group-title">${title}</h2>
        <span class="group-count">${docs.length}</span>
        <span class="group-note">${note}</span>
      </header>
      <div class="cards">${docs.map((doc) => cardHtml(doc, layer)).join("")}</div>
    </section>`;
}

function skeletonHtml() {
  const card = `
    <div class="skeleton-card">
      <div class="sk-row">
        <span class="sk-box"></span>
        <span class="sk-lines">
          <span class="sk-line short"></span>
          <span class="sk-line tiny"></span>
        </span>
      </div>
      <span class="sk-line"></span>
      <span class="sk-line"></span>
    </div>`;

  return `
    <section class="group">
      <header class="group-head">
        <span class="layer-dot semantic"></span>
        <h2 class="group-title">Ищем…</h2>
      </header>
      <div class="cards">${card}${card}${card}</div>
    </section>`;
}

/* ---------- рендер ---------- */

function render() {
  const query = state.query.trim();
  const counts = visibleCounts();

  els.summaryQuery.textContent = query ? `Запрос: «${query}»` : "Запрос не задан";

  if (state.error) {
    els.summaryText.textContent = "Нет связи с сервером";
    els.results.innerHTML = "";
    els.empty.hidden = true;
    els.notFound.hidden = true;
    els.error.hidden = false;
    return;
  }

  els.error.hidden = true;

  if (state.loading) {
    els.summaryText.textContent = "Ищем по библиотеке…";
    els.results.innerHTML = skeletonHtml();
    els.empty.hidden = true;
    els.notFound.hidden = true;
    return;
  }

  if (!query) {
    els.summaryText.textContent = "Введите запрос, чтобы искать по библиотеке";
    els.results.innerHTML = "";
    els.empty.hidden = false;
    els.notFound.hidden = true;
    return;
  }

  els.empty.hidden = true;

  if (counts.total === 0) {
    els.results.innerHTML = "";
    els.notFound.hidden = false;
    els.notFoundText.textContent =
      state.layers === "morph"
        ? "В морфологическом слое совпадений нет — попробуйте включить семантику."
        : "Попробуйте другое слово или снизьте min_score.";
    els.summaryText.textContent = "Ничего не найдено";
    return;
  }

  els.notFound.hidden = true;
  els.summaryText.textContent = `Найдено ${counts.total} · семантика ${counts.sem.length} · морфология ${counts.morph.length}`;

  const blocks = [];
  if (state.layers !== "morph") blocks.push(groupHtml("semantic", counts.sem));
  if (state.layers !== "semantic") blocks.push(groupHtml("morph", counts.morph));
  els.results.innerHTML = blocks.join("");
}

/* ---------- поиск ---------- */

function runSearch() {
  window.clearTimeout(state.loadTimer);
  window.clearTimeout(state.inputTimer);
  const requestId = ++state.requestId;

  state.error = false;
  state.loading = true;
  render();

  state.loadTimer = window.setTimeout(() => {
    if (requestId !== state.requestId) return;
    state.loading = false;
    render();
  }, 320);
}

function scheduleSearch(delay = 260) {
  window.clearTimeout(state.inputTimer);
  window.clearTimeout(state.loadTimer);
  state.requestId += 1;
  state.inputTimer = window.setTimeout(runSearch, delay);
}

/* ---------- фильтры и слои ---------- */

function updateFilterBadge() {
  let active = 0;
  if (Number(els.minScore.value) !== DEFAULTS.minScore) active += 1;
  if (Number(els.morphLimit.value) !== DEFAULTS.morphLimit) active += 1;
  if (Number(els.semanticLimit.value) !== DEFAULTS.semanticLimit) active += 1;
  els.filterCount.textContent = String(active);
  els.filterCount.hidden = active === 0;
}

function applyFilters() {
  state.minScore = Number(els.minScore.value);
  state.morphLimit = Math.max(1, Number(els.morphLimit.value) || DEFAULTS.morphLimit);
  state.semanticLimit = Math.max(
    1,
    Number(els.semanticLimit.value) || DEFAULTS.semanticLimit,
  );
  updateFilterBadge();
  els.filterPanel.hidden = true;
  els.filterToggle.setAttribute("aria-expanded", "false");
  render();
}

function resetFilters() {
  els.minScore.value = String(DEFAULTS.minScore);
  els.morphLimit.value = String(DEFAULTS.morphLimit);
  els.semanticLimit.value = String(DEFAULTS.semanticLimit);
  els.minScoreValue.textContent = DEFAULTS.minScore.toFixed(2);
  applyFilters();
}

function setLayer(layer) {
  state.layers = layer;
  for (const button of document.querySelectorAll(".seg")) {
    const active = button.dataset.layer === layer;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  render();
}

/* ---------- тема ---------- */

function applyTheme(theme) {
  els.root.dataset.theme = theme;
  const use = els.theme.querySelector("use");
  if (use) use.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
  els.theme.setAttribute(
    "aria-label",
    theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему",
  );
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem("persinder-mock-theme");
  } catch {
    stored = null;
  }
  const preferred =
    stored ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(preferred);
}

function toggleTheme() {
  const next = els.root.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem("persinder-mock-theme", next);
  } catch {
    /* file:// или приватный режим — тема просто не запомнится */
  }
}

/* ---------- события ---------- */

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = els.query.value;
  runSearch();
});

els.query.addEventListener("input", () => {
  state.query = els.query.value;
  scheduleSearch();
});

els.clear.addEventListener("click", () => {
  els.query.value = "";
  state.query = "";
  window.clearTimeout(state.inputTimer);
  window.clearTimeout(state.loadTimer);
  state.requestId += 1;
  state.loading = false;
  state.error = false;
  render();
  els.query.focus();
});

els.theme.addEventListener("click", toggleTheme);

els.filterToggle.addEventListener("click", () => {
  const open = els.filterPanel.hidden;
  els.filterPanel.hidden = !open;
  els.filterToggle.setAttribute("aria-expanded", String(open));
});

els.minScore.addEventListener("input", () => {
  els.minScoreValue.textContent = Number(els.minScore.value).toFixed(2);
  updateFilterBadge();
});

els.morphLimit.addEventListener("input", updateFilterBadge);
els.semanticLimit.addEventListener("input", updateFilterBadge);

els.filterApply.addEventListener("click", applyFilters);
els.filterReset.addEventListener("click", resetFilters);

els.retry.addEventListener("click", runSearch);

document.querySelectorAll(".seg").forEach((button) => {
  button.addEventListener("click", () => setLayer(button.dataset.layer));
});

document.querySelectorAll("[data-query]").forEach((button) => {
  button.addEventListener("click", () => {
    els.query.value = button.dataset.query;
    state.query = button.dataset.query;
    runSearch();
    els.query.focus();
  });
});

document.querySelectorAll("[data-view]").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    if (link.dataset.view === "search") {
      els.query.focus();
      return;
    }
    const labels = {
      folders: "Папки",
      models: "Модели",
      settings: "Настройки",
    };
    showToast(`Раздел «${labels[link.dataset.view]}» — следующий шаг.`);
  });
});

document.querySelector(".server-pill").addEventListener("click", (event) => {
  event.preventDefault();
  showToast("Демо: сервер 127.0.0.1:8756 доступен, данные не запрашиваются.");
});

els.results.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;

  const doc = DOCS.find((item) => item.id === button.dataset.id);
  if (!doc) return;

  if (button.dataset.action === "open") {
    showToast(`Открыть «${doc.title}» — в приложении это сделает сервер.`);
    return;
  }

  const folder = doc.path.replace(/[\\/][^\\/]+$/, "");
  showToast(`Проводник: ${folder}`);
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing =
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement;

  if (event.key === "/" && !typing) {
    event.preventDefault();
    els.query.focus();
    els.query.select();
    return;
  }

  if (event.key === "Escape") {
    if (!els.filterPanel.hidden) {
      els.filterPanel.hidden = true;
      els.filterToggle.setAttribute("aria-expanded", "false");
      return;
    }
    if (document.activeElement === els.query && els.query.value) {
      els.query.value = "";
      state.query = "";
      state.loading = false;
      state.requestId += 1;
      render();
    }
  }

  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    state.query = els.query.value;
    runSearch();
  }
});

/* ---------- старт ---------- */

function initFromUrl() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("q")) state.query = params.get("q") || "";
  if (params.get("error") === "1") state.error = true;
  els.query.value = state.query;
}

initTheme();
initFromUrl();
els.minScore.value = String(DEFAULTS.minScore);
els.minScoreValue.textContent = DEFAULTS.minScore.toFixed(2);
updateFilterBadge();
render();
