/* Moving a code to common, and giving a common code back to its coders.
 *
 * Moving is the step that turns one coder's code into an agreed one, so the
 * dialog asks for what that needs: that the code has been discussed and is
 * final, and a description of what counts as it (the coder's own description is
 * offered, to confirm it still holds). The code becomes a new common code, or is
 * merged into one that exists; when its name is already common, the choice is to
 * merge or to rename. Before anything is written the dialog shows, from a dry
 * run, how many quotes or spans will move and how many are exact duplicates of
 * another coder's that will be combined.
 */

import { api, CODER_KEY, escapeHtml, recall } from "./util.js";

const esc = escapeHtml;

/**
 * @param kind      "text" or "video"
 * @param code      your code to move
 * @param common    the common codes of that kind, for merging into
 * @returns a promise of the move's result, or null if cancelled
 */
export function openMoveDialog({ kind, code, common }) {
  return new Promise((resolve) => {
    document.getElementById("move-dialog")?.remove();
    const clash = common.find((c) => c.name.toLowerCase() === code.name.toLowerCase());
    const state = { into: clash ? clash.id : "", rename: "", final: false };
    const overlay = document.createElement("div");
    overlay.id = "move-dialog";
    overlay.className = "login";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "move-title");
    document.body.appendChild(overlay);
    const noun = kind === "text" ? "quote" : "span";

    const close = (result) => {
      overlay.remove();
      resolve(result);
    };

    const body = () => {
      const merging = Boolean(state.into);
      const target = common.find((c) => c.id === state.into);
      return {
        final: state.final,
        description: overlay.querySelector("#move-description").value,
        ...(merging ? { into: state.into } : {}),
        ...(!merging && state.rename.trim() ? { name: state.rename.trim() } : {}),
        _target: target,
      };
    };

    const preview = async () => {
      const out = overlay.querySelector("#move-summary");
      const { _target, ...payload } = body();
      try {
        const s = await api(`/api/library/${kind}-codebook/${code.id}/move?dry_run=1`, {
          method: "POST",
          body: { ...payload, final: true },
        });
        const where = _target ? `✓ ${esc(_target.name)}` : `✓ ${esc(state.rename.trim() || code.name)}`;
        out.innerHTML =
          `<span><b>${s.moved} ${noun}${s.moved === 1 ? "" : "s"}</b> in ${s.recordings} recording${s.recordings === 1 ? "" : "s"} move to ${where}.</span>` +
          (s.duplicates ? `<span><b>${s.duplicates}</b> ${s.duplicates === 1 ? "is an exact duplicate" : "are exact duplicates"} of another coder's and will be combined, credited to both.</span>` : "") +
          (kind === "text" && s.quotes_deleted ? `<span>${s.quotes_deleted} of your quotes carry no other code and will be removed; their notes go with them to common.</span>` : "") +
          (kind === "text" ? `<span>Other codes on your quotes stay as they are.</span>` : "") +
          `<span>Undo later with ⋯ → Return to coders.</span>`;
      } catch (error) {
        out.textContent = error.message;
      }
    };

    const draw = () => {
      const target = common.find((c) => c.id === state.into);
      overlay.innerHTML = `
        <form class="login__card move" novalidate>
          <h2 id="move-title" class="login__title">Move “${esc(code.name)}” to common</h2>
          ${
            clash
              ? `<div class="move__warn"><b>✓ ${esc(clash.name)}</b> is already a common code.
                   <label><input type="radio" name="clash" value="merge" ${state.into === clash.id ? "checked" : ""}> Merge into ✓ ${esc(clash.name)}</label>
                   <label><input type="radio" name="clash" value="rename" ${state.into ? "" : "checked"}> Rename mine first
                     <input type="text" id="move-rename" value="${esc(state.rename)}" placeholder="new name"></label></div>`
              : `<fieldset class="move__as"><legend>Move it as</legend>
                   <label><input type="radio" name="as" value="new" ${state.into ? "" : "checked"}> A new common code, “${esc(code.name)}”</label>
                   <label><input type="radio" name="as" value="merge" ${state.into ? "checked" : ""} ${common.length ? "" : "disabled"}> Merged into
                     <select id="move-into" ${common.length ? "" : "disabled"}>${common
                       .map((c) => `<option value="${c.id}" ${c.id === state.into ? "selected" : ""}>✓ ${esc(c.name)}</option>`)
                       .join("")}</select></label></fieldset>`
          }
          <label class="move__check"><input type="checkbox" id="move-final" ${state.final ? "checked" : ""}> This code has been discussed and is final.</label>
          <label class="move__desc">Description${target ? ` of ✓ ${esc(target.name)}` : ""}
            <textarea id="move-description" rows="3">${esc(target ? target.description || "" : code.description || "")}</textarea>
            <span class="login__hint">${
              target
                ? "This is the common code's current description; edit it if merging changes what the code means."
                : code.description
                  ? "Your code already has a description. Check it still holds before moving."
                  : "Say what counts as this code, for everyone coding with it."
            }</span></label>
          <div class="move__summary" id="move-summary">Counting…</div>
          <p class="login__error" id="move-error" role="alert" hidden></p>
          <div class="login__actions">
            <button class="btn" type="button" data-cancel>Cancel</button>
            <button class="btn btn--primary" type="submit" ${state.final ? "" : "disabled"}>${target ? `Merge into ✓ ${esc(target.name)}` : "Move to common"}</button>
          </div>
        </form>`;
      bind();
      preview();
    };

    const bind = () => {
      overlay.querySelector("[data-cancel]").addEventListener("click", () => close(null));
      overlay.querySelector("#move-final").addEventListener("change", (e) => {
        state.final = e.target.checked;
        overlay.querySelector('[type="submit"]').disabled = !state.final;
      });
      overlay.querySelectorAll('[name="as"], [name="clash"]').forEach((radio) =>
        radio.addEventListener("change", () => {
          if (radio.value === "new" || radio.value === "rename") state.into = "";
          else state.into = clash ? clash.id : overlay.querySelector("#move-into")?.value || "";
          draw();
        })
      );
      overlay.querySelector("#move-into")?.addEventListener("change", (e) => {
        state.into = e.target.value;
        draw();
      });
      overlay.querySelector("#move-rename")?.addEventListener("input", (e) => {
        state.rename = e.target.value;
      });
      overlay.querySelector("#move-rename")?.addEventListener("change", preview);
      overlay.querySelector("form").addEventListener("keydown", (e) => e.stopPropagation());
      overlay.querySelector("form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const error = overlay.querySelector("#move-error");
        const { _target, ...payload } = body();
        try {
          const response = await fetchMove(kind, code.id, payload);
          if (response.clash) {
            error.textContent = `✓ ${response.clash.name} is already a common code. Merge into it, or rename yours.`;
            error.hidden = false;
            return;
          }
          window.dispatchEvent(new CustomEvent("commonchange"));
          close(response);
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      });
    };

    draw();
    overlay.querySelector("#move-final").focus();
  });
}

/** The move itself; a name clash comes back as a 409 carrying the common code. */
async function fetchMove(kind, codeId, payload) {
  const response = await fetch(`/api/library/${kind}-codebook/${codeId}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Coder": recall(CODER_KEY) || "" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 409) return { clash: data.code };
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

/** Give a common code back to its coders, after saying what that does. */
export async function returnToCoders(kind, code) {
  const noun = kind === "text" ? "quotes" : "spans";
  if (
    !confirm(
      `Return ✓ ${code.name} to its coders? Each coder gets their own ${noun} back under an independent code called “${code.name}”, and the common code is removed. Coders other than you receive theirs when they next press Refresh.`
    )
  )
    return null;
  const result = await api(`/api/library/${kind}-codebook/${code.id}/return`, { method: "POST" });
  window.dispatchEvent(new CustomEvent("commonchange"));
  return result;
}

