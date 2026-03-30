/**
 * hestia-schedule-card.js
 * Lovelace card for Hestia Scheduler.
 */

const DOMAIN = "hestia_scheduler";
const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WEEKDAY_LABELS = {
  mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday",
  fri: "Friday", sat: "Saturday", sun: "Sunday",
};
const PRESET_LABELS = {
  home: "Home", away: "Away", eco: "Eco",
  sleep: "Sleep", boost: "Boost", comfort: "Comfort",
};
const PRESET_MDI = {
  home: "mdi:home", away: "mdi:account-arrow-right", eco: "mdi:leaf",
  sleep: "mdi:bed", boost: "mdi:rocket-launch", comfort: "mdi:white-balance-sunny",
};
const VALID_PRESETS = ["home", "away", "eco", "sleep", "boost", "comfort"];
const PRESET_TEMP_APPROX = { home: 20, comfort: 22, boost: 25, sleep: 18, eco: 9, away: 12 };
const TOTAL_MINS = 1440;
const LABEL_W = 80;
const MARKER_W = 3;
const ROW_GAP = 8;

function timeToMinutes(t) {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}
function minutesToTime(m) {
  const h = Math.floor(m / 60) % 24;
  const min = m % 60;
  return `${String(h).padStart(2,"0")}:${String(min).padStart(2,"0")}`;
}
function todayWeekday() {
  return {0:"sun",1:"mon",2:"tue",3:"wed",4:"thu",5:"fri",6:"sat"}[new Date().getDay()];
}
function lerp(v, lo, hi, oLo, oHi) {
  return oLo + ((v - lo) / (hi - lo)) * (oHi - oLo);
}
function tempColor(temp) {
  const c = Math.max(5, Math.min(30, temp));
  const hue = lerp(c, 5, 30, 240, 0);
  const lig = Math.max(45, Math.min(55, lerp(c, 10, 22, 45, 55)));
  return `hsl(${hue.toFixed(0)},75%,${lig.toFixed(0)}%)`;
}
function resolveTemp(slot) {
  if (slot.temperature != null) return slot.temperature;
  return PRESET_TEMP_APPROX[slot.preset] ?? 15;
}
function slotColor(slot) { return tempColor(resolveTemp(slot)); }

function presetIcon(preset, size, color) {
  const icon = PRESET_MDI[preset];
  if (!icon) return "";
  return `<ha-icon icon="${icon}" style="--mdc-icon-size:${size}px;color:${color};vertical-align:middle;"></ha-icon>`;
}

function buildSegments(slots) {
  if (!slots.length) return [];
  const sorted = [...slots].sort((a,b) => timeToMinutes(a.time) - timeToMinutes(b.time));
  return sorted.map((slot, i) => {
    const start = timeToMinutes(slot.time);
    const end = i + 1 < sorted.length ? timeToMinutes(sorted[i+1].time) : TOTAL_MINS;
    return {
      slot, startMinutes: start, endMinutes: end,
      widthPct: ((end - start) / TOTAL_MINS) * 100,
      leftPct: (start / TOTAL_MINS) * 100,
    };
  });
}

function activePresetNow(zone) {
  const day = todayWeekday();
  const slots = zone.days?.[day] ?? [];
  if (!slots.length) return null;
  const now = new Date();
  const nowMins = now.getHours() * 60 + now.getMinutes();
  const sorted = [...slots].sort((a, b) => timeToMinutes(a.time) - timeToMinutes(b.time));
  let active = sorted[sorted.length - 1];
  for (const s of sorted) {
    if (timeToMinutes(s.time) <= nowMins) active = s;
  }
  return active?.preset ?? null;
}

// ---- Day Row ----

class HestiaScheduleDayRow extends HTMLElement {
  set day(d) { this._day = d; this._render(); }
  set zone(z) { this._zone = z; this._render(); }
  set isToday(v) { this._isToday = v; this._render(); this._startNowTimer(); }
  set disabled(v) { this._disabled = v; this._render(); }
  set preheat(p) {
    const key = p ? `${p.active}|${p.startMinutes}|${p.nextSlotMinutes}|${p.isLive}` : "";
    if (key === this._preheatKey) return;
    this._preheatKey = key;
    this._preheat = p;
    this._render();
  }
  set heatingOn(v) {
    if (v === this._heatingOn) return;
    this._heatingOn = v;
    this._render();
  }

  connectedCallback() {
    this.addEventListener("click", () => {
      if (this._day) this.dispatchEvent(new CustomEvent("hestia-edit-day",
        { detail: { day: this._day }, bubbles: true, composed: true }));
    });
    this.addEventListener("mouseenter", () => {
      this.style.background = "rgba(var(--rgb-primary-color, 255,152,0), .08)";
    });
    this.addEventListener("mouseleave", () => {
      this.style.background = "";
    });
  }

  disconnectedCallback() { this._stopNowTimer(); }

  _startNowTimer() {
    this._stopNowTimer();
    if (!this._isToday) return;
    this._nowTimer = setInterval(() => {
      const el = this.querySelector(".now-needle");
      if (!el) return;
      const now = new Date();
      el.style.left = ((now.getHours()*60 + now.getMinutes()) / TOTAL_MINS * 100).toFixed(2) + "%";
    }, 60000);
  }

  _stopNowTimer() {
    if (this._nowTimer) { clearInterval(this._nowTimer); this._nowTimer = null; }
  }

  _render() {
    if (!this._day || !this._zone) return;
    const slots = this._zone.days[this._day] ?? [];
    const segs = buildSegments(slots);
    const label = WEEKDAY_LABELS[this._day];
    const barOpacity = this._disabled ? 0.3 : 1;

    const segHtml = segs.length > 0
      ? segs.map((s, i) => {
          const color = slotColor(s.slot);
          const icon = s.slot.preset ? presetIcon(s.slot.preset, 15, "#fff") : "";
          const lbl = s.slot.preset
            ? icon
            : `<span style="font-size:.72em;font-weight:600;">${resolveTemp(s.slot).toFixed(1)}°C</span>`;
          const bell = s.slot.preemptable
            ? `<ha-icon icon="mdi:bell-alert" style="--mdc-icon-size:12px;color:#FFD54F;vertical-align:middle;margin-left:2px;"></ha-icon>` : "";
          const show = s.widthPct > 4;
          const divider = i > 0
            ? "border-left:2px solid var(--card-background-color, #1c1c1c);" : "";
          return `<div style="position:absolute;top:0;height:100%;left:${s.leftPct.toFixed(3)}%;width:${s.widthPct.toFixed(3)}%;background:${color};display:flex;align-items:center;justify-content:center;overflow:hidden;box-sizing:border-box;${divider}">${show ? `<span style="color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.4);white-space:nowrap;display:inline-flex;align-items:center;">${lbl}${bell}</span>` : ""}</div>`;
        }).join("")
      : `<div style="height:100%;display:flex;align-items:center;justify-content:center;font-size:.72em;color:var(--secondary-text-color);">Click to edit</div>`;

    const todayColor = this._isToday
      ? "color:var(--primary-color, #03a9f4);font-weight:700;"
      : "color:var(--primary-text-color);font-weight:400;";

    this.style.cssText = `display:flex;align-items:center;gap:${ROW_GAP}px;cursor:pointer;border-radius:6px;padding:1px 0;transition:background .12s;`;
    this.title = "Click to edit";

    const sepHtml = segs.length > 1
      ? segs.slice(1).map(s =>
          `<div style="position:absolute;top:0;width:1px;height:100%;left:${s.leftPct.toFixed(3)}%;background:rgba(0,0,0,0.2);z-index:4;pointer-events:none;"></div>`
        ).join("")
      : "";

    let preheatHtml = "";
    const ph = this._preheat;
    if (ph && ph.active && ph.startMinutes < ph.nextSlotMinutes) {
      const gradLeft = (ph.startMinutes / TOTAL_MINS * 100).toFixed(3);
      const gradWidth = ((ph.nextSlotMinutes - ph.startMinutes) / TOTAL_MINS * 100).toFixed(3);
      const gradOpacity = ph.isLive ? 0.92 : 0.45;
      preheatHtml = `<div style="position:absolute;top:0;height:100%;left:${gradLeft}%;width:${gradWidth}%;background:linear-gradient(to right, ${ph.currentSlotColor}, ${ph.nextSlotColor});z-index:3;pointer-events:none;opacity:${gradOpacity};"></div>`;
    }

    let timeIndicator = "";
    if (this._isToday) {
      const now = new Date();
      const nowPct = ((now.getHours() * 60 + now.getMinutes()) / TOTAL_MINS * 100).toFixed(2);
      const fireIcon = this._heatingOn
        ? `<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);line-height:0;filter:drop-shadow(0 0 2px rgba(0,0,0,.8)) drop-shadow(0 0 4px rgba(0,0,0,.4));"><ha-icon icon="mdi:fire" style="--mdc-icon-size:16px;color:var(--error-color,#ef5350);"></ha-icon></div>`
        : "";
      timeIndicator = `<div class="now-needle" style="position:absolute;top:-1px;bottom:-1px;left:${nowPct}%;width:2px;background:var(--primary-color,#03a9f4);z-index:6;border-radius:1px;box-shadow:0 0 3px var(--primary-color,#03a9f4);overflow:visible;">${fireIcon}</div>`;
    }

    this.innerHTML = `
      <span style="width:${LABEL_W}px;font-size:.82em;flex-shrink:0;${todayColor}">${label}</span>
      <div style="width:${MARKER_W}px;height:26px;border-radius:2px;background:var(--primary-color,#03a9f4);flex-shrink:0;visibility:${this._isToday?"visible":"hidden"};"></div>
      <div class="tl" style="position:relative;height:26px;flex:1;min-width:0;background:var(--secondary-background-color,#333);border-radius:5px;overflow:hidden;opacity:${barOpacity};">${segHtml}${sepHtml}${preheatHtml}${timeIndicator}</div>`;
  }
}
customElements.define("hestia-day-row", HestiaScheduleDayRow);

// ---- Editor Dialog ----

class HestiaScheduleEditor extends HTMLElement {
  constructor() { super(); this._slots = []; this._copyDays = new Set(); }

  open(zone, day) {
    this._zone = zone; this._day = day;
    this._slots = (zone.days[day] ?? []).map(s => ({...s}));
    this._copyDays = new Set();
    this._render();
    this.style.display = "block";
  }
  close() { this.style.display = "none"; }
  connectedCallback() {
    this.style.display = "none";
    this.style.position = "fixed";
    this.style.inset = "0";
    this.style.zIndex = "9999";
  }

  _sortedSlots() {
    return [...this._slots].sort((a,b) => timeToMinutes(a.time) - timeToMinutes(b.time));
  }

  _renderTimeline() {
    const sorted = this._sortedSlots();
    if (!sorted.length) return "";
    return sorted.map((slot, i) => {
      const start = timeToMinutes(slot.time);
      const end = i+1 < sorted.length ? timeToMinutes(sorted[i+1].time) : TOTAL_MINS;
      const l = (start/TOTAL_MINS*100).toFixed(2);
      const w = ((end-start)/TOTAL_MINS*100).toFixed(2);
      const divider = i > 0 ? "border-left:2px solid var(--card-background-color,#1c1c1c);" : "";
      return `<div style="position:absolute;top:0;height:100%;left:${l}%;width:${w}%;background:${slotColor(slot)};${divider}box-sizing:border-box;"></div>`;
    }).join("");
  }

  _render() {
    if (!this._day || !this._zone) return;
    const sorted = this._sortedSlots();
    const inputStyle = "padding:4px 6px;border-radius:4px;border:1px solid var(--divider-color,#555);background:var(--card-background-color,#1c1c1c);color:var(--primary-text-color);";

    const headerHtml = `
      <div style="display:grid;grid-template-columns:84px 1fr auto 32px;align-items:center;gap:8px;padding:0 8px 2px;font-size:.7em;font-weight:600;color:var(--secondary-text-color);text-transform:uppercase;letter-spacing:.04em;">
        <span>Time</span>
        <span>Target</span>
        <span title="Notification lead time before this transition fires">Notify before</span>
        <span></span>
      </div>`;

    const slotsHtml = sorted.map((slot, i) => {
      const isPreset = slot.preset != null;
      const color = slotColor(slot);
      const presetOpts = VALID_PRESETS.map(p => {
        const icon = PRESET_MDI[p];
        return `<option value="${p}"${slot.preset===p?" selected":""}>${PRESET_LABELS[p]||p}</option>`;
      }).join("");

      const valHtml = isPreset
        ? `<div style="display:inline-flex;align-items:center;gap:4px;">${presetIcon(slot.preset,18,"var(--primary-text-color)")}<select data-idx="${i}" data-field="preset" style="${inputStyle}">${presetOpts}</select></div>`
        : `<input type="number" data-idx="${i}" data-field="temperature" value="${slot.temperature??20}" min="5" max="30" step="0.5" style="width:72px;${inputStyle}">`;

      const toggleLabel = isPreset ? "°C" : "Preset";
      const toggleTitle = isPreset ? "Switch to direct temperature" : "Switch to preset mode";

      const preemptOpacity = slot.preemptable ? 1 : 0.3;
      const preemptHtml = `
        <div style="display:flex;align-items:center;gap:3px;white-space:nowrap;">
          <input type="checkbox" data-idx="${i}" data-field="preemptable"${slot.preemptable?" checked":""} title="Notify before this transition" style="width:15px;height:15px;accent-color:var(--primary-color,#03a9f4);cursor:pointer;flex-shrink:0;">
          <input type="number" data-idx="${i}" data-field="preempt_lead_minutes" value="${slot.preempt_lead_minutes ?? 15}" min="1" max="120" step="1" title="Minutes before transition" style="width:38px;padding:2px 3px;font-size:.78em;border-radius:4px;border:1px solid var(--divider-color,#555);background:var(--card-background-color,#1c1c1c);color:var(--primary-text-color);opacity:${preemptOpacity};text-align:center;">
          <span style="font-size:.68em;color:var(--secondary-text-color);opacity:${preemptOpacity};">m</span>
        </div>`;

      return `
        <div style="display:grid;grid-template-columns:84px 1fr auto 32px;align-items:center;gap:8px;background:var(--secondary-background-color,#333);border-radius:8px;padding:8px;">
          <input type="time" data-idx="${i}" data-field="time" value="${slot.time}" style="width:78px;${inputStyle}">
          <div style="display:flex;align-items:center;gap:6px;">
            <div style="width:12px;height:12px;border-radius:3px;background:${color};flex-shrink:0;"></div>
            ${valHtml}
            <button data-idx="${i}" data-action="toggle-type" title="${toggleTitle}" style="font-size:.72em;padding:2px 8px;border:1px solid var(--divider-color,#555);border-radius:12px;cursor:pointer;background:transparent;color:var(--primary-text-color);white-space:nowrap;">→ ${toggleLabel}</button>
          </div>
          ${preemptHtml}
          <button data-idx="${i}" data-action="remove" title="Remove slot" style="background:none;border:none;cursor:pointer;color:var(--error-color,#c00);padding:2px;display:flex;align-items:center;justify-content:center;">
            <ha-icon icon="mdi:delete" style="--mdc-icon-size:20px;"></ha-icon>
          </button>
        </div>`;
    }).join("");

    const copyHtml = WEEKDAYS.filter(d => d !== this._day).map(d => {
      const sel = this._copyDays.has(d) ? "background:var(--primary-color,#03a9f4);color:#fff;border-color:var(--primary-color,#03a9f4);" : "";
      return `<span data-day="${d}" style="padding:3px 10px;border-radius:12px;border:1px solid var(--divider-color,#555);cursor:pointer;font-size:.82em;color:var(--primary-text-color);${sel}">${WEEKDAY_LABELS[d].slice(0,3)}</span>`;
    }).join("");

    this.innerHTML = `
      <div style="position:absolute;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;" id="overlay">
        <div style="background:var(--card-background-color,#1c1c1c);border-radius:12px;padding:20px 24px;width:min(620px,95vw);max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.3);display:flex;flex-direction:column;gap:12px;color:var(--primary-text-color);">
          <h2 style="margin:0;font-size:1.1em;">${WEEKDAY_LABELS[this._day]} – ${this._zone.name}</h2>
          <div style="position:relative;height:28px;background:var(--secondary-background-color,#333);border-radius:6px;overflow:hidden;">${this._renderTimeline()}</div>
          ${headerHtml}
          <div id="slots" style="display:flex;flex-direction:column;gap:6px;">${slotsHtml}</div>
          <button id="add-btn" style="align-self:flex-start;background:var(--primary-color,#03a9f4);color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:.85em;">+ Add slot</button>
          <div><strong style="font-size:.85em;">Copy to days:</strong><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;" id="copy-days">${copyHtml}</div></div>
          <div style="display:flex;justify-content:flex-end;gap:10px;">
            <button id="cancel-btn" style="padding:8px 18px;border:1px solid var(--divider-color,#555);border-radius:6px;background:none;cursor:pointer;color:var(--primary-text-color);">Cancel</button>
            <button id="save-btn" style="padding:8px 18px;background:var(--primary-color,#03a9f4);color:#fff;border:none;border-radius:6px;cursor:pointer;">Save</button>
          </div>
        </div>
      </div>`;

    this.querySelector("#overlay").addEventListener("click", e => { if(e.target.id==="overlay") this.close(); });
    this.querySelector("#cancel-btn").addEventListener("click", () => this.close());
    this.querySelector("#save-btn").addEventListener("click", () => this._save());
    this.querySelector("#add-btn").addEventListener("click", () => {
      const last = this._slots[this._slots.length-1];
      const newMin = last ? (timeToMinutes(last.time)+60)%TOTAL_MINS : 480;
      this._slots.push({ time: minutesToTime(newMin), temperature: 20, preset: null, preemptable: false, preempt_lead_minutes: 15 });
      this._render();
    });
    this.querySelector("#copy-days").querySelectorAll("[data-day]").forEach(el => {
      el.addEventListener("click", () => {
        const day = el.dataset.day;
        if (this._copyDays.has(day)) {
          this._copyDays.delete(day);
          el.style.background = ""; el.style.color = "var(--primary-text-color)"; el.style.borderColor = "var(--divider-color,#555)";
        } else {
          this._copyDays.add(day);
          el.style.background = "var(--primary-color,#03a9f4)"; el.style.color = "#fff"; el.style.borderColor = "var(--primary-color,#03a9f4)";
        }
      });
    });
    this.querySelector("#slots").addEventListener("change", e => this._onChange(e));
    this.querySelector("#slots").addEventListener("input", e => this._onInput(e));
    this.querySelector("#slots").addEventListener("click", e => this._onAction(e));
  }

  _onChange(e) {
    const t = e.target;
    const idx = parseInt(t.dataset.idx);
    if (isNaN(idx)) return;
    const field = t.dataset.field;
    const sortedMap = this._sortedSlots().map((s,i) => this._slots.indexOf(s));
    const actual = sortedMap[idx];
    const slot = {...this._slots[actual]};
    if (field === "time") slot.time = t.value;
    else if (field === "temperature") slot.temperature = parseFloat(t.value);
    else if (field === "preset") slot.preset = t.value;
    else if (field === "preemptable") { slot.preemptable = t.checked; this._slots[actual]=slot; this._render(); return; }
    else if (field === "preempt_lead_minutes") { slot.preempt_lead_minutes = parseInt(t.value) || 15; this._slots[actual]=slot; return; }
    this._slots[actual] = slot;
    const swatches = this.querySelectorAll("[style*='width:12px']");
    if (swatches[idx]) swatches[idx].style.background = slotColor(slot);
    const tl = this.querySelector("[style*='position:relative;height:28px']");
    if (tl) tl.innerHTML = this._renderTimeline();
  }

  _onInput(e) {
    const t = e.target;
    if (t.dataset.field === "preempt_lead_minutes") {
      const idx = parseInt(t.dataset.idx);
      if (isNaN(idx)) return;
      const sortedMap = this._sortedSlots().map(s => this._slots.indexOf(s));
      const actual = sortedMap[idx];
      this._slots[actual] = {...this._slots[actual], preempt_lead_minutes: parseInt(t.value) || 15};
    }
  }

  _onAction(e) {
    let t = e.target;
    while (t && !t.dataset.action && t !== this) t = t.parentElement;
    if (!t || !t.dataset.action) return;
    const idx = parseInt(t.dataset.idx);
    const sortedMap = this._sortedSlots().map(s => this._slots.indexOf(s));
    const actual = sortedMap[idx];
    if (t.dataset.action === "remove") {
      this._slots.splice(actual, 1);
      this._render();
    } else if (t.dataset.action === "toggle-type") {
      const s = this._slots[actual];
      if (s.preset != null) this._slots[actual] = {...s, preset: null, temperature: PRESET_TEMP_APPROX[s.preset] ?? 20};
      else this._slots[actual] = {...s, temperature: null, preset: "home"};
      this._render();
    }
  }

  _save() {
    if (!this._day || !this._zone) return;
    const finalSlots = this._sortedSlots();
    const targetDays = [this._day, ...this._copyDays];
    this.dispatchEvent(new CustomEvent("hestia-save-schedule",
      { detail: { zone_id: this._zone.zone_id, days: targetDays, slots: finalSlots }, bubbles: true, composed: true }));
    this.close();
  }
}
customElements.define("hestia-schedule-editor", HestiaScheduleEditor);

// ---- Main card ----

class HestiaScheduleCard extends HTMLElement {
  setConfig(config) { this._config = config; if (!this._hass) this._renderSkeleton(); }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._loadZones();
    else if (this._zones) this._refreshTodayRow();
  }

  connectedCallback() { this._subscribe(); }
  disconnectedCallback() { if (this._unsub) { this._unsub(); this._unsub = null; } }

  async _loadZones() {
    if (!this._hass) return;
    try {
      let zones = await this._hass.callWS({ type: `${DOMAIN}/zones` });
      if (this._config?.zone_id) {
        zones = zones.filter(z => z.zone_id === this._config.zone_id);
      }
      this._zones = zones;
      if (!this._activeZoneId || !this._zones.find(z => z.zone_id === this._activeZoneId)) {
        this._activeZoneId = this._config?.default_zone ?? this._zones[0]?.zone_id ?? null;
      }
      this._render();
    } catch(e) { console.error("Hestia Scheduler: load error", e); }
  }

  async _subscribe() {
    if (!this._hass || this._unsub) return;
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        () => this._loadZones(),
        { type: `${DOMAIN}/subscribe` }
      );
    } catch {}
  }

  _renderSkeleton() {
    this.innerHTML = `<ha-card><div style="padding:16px;color:var(--secondary-text-color)">Loading Hestia Scheduler…</div></ha-card>`;
  }

  _render() {
    const zones = this._zones ?? [];
    const zone = zones.find(z => z.zone_id === this._activeZoneId) ?? zones[0];
    const today = todayWeekday();
    const multiZone = zones.length > 1;

    const tabsHtml = zones.map(z => {
      const active = z.zone_id === this._activeZoneId;
      const tempColor = active ? "rgba(255,255,255,.8)" : "var(--secondary-text-color)";
      const tempStr = (this._config?.show_current_temp !== false && z.current_temperature != null)
        ? `<ha-icon icon="mdi:thermometer" style="--mdc-icon-size:16px;vertical-align:middle;color:${tempColor};"></ha-icon> <span style="font-size:.85em;color:${tempColor};">${z.current_temperature.toFixed(1)}°C</span>` : "";
      const disabledBadge = !z.enabled
        ? `<span style="font-size:.65em;color:var(--error-color,#ef5350);font-weight:600;text-transform:uppercase;letter-spacing:.03em;">off</span>` : "";
      const curPreset = activePresetNow(z);
      const presetIconColor = active ? "rgba(255,255,255,.85)" : "var(--secondary-text-color)";
      const curPresetIcon = curPreset ? presetIcon(curPreset, 16, presetIconColor) : "";

      if (!multiZone) {
        return `<div style="display:inline-flex;align-items:center;gap:6px;padding:4px 2px;font-size:.92em;font-weight:600;color:var(--primary-text-color);">${curPresetIcon} ${z.name} ${disabledBadge} ${tempStr}</div>`;
      }
      const activeStyle = active
        ? "font-weight:700;color:var(--primary-text-color);background:var(--primary-color,#03a9f4);color:#fff;border-radius:8px;"
        : "font-weight:400;color:var(--secondary-text-color);background:none;border-radius:8px;";
      const opacity = !z.enabled ? "opacity:.55;" : "";
      return `<button data-zone="${z.zone_id}" style="display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border:none;${activeStyle}${opacity}cursor:pointer;font-size:.92em;white-space:nowrap;transition:background .15s,color .15s;">${curPresetIcon} ${z.name} ${disabledBadge} ${tempStr}</button>`;
    }).join("");

    const tickHtml = Array.from({length: 25}, (_, h) => {
      const pct = (h / 24 * 100).toFixed(2);
      return `<span style="position:absolute;left:${pct}%;transform:translateX(-50%);font-size:.58em;color:var(--secondary-text-color);user-select:none;bottom:1px;">${h}</span>`;
    }).join("");
    const axisRow = `
      <div style="display:flex;align-items:flex-end;gap:${ROW_GAP}px;padding:1px 0;">
        <span style="width:${LABEL_W}px;flex-shrink:0;"></span>
        <div style="width:${MARKER_W}px;flex-shrink:0;"></div>
        <div style="flex:1;min-width:0;position:relative;height:16px;">${tickHtml}</div>
      </div>`;

    const rowsHtml = WEEKDAYS.map(day =>
      `<hestia-day-row data-day="${day}" data-today="${day===today}"></hestia-day-row>`
    ).join("");

    const disabledLabel = zone && !zone.enabled
      ? `<span style="font-size:.78em;color:var(--error-color,#ef5350);font-weight:600;margin-right:auto;">Schedule disabled</span>` : "";
    const enableBtn = zone
      ? `<button data-zone="${zone.zone_id}" data-enabled="${zone.enabled}" style="font-size:.8em;padding:4px 12px;border:1px solid var(--divider-color,#555);border-radius:6px;background:none;cursor:pointer;color:var(--primary-text-color);">${zone.enabled?"Disable zone":"Enable zone"}</button>`
      : "";

    this.innerHTML = zones.length === 0
      ? `<ha-card><div style="padding:24px;text-align:center;color:var(--secondary-text-color)">No zones configured. Add zones via the integration settings.</div></ha-card>
         <hestia-schedule-editor id="editor"></hestia-schedule-editor>`
      : `<ha-card>
          <div style="padding:12px 16px 0;font-size:1.1em;font-weight:600;">Heating Schedule</div>
          <div style="display:flex;gap:4px;padding:8px 16px 6px;overflow-x:auto;scrollbar-width:none;">${tabsHtml}</div>
          <div style="padding:2px 16px 0;">${axisRow}</div>
          <div id="week-grid" style="display:flex;flex-direction:column;gap:1px;padding:2px 16px 10px;position:relative;">
            ${rowsHtml}
            <div id="hover-cursor" style="position:absolute;top:2px;bottom:10px;width:1px;background:rgba(255,255,255,0.4);pointer-events:none;z-index:20;display:none;">
              <div id="hover-label" style="position:absolute;top:2px;left:50%;transform:translateX(-50%);font-size:.6em;color:rgba(255,255,255,0.9);background:rgba(0,0,0,0.7);padding:1px 5px;border-radius:3px;white-space:nowrap;"></div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:0 16px 12px;">${disabledLabel}${enableBtn}</div>
        </ha-card>
        <hestia-schedule-editor id="editor"></hestia-schedule-editor>`;

    if (zone) {
      const climateState = this._hass?.states?.[zone.climate_entity];
      const heating = climateState?.attributes?.hvac_action === "heating";
      this.querySelectorAll("hestia-day-row").forEach(el => {
        el.zone = zone;
        el.day = el.dataset.day;
        el.isToday = el.dataset.today === "true";
        el.disabled = !zone.enabled;
        el.preheat = this._buildPreheatInfo(zone, el.dataset.day);
        if (el.dataset.today === "true") el.heatingOn = heating;
      });
    }

    this._bindEvents();
  }

  _refreshTodayRow() {
    const zones = this._zones ?? [];
    const zone = zones.find(z => z.zone_id === this._activeZoneId) ?? zones[0];
    if (!zone) return;
    const today = todayWeekday();
    const row = this.querySelector(`hestia-day-row[data-day="${today}"]`);
    if (!row) return;
    row.preheat = this._buildPreheatInfo(zone, today);
    const climateState = this._hass?.states?.[zone.climate_entity];
    row.heatingOn = climateState?.attributes?.hvac_action === "heating";
  }

  _findPreheatState(zoneId) {
    if (!this._hass) return null;
    for (const [eid, s] of Object.entries(this._hass.states)) {
      if (eid.startsWith("sensor.") && eid.includes(zoneId) && eid.endsWith("_pre_heat")
          && s.attributes.preset_temp_cache !== undefined) {
        return s;
      }
    }
    return null;
  }

  _buildPreheatGradient(zone, day, startTimeStr, nextSlotTime, isLive, storageKey) {
    let startMins;
    try { const d = new Date(startTimeStr); startMins = d.getHours() * 60 + d.getMinutes(); } catch {}
    if (startMins === undefined) return null;

    const nextSlotMins = timeToMinutes(nextSlotTime);
    const slots = zone.days[day] ?? [];
    const sorted = [...slots].sort((a, b) => timeToMinutes(a.time) - timeToMinutes(b.time));
    let curSlot = sorted[0], nxtSlot = sorted[0];
    for (let i = 0; i < sorted.length; i++) {
      if (timeToMinutes(sorted[i].time) <= startMins) curSlot = sorted[i];
      if (timeToMinutes(sorted[i].time) === nextSlotMins) nxtSlot = sorted[i];
    }
    const info = {
      active: true, isLive,
      startMinutes: startMins, nextSlotMinutes: nextSlotMins,
      currentSlotColor: slotColor(curSlot), nextSlotColor: slotColor(nxtSlot),
    };
    if (storageKey) {
      try { localStorage.setItem(storageKey, JSON.stringify({
        startMinutes: startMins, nextSlotMinutes: nextSlotMins,
        currentSlotColor: info.currentSlotColor, nextSlotColor: info.nextSlotColor,
      })); } catch {}
    }
    return info;
  }

  _buildPreheatInfo(zone, day) {
    const storageKey = `hestia_ph_${zone.zone_id}_${day}`;
    const today = todayWeekday();

    if (day === today) {
      const s = this._findPreheatState(zone.zone_id);
      if (s) {
        if (s.attributes.preheating === true) {
          const nextSlotTime = s.attributes.next_slot_time;
          const startTime = s.attributes.preheat_started_at;
          if (nextSlotTime && startTime) {
            return this._buildPreheatGradient(zone, day, startTime, nextSlotTime, true, storageKey);
          }
        }

        const lastStart = s.attributes.last_preheat_started_at;
        const lastSlotTime = s.attributes.last_preheat_next_slot_time;
        if (lastStart && lastSlotTime) {
          const grad = this._buildPreheatGradient(zone, day, lastStart, lastSlotTime, false, storageKey);
          if (grad) return grad;
        }
      }
    }

    try {
      const saved = localStorage.getItem(storageKey);
      if (saved) {
        const d = JSON.parse(saved);
        return { active: true, isLive: false, startMinutes: d.startMinutes, nextSlotMinutes: d.nextSlotMinutes,
          currentSlotColor: d.currentSlotColor, nextSlotColor: d.nextSlotColor };
      }
    } catch {}
    return null;
  }

  _bindEvents() {
    this.querySelectorAll("button[data-zone]").forEach(btn => {
      btn.addEventListener("click", () => {
        if (btn.dataset.enabled !== undefined) {
          const zoneId = btn.dataset.zone;
          const cur = btn.dataset.enabled === "true";
          this._hass.callWS({ type: `${DOMAIN}/enable_zone`, zone_id: zoneId, enabled: !cur });
        } else {
          this._activeZoneId = btn.dataset.zone;
          this._render();
        }
      });
    });

    this.addEventListener("hestia-edit-day", e => {
      const zone = (this._zones??[]).find(z => z.zone_id === this._activeZoneId);
      if (!zone) return;
      this.querySelector("#editor")?.open(zone, e.detail.day);
    });

    this.addEventListener("hestia-save-schedule", async e => {
      const { zone_id, days, slots } = e.detail;
      for (const day of days) {
        try {
          localStorage.removeItem(`hestia_ph_${zone_id}_${day}`);
          await this._hass.callWS({ type: `${DOMAIN}/update_schedule`, zone_id, day, slots });
        } catch(err) { console.error("Save failed", err); }
      }
    });

    const weekGrid = this.querySelector('#week-grid');
    const cursor = this.querySelector('#hover-cursor');
    const hoverLabel = this.querySelector('#hover-label');
    if (weekGrid && cursor && hoverLabel) {
      weekGrid.addEventListener('mousemove', e => {
        const tl = weekGrid.querySelector('.tl');
        if (!tl) return;
        const tlRect = tl.getBoundingClientRect();
        const gridRect = weekGrid.getBoundingClientRect();
        const x = e.clientX - tlRect.left;
        if (x < 0 || x > tlRect.width) { cursor.style.display = 'none'; return; }
        const mins = Math.round(x / tlRect.width * TOTAL_MINS);
        const h = Math.floor(mins / 60);
        const m = mins % 60;
        cursor.style.display = 'block';
        cursor.style.left = (tlRect.left - gridRect.left + x) + 'px';
        hoverLabel.textContent = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
      });
      weekGrid.addEventListener('mouseleave', () => { cursor.style.display = 'none'; });
    }
  }

  static getStubConfig() { return { type: "custom:hestia-schedule-card" }; }
}

customElements.define("hestia-schedule-card", HestiaScheduleCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "hestia-schedule-card",
  name: "Hestia Schedule Card",
  description: "Nest-style weekly heating schedule with learning thermostat",
  preview: true,
});
