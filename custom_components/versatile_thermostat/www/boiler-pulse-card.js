/* Read-only Home Assistant dashboard card for VTherm coordinated boiler flow. */
const TAG = "vtherm-boiler-pulse-card";
const UNAVAILABLE = "Unknown";

const REASONS = {
  lower_limit: "Reached the lower limit",
  lower_limit_forecast: "Approaching the lower limit",
  joined_cooling: "Joined this run while cooling",
  cutoff_reached: "Reached its predicted cutoff",
  valve_reopen_delay: "Waiting 3 minutes after closing the valve",
  waiting_for_stable_demand: "Waiting for 2 minutes at or below the lower limit",
  finished_this_run: "Finished heating in this run",
  waiting_for_shared_call: "Waiting for another room to call",
  insufficient_headroom: "Too close to the upper limit to join",
  trend_unavailable: "No recent temperature trend",
  not_cooling: "No cooling trend detected",
  valve_opening_timeout: "Valve opening timed out",
  burner_rest: "Resting after a burner stop",
  confirmed_open: "Full valve opening confirmed",
  waiting_for_valve: "Waiting for full valve opening",
  room_comfort: "Rooms are within their bands",
  room_measurement_stale: "Room temperature report is stale",
  room_measurement_unavailable: "Room temperature unavailable",
  reference_required: "Controller requires reference water temperature",
  normal_control_required: "Room requires its ordinary controller",
  flow_entity_unavailable: "Boiler setpoint entity unavailable",
  flow_sensor_unavailable: "Boiler water sensor unavailable",
};

const readable = value => typeof value === "string" && value.length
  ? REASONS[value] || (value[0].toUpperCase() + value.slice(1)).replaceAll("_", " ")
  : UNAVAILABLE;
const number = value => ((typeof value === "number" || (typeof value === "string" && value.trim())) &&
  Number.isFinite(Number(value))) ? Number(value) : null;
const fixed = (value, digits = 1) => number(value) === null ? null : Number(value).toLocaleString(undefined, {
  maximumFractionDigits: digits,
});
const unit = (value, suffix, digits = 1) => number(value) === null ? UNAVAILABLE : fixed(value, digits) + suffix;
const minutes = sec => number(sec) === null ? UNAVAILABLE : unit(Number(sec) / 60, " min");
const flag = value => value === true ? "Yes" : value === false ? "No" : UNAVAILABLE;
const validState = state => !!state && !["unknown", "unavailable"].includes(state.state);
const asObject = obj => obj && typeof obj === "object" && !Array.isArray(obj) ? obj : {};
const array = val => Array.isArray(val) ? val : [];
const localTemp = (value, suffix = "°C") => unit(value, suffix);
const has = (obj, key) => Object.hasOwn(obj || {}, key);
const relativeName = id => (id || "").split(".").pop().replaceAll("_", " ");

function node(tag, cls, content) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (content !== undefined) element.textContent = content;
  return element;
}

function group(tag, cls, ...children) {
  const element = node(tag, cls);
  element.append(...children);
  return element;
}

function icon(name) {
  const element = node("ha-icon");
  element.setAttribute("icon", "mdi:" + name);
  element.setAttribute("aria-hidden", "true");
  return element;
}

function facts(entries) {
  const dl = node("dl", "facts");
  for (const [label, value] of entries) dl.append(node("dt", "", label), node("dd", "", value));
  return dl;
}

function section(key, summary, ...body) {
  const details = node("details");
  const header = node("summary");
  details.dataset.key = key;
  header.dataset.focus = key;
  header.append(summary, node("span", "chevron"));
  details.append(header, group("div", "detail-body", ...body));
  return details;
}

function meter(label, value, tone) {
  const val = number(value);
  const track = node("div", "track" + (val === null ? " unknown-track" : ""));
  if (val !== null) {
    const fill = node("i", tone);
    fill.style.width = Math.min(100, Math.max(0, val)) + "%";
    track.append(fill);
  }
  track.setAttribute("aria-hidden", "true");
  return group("div", "meter", group("div", "meter-label", node("span", "", label),
    node("strong", "", unit(val, "%"))), track);
}

function roomStatus(id, pulse, climate) {
  if (!validState(climate)) return ["Unavailable", "dim"];
  if (pulse.blocking_zone === id) return ["Blocking", "warn"];
  if (has(pulse.excluded_zones, id)) return ["Excluded", "warn"];
  if (has(pulse.native_zones, id)) return ["Native control", "cool"];
  if (array(pulse.requested_valves).includes(id)) {
    return pulse.reported_opening?.[id] === 1 ? ["Full open", "hot"] : ["Opening", "hot"];
  }
  if (has(pulse.room_decisions, id) || (pulse.phase !== "normal" && has(pulse.reported_opening, id))) {
    return ["Coasting", "cool"];
  }
  return climate.state === "off" ? ["Off", "dim"] : ["Outside pulse control", "dim"];
}

function findBoilers(states) {
  return Object.keys(states).filter(id => id.startsWith("binary_sensor.") &&
    has(states[id].attributes, "central_boiler_manager"));
}

const STYLE = `
  :host{display:block;color:var(--primary-text-color,#202926);font-family:var(--ha-font-family-body,var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif));font-variant-numeric:tabular-nums}
  *{box-sizing:border-box}
  ha-card{display:block;overflow:hidden;container-type:inline-size;background:var(--ha-card-background,var(--card-background-color,#fff))}
  .panel{--fg:var(--primary-text-color,#202926);--muted:var(--secondary-text-color,#596961);--line:var(--divider-color,#dce2de);
    --heat:light-dark(#a34916,#ffad76);--water:light-dark(#087b73,#72d7c7);--amber:light-dark(#855e0b,#e7c36a);
    --hot-fill:light-dark(#d8773b,#e99661);--cool-fill:light-dark(#268d80,#6bbeb3);padding:20px;color:var(--fg);
    color-scheme:var(--vtherm-color-scheme,normal)}
  .head,.section-head,.room-top,.room-line,.metric-top,.meter-label,.link-list,.room-name,.badge{display:flex;align-items:center}
  .head{gap:11px}.head h2{font-size:19px;font-weight:620;letter-spacing:-.025em;line-height:1.25;margin:0;flex:1}
  .glyph{display:grid;place-items:center;width:35px;height:35px;border-radius:10px;color:var(--heat);background:color-mix(in srgb,var(--heat) 11%,transparent);flex:none}
  ha-icon{--mdc-icon-size:19px;display:inline-flex;width:var(--mdc-icon-size);height:var(--mdc-icon-size)}
  .badge{gap:6px;max-width:100%;font-size:12px;font-weight:600;line-height:1.4;white-space:normal}
  .badge::before{content:"";background:currentColor;width:7px;height:7px;border-radius:50%;flex:none}
  .hot{color:var(--heat)}.cool{color:var(--water)}.warn{color:var(--amber)}.dim,.secondary{color:var(--muted)}
  .readings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border:1px solid var(--line);border-radius:12px;margin:16px 0 9px;overflow:hidden}
  .reading{padding:14px 15px 12px;min-width:0}.reading+.reading{border-left:1px solid var(--line)}
  .metric-top{gap:6px;font-size:12px;color:var(--muted);font-weight:550}.metric-top ha-icon{--mdc-icon-size:15px}
  .big{font-size:clamp(24px,7cqi,39px);font-weight:560;letter-spacing:-.045em;line-height:1.18;margin-top:5px}
  .big small{font-size:.48em;font-weight:500;letter-spacing:0;margin-left:3px}
  .reading .unread{font-size:19px;line-height:1.6}.status-line{display:flex;gap:6px 12px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:12px;line-height:1.5}
  .status-line b{font-weight:600;color:var(--fg)}.section-head{justify-content:space-between;margin:20px 0 4px;gap:8px}
  .section-head h3{margin:0;font-size:15px;font-weight:610}.section-head>span{font-size:12px;color:var(--muted)}
  details{border-bottom:1px solid var(--line)}summary{display:flex;align-items:center;list-style:none;cursor:pointer;gap:12px;padding:13px 1px}
  summary::-webkit-details-marker{display:none}summary:focus-visible,button:focus-visible{outline:2px solid var(--primary-color,#1976b8);outline-offset:-2px;border-radius:4px}
  .chevron{flex:none;width:7px;height:7px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;transform:rotate(45deg);margin:0 4px 4px auto}
  details[open]>summary .chevron{transform:rotate(225deg);margin-bottom:-3px}
  .room{flex:1;min-width:0}.room-top{justify-content:space-between;gap:10px;flex-wrap:wrap}
  .room-name{font-size:15px;font-weight:600;overflow-wrap:anywhere;gap:6px}.room-name ha-icon{color:var(--muted);--mdc-icon-size:16px}
  .room-line{flex-wrap:wrap;gap:3px 12px;margin-top:5px;font-size:12px;color:var(--muted)}.room-line b{color:var(--fg);font-size:13px;font-weight:550}
  .decision{font-size:12px;margin:5px 0 0;color:var(--muted);line-height:1.35;overflow-wrap:anywhere}
  .detail-body{padding:0 1px 14px}.meters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:3px 0 12px}
  .meter-label{justify-content:space-between;gap:5px;flex-wrap:wrap;font-size:11px;color:var(--muted)}.meter-label strong{color:var(--fg);font-weight:580}
  .track{height:5px;border-radius:3px;overflow:hidden;background:color-mix(in srgb,var(--muted) 16%,transparent);margin-top:6px}.track i{display:block;height:100%}
  .track .heat{background:var(--hot-fill)}.track .water{background:var(--cool-fill)}.track .neutral{background:var(--muted)}
  .unknown-track{background:repeating-linear-gradient(120deg,transparent 0 3px,color-mix(in srgb,var(--muted) 23%,transparent) 3px 5px)}
  .facts{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:5px 10px;margin:0;font-size:12px;line-height:1.45}
  .facts dt{color:var(--muted)}.facts dd{margin:0;text-align:right;overflow-wrap:anywhere}
  .band{margin:3px 0 13px}.band-top,.band-ends{display:flex;justify-content:space-between;gap:6px;font-size:11px;color:var(--muted);line-height:1.45}
  .band-rail{position:relative;height:6px;border-radius:3px;background:color-mix(in srgb,var(--cool-fill) 21%,transparent);margin:8px 4px 5px}
  .band-rail i{position:absolute;transform:translateX(-50%)}.target-tick{top:-2px;height:10px;width:2px;background:var(--muted)}.current-dot{width:10px;height:10px;border-radius:50%;top:-2px;background:var(--water);box-shadow:0 0 0 2px var(--ha-card-background,var(--card-background-color,#fff))}
  .model{margin-top:8px;border:0}.model summary{font-size:12px;font-weight:600;padding:7px 0}.model .detail-body{padding-bottom:3px}.link-list{flex-wrap:wrap;gap:6px 12px;margin-top:10px}
  button{border:none;padding:4px 0;background:transparent;color:var(--primary-color,#1976b8);font:inherit;font-size:12px;cursor:pointer}
  .system{margin-top:6px;border-bottom:0}.system summary{font-size:13px;font-weight:600;padding-bottom:5px}.system[open] summary{padding-bottom:12px}
  .msg{margin:15px 0 1px;line-height:1.45;font-size:13px;color:var(--muted)}
  @container(max-width:360px){.panel{padding:14px}.head{flex-wrap:wrap}.head h2{font-size:17px}.head>.badge{font-size:11px}.reading{padding:12px}.room-top{gap:3px 7px}.meters{gap:7px}}
  @container(max-width:360px){.meter-label{align-items:flex-start;flex-direction:column;gap:3px}}
`;

function metric(title, value, symbol, tone) {
  const reading = node("div", "big " + (value === null ? "unread dim" : tone));
  if (value === null) reading.textContent = UNAVAILABLE;
  else reading.append(document.createTextNode(fixed(value)), node("small", "", "°C"));
  return group("div", "reading", group("div", "metric-top", icon(symbol), node("span", "", title)), reading);
}

function band(decision, current, target) {
  const low = number(decision.lower_temperature), high = number(decision.upper_temperature);
  if (low === null || high === null || low >= high) return null;
  const rail = node("div", "band-rail");
  rail.setAttribute("aria-hidden", "true");
  for (const [value, cls] of [[target, "target-tick"], [current, "current-dot"]]) {
    if (number(value) === null) continue;
    const mark = node("i", cls);
    mark.style.left = Math.max(0, Math.min(100, (value - low) / (high - low) * 100)) + "%";
    rail.append(mark);
  }
  return group("div", "band", group("div", "band-top", node("span", "", "Comfort band"),
    node("span", "", "Target " + localTemp(target))), rail,
    group("div", "band-ends", node("span", "", localTemp(low)), node("span", "", localTemp(high))));
}

function timeOf(when) {
  const date = new Date(when || NaN);
  return Number.isFinite(date.valueOf()) ? date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }) : UNAVAILABLE;
}

function celsius(state) {
  if (!validState(state)) return null;
  const value = number(state.state);
  if (value === null) return null;
  const u = state.attributes?.unit_of_measurement;
  return u === "°F" ? (value - 32) * 5 / 9 : u === "°C" ? value : null;
}

class BoilerPulseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = node("style", "", STYLE);
    this.card = node("ha-card");
    this.panel = node("div", "panel");
    this.card.append(this.panel);
    this.shadowRoot.append(style, this.card);
    this.config = {};
  }

  static getStubConfig(hass) {
    const boilers = findBoilers(hass?.states || {});
    return boilers.length === 1 ? { entity: boilers[0] } : {};
  }

  setConfig(config) {
    if (!config || (config.entity !== undefined && !String(config.entity).startsWith("binary_sensor."))) {
      throw new Error("Choose a central boiler binary_sensor entity.");
    }
    if (config.rooms !== undefined && (!Array.isArray(config.rooms) || config.rooms.some(room =>
      !(typeof room === "string" ? room : room?.entity)?.startsWith?.("climate.")))) {
      throw new Error("Each room must specify a climate entity.");
    }
    this.config = config;
    this.tracked = null;
    if (this._hass) this.hass = this._hass;
  }

  getCardSize() { return Math.ceil(4 + (this.rooms?.length || 0) * 1.5); }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    if (!hass?.states) return;
    const states = hass.states;
    const dark = hass.themes?.darkMode;
    this.panel.style.colorScheme = typeof dark === "boolean" ? (dark ? "dark" : "light") : "light dark";
    if (this.tracked && this.boilerId && this.tracked.every(([id, prior]) => states[id] === prior)
        && this.inventory === hass.entities) return;
    const boilers = this.config.entity ? [this.config.entity] : findBoilers(states);
    this.boilerId = boilers.length === 1 ? boilers[0] : null;
    const boiler = states[this.boilerId];
    const manager = validState(boiler) ? boiler.attributes.central_boiler_manager : null;
    const flow = manager?.dynamic_flow || {};
    const pulse = flow.pulse || {};
    this.inventory = hass.entities;
    const ids = new Set([this.boilerId, flow.setpoint_entity, flow.temperature_sensor].filter(Boolean));
    const previouslyOpen = new Set(Array.from(this.panel.querySelectorAll("details[open]"), d => d.dataset.key));
    const focus = this.shadowRoot.activeElement?.dataset.focus;
    this.panel.replaceChildren();
    const title = this.config.title || "Boiler pulse diagnostics";
    const header = group("div", "head", group("span", "glyph", icon("radiator")), node("h2", "", title));
    this.panel.append(header);
    if (!manager) {
      let message = "Waiting for central boiler diagnostics.";
      if (!this.boilerId) message = boilers.length > 1 ? "Choose your boiler using the card’s entity setting."
        : "No central boiler found. Set entity to your VTherm central boiler binary sensor.";
      else if (!validState(boiler)) message = this.boilerId + " is unavailable.";
      this.panel.append(node("p", "msg", message));
      this.rooms = [];
    } else {
      const call = manager.is_on === true, pending = manager.activation_scheduled === true;
      header.append(node("span", "badge " + (call ? "hot" : pending ? "warn" : "dim"),
        call ? "Heat call active" : pending ? "Start pending" : manager.is_on === false ? "Standby" : "Call unknown"));
      const numberState = states[flow.setpoint_entity];
      const waterState = states[flow.temperature_sensor];
      const supplyInvalid = flow.reason === "flow_sensor_unavailable" || pulse.reason === "water_unavailable" ||
        waterState?.attributes.assumed_state || waterState?.attributes.restored;
      this.panel.append(group("div", "readings", metric("Boiler setpoint", celsius(numberState), "target", "hot"),
        metric("Measured supply", supplyInvalid ? null : celsius(waterState), "thermometer-water", "cool")));
      const phase = pulse.phase ? readable(pulse.phase) : flow.enabled === false ? "Disabled" : "Ordinary control";
      const flame = pulse.burner === true ? "On" : pulse.burner === false ? "Off" : UNAVAILABLE;
      const summary = node("div", "status-line");
      for (const [label, value] of [["Phase", phase], ["Burner", flame]]) {
        summary.append(group("span", "", document.createTextNode(label + " "), node("b", "", value)));
      }
      this.panel.append(summary);
      this.rooms = this.getRooms(states, pulse);
      const sectionHeader = group("div", "section-head", node("h3", "", "Radiators"));
      if (Array.isArray(pulse.requested_valves)) sectionHeader.append(node("span", "", pulse.requested_valves.length + " requested"));
      this.panel.append(sectionHeader);
      if (!this.rooms.length) this.panel.append(node("p", "msg", "No room diagnostics yet. You can list climate entities under rooms."));
      for (const room of this.rooms) {
        ids.add(room.entity);
        const climate = states[room.entity];
        const a = climate?.attributes || {};
        const diagId = room.diagnostics || a.specific_states?.regulation_diagnostics || a.regulation_diagnostics;
        if (diagId) ids.add(diagId);
        this.panel.append(this.renderRoom(room, climate, diagId, states[diagId], pulse));
      }
      this.panel.append(this.systemDetails(manager, flow, pulse));
    }
    this.tracked = [...ids].map(id => [id, states[id]]);
    for (const element of this.panel.querySelectorAll("details")) element.open = previouslyOpen.has(element.dataset.key);
    if (focus) {
      const control = Array.from(this.panel.querySelectorAll("[data-focus]")).find(el => el.dataset.focus === focus);
      control?.focus({ preventScroll: true });
    }
  }

  getRooms(states, pulse) {
    if (Array.isArray(this.config.rooms)) {
      return this.config.rooms.map(room => typeof room === "string" ? { entity: room } : room);
    }
    const known = new Set([
      ...Object.keys(asObject(pulse.room_decisions)), ...Object.keys(asObject(pulse.reported_opening)),
      ...Object.keys(asObject(pulse.native_zones)), ...Object.keys(asObject(pulse.excluded_zones)),
      ...Object.keys(asObject(pulse.heat_balance_minutes)), ...array(pulse.requested_valves),
      ...array(pulse.recovery_zones), pulse.blocking_zone,
    ].filter(id => typeof id === "string" && id.startsWith("climate.")));
    // The central configuration is shared by all participating VTherm rooms.
    for (const id of Object.keys(states)) {
      if (id.startsWith("climate.") && states[id].attributes.configuration?.is_used_by_central_boiler === true) known.add(id);
    }
    const active = new Set(array(pulse.requested_valves));
    return [...known].sort((a, b) => Number(active.has(b)) - Number(active.has(a)) ||
      (states[a]?.attributes.friendly_name || a).localeCompare(states[b]?.attributes.friendly_name || b)
    ).map(entity => ({ entity }));
  }

  moreInfo(label, id) {
    const button = node("button", "", label);
    button.type = "button";
    button.dataset.focus = "info:" + id;
    button.addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-more-info", {
      bubbles: true, composed: true, detail: { entityId: id },
    })));
    return button;
  }

  renderRoom(room, climate, diagId, sensor, pulse) {
    const id = room.entity;
    const a = validState(climate) ? climate.attributes : {};
    const live = validState(sensor) ? asObject(sensor.attributes.live) : {};
    const smart = asObject(live.power?.boiler_flow), model = asObject(live.model), twin = asObject(live.analysis?.twin);
    const decision = asObject(pulse.room_decisions?.[id]);
    const [label, tone] = roomStatus(id, pulse, climate);
    const name = room.name || climate?.attributes.friendly_name || relativeName(id);
    const header = group("div", "room", group("div", "room-top", node("span", "room-name", name),
      node("span", "badge " + tone, label)));
    const storeUnit = a.temperature_unit || this._hass.config?.unit_system?.temperature || "°C";
    const temp = number(a.current_temperature), target = number(a.temperature);
    const knownPulse = has(pulse.room_decisions, id) || (pulse.phase && pulse.phase !== "normal" && has(pulse.reported_opening, id));
    const request = knownPulse ? (array(pulse.requested_valves).includes(id) ? 100 : 0) : number(smart.pulse_opening_percent);
    const reported = number(pulse.reported_opening?.[id]);
    const readback = reported === null ? null : 100 * reported;
    const rawDemand = number(smart.reference_demand_percent);
    header.append(group("div", "room-line", group("span", "", node("b", "", localTemp(temp, storeUnit)),
      document.createTextNode(" / " + localTemp(target, storeUnit) + " target")),
      node("span", "", "Demand " + unit(rawDemand, "%")),
      node("span", "", "Request " + unit(request ?? a.valve_open_percent, "%"))));
    const why = pulse.native_zones?.[id] || pulse.excluded_zones?.[id] ||
      (pulse.blocking_zone === id ? pulse.reason : null) || decision.reason;
    if (why) {
      const limit = number(pulse.room_temperature_timeout_sec);
      const reason = why === "room_measurement_stale" && limit !== null
        ? "Room temperature report is older than " + unit(limit / 3600, " h")
        : readable(why);
      header.append(node("p", "decision", reason));
    }
    const details = section(id, header);
    const body = details.lastElementChild;
    const c = v => storeUnit === "°F" && v !== null ? (v - 32) * 5 / 9 : v;
    const roomBand = band(decision, c(temp), c(target));
    if (roomBand) body.append(roomBand);
    body.append(group("div", "meters", meter("SmartPI demand", rawDemand, "neutral"),
      meter("Pulse request", request, "heat"), meter("Reported valve", readback, "water")));
    body.append(facts([
      ["Thermostat valve command", unit(a.valve_open_percent, "%")],
      ["Pulse cutoff", localTemp(decision.cutoff_temperature)],
      ["Scheduler trend", unit(decision.slope_per_hour, " °C/h", 2)],
      ["Temperature reported", timeOf(a.specific_states?.last_temperature_datetime)],
      ["Heat balance at full output", unit(pulse.heat_balance_minutes?.[id], " min")],
      ["Recovery requested", Array.isArray(pulse.recovery_zones) ? flag(pulse.recovery_zones.includes(id)) : UNAVAILABLE],
    ]));
    if (Object.keys(live).length) {
      const diagnostics = section("model:" + id, node("span", "", "SmartPI model"), facts([
        ["Estimated delivered heat", unit(smart.estimated_pulse_heat_percent, "%")],
        ["Radiator capacity ratio", unit(smart.capacity_ratio, "×", 2)],
        ["Flow compensation", readable(smart.reason)],
        ["Learning paused", flag(smart.learning_paused)],
        ["SmartPI temperature trend", unit(live.analysis?.control?.temperature_slope_h, " °C/h", 2)],
        ["Heating / cooling response delay", minutes(model.deadtime_heat_s) + " / " + minutes(model.deadtime_cool_s)],
        ["Delay reliable (heat / cool)", flag(model.deadtime_heat_reliable) + " / " + flag(model.deadtime_cool_reliable)],
        ["Room time constant", unit(model.tau_min, " min")],
        ["Time constant reliable", flag(model.tau_reliable)],
        ["Best-case time to room target", minutes(twin.eta_s)],
        ["ETA status", readable(twin.eta_reason)],
        ["Forecast reliable", flag(twin.twin_model_reliable)],
        ["Model confidence", readable(model.confidence)],
      ]));
      diagnostics.className = "model";
      body.append(diagnostics);
    } else if (diagId) body.append(node("p", "msg", "SmartPI diagnostics are unavailable for this room."));
    const links = node("div", "link-list");
    if (climate) links.append(this.moreInfo("Thermostat history", id));
    if (sensor && diagId) links.append(this.moreInfo("SmartPI diagnostics", diagId));
    if (links.childElementCount) body.append(links);
    return details;
  }

  systemDetails(manager, flow, pulse) {
    const ctx = flow.context || {};
    const details = section("system", node("span", "", "Boiler diagnostics"), facts([
      ["Flow control", flow.enabled === true ? "Enabled" : flow.enabled === false ? "Disabled" : UNAVAILABLE],
      ["Flow permits heating", flag(flow.can_heat)],
      ["Flow decision", readable(flow.reason)], ["Pulse decision", readable(pulse.reason)],
      ["Pulse water request", localTemp(pulse.requested_flow)],
      ["Controller target", localTemp(flow.target_temperature)],
      ["Water used by SmartPI", localTemp(ctx.flow_temperature)],
      ["Reference temperature", localTemp(ctx.reference_temperature)],
      ["Radiator exponent", unit(ctx.exponent, "")],
      ["Flow learning allowed", flag(ctx.learning_allowed)],
      ["Allowed drop / rise", localTemp(pulse.undershoot) + " / " + localTemp(pulse.overshoot)],
      ["Blocking room", pulse.blocking_zone || (has(pulse, "blocking_zone") ? "None" : UNAVAILABLE)],
      ["Active devices / threshold", unit(manager.nb_active_device_for_boiler, "") + " / " +
        unit(manager.nb_active_device_for_boiler_threshold, "")],
      ["Active power / threshold (configured units)", unit(manager.total_power_active_for_boiler, "") + " / " +
        unit(manager.total_power_active_for_boiler_threshold, "")],
      ["Start scheduled", flag(manager.activation_scheduled)],
      ["Configured start delay", unit(manager.delayed_activation_sec, " s")],
      ["Keep-alive interval", unit(manager.keep_alive_boiler_delay_sec, " s")],
    ]));
    details.className = "system";
    const links = node("div", "link-list");
    for (const [label, id] of [["Boiler history", this.boilerId], ["Setpoint history", flow.setpoint_entity],
      ["Water history", flow.temperature_sensor]]) if (id && this._hass.states[id]) links.append(this.moreInfo(label, id));
    details.lastElementChild.append(links);
    return details;
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, BoilerPulseCard);
window.customCards ??= [];
if (!window.customCards.some(card => card.type === TAG)) window.customCards.push({
  type: TAG, name: "Boiler pulse diagnostics", preview: true,
  description: "Water temperature, heating decisions and valve feedback for each VTherm room.",
});
