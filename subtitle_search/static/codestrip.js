/* The codebook strip: a coder's codes as chips above the list they filter.
 *
 * One strip for text codes and one for video codes, drawn by the same code so
 * the two codebooks behave alike while staying separate. The strip shows the
 * codes in use first (common, yours, then other coders' in collaborative mode), each with
 * a count; a long codebook is cut short behind "+N more", which opens the rest in
 * place with a filter box. Clicking a chip filters the list below it.
 *
 * Each of your own codes has a ⋯ menu: rename, describe, recolour, merge into
 * another of your codes, move to common, and delete once nothing uses it. A
 * common code's menu has History and Return to coders in place of the merge and
 * the move. Other coders' codes are read-only and have no menu.
 */

import { escapeHtml } from "./util.js";
import { coderTag, isCommon, isMine } from "./coder.js";

//: Actions that act at once rather than opening a small form.
const IMMEDIATE = new Set(["move", "return"]);

//: How many chips show before the rest fold behind "+N more".
const COLLAPSED_LIMIT = 8;

const esc = escapeHtml;

// An open ⋯ menu closes when you click anywhere outside the chip it belongs to.
document.addEventListener("click", (event) => {
  if (!event.target.closest(".strip__chip")) document.querySelectorAll(".strip__menu").forEach((m) => m.remove());
});

/**
 * @param host      element the strip draws into
 * @param options.codes     [{id, name, color, coder, count}]
 * @param options.filter    code id currently filtering the list, or null
 * @param options.colors    colour names a code may take
 * @param options.withKeys  whether codes carry a shortcut key (video codes)
 * @param options.noun      "text code" or "video code", for labels
 * @param options.empty     what to say when there are no codes yet
 * @param options.kind      "text" or "video", for the link to the code page
 * @param options.onFilter  (id | null) -> void
 * @param options.onAction  (action, code, value) -> Promise; actions are
 *                          rename, describe, color, key, merge, delete
 */
export function renderStrip(host, options) {
  const { codes, filter, noun } = options;
  const state = (host._strip ||= { expanded: false, query: "", editing: null });
  host._stripOptions = options;

  // Common codes first, then yours, then other coders'; each by most used.
  const rank = (c) => (isCommon(c) ? 0 : isMine(c) ? 1 : 2);
  const ordered = [...codes].sort(
    (a, b) => rank(a) - rank(b) || b.count - a.count || a.name.localeCompare(b.name)
  );
  const query = state.query.trim().toLowerCase();
  const matching = state.expanded && query ? ordered.filter((c) => c.name.toLowerCase().includes(query)) : ordered;
  const shown = state.expanded ? matching : ordered.slice(0, COLLAPSED_LIMIT);
  const hidden = ordered.length - shown.length;

  if (!codes.length) {
    host.innerHTML = `<p class="strip__empty">${esc(options.empty || `No ${noun}s yet. Add one to a quote to start your codebook.`)}</p>`;
    return;
  }

  host.innerHTML =
    `<div class="strip">` +
    (state.expanded
      ? `<input class="strip__filter" type="search" placeholder="Filter ${noun}s" aria-label="Filter ${noun}s" value="${esc(state.query)}">`
      : "") +
    shown.map((code) => chip(code, filter)).join("") +
    (state.expanded
      ? `<button class="strip__more" type="button" data-strip="less">Show fewer</button>`
      : hidden > 0
        ? `<button class="strip__more" type="button" data-strip="more">+${hidden} more</button>`
        : "") +
    `</div>` +
    (state.editing ? codeEditor(state.editing, options, ordered) : "");

  bind(host);
  if (state.expanded && state.focusFilter) {
    const input = host.querySelector(".strip__filter");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    state.focusFilter = false;
  }
}

function chip(code, filter) {
  const mine = isMine(code);
  const editable = mine || isCommon(code);
  return (
    `<span class="strip__chip${filter === code.id ? " strip__chip--on" : ""}" title="${esc(code.description || code.name)}">` +
    `<button class="strip__pick" type="button" data-pick="${code.id}" aria-pressed="${filter === code.id}">` +
    `<i class="strip__swatch vc--${esc(code.color)}"></i>${esc(code.name)}` +
    (mine ? "" : coderTag(code.coder)) +
    `<span class="strip__count">${code.count}</span></button>` +
    (editable ? `<button class="strip__menu-btn" type="button" data-menu="${code.id}" aria-label="Actions for ${esc(code.name)}" aria-haspopup="menu">⋯</button>` : "") +
    `</span>`
  );
}

/** The code page of a code on the Codebook page. */
export const codePageUrl = (kind, code) => `/codebook?kind=${kind}&code=${encodeURIComponent(code.id)}`;

/** The ⋯ menu of your own code, or of a common one. Shared by the strip and the Codebook page. */
export function codeMenu(code, options) {
  const common = isCommon(code);
  const page = options.kind ? codePageUrl(options.kind, code) : null;
  return `<span class="strip__menu" role="menu">
    ${page ? `<a role="menuitem" class="strip__menu-link" href="${page}">Open code page</a>` : ""}
    <button type="button" role="menuitem" data-act="rename" data-id="${code.id}">Rename</button>
    <button type="button" role="menuitem" data-act="describe" data-id="${code.id}">Edit description</button>
    <button type="button" role="menuitem" data-act="color" data-id="${code.id}">Change colour</button>
    ${options.withKeys && !common ? `<button type="button" role="menuitem" data-act="key" data-id="${code.id}">Set key</button>` : ""}
    ${common && page ? `<a role="menuitem" class="strip__menu-link" href="${page}#history">History</a>` : ""}
    <hr>
    ${
      common
        ? `<button type="button" role="menuitem" data-act="return" data-id="${code.id}">Return to coders…</button>`
        : `<button type="button" role="menuitem" class="primary" data-act="move" data-id="${code.id}">Move to common…</button>
    <button type="button" role="menuitem" data-act="merge" data-id="${code.id}">Merge into another of my codes…</button>`
    }
    <hr>
    <button type="button" role="menuitem" class="danger" data-act="delete" data-id="${code.id}"
      ${uses(code) ? `title="Remove it from its ${uses(code)} uses first, or merge it"` : ""}>Delete…</button>
  </span>`;
}

//: Uses across the whole study, which is what deleting a code depends on; the
//: count on a chip is only what this recording shows.
const uses = (code) => code.total ?? code.count ?? 0;

/** The small form for one ⋯ action. Shared by the strip and the Codebook page. */
export function codeEditor({ action, id }, options, ordered) {
  const code = ordered.find((c) => c.id === id);
  if (!code) return "";
  const title = { rename: "Rename", describe: "Describe", color: "Colour of", key: "Key for", merge: "Merge", delete: "Delete" }[action];
  let field = "";
  if (action === "rename") field = `<input name="value" type="text" value="${esc(code.name)}" aria-label="New name">`;
  if (action === "describe") field = `<textarea name="value" rows="2" aria-label="Description" placeholder="What counts as this code">${esc(code.description || "")}</textarea>`;
  if (action === "key") field = `<input name="value" type="text" maxlength="1" value="${esc(code.key || "")}" aria-label="Key" placeholder="one key">`;
  if (action === "color")
    field = `<select name="value" aria-label="Colour">${options.colors
      .map((c) => `<option value="${c}"${c === code.color ? " selected" : ""}>${c}</option>`)
      .join("")}</select>`;
  if (action === "merge") {
    const targets = ordered.filter((c) => c.id !== id && isMine(c));
    if (!targets.length) field = `<span class="strip__note">You have no other ${options.noun} to merge into.</span>`;
    else
      field = `<span class="strip__note">Every use of “${esc(code.name)}” becomes</span><select name="value" aria-label="Merge into">${targets
        .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`)
        .join("")}</select><span class="strip__note">and “${esc(code.name)}” is removed.</span>`;
  }
  if (action === "delete")
    field = uses(code)
      ? `<span class="strip__note">“${esc(code.name)}” is on ${uses(code)} ${uses(code) === 1 ? "item" : "items"} across the study. Remove it from them first, or merge it into another code.</span>`
      : `<span class="strip__note">Delete “${esc(code.name)}” from your codebook?</span>`;
  const canSubmit = !(action === "delete" && uses(code)) && !(action === "merge" && field.includes("no other"));
  return `<form class="strip__edit" data-id="${id}" data-action="${action}">
    <span class="strip__edit-title">${title} <b>${esc(code.name)}</b></span>${field}
    <span class="strip__edit-actions"><button class="btn" type="button" data-strip="cancel">Cancel</button>
    ${canSubmit ? `<button class="btn ${action === "delete" ? "btn--danger" : "btn--primary"}" type="submit">${action === "delete" ? "Delete" : "Save"}</button>` : ""}</span>
  </form>`;
}

function bind(host) {
  const state = host._strip;
  const options = () => host._stripOptions;

  host.querySelector(".strip__filter")?.addEventListener("input", (event) => {
    state.query = event.target.value;
    state.focusFilter = true;
    renderStrip(host, options());
  });

  host.onclick = (event) => {
    const pick = event.target.closest("[data-pick]");
    if (pick) {
      const id = pick.dataset.pick;
      options().onFilter(options().filter === id ? null : id);
      return;
    }
    const more = event.target.closest("[data-strip]")?.dataset.strip;
    if (more === "more" || more === "less") {
      state.expanded = more === "more";
      state.query = "";
      state.focusFilter = state.expanded;
      renderStrip(host, options());
      return;
    }
    if (more === "cancel") {
      state.editing = null;
      renderStrip(host, options());
      return;
    }
    const menuBtn = event.target.closest("[data-menu]");
    if (menuBtn) {
      const open = menuBtn.nextElementSibling?.classList.contains("strip__menu");
      host.querySelectorAll(".strip__menu").forEach((m) => m.remove());
      if (!open) {
        const code = options().codes.find((c) => c.id === menuBtn.dataset.menu);
        menuBtn.insertAdjacentHTML("afterend", codeMenu(code, options()));
      }
      return;
    }
    if (event.target.closest(".strip__menu-link")) return;
    const act = event.target.closest("[data-act]");
    if (act && IMMEDIATE.has(act.dataset.act)) {
      host.querySelectorAll(".strip__menu").forEach((m) => m.remove());
      const code = options().codes.find((c) => c.id === act.dataset.id);
      options().onAction(act.dataset.act, code);
      return;
    }
    if (act) {
      state.editing = { action: act.dataset.act, id: act.dataset.id };
      renderStrip(host, options());
      host.querySelector(".strip__edit [name=value]")?.focus();
    }
  };

  host.querySelector(".strip__edit")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const code = options().codes.find((c) => c.id === form.dataset.id);
    const value = form.elements.value?.value;
    state.editing = null;
    await options().onAction(form.dataset.action, code, value);
  });
}
