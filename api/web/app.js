/*
 * QueryPilot — the whole client.
 *
 * Vanilla, no framework, no build (resolved Q-A). It renders by the `shape`
 * the server computed, because the classifier that decides is the same one
 * that produced the measurements in `009-frontend.md` §2.3 — two answers to
 * "is this a scalar" that could disagree is the drift the project keeps
 * recording.
 *
 * **Nothing from the server is ever assigned to innerHTML.** Values come from
 * the database and the SQL string is written by a language model; both are
 * data, not markup. Every insertion below goes through textContent or
 * createTextNode.
 */

const form = document.getElementById("ask-form");
const input = document.getElementById("question");
const submit = document.getElementById("submit");
const statusBox = document.getElementById("status");
const resultBox = document.getElementById("result");
const answerBox = document.getElementById("answer");
const sqlBox = document.getElementById("sql");
const tracePanel = document.getElementById("trace-panel");
const traceBox = document.getElementById("trace");
const traceSummary = document.getElementById("trace-summary");
const chartControls = document.getElementById("chart-controls");
const chartToggle = document.getElementById("chart-toggle");
const chartBox = document.getElementById("chart");
const quotaBox = document.getElementById("quota");
const cacheNote = document.getElementById("cache-note");

/* The result currently on screen, so the toggle can redraw without refetching. */
let current = null;

chartToggle.addEventListener("click", () => {
  const showing = !chartBox.hidden;
  chartBox.hidden = showing;
  chartToggle.setAttribute("aria-expanded", String(!showing));
  chartToggle.textContent = showing ? "Show chart" : "Hide chart";
  if (!showing && current) drawChart(current);
});

document.querySelectorAll(".example").forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.textContent.trim();
    form.requestSubmit();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question) return;

  // AC11: the wait is 1-3 seconds and is designed for rather than hidden. The
  // user is waiting on the model, not on us, and saying so is more honest than
  // an indeterminate spinner.
  setBusy(true);
  showStatus("Working — the model is writing SQL and running it…", "busy");
  resultBox.hidden = true;

  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await response.json();
    render(body);
  } catch (err) {
    // A network failure, not an answer. Distinguished from a failed question
    // so the message does not blame the question.
    showStatus("Could not reach QueryPilot. Is the API running?", "error");
  } finally {
    setBusy(false);
    // AC11, and the reason this runs after every question rather than on a
    // timer: the answer that just drained the bucket is the one whose delay
    // needs explaining. A poll on its own schedule would tell the user why
    // they waited at some unrelated moment later on.
    refreshQuota();
  }
});

/*
 * AC10. Asks the server what the provider last said about its limits.
 *
 * Deliberately quiet about failure. This is an explanation, not a feature: if
 * /quota is unreachable the user still gets answers, and an error banner about
 * the *quota reader* would be a worse experience than the silence it replaced.
 */
async function refreshQuota() {
  try {
    const response = await fetch("/quota");
    renderQuota(await response.json());
  } catch (err) {
    quotaBox.hidden = true;
  }
}

function renderQuota(quota) {
  // Shown only when there is something to say. `low` is already false when the
  // server has no reading or when the reading has gone stale -- a warning drawn
  // from a five-minute-old sample of a bucket that refills every minute would
  // describe a state that no longer exists.
  if (!quota || !quota.low || !quota.note) {
    quotaBox.hidden = true;
    quotaBox.textContent = "";
    return;
  }
  quotaBox.hidden = false;
  quotaBox.className = "warn";
  quotaBox.textContent = quota.note;
}

function setBusy(busy) {
  submit.disabled = busy;
  input.disabled = busy;
  submit.textContent = busy ? "Asking…" : "Ask";
}

function showStatus(text, kind) {
  statusBox.hidden = false;
  statusBox.className = kind;
  statusBox.textContent = text;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function resetChart() {
  // Every new answer starts with the chart off (resolved Q-B). Leaving it open
  // across questions would quietly turn the toggle into a preference, which is
  // not what "the human decides, per result" means.
  current = null;
  chartControls.hidden = true;
  chartBox.hidden = true;
  chartToggle.setAttribute("aria-expanded", "false");
  chartToggle.textContent = "Show chart";
  clear(chartBox);
}

/*
 * AC8. Marks an answer that was not computed for this request.
 *
 * `cache_hit` means the provider was not called -- which covers both a stored
 * answer and one shared with a question that arrived at the same moment. The
 * wording says what happened rather than how old the answer is, because for a
 * coalesced request it is seconds old and for a stored one it may not be.
 *
 * **It must not promise that changed data invalidates it.** The key is the
 * question plus the schema and prompt fingerprints, so an added column changes
 * it and an added *row* does not. Telling a user their next question will pick
 * up new data would be a claim the cache cannot honour.
 */
function renderCacheNote(body) {
  if (!body.cache_hit) {
    cacheNote.hidden = true;
    cacheNote.textContent = "";
    return;
  }
  cacheNote.hidden = false;
  cacheNote.textContent =
    "Cached — this answer was reused rather than recomputed, so it reflects " +
    "the data as it was when the question was first answered.";
}

function render(body) {
  resetChart();
  renderCacheNote(body);
  // FastAPI's own 422 for a malformed request has no `ok` field at all.
  if (typeof body.ok !== "boolean") {
    showStatus("That question could not be read. Try rephrasing it.", "error");
    return;
  }

  if (!body.ok) {
    // AC12: a failure shows what failed *and* the SQL that failed, if any. A
    // demo audience learns more from a legible failure than from a spinner
    // that stops.
    showStatus(body.error, "error");
    resultBox.hidden = false;
    clear(answerBox);
    renderSql(body.sql);
    renderTrace(body.trace);
    return;
  }

  statusBox.hidden = true;
  resultBox.hidden = false;
  clear(answerBox);
  answerBox.appendChild(renderAnswer(body));

  // Offered, not drawn.
  if (body.shape === "chartable" && body.series) {
    current = body;
    chartControls.hidden = false;
  }
  renderSql(body.sql);
  renderTrace(body.trace);
}

function renderAnswer(body) {
  switch (body.shape) {
    case "scalar":
      return renderScalar(body);
    case "empty":
      return renderEmpty();
    default:
      // `list`, `table` and `chartable` all render as a table. A chartable
      // result is *also* a table, and the chart is an addition the reader asks
      // for rather than a replacement they are given.
      return renderTable(body);
  }
}

/*
 * The majority case: 28 of 50 corpus questions, 56%. It gets the number,
 * large, and nothing else (AC9). A single-bar chart of one value is the thing
 * the charter's "chosen automatically" promise was retired for.
 */
function renderScalar(body) {
  const wrap = document.createElement("div");
  wrap.className = "scalar";

  const value = document.createElement("p");
  value.className = "scalar-value";
  value.textContent = body.rows[0][0];
  wrap.appendChild(value);

  const label = document.createElement("p");
  label.className = "scalar-label";
  label.textContent = body.columns[0];
  wrap.appendChild(label);
  return wrap;
}

/* Zero rows answers the question; it is not a failure and does not read as one. */
function renderEmpty() {
  const p = document.createElement("p");
  p.className = "empty";
  p.textContent = "No results matched that question.";
  return p;
}

function renderTable(body) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";

  const table = document.createElement("table");
  const thead = table.createTHead().insertRow();
  body.columns.forEach((name) => {
    const th = document.createElement("th");
    th.textContent = name;
    thead.appendChild(th);
  });

  const tbody = table.createTBody();
  body.rows.forEach((row) => {
    const tr = tbody.insertRow();
    row.forEach((cell) => {
      const td = tr.insertCell();
      // null is a real value and must not render as the string "null".
      td.textContent = cell === null ? "—" : cell;
      if (cell === null) td.className = "null";
    });
  });

  wrap.appendChild(table);

  const count = document.createElement("p");
  count.className = "rowcount";
  count.textContent = `${body.rows.length} row${body.rows.length === 1 ? "" : "s"}`;
  wrap.appendChild(count);
  return wrap;
}

function renderSql(sql) {
  sqlBox.textContent = sql || "No query was produced.";
}

/*
 * Resolved Q-E. The retry loop is the evidence that this is an agent rather
 * than a wrapper around one prompt, so the trace shows the error the agent
 * read as well as the fact that it retried — charter §1's diagram is literally
 * *read the error, revise*.
 */
function renderTrace(trace) {
  clear(traceBox);
  if (!trace || trace.length === 0) {
    tracePanel.hidden = true;
    return;
  }
  tracePanel.hidden = false;

  const attempts = trace.length;
  traceSummary.textContent =
    attempts === 1 ? "1 step" : `${attempts} steps, including a retry`;

  trace.forEach((step) => {
    const item = document.createElement("div");
    item.className = step.ok ? "step" : "step failed";

    const head = document.createElement("p");
    head.className = "step-head";
    head.textContent = `Attempt ${step.attempt} — ${step.action} — ${
      step.ok ? "succeeded" : step.category || "failed"
    }`;
    item.appendChild(head);

    if (step.sql) {
      const pre = document.createElement("pre");
      pre.textContent = step.sql;
      item.appendChild(pre);
    }
    if (step.error) {
      const err = document.createElement("p");
      err.className = "step-error";
      err.textContent = step.error;
      item.appendChild(err);
    }
    traceBox.appendChild(item);
  });
}


/*
 * The chart. Hand-rolled SVG, no library (resolved D-3).
 *
 * The whole requirement is one chart type: every chartable result in the
 * corpus is a label and one measure, at most 55 rows, and **not one result in
 * the corpus contains a date or timestamp column** (`009-frontend.md` §2.5), so
 * there is no time axis to support and nothing to interpolate. A CDN chart
 * library would add a network dependency to a demo whose selling point is that
 * `docker compose up` is the only setup instruction.
 *
 * Values arrive as strings when the column is NUMERIC (D-1). Parsing them to
 * float here is the one place that is *correct*: a bar width is inherently
 * approximate, and the exact value is in the table directly above.
 */
const SVG_NS = "http://www.w3.org/2000/svg";
const ROW_HEIGHT = 26;
const LABEL_WIDTH = 168;
const VALUE_WIDTH = 74;
const CHART_WIDTH = 720;
const LABEL_MAX_CHARS = 26;

function svg(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function drawChart(body) {
  clear(chartBox);

  const labelIndex = body.series.label;
  const measureIndex = body.series.measure;
  const points = body.rows.map((row) => ({
    label: row[labelIndex] === null ? "—" : String(row[labelIndex]),
    value: Number.parseFloat(row[measureIndex]),
  }));

  // A measure that is entirely null, non-numeric or non-positive has no
  // meaningful bar length. Saying so beats drawing a row of zero-width bars
  // and letting the reader think the data is empty.
  const usable = points.filter((p) => Number.isFinite(p.value));
  const max = Math.max(...usable.map((p) => p.value), 0);
  if (usable.length === 0 || max <= 0) {
    const note = document.createElement("p");
    note.className = "chart-note";
    note.textContent =
      "These values cannot be drawn as bars. The table above has the numbers.";
    chartBox.appendChild(note);
    return;
  }

  const barsWidth = CHART_WIDTH - LABEL_WIDTH - VALUE_WIDTH;
  const height = points.length * ROW_HEIGHT + 8;
  const chart = svg("svg", {
    viewBox: `0 0 ${CHART_WIDTH} ${height}`,
    width: "100%",
    height,
    role: "img",
    "aria-label": `Bar chart of ${body.columns[measureIndex]} by ${body.columns[labelIndex]}`,
  });

  points.forEach((point, index) => {
    const y = index * ROW_HEIGHT + 4;
    const mid = y + ROW_HEIGHT / 2 - 4;

    const label = svg("text", {
      x: LABEL_WIDTH - 10,
      y: mid,
      "text-anchor": "end",
      "dominant-baseline": "middle",
      class: "chart-label",
    });
    // SVG text does not wrap or ellipsize, so long labels are truncated with
    // the full value kept in a <title> for hover and screen readers.
    label.textContent =
      point.label.length > LABEL_MAX_CHARS
        ? point.label.slice(0, LABEL_MAX_CHARS - 1) + "…"
        : point.label;
    if (point.label.length > LABEL_MAX_CHARS) {
      const title = svg("title", {});
      title.textContent = point.label;
      label.appendChild(title);
    }
    chart.appendChild(label);

    const width = Number.isFinite(point.value)
      ? Math.max((Math.max(point.value, 0) / max) * barsWidth, 1)
      : 0;
    chart.appendChild(
      svg("rect", {
        x: LABEL_WIDTH,
        y,
        width,
        height: ROW_HEIGHT - 8,
        rx: 2,
        class: "chart-bar",
      })
    );

    const value = svg("text", {
      x: LABEL_WIDTH + width + 8,
      y: mid,
      "dominant-baseline": "middle",
      class: "chart-value",
    });
    // The *unparsed* cell, so the number beside the bar is the database's
    // number rather than the float the bar was scaled from.
    value.textContent = body.rows[index][measureIndex];
    chart.appendChild(value);
  });

  chartBox.appendChild(chart);
}

/*
 * AC10 on arrival, not only after the first question.
 *
 * Somebody opening the page mid-benchmark should see the warning before they
 * type, not after they have waited ten seconds for an answer to prove it. The
 * server returns `known: false` until a question has actually been asked, so
 * this shows nothing on a cold process rather than guessing.
 */
refreshQuota();
