/* The Codebook page: each codebook in full, and every use of a code in the study.
 *
 * The reader shows one recording; a codebook covers them all, so reviewing a code
 * -- reading everything filed under it, cleaning it up before it is agreed --
 * happens here. Text codes and video codes are separate codebooks and are shown
 * one at a time. The list on the left is Common, then yours, plus every other
 * coder's in collaborative mode, then Imported. The page on the right is one code: its
 * description, the themes holding it, and each quote or span that carries it,
 * with a link to that moment.
 *
 * The two panes scroll separately and each has a search box: the list matches
 * code names, the table matches quote and note text, and its Recording and Coder
 * headers filter rows. Searches ignore case and stay when another code is opened.
 *
 * Your own and common codes are edited with the same ⋯ menu as in the reader,
 * and their uses removed in bulk: for a text code that takes the code off the
 * chosen quotes; a span carries one code, so removing it deletes the span.
 * Common codes also show their History. Imported codes, read from study diaries,
 * can only be moved to common. Other coders' codes are read-only.
 */

import { $, api, escapeHtml, formatTime } from "./util.js";
import { applyStoredTheme, bindThemeToggle, notify } from "./chrome.js";
import { COMMON, coderTag, currentMode, ensureCoder, isCommon, isImported, isMine, mountCoderControls, nameOf, visibleNow } from "./coder.js";
import { codeEditor, codeMenu } from "./codestrip.js";
import { openMoveDialog, returnToCoders } from "./commondialog.js";

const esc = escapeHtml;
const params = new URLSearchParams(location.search);

const state = {
  kind: ["video", "conversation"].includes(params.get("kind")) ? params.get("kind") : "text",
  codeId: params.get("code"),
  codes: [],
  colors: [],
  applications: [],
  selected: new Set(),
  editing: null,
  history: [],
  //: The themes, of those on screen in this mode, that hold the open code.
  themes: [],
  //: Saved quotes with no code, of those on screen in this mode.
  uncoded: [],
  //: The list search: codes whose name contains it, ignoring case.
  listQuery: "",
  //: The table search: rows whose quote or note contains it, ignoring case.
  pageQuery: "",
  //: Values unticked in the table's Recording and Coder filters; kept when another code is opened.
  hidden: { recording: new Set(), coder: new Set() },
  //: The column whose filter checklist is open, if any.
  filterOpen: null,
};
//: The list group a code or quote falls in: Common, yours, or another coder's.
const groupOf = (item) => (isCommon(item) ? "__common" : isImported(item) ? "__imported" : isMine(item) ? "__mine" : item.coder);
//: Stands in for a code id when the open page is one group's unlabelled quotes.
const UNCODED = "uncoded:";
const uncodedGroup = () => (state.codeId?.startsWith(UNCODED) ? state.codeId.slice(UNCODED.length) : null);
//: Your own codes and common ones can be edited here; other coders' are read-only.
const editable = (code) => isMine(code) || isCommon(code);
//: Whether a code has a ⋯ menu: an editable one, or an imported one, whose menu only moves it to common.
const hasMenu = (code) => editable(code) || isImported(code);

const el = { meta: $("meta"), notices: $("notices"), list: $("code-list"), page: $("code-page") };
const noun = () => `${state.kind} code`;
//: Text and conversation codes sit on saved passages; video codes sit on spans.
const onPassages = () => state.kind !== "video";
const uses = (code) => (state.kind === "text" ? code.quote_count : state.kind === "conversation" ? code.highlight_count : code.span_count) || 0;
//: What one use of the open kind's code is called.
const useWord = () => (state.kind === "text" ? "quote" : state.kind === "conversation" ? "highlight" : "span");
//: The turn cell of a conversation row, which has no time.
const turnText = (r) => (r.start_turn == null ? "" : r.end_turn == null || r.end_turn === r.start_turn ? `turn ${r.start_turn}` : `turn ${r.start_turn} → ${r.end_turn}`);
//: The reader link of a row: the highlight for conversation, the time otherwise.
const rowLink = (r) => `/reader?recording=${encodeURIComponent(r.recording_id)}&${state.kind === "conversation" ? `conv=${encodeURIComponent(r.highlight_id)}` : `t=${r.start_time ?? r.start}`}`;
const rowCells = (r) => (state.kind === "conversation" ? turnText(r) : formatTime(r.start_time ?? r.start));
//: Orders rows by recording title, then time, or for conversation rows by place in the chat (`position` from the server).
const byPosition = (a, b) => (a.position?.[0] ?? Infinity) - (b.position?.[0] ?? Infinity) || (a.position?.[1] ?? 0) - (b.position?.[1] ?? 0);
const byRecording = (a, b) => a.recording_title.localeCompare(b.recording_title) || (state.kind === "conversation" ? byPosition(a, b) : (a.start_time ?? a.start) - (b.start_time ?? b.start));
const current = () => state.codes.find((c) => c.id === state.codeId);
//: The rows the open page lists, before any search or filter.
const pageRows = () => (uncodedGroup() ? state.uncoded.filter((q) => groupOf(q) === uncodedGroup()) : state.applications);

//: Who is behind a row: for a common one, each coder who put this code on it (`coders` from the server).
const codersOf = (r) => (r.coders?.length ? r.coders : [r.coder]);
const coderCell = (r) => codersOf(r).map(coderTag).join(" ");
//: A row's note; a common row keeps one per coder, each labelled with its coder.
const noteCell = (r) => (r.notes?.length ? r.notes.map((n) => `${coderTag(n.coder)} ${esc(n.note)}`).join(" · ") : esc(r.note || ""));
const noteText = (r) => [r.note || "", ...(r.notes || []).map((n) => n.note || "")].join(" ");

//: The table's column filters: the value each row is filtered on, and the name shown in the checklist.
//: The Coder filter lists each combination of coders, so rows both coded can be told from rows one coded.
const FILTERS = {
  recording: { label: "Recording", value: (r) => r.recording_id, name: (r) => r.recording_title },
  coder: { label: "Coder", value: (r) => codersOf(r).join("+"), name: (r) => codersOf(r).map(nameOf).join(" + ") },
};

//: The rows that pass the table search and every column filter.
function visibleRows(rows) {
  const q = state.pageQuery.toLowerCase();
  return rows.filter(
    (r) =>
      (!q || (r.text || "").toLowerCase().includes(q) || noteText(r).toLowerCase().includes(q)) &&
      Object.entries(FILTERS).every(([key, f]) => !state.hidden[key].has(f.value(r)))
  );
}

//: A filterable column header; its checklist lists the values among this page's rows.
function filterHead(key, rows) {
  const f = FILTERS[key];
  const values = new Map(rows.map((r) => [f.value(r), f.name(r)]));
  const narrowed = [...values.keys()].some((v) => state.hidden[key].has(v));
  const open = state.filterOpen === key;
  const menu = open
    ? `<div class="cbook__filter-menu">
        <span class="cbook__filter-acts"><button type="button" data-filter-all="${key}">Select all</button><button type="button" data-filter-none="${key}">Clear</button></span>
        ${[...values]
          .sort((a, b) => a[1].localeCompare(b[1]))
          .map(([v, name]) => `<label><input type="checkbox" data-filter="${key}" value="${esc(v)}" ${state.hidden[key].has(v) ? "" : "checked"}> ${esc(name)}</label>`)
          .join("")}
      </div>`
    : "";
  return `<th class="cbook__filter"><button class="cbook__filter-btn${narrowed ? " cbook__filter-btn--on" : ""}" type="button" data-filter-open="${key}" aria-expanded="${open}">${f.label} ▾</button>${menu}</th>`;
}

//: The count line: every row, or how many of them the search and filters leave.
function factsLine(all, shown, word) {
  const recordings = new Set(shown.map((r) => r.recording_id)).size;
  const count = shown.length === all.length ? `<b>${all.length}</b> ${word}${all.length === 1 ? "" : "s"}` : `<b>${shown.length}</b> of ${all.length} ${word}${all.length === 1 ? "" : "s"} shown`;
  return `<p class="cbook__facts">${count} in <b>${recordings}</b> recording${recordings === 1 ? "" : "s"}</p>`;
}

function remember() {
  const url = new URL(location.href);
  url.searchParams.set("kind", state.kind);
  if (state.codeId) url.searchParams.set("code", state.codeId);
  else url.searchParams.delete("code");
  history.replaceState(null, "", url);
}

async function loadCodes() {
  const book = await api(`/api/library/${state.kind}-codebook`);
  state.codes = book.codes;
  state.colors = book.colors;
  state.uncoded = state.kind === "text" ? (await api("/api/library/text-codebook/uncoded")).quotes.filter(visibleNow)
    : state.kind === "conversation" ? (await api("/api/library/conversation-codebook/uncoded")).highlights.filter(visibleNow) : [];
  const group = uncodedGroup();
  const gone = group
    ? !onPassages() || !(group.startsWith("__") || currentMode() === "collaborative")
    : !visibleNow(current() || {});
  if (state.codeId && gone) state.codeId = null;
  renderList();
  await loadPage();
}

//: Counts page loads, so replies for a code already clicked past are dropped.
let pageLoads = 0;

async function loadPage() {
  const load = ++pageLoads;
  state.selected = new Set();
  state.applications = [];
  remember();
  state.history = [];
  state.themes = [];
  if (current()) {
    const base = `/api/library/${state.kind}-codebook/${state.codeId}`;
    const ref = `${state.kind}:${state.codeId}`;
    const [applications, themes, history] = await Promise.all([
      api(`${base}/applications`),
      api(`/api/library/themes?mode=${currentMode()}`),
      isCommon(current()) ? api(`${base}/history`) : { entries: [] },
    ]);
    if (load !== pageLoads) return;
    state.applications = applications.applications;
    state.themes = themes.themes.filter((t) => (t.refs || []).includes(ref));
    state.history = history.entries;
  }
  renderPage();
}

/* ---------------------------------------------------------------- list -- */

function renderList() {
  const shown = state.codes.filter(visibleNow);
  const q = state.listQuery.toLowerCase();
  const matches = (name) => name.toLowerCase().includes(q);
  const listed = shown.filter((code) => matches(code.name));
  const groups = new Map();
  for (const code of listed) {
    const key = groupOf(code);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(code);
  }
  // Each group lists its coder's unlabelled quotes first; yours shows even at none.
  const uncodedCount = new Map();
  for (const quote of state.uncoded) uncodedCount.set(groupOf(quote), (uncodedCount.get(groupOf(quote)) || 0) + 1);
  if (onPassages()) uncodedCount.set("__mine", uncodedCount.get("__mine") || 0);
  if (!matches(`Unlabelled ${useWord()}s`)) uncodedCount.clear();
  for (const key of uncodedCount.keys()) if (!groups.has(key)) groups.set(key, []);
  const order = [
    "__common",
    "__mine",
    ...[...groups.keys()].filter((k) => !k.startsWith("__")).sort((a, b) => nameOf(a).localeCompare(nameOf(b))),
    "__imported",
  ];
  el.meta.textContent = `${shown.length} ${noun()}${shown.length === 1 ? "" : "s"} · ${currentMode()}`;

  const uncodedRow = (key) =>
    uncodedCount.has(key)
      ? `<div class="cbook__row${state.codeId === UNCODED + key ? " cbook__row--on" : ""}">
          <button class="cbook__pick" type="button" data-pick="${esc(UNCODED + key)}">
            <i class="strip__swatch"></i>
            <span class="cbook__name">Unlabelled ${useWord()}s</span>
            <span class="strip__count">${uncodedCount.get(key)}</span>
          </button>
        </div>`
      : "";
  const empty = shown.length
    ? listed.length || uncodedCount.size ? "" : `<p class="empty">No ${noun()}s match.</p>`
    : `<p class="empty">No ${noun()}s yet. ${
        state.kind === "text" ? "Add one to a quote in the reader." : state.kind === "conversation" ? "Add one to a highlight in the reader." : "Press i and o on a video to code a span."
      }</p>`;
  el.list.innerHTML = order
    .filter((key) => groups.has(key))
    .map(
      (key) => `
      <div class="cbook__group">${key === "__common" ? "Common ✓" : key === "__imported" ? "Imported ⇣" : key === "__mine" ? "Yours · not yet common" : esc(nameOf(key))}</div>
      ${uncodedRow(key)}
      ${groups
        .get(key)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map(
          (code) => `
        <div class="cbook__row${code.id === state.codeId ? " cbook__row--on" : ""}">
          <button class="cbook__pick" type="button" data-pick="${code.id}">
            <i class="strip__swatch vc--${esc(code.color)}"></i>
            <span class="cbook__name">${esc(code.name)}${isMine(code) ? "" : coderTag(code.coder)}</span>
            <span class="strip__count">${uses(code)}</span>
            ${code.description ? `<span class="cbook__desc">${esc(code.description)}</span>` : ""}
          </button>
          ${hasMenu(code) ? `<button class="strip__menu-btn cbook__more" type="button" data-menu="${code.id}" aria-label="Actions for ${esc(code.name)}" aria-haspopup="menu">⋯</button>` : ""}
        </div>`
        )
        .join("")}`
    )
    .join("") + empty;
}

/* ---------------------------------------------------------------- page -- */

function renderUncoded(group) {
  const quotes = pageRows().sort(byRecording);
  const word = useWord();
  const shown = visibleRows(quotes);
  const rows = shown
    .map((q) => {
      const link = rowLink(q);
      return `<tr>
        <td>${esc(q.recording_title)}</td>
        <td class="cbook__time">${rowCells(q)}</td>
        <td><span class="cbook__quote">“${esc(q.text)}”</span>${q.speaker ? `<span class="cbook__speaker">${esc(q.speaker)}</span>` : ""}</td>
        <td>${noteCell(q)}</td>
        <td>${coderCell(q)}</td>
        <td><a class="cbook__jump" href="${link}" target="_blank" rel="noopener">${state.kind === "conversation" ? "open" : "▶ open"}</a></td>
      </tr>`;
    })
    .join("");
  el.page.innerHTML = `
    <header class="cbook__head">
      <i class="strip__swatch cbook__swatch"></i>
      <div class="cbook__title">
        <h2>Unlabelled ${word}s ${group === "__mine" ? "" : coderTag(group === "__common" ? COMMON : group)}</h2>
        <p>Saved ${word}s that have no code yet. Add a code to one in the reader and it leaves this list.</p>
      </div>
    </header>
    ${factsLine(quotes, shown, word)}
    ${
      quotes.length
        ? `<table class="cbook__table"><thead><tr>${filterHead("recording", quotes)}<th>${state.kind === "conversation" ? "Turn" : "Time"}</th><th>${state.kind === "conversation" ? "Highlight" : "Quote"}</th><th>Note</th>${filterHead("coder", quotes)}<th></th></tr></thead><tbody>${rows}</tbody></table>
          ${shown.length ? "" : `<p class="empty">No ${word}s match.</p>`}`
        : `<p class="empty">Every saved ${word} has a code.</p>`
    }`;
}

function renderPage() {
  if (uncodedGroup()) return renderUncoded(uncodedGroup());
  const code = current();
  if (!code) {
    el.page.innerHTML = `<p class="empty">Choose a ${noun()} to see what it means and every place it is used.</p>`;
    return;
  }
  const mine = editable(code);
  const all = state.applications.sort(byRecording);
  const shown = visibleRows(all);
  // A row the search or filters hide is deselected, so bulk removal only acts on rows in view.
  const shownRefs = new Set(shown.map((a) => a.ref));
  state.selected = new Set([...state.selected].filter((ref) => shownRefs.has(ref)));
  const word = useWord();
  const rows = shown
    .map((a) => {
      const link = rowLink(a);
      const what = onPassages()
        ? `<span class="cbook__quote">“${esc(a.text)}”</span>${a.speaker ? `<span class="cbook__speaker">${esc(a.speaker)}</span>` : ""}`
        : `<span class="cbook__len">${Math.round((a.end - a.start) * 10) / 10}s</span>`;
      return `<tr>
        <td>${mine ? `<input type="checkbox" data-ref="${esc(a.ref)}" ${state.selected.has(a.ref) ? "checked" : ""} aria-label="Select">` : ""}</td>
        <td>${esc(a.recording_title)}</td>
        <td class="cbook__time">${rowCells(a)}</td>
        <td>${what}</td>
        <td>${noteCell(a)}</td>
        <td>${coderCell(a)}</td>
        <td><a class="cbook__jump" href="${link}" target="_blank" rel="noopener">${state.kind === "conversation" ? "open" : "▶ open"}</a></td>
      </tr>`;
    })
    .join("");

  const removeLabel = onPassages() ? `Take the code off selected ${word}s` : "Delete selected spans";
  el.page.innerHTML = `
    <header class="cbook__head">
      <i class="strip__swatch cbook__swatch vc--${esc(code.color)}"></i>
      <div class="cbook__title">
        <h2>${esc(code.name)} ${mine ? "" : coderTag(code.coder)}</h2>
        <p>${code.description ? esc(code.description) : `<span class="cbook__none">No description yet.</span>`}</p>
      </div>
      ${hasMenu(code) ? `<span class="cbook__head-menu"><button class="strip__menu-btn cbook__more" type="button" data-menu="${code.id}" aria-label="Actions for ${esc(code.name)}" aria-haspopup="menu">⋯</button></span>` : `<span class="cbook__readonly">${esc(nameOf(code.coder))}'s code · read-only</span>`}
    </header>
    ${state.editing ? codeEditor(state.editing, { colors: state.colors, withKeys: state.kind === "video", noun: noun() }, state.codes.map((c) => ({ ...c, total: uses(c) }))) : ""}
    ${factsLine(all, shown, word)}
    <p class="cbook__facts">In themes: ${
      state.themes.length
        ? state.themes.map((t) => `<a href="/themes">${esc(t.title || "Untitled")}</a> ${coderTag(t.coder)}`).join(", ")
        : "none yet"
    }</p>
    ${
      all.length
        ? `${mine ? `<div class="cbook__toolbar"><label><input type="checkbox" id="select-all" ${state.selected.size && state.selected.size === shown.length ? "checked" : ""}> Select all</label>
            <span class="cbook__count">${state.selected.size} selected</span>
            <button class="btn btn--danger" type="button" id="remove-selected" ${state.selected.size ? "" : "disabled"}>${removeLabel}</button></div>` : ""}
          <table class="cbook__table"><thead><tr><th></th>${filterHead("recording", all)}<th>${state.kind === "conversation" ? "Turn" : "Time"}</th><th>${state.kind === "text" ? "Quote" : state.kind === "conversation" ? "Highlight" : "Span"}</th><th>Note</th>${filterHead("coder", all)}<th></th></tr></thead><tbody>${rows}</tbody></table>
          ${shown.length ? "" : `<p class="empty">No ${word}s match.</p>`}`
        : `<p class="empty">Nothing carries this code yet.</p>`
    }
    ${isCommon(code) ? historyPanel() : ""}`;
  if (location.hash === "#history") el.page.querySelector("#history")?.scrollIntoView();
}

const HISTORY_WORDS = {
  moved: "moved it to common",
  returned: "returned it to its coders",
  edited: "edited it",
  deleted: "deleted it",
  removed: "removed some of its uses",
};

function historyPanel() {
  const rows = state.history
    .map((e) => {
      const what = HISTORY_WORDS[e.action] || e.action;
      const detail = e.action === "moved"
        ? `${e.moved} ${useWord()}${e.moved === 1 ? "" : "s"}${e.duplicates ? `, ${e.duplicates} combined` : ""}${e.from && e.from !== e.name ? `, from “${esc(e.from)}”` : ""}`
        : e.action === "edited"
        ? `${(e.changes || []).map((f) => (f === "color" ? "colour" : f)).join(", ")}${e.from ? `, from “${esc(e.from)}”` : ""}`
        : e.removed != null ? `${e.removed} removed`
        : e.returned != null ? `${e.returned} returned` : "";
      return `<li><time>${esc(new Date(e.at).toLocaleString())}</time> ${coderTag(e.coder)} ${esc(nameOf(e.coder))} ${what}${detail ? ` <span class="cbook__speaker">${detail}</span>` : ""}</li>`;
    })
    .join("");
  return `<section class="cbook__history" id="history"><h3>History</h3>${rows ? `<ol>${rows}</ol>` : `<p class="empty">No history yet.</p>`}</section>`;
}

/* ------------------------------------------------------------- actions -- */

async function act(action, code, value) {
  const base = `/api/library/${state.kind}-codebook/${code.id}`;
  try {
    if (action === "move") {
      const done = await openMoveDialog({ kind: state.kind, code, common: state.codes.filter(isCommon) });
      if (!done) return;
      state.codeId = null;
      notify(el.notices, `Moved “${code.name}” to common.`);
    }
    if (action === "return") {
      const done = await returnToCoders(state.kind, code);
      if (!done) return;
      state.codeId = null;
      notify(el.notices, `Returned ✓ ${code.name} to its coders.`);
    }
    const body = { rename: { name: value }, describe: { description: value }, color: { color: value }, key: { key: value } }[action];
    if (body) await api(base, { method: "PATCH", body });
    if (action === "merge") {
      const result = await api(`${base}/merge`, { method: "POST", body: { into: value } });
      state.codeId = value;
      notify(el.notices, `Merged “${code.name}” (${result.moved} moved).`);
    }
    if (action === "delete") {
      await api(base, { method: "DELETE" });
      state.codeId = null;
    }
  } catch (error) {
    notify(el.notices, error.message, { kind: "warn" });
  }
  state.editing = null;
  // Any codebook change may be a common one, which the header counts until pushed.
  window.dispatchEvent(new CustomEvent("commonchange"));
  await loadCodes();
}

async function removeSelected() {
  const code = current();
  const refs = [...state.selected];
  const what = onPassages() ? `Take “${code.name}” off ${refs.length} ${useWord()}${refs.length === 1 ? "" : "s"}? The ${useWord()}s stay.` : `Delete ${refs.length} span${refs.length === 1 ? "" : "s"} of “${code.name}”?`;
  if (!confirm(what)) return;
  try {
    const result = await api(`/api/library/${state.kind}-codebook/${code.id}/applications/remove`, { method: "POST", body: { refs } });
    notify(el.notices, `${result.removed} removed.`);
  } catch (error) {
    notify(el.notices, error.message, { kind: "warn" });
  }
  await loadCodes();
}

document.addEventListener("click", (event) => {
  // A click outside the open filter checklist closes it, without redrawing the rows under the click.
  if (state.filterOpen && !event.target.closest(".cbook__filter")) {
    state.filterOpen = null;
    el.page.querySelector(".cbook__filter-menu")?.remove();
    el.page.querySelectorAll("[data-filter-open]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }
  const filterOpen = event.target.closest("[data-filter-open]");
  if (filterOpen) {
    state.filterOpen = state.filterOpen === filterOpen.dataset.filterOpen ? null : filterOpen.dataset.filterOpen;
    renderPage();
    return;
  }
  const filterAll = event.target.closest("[data-filter-all], [data-filter-none]");
  if (filterAll) {
    const key = filterAll.dataset.filterAll || filterAll.dataset.filterNone;
    for (const row of pageRows()) state.hidden[key][filterAll.dataset.filterAll ? "delete" : "add"](FILTERS[key].value(row));
    renderPage();
    return;
  }
  const kind = event.target.closest("[data-kind]");
  if (kind && kind.dataset.kind !== state.kind) {
    state.kind = kind.dataset.kind;
    state.codeId = null;
    state.editing = null;
    document.querySelectorAll("[data-kind]").forEach((b) => b.setAttribute("aria-pressed", String(b === kind)));
    loadCodes();
    return;
  }
  const pick = event.target.closest("[data-pick]");
  if (pick) {
    state.codeId = pick.dataset.pick;
    state.editing = null;
    el.page.scrollTop = 0;
    renderList();
    loadPage();
    return;
  }
  const menuBtn = event.target.closest("[data-menu]");
  document.querySelectorAll(".strip__menu").forEach((m) => m !== menuBtn?.nextElementSibling && m.remove());
  if (menuBtn) {
    if (menuBtn.nextElementSibling?.classList.contains("strip__menu")) menuBtn.nextElementSibling.remove();
    else menuBtn.insertAdjacentHTML("afterend", codeMenu(state.codes.find((c) => c.id === menuBtn.dataset.menu), { withKeys: state.kind === "video" }));
    return;
  }
  const action = event.target.closest("[data-act]");
  if (action && (action.dataset.act === "move" || action.dataset.act === "return")) {
    document.querySelectorAll(".strip__menu").forEach((m) => m.remove());
    act(action.dataset.act, state.codes.find((c) => c.id === action.dataset.id));
    return;
  }
  if (action) {
    state.codeId = action.dataset.id;
    state.editing = { action: action.dataset.act, id: action.dataset.id };
    renderList();
    loadPage().then(() => el.page.querySelector(".strip__edit [name=value]")?.focus());
    return;
  }
  if (event.target.closest('[data-strip="cancel"]')) {
    state.editing = null;
    renderPage();
    return;
  }
  if (event.target.id === "remove-selected") removeSelected();
});

document.addEventListener("change", (event) => {
  if (event.target.id === "select-all") {
    state.selected = event.target.checked ? new Set(visibleRows(state.applications).map((a) => a.ref)) : new Set();
    renderPage();
  } else if (event.target.dataset.filter) {
    const hidden = state.hidden[event.target.dataset.filter];
    if (event.target.checked) hidden.delete(event.target.value);
    else hidden.add(event.target.value);
    renderPage();
  } else if (event.target.dataset.ref) {
    if (event.target.checked) state.selected.add(event.target.dataset.ref);
    else state.selected.delete(event.target.dataset.ref);
    renderPage();
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "list-search") {
    state.listQuery = event.target.value;
    renderList();
  } else if (event.target.id === "page-search") {
    state.pageQuery = event.target.value;
    renderPage();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest(".strip__edit");
  if (!form) return;
  event.preventDefault();
  act(form.dataset.action, current(), form.elements.value?.value);
});

applyStoredTheme();
bindThemeToggle($("theme-toggle"));
document.querySelectorAll("[data-kind]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.kind === state.kind)));
ensureCoder()
  .then(() => {
    mountCoderControls($("theme-toggle").parentElement, { onSync: loadCodes });
    window.addEventListener("modechange", loadCodes);
    window.addEventListener("coderchange", loadCodes);
    return loadCodes();
  })
  .catch((error) => notify(el.notices, `Could not load the codebook: ${error.message}`, { kind: "warn" }));
