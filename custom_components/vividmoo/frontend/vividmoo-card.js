/*
 * vividmoo-card: Lovelace card for the Vividmoo litter box integration.
 * Served by the integration itself (no separate install). Configure with:
 *
 *   type: custom:vividmoo-card
 *   device_id: <Home Assistant device id of the litter box>
 *
 * Compatibility: this file must run both as an ES module (modern frontend build,
 * loaded with import()) and as a classic <script> (legacy "es5" build used by older
 * Android WebViews). So: no import/export, and no ?. or ?? (Chrome < 80).
 * Test hooks are exposed on globalThis.__vividmooCard instead of exports.
 */
(function () {
  "use strict";

  var CARD_VERSION = "0.5.0";

  // Drum choreography per work mode: [[fraction of the job's run time, drum angle in degrees], ...].
  // Intentionally EMPTY: no message carries drum position/direction, and the box streams nothing
  // mid-job, so angles must come from observing a real cycle (video). Until then the drum stays still
  // and only the progress ring (driven by real start/pause/finish + observed run times) moves.
  var CHOREOGRAPHY = { clean: [], smooth: [], clear: [] };

  // translation_key -> domain
  var KEYS = {
    work_mode: "sensor", work_state: "sensor", waste_bin_level: "sensor", use_count: "sensor",
    last_use: "sensor", last_cat: "sensor", last_weight: "sensor", last_duration: "sensor",
    cat_weight: "sensor", visits_today: "sensor", visits_7d_avg: "sensor",
    online: "binary_sensor", waste_bin_full: "binary_sensor", cat_inside: "binary_sensor",
    clean: "button", smooth: "button", pause: "button", resume: "button",
    waste_bin_emptied: "button",
  };

  var JOB_LOOK = {
    clean: { icon: "mdi:broom", label: "Cleaning", anim: "spin" },
    smooth: { icon: "mdi:waves", label: "Leveling litter", anim: "wave" },
    clear: { icon: "mdi:delete-empty", label: "Emptying litter", anim: "bounce" },
    reset: { icon: "mdi:restore", label: "Resetting", anim: "spin" },
  };

  // Small helpers instead of ?. and ??
  function get(obj, path) {
    for (var i = 0; i < path.length; i++) {
      if (obj === null || obj === undefined) return undefined;
      obj = obj[path[i]];
    }
    return obj;
  }
  function dflt(value, fallback) { return value === null || value === undefined ? fallback : value; }
  function values(obj) { return obj ? Object.keys(obj).map(function (k) { return obj[k]; }) : []; }

  function escapeHtml(value) {
    var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return String(dflt(value, "")).replace(/[&<>"']/g, function (c) { return map[c]; });
  }

  /** Map translation keys to entity ids for one device (only this integration's entities). */
  function resolveEntities(hass, deviceId) {
    var found = {};
    values(get(hass, ["entities"])).forEach(function (entry) {
      if (entry.platform !== "vividmoo" || entry.device_id !== deviceId) return;
      var domain = entry.entity_id.split(".")[0];
      if (entry.translation_key && KEYS[entry.translation_key] === domain) found[entry.translation_key] = entry.entity_id;
    });
    return found;
  }

  function usable(s) { return Boolean(s) && s.state !== "unknown" && s.state !== "unavailable"; }

  function relativeTime(iso, now, lang) {
    var then = Date.parse(iso);
    if (isNaN(then) || typeof Intl === "undefined" || !Intl.RelativeTimeFormat) return null;
    var sec = Math.round((then - now) / 1000);
    var units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
    var rtf = new Intl.RelativeTimeFormat(lang || "en", { numeric: "auto" });
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(sec) >= units[i][1]) return rtf.format(Math.round(sec / units[i][1]), units[i][0]);
    }
    return rtf.format(0, "minute");
  }

  /** Everything the template needs, derived from hass.states. Pure, so it is unit-testable. */
  function viewModel(hass, ents, now) {
    now = dflt(now, Date.now());
    var st = function (k) { return ents[k] ? hass.states[ents[k]] : undefined; };
    var num = function (k) { return usable(st(k)) ? Number(st(k).state) : null; };
    var unit = function (k) { return dflt(get(st(k), ["attributes", "unit_of_measurement"]), ""); };
    var exists = function (k) { return Boolean(ents[k] && hass.states[ents[k]]); }; // disabled entities have no state
    var mode = usable(st("work_mode")) ? st("work_mode").state : null;
    var jobState = usable(st("work_state")) ? st("work_state").state : null;
    var online = st("online") ? st("online").state === "on" : null;
    var look = JOB_LOOK[mode];
    // Pushed live by the box on entry/exit (1024 toilet_state); unavailable without live updates.
    var catInside = get(st("cat_inside"), ["state"]) === "on";

    var status;
    if (online === false) status = { kind: "offline", icon: "mdi:cloud-off-outline", label: "Offline", anim: "" };
    else if (mode && mode !== "idle" && jobState === "paused") status = { kind: "paused", icon: "mdi:pause-circle", label: (look ? look.label : mode) + " paused", anim: "pulse" };
    else if (mode && mode !== "idle" && jobState === "blocked") status = { kind: "blocked", icon: "mdi:alert-circle", label: (look ? look.label : mode) + (catInside ? ": waiting, cat inside" : ": blocked"), anim: "pulse" };
    else if (mode && mode !== "idle" && look) status = { kind: "running", icon: look.icon, label: look.label, anim: look.anim };
    else if (catInside) status = { kind: "cat", icon: "mdi:cat", label: "Cat inside", anim: "pulse" };
    else if (mode === "idle") status = { kind: "idle", icon: "mdi:cat", label: "Ready", anim: "" };
    else status = { kind: "unknown", icon: "mdi:cat", label: "Ready", hint: "Enable live updates for real-time status", anim: "" };

    var anyEntity = values(ents)[0];
    var device = get(hass, ["devices", get(hass, ["entities", anyEntity, "device_id"])]);
    var lastUse = st("last_use");
    var catWeight = num("cat_weight");
    return {
      name: (device && (device.name_by_user || device.name)) || "Litter box",
      status: status,
      busy: status.kind === "running" || status.kind === "paused" || status.kind === "blocked",
      online: online,
      bin: { level: num("waste_bin_level"), uses: num("use_count"), full: get(st("waste_bin_full"), ["state"]) === "on" },
      last: {
        cat: usable(st("last_cat")) ? st("last_cat").state : null,
        weight: num("last_weight"), weightUnit: unit("last_weight"),
        duration: num("last_duration"),
        seen: usable(lastUse) ? relativeTime(lastUse.state, now, get(hass, ["locale", "language"])) : null,
      },
      visits: { today: num("visits_today"), avg: num("visits_7d_avg") },
      catWeight: catWeight !== null ? { value: catWeight, unit: unit("cat_weight") } : null,
      job: jobTiming(st("work_state"), mode, now),
      // Entity behind each tappable value (opens HA's more-info dialog with its history).
      info: {
        status: (status.kind === "cat" && ents.cat_inside) || ents.work_state || ents.work_mode, bin: ents.waste_bin_level, last: ents.last_weight || ents.last_cat,
        seen: ents.last_use, today: ents.visits_today, avg: ents.visits_7d_avg, catWeight: ents.cat_weight,
      },
      actions: {
        clean: exists("clean"), smooth: exists("smooth"),
        pause: exists("pause") && status.kind === "running",
        resume: exists("resume") && status.kind === "paused",
        emptied: exists("waste_bin_emptied"),
      },
    };
  }

  /** Real job timing from the work_state attributes (set by the integration from the box's pushes). */
  function jobTiming(stateObj, mode, now) {
    var a = get(stateObj, ["attributes"]) || {};
    if (!mode || mode === "idle") return null;
    var expected = a.job_expected_seconds || null;
    if (!a.job_timing_known || !expected) return { mode: mode, known: false, expected: expected };
    var since = a.job_running_since ? Date.parse(a.job_running_since) : null;
    var elapsed = Number(a.job_run_seconds || 0) + (since ? Math.max(0, (now - since) / 1000) : 0);
    return { mode: mode, known: true, expected: expected, elapsed: elapsed, running: Boolean(since) };
  }

  /** CSS @keyframes for a choreography table, or "" when the table is empty. */
  function choreographyCss(mode) {
    var frames = CHOREOGRAPHY[mode] || [];
    if (!frames.length) return "";
    return "@keyframes drum-" + mode + " {" + frames.map(function (f) {
      return (f[0] * 100).toFixed(2) + "% { transform: rotate(" + f[1] + "deg); }";
    }).join(" ") + "}";
  }

  /** Drum icon with a progress ring, synced to the real elapsed run time of the job. */
  function drumBadge(vm) {
    var job = vm.job || { known: false };
    var paused = vm.status.kind !== "running";
    var ring, drumStyle = "";
    if (job.known) {
      var frac = Math.min(1, job.elapsed / job.expected);
      var remaining = Math.max(0, job.expected - job.elapsed);
      ring = '<circle class="ring" pathLength="100" cx="26" cy="26" r="24" style="--from:' + (100 * (1 - frac)).toFixed(2) +
        ";--dur:" + remaining.toFixed(1) + "s;animation-play-state:" + (paused ? "paused" : "running") + '"/>';
      if ((CHOREOGRAPHY[job.mode] || []).length) {
        drumStyle = ' style="animation: drum-' + job.mode + " " + job.expected + "s linear -" + job.elapsed.toFixed(1) +
          "s forwards; animation-play-state:" + (paused ? "paused" : "running") + '"';
      }
    } else {
      ring = '<circle class="ring indeterminate" pathLength="100" cx="26" cy="26" r="24"/>';
    }
    return '<svg class="drum" viewBox="0 0 52 52" role="img" aria-label="' + escapeHtml(vm.status.label) + '">' +
      '<circle class="ring-track" cx="26" cy="26" r="24"/>' + ring +
      '<g class="drum-body"' + drumStyle + ">" +
        '<circle class="shell" cx="26" cy="26" r="16"/>' +
        '<path class="litter" d="M11.2 32 A16 16 0 0 0 40.8 32 Z"/>' +
        '<path class="opening" d="M17 14.5 A16 16 0 0 1 35 14.5"/>' +
      "</g></svg>";
  }

  function gauge(level, full) {
    var pct = level === null ? 0 : Math.max(0, Math.min(100, level));
    var c = Math.PI * 40; // semicircle length
    var tone = full || pct >= 100 ? "var(--error-color, #db4437)" : pct >= 70 ? "var(--warning-color, #ffa600)" : "var(--success-color, #43a047)";
    return '<svg viewBox="0 0 100 58" class="gauge" role="img" aria-label="Waste bin ' + (level === null ? "unknown" : pct + "%") + '">' +
      '<path d="M10 50 A40 40 0 0 1 90 50" class="track"/>' +
      '<path d="M10 50 A40 40 0 0 1 90 50" class="fill" style="stroke:' + tone + ";stroke-dasharray:" + (c * pct) / 100 + " " + c + '"/>' +
      '<text x="50" y="47" text-anchor="middle" class="pct">' + (level === null ? "–" : pct + "%") + "</text></svg>";
  }

  /** Attributes that make an element open the more-info dialog of `entityId` (tap or Enter/Space). */
  function tappable(entityId) {
    return entityId ? ' data-entity="' + escapeHtml(entityId) + '" role="button" tabindex="0"' : "";
  }

  /** Card body HTML (strings from the cloud are escaped). */
  function renderCard(vm) {
    var s = vm.status;
    var offline = vm.online === false;
    var fmt = function (v, d) { return v === null ? "–" : Number(v).toFixed(d || 0); };
    var btn = function (action, icon, label, opts) {
      opts = opts || {};
      return '<button class="action' + (opts.danger ? " danger" : "") + '" data-action="' + action + '"' + (opts.disabled ? " disabled" : "") + ">" +
        '<ha-icon icon="' + icon + '"></ha-icon><span>' + escapeHtml(label) + "</span></button>";
    };
    var lastBits = [vm.last.cat, vm.last.weight !== null ? fmt(vm.last.weight, 1) + " " + vm.last.weightUnit : null,
      vm.last.duration !== null ? fmt(vm.last.duration) + " s" : null].filter(Boolean);
    var stat = function (key, value, label) {
      return '<div class="stat tap"' + tappable(vm.info[key]) + '><span class="big">' + value + "</span><span>" + escapeHtml(label) + "</span></div>";
    };
    var a = vm.actions;
    return "<ha-card>" +
      '<div class="header tap ' + s.kind + '"' + tappable(vm.info.status) + ">" +
        (vm.busy ? '<div class="badge drum-badge">' + drumBadge(vm) + "</div>"
          : '<div class="badge ' + s.anim + '"><ha-icon icon="' + s.icon + '"></ha-icon></div>') +
        '<div class="titles"><div class="name">' + escapeHtml(vm.name) + "</div>" +
        '<div class="status">' + escapeHtml(s.label) + "</div>" +
        (s.hint ? '<div class="hint">' + escapeHtml(s.hint) + "</div>" : "") + "</div></div>" +
      '<div class="body">' +
        '<div class="bin tap"' + tappable(vm.info.bin) + ">" + gauge(vm.bin.level, vm.bin.full) +
          '<div class="bin-label">Waste bin' + (vm.bin.uses !== null ? " · " + fmt(vm.bin.uses) + (vm.bin.uses === 1 ? " use" : " uses") : "") + "</div></div>" +
        '<div class="facts">' +
          '<div class="chip tap"' + tappable(vm.info.last) + '><ha-icon icon="mdi:cat"></ha-icon><span>' +
            (lastBits.length ? escapeHtml(lastBits.join(" · ")) : "No visits yet") + "</span></div>" +
          '<div class="chip tap"' + tappable(vm.info.seen) + '><ha-icon icon="mdi:clock-outline"></ha-icon><span>Last seen ' +
            escapeHtml(dflt(vm.last.seen, "–")) + "</span></div>" +
          '<div class="stats">' + stat("today", fmt(vm.visits.today), "today") + stat("avg", fmt(vm.visits.avg, 1), "per day (7 d)") +
            (vm.catWeight ? stat("catWeight", fmt(vm.catWeight.value, 1), vm.catWeight.unit || "weight") : "") + "</div>" +
        "</div></div>" +
      '<div class="actions">' +
        (a.clean ? btn("clean", "mdi:broom", "Clean", { disabled: offline || vm.busy }) : "") +
        (a.smooth ? btn("smooth", "mdi:waves", "Level", { disabled: offline || vm.busy }) : "") +
        (a.pause ? btn("pause", "mdi:pause", "Pause", { disabled: offline }) : "") +
        (a.resume ? btn("resume", "mdi:play", "Resume", { disabled: offline }) : "") +
        (a.emptied ? btn("waste_bin_emptied", "mdi:delete-restore", "Bin emptied") : "") +
      "</div></ha-card>";
  }

  var STYLES = [
    ":host { display: block; }",
    "ha-card { overflow: hidden; }",
    ".header { display: flex; align-items: center; gap: 14px; padding: 16px; transition: background .4s; }",
    ".header.running { background: linear-gradient(120deg, rgba(var(--rgb-primary-color, 3,169,244), .22), transparent 70%); }",
    ".header.paused { background: linear-gradient(120deg, rgba(255,166,0,.22), transparent 70%); }",
    ".header.blocked { background: linear-gradient(120deg, rgba(219,68,55,.25), transparent 70%); }",
    ".header.offline { opacity: .7; }",
    ".badge { width: 52px; height: 52px; flex: none; border-radius: 50%; display: grid; place-items: center; background: var(--secondary-background-color); color: var(--state-icon-color); }",
    ".running .badge { background: var(--primary-color); color: var(--text-primary-color, #fff); animation: halo 2s infinite; }",
    ".paused .badge { background: var(--warning-color, #ffa600); color: #fff; }",
    ".blocked .badge { background: var(--error-color, #db4437); color: #fff; }",
    ".badge ha-icon { --mdc-icon-size: 30px; }",
    ".spin ha-icon { animation: spin 2.4s linear infinite; }",
    ".wave ha-icon { animation: wave 1.6s ease-in-out infinite; }",
    ".bounce ha-icon { animation: bounce 1.2s ease-in-out infinite; }",
    ".pulse ha-icon { animation: pulse 1.8s ease-in-out infinite; }",
    "@keyframes spin { to { transform: rotate(360deg); } }",
    "@keyframes wave { 50% { transform: translateX(4px); } }",
    "@keyframes bounce { 50% { transform: translateY(-4px); } }",
    "@keyframes pulse { 50% { opacity: .45; } }",
    "@keyframes halo { 0% { box-shadow: 0 0 0 0 rgba(var(--rgb-primary-color, 3,169,244), .5); } 70%, 100% { box-shadow: 0 0 0 10px rgba(0,0,0,0); } }",
    "@media (prefers-reduced-motion: reduce) { .badge, .badge ha-icon, .tap { animation: none !important; transition: none !important; } }",
    /* Tappable values: subtle hover/active feedback, visible keyboard focus. */
    ".tap { cursor: pointer; border-radius: 12px; transition: background-color .15s, transform .1s; -webkit-tap-highlight-color: transparent; }",
    ".tap:hover { background-color: rgba(var(--rgb-primary-text-color, 0,0,0), .05); }",
    ".tap:active { transform: scale(.98); background-color: rgba(var(--rgb-primary-text-color, 0,0,0), .09); }",
    ".tap:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }",
    ".header.tap { border-radius: 0; }",
    /* Drum badge: progress ring from real job timing; drum rotation only from CHOREOGRAPHY. */
    ".drum-badge { background: none !important; box-shadow: none !important; animation: none !important; }",
    ".drum { width: 52px; height: 52px; overflow: visible; }",
    ".drum .ring-track { fill: none; stroke: var(--divider-color, #ddd); stroke-width: 3; }",
    ".drum .ring { fill: none; stroke: var(--primary-color); stroke-width: 3; stroke-linecap: round; transform: rotate(-90deg); transform-origin: 26px 26px; stroke-dasharray: 100; stroke-dashoffset: var(--from, 100); animation: ringfill var(--dur, 0s) linear forwards; }",
    ".paused .drum .ring { stroke: var(--warning-color, #ffa600); }",
    ".blocked .drum .ring { stroke: var(--error-color, #db4437); }",
    ".drum .ring.indeterminate { stroke-dasharray: 22 78; stroke-dashoffset: 0; animation: spin-ring 2.2s linear infinite; }",
    "@keyframes ringfill { to { stroke-dashoffset: 0; } }",
    "@keyframes spin-ring { to { transform: rotate(270deg); } }",
    ".drum-body { transform-origin: 26px 26px; }",
    ".drum .shell { fill: var(--secondary-background-color); stroke: var(--primary-text-color); stroke-width: 2; }",
    ".drum .litter { fill: var(--state-icon-color, #8a8a8a); opacity: .55; }",
    ".drum .opening { fill: none; stroke: var(--card-background-color, #fff); stroke-width: 4; stroke-linecap: round; }",
    ".titles { min-width: 0; }",
    ".name { font-size: 1.15rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
    ".status { color: var(--primary-text-color); opacity: .85; }",
    ".running .status, .cat .status { color: var(--primary-color); font-weight: 600; opacity: 1; }",
    ".hint { font-size: .8rem; color: var(--secondary-text-color); }",
    ".body { display: grid; grid-template-columns: minmax(110px, 140px) 1fr; gap: 12px; padding: 4px 16px 8px; align-items: center; }",
    "@media (max-width: 380px) { .body { grid-template-columns: 1fr; } .bin { max-width: 180px; margin: 0 auto; } }",
    ".bin { padding: 4px; }",
    ".gauge { width: 100%; display: block; }",
    ".gauge .track, .gauge .fill { fill: none; stroke-width: 9; stroke-linecap: round; }",
    ".gauge .track { stroke: var(--divider-color, #ddd); }",
    ".gauge .fill { transition: stroke-dasharray .6s; }",
    ".gauge .pct { font-size: 15px; font-weight: 600; fill: var(--primary-text-color); }",
    ".bin-label { text-align: center; font-size: .8rem; color: var(--secondary-text-color); margin-top: -4px; }",
    ".facts { display: flex; flex-direction: column; gap: 6px; min-width: 0; }",
    ".chip { display: flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 16px; font-size: .9rem; background: var(--secondary-background-color); min-width: 0; }",
    ".chip.tap:hover { filter: brightness(.96); }",
    ".chip span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
    ".chip ha-icon { --mdc-icon-size: 18px; color: var(--state-icon-color); flex: none; }",
    ".stats { display: flex; gap: 6px; flex-wrap: wrap; }",
    ".stat { display: flex; flex-direction: column; padding: 4px 8px; font-size: .75rem; color: var(--secondary-text-color); }",
    ".stat .big { font-size: 1.2rem; font-weight: 600; color: var(--primary-text-color); }",
    ".actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: 8px; padding: 8px 16px 16px; }",
    ".action { min-height: 48px; border: none; border-radius: 12px; cursor: pointer; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; font: inherit; font-size: .8rem; background: var(--secondary-background-color); color: var(--primary-text-color); }",
    ".action:hover:not([disabled]) { filter: brightness(.95); }",
    ".action[disabled] { opacity: .4; cursor: default; }",
    ".action.danger { color: var(--error-color, #db4437); }",
    ".action.pending { opacity: .6; pointer-events: none; }",
    ".action ha-icon { --mdc-icon-size: 22px; }",
  ].join("\n");

  var CONFIRM = {
    waste_bin_emptied: "Reset the waste bin counter? Do this after emptying the drawer.",
  };

  function fire(node, type, detail) {
    node.dispatchEvent(new CustomEvent(type, { detail: detail, bubbles: true, composed: true }));
  }

  /** Resolve a click/key on the card to {action} (button) or {entity} (more-info). Pure for tests. */
  function targetOf(path) {
    for (var i = 0; i < path.length; i++) {
      var el = path[i];
      var ds = el && el.dataset;
      if (!ds) continue;
      if (ds.action) return el.disabled ? null : { action: ds.action, el: el };
      if (ds.entity) return { entity: ds.entity, el: el };
    }
    return null;
  }

  var Base = typeof HTMLElement !== "undefined" ? HTMLElement : function () {};

  class VividmooCard extends Base {
    setConfig(config) {
      if (!config || !config.device_id) throw new Error("vividmoo-card: set device_id (the litter box device)");
      this._config = config;
      this._sig = null;
    }

    set hass(hass) {
      this._hass = hass;
      var ents = resolveEntities(hass, this._config.device_id);
      // Re-render only when one of our entities changed.
      var sig = values(ents).map(function (id) { var s = hass.states[id]; return id + "=" + (s && s.state) + "@" + (s && s.last_updated); }).join("|");
      if (sig === this._sig && this.shadowRoot) return;
      this._sig = sig;
      this._ents = ents;
      this._render();
    }

    _render() {
      var self = this;
      if (!this.shadowRoot) {
        this.attachShadow({ mode: "open" });
        this.shadowRoot.addEventListener("click", function (ev) { self._activate(ev.composedPath(), ev); });
        this.shadowRoot.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") self._activate(ev.composedPath(), ev, true);
        });
      }
      if (!Object.keys(this._ents).length) {
        this.shadowRoot.innerHTML = "<style>" + STYLES + '</style><ha-card><div class="header"><div class="titles">' +
          '<div class="name">Vividmoo</div><div class="hint">No Vividmoo entities found for this device_id.</div></div></div></ha-card>';
        return;
      }
      var model = viewModel(this._hass, this._ents);
      this.shadowRoot.innerHTML = "<style>" + STYLES + "\n" + (model.job ? choreographyCss(model.job.mode) : "") + "</style>" + renderCard(model);
    }

    _activate(path, ev, fromKey) {
      var target = targetOf(path);
      if (!target) return;
      if (target.entity) {
        if (fromKey) ev.preventDefault();
        fire(this, "hass-more-info", { entityId: target.entity });
      } else if (!fromKey) {
        this._press(target.action, target.el);
      }
    }

    _press(action, button) {
      var self = this;
      if (CONFIRM[action] && !window.confirm(CONFIRM[action])) return Promise.resolve();
      button.classList.add("pending");
      return Promise.resolve()
        .then(function () { return self._hass.callService("button", "press", { entity_id: self._ents[action] }); })
        .catch(function (err) { fire(self, "hass-notification", { message: "Vividmoo: " + ((err && err.message) || err) }); })
        .then(function () { button.classList.remove("pending"); });
    }

    getCardSize() { return 5; }
    getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

    static getConfigElement() { return document.createElement("vividmoo-card-editor"); }
    static getStubConfig(hass) {
      var entry = values(get(hass, ["entities"])).find(function (e) { return e.platform === "vividmoo" && e.device_id; });
      return { device_id: entry ? entry.device_id : "" };
    }
  }

  class VividmooCardEditor extends Base {
    setConfig(config) { this._config = Object.assign({}, config); this._render(); }
    set hass(hass) { this._hass = hass; this._render(); }
    _render() {
      var self = this;
      if (!this._hass || !this._config) return;
      if (!this._selector) {
        this._selector = document.createElement("ha-selector");
        this._selector.selector = { device: { integration: "vividmoo" } };
        this._selector.label = "Litter box";
        this._selector.addEventListener("value-changed", function (ev) {
          self._config = Object.assign({}, self._config, { device_id: ev.detail.value });
          fire(self, "config-changed", { config: self._config });
        });
        this.appendChild(this._selector);
      }
      this._selector.hass = this._hass;
      this._selector.value = this._config.device_id;
    }
  }

  globalThis.__vividmooCard = {
    CARD_VERSION: CARD_VERSION, KEYS: KEYS, resolveEntities: resolveEntities, viewModel: viewModel,
    renderCard: renderCard, escapeHtml: escapeHtml, targetOf: targetOf, VividmooCard: VividmooCard,
    CHOREOGRAPHY: CHOREOGRAPHY, choreographyCss: choreographyCss, jobTiming: jobTiming,
  };

  // Registered twice on purpose (modern import() and legacy <script>); define only once.
  if (typeof customElements !== "undefined" && !customElements.get("vividmoo-card")) {
    customElements.define("vividmoo-card", VividmooCard);
    customElements.define("vividmoo-card-editor", VividmooCardEditor);
    window.customCards = window.customCards || [];
    window.customCards.push({
      type: "vividmoo-card",
      name: "Vividmoo litter box",
      description: "Status, waste bin, last visit and actions for a Vividmoo litter box.",
      preview: true,
    });
    console.info("%c VIVIDMOO-CARD %c " + CARD_VERSION + " ", "background:#03a9f4;color:#fff", "");
  }
})();
