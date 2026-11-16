/* Эскиз поиска: данные демонстрационные, API не вызывается. */

const DOCS = [
  {
    id: "kuku",
    title: "куку1.lic",
    path: "D:\\Obsidian\\куку1.lic",
    area: "Личное",
    chunks: 1,
    sem: 0.8494,
    morph: 14.4987,
    snippet: "жил был медведь и серый волчок ффф",
  },
  {
    id: "lebon",
    title: "Lebon_G_-_Psikhologia_narodov_i_mass_1995.pdf",
    path: "D:\\Library\\Books\\Lebon_G_-_Psikhologia_narodov_i_mass_1995.pdf",
    area: "Чужое",
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
    area: "Чужое",
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
    area: "Личное",
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
    area: "Web",
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
    area: "Личное",
    chunks: 4,
    sem: 0.7948,
    morph: 4.221,
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

const STATUS = {
  address: "127.0.0.1:8756",
  vectors: 6829,
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
  logo: document.querySelector("#logo"),
  form: document.querySelector("#search-form"),
  query: document.querySelector("#query"),
  clear: document.querySelector("#clear-btn"),
  results: document.querySelector("#results"),
  summary: document.querySelector("#summary"),
  countAll: document.querySelector("#count-all"),
  countSemantic: document.querySelector("#count-semantic"),
  countMorph: document.querySelector("#count-morph"),
  empty: document.querySelector("#empty-state"),
  notFound: document.querySelector("#not-found-state"),
  notFoundText: document.querySelector("#not-found-text"),
  error: document.querySelector("#error-state"),
  retry: document.querySelector("#retry-btn"),
  searchView: document.querySelector("#search-view"),
  settingsView: document.querySelector("#settings-view"),
  settingsBtn: document.querySelector("#settings-btn"),
  settingsBack: document.querySelector("#settings-back"),
  settingsTabs: document.querySelector("#settings-tabs"),
  statusBtn: document.querySelector("#status-btn"),
  statusTip: document.querySelector("#status-tip"),
  themeBtn: document.querySelector("#theme-btn"),
  statsModal: document.querySelector("#stats-modal"),
  filterToggle: document.querySelector("#filter-toggle"),
  filters: document.querySelector("#filters"),
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
  return safe.replace(pattern, "<b>$1</b>");
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function splitPath(path) {
  const index = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  if (index < 0) return { dir: "", file: path };
  return { dir: path.slice(0, index + 1), file: path.slice(index + 1) };
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.classList.remove("show");
  }, 2600);
}

/* ---------- тема ---------- */

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.content = dark ? "dark" : "light";
  const label = dark ? "Светлая тема" : "Тёмная тема";
  els.themeBtn.setAttribute("aria-label", label);
  els.themeBtn.title = label;
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
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem("persinder-mock-theme", next);
  } catch {
    /* файл открыт напрямую в приватном режиме — тема просто не запомнится */
  }
}

/* ---------- демо-данные под текущее состояние ---------- */

function matchesQuery(doc, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return false;
  return [doc.title, doc.path, doc.snippet].some((value) =>
    value.toLowerCase().includes(needle),
  );
}

function semanticDocs() {
  // Семантика в эскизе «расширяет» запрос: если запрос встречается в демо-данных,
  // показываем документы, проходящие порог близости. Иначе — пусто.
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

function resultHtml(doc, layer) {
  const isSemantic = layer === "semantic";
  const score = isSemantic ? doc.sem.toFixed(4) : doc.morph.toFixed(4);
  const scoreLabel = isSemantic ? "cos" : "bm25";
  const { dir, file } = splitPath(doc.path);
  const chunkWord = plural(doc.chunks, "чанк", "чанка", "чанков");
  const id = escapeHtml(doc.id);

  const pathHtml = dir
    ? `${escapeHtml(dir)}<span class="sep">›</span>${escapeHtml(file)}`
    : escapeHtml(file);

  const snippet = doc.snippet.trim();
  const snippetTail = /[.!?…»"”]$/.test(snippet) ? "" : "…";

  return `
    <article class="result" data-doc="${id}">
      <h3 class="result-title">
        <a href="#" data-action="open" data-id="${id}">${escapeHtml(doc.title)}</a>
      </h3>
      <p class="result-path">
        ${pathHtml}<span class="sep">·</span><span class="area">${escapeHtml(doc.area)}</span>
      </p>
      <p class="result-snippet">…${highlight(snippet, state.query)}${snippetTail}</p>
      <div class="result-meta">
        <span class="score">${scoreLabel} ${score}</span>
        <span>${doc.chunks} ${chunkWord}</span>
        <span class="result-actions">
          <button class="link-btn" type="button" data-action="open" data-id="${id}">Открыть</button>
          <button class="link-btn muted" type="button" data-action="reveal" data-id="${id}">Показать в папке</button>
        </span>
      </div>
    </article>`;
}

function groupHtml(layer, docs) {
  const isSemantic = layer === "semantic";
  const title = isSemantic ? "Семантика" : "Морфология";

  return `
    <section class="result-group" data-layer="${layer}">
      <h2 class="group-title">${title} <span class="group-count">${docs.length}</span></h2>
      ${docs.map((doc) => resultHtml(doc, layer)).join("")}
    </section>`;
}

function skeletonHtml() {
  const block = `
    <div class="skeleton">
      <div class="sk-line title"></div>
      <div class="sk-line path"></div>
      <div class="sk-line full"></div>
      <div class="sk-line short"></div>
    </div>`;

  return `${block}${block}${block}`;
}

/* ---------- рендер ---------- */

function render() {
  const query = state.query.trim();
  const counts = visibleCounts();

  els.countSemantic.textContent = String(semanticDocs().length);
  els.countMorph.textContent = String(morphDocs().length);
  els.countAll.textContent = String(
    semanticDocs().length + morphDocs().length,
  );

  if (state.error) {
    els.summary.textContent = "Нет связи с сервером";
    els.results.innerHTML = "";
    els.empty.hidden = true;
    els.notFound.hidden = true;
    els.error.hidden = false;
    return;
  }

  els.error.hidden = true;

  if (state.loading) {
    els.summary.textContent = "Ищем…";
    els.results.innerHTML = skeletonHtml();
    els.empty.hidden = true;
    els.notFound.hidden = true;
    return;
  }

  if (!query) {
    els.summary.textContent = "Морфология и семантика по вашей библиотеке";
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
    els.summary.textContent = "Ничего не найдено";
    return;
  }

  els.notFound.hidden = true;

  const parts = [`Результатов: ${counts.total}`];
  if (state.layers !== "morph") parts.push(`семантика ${counts.sem.length}`);
  if (state.layers !== "semantic") parts.push(`морфология ${counts.morph.length}`);
  els.summary.textContent = parts.join(" · ");

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

function setLayer(layer) {
  state.layers = layer;
  for (const tab of document.querySelectorAll("#search-view .tab[data-layer]")) {
    const active = tab.dataset.layer === layer;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-pressed", String(active));
  }
  render();
}

function applyFilters() {
  state.minScore = Number(els.minScore.value);
  state.morphLimit = Math.max(
    1,
    Number(els.morphLimit.value) || DEFAULTS.morphLimit,
  );
  state.semanticLimit = Math.max(
    1,
    Number(els.semanticLimit.value) || DEFAULTS.semanticLimit,
  );
  els.filters.hidden = true;
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

/* ---------- виды ---------- */

let lastFocus = null;

function showView(name) {
  const isSettings = name === "settings";
  els.searchView.hidden = isSettings;
  els.settingsView.hidden = !isSettings;
  els.settingsBtn.setAttribute("aria-pressed", String(isSettings));
  window.scrollTo(0, 0);
  if (isSettings) {
    els.settingsBack.focus();
  } else {
    els.query.focus();
  }
}

function showSettingsPanel(panel) {
  for (const tab of els.settingsTabs.querySelectorAll(".tab")) {
    const active = tab.dataset.panel === panel;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-pressed", String(active));
  }
  for (const section of els.settingsView.querySelectorAll(".settings-panel")) {
    section.hidden = section.dataset.panel !== panel;
  }
}

function openStats() {
  lastFocus = document.activeElement;
  els.statsModal.hidden = false;
  const close = els.statsModal.querySelector(".modal-close");
  if (close) close.focus();
}

function closeStats() {
  els.statsModal.hidden = true;
  if (lastFocus instanceof HTMLElement) lastFocus.focus();
}

/* ---------- события ---------- */

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = els.query.value;
  if (!els.settingsView.hidden) showView("search");
  runSearch();
});

els.query.addEventListener("input", () => {
  state.query = els.query.value;
  if (!els.settingsView.hidden) showView("search");
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

for (const tab of document.querySelectorAll("#search-view .tab[data-layer]")) {
  tab.addEventListener("click", () => setLayer(tab.dataset.layer));
}

els.filterToggle.addEventListener("click", () => {
  const open = els.filters.hidden;
  els.filters.hidden = !open;
  els.filterToggle.setAttribute("aria-expanded", String(open));
});

els.minScore.addEventListener("input", () => {
  els.minScoreValue.textContent = Number(els.minScore.value).toFixed(2);
});

els.filterApply.addEventListener("click", applyFilters);
els.filterReset.addEventListener("click", resetFilters);
els.retry.addEventListener("click", runSearch);

els.themeBtn.addEventListener("click", toggleTheme);

els.settingsBtn.addEventListener("click", () => {
  showView(els.settingsView.hidden ? "settings" : "search");
});

els.settingsBack.addEventListener("click", () => showView("search"));
els.logo.addEventListener("click", (event) => {
  event.preventDefault();
  showView("search");
});

els.settingsTabs.addEventListener("click", (event) => {
  const tab = event.target.closest(".tab[data-panel]");
  if (tab) showSettingsPanel(tab.dataset.panel);
});

els.statusBtn.addEventListener("click", openStats);
for (const node of els.statsModal.querySelectorAll("[data-close-stats]")) {
  node.addEventListener("click", closeStats);
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-toast]");
  if (button) showToast(button.dataset.toast);
});

els.results.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  if (target.tagName === "A") event.preventDefault();

  const doc = DOCS.find((item) => item.id === target.dataset.id);
  if (!doc) return;

  if (target.dataset.action === "open") {
    showToast(`Открыть «${doc.title}» — в приложении это сделает сервер.`);
    return;
  }

  const { dir } = splitPath(doc.path);
  showToast(`Проводник: ${dir.replace(/[\\/]$/, "")}`);
});

for (const button of document.querySelectorAll("[data-query]")) {
  button.addEventListener("click", () => {
    els.query.value = button.dataset.query;
    state.query = button.dataset.query;
    runSearch();
    els.query.focus();
  });
}

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing =
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement;

  if (event.key === "Escape") {
    if (!els.statsModal.hidden) {
      closeStats();
      return;
    }
    if (!els.filters.hidden) {
      els.filters.hidden = true;
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
    return;
  }

  if (event.key === "/" && !typing) {
    event.preventDefault();
    if (!els.settingsView.hidden) showView("search");
    els.query.focus();
    els.query.select();
  }
});

/* ---------- старт ---------- */

function initFromUrl() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("q")) state.query = params.get("q") || "";
  if (params.get("error") === "1") state.error = true;
  els.query.value = state.query;
  if (params.get("view") === "settings") {
    showView("settings");
    if (params.get("panel")) showSettingsPanel(params.get("panel"));
  }
  if (params.get("stats") === "1") openStats();
}

els.statusTip.textContent = `${STATUS.address} · ${STATUS.vectors.toLocaleString("ru-RU")} векторов`;
els.minScore.value = String(DEFAULTS.minScore);
els.minScoreValue.textContent = DEFAULTS.minScore.toFixed(2);
initTheme();
initFromUrl();
render();
