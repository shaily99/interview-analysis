/* The Codebook page: each codebook in full, and every use of a code in the study.
 *
 * The reader shows one recording; a codebook covers them all, so reviewing a code
 * -- reading everything filed under it, cleaning it up before it is agreed --
 * happens here. Text codes and video codes are separate codebooks and are shown
 * one at a time. The list on the left is Common, then yours, plus every other
 * coder's in collaborative mode. The page on the right is one code: its
 * description, the themes holding it, and each quote or span that carries it,
 * with a link to that moment.
 *
 * Your own and common codes are edited with the same ⋯ menu as in the reader,
 * and their uses removed in bulk: for a text code that takes the code off the
 * chosen quotes; a span carries one code, so removing it deletes the span.
 * Common codes also show their History. Other coders' codes are read-only.
 */

import { $, api, escapeHtml, formatTime } from "./util.js";
import { applyStoredTheme, bindThemeToggle, notify } from "./chrome.js";
import { coderTag, currentMode, ensureCoder, isCommon, isMine, mountCoderControls, nameOf, visibleNow } from "./coder.js";
import { codeEditor, codeMenu } from "./codestrip.js";
import { openMoveDialog, returnToCoders } from "./commondialog.js";

const esc = escapeHtml;
const params = new URLSearchParams(location.search);

const state = {
  kind: params.get("kind") === "video" ? "video" : "text",
  codeId: params.get("code"),
  codes: [],
  colors: [],
  applications: [],
  selected: new Set(),
  editing: null,
  history: [],
  //: The themes, of those on screen in this mode, that hold the open code.
  themes: [],
};
//: Your own codes and common ones can be edited here; other coders' are read-only.
const editable = (code) => isMine(code) || isCommon(code);

const el = { meta: $("meta"), notices: $("notices"), list: $("code-list"), page: $("code-page") };
const noun = () => (state.kind === "text" ? "text code" : "video code");
const uses = (code) => (state.kind === "text" ? code.quote_count : code.span_count) || 0;
const current = () => state.codes.find((c) => c.id === state.codeId);

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
  if (state.codeId && !visibleNow(current() || {})) state.codeId = null;
  renderList();
  await loadPage();
}

async function loadPage() {
  state.selected = new Set();
  state.applications = [];
  remember();
  state.history = [];
  state.themes = [];
  if (current()) {
    state.applications = (await api(`/api/library/${state.kind}-codebook/${state.codeId}/applications`)).applications;
    const ref = `${state.kind}:${state.codeId}`;
    state.themes = (await api(`/api/library/themes?mode=${currentMode()}`)).themes.filter((t) => (t.refs || []).includes(ref));
    if (isCommon(current())) {
      state.history = (await api(`/api/library/${state.kind}-codebook/${state.codeId}/history`)).entries;
    }
  }
  renderPage();
}

/* ---------------------------------------------------------------- list -- */

function renderList() {
  const shown = state.codes.filter(visibleNow);
  const groups = new Map();
  for (const code of shown) {
    const key = isCommon(code) ? "__common" : isMine(code) ? "__mine" : code.coder;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(code);
  }
  const order = [
    "__common",
    "__mine",
    ...[...groups.keys()].filter((k) => !k.startsWith("__")).sort((a, b) => nameOf(a).localeCompare(nameOf(b))),
  ];
  el.meta.textContent = `${shown.length} ${noun()}${shown.length === 1 ? "" : "s"} · ${currentMode()}`;

  if (!shown.length) {
    el.list.innerHTML = `<p class="empty">No ${noun()}s yet. ${
      state.kind === "text" ? "Add one to a quote in the reader." : "Press i and o on a video to code a span."
    }</p>`;
    return;
  }
  el.list.innerHTML = order
    .filter((key) => groups.has(key))
    .map(
      (key) => `
      <div class="cbook__group">${key === "__common" ? "Common ✓" : key === "__mine" ? "Yours · not yet common" : esc(nameOf(key))}</div>
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
          ${editable(code) ? `<button class="strip__menu-btn cbook__more" type="button" data-menu="${code.id}" aria-label="Actions for ${esc(code.name)}" aria-haspopup="menu">⋯</button>` : ""}
        </div>`
        )
        .join("")}`
    )
    .join("");
}

/* ---------------------------------------------------------------- page -- */

function renderPage() {
  const code = current();
  if (!code) {
    el.page.innerHTML = `<p class="empty">Choose a ${noun()} to see what it means and every place it is used.</p>`;
    return;
  }
  const mine = editable(code);
  const recordings = new Set(state.applications.map((a) => a.recording_id));
  const rows = state.applications
    .sort((a, b) => a.recording_title.localeCompare(b.recording_title) || (a.start_time ?? a.start) - (b.start_time ?? b.start))
    .map((a) => {
      const at = a.start_time ?? a.start;
      const link = state.kind === "text"
        ? `/reader?recording=${encodeURIComponent(a.recording_id)}&t=${at}`
        : `/code?recording=${encodeURIComponent(a.recording_id)}&t=${at}`;
      const what = state.kind === "text"
        ? `<span class="cbook__quote">“${esc(a.text)}”</span>${a.speaker ? `<span class="cbook__speaker">${esc(a.speaker)}</span>` : ""}`
        : `<span class="cbook__len">${Math.round((a.end - a.start) * 10) / 10}s</span>${a.note ? `<span class="cbook__speaker">${esc(a.note)}</span>` : ""}`;
      return `<tr>
        <td>${mine ? `<input type="checkbox" data-ref="${esc(a.ref)}" ${state.selected.has(a.ref) ? "checked" : ""} aria-label="Select">` : ""}</td>
        <td>${esc(a.recording_title)}</td>
        <td class="cbook__time">${formatTime(at)}</td>
        <td>${what}</td>
        <td>${coderTag(a.coder)}</td>
        <td><a class="cbook__jump" href="${link}" target="_blank" rel="noopener">▶ open</a></td>
      </tr>`;
    })
    .join("");

  const removeLabel = state.kind === "text" ? "Take the code off selected quotes" : "Delete selected spans";
  el.page.innerHTML = `
    <header class="cbook__head">
      <i class="strip__swatch cbook__swatch vc--${esc(code.color)}"></i>
      <div class="cbook__title">
        <h2>${esc(code.name)} ${mine ? "" : coderTag(code.coder)}</h2>
        <p>${code.description ? esc(code.description) : `<span class="cbook__none">No description yet.</span>`}</p>
      </div>
      ${mine ? `<span class="cbook__head-menu"><button class="strip__menu-btn cbook__more" type="button" data-menu="${code.id}" aria-label="Actions for ${esc(code.name)}" aria-haspopup="menu">⋯</button></span>` : `<span class="cbook__readonly">${esc(nameOf(code.coder))}'s code · read-only</span>`}
    </header>
    ${state.editing ? codeEditor(state.editing, { colors: state.colors, withKeys: state.kind === "video", noun: noun() }, state.codes.map((c) => ({ ...c, total: uses(c) }))) : ""}
    <p class="cbook__facts"><b>${state.applications.length}</b> ${state.kind === "text" ? "quote" : "span"}${state.applications.length === 1 ? "" : "s"} in <b>${recordings.size}</b> recording${recordings.size === 1 ? "" : "s"}</p>
    <p class="cbook__facts">In themes: ${
      state.themes.length
        ? state.themes.map((t) => `<a href="/themes">${esc(t.title || "Untitled")}</a> ${coderTag(t.coder)}`).join(", ")
        : "none yet"
    }</p>
    ${
      state.applications.length
        ? `${mine ? `<div class="cbook__toolbar"><label><input type="checkbox" id="select-all" ${state.selected.size && state.selected.size === state.applications.length ? "checked" : ""}> Select all</label>
            <span class="cbook__count">${state.selected.size} selected</span>
            <button class="btn btn--danger" type="button" id="remove-selected" ${state.selected.size ? "" : "disabled"}>${removeLabel}</button></div>` : ""}
          <table class="cbook__table"><thead><tr><th></th><th>Recording</th><th>Time</th><th>${state.kind === "text" ? "Quote" : "Span"}</th><th>Coder</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="empty">Nothing carries this code yet.</p>`
    }
    ${isCommon(code) ? historyPanel() : ""}`;
  if (location.hash === "#history") el.page.querySelector("#history")?.scrollIntoView();
}

const HISTORY_WORDS = { moved: "moved it to common", returned: "returned it to its coders" };

function historyPanel() {
  const rows = state.history
    .map((e) => {
      const what = HISTORY_WORDS[e.action] || e.action;
      const detail = e.action === "moved"
        ? `${e.moved} ${state.kind === "text" ? "quote" : "span"}${e.moved === 1 ? "" : "s"}${e.duplicates ? `, ${e.duplicates} combined` : ""}${e.from && e.from !== e.name ? `, from “${esc(e.from)}”` : ""}`
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
  const what = state.kind === "text" ? `Take “${code.name}” off ${refs.length} quote${refs.length === 1 ? "" : "s"}? The quotes stay.` : `Delete ${refs.length} span${refs.length === 1 ? "" : "s"} of “${code.name}”?`;
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
    state.selected = event.target.checked ? new Set(state.applications.map((a) => a.ref)) : new Set();
    renderPage();
  } else if (event.target.dataset.ref) {
    if (event.target.checked) state.selected.add(event.target.dataset.ref);
    else state.selected.delete(event.target.dataset.ref);
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
    mountCoderControls($("theme-toggle").parentElement, { onRefresh: loadCodes });
    window.addEventListener("modechange", loadCodes);
    window.addEventListener("coderchange", loadCodes);
    return loadCodes();
  })
  .catch((error) => notify(el.notices, `Could not load the codebook: ${error.message}`, { kind: "warn" }));
