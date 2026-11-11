"use strict";

/* Settings panel. A modal with a section sidebar that opens over whichever page
   you are on (Settings in the sidebar, or Ctrl/⌘ + ,). Changes save as
   you make them. The workspace registers a provider so its own saved state
   (rule order, auto-run) is kept and it hears about theme and format changes. */

(() => {
  const STORAGE_KEY = "kumosql-ui";
  const THEMES = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];
  const FORMAT_FIELDS = [
    { name: "keyword_case", label: "Keyword case", hint: "How SQL keywords like SELECT and FROM are written.", type: "choice",
      options: [["upper", "UPPER"], ["lower", "lower"], ["capitalise", "Capitalised"], ["consistent", "Consistent"]] },
    { name: "comma_position", label: "Commas", hint: "Put commas at the end or the start of each line.", type: "choice",
      options: [["trailing", "Trailing"], ["leading", "Leading"]] },
    { name: "indent_unit", label: "Indent with", hint: "Indent nested clauses with spaces or tabs.", type: "choice",
      options: [["space", "Spaces"], ["tab", "Tabs"]] },
    { name: "tab_space_size", label: "Indent size", hint: "Spaces per indent level (1 to 8).", type: "number", min: 1, max: 8 },
    { name: "max_line_length", label: "Max line length", hint: "Lines longer than this are wrapped (20 to 500).", type: "number", min: 20, max: 500 },
    { name: "rules", label: "sqlfluff rules", hint: "Rules or groups to apply, comma separated.", type: "list", placeholder: "layout, capitalisation, LT01" },
    { name: "exclude_rules", label: "Excluded rules", hint: "Rules to skip, comma separated.", type: "list", placeholder: "e.g. LT05" },
  ];
  const SECTIONS = [
    { id: "appearance", label: "Appearance", icon: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>' },
    { id: "formatting", label: "SQL formatting", icon: '<path d="M4 6h16M4 12h10M4 18h13"/>' },
    { id: "storage", label: "Local data folder", icon: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' },
    { id: "repositories", label: "Repositories", icon: '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="9" r="2"/><path d="M6 8v8M18 11c0 4-6 3-12 5"/>' },
    { id: "bigquery", label: "BigQuery projects", icon: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>' },
    { id: "datasources", label: "Data sources", icon: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>' },
    { id: "scopes", label: "Scopes", icon: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>' },
    { id: "diagnostics", label: "Diagnostics", icon: '<path d="M4 4h16v13H8l-4 4z"/><path d="M8 9h8M8 13h5"/>' },
    { id: "analysis", label: "Analysis", icon: '<path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="9"/>' },
    { id: "solver", label: "Solver", icon: '<path d="M4 20h16M6 20V9l6-5 6 5v11M10 20v-6h4v6"/>' },
    { id: "catalogs", label: "Catalogs", icon: '<path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z"/><path d="M8 7h6M8 11h6"/>' },
    { id: "tagrules", label: "Tag rules", icon: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1"/>' },
  ];

  let dialog;
  let provider = null;
  let listeners = [];
  let ui = {};
  let profiles = [];
  let activeId = "";
  let current = "appearance";
  let saveTimer;
  let uiWaiters = [];
  let formatTimer;
  // sqlfluff's rule list (from the installed sqlfluff) and the active
  // configuration's enabled rule codes while the formatting section is shown.
  let ruleCatalog = null;
  let enabledRules = null;
  const CATEGORY_HINTS = {
    layout: "Spacing, indentation, line breaks and line length.",
    capitalisation: "Upper or lower case for keywords, names, functions, literals and types.",
    aliasing: "How tables and columns are aliased.",
    convention: "One consistent choice where SQL allows several ways to write the same thing.",
    structure: "Simpler query structure, like dropping redundant CASE branches or unused CTEs.",
    ambiguous: "SQL whose meaning is unclear, like a bare JOIN or DISTINCT with GROUP BY.",
    references: "How columns and tables are referenced and quoted.",
    jinja: "Spacing inside Jinja template tags.",
  };

  const h = (tag, attrs = {}, ...children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) element.setAttribute(key, value === true ? "" : value);
    }
    element.append(...children.filter(Boolean));
    return element;
  };
  const svg = (paths) => {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("class", "icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = paths;
    return icon;
  };

  function active() {
    return profiles.find((item) => item.id === activeId) || profiles[0];
  }

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }

  function notify() {
    for (const listener of listeners) listener(structuredClone(ui));
  }

  async function putJson(url, payload, fallbackError) {
    const response = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || fallbackError);
    return data;
  }

  // Resolves once the server has stored the settings (calls within 200 ms share one write).
  function saveUi() {
    ui = structuredClone({ ...ui, ...(provider ? provider.getUi() : {}), ...pending() });
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(ui));
    } catch {
      /* browser storage unavailable; the server copy still persists */
    }
    notify();
    clearTimeout(saveTimer);
    const written = new Promise((resolve, reject) => uiWaiters.push({ resolve, reject }));
    written.catch(() => {}); // callers that do not wait still see the error in the status line
    saveTimer = setTimeout(() => {
      const waiters = uiWaiters;
      uiWaiters = [];
      putJson("/api/settings/ui", ui, "Could not save settings").then(
        () => waiters.forEach((waiter) => waiter.resolve()),
        (error) => {
          setStatus(error.message, true);
          waiters.forEach((waiter) => waiter.reject(error));
        },
      );
    }, 200);
    return written;
  }

  // Values owned by this panel; they win over whatever the page last saved.
  function pending() {
    return { theme: ui.theme || "system", sqlfluffProfiles: profiles, activeSqlfluffProfile: activeId };
  }

  function setStatus(text, error = false) {
    const status = dialog?.querySelector(".sp-status");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("is-error", error);
  }

  /* ---------- Controls ---------- */

  function segmented(name, options, value, onChange) {
    const group = h("div", { class: "sp-segmented", role: "radiogroup", "aria-label": name });
    for (const [optionValue, label] of options) {
      const button = h("button", {
        type: "button", role: "radio", "aria-checked": String(optionValue === value), text: label,
        onclick: () => {
          for (const other of group.children) other.setAttribute("aria-checked", String(other === button));
          onChange(optionValue);
        },
      });
      group.append(button);
    }
    group.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const buttons = [...group.children];
      const index = buttons.indexOf(document.activeElement);
      const next = buttons[(index + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      next.click();
    });
    return group;
  }

  function row(label, hint, control, id) {
    return h("div", { class: "sp-row", "data-search": `${label} ${hint}`.toLowerCase() },
      h("div", { class: "sp-row-text" }, h("label", { class: "sp-row-label", for: id, text: label }), h("p", { class: "sp-row-hint", text: hint })),
      h("div", { class: "sp-row-control" }, control));
  }

  /* ---------- Sections ---------- */

  function renderAppearance(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Appearance" }),
      row("Theme", "Match your system, or always use light or dark.", segmented("Theme", THEMES, ui.theme || "system", (theme) => {
        ui.theme = theme;
        applyTheme(theme);
        saveUi();
      })),
    );
  }

  function readFormat(form) {
    const format = { ...(active().format || {}) };
    for (const field of FORMAT_FIELDS) {
      const element = form.querySelector(`[data-field="${field.name}"]`);
      if (field.type === "choice") format[field.name] = element.querySelector('[aria-checked="true"]')?.dataset.value ?? format[field.name];
      else if (field.type === "number") format[field.name] = Number(element.value);
      else if (element) format[field.name] = element.value.split(",").map((item) => item.trim()).filter(Boolean);
    }
    if (enabledRules) {
      format.rules = ruleCatalog.map((rule) => rule.code).filter((code) => enabledRules.has(code));
      format.exclude_rules = [];
    }
    return format;
  }

  // Saved configurations may name groups ("layout"), rule names or legacy
  // aliases; turn them into the rule codes they cover.
  function expandRules(selectors) {
    const codes = new Set();
    for (const raw of selectors || []) {
      const selector = String(raw).trim().toLowerCase();
      for (const rule of ruleCatalog) {
        if (rule.code.toLowerCase() === selector || rule.name === selector
          || rule.aliases.some((alias) => alias.toLowerCase() === selector)
          || rule.groups.includes(selector)) codes.add(rule.code);
      }
    }
    return codes;
  }

  function renderRules(form) {
    const format = active().format || {};
    const excluded = expandRules(format.exclude_rules);
    enabledRules = new Set([...expandRules(format.rules)].filter((code) => !excluded.has(code)));
    const save = () => scheduleFormatSave(form);
    const categories = [...new Set(ruleCatalog.map((rule) => rule.category))];
    const search = h("input", { type: "search", class: "sp-input sp-rule-search", placeholder: "Search rules by code, name or description", "aria-label": "Search sqlfluff rules" });
    const empty = h("p", { class: "sp-rule-empty", text: "No rules match.", hidden: true });
    const groups = [];

    for (const category of categories) {
      const rules = ruleCatalog.filter((rule) => rule.category === category);
      const count = h("span", { class: "sp-rule-count" });
      const updateCount = () => {
        const on = rules.filter((rule) => enabledRules.has(rule.code)).length;
        count.textContent = `${on} of ${rules.length} on`;
      };
      const toggles = [];
      const setAll = (value) => {
        for (const [rule, input] of toggles) {
          if (!rule.fixable) continue;
          input.checked = value;
          if (value) enabledRules.add(rule.code); else enabledRules.delete(rule.code);
        }
        updateCount();
        save();
      };
      const list = h("div", { class: "sp-rule-list" });
      for (const rule of rules) {
        const id = `sp-rule-${rule.code}`;
        const input = h("input", { id, type: "checkbox", checked: enabledRules.has(rule.code), disabled: !rule.fixable });
        input.addEventListener("change", () => {
          if (input.checked) enabledRules.add(rule.code); else enabledRules.delete(rule.code);
          updateCount();
          save();
        });
        toggles.push([rule, input]);
        list.append(h("div", { class: `sp-rule${rule.fixable ? "" : " is-lint-only"}`, "data-search": `${rule.code} ${rule.name} ${rule.description}`.toLowerCase() },
          h("label", { class: "sp-rule-text", for: id },
            h("span", { class: "sp-rule-desc", text: rule.description }),
            h("span", { class: "sp-rule-meta" },
              h("code", { text: rule.code }), h("span", { text: rule.name }),
              rule.fixable ? null : h("span", { class: "sp-rule-tag", text: "Lint only", title: "sqlfluff can report this but not fix it, so it never changes formatted SQL" }))),
          h("label", { class: "switch sp-switch", title: rule.fixable ? "" : "Lint-only rules can't change formatted SQL" },
            input, h("span", { class: "switch-track", "aria-hidden": "true" }))));
      }
      updateCount();
      const group = h("section", { class: "sp-rule-group" },
        h("div", { class: "sp-rule-head" },
          h("div", {},
            h("h4", { text: category[0].toUpperCase() + category.slice(1) }),
            h("p", { class: "sp-row-hint", text: CATEGORY_HINTS[category] || "" })),
          h("div", { class: "sp-inline" }, count,
            h("button", { type: "button", class: "link-button", text: "All on", onclick: () => setAll(true) }),
            h("button", { type: "button", class: "link-button", text: "All off", onclick: () => setAll(false) }))),
        list);
      groups.push(group);
    }

    search.addEventListener("input", () => {
      const text = search.value.trim().toLowerCase();
      let shown = 0;
      for (const group of groups) {
        let groupShown = 0;
        for (const rule of group.querySelectorAll(".sp-rule")) {
          const match = !text || rule.dataset.search.includes(text);
          rule.hidden = !match;
          if (match) groupShown += 1;
        }
        group.hidden = !groupShown;
        shown += groupShown;
      }
      empty.hidden = shown > 0;
    });

    const lintOnly = ruleCatalog.filter((rule) => !rule.fixable).length;
    return h("div", { class: "sp-rules" },
      h("h3", { class: "sp-subheading", text: "Rules" }),
      h("p", { class: "sp-lede", text: `The sqlfluff rules the Format SQL rule applies (${ruleCatalog.length} BigQuery rules from sqlfluff). ${lintOnly} are lint only: sqlfluff can report them but not fix them, so they can't change formatted SQL and stay off.` }),
      search, empty, ...groups);
  }

  function scheduleFormatSave(form) {
    clearTimeout(formatTimer);
    setStatus("Saving…");
    formatTimer = setTimeout(async () => {
      if (enabledRules && !enabledRules.size) {
        setStatus("Turn on at least one rule", true);
        return;
      }
      const profile = active();
      try {
        profile.format = await putJson("/api/settings/format", readFormat(form), "Could not save sqlfluff settings");
        await saveUi();
        setStatus("Saved");
      } catch (error) {
        setStatus(error.message, true);
      }
    }, 350);
  }

  async function activate(id) {
    activeId = id;
    const profile = active();
    try {
      if (profile.format) profile.format = await putJson("/api/settings/format", profile.format, "Could not apply sqlfluff settings");
    } catch (error) {
      setStatus(error.message, true);
    }
    saveUi();
    show("formatting");
  }

  function renderFormatting(body) {
    const profile = active();
    const format = profile.format || {};
    const select = h("select", { id: "sp-profile", class: "sp-input", onchange: (event) => activate(event.target.value) });
    for (const item of profiles) select.append(h("option", { value: item.id, text: item.name, selected: item.id === activeId }));
    // Renaming waits for the Rename button (or Enter); typing alone saves nothing.
    const rename = () => {
      const next = name.value.trim();
      if (next === profile.name) return;
      if (!next || profiles.some((item) => item !== profile && item.name.toLowerCase() === next.toLowerCase())) {
        setStatus(next ? "Configuration names must be unique" : "Give this configuration a name", true);
        return;
      }
      profile.name = next;
      name.value = next;
      select.querySelector(`option[value="${profile.id}"]`).textContent = next;
      renameButton.disabled = true;
      saveUi().then(() => setStatus("Renamed"), () => {});
    };
    const name = h("input", {
      id: "sp-profile-name", class: "sp-input", type: "text", maxlength: "60", value: profile.name,
      oninput: () => {
        const next = name.value.trim();
        renameButton.disabled = !next || next === profile.name;
        setStatus("");
      },
      onkeydown: (event) => { if (event.key === "Enter") { event.preventDefault(); rename(); } },
    });
    const renameButton = h("button", { type: "button", class: "toolbar-button", text: "Rename", disabled: true, onclick: rename });
    const actions = h("div", { class: "sp-inline" },
      renameButton,
      h("button", { type: "button", class: "toolbar-button", text: "New", onclick: () => {
        const item = { id: crypto.randomUUID(), name: `Configuration ${profiles.length + 1}`, format: { ...format } };
        profiles.push(item);
        activate(item.id).then(() => dialog.querySelector("#sp-profile-name")?.select());
      } }),
      h("button", { type: "button", class: "toolbar-button sp-danger", text: "Delete", disabled: profiles.length < 2, onclick: () => {
        if (profiles.length < 2 || !confirm(`Delete the configuration “${profile.name}”?`)) return;
        profiles = profiles.filter((item) => item !== profile);
        activate(profiles[0].id);
      } }),
    );

    const form = h("form", { class: "sp-group", autocomplete: "off", onsubmit: (event) => event.preventDefault() });
    for (const field of FORMAT_FIELDS) {
      if (field.type === "list" && ruleCatalog) continue;
      const id = `sp-${field.name}`;
      let control;
      if (field.type === "choice") {
        control = segmented(field.label, field.options, format[field.name], () => scheduleFormatSave(form));
        for (const [index, [value]] of field.options.entries()) control.children[index].dataset.value = value;
      } else if (field.type === "number") {
        control = h("input", { id, class: "sp-input sp-number", type: "number", min: field.min, max: field.max, step: "1", value: format[field.name] ?? "" });
      } else {
        control = h("input", { id, class: "sp-input sp-text", type: "text", spellcheck: "false", placeholder: field.placeholder, value: (format[field.name] || []).join(", ") });
      }
      control.dataset.field = field.name;
      if (field.type !== "choice") control.addEventListener("change", () => scheduleFormatSave(form));
      form.append(row(field.label, field.hint, control, field.type === "choice" ? null : id));
    }

    body.append(
      h("h3", { class: "sp-heading", text: "SQL formatting" }),
      h("p", { class: "sp-lede", text: "Used by the Format SQL rule in the pipeline. Keep several named sqlfluff configurations and pick which one is active." }),
      h("div", { class: "sp-group" },
        row("Active configuration", "The configuration the Format SQL rule uses.", select, "sp-profile"),
        row("Name", "Type a new name, then press Rename.", h("div", { class: "sp-inline" }, name, actions), "sp-profile-name")),
      form,
      ...(ruleCatalog ? [renderRules(form)] : []),
    );
  }

  function renderScopes(body) {
    if (!window.KumoScopes) {
      body.append(h("p", { class: "sp-lede", text: "Scopes could not be loaded on this page." }));
      return;
    }
    window.KumoScopes.renderManager(body, { onStatus: setStatus });
  }

  function renderDataSources(body) {
    if (!window.KumoDataSources) {
      body.append(h("p", { class: "sp-lede", text: "Data sources could not be loaded on this page." }));
      return;
    }
    window.KumoDataSources.render(body, { onStatus: setStatus });
  }

  function renderCatalogs(body) {
    if (!window.KumoCatalogs) {
      body.append(h("p", { class: "sp-lede", text: "Catalogs could not be loaded on this page." }));
      return;
    }
    window.KumoCatalogs.renderManager(body, { onStatus: setStatus });
  }

  function renderTagRules(body) {
    if (!window.KumoTags) {
      body.append(h("p", { class: "sp-lede", text: "Tag rules could not be loaded on this page." }));
      return;
    }
    window.KumoTags.renderRules(body, { onStatus: setStatus });
  }

  /* Connected Dataform repositories: saved on this computer and reloaded on start. */
  let reposChanged = false;
  async function repoCall(method, url, payload) {
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    if (method !== "GET") reposChanged = true;
    return data;
  }

  function ago(iso) {
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 90) return "just now";
    if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} days ago`;
  }

  async function renderStorage(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Local data folder" }),
      h("p", { class: "sp-lede", text: "The folder where KumoSQL keeps its files on this computer: your settings and saved configurations (formatting rules, scopes, BigQuery projects, repositories), its caches, its log and copies of your Dataform repositories. Only a tiny pointer file stays in the default location. Changing the folder moves the existing files into it. Choose a folder in your home folder, not under AppData: the Microsoft Store build of Python hides AppData from git. KumoSQL checks that the folder exists, is writable, and that git can use it." }));
    const status = h("p", { class: "sp-row-hint" });
    const folder = h("input", { type: "text", class: "sp-input sp-text", "aria-label": "Local data folder", autocomplete: "off", required: "" });
    const use = h("button", { type: "button", class: "link-button", text: "Use suggested folder" });
    const form = h("form", { class: "repo-form" }, folder, h("button", { type: "submit", class: "toolbar-button", text: "Save folder" }), use);
    body.append(form, status);
    let info = { folder: null, suggested: "" };
    const draw = () => {
      folder.value = info.folder || "";
      folder.placeholder = info.suggested;
      status.textContent = info.override ? `Overridden by the KUMOSQL_GIT_CACHE environment variable: ${info.override}`
        : info.folder ? `Settings, caches, logs and repository clones are kept in ${info.folder}.` : "No folder chosen yet. Connecting a repository needs one.";
    };
    use.addEventListener("click", () => { folder.value = info.suggested; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setStatus("Checking the folder with git…");
      try {
        info = await repoCall("POST", "/api/storage", { folder: folder.value.trim() });
        setStatus("Saved");
        draw();
        const moved = info.migrated;
        if (moved) {
          const parts = [];
          if (moved.moved?.length) parts.push(`moved ${(moved.moved || []).join(", ")}`);
          if (moved.merged.length) parts.push(`merged settings from ${moved.from}`);
          if (moved.kept?.length) parts.push(`kept the copy already here of ${(moved.kept || []).join(", ")} (the old one stays in ${moved.from})`);
          status.textContent += ` From ${moved.from}: ${parts.join("; ")}.`;
        }
        if (info.previous) status.textContent += ` Clones in ${info.previous} are not moved; repositories are cloned again here on their next load, and the old folder can be deleted.`;
      } catch (error) {
        setStatus(error.message, true);
      }
    });
    try { info = await repoCall("GET", "/api/storage"); draw(); } catch (error) { setStatus(error.message, true); }
  }

  async function renderRepositories(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Repositories" }),
      h("p", { class: "sp-lede", text: "Connect the Dataform repositories you want analysed. They are saved here and reloaded each time KumoSQL starts. Git runs on this computer with your own credentials, so private repositories work with an https URL (https://github.com/owner/repo.git), an SSH remote, or a local path. The active repository feeds the Query graph, Cost and Change reports pages." }));
    const list = h("ul", { class: "repo-list" });
    const url = h("input", { type: "text", class: "sp-input sp-text", placeholder: "https://github.com/owner/repository.git", "aria-label": "Repository URL", autocomplete: "off", required: "" });
    const branch = h("input", { type: "text", class: "sp-input", placeholder: "branch (default)", "aria-label": "Branch", autocomplete: "off", size: "14" });
    const form = h("form", { class: "repo-form" }, url, branch, h("button", { type: "submit", class: "toolbar-button", text: "Connect" }));
    const needFolder = h("p", { class: "sp-row-hint is-error", hidden: true });
    const chooseFolder = h("button", { type: "button", class: "link-button", text: "Choose a local data folder", hidden: true });
    chooseFolder.addEventListener("click", () => show("storage"));
    const clearAll = h("button", { type: "button", class: "link-button", text: "Clear all repositories", hidden: true });
    body.append(list, needFolder, chooseFolder, form, clearAll);
    let data = { repositories: [], active: null };
    repoCall("GET", "/api/storage").then((info) => {
      if (info.configured) return;
      for (const element of form.elements) element.disabled = true;
      needFolder.textContent = "Choose a local data folder before connecting a repository. KumoSQL keeps repository clones there.";
      needFolder.hidden = false;
      chooseFolder.hidden = false;
    }).catch(() => { /* the server will say so when you connect */ });

    let schedules = { repositories: {}, default_location: "" };
    const when = (seconds) => (seconds ? ago(new Date(seconds * 1000).toISOString()) : "");
    // Production schedules (Dataform workflow configurations) for one repository.
    function scheduleBlock(repo) {
      const info = schedules.repositories[repo.id];
      const box = h("div", { class: "repo-wf" });
      if (!info) return box;
      // Only a loaded lookup has counts; other states (needs_projects, error) carry a message.
      const text = info.state === "loaded"
        ? `${info.configs} workflow configuration${info.configs === 1 ? "" : "s"} in Dataform (${(info.repositories || []).join(", ")}), ${info.active_production} active in production · refreshed ${when(info.fetched_at)}${info.stale ? " · could not refresh, showing the saved copy" : ""}${info.refreshing ? " · refreshing" : ""}`
        : info.message || "";
      box.append(h("p", { class: `sp-row-hint${info.state === "error" ? " is-error" : ""}`, text: `Production schedules: ${text}` }));
      for (const warning of info.warnings || []) box.append(h("p", { class: "sp-row-hint is-error", text: warning }));
      const projects = h("input", { type: "text", class: "sp-input sp-text", "aria-label": "Google Cloud projects to search", autocomplete: "off",
        placeholder: info.override ? "" : `${(info.projects || []).join(", ") || "projects from BigQuery settings"}`, value: info.override ? (info.projects || []).join(", ") : "" });
      const location = h("input", { type: "text", class: "sp-input", "aria-label": "Dataform location", autocomplete: "off", size: "16", placeholder: info.location, value: info.override ? info.location : "" });
      const load = h("button", { type: "button", class: "toolbar-button", text: "Refresh schedules" });
      const save = h("button", { type: "button", class: "link-button", text: "Save search" });
      load.addEventListener("click", () => work(load, async () => { await repoCall("POST", "/api/workflow-configs/refresh", { id: repo.id }); }));
      save.addEventListener("click", () => work(save, async () => {
        await repoCall("POST", "/api/workflow-configs/settings", { id: repo.id, projects: projects.value, location: location.value.trim() });
        await repoCall("POST", "/api/workflow-configs/refresh", { id: repo.id });
      }));
      box.append(h("div", { class: "repo-form" }, projects, location, save, load));
      return box;
    }
    async function work(button, action) {
      button.disabled = true;
      setStatus("Asking Dataform…");
      try { await action(); setStatus("Loaded"); } catch (error) { setStatus(error.message, true); }
      button.disabled = false;
      try { await refreshList(); } catch { /* keep the last list */ }
    }

    const draw = () => {
      list.replaceChildren();
      clearAll.hidden = !data.repositories.length;
      if (!data.repositories.length) list.append(h("li", { class: "sp-row-hint", text: "No repositories connected yet." }));
      for (const repo of data.repositories) {
        const isActive = repo.id === data.active;
        const status = repo.loading ? "Loading with git… this can take a while for a large repository. You can keep using KumoSQL."
          : repo.error ? `Failed ${ago(repo.error_at)}: ${repo.error}`
          : repo.last_loaded ? `${repo.label || "Loaded"} · ${repo.files} files · loaded ${ago(repo.last_loaded)}${repo.stale_reason ? ` · using saved copy, could not fetch: ${repo.stale_reason}` : ""}`
          : "Not loaded yet";
        const refresh = h("button", { type: "button", class: "toolbar-button", text: "Refresh" });
        refresh.addEventListener("click", () => run(refresh, async () => { await repoCall("POST", "/api/repositories/refresh", { id: repo.id }); }, "Loaded"));
        const use = h("button", { type: "button", class: "toolbar-button", text: "Use", hidden: isActive });
        use.addEventListener("click", () => run(use, async () => { await repoCall("POST", "/api/repositories/activate", { id: repo.id }); }, "Loaded"));
        const remove = h("button", { type: "button", class: "link-button", text: "Remove" });
        remove.addEventListener("click", () => run(remove, async () => {
          const rest = data.repositories.filter((item) => item.id !== repo.id).map(({ url: u, branch: b }) => ({ url: u, branch: b }));
          await repoCall("POST", "/api/repositories", { repositories: rest });
        }, "Removed"));
        list.append(h("li", { class: `repo-item${isActive ? " is-active" : ""}` },
          h("div", { class: "repo-main" },
            h("strong", { class: "repo-url", text: repo.url }),
            h("span", { class: "repo-branch", text: repo.branch ? ` @ ${repo.branch}` : "" }),
            isActive ? h("span", { class: "repo-badge", text: "Active" }) : ""),
          h("p", { class: `sp-row-hint${repo.error ? " is-error" : ""}`, text: status }),
          repo.note && !repo.error ? h("p", { class: "sp-row-hint", text: repo.note }) : "",
          scheduleBlock(repo),
          h("div", { class: "repo-actions" }, use, refresh, remove)));
      }
    };
    let poll = null;
    const refreshList = async () => {
      clearTimeout(poll);
      data = await repoCall("GET", "/api/repositories");
      // A load runs in the background (at start-up, or started elsewhere): keep the status current.
      if (data.repositories.some((repo) => repo.loading)) {
        poll = setTimeout(() => { if (body.isConnected) refreshList().catch(() => {}); }, 2000);
      }
      try { schedules = await repoCall("GET", "/api/workflow-configs"); } catch { schedules = { repositories: {} }; }
      draw();
    };
    async function run(button, action, done) {
      button.disabled = true;
      setStatus("Working with git…");
      try { await action(); setStatus(done); } catch (error) { setStatus(error.message, true); }
      button.disabled = false;
      try { await refreshList(); } catch { /* keep the last list */ }
    }
    clearAll.addEventListener("click", () => {
      if (!window.confirm("Remove every connected repository and delete the cached copies, saved schedule lookups and saved analyses? Your other settings stay. This cannot be undone.")) return;
      run(clearAll, async () => { await repoCall("POST", "/api/repositories/clear", {}); }, "All repositories cleared");
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      run(button, async () => {
        const current = data.repositories.map(({ url: u, branch: b }) => ({ url: u, branch: b }));
        const saved = await repoCall("POST", "/api/repositories", { repositories: [...current, { url: url.value.trim(), branch: branch.value.trim() }] });
        const added = saved.repositories[saved.repositories.length - 1];
        url.value = "";
        branch.value = "";
        await repoCall("POST", "/api/repositories/activate", { id: added.id });
      }, "Connected");
    });
    try { await refreshList(); } catch (error) { setStatus(error.message, true); }
  }

  function renderBigQuery(body) {
    body.append(h("h3", { class: "sp-heading", text: "BigQuery projects" }));
    if (!window.KumoBqProjects) {
      body.append(h("p", { class: "sp-lede", text: "The project picker could not be loaded on this page." }));
      return;
    }
    window.KumoBqProjects.render(body, { onStatus: setStatus });
    window.KumoBqProjects.renderBilling(body, { onStatus: setStatus });
  }

  async function renderAnalysis(body) {
    const field = (label) => h("input", { type: "number", class: "sp-input sp-number", min: "0", max: "86400", step: "10", "aria-label": label });
    const model = field("Seconds per model");
    const total = field("Seconds in total");
    const draw = (info) => {
      model.value = info.model_seconds; total.value = info.total_seconds;
      model.disabled = info.locked.includes("model_seconds"); total.disabled = info.locked.includes("total_seconds");
    };
    const put = async (payload) => {
      setStatus("Saving…");
      try { draw(await repoCall("PUT", "/api/lineage-limits", payload)); setStatus("Saved"); } catch (error) { setStatus(error.message, true); }
    };
    model.addEventListener("change", () => put({ model_seconds: Number(model.value) }));
    total.addEventListener("change", () => put({ total_seconds: Number(total.value) }));
    body.append(
      h("h3", { class: "sp-heading", text: "Column lineage time limit" }),
      h("div", { class: "sp-inline" }, model, h("span", { text: "seconds per model (0 for none)" })),
      h("div", { class: "sp-inline" }, total, h("span", { text: "seconds in total (0 for none)" })),
    );
    try { draw(await repoCall("GET", "/api/lineage-limits")); } catch (error) { setStatus(error.message, true); }

    const fetchColumns = h("input", { type: "checkbox", id: "schema-fetch", "aria-label": "Look up columns of unknown tables" });
    fetchColumns.addEventListener("change", async () => {
      setStatus("Saving…");
      try { fetchColumns.checked = (await repoCall("PUT", "/api/schema-fetch", { enabled: fetchColumns.checked })).enabled; setStatus("Saved"); } catch (error) { setStatus(error.message, true); }
    });
    body.append(
      h("h3", { class: "sp-heading", text: "Unknown tables" }),
      h("label", { class: "sp-inline", for: "schema-fetch" }, fetchColumns, h("span", { text: "Look up their columns in BigQuery" })),
    );
    try { fetchColumns.checked = (await repoCall("GET", "/api/schema-fetch")).enabled; } catch (error) { setStatus(error.message, true); }
  }

  async function renderSolver(body) {
    const enabled = h("input", { type: "checkbox", id: "solver-enabled", "aria-label": "Use the equivalence solver" });
    const timeout = h("input", { type: "number", class: "sp-input sp-number", min: "500", max: "60000", step: "500", "aria-label": "Time limit in milliseconds" });
    const rows = h("input", { type: "number", class: "sp-input sp-number", min: "0", max: "6", step: "1", "aria-label": "Rows per table in the bounded check" });
    const facts = h("p", { class: "sp-row-hint" });
    const draw = (info) => {
      enabled.checked = info.enabled;
      timeout.value = info.timeout_ms;
      rows.value = info.bounded_rows;
      if (!info.available) facts.textContent = "z3-solver is not installed, so nothing is proven by the solver.";
      else if (!info.enabled) facts.textContent = "";
      else facts.textContent = `${info.tables} tables known, ${info.constrained} with declared keys or NOT NULL columns.`;
    };
    const put = async (payload) => {
      setStatus("Saving…");
      try { draw(await repoCall("PUT", "/api/prover", payload)); setStatus("Saved"); } catch (error) { setStatus(error.message, true); }
    };
    enabled.addEventListener("change", () => put({ enabled: enabled.checked }));
    timeout.addEventListener("change", () => put({ timeout_ms: Number(timeout.value) }));
    rows.addEventListener("change", () => put({ bounded_rows: Number(rows.value) }));
    body.append(
      h("h3", { class: "sp-heading", text: "Solver" }),
      h("label", { class: "sp-inline", for: "solver-enabled" }, enabled, h("span", { text: "Prove rewrites equivalent with the solver" })),
      h("div", { class: "sp-inline" }, timeout, h("span", { text: "ms per check" })),
      h("div", { class: "sp-inline" }, rows, h("span", { text: "rows per table, bounded check (0 for off)" })),
      facts,
    );
    try { draw(await repoCall("GET", "/api/prover")); } catch (error) { setStatus(error.message, true); }

    const list = h("div", { class: "sp-list" });
    const field = (placeholder, label) => h("input", { type: "text", class: "sp-input", placeholder, spellcheck: "false", "aria-label": label });
    const left = field("project.dataset.table_a", "Table A");
    const right = field("project.dataset.table_b", "Table B");
    const pairs = field("x = y, amount = total", "Columns of A = columns of B");
    const whole = h("input", { type: "checkbox", id: "equivalence-whole", "aria-label": "Same rows in every column" });
    const drawList = (items) => list.replaceChildren(...items.map((item) => h("div", { class: "sp-row" },
      h("span", { text: `${item.left} ≡ ${item.right}${item.columns.length ? ` (${item.columns.map((p) => p.join(" = ")).join(", ")})` : ""}${item.whole ? ", whole table" : ""}` }),
      h("button", { type: "button", class: "toolbar-button", text: "Remove", onclick: async () => {
        try { await repoCall("POST", "/api/equivalences/remove", { right: item.right }); await refresh(); } catch (error) { setStatus(error.message, true); }
      } }))));
    const refresh = async () => drawList((await repoCall("GET", "/api/equivalences")).equivalences);
    const save = async () => {
      const columns = pairs.value.split(",").map((part) => part.split("=").map((name) => name.trim())).filter((pair) => pair.some(Boolean));
      try {
        await repoCall("POST", "/api/equivalences", { left: left.value, right: right.value, columns, whole: whole.checked });
        left.value = right.value = pairs.value = ""; whole.checked = false; setStatus("Saved"); await refresh();
      } catch (error) { setStatus(error.message, true); }
    };
    const boundedLine = (bounded) => !bounded || bounded.status === "unknown" ? ""
      : bounded.status === "bounded_equivalent" ? ` (${bounded.label})` : ` (different results, ${bounded.bound} ${bounded.bound === 1 ? "row" : "rows"})`;
    const verdict = h("p", { class: "sp-row-hint" });
    const verdictDetail = h("div", { class: "sp-query-detail" });
    const conditionsOf = (conditions) => h("details", { class: "ev-assumptions", open: "" },
      h("summary", { text: `Conditions (${conditions.length})` }),
      h("ul", {}, ...conditions.map((item) => h("li", {}, item.check_sql ? h("details", {}, h("summary", { text: item.text }), h("pre", { class: "sp-pre", text: item.check_sql })) : h("span", { text: item.text })))));
    const assumptionsOf = (assumptions) => h("details", { class: "ev-assumptions" }, h("summary", { text: `Assumptions (${assumptions.length})` }),
      h("ul", {}, ...assumptions.map((item) => h("li", { text: item }))));
    const compare = async () => {
      verdict.textContent = "Comparing…";
      verdictDetail.replaceChildren();
      try {
        const result = await repoCall("POST", "/api/prove-tables", { left: left.value, right: right.value });
        verdict.textContent = result.status === "equivalent"
          ? `Equivalent (${result.method}${result.lemmas.length ? `, ${result.lemmas.length} layers matched` : ""}). ${result.assumptions.filter((a) => a.startsWith("declared")).join(" ")}`
          : result.status === "conditional" ? `Equivalent under ${result.conditions.length} ${result.conditions.length === 1 ? "condition" : "conditions"} (${result.method}).`
          : `Not proven: ${result.reason}${boundedLine(result.bounded)}`;
        if (result.status === "conditional") {
          verdictDetail.append(conditionsOf(result.conditions));
          if (result.assumptions.length) verdictDetail.append(assumptionsOf(result.assumptions));
        }
      } catch (error) { verdict.textContent = error.message; }
    };
    body.append(
      h("h4", { class: "sp-heading", text: "Equivalent columns" }),
      list,
      h("div", { class: "sp-inline" }, left, right),
      h("div", { class: "sp-inline" }, pairs, h("label", { for: "equivalence-whole" }, whole, h("span", { text: "same rows" }))),
      h("div", { class: "sp-inline" },
        h("button", { type: "button", class: "toolbar-button", text: "Save", onclick: save }),
        h("button", { type: "button", class: "toolbar-button", text: "Compare tables", onclick: compare })),
      verdict, verdictDetail,
    );
    refresh().catch((error) => setStatus(error.message, true));

    const query = (label) => h("textarea", { class: "sp-input sp-query", rows: "6", spellcheck: "false", "aria-label": label, placeholder: label });
    const queryA = query("Query A");
    const queryB = query("Query B");
    const queryVerdict = h("p", { class: "sp-row-hint" });
    const queryDetail = h("div", { class: "sp-query-detail" });
    const prove = async () => {
      queryVerdict.textContent = "Comparing…";
      queryDetail.replaceChildren();
      try {
        const result = await repoCall("POST", "/api/prove-queries", { left: queryA.value, right: queryB.value, explain: true });
        queryVerdict.textContent = result.status === "proven_equivalent" ? "Equivalent."
          : result.status === "proven_conditionally" ? `Equivalent under ${result.conditions.length} ${result.conditions.length === 1 ? "condition" : "conditions"}.`
          : result.status === "not_equivalent" ? `Different results: ${result.reason}` : `Not proven: ${result.reason}${boundedLine(result.bounded)}`;
        if (result.status === "proven_conditionally") queryDetail.append(conditionsOf(result.conditions));
        if (result.except_when) queryDetail.append(h("p", { class: "sp-except" }, "Equivalent except when ", h("code", { class: "sp-except-sql", text: result.except_when.sql })));
        if (!result.counterexample && result.bounded && result.bounded.counterexample) {
          const found = Object.entries(result.bounded.counterexample.tables).map(([name, items]) => `${name}: ${items.map((row) => JSON.stringify(row)).join(" ")}`);
          queryDetail.append(h("pre", { class: "sp-pre", text: found.join("\n") }));
        }
        if (result.counterexample) {
          const rows = (items) => items.length ? items.map((row) => row.join(", ")).join(" | ") : "no rows";
          const tables = Object.entries(result.counterexample.tables).map(([name, items]) => `${name}: ${items.length ? items.map((row) => JSON.stringify(row)).join(" ") : "empty"}`);
          queryDetail.append(h("pre", { class: "sp-pre", text: [...tables, `A returns: ${rows(result.counterexample.left_rows)}`, `B returns: ${rows(result.counterexample.right_rows)}`].join("\n") }));
        }
        if (result.assumptions.length) queryDetail.append(assumptionsOf(result.assumptions));
      } catch (error) { queryVerdict.textContent = error.message; }
    };
    body.append(
      h("h4", { class: "sp-heading", text: "Compare queries" }),
      h("div", { class: "sp-inline sp-queries" }, queryA, queryB),
      h("div", { class: "sp-inline" }, h("button", { type: "button", class: "toolbar-button", text: "Prove equivalent", onclick: prove })),
      queryVerdict, queryDetail,
    );
  }

  async function renderDiagnostics(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Diagnostics" }),
      h("p", { class: "sp-lede", text: "Copy a report to paste into a chat or a bug report. It holds the KumoSQL, Python, git and operating system versions, your settings and the recent log. Repository, project, dataset, table, model and file names, folders, e-mail addresses and your user name are replaced by placeholders such as repo#1 and model#417, SQL text is left out and secrets are hidden. The same name keeps the same placeholder, so the lines can still be followed. The real names behind the placeholders stay in a private file in your local data folder; to look one up, run: python -m kumosql.ui --lookup model#417" }));
    const preview = h("textarea", { class: "sp-input diag-preview", readonly: "", rows: "14", spellcheck: "false", "aria-label": "Diagnostics report" });
    const copy = h("button", { type: "button", class: "toolbar-button", text: "Copy diagnostics" });
    const hint = h("p", { class: "sp-row-hint", text: "Look the report over before sending it. Start KumoSQL with --no-redact only for local debugging: then the log is left out of this report." });
    copy.addEventListener("click", async () => {
      setStatus("Collecting…");
      try {
        const data = await repoCall("GET", "/api/diagnostics");
        preview.value = data.text;
        try {
          await navigator.clipboard.writeText(data.text);
        } catch (clipboardError) {
          preview.focus();
          preview.select();
          if (!document.execCommand("copy")) throw clipboardError;
        }
        setStatus("Copied to the clipboard");
      } catch (error) {
        setStatus(preview.value ? "Select the text below and copy it" : error.message, !preview.value);
      }
    });
    body.append(h("div", { class: "repo-form" }, copy), hint, preview);
  }

  const RENDERERS = { diagnostics: renderDiagnostics, appearance: renderAppearance, formatting: renderFormatting, storage: renderStorage, repositories: renderRepositories, bigquery: renderBigQuery, datasources: renderDataSources, analysis: renderAnalysis, solver: renderSolver, scopes: renderScopes, catalogs: renderCatalogs, tagrules: renderTagRules };

  function show(id) {
    current = RENDERERS[id] ? id : "appearance";
    enabledRules = null;
    for (const link of dialog.querySelectorAll(".sp-nav button")) {
      link.setAttribute("aria-current", String(link.dataset.section === current));
    }
    const body = dialog.querySelector(".sp-body");
    body.replaceChildren();
    RENDERERS[current](body);
    body.scrollTop = 0;
    filter(dialog.querySelector(".sp-search input").value);
  }

  // Search narrows the sidebar to sections with a matching setting.
  const KEYWORDS = {
    appearance: "appearance theme light dark system colour color mode",
    diagnostics: "diagnostics copy report log bug support redacted versions version python os paste send help",
    storage: "storage local data folder directory clones cache path appdata home",
    repositories: "repositories repository dataform git connect ssh https branch refresh private remote url project",
    bigquery: "bigquery projects choose select project catalog browse tab billing project query cache hours lifetime",
    datasources: "data sources source query sql bigquery table populate cache fields columns applies to",
    scopes: "scopes scope rule rules filter condition submitter project dataset field limit active",
    analysis: "analysis lineage column tracing time limit seconds timeout slow model budget",
    solver: "solver prover proof prove equivalent equivalence z3 smt rewrite verification keys not null time limit",
    catalogs: "catalog catalogs owned owns team ownership tables views functions udf procedures dataform workbooks rule rules",
    tagrules: "tag tags rules rule label retired batch tagging dataset schema table view function udf procedure objects",
    formatting: `sql formatting sqlfluff configuration profile ${FORMAT_FIELDS.map((field) => `${field.label} ${field.hint}`).join(" ")}`.toLowerCase(),
  };
  function keywordsFor(section) {
    if (section !== "formatting" || !ruleCatalog) return KEYWORDS[section];
    const rules = ruleCatalog.map((rule) => `${rule.code} ${rule.name} ${rule.description}`).join(" ");
    return `${KEYWORDS.formatting} ${rules.toLowerCase()}`;
  }

  function filter(query) {
    const text = query.trim().toLowerCase();
    let first = null;
    for (const link of dialog.querySelectorAll(".sp-nav button")) {
      const match = !text || keywordsFor(link.dataset.section).includes(text);
      link.hidden = !match;
      if (match && !first) first = link.dataset.section;
    }
    for (const item of dialog.querySelectorAll(".sp-row")) {
      item.classList.toggle("is-match", Boolean(text) && item.dataset.search.includes(text));
    }
    // A search that only matches sqlfluff rules narrows the rule list as well.
    const ruleSearch = dialog.querySelector(".sp-rule-search");
    if (ruleSearch && !KEYWORDS.formatting.includes(text)) {
      ruleSearch.value = text;
      ruleSearch.dispatchEvent(new Event("input"));
    }
    return first;
  }

  function build() {
    const search = h("input", { type: "search", placeholder: "Search", "aria-label": "Search settings" });
    search.addEventListener("input", () => {
      const first = filter(search.value);
      if (first && !keywordsFor(current).includes(search.value.trim().toLowerCase())) show(first);
    });
    const nav = h("nav", { class: "sp-nav", "aria-label": "Settings sections" });
    for (const section of SECTIONS) {
      nav.append(h("button", { type: "button", "data-section": section.id, onclick: () => show(section.id) }, svg(section.icon), h("span", { text: section.label })));
    }
    dialog = h("dialog", { class: "settings-panel", "aria-label": "Settings" },
      h("aside", { class: "sp-side" },
        h("label", { class: "sp-search" }, svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'), search),
        h("p", { class: "sp-side-label", text: "Settings" }),
        nav,
        h("p", { class: "sp-side-foot", text: "Saved on this computer" })),
      h("div", { class: "sp-main" },
        h("div", { class: "sp-top" },
          h("span", { class: "sp-status", role: "status", "aria-live": "polite" }),
          h("button", { type: "button", class: "icon-button sp-close", "aria-label": "Close settings", onclick: () => dialog.close() }, svg('<path d="M6 6l12 12M18 6 6 18"/>'))),
        h("div", { class: "sp-body" })),
    );
    // Pages that show the connected project reload to pick up a changed repository.
    dialog.addEventListener("close", () => {
      if (reposChanged && document.getElementById("project-form")) location.reload();
      reposChanged = false;
    });
    // Close when clicking the dimmed backdrop, not the panel.
    dialog.addEventListener("mousedown", (event) => { if (event.target === dialog) dialog.close(); });
    // A focused search box would otherwise swallow Escape.
    dialog.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); dialog.close(); } });
    document.body.append(dialog);
  }

  async function load() {
    let format = null;
    try {
      ui = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    } catch {
      ui = {};
    }
    try {
      const response = await fetch("/api/settings");
      if (response.ok) {
        const settings = await response.json();
        if (settings.ui && Object.keys(settings.ui).length) ui = settings.ui;
        format = settings.format;
      }
    } catch {
      /* fall back to browser storage and defaults */
    }
    if (provider) ui = { ...ui, ...structuredClone(provider.getUi()) };
    if (!ruleCatalog) {
      try {
        const response = await fetch("/api/sqlfluff/rules");
        if (response.ok) ruleCatalog = await response.json();
      } catch {
        /* keep the free-text rule fields */
      }
    }
    const loaded = Array.isArray(ui.sqlfluffProfiles) ? ui.sqlfluffProfiles : [];
    profiles = loaded.filter((item) => item && typeof item.name === "string" && item.format && typeof item.id === "string");
    if (!profiles.length) profiles = [{ id: crypto.randomUUID(), name: "Default", format }];
    activeId = profiles.some((item) => item.id === ui.activeSqlfluffProfile) ? ui.activeSqlfluffProfile : profiles[0].id;
  }

  async function open(section = current) {
    if (!dialog) build();
    await load();
    setStatus("");
    dialog.querySelector(".sp-search input").value = "";
    show(section);
    if (!dialog.open) dialog.showModal();
    dialog.querySelector(`.sp-nav button[data-section="${current}"]`)?.focus();
  }

  window.KumoSettings = {
    open,
    // The sidebar's theme button: apply, then save like the Appearance section does.
    async setTheme(theme) {
      applyTheme(theme);
      await load();
      ui.theme = theme;
      saveUi();
    },
    // The workspace passes getUi (its current saved state) and onChange.
    register({ getUi, onChange }) {
      provider = { getUi };
      if (onChange) listeners.push(onChange);
    },
  };

  // Apply the saved theme on every page, including ones with no page script of
  // their own: the browser copy first so it shows at once, then the server's.
  try {
    applyTheme(JSON.parse(localStorage.getItem(STORAGE_KEY))?.theme);
  } catch {
    /* browser storage unavailable */
  }
  fetch("/api/settings")
    .then((response) => (response.ok ? response.json() : null))
    .then((settings) => { if (settings?.ui?.theme) applyTheme(settings.ui.theme); })
    .catch(() => {});

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-open-settings]");
    if (!trigger) return;
    event.preventDefault();
    open(trigger.dataset.openSettings || undefined);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "," && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      open();
    }
  });
})();
