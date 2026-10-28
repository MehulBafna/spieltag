/* Spieltag dashboard. Reads four small JSON files and draws everything with the DOM and SVG.
   No libraries, no external requests, no inline styles or scripts, so a strict
   Content-Security-Policy works. Text from data is always inserted with textContent. */
(() => {
  "use strict";

  const PRIMARY = "elo_ordered_logit";
  const SECOND = "feature_logit";
  const REFERENCE = "climatology";
  const MODEL_NAMES = {
    elo_ordered_logit: "Elo ratings",
    feature_logit: "Form and rest",
    climatology: "League averages",
  };
  const OUTCOME_TEXT = { home: "Home win", draw: "Draw", away: "Away win" };
  const STALE_AFTER_MS = 3 * 3600 * 1000;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TZ = "Europe/Berlin";

  const $ = (id) => document.getElementById(id);
  const modelName = (key) => MODEL_NAMES[key] || key;

  /* ---------- small helpers ---------- */

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function clear(node) {
    node.replaceChildren();
  }

  const fmtDay = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, weekday: "short", day: "numeric", month: "short",
  });
  const fmtTime = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const fmtWeekday = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short" });
  const weekdayTime = (iso) => `${fmtWeekday.format(new Date(iso))} ${fmtTime.format(new Date(iso))}`;
  const day = (iso) => fmtDay.format(new Date(iso));
  const time = (iso) => fmtTime.format(new Date(iso));
  const dayTime = (iso) => `${day(iso)}, ${time(iso)}`;
  const int = (n) => Number(n).toLocaleString("en-GB");
  const signed = (x, digits = 1) => `${x < 0 ? "−" : ""}${Math.abs(x).toFixed(digits)}%`;

  function ago(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 2) return "just now";
    if (minutes < 90) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  /* Round three probabilities to whole percents that add up to 100. */
  function percents(values) {
    const raw = values.map((v) => v * 100);
    const floors = raw.map(Math.floor);
    let missing = 100 - floors.reduce((a, b) => a + b, 0);
    const order = raw.map((v, i) => [v - Math.floor(v), i]).sort((a, b) => b[0] - a[0]);
    for (const [, i] of order) {
      if (missing <= 0) break;
      floors[i] += 1;
      missing -= 1;
    }
    return floors;
  }

  async function load(name) {
    try {
      const response = await fetch(`data/${name}`, { cache: "no-cache" });
      return response.ok ? await response.json() : null;
    } catch {
      return null;
    }
  }

  /* ---------- tooltip: one element, used by every chart and bar ---------- */

  const tip = $("tip");

  function showTip(fill, pointer, target) {
    clear(tip);
    fill(tip);
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    let x, y;
    if (pointer) {
      x = pointer.clientX + 14;
      y = pointer.clientY + 16;
    } else {
      const rect = target.getBoundingClientRect();
      x = rect.left;
      y = rect.bottom + 8;
    }
    x = Math.min(Math.max(8, x), window.innerWidth - box.width - 8);
    if (y + box.height > window.innerHeight - 8) y = Math.max(8, (pointer ? pointer.clientY : y) - box.height - 16);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  const hideTip = () => {
    tip.hidden = true;
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTip();
  });

  function attachTip(node, fill) {
    node.addEventListener("pointermove", (event) => showTip(fill, event, node));
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("focus", () => showTip(fill, null, node));
    node.addEventListener("blur", hideTip);
  }

  const tipTitle = (parent, text) => parent.append(el("div", { class: "tip-title", text }));
  function tipRow(parent, label, value, color) {
    const left = el("span");
    if (color) {
      const key = el("span", { class: "tip-key" });
      key.style.background = color;
      left.append(key);
    }
    left.append(document.createTextNode(label));
    parent.append(el("div", { class: "tip-row" }, left, el("b", { text: value })));
  }

  /* ---------- health line and hero ---------- */

  const ICONS = {
    ok: "M3 8.5l3.2 3.2L13 4.8",
    warn: "M8 3.5v5.5M8 11.8v.4",
    bad: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  };

  function healthIcon(state) {
    return svg("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true", fill: "none" },
      svg("circle", { cx: 8, cy: 8, r: 7.25, stroke: "currentColor", "stroke-width": 1.5 }),
      svg("path", { d: ICONS[state], stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }

  function renderHealth(status) {
    const node = $("health");
    let state = "bad";
    let text = "No pipeline data yet";
    if (status) {
      const age = Date.now() - new Date(status.generated_at).getTime();
      const problems = [];
      if (status.freeze_error) problems.push("writing forecasts failed");
      if (status.ingest && status.ingest.status === "failed") problems.push("match data unreachable, using stored data");
      if (status.counts && status.counts.missed > 0) problems.push(`${status.counts.missed} forecast${status.counts.missed === 1 ? "" : "s"} missed`);
      if (age > STALE_AFTER_MS) {
        state = "bad";
        text = `Out of date, last update ${ago(status.generated_at)}`;
      } else if (problems.length || !status.healthy) {
        state = "warn";
        text = `Needs attention: ${problems[0] || "see the status file"}`;
      } else {
        state = "ok";
        text = `Pipeline running, updated ${ago(status.generated_at)}`;
      }
    }
    node.dataset.state = state;
    clear(node);
    node.append(healthIcon(state), document.createTextNode(text));
  }

  function renderHero(backtest, live) {
    const box = $("hero-figure");
    clear(box);
    const fig = el("div", { class: "fig" });
    const primary = backtest && backtest.models && backtest.models[PRIMARY];
    if (primary && primary.rps_skill_vs_reference != null) {
      fig.append(
        el("p", { class: "fig-value", text: `${(primary.rps_skill_vs_reference * 100).toFixed(1)}%` }),
        el("p", { class: "fig-label", text: `lower error than league averages across ${int(primary.matches)} past matches` }));
    } else {
      fig.append(
        el("p", { class: "fig-value", text: "–" }),
        el("p", { class: "fig-label", text: "Backtest results appear here once they have been calculated" }));
    }

    const graded = (live && live.matches_scored) || 0;
    const needed = (live && live.min_matches_for_verdict) || 30;
    const meter = el("div", {
      class: "meter", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": needed,
      "aria-valuenow": Math.min(graded, needed), "aria-label": "Live matches graded",
    });
    const fill = el("div", { class: "meter-fill" });
    fill.style.width = "0%";
    meter.append(fill);
    const label = graded >= needed
      ? `Live: ${int(graded)} matches graded`
      : `Live: ${int(graded)} of ${needed} matches graded for a first verdict`;
    fig.append(meter, el("p", { class: "meter-label", text: label }));
    box.append(fig);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      fill.style.width = `${Math.min(100, (graded / needed) * 100)}%`;
    }));
  }

  /* ---------- probability bar ---------- */

  const lockIcon = () =>
    svg("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true", fill: "none" },
      svg("rect", { x: 2.5, y: 6.25, width: 9, height: 6, rx: 1.5, stroke: "currentColor", "stroke-width": 1.4 }),
      svg("path", { d: "M4.75 6.25V4.5a2.25 2.25 0 014.5 0v1.75", stroke: "currentColor", "stroke-width": 1.4 }));

  function probabilityBar(p, happened) {
    const values = [p.p_home, p.p_draw, p.p_away];
    const bar = el("div", { class: `bar${happened ? " faded" : ""}`, "aria-hidden": "true" });
    ["h", "d", "a"].forEach((cls, i) => {
      const seg = el("i", { class: cls });
      seg.style.flex = `${Math.max(values[i], 0.005)} 1 0`;
      if (happened && happened === ["home", "draw", "away"][i]) seg.classList.add("happened");
      bar.append(seg);
    });
    const pc = percents(values);
    const line = el("div", { class: "pcs", "aria-hidden": "true" });
    ["home", "draw", "away"].forEach((outcome, i) => {
      line.append(el("span", {}, el("i", { class: `swatch ${outcome}` }), document.createTextNode(`${pc[i]}%`)));
    });
    return [bar, line, pc];
  }

  function forecastCell(models, title, sealedText, happened, options = {}) {
    const primary = models[PRIMARY] || Object.values(models)[0];
    const [bar, line, pc] = probabilityBar(primary, happened);
    const wrap = el("div", {
      class: "bar-wrap", tabindex: 0, role: "group",
      "aria-label": `${title}. Home win ${pc[0]} percent, draw ${pc[1]} percent, away win ${pc[2]} percent.`,
    });
    wrap.append(bar, line);
    attachTip(wrap, (box) => {
      tipTitle(box, title);
      for (const key of [PRIMARY, SECOND, REFERENCE]) {
        const m = models[key];
        if (!m) continue;
        const r = percents([m.p_home, m.p_draw, m.p_away]);
        tipRow(box, modelName(key), `${r[0]} / ${r[1]} / ${r[2]}`);
      }
      if (sealedText) box.append(el("div", { text: sealedText }));
      if (options.note) box.append(el("div", { text: options.note }));
    });
    return wrap;
  }

  /* ---------- next fixtures ---------- */

  function renderFixtures(upcoming) {
    const box = $("fixtures");
    clear(box);
    if (!upcoming || !upcoming.fixtures || !upcoming.fixtures.length) {
      box.append(el("p", { class: "empty", text: upcoming
        ? "No fixtures are scheduled right now. The season may be over, or the next one has not been published yet."
        : "The fixtures could not be loaded. Reload the page in a minute." }));
      return;
    }
    let currentRound = null;
    for (const f of upcoming.fixtures) {
      if (f.matchday !== currentRound) {
        currentRound = f.matchday;
        box.append(el("h3", { class: "round-title", text: `Matchday ${f.matchday}` }));
      }
      const row = el("div", { class: "row" });
      const when = el("div", { class: "when" },
        el("b", { text: day(f.kickoff_utc) }), document.createTextNode(`${time(f.kickoff_utc)} Berlin`));

      const teams = el("div", { class: "teams" }, el("span", { text: f.home_team }), el("span", { text: f.away_team }));
      const title = `${f.home_team} against ${f.away_team}`;
      const forecast = el("div", { class: "forecast" });
      const seal = el("div", { class: "seal" });
      if (f.status === "frozen") {
        forecast.append(forecastCell(f.models, title, `Sealed ${dayTime(f.frozen_at_utc)} Berlin time`, null));
        seal.append(lockIcon(), el("span", { text: `Sealed ${weekdayTime(f.frozen_at_utc)}` }));
      } else {
        forecast.append(el("div", { class: "unsealed", role: "img", "aria-label": `${title}: forecast not sealed yet` }));
        seal.append(lockIcon(), el("span", { text: f.freezes_at_utc ? `Seals ${weekdayTime(f.freezes_at_utc)}` : "Not sealed yet" }));
      }
      row.append(when, teams, forecast, seal);
      box.append(row);
    }
  }

  /* ---------- latest graded matches ---------- */

  function renderRecent(live) {
    const box = $("recent");
    clear(box);
    if (!live || !live.recent || !live.recent.length) {
      box.append(el("p", { class: "empty", text: "No forecast has been graded yet. The first ones appear after the final whistle of a match whose forecast was sealed before kickoff." }));
      return;
    }
    for (const m of live.recent) {
      const when = el("div", { class: "when" },
        el("b", { text: day(m.kickoff_utc) }), document.createTextNode(`Matchday ${m.matchday}`));
      const teams = el("div", { class: "teams" },
        el("span", {}, document.createTextNode(m.home_team), el("span", { class: "score", text: `${m.home_goals}:${m.away_goals}` })),
        el("span", { text: m.away_team }));
      const title = `${m.home_team} ${m.home_goals}:${m.away_goals} ${m.away_team}`;
      const forecast = el("div", { class: "forecast" }, forecastCell(
        { [PRIMARY]: { p_home: m.p_home, p_draw: m.p_draw, p_away: m.p_away } },
        title, null, m.outcome,
        { note: `Forecast error ${m.rps.toFixed(3)}. Lower is better.` }));
      const verdict = el("div", { class: "verdict" },
        el("b", { text: OUTCOME_TEXT[m.outcome] }), document.createTextNode(`Error ${m.rps.toFixed(3)}`));
      box.append(el("div", { class: "row" }, when, teams, forecast, verdict));
    }
  }

  /* ---------- charts ---------- */

  function niceStep(span, target) {
    const raw = span / target;
    const power = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / power;
    return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * power;
  }

  function niceTicks(lo, hi, target = 5) {
    const step = niceStep(hi - lo, target);
    const ticks = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) ticks.push(Math.round(t / step) * step);
    return { ticks, step };
  }

  function whenResized(node, draw) {
    let width = 0;
    const run = () => {
      const next = Math.round(node.clientWidth);
      if (next && next !== width) {
        width = next;
        draw(width);
      }
    };
    new ResizeObserver(run).observe(node);
    run();
  }

  const plotEmpty = (node, text) => {
    clear(node);
    node.append(el("p", { class: "plot-empty", text }));
  };

  /* Improvement over the reference, from a paired RPS difference and the reference's own RPS. */
  function skillRow(label, pair, referenceRps, matches, state) {
    if (!pair || !referenceRps || !(matches >= 2)) return { label, sub: "No graded matches yet", state: "none" };
    return {
      label, state, matches,
      mean: (-pair.mean / referenceRps) * 100,
      low: (-pair.high / referenceRps) * 100,
      high: (-pair.low / referenceRps) * 100,
      sub: state === "early" ? `${int(matches)} matches, too early to call` : `${int(matches)} matches`,
    };
  }

  function forestRows(backtest, live) {
    const rows = [];
    if (backtest && backtest.models && backtest.models[REFERENCE] && backtest.models[PRIMARY]) {
      const paired = (backtest.paired_with_best || {})[`${PRIMARY}_minus_${REFERENCE}`];
      const ref = backtest.models[REFERENCE].rps;
      const own = backtest.models[PRIMARY];
      if (paired) {
        rows.push(skillRow("Backtest",
          { mean: paired.mean_rps_difference, low: paired.ci95_low, high: paired.ci95_high },
          ref, own.matches, "ok"));
      } else {
        const s = own.rps_skill_vs_reference * 100;
        rows.push({ label: "Backtest", sub: `${int(own.matches)} matches`, state: "ok", matches: own.matches, mean: s, low: s, high: s });
      }
    } else {
      rows.push({ label: "Backtest", sub: "Not calculated yet", state: "none" });
    }
    const pair = live && live.primary_vs_reference;
    if (pair && live.models && live.models[REFERENCE]) {
      rows.push(skillRow("Live",
        { mean: pair.mean_rps_difference, low: pair.ci95_low, high: pair.ci95_high },
        live.models[REFERENCE].rps, pair.matches, pair.verdict === "too_early" ? "early" : "ok"));
    } else {
      rows.push({ label: "Live", sub: "No graded matches yet", state: "none" });
    }
    return rows;
  }

  function drawForest(node, rows, width) {
    clear(node);
    const narrow = width < 560;
    const labelCol = narrow ? 0 : 150;
    const right = 64;
    const left = labelCol;
    const rowH = narrow ? 76 : 64;
    const top = 8;
    const axisH = 52;
    const height = top + rowH * rows.length + axisH;
    const plotW = Math.max(120, width - left - right);

    const marks = rows.filter((r) => r.state !== "none");
    const lows = marks.map((r) => r.low);
    const highs = marks.map((r) => r.high);
    let lo = Math.min(0, ...lows, -2);
    let hi = Math.max(5, ...highs);
    const pad = (hi - lo) * 0.06;
    lo -= pad;
    hi += pad;
    const { ticks } = niceTicks(lo, hi, narrow ? 4 : 6);
    const x = (v) => left + ((v - lo) / (hi - lo)) * plotW;

    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "Improvement over league averages, backtest and live, with 95 percent intervals" });
    for (const t of ticks) {
      root.append(svg("line", { class: t === 0 ? "zero" : "grid", x1: x(t), x2: x(t), y1: top, y2: top + rowH * rows.length }));
      root.append(svg("text", { class: "tick-label", x: x(t), y: top + rowH * rows.length + 18, "text-anchor": "middle", text: t === 0 ? "0%" : signed(t, 0) }));
    }
    const axisY = top + rowH * rows.length + 40;
    root.append(svg("text", { x: x(lo) , y: axisY, text: "← worse than league averages" }));
    root.append(svg("text", { x: left + plotW, y: axisY, "text-anchor": "end", text: "better →" }));

    rows.forEach((row, i) => {
      const cy = top + rowH * i + rowH / 2 + (narrow ? 12 : 0);
      const labelY = narrow ? top + rowH * i + 18 : cy - 4;
      const halo = narrow ? " halo" : "";
      root.append(svg("text", { class: `row-label${halo}`, x: 0, y: labelY, text: row.label }));
      root.append(svg("text", { class: halo.trim(), x: 0, y: labelY + 16, text: row.sub }));
      const group = svg("g", { tabindex: 0, role: "group", "aria-label": rowSummary(row) });
      if (row.state !== "none") {
        const early = row.state === "early";
        const color = early ? "var(--muted)" : "var(--model-primary)";
        if (row.high > row.low) {
          group.append(svg("line", { class: "interval", x1: x(row.low), x2: x(row.high), y1: cy, y2: cy, stroke: color }));
        }
        group.append(svg("circle", { class: "ring", cx: x(row.mean), cy, r: 6, fill: early ? "var(--paper)" : color, stroke: early ? color : "var(--paper)", "stroke-width": early ? 2 : 2 }));
        group.append(svg("text", { class: "value-label", x: Math.min(x(Math.max(row.high, row.mean)) + 12, width - 4), y: cy + 4, "text-anchor": x(Math.max(row.high, row.mean)) + 60 > width ? "end" : "start", text: signed(row.mean) }));
      }
      group.append(svg("rect", { class: "hit", x: 0, y: top + rowH * i, width, height: rowH }));
      attachTip(group, (box) => {
        tipTitle(box, row.label);
        if (row.state === "none") { box.append(el("div", { text: row.sub })); return; }
        tipRow(box, "Improvement", signed(row.mean));
        if (row.high > row.low) tipRow(box, "95% interval", `${signed(row.low)} to ${signed(row.high)}`);
        tipRow(box, "Matches", int(row.matches));
        if (row.state === "early") box.append(el("div", { text: "Too early to call." }));
      });
      root.append(group);
    });
    node.append(root);
  }

  function rowSummary(row) {
    if (row.state === "none") return `${row.label}: ${row.sub}`;
    return `${row.label}: improvement ${signed(row.mean)}, 95 percent interval ${signed(row.low)} to ${signed(row.high)}, ${row.matches} matches`;
  }

  function renderForest(backtest, live) {
    const rows = forestRows(backtest, live);
    const node = $("forest");
    whenResized(node, (w) => drawForest(node, rows, w));
    const verdict = live && live.primary_vs_reference ? live.primary_vs_reference.verdict : null;
    const messages = {
      too_early: "The live record is too short to call. A first verdict needs 30 graded matches.",
      better: "The live forecasts are clearly better than league averages on these matches.",
      worse: "The live forecasts are clearly worse than league averages on these matches.",
      not_distinguishable: "On the live matches so far, the data cannot tell the model from league averages.",
    };
    if (verdict && messages[verdict]) {
      $("forest-chart").querySelector("figcaption").append(el("span", { text: messages[verdict] }));
    }
  }

  function renderModelTable(backtest, live) {
    const box = $("model-table");
    clear(box);
    const keys = [PRIMARY, SECOND, REFERENCE];
    for (const source of [backtest, live]) {
      for (const k of Object.keys((source && source.models) || {})) if (!keys.includes(k)) keys.push(k);
    }
    const cell = (m, field, fmt) => (m && m[field] != null ? fmt(m[field]) : "–");
    const f3 = (v) => v.toFixed(3);
    const pct = (v) => signed(v * 100);
    const head = el("tr", {},
      ...["Model", "Backtest matches", "Backtest error", "Backtest improvement", "Live matches", "Live error", "Live improvement"]
        .map((t) => el("th", { text: t, scope: "col" })));
    const table = el("table", {}, el("caption", { class: "note", text: "Error is the ranked probability score. Lower is better." }), el("thead", {}, head));
    const body = el("tbody");
    for (const k of keys) {
      const b = backtest && backtest.models && backtest.models[k];
      const l = live && live.models && live.models[k];
      if (!b && !l) continue;
      const isRef = k === REFERENCE;
      body.append(el("tr", {},
        el("th", { text: modelName(k), scope: "row" }),
        el("td", { text: cell(b, "matches", int) }), el("td", { text: cell(b, "rps", f3) }),
        el("td", { text: isRef ? "–" : cell(b, "rps_skill_vs_reference", pct) }),
        el("td", { text: cell(l, "matches", int) }), el("td", { text: cell(l, "rps", f3) }),
        el("td", { text: isRef ? "–" : cell(l, "rps_skill_vs_reference", pct) })));
    }
    table.append(body);
    box.append(el("div", { class: "table-scroll" }, table));
  }

  /* calibration: forecast home-win chance against what happened */
  function renderCalibration(backtest) {
    const node = $("calibration");
    const bands = backtest && backtest.calibration_of_best_home_win;
    if (!bands || !bands.length) {
      plotEmpty(node, "The calibration chart appears once the backtest has been calculated.");
      return;
    }
    const best = modelName(backtest.best_model);
    $("calibration-title").textContent = `Calibration of ${best} in the backtest`;
    const table = $("calibration-table");
    clear(table);
    table.append(el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["Forecast band", "Matches", "Mean forecast", "Home wins"].map((t) => el("th", { text: t, scope: "col" })))),
      el("tbody", {}, ...bands.map((b) => el("tr", {},
        el("th", { text: b.band, scope: "row" }), el("td", { text: int(b.matches) }),
        el("td", { text: `${(b.forecast * 100).toFixed(1)}%` }), el("td", { text: `${(b.observed * 100).toFixed(1)}%` })))))));

    whenResized(node, (width) => {
      clear(node);
      const size = Math.min(width, 400);
      const m = { l: 60, r: 12, t: 8, b: 44 };
      const plot = size - m.l - m.r;
      const sx = (v) => m.l + v * plot;
      const sy = (v) => m.t + (1 - v) * plot;
      const root = svg("svg", { viewBox: `0 0 ${size} ${size}`, role: "img", "aria-label": "Forecast home-win chance against share of home wins, by band" });
      for (const t of [0, 0.2, 0.4, 0.6, 0.8, 1]) {
        root.append(svg("line", { class: "grid", x1: sx(0), x2: sx(1), y1: sy(t), y2: sy(t) }));
        root.append(svg("line", { class: "grid", x1: sx(t), x2: sx(t), y1: sy(0), y2: sy(1) }));
        root.append(svg("text", { class: "tick-label", x: sx(t), y: sy(0) + 16, "text-anchor": "middle", text: `${Math.round(t * 100)}%` }));
        root.append(svg("text", { class: "tick-label", x: m.l - 8, y: sy(t) + 4, "text-anchor": "end", text: `${Math.round(t * 100)}%` }));
      }
      root.append(svg("line", { class: "diag", x1: sx(0), y1: sy(0), x2: sx(1), y2: sy(1) }));
      root.append(svg("text", { x: sx(0.5), y: sy(0) + 36, "text-anchor": "middle", text: "Forecast chance of a home win" }));
      const yTitle = svg("text", { x: 0, y: 0, "text-anchor": "middle", transform: `translate(12 ${sy(0.5)}) rotate(-90)`, text: "Share of home wins" });
      root.append(yTitle);
      for (const b of bands) {
        const group = svg("g", { tabindex: 0, role: "img", "aria-label": `Forecast ${(b.forecast * 100).toFixed(0)} percent, home won ${(b.observed * 100).toFixed(0)} percent, ${b.matches} matches` });
        group.append(svg("circle", { class: "ring fill-home", cx: sx(b.forecast), cy: sy(b.observed), r: 5 }));
        group.append(svg("circle", { class: "hit", cx: sx(b.forecast), cy: sy(b.observed), r: 14 }));
        attachTip(group, (box) => {
          tipTitle(box, `Forecast band ${b.band}`);
          tipRow(box, "Mean forecast", `${(b.forecast * 100).toFixed(1)}%`);
          tipRow(box, "Home wins", `${(b.observed * 100).toFixed(1)}%`);
          tipRow(box, "Matches", int(b.matches));
        });
        root.append(group);
      }
      node.append(root);
    });
  }

  /* live trend: running improvement after each matchday */
  function renderTrend(live) {
    const node = $("trend");
    const trend = (live && live.trend) || [];
    const tableBox = $("trend-table");
    clear(tableBox);
    if (trend.length < 2) {
      plotEmpty(node, trend.length === 1
        ? "One matchday has been graded. The trend line appears after the second."
        : "No matchday has been graded yet. The trend line appears after the second.");
      return;
    }
    const series = [PRIMARY, SECOND].filter((k) => trend.every((t) => t.skill && t.skill[k] != null));
    const cls = { [PRIMARY]: "primary", [SECOND]: "second" };

    node.parentElement.querySelectorAll(".legend").forEach((n) => n.remove());
    const legend = el("ul", { class: "legend", "aria-label": "Lines" });
    for (const k of series) legend.append(el("li", {}, el("i", { class: cls[k] }), document.createTextNode(modelName(k))));
    node.before(legend);

    tableBox.append(el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Matchday", scope: "col" }), el("th", { text: "Matches graded", scope: "col" }),
        ...series.map((k) => el("th", { text: modelName(k), scope: "col" })))),
      el("tbody", {}, ...trend.map((t) => el("tr", {}, el("th", { text: `${t.season}/${String(t.matchday).padStart(2, "0")}`, scope: "row" }),
        el("td", { text: int(t.matches) }), ...series.map((k) => el("td", { text: signed(t.skill[k] * 100) }))))))));

    whenResized(node, (width) => {
      clear(node);
      const height = Math.round(Math.min(300, Math.max(220, width * 0.6)));
      const m = { l: 44, r: 56, t: 12, b: 44 };
      const plotW = width - m.l - m.r;
      const plotH = height - m.t - m.b;
      const values = trend.flatMap((t) => series.map((k) => t.skill[k] * 100));
      let lo = Math.min(0, ...values);
      let hi = Math.max(0, ...values);
      const pad = Math.max((hi - lo) * 0.12, 1);
      lo -= pad;
      hi += pad;
      const maxN = trend[trend.length - 1].matches;
      const minN = trend[0].matches;
      const sx = (n) => m.l + (maxN === minN ? 0.5 : (n - minN) / (maxN - minN)) * plotW;
      const sy = (v) => m.t + (1 - (v - lo) / (hi - lo)) * plotH;

      const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Live improvement over league averages after each matchday" });
      const { ticks } = niceTicks(lo, hi, 5);
      for (const t of ticks) {
        root.append(svg("line", { class: t === 0 ? "zero" : "grid", x1: m.l, x2: m.l + plotW, y1: sy(t), y2: sy(t) }));
        root.append(svg("text", { class: "tick-label", x: m.l - 8, y: sy(t) + 4, "text-anchor": "end", text: t === 0 ? "0%" : signed(t, 0) }));
      }
      const xt = niceTicks(minN, maxN, 5).ticks;
      for (const n of xt) root.append(svg("text", { class: "tick-label", x: sx(n), y: m.t + plotH + 18, "text-anchor": "middle", text: String(n) }));
      root.append(svg("text", { x: m.l + plotW / 2, y: height - 4, "text-anchor": "middle", text: "Matches graded" }));
      const allPositive = Math.min(...values) >= 0;
      root.append(svg("text", { x: m.l + 6, y: sy(0) + (allPositive ? 16 : -6), text: "league averages" }));

      for (const k of series) {
        const d = trend.map((t, i) => `${i ? "L" : "M"}${sx(t.matches).toFixed(1)} ${sy(t.skill[k] * 100).toFixed(1)}`).join("");
        root.append(svg("path", { d, fill: "none", class: `line-${cls[k]}`, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      }
      const ends = series.map((k) => ({ k, y: sy(trend[trend.length - 1].skill[k] * 100) })).sort((a, b) => a.y - b.y);
      const crowded = ends.length === 2 && Math.abs(ends[0].y - ends[1].y) < 15;
      for (const e of ends) {
        const last = trend[trend.length - 1];
        root.append(svg("circle", { class: `ring fill-${cls[e.k]}`, cx: sx(last.matches), cy: e.y, r: 5 }));
        if (!crowded) root.append(svg("text", { class: "value-label", x: sx(last.matches) + 10, y: e.y + 4, text: signed(last.skill[e.k] * 100) }));
      }

      const cross = svg("line", { class: "crosshair", y1: m.t, y2: m.t + plotH, x1: 0, x2: 0, visibility: "hidden" });
      const overlay = svg("rect", { class: "hit", x: m.l, y: m.t, width: plotW, height: plotH, tabindex: 0, role: "group", "aria-label": "Trend values. Use the left and right arrow keys to move between matchdays." });
      root.append(cross, overlay);
      let index = trend.length - 1;
      const show = (i, pointer) => {
        index = i;
        const t = trend[i];
        cross.setAttribute("x1", sx(t.matches));
        cross.setAttribute("x2", sx(t.matches));
        cross.setAttribute("visibility", "visible");
        showTip((box) => {
          tipTitle(box, `Matchday ${t.matchday}, ${int(t.matches)} matches graded`);
          for (const k of series) tipRow(box, modelName(k), signed(t.skill[k] * 100), `var(--model-${cls[k]})`);
        }, pointer, overlay);
      };
      const nearest = (event) => {
        const box = root.getBoundingClientRect();
        const px = ((event.clientX - box.left) / box.width) * width;
        let best = 0;
        trend.forEach((t, i) => { if (Math.abs(sx(t.matches) - px) < Math.abs(sx(trend[best].matches) - px)) best = i; });
        return best;
      };
      overlay.addEventListener("pointermove", (event) => show(nearest(event), event));
      overlay.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
      overlay.addEventListener("focus", () => show(index, null));
      overlay.addEventListener("blur", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
      overlay.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft") { event.preventDefault(); show(Math.max(0, index - 1), null); }
        if (event.key === "ArrowRight") { event.preventDefault(); show(Math.min(trend.length - 1, index + 1), null); }
      });
      node.append(root);
    });
  }

  /* ---------- live now: the sealed forecast against the live estimate ---------- */

  const SIDES = ["home", "draw", "away"];
  const SIDE_COLOR = { home: "var(--home)", draw: "var(--draw)", away: "var(--away)" };
  const SIDE_TEXT = { home: "Home", draw: "Draw", away: "Away" };

  function minuteText(p) {
    if (!p) return "";
    if (p.phase === "full_time") return "Full time";
    if (p.phase === "half_time") return "Half time";
    return p.minute >= 90 ? "90+" : `${p.minute}′`;
  }

  const probs = (source, prefix = "p_") => SIDES.map((s) => source[`${prefix}${s}`]);

  function compareRow(label, p, happened) {
    const [bar, line] = probabilityBar({ p_home: p[0], p_draw: p[1], p_away: p[2] }, happened);
    return el("div", { class: "cmp-row" },
      el("span", { class: "cmp-label", text: label }),
      el("div", { class: "cmp-bar" }, bar, line));
  }

  /* Change in whole percentage points, computed from the numbers that are shown. */
  function deltaRow(sealed, live) {
    const a = percents(sealed);
    const b = percents(live);
    const row = el("div", { class: "cmp-row" }, el("span", { class: "cmp-label", text: "Change" }));
    const chips = el("div", { class: "deltas" });
    SIDES.forEach((side, i) => {
      const d = b[i] - a[i];
      const arrow = d > 0 ? "▲" : d < 0 ? "▼" : "–";
      const chip = el("span", { class: `delta ${d > 0 ? "up" : d < 0 ? "down" : "flat"}` },
        el("i", { class: `swatch ${side}` }),
        document.createTextNode(`${SIDE_TEXT[side]} ${arrow} ${Math.abs(d)} pp`));
      chips.append(chip);
    });
    row.append(chips);
    return row;
  }

  function drawLiveChart(node, t, width) {
    clear(node);
    const sealed = probs(t.sealed);
    const pts = [{ minute: 0, p: sealed, home: 0, away: 0, phase: "start" }].concat(
      t.points.map((q) => ({ minute: Math.min(q.minute, 90), p: probs(q), home: q.home, away: q.away, phase: q.phase })));
    const height = Math.round(Math.min(260, Math.max(190, width * 0.42)));
    const m = { l: 40, r: 78, t: 10, b: 34 };
    const plotW = width - m.l - m.r;
    const plotH = height - m.t - m.b;
    const sx = (minute) => m.l + (minute / 90) * plotW;
    const sy = (v) => m.t + (1 - v) * plotH;
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `${t.home_team} against ${t.away_team}: win probability by minute, live line against the sealed forecast` });

    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      root.append(svg("line", { class: "grid", x1: m.l, x2: m.l + plotW, y1: sy(v), y2: sy(v) }));
      root.append(svg("text", { class: "tick-label", x: m.l - 8, y: sy(v) + 4, "text-anchor": "end", text: `${Math.round(v * 100)}%` }));
    }
    for (const minute of [0, 15, 30, 45, 60, 75, 90]) {
      root.append(svg("text", { class: "tick-label", x: sx(minute), y: m.t + plotH + 18, "text-anchor": "middle", text: String(minute) }));
    }
    root.append(svg("line", { class: "zero", x1: sx(45), x2: sx(45), y1: m.t, y2: m.t + plotH, "stroke-dasharray": "2 4" }));

    SIDES.forEach((side, i) => {
      root.append(svg("line", { class: "sealed-line", x1: sx(0), x2: sx(90), y1: sy(sealed[i]), y2: sy(sealed[i]), stroke: SIDE_COLOR[side] }));
    });
    SIDES.forEach((side, i) => {
      const d = pts.map((q, k) => `${k ? "L" : "M"}${sx(q.minute).toFixed(1)} ${sy(q.p[i]).toFixed(1)}`).join("");
      root.append(svg("path", { d, fill: "none", stroke: SIDE_COLOR[side], "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    });

    pts.forEach((q, k) => {
      if (k > 0 && (q.home !== pts[k - 1].home || q.away !== pts[k - 1].away)) {
        root.append(svg("line", { class: "goal-tick", x1: sx(q.minute), x2: sx(q.minute), y1: m.t, y2: m.t + plotH }));
        root.append(svg("text", { class: "goal-label halo", x: sx(q.minute) + 4, y: m.t + 12, text: `${q.home}:${q.away}` }));
      }
    });

    const last = pts[pts.length - 1];
    const ends = SIDES.map((side, i) => ({ side, i, y: sy(last.p[i]) })).sort((a, b) => a.y - b.y);
    for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 14) ends[k].y = ends[k - 1].y + 14;
    const overflow = ends[ends.length - 1].y - (m.t + plotH);
    if (overflow > 0) for (const e of ends) e.y -= overflow;
    const pc = percents(last.p);
    for (const e of ends) {
      root.append(svg("circle", { class: "ring", cx: sx(last.minute), cy: sy(last.p[e.i]), r: 4.5, fill: SIDE_COLOR[e.side] }));
      root.append(svg("text", { class: "value-label", x: m.l + plotW + 10, y: e.y + 4, text: `${SIDE_TEXT[e.side]} ${pc[e.i]}%` }));
    }

    const cross = svg("line", { class: "crosshair", y1: m.t, y2: m.t + plotH, x1: 0, x2: 0, visibility: "hidden" });
    const overlay = svg("rect", { class: "hit", x: m.l, y: m.t, width: plotW, height: plotH, tabindex: 0, role: "group",
      "aria-label": "Probabilities over the match. Use the left and right arrow keys to move between estimates." });
    root.append(cross, overlay);
    let index = pts.length - 1;
    const show = (k, pointer) => {
      index = k;
      const q = pts[k];
      cross.setAttribute("x1", sx(q.minute));
      cross.setAttribute("x2", sx(q.minute));
      cross.setAttribute("visibility", "visible");
      showTip((box) => {
        tipTitle(box, `${q.phase === "start" ? "Kickoff" : minuteText(q)} · ${q.home}:${q.away}`);
        const now = percents(q.p);
        const was = percents(sealed);
        SIDES.forEach((side, i) => tipRow(box, `${SIDE_TEXT[side]} (sealed ${was[i]}%)`, `${now[i]}%`, SIDE_COLOR[side]));
      }, pointer, overlay);
    };
    const nearest = (event) => {
      const box = root.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * width;
      let best = 0;
      pts.forEach((q, k) => { if (Math.abs(sx(q.minute) - px) < Math.abs(sx(pts[best].minute) - px)) best = k; });
      return best;
    };
    overlay.addEventListener("pointermove", (event) => show(nearest(event), event));
    overlay.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    overlay.addEventListener("focus", () => show(index, null));
    overlay.addEventListener("blur", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") { event.preventDefault(); show(Math.max(0, index - 1), null); }
      if (event.key === "ArrowRight") { event.preventDefault(); show(Math.min(pts.length - 1, index + 1), null); }
    });
    node.append(root);
  }

  function liveTable(t) {
    const rows = t.points.map((q) => {
      const r = percents(probs(q));
      return el("tr", {}, el("th", { text: minuteText(q), scope: "row" }), el("td", { text: `${q.home}:${q.away}` }),
        el("td", { text: `${r[0]}%` }), el("td", { text: `${r[1]}%` }), el("td", { text: `${r[2]}%` }));
    });
    return el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["Minute", "Score", "Home", "Draw", "Away"].map((h) => el("th", { text: h, scope: "col" })))),
      el("tbody", {}, ...rows)));
  }

  function liveCard(t) {
    const last = t.points[t.points.length - 1];
    const finished = Boolean(t.final);
    const sealed = probs(t.sealed, "p_");
    const title = `${t.home_team} ${last.home}:${last.away} ${t.away_team}`;
    const card = el("article", { class: `live-card${finished ? " done" : ""}`, "aria-label": title });

    const meta = el("div", { class: "live-meta" },
      el("span", { class: `minute${finished ? "" : " running"}`, text: minuteText(last) }),
      el("span", { text: finished ? `Matchday ${t.matchday}` : `Updated ${time(last.t)}` }));
    card.append(el("div", { class: "live-head" },
      el("div", { class: "live-teams" }, el("span", { text: t.home_team }),
        el("b", { class: "live-score", text: `${last.home}:${last.away}` }), el("span", { text: t.away_team })),
      meta));

    const cmp = el("div", { class: "cmp" });
    cmp.append(compareRow("Sealed before kickoff", sealed, finished ? SIDES[t.final.outcome] : null));
    if (finished) {
      const mid = [...t.points].reverse().find((q) => q.phase !== "full_time" && q.minute <= 75);
      if (mid) {
        cmp.append(compareRow(`Live estimate at ${mid.minute}′`, probs(mid), SIDES[t.final.outcome]));
        cmp.append(deltaRow(sealed, probs(mid)));
      }
    } else {
      cmp.append(compareRow("Live estimate", probs(last), null));
      cmp.append(deltaRow(sealed, probs(last)));
    }
    card.append(cmp);

    if (!finished && last.exp_final_home != null) {
      card.append(el("p", { class: "live-facts", text:
        `Expected final score ${last.exp_final_home.toFixed(1)} : ${last.exp_final_away.toFixed(1)}. Average of all the ways the rest of the match can go.` }));
    }

    const plot = el("div", { class: "plot" });
    card.append(plot);
    whenResized(plot, (w) => drawLiveChart(plot, t, w));
    card.append(el("details", { class: "table-view" }, el("summary", { text: "Show as table" }), liveTable(t)));
    return card;
  }

  function renderLiveStats(data) {
    const box = $("live-stats");
    clear(box);
    const rows = ((data && data.checkpoints) || []).filter((r) => r.matches > 0);
    if (!rows.length) {
      box.append(el("p", { class: "note", text: "When live matches finish, a table here scores the live line against the sealed forecast at minutes 15, 30, 45, 60 and 75." }));
      return;
    }
    const f3 = (v) => (v == null ? "–" : v.toFixed(3));
    const head = ["Minute", "Matches", "Sealed error", "Live error", "Live improvement", "Live better in", "95% interval of the change"];
    const body = rows.map((r) => el("tr", {},
      el("th", { text: `${r.minute}′`, scope: "row" }), el("td", { text: int(r.matches) }),
      el("td", { text: f3(r.rps_sealed) }), el("td", { text: f3(r.rps_live) }),
      el("td", { text: r.gain == null ? "–" : signed(r.gain * 100) }),
      el("td", { text: `${r.live_better} of ${r.matches}` }),
      el("td", { text: r.diff_low == null ? "–" : `${r.diff_low.toFixed(3)} to ${r.diff_high.toFixed(3)}` })));
    const fewest = Math.min(...rows.map((r) => r.matches));
    box.append(el("figure", { class: "chart live-stats" },
      el("figcaption", {},
        el("strong", { text: "Does the live line beat the sealed line?" }),
        el("span", { text: "Average forecast error at fixed minutes, over finished matches. Lower is better. A live line that uses the score should improve the later the minute." }),
        fewest < 20 ? el("span", { text: `Only ${fewest} to ${Math.max(...rows.map((r) => r.matches))} matches so far, so these numbers move a lot.` }) : null),
      el("div", { class: "table-scroll" }, el("table", {},
        el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { text: h, scope: "col" })))),
        el("tbody", {}, ...body)))));
  }

  function renderLive(data) {
    const box = $("live-cards");
    clear(box);
    const matches = (data && data.matches) || [];
    if (!matches.length) {
      box.append(el("p", { class: "empty", text: data
        ? "No match is being played right now. Live estimates appear here during matches whose forecast was sealed."
        : "Live estimates are not switched on, or have not started yet." }));
    } else {
      const ordered = [...matches].sort((a, b) =>
        (Boolean(a.final) - Boolean(b.final)) || (b.kickoff_utc.localeCompare(a.kickoff_utc)));
      for (const t of ordered) box.append(liveCard(t));
    }
    renderLiveStats(data);
    return matches.some((t) => !t.final);
  }

  let liveStamp = null;
  let liveRunning = false;

  async function pollLive() {
    const data = await load("inplay.json");
    const stamp = data ? data.generated_at : "none";
    if (stamp !== liveStamp) {
      liveStamp = stamp;
      liveRunning = renderLive(data);
    }
    setTimeout(pollLive, liveRunning ? 30000 : 120000);
  }

  /* ---------- start ---------- */

  async function start() {
    const [status, live, upcoming, backtest] = await Promise.all(
      ["status.json", "live.json", "upcoming.json", "backtest.json"].map(load));
    renderHealth(status);
    renderHero(backtest, live);
    renderFixtures(upcoming);
    renderForest(backtest, live);
    renderModelTable(backtest, live);
    renderCalibration(backtest);
    renderTrend(live);
    renderRecent(live);
    setInterval(() => renderHealth(status), 60000);
    pollLive();
  }

  start();
})();
