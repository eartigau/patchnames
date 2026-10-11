/*
 * patchdoc search page. The server (patchnames/gui.py) answers:
 *   /api/facets, /api/search, /api/sequence, /api/checklist,
 *   /thumb/NAME, /preview/NAME, and POST /api/reveal, /api/open.
 * Everything that comes from the index (titles, descriptions, eBird text) is
 * put in the page as text, never as HTML.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = ["q", "species", "place", "region", "year", "since", "until", "topic", "kind", "flag", "sort"];
const PAGE = 60;
const state = { items: [], offset: 0, done: false, loading: false, token: 0, current: -1, checklist: "", total: 0 };

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const kid of kids.flat()) if (kid) node.append(kid);
  return node;
}

const split = (text) => (text || "").split("; ").map((word) => word.trim()).filter(Boolean);
const number = (n) => Number(n || 0).toLocaleString("en-CA");
const image = (kind, name) => `/${kind}/${encodeURIComponent(name)}`;

function region(photo) {
  return [photo.county, photo.state, photo.country_name].filter(Boolean).join(", ");
}

/* ── Search ─────────────────────────────────────────────────────────── */

function query() {
  const params = new URLSearchParams();
  for (const key of FIELDS) {
    const value = $(key).value.trim();
    if (value && !(key === "sort" && value === "new")) params.set(key, value);
  }
  if ($("listed").checked && $("species").value.trim()) params.set("listed", "1");
  if (state.checklist) params.set("checklist", state.checklist);
  return params;
}

async function load(reset) {
  if (reset) {
    state.token += 1;
    state.items = [];
    state.offset = 0;
    state.done = false;
    $("grid").replaceChildren();
    history.replaceState(null, "", "?" + query());
  } else if (state.loading || state.done) {
    return;
  }
  const token = state.token;
  state.loading = true;
  $("status").textContent = "Searching…";
  const params = query();
  params.set("offset", state.offset);
  params.set("limit", PAGE);
  try {
    const data = await (await fetch("/api/search?" + params)).json();
    if (token !== state.token) return;          // a newer search took over
    if (data.error) throw new Error(data.error);
    for (const photo of data.photos) {
      state.items.push(photo);
      $("grid").append(tile(photo, state.items.length - 1));
    }
    state.offset += data.photos.length;
    state.done = data.photos.length < PAGE;
    state.total = data.totals.sequences;
    totals(data.totals);
    $("status").textContent = state.items.length
      ? (state.done ? "" : `${number(state.items.length)} of ${number(state.total)} sequences shown; scroll for more.`)
      : "Nothing matches. Loosen a filter, or try another word.";
  } catch (err) {
    if (token === state.token) $("status").textContent = "The search failed: " + err.message;
  } finally {
    if (token === state.token) state.loading = false;
  }
}

function totals(sums) {
  const stat = (label, value) =>
    el("div", { class: "stat" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: number(value) }));
  $("stats").replaceChildren(
    stat("Sequences", sums.sequences), stat("Photos", sums.photos),
    stat("Species identified", sums.species), stat("Places", sums.places));
}

function tile(photo, index) {
  const img = el("img", { src: image("thumb", photo.name), loading: "lazy", alt: photo.title || photo.name });
  img.addEventListener("error", () =>
    img.replaceWith(el("div", { class: "missing", text: "no thumbnail yet (is the archive drive mounted?)" })));
  const unsure = photo.species && photo.certainty && photo.certainty !== "sûre";
  const marks = el("div", { class: "marks" },
    el("span", {}, photo.seq_n > 1 && el("span", { class: "mark", text: "×" + photo.seq_n })),
    el("span", {},
      unsure && el("span", { class: "mark warn", text: "?" }),
      photo.on_checklist === 0 && el("span", { class: "mark alert", text: "≠" })));
  const date = (photo.local || photo.taken || "").slice(0, 10);
  const figure = el("figure", { class: "gallery-item", onclick: () => openViewer(index) }, img, marks,
    el("figcaption", {}, photo.title
      ? [el("div", { class: "name", text: photo.title }),
         el("div", { class: "where", text: [date, photo.location || region(photo)].filter(Boolean).join(" · ") })]
      // not described: the place stands for the title
      : [el("div", { class: "name none", text: photo.location || "Not described yet" }),
         el("div", { class: "where", text: [date, photo.location ? region(photo) : ""].filter(Boolean).join(" · ") })]));
  figure.title = [photo.title, photo.description, unsure && "Identification to confirm",
    photo.on_checklist === 0 && "Species not on the eBird checklist"].filter(Boolean).join("\n");
  return figure;
}

function setFilter(changes) {
  closeViewer();
  for (const [key, value] of Object.entries(changes)) {
    if (key === "checklist") state.checklist = value;
    else $(key).value = value;
  }
  showChecklistChip();
  load(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showChecklistChip() {
  const chip = $("checklistChip");
  chip.hidden = !state.checklist;
  chip.textContent = state.checklist ? `eBird checklist ${state.checklist}  ×` : "";
}

/* ── Viewer ─────────────────────────────────────────────────────────── */

function show(name) {
  $("viewerImg").src = image("preview", name);
  for (const img of $("viewerStrip").children) img.classList.toggle("on", img.dataset.name === name);
}

async function openViewer(index) {
  const photo = state.items[index];
  if (!photo) return;
  state.current = index;
  history.replaceState(null, "", "?" + query() + "#" + encodeURIComponent(photo.name));
  $("viewer").hidden = false;
  document.body.style.overflow = "hidden";
  $("viewerStrip").replaceChildren();
  $("viewerImg").alt = photo.title || photo.name;
  show(photo.name);
  $("viewerInfo").replaceChildren(...info(photo));
  $("viewerInfo").scrollTop = 0;
  if (photo.seq_n > 1) {
    const frames = await (await fetch("/api/sequence?seq=" + encodeURIComponent(photo.seq))).json();
    if (state.current !== index || !Array.isArray(frames)) return;
    $("viewerStrip").replaceChildren(...frames.map((frame) => {
      const img = el("img", { src: image("thumb", frame.name), loading: "lazy", alt: frame.name,
                              title: frame.name, onclick: () => show(frame.name) });
      img.dataset.name = frame.name;
      img.classList.toggle("on", frame.name === photo.name);
      img.addEventListener("error", () => img.remove());
      return img;
    }));
  }
}

function closeViewer() {
  $("viewer").hidden = true;
  $("viewerImg").src = "";
  document.body.style.overflow = "";
  state.current = -1;
  history.replaceState(null, "", "?" + query());
}

async function step(by) {
  let next = state.current + by;
  if (next >= state.items.length && !state.done) await load(false);
  next = Math.max(0, Math.min(next, state.items.length - 1));
  if (next !== state.current) openViewer(next);
}

async function act(what, name, box) {
  box.textContent = "";
  try {
    const reply = await fetch("/api/" + what, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Patchdoc": "1" },
      body: JSON.stringify({ name }),
    });
    const data = await reply.json();
    if (data.error) box.textContent = data.error;
  } catch (err) {
    box.textContent = String(err);
  }
}

async function listSpecies(photo, box) {
  if (box.childElementCount) {
    box.replaceChildren();
    return;
  }
  const list = await (await fetch("/api/checklist?sid=" + encodeURIComponent(photo.checklist))).json();
  if (!list.species) {
    box.replaceChildren(el("p", { class: "mono", text: "This checklist is not in the eBird data of the index." }));
    return;
  }
  const mine = split(photo.latin).map((name) => name.toLowerCase());
  box.replaceChildren(
    el("p", { class: "mono", text: [list.start || list.day, list.duration && list.duration + " min", list.protocol]
      .filter(Boolean).join(" · ") }),
    list.comments && el("p", { class: "mono", text: list.comments }),
    el("ul", { class: "listing" }, list.species.map((sp) =>
      el("li", { class: mine.includes(sp.latin.toLowerCase()) ? "on" : "", title: sp.details || sp.latin },
        el("span", { text: sp.francais }), el("span", { class: "n", text: sp.count })))));
}

function info(photo) {
  const out = [el("h2", { text: photo.title || "Not described yet" })];
  const badge = (text, kind) => el("span", { class: "badge" + (kind ? " " + kind : ""), text });
  const badges = [];
  if (photo.species && photo.certainty) {
    badges.push(badge("identification: " + photo.certainty, photo.certainty === "sûre" ? "good" : "warn"));
  }
  if (photo.on_checklist === 1) badges.push(badge("on the eBird checklist", "good"));
  if (photo.on_checklist === 0) badges.push(badge("not on the eBird checklist", "alert"));
  if (photo.topic) badges.push(badge(photo.topic));
  if (photo.seq_n > 1) badges.push(badge(photo.seq_n + " shots"));
  if (badges.length) out.push(el("div", { class: "badges" }, badges));
  if (photo.description) out.push(el("p", { text: photo.description }));
  if (photo.note) out.push(el("p", { class: "note", text: "Against the checklist: " + photo.note }));
  if (!photo.described) {
    out.push(el("p", { class: "mono", text: "Claude has not described this sequence. What is shown comes from "
      + "its name and from the eBird checklist it was taken during." }));
  }

  const species = split(photo.species), latin = split(photo.latin);
  if (species.length) {
    out.push(el("h3", { text: "Species" }), el("div", { class: "chips" }, species.map((name, k) =>
      el("span", { class: "chip", text: latin[k] ? `${name} (${latin[k]})` : name, title: "All the photos of this species",
                   onclick: () => setFilter({ species: name }) }))));
  }
  const words = [...split(photo.habitat), ...split(photo.keywords)];
  if (words.length) {
    out.push(el("h3", { text: "Keywords" }), el("div", { class: "chips" }, words.map((word) =>
      el("span", { class: "chip", text: word, onclick: () => setFilter({ q: word }) }))));
  }

  if (photo.location || photo.checklist) {
    out.push(el("h3", { text: "Where" }));
    if (photo.location) {
      out.push(el("div", { class: "chips" }, el("span", { class: "chip", text: photo.location,
        title: "All the photos taken here", onclick: () => setFilter({ place: photo.location }) })));
    }
    if (region(photo)) out.push(el("p", { text: region(photo) }));
    if (photo.match === "gps") {
      out.push(el("p", { class: "mono", text: "The eBird place closest to the position of the photo." }));
    }
    if (photo.checklist) {
      const how = { inside: "taken during it", near: "taken within an hour of it",
                    other: "chosen by Claude among the checklists of the day" }[photo.match] || "";
      const box = el("div", {});
      out.push(
        el("p", {}, el("a", { href: "https://ebird.org/checklist/" + encodeURIComponent(photo.checklist),
                              target: "_blank", rel: "noopener", text: "eBird checklist " + photo.checklist }),
          how ? ` (${how})` : ""),
        el("div", { class: "actions" },
          el("button", { class: "pill", type: "button", text: "Its species", onclick: () => listSpecies(photo, box) }),
          el("button", { class: "pill", type: "button", text: "All its photos",
                         onclick: () => setFilter({ checklist: photo.checklist }) })),
        box);
    }
  }

  out.push(el("h3", { text: "When" }));
  const offset = Number(photo.clock_offset || 0);
  out.push(el("p", { class: "mono", text: (photo.local || photo.taken || "unknown").slice(0, 16)
    + (offset ? `  (camera clock ${(photo.taken || "").slice(11, 16)}, off by ${offset > 0 ? "+" : ""}${offset} h)` : "") }));

  const gear = [photo.camera, photo.lens, photo.focal && Math.round(photo.focal) + " mm"].filter(Boolean);
  if (gear.length) out.push(el("h3", { text: "Camera" }), el("p", { class: "mono", text: gear.join(" · ") }));

  const said = el("p", { class: "mono" });
  out.push(el("h3", { text: "File" }), el("p", { class: "mono", text: photo.path || photo.name }),
    el("div", { class: "actions" },
      el("button", { class: "pill", type: "button", text: "Show in Finder", onclick: () => act("reveal", photo.name, said) }),
      el("button", { class: "pill", type: "button", text: "Open", onclick: () => act("open", photo.name, said) })),
    said);
  return out;
}

/* ── Start ──────────────────────────────────────────────────────────── */

function options(select, rows) {
  for (const [value, label] of rows) select.append(el("option", { value, text: label }));
}

async function start() {
  const facets = await (await fetch("/api/facets")).json();
  if (!facets.total) {
    $("results").replaceChildren(el("div", { class: "empty" },
      el("h2", { text: "No photo in the index yet" }),
      el("p", { class: "hint", text: "Describe the photos of a folder (they are renamed and archived as "
        + "patchnames does), or add the photos already in the archive from their names:" }),
      el("pre", { class: "codeblock", text: "cd /path/to/new/photos\npatchdoc\n\npatchdoc index" })));
    $("counts").textContent = "index: " + facets.index;
    return;
  }
  $("counts").textContent = `${number(facets.total)} photos in the index, ${number(facets.described)} described`
    + (facets.ebird ? ` · eBird data of ${facets.ebird.slice(0, 10)}` : " · no eBird data");

  const names = new Set([...facets.species, ...facets.listed].map((row) => row[0]));
  for (const name of [...names].sort((a, b) => a.localeCompare(b, "fr"))) {
    $("speciesList").append(el("option", { value: name }));
  }
  for (const [place] of facets.places) $("placeList").append(el("option", { value: place }));
  const countries = new Map();
  for (const [code, country, name, count] of facets.regions) {
    if (!countries.has(country)) countries.set(country, { name, count: 0, states: [] });
    countries.get(country).count += count;
    countries.get(country).states.push([code, `${code} (${number(count)})`]);
  }
  for (const [country, entry] of [...countries].sort((a, b) => a[1].name.localeCompare(b[1].name, "fr"))) {
    const group = el("optgroup", { label: entry.name });
    options(group, [[country, `${entry.name}, all (${number(entry.count)})`], ...entry.states]);
    $("region").append(group);
  }
  options($("year"), facets.years.map(([year, count]) => [year, `${year} (${number(count)})`]));
  options($("topic"), facets.topics.map(([topic, count]) => [topic, `${topic} (${number(count)})`]));

  // the search of the address bar, to come back to it
  const asked = new URLSearchParams(location.search);
  for (const key of FIELDS) if (asked.get(key)) $(key).value = asked.get(key);
  state.checklist = asked.get("checklist") || "";
  showChecklistChip();

  let timer = null;
  const soon = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); };
  for (const key of FIELDS) {
    $(key).addEventListener($(key).tagName === "SELECT" || $(key).type === "date" ? "change" : "input", soon);
  }
  $("listed").addEventListener("change", () => load(true));
  $("filters").addEventListener("submit", (event) => { event.preventDefault(); load(true); });
  $("checklistChip").addEventListener("click", () => setFilter({ checklist: "" }));
  $("reset").addEventListener("click", () => {
    for (const key of FIELDS) $(key).value = key === "sort" ? "new" : "";
    state.checklist = "";
    showChecklistChip();
    load(true);
  });

  $("viewerClose").addEventListener("click", closeViewer);
  $("viewerPrev").addEventListener("click", () => step(-1));
  $("viewerNext").addEventListener("click", () => step(1));
  $("viewer").addEventListener("click", (event) => { if (event.target === $("viewer")) closeViewer(); });
  document.addEventListener("keydown", (event) => {
    if ($("viewer").hidden) return;
    if (event.key === "Escape") closeViewer();
    else if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
  });

  new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting) && state.items.length) load(false);
  }, { rootMargin: "800px" }).observe($("more"));

  // the address of a photo (…#name) opens it
  const wanted = decodeURIComponent(location.hash.slice(1));
  await load(true);
  const at = state.items.findIndex((photo) => photo.name === wanted);
  if (wanted && at >= 0) openViewer(at);
}

start().catch((err) => { $("status").textContent = "The page could not start: " + err.message; });
