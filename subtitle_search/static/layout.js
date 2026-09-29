/* Panes that resize and collapse, and remember both.
 *
 * The reader shows the video with its code rows, the transcript, and the text
 * codes side by side, because coding what someone does means checking it against
 * what they say at the same moment. How much room each deserves depends on the
 * task, so every divider drags (or moves with the arrow keys; double-click resets
 * the panes either side), and every pane collapses to a labelled strip that
 * reopens it. There are no preset layouts to remember: the arrangement you leave
 * is the one you get back.
 *
 * Markup: a `.panes` container holds `[data-pane]` panes and `.pane-stack`
 * columns (`data-stack`) of panes, with `.pane-div` dividers between them
 * (`.pane-div--h` between panes stacked vertically). A pane's head has a
 * `[data-collapse]` button. A stack whose panes are all collapsed collapses too,
 * to a single strip.
 */

import { recall, remember } from "./util.js";

const MIN = 80;
const STEP = 24;

const nameOf = (box) => box.dataset.pane || box.dataset.stack;
const isBox = (el) => el?.matches?.("[data-pane], .pane-stack");

export function initPanes(root, { storageKey, onChange }) {
  const boxes = [...root.querySelectorAll("[data-pane], .pane-stack")];
  const panes = boxes.filter((b) => b.dataset.pane);
  const stacks = boxes.filter((b) => b.classList.contains("pane-stack"));
  const defaults = new Map(boxes.map((b) => [nameOf(b), b.style.flexGrow || "1"]));

  let saved = {};
  try {
    saved = JSON.parse(recall(storageKey, "{}")) || {};
  } catch (_) {
    saved = {};
  }
  for (const box of boxes) {
    if (saved.grow?.[nameOf(box)]) box.style.flexGrow = saved.grow[nameOf(box)];
  }
  for (const pane of panes) pane.hidden = Boolean(saved.hidden?.includes(nameOf(pane)));

  // A strip in place of each pane and stack, shown while it is collapsed.
  for (const box of boxes) {
    const rail = document.createElement("button");
    rail.type = "button";
    rail.className = "pane-rail";
    rail.dataset.rail = nameOf(box);
    rail.textContent = box.dataset.label || nameOf(box);
    rail.title = `Show ${rail.textContent}`;
    box.before(rail);
  }
  const railOf = (box) => root.querySelector(`[data-rail="${nameOf(box)}"]`);

  const save = () =>
    remember(
      storageKey,
      JSON.stringify({
        grow: Object.fromEntries(boxes.map((b) => [nameOf(b), b.style.flexGrow || "1"])),
        hidden: panes.filter((p) => p.hidden).map(nameOf),
      })
    );

  const sync = () => {
    for (const stack of stacks) {
      stack.hidden = [...stack.querySelectorAll(":scope > [data-pane]")].every((p) => p.hidden);
    }
    for (const box of boxes) {
      const inHiddenStack = box.dataset.pane && box.closest(".pane-stack")?.hidden;
      railOf(box).hidden = !box.hidden || Boolean(inHiddenStack);
    }
    // A divider shows only between two boxes that are both showing, and only once.
    for (const container of [root, ...stacks]) {
      let seen = false;
      let waiting = null;
      for (const child of container.children) {
        if (child.classList.contains("pane-div")) {
          child.hidden = true;
          if (seen && !waiting) waiting = child;
        } else if (isBox(child) && !child.hidden) {
          if (waiting) waiting.hidden = false;
          waiting = null;
          seen = true;
        }
      }
    }
    save();
    onChange?.();
  };

  /** Open a pane, and the stack it sits in. */
  const show = (name) => {
    const pane = root.querySelector(`[data-pane="${name}"]`);
    if (pane && pane.hidden) {
      pane.hidden = false;
      sync();
    }
  };

  root.addEventListener("click", (event) => {
    const collapse = event.target.closest("[data-collapse]");
    if (collapse && root.contains(collapse)) {
      collapse.closest("[data-pane]").hidden = true;
      sync();
      return;
    }
    const rail = event.target.closest("[data-rail]");
    if (rail) {
      const box = root.querySelector(`[data-pane="${rail.dataset.rail}"], [data-stack="${rail.dataset.rail}"]`);
      const inside = box.dataset.stack ? [...box.querySelectorAll(":scope > [data-pane]")] : [box];
      for (const pane of inside) pane.hidden = false;
      sync();
    }
  });

  // Dragging a divider trades space between the showing boxes either side of it.
  const neighbours = (divider) => {
    let before = divider.previousElementSibling;
    while (before && !(isBox(before) && !before.hidden)) before = before.previousElementSibling;
    let after = divider.nextElementSibling;
    while (after && !(isBox(after) && !after.hidden)) after = after.nextElementSibling;
    return [before, after];
  };
  const showing = (divider) => [...divider.parentElement.children].filter((c) => isBox(c) && !c.hidden);

  const resize = (divider, delta) => {
    const vertical = divider.classList.contains("pane-div--h");
    const [a, b] = neighbours(divider);
    if (!a || !b) return;
    const measure = (el) => (vertical ? el.getBoundingClientRect().height : el.getBoundingClientRect().width);
    const visible = showing(divider);
    const sizes = new Map(visible.map((el) => [el, measure(el)]));
    // Sizes become pixels; collapsed boxes are rescaled to match, so one
    // reopened later gets back the same share it had.
    const growSum = visible.reduce((sum, el) => sum + (Number(el.style.flexGrow) || 1), 0);
    const pxSum = [...sizes.values()].reduce((sum, px) => sum + px, 0);
    const scale = growSum ? pxSum / growSum : 1;
    for (const el of divider.parentElement.children) {
      if (isBox(el) && el.hidden) el.style.flexGrow = String(Math.round((Number(el.style.flexGrow) || 1) * scale));
    }
    const total = sizes.get(a) + sizes.get(b);
    const na = Math.min(total - MIN, Math.max(MIN, sizes.get(a) + delta));
    sizes.set(a, na);
    sizes.set(b, total - na);
    for (const [el, px] of sizes) el.style.flexGrow = String(Math.round(px));
    onChange?.();
  };

  for (const divider of root.querySelectorAll(".pane-div")) {
    const vertical = divider.classList.contains("pane-div--h");
    divider.tabIndex = 0;
    divider.setAttribute("role", "separator");
    divider.setAttribute("aria-orientation", vertical ? "horizontal" : "vertical");
    divider.setAttribute("aria-label", "Resize: drag, or use the arrow keys; double-click to reset");
    divider.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      let last = vertical ? event.clientY : event.clientX;
      divider.setPointerCapture(event.pointerId);
      divider.classList.add("pane-div--drag");
      const move = (e) => {
        const now = vertical ? e.clientY : e.clientX;
        resize(divider, now - last);
        last = now;
      };
      const up = () => {
        divider.classList.remove("pane-div--drag");
        divider.removeEventListener("pointermove", move);
        divider.removeEventListener("pointerup", up);
        divider.removeEventListener("pointercancel", up);
        save();
      };
      divider.addEventListener("pointermove", move);
      divider.addEventListener("pointerup", up);
      divider.addEventListener("pointercancel", up);
    });
    divider.addEventListener("keydown", (event) => {
      const keys = vertical ? { ArrowUp: -STEP, ArrowDown: STEP } : { ArrowLeft: -STEP, ArrowRight: STEP };
      if (!(event.key in keys)) return;
      event.preventDefault();
      event.stopPropagation();
      resize(divider, keys[event.key]);
      save();
    });
    divider.addEventListener("dblclick", () => {
      // Every box in the row, collapsed ones too, so they stay on one scale.
      for (const el of divider.parentElement.children) if (isBox(el)) el.style.flexGrow = defaults.get(nameOf(el));
      save();
      onChange?.();
    });
  }

  sync();
  return { show, sync };
}
