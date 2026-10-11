/*
 * patchdoc search page. The server (patchnames/gui.py) answers:
 *   /api/facets, /api/search, /api/sequence, /api/checklist,
 *   /thumb/NAME, /preview/NAME, and POST /api/reveal, /api/open.
 * Everything that comes from the index (titles, descriptions, eBird text) is
 * put in the page as text, never as HTML. What the page says is in French.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = ["section", "q", "species", "person", "place", "region", "year", "since", "until", "topic",
                "eye", "clear", "level", "flag", "sort"];
const PAGE = 60;
const state = { items: [], offset: 0, done: false, loading: false, token: 0, current: -1, checklist: "", total: 0 };
// the batch of the button above the results: how many finished sequences
// this page has taken in, its timer, and when the button was first clicked
const batch = { since: 0, timer: null, armed: 0 };

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

/* ── Map of the places: discs that gather as one zooms out ───────────── */

const chart = { map: null, layer: null, key: null, places: [] };

function disc(count, many, on) {
  const size = Math.round(26 + 6 * Math.log10(Math.max(1, count)) * 1.6);
  return L.divIcon({ html: `<div class="spot${many ? " many" : ""}${on ? " on" : ""}" style="width:${size}px;height:${size}px">${count > 9999 ? Math.round(count / 1000) + "k" : count}</div>`,
                     className: "", iconSize: [size, size] });
}

// the discs of the places, the one chosen marked; a disc picks its place
function drawPlaces() {
  const chosen = $("place").value.trim().toLowerCase();
  chart.layer.clearLayers();
  for (const [place, lat, lon, sequences, photos] of chart.places) {
    const marker = L.marker([lat, lon], { icon: disc(sequences, false, chosen && place.toLowerCase() === chosen), count: sequences });
    marker.bindTooltip(`${place} · ${plural(sequences, "séquence", "séquences")}, ${plural(photos, "photo", "photos")}`);
    marker.on("click", () => {
      $("place").value = place;
      drawPlaces();
      load(true);
      $("results").scrollIntoView({ behavior: "smooth" });
    });
    chart.layer.addLayer(marker);
  }
}

// the map follows the filters, except the place itself, so that another can be picked
async function showPlaces() {
  if (!window.L || !L.markerClusterGroup) return;         // the map library did not load (offline)
  const params = query();
  params.delete("place");
  if (params.toString() === chart.key) return drawPlaces();
  chart.key = params.toString();
  chart.places = await (await fetch("/api/places?" + params)).json();
  if (!Array.isArray(chart.places)) chart.places = [];    // a server from before the map
  $("mapCard").hidden = !chart.places.length && !chart.map;
  $("mapNote").textContent = chart.places.length
    ? `${plural(chart.places.length, "lieu", "lieux")} pour cette recherche ; cliquez un disque pour voir ses photos, la molette zoome.`
    : "Aucun lieu connu pour cette recherche.";
  if (!chart.places.length && !chart.map) return;
  if (!chart.map) {
    chart.map = L.map("map", { worldCopyJump: true });
    // the tiles of OpenStreetMap, which need no key; app.css darkens them
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; les contributeurs d\'<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(chart.map);
    chart.layer = L.markerClusterGroup({
      showCoverageOnHover: false, maxClusterRadius: 46,
      iconCreateFunction: (cluster) => disc(cluster.getAllChildMarkers().reduce((sum, m) => sum + m.options.count, 0), true),
    }).addTo(chart.map);
  }
  drawPlaces();
  if (chart.places.length) {
    chart.map.fitBounds(L.latLngBounds(chart.places.map((row) => [row[1], row[2]])).pad(0.15), { maxZoom: 13 });
  }
}

/* ── Sections: bird photos, family photos, the rest ─────────────────── */

// show the section chosen and the filters that go with it; a filter that
// leaves the form is emptied (it would go on filtering unseen)
function setSection(section, quiet) {
  $("section").value = section;
  for (const tab of $("sections").querySelectorAll(".tab")) tab.classList.toggle("on", tab.dataset.section === section);
  for (const label of $("filters").querySelectorAll("[data-for]")) {
    const shown = !section || label.dataset.for === section;
    label.hidden = !shown;
    if (!shown && !quiet) for (const input of label.querySelectorAll("input:not([type=checkbox]), select")) input.value = "";
  }
  if (!quiet) load(true);
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
    if (!$("map").hidden) showPlaces();
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
    batchButton();
    $("status").textContent = state.items.length
      ? (state.done ? "" : `${number(state.items.length)} séquences affichées sur ${number(state.total)} ; faites défiler pour la suite.`)
      : "Rien ne correspond. Élargissez un filtre ou essayez un autre mot.";
  } catch (err) {
    if (token === state.token) $("status").textContent = "La recherche a échoué : " + err.message;
  } finally {
    if (token === state.token) state.loading = false;
  }
}

/* ── The sequences shown, identified by Claude in the background ────── */

const plural = (n, one, many) => (n > 1 ? `${number(n)} ${many}` : `${n} ${one}`);
const undescribed = () => state.items.filter((photo) => !photo.described && photo.reachable !== false);

async function post(path, body) {
  const reply = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Patchdoc": "1" },
    body: JSON.stringify(body || {}),
  });
  return reply.json();
}

// put again in the page a sequence the index has just learned more about
function refresh(index) {
  if (index < 0) return;
  const photo = state.items[index];
  if ($("grid").children[index]) $("grid").children[index].replaceWith(tile(photo, index));
  if (state.current === index) {
    $("viewerInfo").replaceChildren(...info(photo));
    showPeople();
  }
}

function batchButton() {
  $("batch").hidden = !state.claude || !state.items.length;
  const count = undescribed().length;
  batch.armed = 0;
  $("batchGo").disabled = !count;
  $("batchGo").textContent = count
    ? `Identifier avec Claude ${count > 1 ? "les " + number(count) + " séquences affichées" : "la séquence affichée"} sans description`
    : "Toutes les séquences affichées sont décrites";
  $("batchGo").title = "Claude regarde en arrière-plan chaque séquence affichée qui n'a pas de description, "
    + "deux à la fois ; les réponses sont écrites dans la base et dans l'en-tête des fichiers. "
    + "Faites défiler pour en afficher davantage.";
}

async function batchGo() {
  const todo = undescribed();
  if (!todo.length) return;
  if (Date.now() - batch.armed > 8000) {        // a first click says what a second one will do
    batch.armed = Date.now();
    $("batchGo").textContent = `Confirmer : ${plural(todo.length, "appel", "appels")} à Claude, en arrière-plan`;
    return;
  }
  const reply = await post("/api/batch", { names: todo.map((photo) => photo.name) });
  batchButton();
  if (reply.error) {
    $("batchStatus").textContent = reply.error === "refusé"
      ? "Ce serveur date d'avant cette fonction : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
      : reply.error;
    return;
  }
  batch.since = 0;
  watchBatch();
}

async function watchBatch() {
  clearTimeout(batch.timer);
  let status;
  try {
    status = await (await fetch("/api/batch?since=" + batch.since)).json();
  } catch (err) {
    return;
  }
  if (status.total === undefined) return;        // a server from before this
  for (const item of status.finished) {
    const index = state.items.findIndex((photo) => photo.seq === item.seq);
    if (index >= 0 && item.photo) {
      Object.assign(state.items[index], item.photo);
      refresh(index);
    }
  }
  batch.since = status.next;
  $("batchStop").hidden = !status.running;
  const failed = status.failed ? `, ${plural(status.failed, "échec", "échecs")}` : "";
  $("batchStatus").textContent = !status.total && !status.note ? ""
    : (status.running
      ? `Claude en arrière-plan : ${status.done + status.failed} sur ${plural(status.total, "séquence", "séquences")}${failed}, ${status.active} en cours.`
      : `Identification terminée : ${status.done} sur ${plural(status.total, "séquence", "séquences")}${failed}.`)
      + (status.note ? " " + status.note : "");
  if (status.finished.length || !status.running) batchButton();
  if (status.running) batch.timer = setTimeout(watchBatch, 3000);
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
  state.shown = name;       // the frame on screen: the one the file buttons act on
  $("viewerImg").src = image("preview", name);
  loadFaces();
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
  showPeople();
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

async function act(what, name, box, app) {
  box.textContent = "";
  try {
    const reply = await fetch("/api/" + what, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Patchdoc": "1" },
      body: JSON.stringify({ name, app: Boolean(app) }),
    });
    const data = await reply.json();
    if (data.error) box.textContent = data.error;
  } catch (err) {
    box.textContent = String(err);
  }
}

/* ── Faces: a square on each, a name given once, found again elsewhere ── */

const facesOn = () => { try { return localStorage.getItem("patchdoc.visages") === "oui"; } catch (err) { return false; } };

// the squares, placed on the picture as it is drawn inside its element
function drawFaces() {
  const box = $("viewerFaces"), img = $("viewerImg");
  box.replaceChildren();
  if (!facesOn() || !state.faceList || !img.naturalWidth) return;
  const frame = img.getBoundingClientRect(), stage = box.getBoundingClientRect();
  const scale = Math.min(frame.width / img.naturalWidth, frame.height / img.naturalHeight);
  const width = img.naturalWidth * scale, height = img.naturalHeight * scale;
  const left = frame.left - stage.left + (frame.width - width) / 2;
  const top = frame.top - stage.top + (frame.height - height) / 2;
  state.faceList.forEach((face, k) => box.append(el("div", {
    class: "face" + (face.sure === 1 ? " sure" : face.person ? " guess" : ""),
    style: `left:${left + face.x * width}px;top:${top + face.y * height}px;width:${face.w * width}px;height:${face.h * height}px`,
  }, el("span", { text: `${k + 1}${face.person ? " " + face.person + (face.sure === 1 ? "" : " ?") : ""}` }))));
}

// the faces of the frame on screen, looked for by the server if nobody did
async function loadFaces() {
  state.faceList = null;
  state.faceNote = "";
  drawFaces();
  if (!facesOn() || !state.shown) return;
  const name = state.shown;
  state.faceNote = "Recherche des visages…";
  showPeople();
  const reply = await (await fetch("/api/faces?name=" + encodeURIComponent(name))).json();
  if (state.shown !== name) return;
  state.faceList = reply.faces || [];
  state.faceNote = reply.error || (state.faceList.length ? "" : "Aucun visage trouvé sur cette vue.");
  drawFaces();
  showPeople();
}

// who a face is: a name given, a proposal confirmed, or (empty) taken off
async function nameFace(face, person, born) {
  const reply = await post("/api/face", { id: face.id, person, born });
  if (reply.error) { state.faceNote = reply.error; return showPeople(); }
  state.faceList = reply.faces;
  $("personList").replaceChildren(...reply.people.map(([name]) => el("option", { value: name })));
  const index = state.current;
  if (index >= 0 && reply.photo && state.items[index].seq === reply.photo.seq) Object.assign(state.items[index], reply.photo);
  refresh(index);
  drawFaces();
}

// the rows of the faces in the panel: a number, a name to give, the year of birth
function showPeople() {
  const box = $("peopleRows");
  if (!box) return;
  box.replaceChildren(state.faceNote && el("p", { class: "mono", text: state.faceNote }),
    ...(state.faceList || []).map((face, k) => {
      const who = el("input", { class: "field", list: "personList", value: face.person || "",
                                placeholder: "qui est-ce ?", "aria-label": "Nom du visage " + (k + 1) });
      const born = el("input", { class: "field born", inputmode: "numeric", maxlength: "4", value: face.born || "",
                                 placeholder: "né en", title: "Année de naissance : la reconnaissance en tient compte (un enfant change vite, et personne n'est cherché avant sa naissance)" });
      const give = () => nameFace(face, who.value, born.value);
      for (const input of [who, born]) input.addEventListener("keydown", (event) => { if (event.key === "Enter") give(); });
      return el("div", { class: "who" }, el("span", { class: "n", text: String(k + 1) }), who, born,
        el("button", { class: "pill", type: "button", text: face.person && face.sure !== 1 ? "Oui" : "Nommer",
                       title: face.person && face.sure !== 1 ? "Confirmer le nom proposé" : "Donner ce nom à ce visage", onclick: give }),
        face.person && el("button", { class: "pill", type: "button", text: face.sure === 1 ? "Retirer" : "Non",
                       title: face.sure === 1 ? "Retirer ce nom" : "Ce n'est pas cette personne", onclick: () => nameFace(face, "", "") }));
    }));
}

// the buttons of the viewer: Claude describes the sequence where its files
// are ("describe"), and what the index says of it is written into its files
// ("write" does only that). photo.job is what is said of the work under way.
async function work(photo, what) {
  const index = state.items.indexOf(photo);
  const say = (text, busy) => {
    photo.job = text;
    photo.busy = busy;
    if (state.current === index) $("viewerInfo").replaceChildren(...info(photo));
  };
  const update = (fresh) => {
    Object.assign(photo, fresh);
    refresh(index);
    if (state.current === index) show(state.shown || photo.name);    // its preview may be new
    batchButton();
  };
  say(what === "describe" ? "Claude regarde la séquence…" : "Écriture dans les fichiers…", true);
  try {
    const asked = await (await fetch("/api/" + what, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Patchdoc": "1" },
      body: JSON.stringify({ name: photo.name }),
    })).json();
    if (asked.error) return say(asked.error, false);
    let shown = false;
    for (let waited = 0; waited < 7200; waited += 2) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const job = await (await fetch("/api/job?id=" + encodeURIComponent(asked.job))).json();
      if (job.photo && !shown && job.state !== "running") {
        update(job.photo);          // Claude's answer, before the files have it
        shown = true;
      }
      if (job.state === "error") return say(job.error, false);
      if (job.state === "writing") {
        say(job.files ? `Écriture dans les fichiers : ${job.written} sur ${job.files}…` : "Écriture dans les fichiers…", true);
      } else if (job.state === "done") {
        update(job.photo);
        // (a server started before this could write only knows the index)
        return say(job.photo.written && job.photo.written !== "pending"
          ? "Les fichiers de la séquence sont à jour."
          : "La base est à jour, mais pas les fichiers : ce serveur date d'avant la mise à jour. "
            + "Arrêtez-le (Ctrl-C), relancez `patchdoc gui`, puis « Écrire dans les fichiers ».", false);
      }
    }
    say("L'écriture dure plus de deux heures ; elle continue sans cette page.", false);
  } catch (err) {
    say(String(err), false);
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
  for (const [key, label] of [["eye", "œil net"], ["clear", "dégagé"], ["level", "hauteur d'œil"]]) {
    if (photo[key]) badges.push(badge(`${label} ${photo[key]}/5`, photo[key] >= 4 ? "good" : photo[key] <= 2 ? "warn" : ""));
  }
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
    out.push(el("div", { class: "actions" },
      el("button", { class: "pill go", type: "button", disabled: photo.busy && "disabled",
                     text: photo.described ? "Identifier de nouveau avec Claude" : "Identifier la séquence avec Claude",
                     title: "Un appel à Claude, environ 20 s ; la réponse est écrite dans la base et dans l'en-tête des fichiers de la séquence",
                     onclick: () => work(photo, "describe") }),
      (photo.described || photo.checklist) && el("button", { class: "pill", type: "button", disabled: photo.busy && "disabled",
                     text: "Écrire dans les fichiers",
                     title: "Écrire dans l'en-tête des fichiers ce que la base sait de la séquence : description, lieu, latitude et longitude de la liste eBird (sans appeler Claude)",
                     onclick: () => work(photo, "write") })),
      el("p", { class: "mono", text: photo.job || (photo.written === "pending"
        ? "Les fichiers attendent encore ce que dit la base ; l'écriture reprendra toute seule."
        : photo.written ? "Les fichiers de la séquence portent cette description." : "") }));
  } else if (photo.reachable === false) {
    out.push(el("p", { class: "mono", text: "Pour demander à Claude d'identifier la séquence ou écrire dans les fichiers, montez le disque d'archive." }));
  }

  // the people: names given by the photographer, names proposed, and the faces to name
  const people = split(photo.people), guessed = split(photo.guessed);
  if (state.facesCan || people.length || guessed.length) {
    out.push(el("h3", { text: "Personnes" }));
    if (people.length || guessed.length) {
      out.push(el("div", { class: "chips" },
        people.map((name) => el("span", { class: "chip", text: name, title: "Toutes les photos de cette personne", onclick: () => setFilter({ person: name }) })),
        guessed.map((name) => el("span", { class: "chip", text: name + " ?", title: "Nom proposé d'après les visages déjà nommés ; à confirmer", onclick: () => setFilter({ person: name }) }))));
    }
    if (state.facesCan) {
      out.push(el("div", { class: "actions" }, el("button", { class: "pill", type: "button",
        text: facesOn() ? "Masquer les visages" : "Montrer les visages",
        title: "Un carré sur chaque visage de la vue affichée, pour dire qui c'est ; la reconnaissance se fait sur cette machine",
        onclick: () => {
          try { localStorage.setItem("patchdoc.visages", facesOn() ? "non" : "oui"); } catch (err) { /* private mode */ }
          $("viewerInfo").replaceChildren(...info(photo));
          loadFaces();
        } })));
      if (facesOn()) out.push(el("div", { id: "peopleRows" }));
    }
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
    // the buttons act on the frame on screen, which may not be the one of the tile
    const shown = () => state.shown || photo.name;
    out.push(el("div", { class: "actions" },
      el("button", { class: "pill", type: "button", text: "Afficher dans le Finder", onclick: () => act("reveal", shown(), said) }),
      el("button", { class: "pill", type: "button", text: "Ouvrir", onclick: () => act("open", shown(), said) }),
      state.rawApp && /\.(nef|dng)$/i.test(photo.name) && el("button", { class: "pill", type: "button",
        text: "Ouvrir avec " + state.rawApp, onclick: () => act("open", shown(), said, true) }),
      state.panoCan && photo.seq_n > 1 && !photo.bird && el("button", { class: "pill", type: "button",
        text: "Assembler le panorama",
        title: "Si les vues de la séquence se suivent comme celles d'un panorama, les assembler en une image, écrite à côté d'elles",
        onclick: async (event) => {
          event.currentTarget.disabled = true;
          said.textContent = "Assemblage du panorama…";
          const reply = await post("/api/pano", { name: photo.name });
          said.textContent = reply.error || `Panorama assemblé : ${reply.photo.name}. Cherchez « panorama » pour le voir.`;
          event.target.disabled = false;
        } })),
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
  state.rawApp = facets.raw_app;
  state.facesCan = facets.faces;
  state.panoCan = facets.pano;
  for (const [name] of facets.people || []) $("personList").append(el("option", { value: name }));
  $("viewerImg").addEventListener("load", drawFaces);
  window.addEventListener("resize", drawFaces);
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
  setSection($("section").value, true);
  for (const tab of $("sections").querySelectorAll(".tab")) tab.addEventListener("click", () => setSection(tab.dataset.section));
  state.checklist = asked.get("checklist") || "";
  showChecklistChip();

  let timer = null;
  const soon = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); };
  for (const key of FIELDS) {
    if (key !== "section") $(key).addEventListener($(key).tagName === "SELECT" || $(key).type === "date" ? "change" : "input", soon);
  }
  $("listed").addEventListener("change", () => load(true));
  $("filters").addEventListener("submit", (event) => { event.preventDefault(); load(true); });
  $("checklistChip").addEventListener("click", () => setFilter({ checklist: "" }));
  $("reset").addEventListener("click", () => {
    for (const key of FIELDS) $(key).value = key === "sort" ? "new" : "";
    state.checklist = "";
    showChecklistChip();
    setSection("");
  });

  $("mapToggle").addEventListener("click", () => {
    $("map").hidden = !$("map").hidden;
    $("mapToggle").textContent = $("map").hidden ? "Afficher la carte" : "Masquer la carte";
    try { localStorage.setItem("patchdoc.carte", $("map").hidden ? "non" : "oui"); } catch (err) { /* private mode */ }
    if (!$("map").hidden) { showPlaces(); if (chart.map) chart.map.invalidateSize(); }
  });
  try {
    if (localStorage.getItem("patchdoc.carte") === "non") {
      $("map").hidden = true;
      $("mapToggle").textContent = "Afficher la carte";
      $("mapCard").hidden = false;
    }
  } catch (err) { /* private mode */ }
  $("batchGo").addEventListener("click", batchGo);
  $("batchStop").addEventListener("click", async () => { await post("/api/batch/stop"); watchBatch(); });
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
  watchBatch();         // a batch started before the page was opened goes on
}

start().catch((err) => { $("status").textContent = "La page n'a pas pu démarrer : " + err.message; });
