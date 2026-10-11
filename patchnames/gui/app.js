/*
 * patchdoc search page. The server (patchnames/gui.py) answers:
 *   /api/facets, /api/search, /api/sequence, /api/checklist,
 *   /thumb/NAME, /preview/NAME, and POST /api/reveal, /api/open.
 * Everything that comes from the index (titles, descriptions, eBird text) is
 * put in the page as text, never as HTML. What the page says is in French.
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
const number = (n) => Number(n || 0).toLocaleString("fr-CA");
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
  $("status").textContent = "Recherche…";
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
      ? (state.done ? "" : `${number(state.items.length)} séquences affichées sur ${number(state.total)} ; faites défiler pour la suite.`)
      : "Rien ne correspond. Élargissez un filtre ou essayez un autre mot.";
  } catch (err) {
    if (token === state.token) $("status").textContent = "La recherche a échoué : " + err.message;
  } finally {
    if (token === state.token) state.loading = false;
  }
}

function totals(sums) {
  const stat = (label, value) =>
    el("div", { class: "stat" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: number(value) }));
  $("stats").replaceChildren(
    stat("Séquences", sums.sequences), stat("Photos", sums.photos),
    stat("Espèces identifiées", sums.species), stat("Lieux", sums.places));
}

function tile(photo, index) {
  const img = el("img", { src: image("thumb", photo.name), loading: "lazy", alt: photo.title || photo.name });
  img.addEventListener("error", () =>
    img.replaceWith(el("div", { class: "missing", text: "pas encore de vignette (le disque d'archive est-il monté ?)" })));
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
      : [el("div", { class: "name none", text: photo.location || "Pas encore décrite" }),
         el("div", { class: "where", text: [date, photo.location ? region(photo) : ""].filter(Boolean).join(" · ") })]));
  figure.title = [photo.title, photo.description, unsure && "Identification à confirmer",
    photo.on_checklist === 0 && "Espèce absente de la liste eBird"].filter(Boolean).join("\n");
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
  chip.textContent = state.checklist ? `liste eBird ${state.checklist}  ×` : "";
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

// the button of the viewer: Claude describes the sequence where its files are
async function describe(photo, button, box) {
  const index = state.items.indexOf(photo);
  button.disabled = true;
  box.textContent = "Claude regarde la séquence…";
  const failed = (message) => { box.textContent = message; button.disabled = false; };
  try {
    const asked = await (await fetch("/api/describe", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Patchdoc": "1" },
      body: JSON.stringify({ name: photo.name }),
    })).json();
    if (asked.error) return failed(asked.error);
    for (let waited = 0; waited < 300; waited += 2) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const job = await (await fetch("/api/job?id=" + encodeURIComponent(asked.job))).json();
      if (job.state === "error") return failed("Claude n'a pas pu décrire la séquence : " + job.error);
      if (job.state !== "done") continue;
      Object.assign(photo, job.photo);
      if (index >= 0 && $("grid").children[index]) $("grid").children[index].replaceWith(tile(photo, index));
      if (state.current === index) {
        $("viewerInfo").replaceChildren(...info(photo));
        show(photo.name);
      }
      return;
    }
    failed("Claude met trop de temps à répondre ; réessayez plus tard.");
  } catch (err) {
    failed(String(err));
  }
}

async function listSpecies(photo, box) {
  if (box.childElementCount) {
    box.replaceChildren();
    return;
  }
  const list = await (await fetch("/api/checklist?sid=" + encodeURIComponent(photo.checklist))).json();
  if (!list.species) {
    box.replaceChildren(el("p", { class: "mono", text: "Cette liste n'est pas dans les données eBird de l'index." }));
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
  const out = [el("h2", { text: photo.title || "Pas encore décrite" })];
  const badge = (text, kind) => el("span", { class: "badge" + (kind ? " " + kind : ""), text });
  const badges = [];
  if (photo.species && photo.certainty) {
    badges.push(badge("identification : " + photo.certainty, photo.certainty === "sûre" ? "good" : "warn"));
  }
  if (photo.on_checklist === 1) badges.push(badge("sur la liste eBird", "good"));
  if (photo.on_checklist === 0) badges.push(badge("absente de la liste eBird", "alert"));
  if (photo.topic) badges.push(badge(photo.topic));
  if (photo.seq_n > 1) badges.push(badge(photo.seq_n + " vues"));
  if (badges.length) out.push(el("div", { class: "badges" }, badges));
  if (photo.description) out.push(el("p", { text: photo.description }));
  if (photo.note) out.push(el("p", { class: "note", text: "Par rapport à la liste : " + photo.note }));
  if (!photo.described) {
    out.push(el("p", { class: "mono", text: "Claude n'a pas décrit cette séquence. Ce qui est affiché vient de "
      + "son nom et de la liste eBird pendant laquelle elle a été prise." }));
  }
  if (photo.reachable && state.claude) {
    const each = state.claude.each.toFixed(state.claude.each < 0.01 ? 3 : 2).replace(".", ",");
    const said = el("p", { class: "mono", text: `Un appel à Claude : environ 20 s et ${each} $ US au prix de l'API. `
      + "La réponse va dans la base, pas dans les fichiers." });
    out.push(el("div", { class: "actions" },
      el("button", { class: "pill go", type: "button",
                     text: photo.described ? "Identifier de nouveau avec Claude" : "Identifier la séquence avec Claude",
                     onclick: (event) => describe(photo, event.currentTarget, said) })), said);
  } else if (photo.reachable === false) {
    out.push(el("p", { class: "mono", text: "Pour demander à Claude d'identifier la séquence, montez le disque d'archive." }));
  }

  const species = split(photo.species), latin = split(photo.latin);
  if (species.length) {
    out.push(el("h3", { text: "Espèces" }), el("div", { class: "chips" }, species.map((name, k) =>
      el("span", { class: "chip", text: latin[k] ? `${name} (${latin[k]})` : name, title: "Toutes les photos de cette espèce",
                   onclick: () => setFilter({ species: name }) }))));
  }
  const words = [...split(photo.habitat), ...split(photo.keywords)];
  if (words.length) {
    out.push(el("h3", { text: "Mots-clés" }), el("div", { class: "chips" }, words.map((word) =>
      el("span", { class: "chip", text: word, onclick: () => setFilter({ q: word }) }))));
  }

  if (photo.location || photo.checklist) {
    out.push(el("h3", { text: "Où" }));
    if (photo.location) {
      out.push(el("div", { class: "chips" }, el("span", { class: "chip", text: photo.location,
        title: "Toutes les photos prises ici", onclick: () => setFilter({ place: photo.location }) })));
    }
    if (region(photo)) out.push(el("p", { text: region(photo) }));
    if (photo.match === "gps") {
      out.push(el("p", { class: "mono", text: "Le lieu eBird le plus proche de la position de la photo." }));
    }
    if (photo.checklist) {
      const how = { inside: "photo prise pendant la liste", near: "photo prise à moins d'une heure de la liste",
                    other: "liste choisie par Claude parmi celles du jour" }[photo.match] || "";
      const box = el("div", {});
      out.push(
        el("p", {}, el("a", { href: "https://ebird.org/checklist/" + encodeURIComponent(photo.checklist),
                              target: "_blank", rel: "noopener", text: "Liste eBird " + photo.checklist }),
          how ? ` (${how})` : ""),
        el("div", { class: "actions" },
          el("button", { class: "pill", type: "button", text: "Ses espèces", onclick: () => listSpecies(photo, box) }),
          el("button", { class: "pill", type: "button", text: "Toutes ses photos",
                         onclick: () => setFilter({ checklist: photo.checklist }) })),
        box);
    }
  }

  out.push(el("h3", { text: "Quand" }));
  const offset = Number(photo.clock_offset || 0);
  out.push(el("p", { class: "mono", text: (photo.local || photo.taken || "inconnu").slice(0, 16)
    + (offset ? `  (horloge de l'appareil ${(photo.taken || "").slice(11, 16)}, décalée de ${offset > 0 ? "+" : ""}${offset} h)` : "") }));

  const gear = [photo.camera, photo.lens, photo.focal && Math.round(photo.focal) + " mm"].filter(Boolean);
  if (gear.length) out.push(el("h3", { text: "Appareil" }), el("p", { class: "mono", text: gear.join(" · ") }));

  const said = el("p", { class: "mono" });
  out.push(el("h3", { text: "Fichier" }), el("p", { class: "mono", text: photo.path || photo.name }));
  if (photo.reachable !== false) {
    out.push(el("div", { class: "actions" },
      el("button", { class: "pill", type: "button", text: "Afficher dans le Finder", onclick: () => act("reveal", photo.name, said) }),
      el("button", { class: "pill", type: "button", text: "Ouvrir", onclick: () => act("open", photo.name, said) })),
      said);
  } else {
    out.push(el("p", { class: "mono", text: "Hors d'atteinte pour l'instant : le disque d'archive n'est pas monté." }));
  }
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
      el("h2", { text: "Aucune photo dans l'index pour l'instant" }),
      el("p", { class: "hint", text: "Décrivez les photos d'un dossier (elles sont renommées et archivées comme le "
        + "fait patchnames), ou ajoutez d'après leurs noms les photos déjà dans l'archive :" }),
      el("pre", { class: "codeblock", text: "cd /chemin/des/nouvelles/photos\npatchdoc\n\npatchdoc index" })));
    $("counts").textContent = "index : " + facets.index;
    return;
  }
  $("counts").textContent = `${number(facets.total)} photos dans l'index, dont ${number(facets.described)} décrites`
    + (facets.ebird ? ` · données eBird du ${facets.ebird.slice(0, 10)}` : " · pas de données eBird");
  // (a server started before an update of patchdoc says less: relaunch it)
  state.claude = facets.claude;
  const store = facets.thumbs && facets.thumbs.total
    ? ` · vignettes : ${number(facets.thumbs.have)} sur ${number(facets.thumbs.total)}`
      + (facets.thumbs.filling ? " (les autres se font en arrière-plan)" : "")
    : "";
  $("drive").textContent = !facets.archive
    ? "Ce serveur date d'avant une mise à jour de patchdoc : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
    : (facets.archive.mounted
      ? `disque d'archive : monté (${facets.archive.mounted})`
      : "disque d'archive : non monté. La recherche et les vignettes déjà faites restent disponibles ; "
        + "ouvrir un fichier, faire une vignette ou appeler Claude demande le disque.") + store;

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
    options(group, [[country, `${entry.name}, tout (${number(entry.count)})`], ...entry.states]);
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

start().catch((err) => { $("status").textContent = "La page n'a pas pu démarrer : " + err.message; });
