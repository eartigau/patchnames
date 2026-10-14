/*
 * patchdoc search page. The server (patchnames/gui.py) answers:
 *   /api/facets, /api/search, /api/sequence, /api/checklist,
 *   /thumb/NAME, /preview/NAME, and POST /api/reveal, /api/open.
 * Everything that comes from the index (titles, descriptions, eBird text) is
 * put in the page as text, never as HTML. What the page says is in French.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = ["section", "place_of", "q", "species", "person", "place", "region", "year", "since", "until", "topic",
                "eye", "clear", "level", "flag", "sort"];
const PAGE = 60;
const state = { items: [], offset: 0, done: false, loading: false, token: 0, current: -1, checklist: "", total: 0,
                frames: [], versions: {} };
// (state.versions: for the photos whose pictures were made again another way
// up, how many times: their addresses change, past what the browser kept)
const versioned = (rows) => { for (const row of rows || []) if (row && row.turned) state.versions[row.name] = row.turned; };
// (state.names: the names offered for a face, those the photographer starts
// with and those given so far)
const NEW = "\u0000nouveau";      // the choice "ajouter un nom" of a face, "autre espèce" of a species
const UNKNOWN = "\u0000indéterminé";       // the choice of a bird that cannot be named
// the notes of a bird photo, 1 to 5: column of the index, what the page calls it
const NOTES = [["eye", "œil net"], ["clear", "dégagé"], ["level", "hauteur d'œil"]];
// the batch of the buttons above the results: how many finished sequences
// this page has taken in, its timer, when each button was first clicked, and
// what the server says of the whole archive ({on, left}; null from a server
// that cannot do it)
const batch = { since: 0, timer: null, armed: 0, allArmed: 0, archive: null };

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
// (a picture made again another way up is asked for anew: see state.versions)
const image = (kind, name) => `/${kind}/${encodeURIComponent(name)}` + (state.versions[name] ? "?v=" + state.versions[name] : "");
const badge = (text, kind) => el("span", { class: "badge" + (kind ? " " + kind : ""), text });

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
  if (!chart.layer) return;         // (a search without a place: no map was made)
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

// the order of the gallery, chosen with the buttons above it
function setSort(sort, quiet) {
  const known = [...$("sorts").querySelectorAll(".tab")].some((tab) => tab.dataset.sort === sort);
  $("sort").value = known ? sort : "new";
  for (const tab of $("sorts").querySelectorAll(".tab")) tab.classList.toggle("on", tab.dataset.sort === $("sort").value);
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
  const token = state.token, from = state.offset;
  let empty = false;        // a page without a photo, of a search that is not over
  state.loading = true;
  $("status").textContent = "Recherche…";
  const params = query();
  params.set("offset", state.offset);
  params.set("limit", PAGE);
  try {
    const data = await (await fetch("/api/search?" + params)).json();
    if (token !== state.token) return;          // a newer search took over
    if (data.error) throw new Error(data.error);
    versioned(data.photos);
    for (const photo of data.photos) {
      state.items.push(photo);
      $("grid").append(tile(photo, state.items.length - 1));
    }
    // (the view of a copy of the archive skips the photos that copy does not have)
    state.offset = data.next === undefined ? state.offset + data.photos.length : data.next;
    state.done = data.more === undefined ? data.photos.length < PAGE : !data.more;
    state.total = data.totals.sequences;
    totals(data.totals);
    batchButton();
    empty = !data.photos.length && !state.done;
    $("status").textContent = state.items.length
      ? (state.done ? "" : `${number(state.items.length)} séquences affichées sur ${number(state.total)} ; faites défiler pour la suite.`)
      : empty ? "Recherche…" : "Rien ne correspond. Élargissez un filtre ou essayez un autre mot.";
  } catch (err) {
    if (token === state.token) $("status").textContent = "La recherche a échoué : " + err.message;
  } finally {
    if (token === state.token) state.loading = false;
  }
  // a short page of a search that is not over (the view of a copy of the
  // archive skips what that copy does not have): the next one follows while
  // the end of the gallery is still on screen
  if (token === state.token && !state.done && state.offset > from
      && (empty || $("more").getBoundingClientRect().top < window.innerHeight + 800)) load(false);
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
  $("batch").hidden = !state.claude || (!state.items.length && !batch.archive);
  $("batchGo").hidden = !state.items.length;
  archiveButton();
  const count = undescribed().length;
  batch.armed = 0;
  $("batchGo").disabled = !count;
  $("batchGo").textContent = count
    ? `Identifier avec Claude ${count > 1 ? "les " + number(count) + " séquences affichées" : "la séquence affichée"} sans description`
    : "Toutes les séquences affichées sont décrites";
  $("batchGo").title = "Claude regarde en arrière-plan chaque séquence affichée qui n'a pas de description, "
    + "deux à la fois ; les réponses arrivent dans la page et dans la base, puis sont écrites à la suite dans l'en-tête des fichiers. "
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

// the button of the whole archive: every sequence of it that has no
// description, whatever the page shows (left alone while it waits for its
// second click)
function archiveButton() {
  const info = batch.archive;
  $("batchAll").hidden = !info;
  if (!info || Date.now() - batch.allArmed < 8000) return;
  $("batchAll").disabled = info.on || !info.left;
  $("batchAll").textContent = info.on ? "Toute l'archive est en cours d'identification"
    : info.left ? `Identifier toute l'archive : ${plural(info.left, "séquence", "séquences")} sans description`
    : "Toute l'archive est décrite";
  $("batchAll").title = "Claude regarde en arrière-plan chaque séquence de l'archive qui n'a pas de description, "
    + "les plus récentes d'abord, deux à la fois. Le travail continue si la page est fermée, et reprend si "
    + "`patchdoc gui` est relancé, jusqu'à ce que « Arrêter » y mette fin. Les séquences demandées ici entre-temps passent devant.";
}

async function batchAll() {
  const info = batch.archive;
  if (!info || info.on || !info.left) return;
  if (Date.now() - batch.allArmed > 8000) {     // a first click says what a second one will do
    batch.allArmed = Date.now();
    $("batchAll").textContent = `Confirmer : ${plural(info.left, "séquence", "séquences")} à montrer à Claude, en arrière-plan`
      + (info.left > 1000 ? " (des jours de travail)" : "");
    return;
  }
  batch.allArmed = 0;
  const reply = await post("/api/batch", { all: true });
  if (reply.error) {
    archiveButton();
    $("batchStatus").textContent = reply.error;
    return;
  }
  batch.since = 0;
  watchBatch();
}

// seconds as a clock: 75 -> "1:15", 3700 -> "1 h 01"
function clock(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} h ${String(minutes).padStart(2, "0")}` : `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

// how far the batch is, like a progress bar in a terminal: the share done,
// the time it has run, the time left at this pace, then what each sequence
// under way is at and the writing into the files that follows
function showBatch(status) {
  const finished = status.done + status.failed;
  // (a server started before it timed its batches: this page times what it sees)
  if (status.running && !batch.t0) Object.assign(batch, { t0: Date.now(), d0: finished });
  if (!status.running) batch.t0 = 0;
  const timed = status.seconds !== undefined;
  const seconds = timed ? status.seconds : batch.t0 ? (Date.now() - batch.t0) / 1000 : 0;
  // the bar counts photos: those of the sequences done, and half of those read
  // of the sequences under way (Claude has yet to look at them)
  let share, left = null;
  if (status.photos) {
    const made = status.photos_done + 0.5 * (status.photos_read || 0);
    share = Math.min(1, made / status.photos);
    if (made > 0 && seconds >= 20) left = seconds * (status.photos - made) / made;
  } else {
    const seen = finished - (batch.d0 || 0);        // finished while this page was watching
    share = status.total ? finished / status.total : 0;
    if (seen > 0) left = seconds * (status.total - finished) / seen;
  }
  $("batchBar").hidden = !status.running;
  $("batchFill").style.width = (100 * share).toFixed(1) + "%";
  const failed = (status.failed ? `, ${plural(status.failed, "échec", "échecs")}` : "")
    + (status.retry ? `, ${status.retry} à réessayer` : "");
  const whole = status.archive && status.archive.on;
  const parts = status.running
    ? [`${whole ? "Toute l'archive" : "Claude"} en arrière-plan : ${finished} sur ${plural(status.total, "séquence", "séquences")}${failed}`,
       Math.round(100 * share) + " %",
       seconds ? clock(seconds) + (timed ? " écoulées" : " de suivi") : "",
       left === null ? "temps restant : encore inconnu" : "reste environ " + clock(left),
       finished && timed ? Math.round(seconds / finished) + " s par séquence" : ""]
    : [`Identification terminée : ${status.done} sur ${plural(status.total, "séquence", "séquences")}${failed}`,
       timed && seconds ? "en " + clock(seconds) : ""];
  $("batchStatus").textContent = !status.total && !status.note ? ""
    : parts.filter(Boolean).join(" · ") + "." + (status.note ? " " + status.note : "");
  const lines = (status.jobs || []).map((job) => {
    const reading = job.frames && job.measured < job.frames;
    return `▸ ${job.seq} · ${plural(job.photos, "vue", "vues")} · `
      + (reading ? `lecture des vues ${job.measured} sur ${job.frames}`
        : job.stage === "yeux" ? "Claude regarde l'œil de chaque vue de près" : "Claude regarde la séquence")
      + ` · ${clock(job.seconds)}`;
  });
  // a batch that waits: Claude at its usage limit, the network or the drive gone
  if (status.hold) {
    lines.unshift(`En attente encore ${clock(status.hold)} : plusieurs séquences de suite ont échoué `
      + "(limite d'utilisation de Claude, réseau ou disque ?). La file reprend ensuite, et ces séquences seront réessayées."
      + (status.why ? " Dernière erreur : " + status.why : ""));
  } else if (status.away) {
    lines.unshift("Le disque d'archive n'est plus monté : la file attend son retour.");
  }
  const writes = status.writes || {};
  if (writes.seq) {
    lines.push(`Écriture dans les fichiers : ${writes.files ? `fichier ${writes.written} sur ${writes.files}` : "début"} de ${writes.seq}`
      + (writes.waiting > 1 ? `, puis ${plural(writes.waiting - 1, "séquence", "séquences")}` : "") + ".");
  }
  if (writes.error) lines.push("Écriture en échec, elle sera reprise : " + writes.error);
  if (status.running && !status.jobs) lines.push("Ce serveur date d'avant le suivi détaillé : relancez `patchdoc gui` pour voir ce que fait chaque séquence.");
  $("batchJobs").textContent = lines.join("\n");
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
  versioned(status.finished.map((item) => item.photo));
  for (const item of status.finished) {
    const index = state.items.findIndex((photo) => photo.seq === item.seq);
    if (index >= 0 && item.photo) {
      Object.assign(state.items[index], item.photo);
      refresh(index);
    }
  }
  batch.since = status.next;
  batch.archive = status.archive || null;
  $("batchStop").hidden = !status.running;
  showBatch(status);
  archiveButton();
  if (status.finished.length || !status.running) batchButton();
  // (the files are still being written after the last sequence is identified)
  if (status.running || (status.writes && status.writes.seq)) batch.timer = setTimeout(watchBatch, 2000);
}

/* ── What the page looks at: the archive, or the photos that wait to go there ── */

// (armed, at: the button of its actions that was clicked once, and when)
const inbox = { timer: null, armed: "", at: 0 };

// at the top of the page: everything, the archive, or the photos that wait in
// the folders of config 'inbox_dirs' (~/photo) to be sent to the archive.
// Choosing those looks at the folders again, then shows what is there, to
// describe and note it where it is.
async function showInbox(data) {
  clearTimeout(inbox.timer);
  if (!data) {
    try { data = await (await fetch("/api/inbox")).json(); } catch (err) { return; }
  }
  if (!data.dirs) return;          // a server from before this
  $("inbox").hidden = false;
  const where = $("place_of").value;
  const choose = async (value) => {
    $("place_of").value = value;
    if (value === "attente") return showInbox(await post("/api/inbox"));       // a fresh look at the folders
    showInbox();
    load(true);
  };
  const waiting = `${data.dirs[0] || "~/photo"} · pas encore archivé` + (data.photos ? ` (${number(data.photos)})` : "");
  // an archive that has several copies: each is a place to look at
  const copies = (data.copies || []).length > 1 ? data.copies : [];
  const chips = [["", "Tout", "L'archive et les photos qui attendent"]];
  for (const copy of copies) {
    chips.push([copy.root, `Archive ${copy.name}` + (copy.used ? " · en usage" : copy.mounted ? "" : " · pas branchée"),
                copy.mounted ? `Ce que la copie ${copy.name} de l'archive contient ; la page lit et écrit alors sur cette copie (${copy.root})`
                  : `${copy.root} n'est pas branchée`, copy]);
  }
  if (!copies.length) chips.push(["archive", "L'archive", "Seulement les photos déjà dans l'archive"]);
  chips.push(["attente", waiting, "Les photos posées dans " + data.dirs.join(", ") + " : les décrire et les noter avant de les envoyer à l'archive"]);
  $("inboxChoice").replaceChildren(...chips.map(([value, label, title, copy]) =>
    el("span", { class: "chip" + (where === value ? " on" : copy && !copy.mounted ? " off" : ""), text: label, title,
                 onclick: !copy ? () => choose(value) : copy.mounted ? () => lookAt(copy) : null })));
  const shown = copies.find((copy) => copy.root === where), look = (shown && shown.look) || {};
  let text = "";
  if (data.state === "reading") {
    text = `Lecture des photos de ${data.dirs.join(", ")}…` + (data.total ? ` ${data.read} sur ${data.total}` : "");
    inbox.timer = setTimeout(showInbox, 1000);
  } else if (data.state === "error") {
    text = "La lecture a échoué : " + data.error;
  } else if (shown) {
    // what the copy holds, from the last time it was listed
    const found = look.read
      ? `Archive ${shown.name} : ${plural(look.photos, "photo", "photos")}`
        + (look.new ? `, dont ${plural(look.new, "nouvelle", "nouvelles")} pour la base` : "")
        + (look.absent ? ` ; ${plural(look.absent, "photo", "photos")} de la base n'y ${look.absent > 1 ? "sont" : "est"} pas` : "")
        + (look.outside ? ` ; ${plural(look.outside, "image", "images")} de ses autres dossiers ${look.outside > 1 ? "ne sont" : "n'est"} pas dans la base` : "")
        + ` (lue le ${look.read}).`
      : "";
    text = look.state === "error" ? `La lecture de ${shown.name} a échoué : ${look.error}`
      : look.state === "reading" ? `Lecture de ce que contient ${shown.name}… `
        + (found || "La galerie ne montre déjà que les photos qui s'y trouvent ; les totaux seront justes une fois la lecture finie.")
      : found || `${shown.name} n'a pas encore été lue en entier : la galerie ne montre que les photos qui s'y trouvent, `
        + "mais les totaux comptent toute l'archive. Un clic sur sa puce la lit.";
  } else if (where === "attente") {
    text = data.files
      ? `${plural(data.files, "photo attend", "photos attendent")} dans ${data.dirs.join(", ")}`
        + (data.new ? ` ; ${plural(data.new, "nouvelle", "nouvelles")}` : "")
        + (data.elsewhere ? ` ; ${plural(data.elsewhere, "laissée", "laissées")} de côté (la base a déjà ce nom ailleurs)` : "")
        + (data.plan ? ". Les boutons ci-dessous les mettent à l'heure, les renomment et les envoient à l'archive, avec ce qui a été noté."
          : ". Une fois décrites, `patchdoc` dans leur dossier les renomme et les archive avec ce qui a été noté.")
      : `Aucune photo dans ${data.dirs.join(", ")}.`;
  }
  $("inboxStatus").textContent = text;
  if (data.state === "done" && inbox.was === "reading") load(true);      // what was just read
  inbox.was = data.state;
  showActions(data, where === "attente" && data.state !== "reading");
  const act = data.act || {};
  if (act.state === "running" && data.state !== "reading") inbox.timer = setTimeout(showInbox, 1000);
  else if (look.state === "reading" && data.state !== "reading") inbox.timer = setTimeout(showInbox, 1500);
  if (shown && look.state === "done" && inbox.looked === "reading") load(true);     // what the copy was just found to hold
  inbox.looked = shown ? look.state : "";
  if (act.state !== "running" && inbox.acted === "running") load(true);  // the files changed name or place
  inbox.acted = act.state;
}

// show what a copy of the archive holds: the page reads and writes on that
// copy from then on, and the server lists it again in the background
async function lookAt(copy) {
  const reply = await post("/api/archive/look", { root: copy.root });
  if (reply.error) {
    $("inboxStatus").textContent = reply.error === "refusé"
      ? "Ce serveur date d'avant cette fonction : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
      : reply.error;
    return;
  }
  $("place_of").value = copy.root;
  if (!copy.used) {
    history.replaceState(null, "", "?" + query());
    return location.reload();         // (the photos are read from another drive: everything is asked again)
  }
  showInbox();
  load(true);
}

// the three buttons of the photos that wait: put their hours right, name
// them, send them to the archive. A first click says what a second one does;
// the work is done by the server, one action at a time, with a bar.
function showActions(data, shown) {
  const plan = data.plan, act = data.act || {};
  $("inboxActions").hidden = !shown || !plan;       // (a server from before this has no plan)
  if (!shown || !plan) {
    $("inboxBar").hidden = true;
    $("inboxAct").textContent = "";
    return;
  }
  const running = act.state === "running";
  const armed = (what) => inbox.armed === what && Date.now() - inbox.at < 8000;
  const go = async (what) => {
    if (!armed(what)) {
      Object.assign(inbox, { armed: what, at: Date.now() });
      return showActions(data, shown);
    }
    inbox.armed = "";
    const [action, root] = what.split("|");
    const reply = await post("/api/inbox/act", root ? { what: action, root } : { what: action });
    if (!reply.dirs) return;
    await showInbox(reply);
    if (reply.refused) $("inboxAct").textContent = reply.refused;
  };
  const clocks = plan.offsets.length === 1 ? "horloge " + plan.offsets[0][0]
    : plan.offsets.map(([hours, count]) => `${hours} pour ${number(count)}`).join(", ");
  // (a server from before the choice of the archive only sends to the one in use)
  const archives = plan.archives || [Object.assign({ root: "", used: true }, plan.archive)];
  const button = (what, text, confirm, title, can) => el("button", {
    class: "pill" + (can && !running ? " go" : ""), type: "button", disabled: (running || !can) && "disabled",
    text: armed(what) ? confirm : text, title, onclick: () => go(what) });
  const buttons = [
    button("times",
      plan.retime ? `Mettre à jour les heures : ${plural(plan.retime, "photo", "photos")} (${clocks})` : "Les heures sont à jour",
      `Confirmer : heure corrigée dans ${plural(plan.retime, "fichier", "fichiers")} (${clocks}), et noms refaits`,
      "Écrire dans les fichiers l'heure que patchdoc a trouvée d'après vos listes eBird pour les photos dont l'horloge de l'appareil était décalée, "
        + "puis leur donner le nom de leur nouvelle heure. Les photos dont l'horloge était juste ne sont pas touchées.",
      plan.retime > 0),
    button("names",
      "Renommer comme patchnames" + (plan.odd ? ` : ${plural(plan.odd, "nom", "noms")} à refaire` : ""),
      "Confirmer : chaque fichier prend le nom de sa date, de son heure et de son appareil",
      "Donner à chaque fichier son nom canonique (AAMMJJ_HHMMSS_QUI_NUMÉRO), comme `patchnames`. Rien n'est effacé : "
        + "un fichier dont le nom est déjà pris est laissé tel quel.",
      plan.files > 0),
    // one button for each copy of the archive: the files go to the one chosen
    ...archives.map((copy) => button("send|" + copy.root,
      copy.mounted ? `Envoyer vers ${copy.name} : ${plural(plan.files, "fichier", "fichiers")} (${size(plan.bytes)})`
        : `${copy.name} n'est pas ${copy.root.startsWith("/Volumes/") ? "branchée" : "montée"}`,
      `Confirmer : ${plural(plan.files, "fichier déplacé", "fichiers déplacés")} vers l'archive ${copy.name}`
        + (plan.retime ? ` ; attention, ${plural(plan.retime, "photo a", "photos ont")} encore une horloge décalée` : ""),
      `Déplacer chaque fichier vers la copie ${copy.name} de l'archive, dans le dossier de son mois, sous oiseaux ou autres selon l'objectif, `
        + "après lui avoir donné son nom canonique. Un fichier déjà dans l'archive est laissé ici ; rien n'est écrasé. "
        + "L'autre copie le recevra à la prochaine copie d'un disque à l'autre.",
      plan.files > 0 && copy.mounted)),
  ];
  if (running) buttons.push(el("button", { class: "pill", type: "button", text: "Arrêter", onclick: async () => showInbox(await post("/api/inbox/stop")) }));
  $("inboxActions").replaceChildren(...buttons);

  const left = act.left ? ` ${plural(act.left, "fichier laissé tel quel", "fichiers laissés tels quels")} : `
    + (act.problems || []).join(" ; ") + (act.left > (act.problems || []).length ? " ; …" : "") + "." : "";
  let text = "", share = 0;
  if (running) {
    share = act.what === "send" && act.all ? act.bytes / act.all : act.total ? act.done / act.total : 0;
    const ahead = share > 0 && act.seconds >= 10 ? "reste environ " + clock(act.seconds * (1 - share) / share) : "";
    text = (act.what === "times" ? [`Mise à l'heure : ${number(act.done)} sur ${plural(act.total, "photo", "photos")}`]
      : act.what === "names" ? [`Noms : ${number(act.done)} sur ${plural(act.total, "fichier", "fichiers")}`,
                                `${number(act.renamed || 0)} renommés`]
      : [`Envoi vers ${act.to || "l'archive"} : ${number(act.done)} sur ${plural(act.total, "fichier", "fichiers")}`,
         act.all ? `${size(act.bytes)} sur ${size(act.all)}` : ""])
      .concat([clock(act.seconds || 0) + " écoulées", ahead]).filter(Boolean).join(" · ") + ".";
  } else if (act.state === "error") {
    text = "L'action a échoué : " + act.error;
  } else if (act.state === "done") {
    text = (act.what === "times" ? `${plural(act.done, "photo mise", "photos mises")} à l'heure, ${number(act.renamed)} renommées.`
      : act.what === "names" ? `${plural(act.renamed, "fichier renommé", "fichiers renommés")} sur ${number(act.total)} ; les autres avaient déjà leur nom.`
      : `${plural(act.sent, "fichier envoyé", "fichiers envoyés")} vers l'archive ${act.to} en ${clock(act.seconds || 0)}.`)
      + (act.stopped ? " Arrêté avant la fin." : "") + left;
  }
  $("inboxBar").hidden = !running;
  $("inboxFill").style.width = (100 * Math.min(1, share)).toFixed(1) + "%";
  $("inboxAct").textContent = text;
}

/* ── The backup of the index on GitHub ───────────────────────────────── */

const backup = { timer: null };

// the index lives on this machine: a button sends a copy of it, compressed
// and encrypted with a password, to a private GitHub repository (backup.py)
async function showBackup(data) {
  clearTimeout(backup.timer);
  if (!data) {
    try { data = await (await fetch("/api/backup")).json(); } catch (err) { return; }
  }
  if (data.state === undefined) return;         // a server from before this
  $("backup").hidden = false;
  const busy = data.state === "running";
  const stages = { copie: "copie de la base", compression: "compression", chiffrement: "chiffrement, et relecture pour vérifier",
                   envoi: "envoi vers GitHub" };
  $("backupActions").replaceChildren(el("button", {
    class: "pill" + (data.password && !busy ? " go" : ""), type: "button", disabled: (busy || !data.password) && "disabled",
    text: busy ? "Sauvegarde en cours…" : "Sauvegarder la base sur GitHub",
    title: "Une copie de la base de recherche, sans les vignettes, compressée puis chiffrée avec votre mot de passe sur cette machine, "
      + "est envoyée dans un dépôt GitHub privé. Les dernières sont gardées.",
    onclick: async () => showBackup(await post("/api/backup")) }));
  const last = data.last && data.last.when
    ? `Dernière sauvegarde : ${data.last.when} (${size(data.last.bytes)}, chiffrée), dans le dépôt privé ${data.last.repo}.`
    : "Aucune sauvegarde pour l'instant.";
  $("backupStatus").textContent = busy ? `Sauvegarde : ${stages[data.stage] || "début"}…`
    : data.state === "error" ? `La sauvegarde a échoué : ${data.error}. ${last}`
    : !data.password ? "Donnez d'abord un mot de passe, une fois, dans un terminal : patchdoc sauvegarde --mot-de-passe. " + last
    : last;
  if (busy) backup.timer = setTimeout(showBackup, 1500);
}

/* ── The copies of the archive: which one is used, and rsync between them ── */

// bytes as a size: 12345678 -> "12,3 Mo"
function size(bytes) {
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let value = Number(bytes || 0), unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
  return value.toLocaleString("fr-CA", { maximumFractionDigits: value < 100 && unit ? 1 : 0 }) + " " + units[unit];
}

const archive = { timer: null };

async function archiveDo(path, body) {
  const reply = await post(path, body);
  showArchive(reply);
  return reply;
}

// the archive lives on drives that are copies of each other: say which one
// is used (a click on another that is there changes it), and bring to one
// what the other has that is newer, with how far that copy is
async function showArchive(data) {
  clearTimeout(archive.timer);
  if (!data) {
    try { data = await (await fetch("/api/archive")).json(); } catch (err) { return; }
  }
  if (!data.copies || data.copies.length < 2) return;        // one archive only, or a server from before this
  $("archive").hidden = false;
  const job = data.sync || {}, busy = ["planning", "running", "backup"].includes(job.state);
  $("archiveCopies").replaceChildren(...data.copies.map((copy) => el("span", {
    class: "chip" + (copy.used ? " on" : copy.mounted ? "" : " off"),
    text: copy.name + (copy.used ? " · utilisée" : copy.mounted ? "" : copy.server ? " · sur le serveur, pas montée" : " · pas branchée"),
    title: copy.used ? "La page lit et écrit sur cette copie ; un clic montre ce qu'elle contient : " + copy.root
      : copy.mounted ? "Utiliser cette copie et voir ce qu'elle contient : " + copy.root : copy.root,
    onclick: copy.mounted && !busy ? () => lookAt(copy) : null,
  })));
  // a copy can give or take when it is there, or when rsync can reach its server
  const there = (copy) => copy.mounted || copy.server;
  const pairs = data.copies.flatMap((from) => data.copies.filter((to) => to !== from).map((to) => [from, to]));
  const buttons = busy || job.state === "planned" ? [] : pairs.map(([from, to]) => el("button", {
    class: "pill", type: "button", disabled: !(there(from) && there(to)) && "disabled",
    text: `${from.name} → ${to.name}`,
    title: there(from) && there(to)
      ? `Comparer, puis copier vers ${to.name} ce que ${from.name} a de plus récent ou que ${to.name} n'a pas ; rien n'est effacé`
      : `Branchez ${there(from) ? to.name : from.name} pour copier`,
    onclick: () => archiveDo("/api/sync", { src: from.root, dst: to.root }),
  }));
  const pair = `${job.from_name} vers ${job.to_name}`;
  let text = data.error || "", bar = false;
  if (job.state === "planning") {
    // a bar as tqdm draws one: the share, the count, [elapsed < left, pace]
    const seen = job.seen || 0, seconds = job.seconds || 0;
    const share = job.total ? Math.min(0.99, seen / job.total) : 0;
    const pace = seconds >= 3 && seen ? seen / seconds : 0;
    const left = pace && job.total > seen ? (job.total - seen) / pace : null;
    bar = true;
    $("syncFill").style.width = (100 * share).toFixed(1) + "%";
    $("syncBar").classList.toggle("unknown", !job.total);
    text = `Comparaison de ${job.from_name} et de ${job.to_name} : `
      + (job.total ? `${Math.floor(100 * share)} % · ${number(seen)} / ${number(job.total)} fichiers` : `${number(seen)} fichiers`)
      + ` [${clock(seconds)}` + (left === null ? "" : ` < ${clock(left)}`)
      + (pace ? `, ${number(Math.round(pace))} fichiers/s` : "") + "]"
      + (job.seen === undefined ? " (relancez `patchdoc gui` pour voir l'avancement)" : "");
    buttons.push(el("button", { class: "pill", type: "button", text: "Arrêter", onclick: () => archiveDo("/api/sync/stop") }));
  } else if (job.state === "planned") {
    text = job.files
      ? `${plural(job.files, "fichier", "fichiers")} (${size(job.bytes)}) plus récents ou absents, à copier de ${pair}.`
      : `Rien à copier de ${pair} : ${job.to_name} a déjà tout ce que ${job.from_name} a de plus récent.`;
    if (job.files) {
      buttons.push(el("button", { class: "pill go", type: "button", text: `Copier de ${pair}`,
                                  onclick: () => archiveDo("/api/sync", { src: job.src, dst: job.dst, go: true }) }));
    }
    buttons.push(el("button", { class: "pill", type: "button", text: job.files ? "Annuler" : "D'accord", onclick: () => archiveDo("/api/sync/stop") }));
  } else if (job.state === "running") {
    $("syncBar").classList.remove("unknown");
    const share = job.bytes ? Math.min(1, job.sent / job.bytes) : job.files ? Math.min(1, job.copied / job.files) : 0;
    const left = share > 0 && job.seconds >= 10 ? job.seconds * (1 - share) / share : null;
    bar = true;
    $("syncFill").style.width = (100 * share).toFixed(1) + "%";
    text = [`Copie de ${pair} : ${number(job.copied)} sur ${plural(job.files, "fichier", "fichiers")}`,
            job.bytes ? `${size(job.sent)} sur ${size(job.bytes)}` : "", Math.round(100 * share) + " %", job.pace,
            clock(job.seconds || 0) + " écoulées", left === null ? "" : "reste environ " + clock(left)].filter(Boolean).join(" · ") + ".";
    buttons.push(el("button", { class: "pill", type: "button", text: "Arrêter", onclick: () => archiveDo("/api/sync/stop") }));
  } else if (job.state === "backup") {
    text = `Copie de ${pair} faite. Sauvegarde de la base de recherche sur les disques…`;
  } else if (job.state === "done") {
    text = `Copie terminée de ${pair} : ${plural(job.files, "fichier", "fichiers")} (${size(job.bytes)}) en ${clock(job.seconds || 0)}.`;
  } else if (job.state === "stopped") {
    text = `Copie de ${pair} arrêtée après ${plural(job.copied || 0, "fichier", "fichiers")} ; elle peut être relancée, elle reprendra là.`;
  } else if (job.state === "error") {
    text = `La copie de ${pair} a échoué : ${job.error}`;
  }
  $("archiveActions").replaceChildren(
    el("span", { class: "label", text: busy || job.state === "planned" ? "" : "Copier le plus récent" }), ...buttons);
  // (the index lives on this machine only: a copy of it is left on the drives)
  if ((job.saved || []).length && ["done", "planned"].includes(job.state)) {
    text += ` Base de recherche sauvegardée sur ${job.saved.join(" et ")} (dossier patchdoc).`;
  }
  // (the duplicates put aside on one copy by `patchdoc doublons` are put aside on the other too)
  if (job.aside && text) text += ` ${plural(job.aside, "doublon rangé", "doublons rangés")} de côté sur l'autre copie, dans son dossier doublons.`;
  $("syncBar").hidden = !bar;
  $("syncStatus").textContent = text;
  if (busy) archive.timer = setTimeout(showArchive, 2000);
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
      photo.on_checklist === 0 && el("span", { class: "mark alert", text: "≠" }),
      hasEye(photo) && loupeIcon((event) => loupe(event, photo))));
  const date = (photo.local || photo.taken || "").slice(0, 10);
  // a title that is a species: its French name, and its Latin name on a line of its own
  const named = /^([^()]+) \(([^()]+)\)$/.exec(photo.title || "");
  const one = named && split(photo.latin).includes(named[2]);
  const figure = el("figure", { class: "gallery-item", onclick: () => openViewer(index) }, img, marks,
    el("figcaption", {}, photo.title
      ? [el("div", { class: "name", text: one ? named[1] : photo.title }),
         one && el("div", { class: "latin", text: named[2] }),
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

/* ── The loupe: the eye of a bird at full resolution, under the cursor ── */

const hasEye = (item) => Boolean(item) && item.eye_x !== null && item.eye_x !== undefined;
const LOUPE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true">'
  + '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.6 15.6 21 21"/></svg>';

// a small magnifier on a photo whose eye is known. The loupe only comes up
// with the cursor on it (or a click, on a screen without a cursor): shown at
// every move over the photos, it got in the way. show(event) brings it up.
function loupeIcon(show, extra) {
  const icon = el("span", { class: "loupe-icon" + (extra ? " " + extra : ""), title: "L'œil à pleine résolution", role: "img",
                            "aria-label": "Loupe sur l'œil" });
  icon.innerHTML = LOUPE_SVG;             // (a constant of this page, nothing from the index)
  icon.addEventListener("mouseenter", show);
  icon.addEventListener("mousemove", show);
  icon.addEventListener("mouseleave", hideLoupe);
  icon.addEventListener("click", (event) => {
    event.stopPropagation();              // (not a click on the photo under it)
    if ($("loupe").hidden) show(event); else hideLoupe();
  });
  return icon;
}

// show next to the cursor the square around the eye of a photo (a frame of
// the viewer, a tile); nothing for a photo whose eye is not known. avoid: a
// point of the window the loupe should not cover (the eye itself, on the picture)
function loupe(event, item, avoid) {
  const box = $("loupe"), img = box.firstElementChild;
  if (!hasEye(item)) return hideLoupe();
  // (the address changes with the place of the eye: a sequence described again shows its new square)
  const src = `/eye/${encodeURIComponent(item.name)}?v=${item.eye_x}_${item.eye_y}`;
  if (img.dataset.src !== src) {
    img.dataset.src = src;
    img.src = src;
  }
  box.lastElementChild.textContent = [item.eye && `œil net ${item.eye}/5`, item.best && "★ meilleure vue"].filter(Boolean).join(" · ");
  box.hidden = false;
  // one of the four corners around the cursor: inside the window, and off the eye if it can
  const size = box.offsetWidth, gap = 24;
  const spots = [[1, 1], [-1, 1], [1, -1], [-1, -1]].map(([dx, dy]) => ({
    x: dx > 0 ? event.clientX + gap : event.clientX - gap - size,
    y: dy > 0 ? event.clientY + gap : event.clientY - gap - size }));
  const inside = (spot) => spot.x >= 0 && spot.y >= 0 && spot.x + size <= window.innerWidth && spot.y + size <= window.innerHeight;
  const clear = (spot) => !avoid || avoid.x < spot.x - 20 || avoid.x > spot.x + size + 20 || avoid.y < spot.y - 20 || avoid.y > spot.y + size + 20;
  const spot = spots.find((one) => inside(one) && clear(one)) || spots.find(inside) || spots[0];
  box.style.left = Math.max(4, spot.x) + "px";
  box.style.top = Math.max(4, spot.y) + "px";
}

function hideLoupe() {
  $("loupe").hidden = true;
  $("viewerEye").replaceChildren();
}

// the frame on screen in the viewer, with what is known of its eye
function shownFrame() {
  const photo = state.items[state.current];
  return state.frames.find((frame) => frame.name === state.shown) || (photo && photo.name === state.shown ? photo : null);
}

// the magnifier of the viewer, in a corner of the picture as it is drawn, when
// the eye of the frame on screen is known
function placeViewerLoupe() {
  const icon = $("viewerLoupe"), img = $("viewerImg"), frame = shownFrame();
  icon.hidden = !hasEye(frame) || !img.naturalWidth;
  if (icon.hidden) return;
  const shown = img.getBoundingClientRect(), stage = icon.parentElement.getBoundingClientRect();
  const scale = Math.min(shown.width / img.naturalWidth, shown.height / img.naturalHeight);
  icon.style.left = shown.left - stage.left + (shown.width - img.naturalWidth * scale) / 2 + 10 + "px";
  icon.style.top = shown.top - stage.top + (shown.height - img.naturalHeight * scale) / 2 + 10 + "px";
}

// the magnifier of the viewer under the cursor: the loupe, and a ring where the eye is
function overViewer(event) {
  const frame = shownFrame(), img = $("viewerImg"), ring = $("viewerEye");
  if (!hasEye(frame) || !img.naturalWidth) return loupe(event, frame);
  const shown = img.getBoundingClientRect(), stage = ring.getBoundingClientRect();
  const scale = Math.min(shown.width / img.naturalWidth, shown.height / img.naturalHeight);
  const width = img.naturalWidth * scale, height = img.naturalHeight * scale;
  const eye = { x: shown.left + (shown.width - width) / 2 + frame.eye_x * width,
                y: shown.top + (shown.height - height) / 2 + frame.eye_y * height };
  loupe(event, frame, eye);
  ring.replaceChildren(el("div", { class: "eye-ring", style: `left:${eye.x - stage.left}px;top:${eye.y - stage.top}px` }));
}

/* ── Viewer ─────────────────────────────────────────────────────────── */

// an hourglass over the picture while the one asked for is on its way: a
// frame whose preview is not kept is read from the photo itself, which takes
// a while on a network drive, and the picture before it stays until then
// (not for a picture that comes at once: the hourglass would only flicker)
function waiting(on) {
  clearTimeout(state.waits);
  const mark = (there) => {
    $("viewerWait").hidden = !there;
    $("viewerImg").classList.toggle("stale", there);
  };
  if (on) state.waits = setTimeout(() => mark(true), 150);
  else mark(false);
}

function show(name) {
  state.shown = name;       // the frame on screen: the one the file buttons act on
  const url = image("preview", name);
  if (new URL(url, location.href).href !== $("viewerImg").src) waiting(true);
  $("viewerImg").src = url;
  loadFaces();
  for (const cell of $("viewerStrip").children) cell.classList.toggle("on", cell.dataset.name === name);
  showNotes();
}

// the notes of the frame on screen. The frames of a burst of bird photos are
// noted one by one (one of them is then its best); in a sequence noted
// before that, or of a single frame, the notes are those of the sequence.
function noteBadges(photo) {
  const best = state.frames.find((frame) => frame.best);
  const frame = (best && state.frames.find((item) => item.name === state.shown)) || photo;
  // a note out of five: as many dots lit
  const out = NOTES.filter(([key]) => frame[key]).map(([key, label]) =>
    el("span", { class: "rating" + (frame[key] >= 4 ? " good" : frame[key] <= 2 ? " warn" : ""), title: `${label} : ${frame[key]} sur 5` },
      label, el("span", { class: "dots" }, [1, 2, 3, 4, 5].map((n) => el("i", { class: n <= frame[key] ? "on" : "" })))));
  if (best && frame.best) out.push(badge("★ meilleure vue de la rafale", "good"));
  else if (best) {
    out.push(el("span", { class: "badge link", text: "→ meilleure vue", title: "Afficher la meilleure vue de la rafale",
                          onclick: () => show(best.name) }));
  }
  return out;
}

function showNotes() {
  const box = $("frameNotes"), photo = state.items[state.current];
  if (box && photo) box.replaceChildren(...noteBadges(photo));
  placeViewerLoupe();
}

// the strip of the frames of the sequence on screen, each with its notes
async function loadStrip(photo, index) {
  if (photo.seq_n <= 1) return;
  const frames = await (await fetch("/api/sequence?seq=" + encodeURIComponent(photo.seq))).json();
  if (state.current !== index || !Array.isArray(frames)) return;
  versioned(frames);
  state.frames = frames;
  const rated = frames.some((frame) => frame.best);
  $("viewerStrip").replaceChildren(...frames.map((frame) => {
    const img = el("img", { src: image("thumb", frame.name), loading: "lazy", alt: frame.name });
    const said = NOTES.filter(([key]) => frame[key]).map(([key, label]) => `${label} ${frame[key]}/5`).join(" · ");
    const cell = el("div", {
      class: "frame" + (frame.best ? " best" : ""),
      title: [frame.name, rated && said, frame.best && "Meilleure vue de la rafale",
              rated && frame.sharp && `Netteté mesurée sur l'œil : ${Math.round(100 * frame.sharp)} % de la vue la plus nette`]
        .filter(Boolean).join("\n"),
      onclick: () => show(frame.name),
    }, img, rated && el("span", { class: "notes", text: (frame.best ? "★ " : "")
      + NOTES.map(([key]) => frame[key] || "·").join(" ") }));
    cell.dataset.name = frame.name;
    cell.classList.toggle("on", frame.name === state.shown);
    img.addEventListener("error", () => cell.remove());
    // (the eye of each frame under its magnifier: the frames of a burst compared without opening them)
    if (hasEye(frame)) cell.append(loupeIcon((event) => loupe(event, frame)));
    return cell;
  }));
  showNotes();
}

async function openViewer(index) {
  const photo = state.items[index];
  if (!photo) return;
  state.current = index;
  history.replaceState(null, "", "?" + query() + "#" + encodeURIComponent(photo.name));
  $("viewer").hidden = false;
  document.body.style.overflow = "hidden";
  $("viewerStrip").replaceChildren();
  state.frames = [];
  $("viewerImg").alt = photo.title || photo.name;
  show(photo.name);
  $("viewerInfo").replaceChildren(...info(photo));
  $("viewerInfo").scrollTop = 0;
  showPeople();
  await loadStrip(photo, index);
}

function closeViewer() {
  hideLoupe();
  waiting(false);
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
  state.names = reply.names || reply.people.map(([name]) => name);
  $("personList").replaceChildren(...reply.people.map(([name]) => el("option", { value: name })));
  const index = state.current;
  if (index >= 0 && reply.photo && state.items[index].seq === reply.photo.seq) Object.assign(state.items[index], reply.photo);
  refresh(index);
  drawFaces();
}

// the rows of the faces in the panel: a number, a name to choose (those the
// photographer starts with, those given so far, or a new one to type), the
// year of birth
function showPeople() {
  const box = $("peopleRows");
  if (!box) return;
  box.replaceChildren(state.faceNote && el("p", { class: "mono", text: state.faceNote }),
    ...(state.faceList || []).map((face, k) => {
      const names = [...new Set([...(state.names || []), face.person].filter(Boolean))];
      const who = el("select", { class: "field", "aria-label": "Nom du visage " + (k + 1) },
        el("option", { value: "", text: "qui est-ce ?" }),
        names.map((name) => el("option", { value: name, text: name })),
        el("option", { value: NEW, text: "+ ajouter un nom…" }));
      who.value = face.person || "";
      const typed = el("input", { class: "field", placeholder: "nouveau nom", hidden: "hidden",
                                  "aria-label": "Nouveau nom pour le visage " + (k + 1) });
      const born = el("input", { class: "field born", inputmode: "numeric", maxlength: "4", value: face.born || "",
                                 placeholder: "né en", title: "Année de naissance : la reconnaissance en tient compte (un enfant change vite, et personne n'est cherché avant sa naissance)" });
      const chosen = () => (chosenNew() ? typed.value.trim() : who.value === NEW ? "" : who.value);
      const give = () => { if (chosen()) nameFace(face, chosen(), born.value); };
      // "ajouter un nom": a field to type it takes the place of the list
      // (Escape, or leaving it empty, brings the list back)
      const list = () => { typed.hidden = true; who.hidden = false; who.value = face.person || ""; };
      who.addEventListener("change", () => {
        if (who.value !== NEW) return give();        // a name of the list is given at once
        who.hidden = true;
        typed.hidden = false;
        typed.focus();
      });
      typed.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        event.stopPropagation();
        typed.value = "";
        list();
      });
      typed.addEventListener("blur", () => { if (!typed.value.trim() && who.hidden) list(); });
      const chosenNew = () => !typed.hidden;
      for (const input of [typed, born]) input.addEventListener("keydown", (event) => { if (event.key === "Enter") give(); });
      return el("div", { class: "who" }, el("span", { class: "n", text: String(k + 1) }), who, typed, born,
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
    photo.doing = busy ? what : "";
    if (state.current === index) $("viewerInfo").replaceChildren(...info(photo));
  };
  const update = (fresh) => {
    // (the best frame of a burst now stands for it: it is the one to show)
    const moved = fresh.name !== photo.name;
    Object.assign(photo, fresh);
    refresh(index);
    if (state.current === index) {
      show(moved ? photo.name : state.shown || photo.name);          // its preview may be new
      loadStrip(photo, index);
    }
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
      if (job.state === "running" && job.frames) {
        say(job.measured < job.frames
          ? `Lecture des vues de la rafale, pour noter chacune : ${job.measured} sur ${job.frames}…`
          : job.stage === "yeux" ? "Claude regarde l'œil de chaque vue de près…" : "Claude regarde la séquence…", true);
      } else if (job.state === "writing") {
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

// the identification set right by the photographer: for each species of the
// sequence, another one of its eBird checklist, any other species, or a bird
// that cannot be named; "Confirmer" when Claude was right but not sure
function speciesRows(photo) {
  const box = el("div", { class: "fix" });
  const index = state.items.indexOf(photo), species = split(photo.species);
  const send = async (old, name) => {
    const reply = await post("/api/species", { name: photo.name, old, new: name });
    if (reply.error || !reply.photo) {
      box.append(el("p", { class: "mono", text: reply.error === "refusé"
        ? "Ce serveur date d'avant cette fonction : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
        : reply.error || "La correction a échoué." }));
      return;
    }
    Object.assign(photo, reply.photo);
    refresh(index);
    batchButton();
  };
  const fill = (listed) => box.replaceChildren(
    el("p", { class: "mono", text: photo.verified
      ? "Identification vérifiée par vous" + (photo.verified !== photo.species
          ? ` (proposée d'abord : ${photo.verified}). « Identifier de nouveau avec Claude » refait la description pour cette espèce` : "")
        + ". Pour la changer :"
      : species.length ? "Si l'identification est fausse, la corriger :" : "Nommer l'oiseau :" }),
    ...(species.length ? species : [""]).map((name) => {
      const others = listed.filter((other) => other !== name);
      const typed = el("input", { class: "field", list: "speciesList", placeholder: "nom français de l'espèce", hidden: "hidden",
                                  "aria-label": "Autre espèce" });
      const choice = el("select", { class: "field", "aria-label": "Corriger l'espèce " + name },
        el("option", { value: name, text: name || "quelle espèce ?" }),
        others.length && el("optgroup", { label: "Sur la liste eBird de la séquence" },
          others.map((other) => el("option", { value: other, text: other }))),
        (name || photo.title) && el("option", { value: UNKNOWN, text: "Indéterminé" }),
        el("option", { value: NEW, text: "+ autre espèce…" }));
      const back = () => { typed.hidden = true; choice.hidden = false; choice.value = name; };
      choice.addEventListener("change", () => {
        if (choice.value === UNKNOWN) return send(name, "");
        if (choice.value !== NEW) return send(name, choice.value);
        choice.hidden = true;
        typed.hidden = false;
        typed.focus();
      });
      typed.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && typed.value.trim()) send(name, typed.value.trim());
        if (event.key !== "Escape") return;
        event.stopPropagation();
        typed.value = "";
        back();
      });
      typed.addEventListener("blur", () => { if (!typed.value.trim() && choice.hidden) back(); });
      return el("div", { class: "who" }, choice, typed,
        name && !photo.verified && el("button", { class: "pill", type: "button", text: "Confirmer",
          title: "L'identification est juste : la marquer comme vérifiée par vous", onclick: () => send(name, name) }));
    }));
  fill([]);
  if (photo.checklist) {
    fetch("/api/checklist?sid=" + encodeURIComponent(photo.checklist)).then((reply) => reply.json())
      .then((list) => fill((list.species || []).map((sp) => sp.francais))).catch(() => {});
  }
  return box;
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

// turn the photo on screen a quarter turn (or every frame of its sequence):
// the server rewrites the Orientation of the file, then makes its pictures again
async function rotate(photo, way, whole, said) {
  const index = state.items.indexOf(photo), name = state.shown || photo.name;
  said.textContent = whole ? "Rotation de toutes les vues de la séquence…" : "Rotation…";
  waiting(true);
  let reply;
  try {
    reply = await post("/api/rotate", { name, way, all: whole });
  } catch (err) {
    reply = { error: String(err) };
  }
  if (reply.error || !reply.photo) {
    waiting(false);
    said.textContent = reply.error === "refusé"
      ? "Ce serveur date d'avant cette fonction : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
      : reply.error || "La rotation a échoué.";
    return;
  }
  versioned([reply.photo, ...reply.frames]);
  Object.assign(photo, reply.photo);
  refresh(index);
  if (state.current !== index) return waiting(false);
  await loadStrip(photo, index);
  show(name);
}

// the headings of the panel of a photo, each with a small drawing
const ICONS = {
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.5" cy="9" r="2.4"/><path d="M17 14.3c2.3.5 4 2.6 4 5.2"/>',
  bird: '<path d="M20.2 12.2a6 6 0 0 0-8.5-8.5L5 10.5V19h8.5z"/><path d="M16 8 2 22M17.5 15H9"/>',
  tag: '<path d="M3 11.5V4h7.5L21 14.5 14.5 21 3 11.5z"/><circle cx="7.6" cy="8.4" r="1.3"/>',
  pin: '<path d="M12 21s7-6.2 7-11.5a7 7 0 0 0-14 0C5 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
};

function heading(drawing, text) {
  const icon = el("span", { class: "icon" });
  // (constants of this page, nothing from the index)
  icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[drawing]}</svg>`;
  return el("h3", {}, icon, text);
}

// a small map of where the photo was taken, in its panel. The panel is drawn
// again as a job goes on: the map is kept as long as the place is the same.
const mini = { key: "", node: null, map: null };

function miniMap(photo) {
  if (!window.L || photo.lat === null || photo.lat === undefined || photo.lon === null || photo.lon === undefined) return null;
  const key = `${photo.lat},${photo.lon}`;
  if (mini.key !== key) {
    if (mini.map) mini.map.remove();
    Object.assign(mini, { key, map: null, node: el("div", { class: "mini-map" }) });
  }
  const node = mini.node, at = [photo.lat, photo.lon];
  node.title = photo.location ? "Toutes les photos prises ici : " + photo.location : "";
  node.onclick = () => { if (photo.location) setFilter({ place: photo.location }); };
  requestAnimationFrame(() => {
    if (!node.isConnected) return;
    if (mini.map) return mini.map.invalidateSize();
    mini.map = L.map(node, { zoomControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
                             boxZoom: false, keyboard: false, touchZoom: false }).setView(at, 10);
    mini.map.attributionControl.setPrefix(false);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap" }).addTo(mini.map);
    L.circleMarker(at, { radius: 7, color: "#e8eef8", weight: 2, fillColor: "#62c2ff", fillOpacity: 0.95, interactive: false }).addTo(mini.map);
  });
  return node;
}

function info(photo) {
  // a title that is a species: its French name, and its Latin name under it
  const named = /^([^()]+) \(([^()]+)\)$/.exec(photo.title || "");
  const out = named && split(photo.latin).includes(named[2])
    ? [el("h2", { text: named[1] }), el("p", { class: "latin", text: named[2] })]
    : [el("h2", { text: photo.title || "Pas encore décrite" })];
  const badges = [];
  if (photo.verified) {
    badges.push(badge("identification vérifiée par vous", "good"));
  } else if (photo.species && photo.certainty) {
    badges.push(badge("identification : " + photo.certainty, photo.certainty === "sûre" ? "good" : "warn"));
  }
  if (photo.on_checklist === 1) badges.push(badge("sur la liste eBird", "good"));
  if (photo.on_checklist === 0) badges.push(badge("absente de la liste eBird", "alert"));
  if (photo.topic) badges.push(badge(photo.topic));
  if (photo.seq_n > 1) badges.push(badge(photo.seq_n + " vues"));
  if (badges.length) out.push(el("div", { class: "badges" }, badges));
  out.push(el("div", { class: "badges", id: "frameNotes" }, noteBadges(photo)));
  if (photo.reachable !== false) {
    // two arrows that turn the photo on screen, and write how it is to be seen into its file
    const said = el("span", { class: "mono" });
    const arrow = (way, sign, where) => el("button", {
      class: "pill turn", type: "button", text: `${sign} ${where}`,
      title: `Tourner la vue affichée d'un quart de tour ${where} : l'orientation est écrite dans l'en-tête du fichier `
        + "(le mot-clé EXIF Orientation) ; l'image elle-même n'est pas réécrite. "
        + (photo.seq_n > 1 ? "Maj + clic : toutes les vues de la séquence." : ""),
      onclick: (event) => rotate(photo, way, event.shiftKey && photo.seq_n > 1, said) });
    out.push(el("div", { class: "actions turns" }, arrow("ccw", "↺", "vers la gauche"), arrow("cw", "↻", "vers la droite"), said));
  }
  if (photo.description) out.push(el("p", { class: "lede", text: photo.description }));
  if (photo.note) out.push(el("p", { class: "note", text: "Par rapport à la liste : " + photo.note }));
  if (!photo.described) {
    out.push(el("p", { class: "mono", text: "Claude n'a pas décrit cette séquence. Ce qui est affiché vient de "
      + "son nom et de la liste eBird pendant laquelle elle a été prise." }));
  }
  if (photo.reachable && state.claude) {
    // whether the files hold what the index says is said on the button itself:
    // written (when), waiting, or not yet
    const waits = photo.written === "pending", wrote = Boolean(photo.written) && !waits;
    const when = /^\d{4}-\d\d-\d\d \d\d:\d\d/.test(photo.written || "")
      ? ` le ${photo.written.slice(0, 10)} à ${photo.written.slice(11, 16)}` : "";
    const writable = photo.described || photo.checklist;
    out.push(el("div", { class: "block" }, el("div", { class: "actions" },
      el("button", { class: "pill go", type: "button", disabled: photo.busy && "disabled",
                     text: photo.doing === "describe" ? "Identification en cours…"
                       : photo.described ? "Identifier de nouveau avec Claude" : "Identifier la séquence avec Claude",
                     title: "Un appel à Claude, environ 20 s (pour une rafale d'oiseau, chaque vue est d'abord lue et notée) ; la réponse est écrite dans la base et dans l'en-tête des fichiers de la séquence",
                     onclick: () => work(photo, "describe") }),
      writable && el("button", { class: "pill " + (wrote && !photo.doing ? "done" : "go"), type: "button", disabled: photo.busy && "disabled",
                     text: photo.doing === "write" ? "Écriture en cours…"
                       : wrote ? "✓ Déjà écrit dans les fichiers"
                       : waits ? "Écrire dans les fichiers (en attente)" : "Écrire dans les fichiers",
                     title: (wrote ? `Les fichiers de la séquence portent déjà ce que la base en sait${when ? " : écrit" + when : ""}. Un clic l'écrit de nouveau. `
                       : waits ? "Les fichiers attendent encore ce que dit la base : l'écriture reprendra toute seule, un clic la fait tout de suite. "
                       : "Ce que dit la base n'a pas encore été écrit dans ces fichiers. ")
                       + "Ce qui s'écrit dans leur en-tête : description, lieu, latitude et longitude de la liste eBird (sans appeler Claude).",
                     onclick: () => work(photo, "write") })),
      el("p", { class: "mono", text: photo.job || (waits
        ? "Les fichiers attendent encore ce que dit la base ; l'écriture reprendra toute seule."
        : wrote ? (when ? `Écrit dans les fichiers${when}.` : "Les fichiers de la séquence portent déjà cette description.")
        : writable ? "Pas encore écrit dans les fichiers." : "") })));
  } else if (photo.reachable === false) {
    out.push(el("p", { class: "mono", text: "Pour demander à Claude d'identifier la séquence ou écrire dans les fichiers, montez le disque d'archive." }));
  }

  // the people: names given by the photographer, names proposed, and the faces to name
  const people = split(photo.people), guessed = split(photo.guessed);
  if (state.facesCan || people.length || guessed.length) {
    out.push(heading("people", "Personnes"));
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
  if (species.length || photo.bird || photo.topic === "oiseau") {
    out.push(heading("bird", "Espèces"));
    if (species.length) {
      out.push(el("div", { class: "chips" }, species.map((name, k) =>
        el("span", { class: "chip", text: latin[k] ? `${name} (${latin[k]})` : name, title: "Toutes les photos de cette espèce",
                     onclick: () => setFilter({ species: name }) }))));
    }
    out.push(speciesRows(photo));
  }
  const words = [...split(photo.habitat), ...split(photo.keywords)];
  if (words.length) {
    out.push(heading("tag", "Mots-clés"), el("div", { class: "chips" }, words.map((word) =>
      el("span", { class: "chip", text: word, onclick: () => setFilter({ q: word }) }))));
  }

  if (photo.location || photo.checklist) {
    out.push(heading("pin", "Où"));
    if (photo.location) {
      out.push(el("div", { class: "chips" }, el("span", { class: "chip", text: photo.location,
        title: "Toutes les photos prises ici", onclick: () => setFilter({ place: photo.location }) })));
    }
    if (region(photo)) out.push(el("p", { text: region(photo) }));
    const map = miniMap(photo);
    if (map) out.push(map);
    if (photo.own_position || photo.match === "gps") {
      out.push(el("p", { class: "mono", text: "Lieu nommé d'après la position que porte la photo." }));
    }
    if (photo.checklist) {
      const how = { inside: "photo prise pendant la liste", near: "photo prise à moins d'une heure de la liste",
                    other: "liste choisie par Claude parmi celles du jour" }[photo.match] || "";
      const box = el("div", {});
      // the checklist on eBird: its address, written out, is the link
      const url = "https://ebird.org/checklist/" + encodeURIComponent(photo.checklist);
      out.push(
        el("p", {}, `Liste eBird ${photo.checklist}` + (how ? ` (${how})` : "")),
        el("p", { class: "mono" }, el("a", { class: "out", href: url, target: "_blank", rel: "noopener", text: url + " ↗",
                                             title: "Ouvrir cette liste sur eBird, dans un nouvel onglet" })),
        el("div", { class: "actions" },
          el("button", { class: "pill", type: "button", text: "Ses espèces", onclick: () => listSpecies(photo, box) }),
          el("button", { class: "pill", type: "button", text: "Toutes ses photos",
                         onclick: () => setFilter({ checklist: photo.checklist }) })),
        box);
    }
  }

  // when and with what: a line for each
  const offset = Number(photo.clock_offset || 0);
  const facts = [
    ["Date", (photo.local || photo.taken || "inconnue").slice(0, 16)],
    offset && ["Horloge", `${(photo.taken || "").slice(11, 16)} à l'appareil, décalée de ${offset > 0 ? "+" : ""}${offset} h`],
    photo.camera && ["Appareil", photo.camera],
    photo.lens && ["Objectif", photo.lens],
    photo.focal && ["Focale", Math.round(photo.focal) + " mm"],
  ].filter(Boolean);
  out.push(heading("camera", "Prise de vue"),
    el("dl", { class: "facts" }, facts.flatMap(([label, value]) => [el("dt", { text: label }), el("dd", { text: value })])));

  const said = el("p", { class: "mono" });
  out.push(heading("file", "Fichier"), el("p", { class: "mono", text: photo.path || photo.name }));
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
  // what the index holds, as tiles next to the title: a count, and a bar for a share
  const thumbs = facets.thumbs && facets.thumbs.total ? facets.thumbs : null;
  const stat = (label, value, part, whole, title) => el("div", { class: "stat", title },
    el("div", { class: "label", text: label }), el("div", { class: "value", text: value }),
    whole ? el("div", { class: "meter" }, el("div", { style: `width:${Math.min(100, 100 * part / whole).toFixed(1)}%` })) : null);
  const share = (part, whole) => `${number(part)} · ${Math.floor(100 * part / whole)} %`;
  $("heroStats").replaceChildren(...[
    stat("Photos", number(facets.total), 0, 0, "Les photos de la base : l'archive et celles qui attendent"),
    stat("Décrites", share(facets.described, facets.total), facets.described, facets.total, "Les photos que Claude a décrites"),
    thumbs && stat("Vignettes", share(thumbs.have, thumbs.total), thumbs.have, thumbs.total,
      "Les séquences dont la vignette est gardée sur cette machine" + (thumbs.filling ? " ; les autres se font en arrière-plan" : "")),
    stat("Données eBird", facets.ebird ? facets.ebird.slice(0, 10) : "aucune", 0, 0, "La date de l'export eBird dont viennent les listes"),
  ].filter(Boolean));
  // (a server started before an update of patchdoc says less: relaunch it)
  state.claude = facets.claude;
  state.rawApp = facets.raw_app;
  state.facesCan = facets.faces;
  state.names = facets.names || (facets.people || []).map(([name]) => name);
  state.panoCan = facets.pano;
  for (const [name] of facets.people || []) $("personList").append(el("option", { value: name }));
  for (const ended of ["load", "error"]) $("viewerImg").addEventListener(ended, () => waiting(false));
  $("viewerImg").addEventListener("load", drawFaces);
  $("viewerImg").addEventListener("load", placeViewerLoupe);
  window.addEventListener("resize", placeViewerLoupe);
  $("viewerLoupe").replaceWith(Object.assign(loupeIcon(overViewer, "big"), { id: "viewerLoupe", hidden: true }));
  $("loupe").firstElementChild.addEventListener("error", hideLoupe);
  window.addEventListener("resize", drawFaces);
  $("drive").classList.toggle("ok", Boolean(facets.archive && facets.archive.mounted));
  $("drive").textContent = !facets.archive
    ? "Ce serveur date d'avant une mise à jour de patchdoc : arrêtez-le (Ctrl-C) et relancez `patchdoc gui`."
    : (facets.archive.mounted
      ? `Disque d'archive monté : ${facets.archive.mounted}`
      : "Disque d'archive non monté. La recherche et les vignettes déjà faites restent disponibles ; "
        + "ouvrir un fichier, faire une vignette ou appeler Claude demande le disque.")
    // an archive on a server: exiftool is run there when it has one, which spares the trips of the files
    + (facets.remote || []).filter((server) => server.asked).map((server) => server.tool
      ? ` · exiftool tourne sur le serveur de l'archive (${server.host})`
      : ` · le serveur de l'archive (${server.host}) n'a pas exiftool : les fichiers sont lus et récrits à travers le réseau, `
        + "bien plus lentement (sur le serveur : sudo apt install libimage-exiftool-perl)").join("");

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
  setSort($("sort").value || "new", true);
  for (const tab of $("sorts").querySelectorAll(".tab")) tab.addEventListener("click", () => setSort(tab.dataset.sort));
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
    setSort("new", true);
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
  // the server is stopped from the page: started from the Dock, it has no terminal
  $("quit").addEventListener("click", async (event) => {
    event.preventDefault();
    const link = event.currentTarget;
    if (Date.now() - (link.armed || 0) > 8000) {
      link.armed = Date.now();
      link.textContent = "Confirmer : arrêter patchdoc (ce que Claude regarde en ce moment est abandonné)";
      return;
    }
    const reply = await post("/api/quit").catch(() => ({}));
    link.replaceWith(reply.ok ? "patchdoc est arrêté : l'icône du Dock, ou `patchdoc gui`, le relance."
      : "Ce serveur date d'avant cette fonction : arrêtez-le avec Ctrl-C dans son terminal.");
  });
  $("batchGo").addEventListener("click", batchGo);
  $("batchAll").addEventListener("click", batchAll);
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
  showArchive();
  showInbox();
  showBackup();
}

start().catch((err) => { $("status").textContent = "La page n'a pas pu démarrer : " + err.message; });
