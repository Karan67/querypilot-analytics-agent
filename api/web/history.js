/*
 * QueryPilot — the history reader (Iteration 7 T7, AC4).
 *
 * **Nothing from the server is ever assigned to innerHTML.** That rule is
 * sharper here than on the answer page: every question in this list is text a
 * user typed, and every SQL string was written by a language model. Both are
 * data. Every insertion below goes through textContent.
 *
 * The numbers are shown the way the store records them, including the two that
 * are easy to misread and are therefore labelled rather than left bare:
 *
 *   - a cached answer cost zero tokens *for that request*, and was paid for
 *     once by the answer that produced it. The summary says so.
 *   - a token count is either the provider's billed figure or a local estimate,
 *     and D-1's rule is that a number which does not say which it is, is not a
 *     measurement. Estimates are marked.
 */

const statusBox = document.getElementById("status");
const summaryBox = document.getElementById("summary");
const answersBox = document.getElementById("answers");

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function showStatus(text, kind) {
  statusBox.hidden = false;
  statusBox.className = kind;
  statusBox.textContent = text;
}

async function load() {
  try {
    const response = await fetch("/history/data");
    const body = await response.json();

    if (!body.ok) {
      // The store is unreadable, which is a different thing from empty. Saying
      // "no questions yet" here would be a lie about the data.
      showStatus(
        "The history store could not be read, so this list is not empty — it " +
          "is unknown. " + body.error,
        "error"
      );
      return;
    }
    render(body.answers);
  } catch (err) {
    showStatus("Could not reach QueryPilot. Is the API running?", "error");
  }
}

function render(answers) {
  statusBox.hidden = true;
  clear(answersBox);

  if (!answers.length) {
    summaryBox.hidden = true;
    answersBox.appendChild(
      el("p", "empty", "Nothing has been asked yet. Ask a question and it will appear here.")
    );
    return;
  }

  renderSummary(answers);
  answers.forEach((answer) => answersBox.appendChild(renderAnswer(answer)));
}

/*
 * Four totals, each of which is a sum over the rows below rather than a
 * separately stored figure — so a surprising number can always be traced to the
 * answer that caused it.
 */
function renderSummary(answers) {
  const billed = answers.filter((a) => a.provider_calls > 0);
  const hits = answers.filter((a) => a.cache_hit);
  const failed = answers.filter((a) => !a.ok);
  const tokens = answers.reduce((sum, a) => sum + (a.tokens || 0), 0);

  // Summing needs no `cache_hit` filter, because a hit records zero rather than
  // replaying what the original answer cost. That was decided at T4 precisely
  // so a total like this one cannot overstate spend.
  const estimated = billed.some((a) => !a.measured);

  summaryBox.hidden = false;
  clear(summaryBox);

  const grid = el("div", "summary-grid");
  grid.appendChild(stat(answers.length, "questions", `${failed.length} failed`));
  grid.appendChild(
    stat(
      tokens.toLocaleString(),
      estimated ? "tokens (some estimated)" : "tokens billed",
      `${billed.length} provider calls`
    )
  );
  grid.appendChild(
    stat(hits.length, "served from cache", hits.length ? "cost nothing to repeat" : "")
  );
  grid.appendChild(stat(medianOf(answers.map((a) => a.total_ms)) + "ms", "median latency", ""));
  summaryBox.appendChild(grid);
}

function stat(value, label, note) {
  const box = el("div", "stat");
  box.appendChild(el("p", "stat-value", value));
  box.appendChild(el("p", "stat-label", label));
  if (note) box.appendChild(el("p", "stat-note", note));
  return box;
}

function medianOf(numbers) {
  if (!numbers.length) return 0;
  const sorted = numbers.slice().sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : Math.round((sorted[middle - 1] + sorted[middle]) / 2);
}

function renderAnswer(answer) {
  const card = el("article", answer.ok ? "answer-card" : "answer-card failed");

  const head = el("div", "answer-head");
  head.appendChild(el("p", "answer-question", answer.question));
  head.appendChild(el("p", "answer-when", answer.asked_at.replace("T", " ").replace("+00:00", " UTC")));
  card.appendChild(head);

  const tags = el("p", "answer-tags");
  if (!answer.ok) tags.appendChild(el("span", "tag bad", answer.category || "failed"));
  if (answer.cache_hit) tags.appendChild(el("span", "tag", "cached"));
  if (answer.attempts_used > 1) {
    // The retry loop is the charter's central claim, so a question that needed
    // one is worth pointing at rather than leaving in the trace alone.
    tags.appendChild(el("span", "tag", `${answer.attempts_used} attempts`));
  }
  tags.appendChild(el("span", "tag", tokenLabel(answer)));
  tags.appendChild(el("span", "tag", `${answer.total_ms}ms total · ${answer.provider_ms}ms model`));
  if (answer.row_count !== null) tags.appendChild(el("span", "tag", `${answer.row_count} rows`));
  card.appendChild(tags);

  if (answer.sql) {
    const details = el("details", "panel");
    details.appendChild(el("summary", null, "SQL"));
    details.appendChild(el("pre", null, answer.sql));
    card.appendChild(details);
  }

  if (answer.trace.length) {
    const details = el("details", "panel");
    details.appendChild(
      el("summary", null, `How the agent got there — ${answer.trace.length} step(s)`)
    );
    answer.trace.forEach((step) => details.appendChild(renderStep(step)));
    card.appendChild(details);
  }

  card.appendChild(renderProvenance(answer));
  return card;
}

/*
 * A cache hit spent nothing on *this* request; the answer was paid for once,
 * by the row that produced it. Saying "0 tokens" alone would read as free.
 */
function tokenLabel(answer) {
  if (answer.cache_hit) return "0 tokens (reused)";
  if (answer.tokens === null) return "cost not recorded";
  const suffix = answer.measured ? "" : ", estimated";
  return `${answer.tokens.toLocaleString()} tokens${suffix}`;
}

function renderStep(step) {
  const box = el("div", step.ok ? "step" : "step failed");
  box.appendChild(el("p", "step-head", `Attempt ${step.attempt} — ${step.action}`));
  if (step.sql) box.appendChild(el("pre", null, step.sql));
  if (step.error) box.appendChild(el("p", "step-error", `${step.category}: ${step.error}`));
  return box;
}

/*
 * What produced this answer. Without it a row is uninterpretable the moment the
 * prompt or the schema moves — the same reason EVALS.md entries carry their
 * fingerprints, applied to a log rather than a benchmark.
 */
function renderProvenance(answer) {
  const parts = [];
  if (answer.model) parts.push(answer.model);
  if (answer.prompt_fp) parts.push(`prompt ${answer.prompt_fp}`);
  if (answer.schema_fp) parts.push(`schema ${answer.schema_fp}`);
  return el("p", "provenance", parts.join(" · ") || "no provider was called");
}

load();
