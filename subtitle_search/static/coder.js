/* Who is coding, and whose work is on screen.
 *
 * Several people code one study from a shared folder. Each browser codes as one
 * of them, chosen on a login screen that lists the coders already in the study or
 * adds a new one. There is no password: the name labels work, it does not guard
 * it. The choice is remembered, and every write carries it (util.api).
 *
 * The mode switch decides whose quotes and spans are shown. Independent shows
 * your own and common ones, so you code without seeing anyone else's decisions.
 * Collaborative shows everyone's, labelled with their initials, and other
 * people's work is read-only. Switching fires a `modechange` event on window so
 * each view redraws without a reload.
 */

import { $, api, CODER_KEY, escapeHtml, recall, remember } from "./util.js";

const MODE_KEY = "subtitle-search:mode";

const state = { coders: [], me: null };

export const currentCoder = () => state.me;
export const currentMode = () => (recall(MODE_KEY, "independent") === "collaborative" ? "collaborative" : "independent");
export const isMine = (item) => Boolean(state.me) && item?.coder === state.me.id;
//: What the server calls agreed codes and their quotes and spans, wherever an item names its coder.
export const COMMON = "common";
export const isCommon = (item) => item?.coder === COMMON;
/** Whether an item belongs on screen in the current mode. Common work is agreed, so always. */
export const visibleNow = (item) => currentMode() === "collaborative" || isMine(item) || isCommon(item);
export const initialsOf = (coderId) =>
  coderId === COMMON ? "✓" : state.coders.find((c) => c.id === coderId)?.initials || "?";
export const nameOf = (coderId) =>
  coderId === COMMON ? "Common" : state.coders.find((c) => c.id === coderId)?.name || "Unknown coder";

export async function loadCoders() {
  state.coders = (await api("/api/coders")).coders;
  const stored = recall(CODER_KEY);
  state.me = state.coders.find((c) => c.id === stored) || null;
  return state.coders;
}

/** Resolve once a coder is chosen, showing the login screen if none is yet. */
export async function ensureCoder() {
  await loadCoders();
  if (state.me) return state.me;
  return openLogin();
}

function choose(coder) {
  state.me = coder;
  remember(CODER_KEY, coder.id);
  syncBadge();
}

/* -------------------------------------------------------------- login -- */

function openLogin({ editing = false } = {}) {
  return new Promise((resolve) => {
    document.getElementById("coder-login")?.remove();
    const overlay = document.createElement("div");
    overlay.id = "coder-login";
    overlay.className = "login";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "login-title");
    document.body.appendChild(overlay);

    const me = state.me;
    let picked = editing ? "edit" : state.coders.length ? state.coders[0].id : "new";

    const draw = () => {
      const rows = editing
        ? ""
        : state.coders
            .map(
              (c) => `<label class="login__row"><input type="radio" name="who" value="${c.id}" ${picked === c.id ? "checked" : ""}>
                 <span class="login__name">${escapeHtml(c.name)}</span><span class="login__initials">${escapeHtml(c.initials)}</span></label>`
            )
            .join("");
      const showFields = editing || picked === "new";
      overlay.innerHTML = `
        <form class="login__card" novalidate>
          <h2 id="login-title" class="login__title">${editing ? "Your name and initials" : "Who is coding?"}</h2>
          ${editing ? "" : `<p class="login__lead">Your quotes and codes are labelled with this. Everyone in the study folder can see who made what.</p>`}
          <div class="login__list">${rows}
            ${editing ? "" : `<label class="login__row"><input type="radio" name="who" value="new" ${picked === "new" ? "checked" : ""}><span class="login__name">New coder…</span></label>`}
          </div>
          <div class="login__fields" ${showFields ? "" : "hidden"}>
            <label>Name <input id="login-name" type="text" autocomplete="name" value="${escapeHtml(editing ? me.name : "")}"></label>
            <label>Initials <input id="login-initials" type="text" maxlength="6" value="${escapeHtml(editing ? me.initials : "")}"></label>
            <p class="login__hint" id="login-hint">Shown on your codes, like <b>SB</b>.</p>
          </div>
          <p class="login__error" id="login-error" role="alert" hidden></p>
          <div class="login__actions">
            ${editing || state.me ? `<button class="btn" type="button" data-cancel>Cancel</button>` : ""}
            <button class="btn btn--primary" type="submit">${editing ? "Save" : "Start coding"}</button>
          </div>
        </form>`;
      bind();
    };

    const clash = (initials) =>
      state.coders.find((c) => c.initials.toLowerCase() === initials.toLowerCase() && (!editing || c.id !== me.id));

    const check = () => {
      const hint = $("login-hint");
      const initials = $("login-initials")?.value.trim() || "";
      const other = initials && clash(initials);
      const submit = overlay.querySelector('[type="submit"]');
      if (other) {
        hint.innerHTML = `⚠ ${escapeHtml(other.name)} already uses <b>${escapeHtml(other.initials)}</b>. Try a longer form.`;
        submit.disabled = true;
      } else {
        hint.innerHTML = "Shown on your codes, like <b>SB</b>.";
        submit.disabled = (picked === "new" || editing) && (!initials || !$("login-name").value.trim());
      }
    };

    let initialsTouched = editing;
    const bind = () => {
      const name = $("login-name");
      const initials = $("login-initials");
      overlay.querySelectorAll('[name="who"]').forEach((radio) =>
        radio.addEventListener("change", () => {
          picked = radio.value;
          draw();
          if (picked === "new") $("login-name").focus();
        })
      );
      if (name) {
        name.addEventListener("input", async () => {
          if (!initialsTouched) {
            const params = new URLSearchParams({ name: name.value, ...(editing ? { coder: me.id } : {}) });
            initials.value = (await api(`/api/coders/suggest?${params}`)).initials;
          }
          check();
        });
        initials.addEventListener("input", () => {
          initialsTouched = true;
          check();
        });
        check();
      }
      overlay.querySelector("[data-cancel]")?.addEventListener("click", () => {
        overlay.remove();
        resolve(state.me);
      });
      overlay.querySelector("form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const error = $("login-error");
        try {
          let coder;
          if (editing) {
            coder = (await api(`/api/coders/${me.id}`, { method: "PATCH", body: { name: name.value, initials: initials.value } })).coder;
          } else if (picked === "new") {
            coder = (await api("/api/coders", { method: "POST", body: { name: name.value, initials: initials.value } })).coder;
          } else {
            coder = state.coders.find((c) => c.id === picked);
          }
          await loadCoders();
          choose(state.coders.find((c) => c.id === coder.id) || coder);
          overlay.remove();
          resolve(state.me);
          window.dispatchEvent(new CustomEvent("coderchange"));
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      });
    };

    draw();
    overlay.querySelector('input:checked, input[type="text"]')?.focus();
  });
}

/* ------------------------------------------------------- header controls -- */

/**
 * Put the coder badge, the mode switch and Refresh into a page header.
 *
 * @param host       the header's actions container; controls go before #theme-toggle
 * @param withMode   whether this page follows the mode switch
 * @param onRefresh  called after the server has re-read the folder
 */
export function mountCoderControls(host, { withMode = true, onRefresh } = {}) {
  const bar = document.createElement("span");
  bar.className = "coderbar";
  bar.innerHTML =
    (withMode
      ? `<span class="seg" role="group" aria-label="Whose work to show">
           <button type="button" data-mode="independent">Independent</button>
           <button type="button" data-mode="collaborative">Collaborative</button>
         </span>`
      : "") +
    `<span class="coderbar__pending" id="common-pending" hidden></span>
     <button class="btn" type="button" id="refresh-btn" title="Read what collaborators have synced into the folder, and push your common changes">↻ Refresh</button>
     <span class="coderbar__who">
       <button class="coderbar__badge" type="button" id="coder-badge" aria-haspopup="menu" aria-expanded="false"></button>
       <span class="coderbar__menu" id="coder-menu" role="menu" hidden>
         <button type="button" role="menuitem" data-coder="edit">Change name or initials…</button>
         <button type="button" role="menuitem" data-coder="switch">Code as someone else…</button>
       </span>
     </span>`;
  host.insertBefore(bar, host.querySelector("#theme-toggle"));

  const syncMode = () =>
    bar.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === currentMode())));
  syncMode();
  bar.querySelectorAll("[data-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.mode === currentMode()) return;
      remember(MODE_KEY, b.dataset.mode);
      syncMode();
      window.dispatchEvent(new CustomEvent("modechange"));
    })
  );

  $("refresh-btn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/refresh", { method: "POST" });
      await loadCoders();
      syncBadge();
      await onRefresh?.();
      await refreshPending();
      if (result.claimed) showStatus(`${result.claimed} returned item${result.claimed === 1 ? "" : "s"} added to your codes.`);
    } catch (error) {
      showStatus(`Refresh failed: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });

  const badge = $("coder-badge");
  const menu = $("coder-menu");
  badge.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    badge.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".coderbar__who")) {
      menu.hidden = true;
      badge.setAttribute("aria-expanded", "false");
    }
  });
  menu.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-coder]")?.dataset.coder;
    if (!action) return;
    menu.hidden = true;
    await openLogin({ editing: action === "edit" });
  });
  syncBadge();
  refreshPending();
  window.addEventListener("commonchange", refreshPending);
}

function syncBadge() {
  const badge = $("coder-badge");
  if (!badge || !state.me) return;
  badge.innerHTML = `<b>${escapeHtml(state.me.initials)}</b>${escapeHtml(state.me.name)}`;
  badge.title = `Coding as ${state.me.name}`;
}

/** A small label for another coder's work: their initials, full name on hover; ✓ for common. */
export function coderTag(coderId) {
  return `<em class="who${coderId === COMMON ? " who--common" : ""}" title="${escapeHtml(nameOf(coderId))}">${escapeHtml(initialsOf(coderId))}</em>`;
}

/* ------------------------------------------------ common changes waiting -- */

/** A short message beside Refresh, for what the last Refresh did or why it failed. */
function showStatus(message) {
  const host = $("common-pending");
  if (!host) return;
  host.hidden = false;
  host.textContent = message;
}

/** Show how many of your common changes the shared files lack, and any sync conflicts. */
export async function refreshPending() {
  const host = $("common-pending");
  if (!host || !state.me) return;
  try {
    const status = await api("/api/common/status");
    const parts = [];
    if (status.pending) parts.push(`${status.pending} change${status.pending === 1 ? "" : "s"} not pushed`);
    if (status.conflicts.length) parts.push(`⚠ ${status.conflicts.length} sync conflict${status.conflicts.length === 1 ? "" : "s"}`);
    host.textContent = parts.join(" · ");
    host.title = status.conflicts.length
      ? `The sync client made conflict copies: ${status.conflicts.join(", ")}. Compare them with the shared files by hand; nothing was lost, since every change is also in its author's own folder.`
      : "Press Refresh to write your common changes into the shared files.";
    host.hidden = !parts.length;
  } catch (_) {
    host.hidden = true;
  }
}
