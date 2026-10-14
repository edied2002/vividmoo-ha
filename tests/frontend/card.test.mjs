// Run: node --test tests/frontend/card.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC_PATH = new URL("../../custom_components/vividmoo/frontend/vividmoo-card.js", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");
await import(SRC_PATH.href); // module-style load, like the modern frontend's import()
const { resolveEntities, viewModel, renderCard, escapeHtml, targetOf, VividmooCard } = globalThis.__vividmooCard;

const DEV = "dev1";
function makeHass(states = {}, extra = {}) {
  const ids = {
    work_mode: "sensor.cato_work_mode", work_state: "sensor.cato_work_state", waste_bin_level: "sensor.cato_bin",
    use_count: "sensor.cato_uses", last_use: "sensor.cato_last_use", last_cat: "sensor.cato_last_cat",
    last_weight: "sensor.cato_last_weight", last_duration: "sensor.cato_duration", cat_weight: "sensor.cato_cat_weight",
    visits_today: "sensor.cato_visits_today", visits_7d_avg: "sensor.cato_visits_avg", online: "binary_sensor.cato_online",
    waste_bin_full: "binary_sensor.cato_bin_full", clean: "button.cato_clean", smooth: "button.cato_level",
    empty: "button.cato_empty", pause: "button.cato_pause", resume: "button.cato_resume", waste_bin_emptied: "button.cato_emptied",
    cat_inside: "binary_sensor.cato_cat_inside",
  };
  const entities = Object.fromEntries(Object.entries(ids).map(([tk, id]) => [id, { entity_id: id, device_id: DEV, platform: "vividmoo", translation_key: tk }]));
  entities["sensor.other"] = { entity_id: "sensor.other", device_id: DEV, platform: "other", translation_key: "work_mode" };
  entities["sensor.box2_mode"] = { entity_id: "sensor.box2_mode", device_id: "dev2", platform: "vividmoo", translation_key: "work_mode" };
  const base = {
    "sensor.cato_work_mode": "idle", "sensor.cato_work_state": "idle", "sensor.cato_bin": "60", "sensor.cato_uses": "30",
    "sensor.cato_last_use": "2026-10-11T15:31:58+00:00", "sensor.cato_last_cat": "Mochi", "sensor.cato_last_weight": "5.9",
    "sensor.cato_duration": "36", "sensor.cato_cat_weight": "5.9", "sensor.cato_visits_today": "5", "sensor.cato_visits_avg": "4.6",
    "binary_sensor.cato_online": "on", "binary_sensor.cato_bin_full": "off", "binary_sensor.cato_cat_inside": "off", "button.cato_clean": "unknown",
    "button.cato_level": "unknown", "button.cato_pause": "unknown", "button.cato_resume": "unknown", "button.cato_emptied": "unknown",
  };
  const hassStates = {};
  for (const [id, state] of Object.entries({ ...base, ...states })) {
    if (state === null) continue; // disabled entity: no state object
    hassStates[id] = { entity_id: id, state, attributes: id.includes("weight") ? { unit_of_measurement: "kg" } : {}, last_updated: "x" };
  }
  return { entities, states: hassStates, devices: { [DEV]: { name: "Bathroom box" } }, locale: { language: "en" }, ...extra };
}
const vmFor = (states, now) => { const h = makeHass(states); return viewModel(h, resolveEntities(h, DEV), now); };

// -- compatibility with the legacy (es5) frontend build ---------------------------------

test("parses as a classic <script> (legacy frontend build loads it with _ls())", () => {
  assert.doesNotThrow(() => new vm.Script(SRC, { filename: "vividmoo-card.js" }));
});

test("no module-only or Chrome<80 syntax", () => {
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "").replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""');
  assert.doesNotMatch(code, /^\s*(export|import)\s/m);
  assert.doesNotMatch(code, /\?\./, "optional chaining");
  assert.doesNotMatch(code, /\?\?/, "nullish coalescing");
  assert.doesNotMatch(code, /`/, "template literals are fine in Chrome 41+, but keep the file uniform");
});

test("runs as a classic script in a fresh context and registers the card once", () => {
  const defined = {};
  const ctx = {
    console: { info() {} }, HTMLElement: class {}, Intl, Date, Math, Object, String, Number, Boolean, Promise, isNaN,
    CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } },
  };
  ctx.globalThis = ctx; ctx.window = ctx;
  vm.createContext(ctx);
  new vm.Script(SRC).runInContext(ctx);
  new vm.Script(SRC).runInContext(ctx); // loaded via both import() and es5 <script>: no double define
  assert.ok(defined["vividmoo-card"] && defined["vividmoo-card-editor"]);
  assert.equal(ctx.customCards.length, 1);
});

// -- view model and rendering ---------------------------------------------------------------

test("resolves only this device's vividmoo entities, by translation key and domain", () => {
  const ents = resolveEntities(makeHass(), DEV);
  assert.equal(ents.work_mode, "sensor.cato_work_mode");
  assert.equal(Object.keys(ents).length, 20);
  assert.ok(!Object.values(ents).includes("sensor.other") && !Object.values(ents).includes("sensor.box2_mode"));
});

test("running clean: emphasis, Pause offered, Clean/Level disabled", () => {
  const v = vmFor({ "sensor.cato_work_mode": "clean", "sensor.cato_work_state": "running" });
  assert.deepEqual([v.status.kind, v.status.label, v.status.anim], ["running", "Cleaning", "spin"]);
  const html = renderCard(v);
  assert.match(html, /class="header tap running"/);
  assert.match(html, /data-action="clean" disabled/);
  assert.match(html, /data-action="pause"/);
  assert.doesNotMatch(html, /data-action="resume"/);
});

test("paused smooth offers Resume; idle Ready; unknown hints; offline wins", () => {
  let v = vmFor({ "sensor.cato_work_mode": "smooth", "sensor.cato_work_state": "paused" });
  assert.equal(v.status.label, "Leveling litter paused"); assert.ok(v.actions.resume && !v.actions.pause);
  assert.equal(vmFor().status.label, "Ready");
  assert.match(vmFor({ "sensor.cato_work_mode": "unknown", "sensor.cato_work_state": "unknown" }).status.hint, /live updates/);
  v = vmFor({ "binary_sensor.cato_online": "off", "sensor.cato_work_mode": "clean", "sensor.cato_work_state": "running" });
  assert.equal(v.status.kind, "offline"); assert.match(renderCard(v), /data-action="clean" disabled/);
});

test("Empty hidden while disabled, shown (danger) once enabled", () => {
  assert.doesNotMatch(renderCard(vmFor()), /data-action="empty"/);
  assert.match(renderCard(vmFor({ "button.cato_empty": "unknown" })), /class="action danger" data-action="empty"/);
});

test("gauge, chips, stats, last seen; cloud strings escaped", () => {
  const v = vmFor({}, Date.parse("2026-10-11T17:31:58+00:00"));
  assert.equal(v.name, "Bathroom box"); assert.equal(v.last.seen, "2 hours ago");
  const html = renderCard(v);
  assert.match(html, />60%</); assert.match(html, /30 uses/); assert.match(html, /Mochi · 5\.9 kg · 36 s/);
  assert.equal(escapeHtml(`<img onerror="x">`), "&lt;img onerror=&quot;x&quot;&gt;");
  assert.doesNotMatch(renderCard(vmFor({ "sensor.cato_last_cat": "<script>alert(1)</script>" })), /<script>/);
});

// -- tap to open more-info -------------------------------------------------------------------

test("every value is tappable and points at the matching entity", () => {
  const html = renderCard(vmFor());
  const expected = {
    header: "sensor.cato_work_state", bin: "sensor.cato_bin", "chip-last": "sensor.cato_last_weight",
    "chip-seen": "sensor.cato_last_use", today: "sensor.cato_visits_today", avg: "sensor.cato_visits_avg", catWeight: "sensor.cato_cat_weight",
  };
  for (const id of Object.values(expected)) {
    assert.match(html, new RegExp(`data-entity="${id.replace(".", "\\.")}" role="button" tabindex="0"`), id);
  }
  assert.equal((html.match(/data-entity=/g) || []).length, 7);
  assert.equal((html.match(/class="action[^"]*" data-action="[^"]*"[^>]*data-entity/g) || []).length, 0, "buttons stay buttons");
});

function fakeCard() {
  const card = new VividmooCard();
  card.events = [];
  card.dispatchEvent = (e) => card.events.push(e);
  card._ents = { clean: "button.cato_clean" };
  card._hass = { calls: [], callService(domain, service, data) { this.calls.push([domain, service, data]); return Promise.resolve(); } };
  return card;
}
const el = (dataset, extra = {}) => ({ dataset, classList: { add() {}, remove() {} }, ...extra });

test("tap and Enter/Space on a value fire hass-more-info with entityId", () => {
  const card = fakeCard();
  let prevented = false;
  card._activate([{}, el({ entity: "sensor.cato_bin" }), el({})], { preventDefault() { prevented = true; } });
  card._activate([el({ entity: "sensor.cato_visits_today" })], { preventDefault() { prevented = true; } }, true);
  assert.deepEqual(card.events.map((e) => [e.type, e.detail.entityId, e.bubbles, e.composed]),
    [["hass-more-info", "sensor.cato_bin", true, true], ["hass-more-info", "sensor.cato_visits_today", true, true]]);
  assert.equal(prevented, true); // Space must not scroll the page
});

test("buttons still press the button entity; disabled buttons and keyboard on buttons do nothing extra", async () => {
  const card = fakeCard();
  card._activate([el({ action: "clean" }, { disabled: true })], {});
  card._activate([el({ action: "clean" })], {}, true); // native <button> already turns Enter into a click
  card._activate([el({ action: "clean" })], {});
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(card._hass.calls, [["button", "press", { entity_id: "button.cato_clean" }]]);
  assert.equal(card.events.length, 0);
  assert.equal(targetOf([el({ action: "clean" }), el({ entity: "sensor.x" })]).action, "clean"); // innermost wins
});

// -- drum badge and real job timing ------------------------------------------------------

const { CHOREOGRAPHY, choreographyCss } = globalThis.__vividmooCard;
const NOW = Date.parse("2026-10-11T16:33:30+00:00");
const jobAttrs = (extra) => ({ job_timing_known: true, job_expected_seconds: 90, job_run_seconds: 6,
  job_running_since: "2026-10-11T16:33:20+00:00", ...extra });
function vmWithJob(mode, state, attrs) {
  const h = makeHass({ "sensor.cato_work_mode": mode, "sensor.cato_work_state": state });
  h.states["sensor.cato_work_state"].attributes = attrs;
  return viewModel(h, resolveEntities(h, DEV), NOW);
}

test("idle shows the cat icon, an active job shows the drum", () => {
  assert.match(renderCard(vmFor()), /icon="mdi:cat"/);
  const html = renderCard(vmWithJob("clean", "running", jobAttrs()));
  assert.match(html, /class="badge drum-badge"/); assert.match(html, /<svg class="drum"/);
  assert.doesNotMatch(html, /icon="mdi:broom"><\/ha-icon><\/div><div class="titles"/);
});

test("progress ring starts at the real elapsed time and runs for the remaining time", () => {
  const v = vmWithJob("clean", "running", jobAttrs());
  assert.equal(v.job.elapsed, 16); // 6 s before the last resume + 10 s since
  const html = renderCard(v);
  assert.match(html, /--from:82\.22;--dur:74\.0s;animation-play-state:running/);
});

test("paused freezes the ring; unknown timing shows an indeterminate ring", () => {
  const paused = renderCard(vmWithJob("clean", "paused", jobAttrs({ job_running_since: null })));
  assert.match(paused, /--from:93\.33;--dur:84\.0s;animation-play-state:paused/);
  const unknown = renderCard(vmWithJob("smooth", "running", { job_timing_known: false, job_expected_seconds: 50 }));
  assert.match(unknown, /class="ring indeterminate"/);
});

test("drum stays still until a real choreography is filled in; then it is synced to elapsed time", () => {
  assert.deepEqual(CHOREOGRAPHY, { clean: [], smooth: [], clear: [] }); // ships empty: no invented motion
  assert.doesNotMatch(renderCard(vmWithJob("clean", "running", jobAttrs())), /drum-clean/);
  assert.equal(choreographyCss("clean"), "");
  CHOREOGRAPHY.clean.push([0, 0], [0.5, -120], [1, 0]); // sample table, as it would come from a video
  try {
    assert.match(renderCard(vmWithJob("clean", "running", jobAttrs())), /animation: drum-clean 90s linear -16\.0s forwards; animation-play-state:running/);
    assert.equal(choreographyCss("clean"), "@keyframes drum-clean {0.00% { transform: rotate(0deg); } 50.00% { transform: rotate(-120deg); } 100.00% { transform: rotate(0deg); }}");
  } finally {
    CHOREOGRAPHY.clean.length = 0;
  }
});

test("cat inside: live status over Ready, taps open the presence sensor; unavailable falls back", () => {
  let v = vmFor({ "binary_sensor.cato_cat_inside": "on" });
  assert.deepEqual([v.status.kind, v.status.label, v.status.icon], ["cat", "Cat inside", "mdi:cat"]);
  assert.equal(v.info.status, "binary_sensor.cato_cat_inside"); assert.equal(v.busy, false);
  assert.match(renderCard(v), /class="header tap cat"/);
  // A job blocked by the cat says why; offline still wins.
  v = vmFor({ "binary_sensor.cato_cat_inside": "on", "sensor.cato_work_mode": "clean", "sensor.cato_work_state": "blocked" });
  assert.equal(v.status.label, "Cleaning: waiting, cat inside");
  assert.equal(vmFor({ "binary_sensor.cato_cat_inside": "on", "binary_sensor.cato_online": "off" }).status.kind, "offline");
  assert.equal(vmFor({ "binary_sensor.cato_cat_inside": "unavailable" }).status.label, "Ready");
  assert.equal(vmFor({ "binary_sensor.cato_cat_inside": null }).status.label, "Ready"); // older integration
});
