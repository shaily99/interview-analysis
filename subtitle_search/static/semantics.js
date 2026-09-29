/* Three views that read the corpus rather than your codebook.
 *
 * Map     - every quote placed by what it says. Clusters here are formed by the
 *           language, so they can disagree with your themes; where they do is
 *           either a theme you missed or a distinction you decided not to make.
 *           Dots take the colour of the first theme holding one of the quote's
 *           codes. Draw a loop around a group to make a theme from its codes.
 * Graph   - text codes pulled together by the quotes they share. The same numbers as
 *           the Pairs list, arranged so the shape of the codebook is visible:
 *           what clumps, what dangles, what sits on its own.
 * Signals - whether new interviews are still turning up new text codes, and which
 *           quotes refuse to group with anything.
 *
 * Nothing here files anything. Every grouping is a proposal a person accepts.
 */

import { $, api, escapeHtml, formatTime } from "./util.js";
import { currentMode } from "./coder.js";

const PALETTE = ["amber", "teal", "rose", "violet", "sage"];

let ctx = null;
let semantics = null;
let selection = new Set();

export function initSemantics(context) {
  ctx = context;
  bindMap();
  bindGraph();
}

/* ------------------------------------------------------------ fetching -- */

async function ensureSemantics({ force = false } = {}) {
  const neural = $("map-neural").checked;
  if (semantics && !force && semantics.neural === neural) return semantics;

  $("map-note").textContent = neural
    ? "encoding with the language model…"
    : "reading word overlap…";
  const data = await api(`/api/library/semantics?neural=${neural ? 1 : 0}&mode=${currentMode()}`);
  semantics = { ...data, neural };
  return semantics;
}

/* ----------------------------------------------------------------- map -- */

const MAP_PAD = 28;

function colourOf(point, mode) {
  if (mode === "cluster") return PALETTE[point.cluster % PALETTE.length];
  if (mode === "recording") {
    const ids = [...ctx.state.recordings.keys()];
    return PALETTE[ids.indexOf(ctx.state.byRef.get(point.ref)?.recording_id) % PALETTE.length];
  }
  // Themes hold codes: a quote takes the colour of the first theme holding any
  // of its codes.
  const codes = (ctx.state.byRef.get(point.ref)?.codes || []).map((id) => `text:${id}`);
  const theme = ctx.state.themes.findIndex((t) => t.refs.some((ref) => codes.includes(ref)));
  return theme < 0 ? "unplaced" : PALETTE[theme % PALETTE.length];
}

export async function renderMap() {
  const svg = $("map");
  try {
    await ensureSemantics();
  } catch (error) {
    $("map-note").textContent = "";
    svg.innerHTML = "";
    ctx.notify(`The map needs its numeric stack: ${error.message}`, { kind: "warn" });
    return;
  }

  const mode = $("map-colour").value;
  const box = svg.getBoundingClientRect();
  const width = Math.max(320, box.width);
  const height = Math.max(240, box.height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  // Two people saying the same thing get the same vector and land on the same
  // pixel, which hides one of them completely. Spread a stack around a small
  // ring so every quote stays its own target -- deterministically, so the map
  // does not rearrange itself between renders.
  const stacks = new Map();
  for (const point of semantics.points) {
    const key = `${point.x.toFixed(3)},${point.y.toFixed(3)}`;
    if (!stacks.has(key)) stacks.set(key, []);
    stacks.get(key).push(point.ref);
  }

  const place = (point) => {
    const key = `${point.x.toFixed(3)},${point.y.toFixed(3)}`;
    const stack = stacks.get(key) || [];
    let dx = 0;
    let dy = 0;
    if (stack.length > 1) {
      const index = stack.indexOf(point.ref);
      const angle = (index / stack.length) * Math.PI * 2;
      const radius = 7 + Math.min(14, stack.length);
      dx = Math.cos(angle) * radius;
      dy = Math.sin(angle) * radius;
    }
    return [
      MAP_PAD + point.x * (width - MAP_PAD * 2) + dx,
      MAP_PAD + point.y * (height - MAP_PAD * 2) + dy,
    ];
  };

  const circles = semantics.points
    .map((point) => {
      const [cx, cy] = place(point);
      const quote = ctx.state.byRef.get(point.ref);
      return `<circle class="map__dot map__dot--${colourOf(point, mode)}${
        selection.has(point.ref) ? " map__dot--picked" : ""
      }" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="6" data-ref="${point.ref}"
        tabindex="0" role="button"><title>${escapeHtml(
          (quote?.text || "").slice(0, 120)
        )}</title></circle>`;
    })
    .join("");

  svg.innerHTML = `<g id="map-lasso"></g>${circles}`;
  $("map-note").textContent =
    `${semantics.points.length} quotes · ${semantics.backend === "neural" ? "language model" : "word overlap"}` +
    ` · laid out with ${semantics.projector} · ${semantics.cluster_count} clusters`;
  $("map-neural-label").textContent = semantics.neural_available
    ? "use the language model (downloads it once)"
    : "language model not installed";
  $("map-neural").disabled = !semantics.neural_available;
  syncSelectionButton();
}

function syncSelectionButton() {
  const button = $("map-make-theme");
  button.hidden = selection.size === 0;
  // A theme holds codes, so it takes the codes these quotes carry.
  button.textContent = `Make a theme from these ${selection.size} quotes' codes`;
}

function insidePolygon(x, y, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function bindMap() {
  const svg = $("map");
  $("map-colour").addEventListener("change", renderMap);
  $("map-neural").addEventListener("change", () => renderMap());

  // Lasso: drag on empty space to draw a loop; everything inside it is picked.
  let path = null;

  const toLocal = (event) => {
    const box = svg.getBoundingClientRect();
    const view = svg.viewBox.baseVal;
    return [
      ((event.clientX - box.left) / box.width) * (view.width || box.width),
      ((event.clientY - box.top) / box.height) * (view.height || box.height),
    ];
  };

  svg.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".map__dot")) return;
    path = [toLocal(event)];
    svg.setPointerCapture(event.pointerId);
  });

  svg.addEventListener("pointermove", (event) => {
    if (!path) return;
    path.push(toLocal(event));
    const points = path.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const lasso = svg.querySelector("#map-lasso");
    if (lasso) lasso.innerHTML = `<polygon class="map__lasso" points="${points}"></polygon>`;
  });

  svg.addEventListener("pointerup", (event) => {
    if (!path) return;
    const loop = path;
    path = null;
    try {
      svg.releasePointerCapture(event.pointerId);
    } catch (_) { /* pointer already gone */ }

    if (loop.length > 4) {
      selection = new Set();
      for (const dot of svg.querySelectorAll(".map__dot")) {
        const x = Number(dot.getAttribute("cx"));
        const y = Number(dot.getAttribute("cy"));
        if (insidePolygon(x, y, loop)) selection.add(dot.dataset.ref);
      }
    }
    renderMap();
  });

  svg.addEventListener("click", (event) => {
    const dot = event.target.closest(".map__dot");
    if (dot) showNeighbours(dot.dataset.ref);
  });

  svg.addEventListener("keydown", (event) => {
    const dot = event.target.closest(".map__dot");
    if (dot && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      showNeighbours(dot.dataset.ref);
    }
  });

  $("map-make-theme").addEventListener("click", async () => {
    if (!selection.size) return;
    const title = prompt(`Name for a theme of ${selection.size} quotes`, "");
    if (title === null) return;
    try {
      // A lasso makes an area on the canvas too, so this answers with the whole
      // of it rather than just the themes -- one place takes a reply on.
      ctx.adopt(
        await api("/api/library/themes/from-refs", {
          method: "POST",
          body: { title, refs: [...selection] },
        })
      );
      selection = new Set();
      renderMap();
    } catch (error) {
      ctx.notify(`Could not make that theme: ${error.message}`, { kind: "warn" });
    }
  });
}

async function showNeighbours(ref) {
  const side = $("map-side");
  const quote = ctx.state.byRef.get(ref);
  if (!quote) return;

  side.innerHTML = `<div class="qgrid qgrid--one">${ctx.quoteCard(quote, {
    draggable: false,
  })}</div><p class="empty">finding what sits nearest…</p>`;

  try {
    const { similar } = await api(
      `/api/library/similar?ref=${encodeURIComponent(ref)}&k=6&neural=${semantics?.neural ? 1 : 0}`
    );
    const rows = similar
      .map((hit) => {
        const other = ctx.state.byRef.get(hit.ref);
        if (!other) return "";
        return `<button class="neighbour" type="button" data-ref="${hit.ref}">
            <span class="neighbour__score">${(hit.score * 100).toFixed(0)}</span>
            <span class="neighbour__text">${escapeHtml(other.text)}</span>
            <span class="neighbour__where">${escapeHtml(other.recording_title)} · ${formatTime(other.start_time)}</span>
          </button>`;
      })
      .join("");
    side.innerHTML =
      `<div class="qgrid qgrid--one">${ctx.quoteCard(quote, { draggable: false })}</div>` +
      `<h3 class="side__title">Nearest in meaning</h3>${rows || '<p class="empty">Nothing close.</p>'}`;
    side.querySelectorAll(".neighbour").forEach((button) =>
      button.addEventListener("click", () => showNeighbours(button.dataset.ref))
    );
  } catch (error) {
    side.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

/* --------------------------------------------------------------- graph -- */

/* A small spring simulation: linked tags pull together, every pair pushes
 * apart, and a weak pull to the middle keeps it on screen. Deliberately not a
 * library -- the whole thing is forty lines and stays readable. */
function simulate(nodes, links, width, height, ticks = 320) {
  const centreX = width / 2;
  const centreY = height / 2;

  // Scale the forces to the canvas and the number of tags, or a handful of
  // nodes huddle in the middle of a wide screen and a hundred pile off the edge.
  const room = Math.sqrt((width * height) / Math.max(1, nodes.length));
  const repulsion = room * room * 0.5;
  const restBase = room * 0.9;

  for (const node of nodes) {
    node.vx = 0;
    node.vy = 0;
  }

  for (let step = 0; step < ticks; step += 1) {
    const cooling = 1 - step / ticks;

    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const distance = Math.max(24, Math.hypot(dx, dy));
        const push = (repulsion * cooling) / (distance * distance);
        dx /= distance;
        dy /= distance;
        a.vx -= dx * push;
        a.vy -= dy * push;
        b.vx += dx * push;
        b.vy += dy * push;
      }
    }

    for (const link of links) {
      const dx = link.target.x - link.source.x;
      const dy = link.target.y - link.source.y;
      const distance = Math.hypot(dx, dy) || 0.01;
      // The more quotes two tags share, the shorter the spring between them.
      const rest = Math.max(90, restBase / (1 + link.weight * 0.6));
      const pull = ((distance - rest) * 0.025 * cooling) / distance;
      link.source.vx += dx * pull;
      link.source.vy += dy * pull;
      link.target.vx -= dx * pull;
      link.target.vy -= dy * pull;
    }

    for (const node of nodes) {
      if (node.pinned) continue;
      // Just enough pull to the middle to keep an unlinked tag on screen.
      node.vx += (centreX - node.x) * 0.0016;
      node.vy += (centreY - node.y) * 0.0016;
      node.x += Math.max(-18, Math.min(18, node.vx));
      node.y += Math.max(-18, Math.min(18, node.vy));
      node.vx *= 0.84;
      node.vy *= 0.84;
      node.x = Math.max(56, Math.min(width - 56, node.x));
      node.y = Math.max(40, Math.min(height - 40, node.y));
    }
  }
}

let graphNodes = [];

export function renderGraph() {
  const svg = $("graph");
  const box = svg.getBoundingClientRect();
  const width = Math.max(360, box.width);
  const height = Math.max(260, box.height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  if (!ctx.state.tags.length) {
    svg.innerHTML = "";
    $("graph-side").innerHTML = '<p class="empty">No text codes yet.</p>';
    return;
  }

  const byTag = new Map();
  graphNodes = ctx.state.tags.map((entry, index) => {
    const angle = (index / ctx.state.tags.length) * Math.PI * 2;
    const node = {
      tag: entry.tag,
      quotes: entry.quote_count,
      recordings: entry.recording_count,
      colour: entry.color || "amber",
      x: width / 2 + Math.cos(angle) * Math.min(width, height) * 0.3,
      y: height / 2 + Math.sin(angle) * Math.min(width, height) * 0.3,
    };
    byTag.set(entry.tag, node);
    return node;
  });

  const links = ctx.state.cooccurrence
    .map((pair) => ({
      source: byTag.get(pair.a),
      target: byTag.get(pair.b),
      weight: pair.count,
    }))
    .filter((link) => link.source && link.target);

  simulate(graphNodes, links, width, height);

  const strongest = Math.max(1, ...links.map((l) => l.weight));
  const edges = links
    .map(
      (link) =>
        `<line class="graph__edge" x1="${link.source.x.toFixed(1)}" y1="${link.source.y.toFixed(1)}"
           x2="${link.target.x.toFixed(1)}" y2="${link.target.y.toFixed(1)}"
           style="--w:${(link.weight / strongest).toFixed(2)}"></line>`
    )
    .join("");

  const biggest = Math.max(1, ...graphNodes.map((n) => n.quotes));
  const dots = graphNodes
    .map((node) => {
      const radius = 7 + (node.quotes / biggest) * 16;
      return `<g class="graph__node" data-tag="${escapeHtml(node.tag)}" tabindex="0" role="button">
          <circle class="graph__dot graph__dot--${node.colour}" cx="${node.x.toFixed(1)}"
                  cy="${node.y.toFixed(1)}" r="${radius.toFixed(1)}"></circle>
          <text class="graph__label" x="${node.x.toFixed(1)}" y="${(node.y + radius + 12).toFixed(1)}"
                text-anchor="middle">${escapeHtml(node.tag)}</text>
        </g>`;
    })
    .join("");

  svg.innerHTML = edges + dots;
}

function bindGraph() {
  const svg = $("graph");
  const open = (tag) => {
    const quotes = ctx.state.quotes.filter((q) => (q.tags || []).includes(tag));
    const partners = ctx.state.cooccurrence
      .filter((p) => p.a === tag || p.b === tag)
      .map((p) => `${escapeHtml(p.a === tag ? p.b : p.a)} (${p.count})`)
      .join(", ");
    $("graph-side").innerHTML =
      `<h3 class="side__title">${escapeHtml(tag)}</h3>` +
      `<p class="side__note">${quotes.length} quotes${partners ? ` · travels with ${partners}` : " · travels alone"}</p>` +
      `<button class="btn" id="graph-play" type="button">Play all ${quotes.length}</button>` +
      `<div class="qgrid qgrid--one">${quotes.map((q) => ctx.quoteCard(q, { draggable: false })).join("")}</div>`;
    $("graph-play")?.addEventListener("click", () => ctx.startQueue(quotes, tag));
  };

  svg.addEventListener("click", (event) => {
    const node = event.target.closest(".graph__node");
    if (node) open(node.dataset.tag);
  });
  svg.addEventListener("keydown", (event) => {
    const node = event.target.closest(".graph__node");
    if (node && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      open(node.dataset.tag);
    }
  });
}

/* ------------------------------------------------------------- signals -- */

export async function renderSignals() {
  try {
    await ensureSemantics();
  } catch (error) {
    $("saturation").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
    return;
  }

  const points = semantics.saturation.points;
  const host = $("saturation");
  if (!points.length) {
    host.innerHTML = '<p class="empty">No recordings.</p>';
    return;
  }

  const width = 720;
  const height = 220;
  const pad = 34;
  const top = Math.max(1, semantics.saturation.total_tags);
  const stepX = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
  const at = (point, index) => [
    pad + index * stepX,
    height - pad - (point.total / top) * (height - pad * 2),
  ];

  const line = points.map((p, i) => at(p, i).map((n) => n.toFixed(1)).join(",")).join(" ");
  const bars = points
    .map((point, index) => {
      const [x] = at(point, index);
      const barHeight = (point.new / top) * (height - pad * 2);
      return `<rect class="sat__bar" x="${(x - 7).toFixed(1)}" y="${(height - pad - barHeight).toFixed(1)}"
                width="14" height="${Math.max(0, barHeight).toFixed(1)}"></rect>`;
    })
    .join("");
  const dots = points
    .map((point, index) => {
      const [x, y] = at(point, index);
      return `<circle class="sat__dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4"><title>${escapeHtml(
        point.title
      )}: ${point.total} text codes, ${point.new} new</title></circle>
      <text class="sat__label" x="${x.toFixed(1)}" y="${height - pad + 16}" text-anchor="middle">${escapeHtml(
        point.title
      )}</text>`;
    })
    .join("");

  const stillClimbing = points[points.length - 1].new > 0;
  $("saturation-note").textContent = stillClimbing
    ? "the last interview still brought new text codes"
    : "the last interviews brought nothing new";

  host.innerHTML = `
    <svg class="sat" viewBox="0 0 ${width} ${height}" role="img"
         aria-label="Cumulative distinct text codes across interviews">
      <line class="sat__axis" x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}"></line>
      ${bars}
      <polyline class="sat__line" points="${line}"></polyline>
      ${dots}
    </svg>
    <p class="side__note">Bars are text codes appearing for the first time in that interview; the
      line is the running total. A curve still climbing at the last participant is the study
      saying it is not finished. It measures the codebook rather than the world — a flat
      curve can equally mean you stopped noticing.</p>`;

  const lonely = semantics.loneliest
    .map((entry) => ctx.state.byRef.get(entry.ref))
    .filter(Boolean);
  $("loneliest").innerHTML = lonely.length
    ? `<div class="qgrid">${lonely.map((q) => ctx.quoteCard(q, { draggable: false })).join("")}</div>`
    : '<p class="empty">Not enough quotes to tell yet.</p>';
}

/** Forget a lasso selection, e.g. when the quotes on screen change with the mode. */
export function clearSelection() {
  selection = new Set();
}

export function invalidateSemantics() {
  semantics = null;
}
