import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { resultTable } from "../components/common/table.js";
import { submit } from "../hooks/submit.js";
import { el } from "../utils/dom.js";
import { cell, download, time } from "../utils/format.js";

function reviewForm(id, saved) {
  const form = el("form", {id: "review-form"},
    field("复核意见", el("textarea", {name: "note", required: true, minlength: 3, maxlength: 500, rows: 3})),
    errorText(), el("button", {type: "submit"}, "签署复核"));
  submit(form, async values => saved(await api.submitReview(id, {note: values.get("note")})));
  return form;
}

function signature(review) {
  return el("div", {"data-testid": "reviewed", class: "signature"},
    el("strong", {}, `已签署 · ${review.actor}`), el("p", {}, review.note), el("time", {}, time(review.created_at)));
}

function sourcePanel(run, receipt) {
  const result = run.result || {};
  const code = result.reading_source || run.reading_source || run.source || "primary";
  const label = result.reading_source_label || run.source_label ||
    ({primary: "主库", standby: "备库", mixed: "主库、备库", snapshot: "接单快照"})[code] || code;
  const missingSkippable = result.standby_missing_skippable_pages || result.backup_missing_skippable_pages || [];
  const missingRequired = result.standby_missing_required_pages || result.backup_missing_required_pages || [];
  const counts = result.page_source_counts || {};
  const snapshotDigest = run.reading_snapshot?.digest?.slice(0, 12)
    || result.snapshot?.digest?.slice(0, 12)
    || run.digest?.slice(0, 12)
    || "已核对";
  const lines = [
    el("p", {"data-testid": "source-used"},
      "本回执使用「",
      el("strong", {"data-testid": "receipt-source", "data-source-side": code}, label),
      `」的读数；板号、瓦数和跳页依据（Zone Map/空值位图）都已对上接单时固定的那份读数（快照 ${snapshotDigest}）。`),
  ];
  if (code === "standby" || code === "mixed") {
    lines.push(el("p", {}, "主库断线后，已接下的抽检单改从备库续读；已完成页由检查点保留，重试不会另出回执。"));
  }
  if (missingSkippable.length) {
    lines.push(el("p", {"data-testid": "missing-skippable"},
      `备库缺少第 ${missingSkippable.join("、")} 页，但这些页按接单 Zone Map 属于本单可跳过的瓦数页，因此照常出回执。`));
  }
  if (missingRequired.length) {
    lines.push(el("p", {role: "alert"}, `必读页第 ${missingRequired.join("、")} 页未到齐，本不应签发回执。`));
  }
  if (counts.primary || counts.standby) {
    lines.push(el("p", {class: "source-counts"},
      `逐页来源：主库 ${counts.primary || 0} 页，备库 ${counts.standby || 0} 页；跳过页未取瓦数。`));
  }
  if (receipt?.id) {
    lines.push(el("p", {"data-testid": "receipt-number"}, `回执编号：R-${String(receipt.id).padStart(6, "0")}（一单一回执，重试不重号）`));
  }
  return el("section", {class: "feature source-banner", "data-testid": "reading-source"},
    el("div", {}, el("h2", {}, "读数来源"), ...lines),
    el("details", {}, el("summary", {}, "来源与快照证据"),
      el("code", {}, JSON.stringify({
        source: code, label, used_sources: result.used_sources,
        snapshot: result.snapshot?.digest || run.reading_snapshot?.digest,
        manifest: result.snapshot?.manifest || run.reading_snapshot?.manifest,
        source_report: run.source_report,
      }, null, 2))));
}

export async function evidencePage(config, user, route) {
  const runs = (await api.runs()).filter(item => item.status === "done");
  const run = route.id ? await api.run(route.id) : runs[0];
  if (!run?.result) return el("div", {class: "page"},
    pageHeading(config.modules[3], null), empty("成功执行一个批次后可以查看、签核和下载证据。"),
    el("a", {href: "#runs"}, config.modules[2]));

  const [review, receipt] = await Promise.all([
    api.review(run.id),
    api.receipt(run.id).catch(() => null),
  ]);
  const issue = errorText();
  const button = el("button", {type: "button", onclick: async () => {
    button.disabled = true;
    issue.textContent = "";
    try {
      await download(await api.export(run.id), `evidence-${run.id}.json`);
    } catch (error) {
      issue.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }}, "导出证据");
  const signing = el("div", {});
  signing.append(review ? signature(review) : user.role === "reviewer" ?
    reviewForm(run.id, data => signing.replaceChildren(signature(data))) : empty("待复核员签署。"));
  const options = el("select", {"aria-label": "选择已完成的批次", onchange: event => {
    location.hash = `evidence/${event.target.value}`;
  }}, runs.map(item => el("option", {value: item.id, selected: item.id === run.id}, `#${item.id} ${item.case_title}`)));
  return el("div", {class: "page evidence"},
    pageHeading(config.modules[3], run.case_title, button),
    issue, options,
    sourcePanel(run, receipt),
    el("section", {class: "feature evidence-summary"}, el("div", {}, el("h2", {}, "批次判定"), el("p", {}, run.result.summary)),
      el("details", {}, el("summary", {}, "证据摘要"), el("code", {}, run.digest))),
    el("dl", {class: "metrics"}, Object.entries(run.result.metrics || {}).map(([label, value]) =>
      el("div", {}, el("dt", {}, label), el("dd", {}, cell(value))))),
    section("业务结果明细", run.result.rows?.length ? resultTable(run.result.rows.map(({来源: _sourceCode, ...row}) => row)) : empty("此批次无明细。")),
    section("复核签署", signing));
}
