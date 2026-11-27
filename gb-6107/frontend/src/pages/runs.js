import { api } from "../api/workbench.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { table } from "../components/common/table.js";
import { status } from "../components/common/status.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { time } from "../utils/format.js";

const ACTIVE = new Set(["queued", "processing", "waiting"]);

function sourceDrill(run, onChanged) {
  const message = el("p", {role: "status", "data-testid": "drill-message"});
  async function act(body, success) {
    message.textContent = "处理中…";
    try {
      await api.controlSources(run.id, {...body, run_now: body.run_now ?? true});
      const next = await api.run(run.id);
      message.textContent = success(next);
      onChanged?.(next);
    } catch (error) {
      message.textContent = error.message;
    }
  }
  const missingInput = el("input", {
    name: "missing_pages", value: "2", inputmode: "numeric",
    "aria-label": "备库未到页号，逗号分隔",
  });
  const interruptInput = el("input", {
    name: "interrupt_after", type: "number", min: "0", value: "1",
    "aria-label": "读几页后中断",
  });
  return el("div", {class: "source-drill", "data-testid": "source-drill"},
    el("h3", {}, "主备断线 / 缺页 / 中断演练"),
    el("div", {class: "drill-actions"},
      el("button", {type: "button", "data-testid": "failover-primary", onclick: () =>
        act({primary_down: true}, next => `已切换策略；当前状态：${next.status}`)}, "断开主库并改用备库"),
      el("label", {}, "备库缺页", missingInput),
      el("button", {type: "button", "data-testid": "set-missing-pages", onclick: () => {
        const pages = missingInput.value.split(/[,，\s]+/).filter(Boolean).map(Number).filter(Number.isInteger);
        return act({standby_missing_pages: pages}, next =>
          next.wait_reason ? `已留队：${next.wait_reason}` : "这些页可跳过，回执已照常完成。");
      }}, "设置缺页并试跑"),
      el("button", {type: "button", "data-testid": "recover-pages", onclick: () =>
        act({recover: true}, () => "已恢复主备库并补齐页，从检查点继续。")}, "补齐页 / 恢复"),
      el("label", {}, "读几页后中断", interruptInput),
      el("button", {type: "button", "data-testid": "interrupt-reading", onclick: () =>
        act({interrupt_after: Number(interruptInput.value || 1)}, next =>
          `已在中途暂停，检查点 ${next.checkpoint ?? 0}；再次续跑不会重复出回执。`)}, "注入中断"),
      el("button", {type: "button", "data-testid": "resume-run", onclick: async event => {
        event.currentTarget.disabled = true;
        try {
          await api.advanceRun(run.id);
          const next = await api.run(run.id);
          message.textContent = next.status === "done" ? "已续跑完成，回执只签发一次。" : `当前状态：${next.status}`;
          onChanged?.(next);
        } catch (error) {
          message.textContent = error.message;
        } finally {
          event.currentTarget.disabled = false;
        }
      }}, "断点续跑 / 重试")),
    message);
}

function reportState(run, onChanged, canControl) {
  const source = run.result?.reading_source_label || run.source_label || run.reading_source;
  return el("div", {class: "stack", id: "run-state", "data-run-state": run.id},
    el("div", {class: "run-status-line"}, status(run.waiting ? "waiting" : run.status),
      source ? el("span", {"data-testid": "run-source"}, `读数来源：${source}`) : null),
    run.wait_reason ? el("p", {role: "status", class: "wait-reason", "data-testid": "wait-reason"},
      `${run.wait_reason}，单据继续保留在队列中。`) : null,
    run.missing_pages?.length ? el("p", {"data-testid": "missing-pages"},
      `未到页：第 ${run.missing_pages.join("、")} 页`) : null,
    el("p", {}, run.status === "queued" || run.status === "processing" || run.status === "waiting" ?
      "后台正在处理，页面会自动更新。" : run.failure || run.result?.summary),
    run.progress && (run.status === "queued" || run.status === "processing") ?
      el("p", {"data-testid": "checkpoint"}, `已保存页级检查点：${run.checkpoint ?? run.progress.cursor ?? 0}`) : null,
    run.status === "done" ? el("a", {class: "button", href: `#evidence/${run.id}`}, "查看裁剪回执") : null,
    canControl ? sourceDrill(run, onChanged) : null);
}

export async function runsPage(config, route, signal, user) {
  const canControl = user?.role === "operator";
  const items = await api.runs();
  const latest = route.id ? await api.run(route.id, signal) : items[0];
  let detail;
  function replaceDetail(next) {
    const replacement = reportState(next, replaceDetail, canControl);
    detail.replaceWith(replacement);
    detail = replacement;
  }
  detail = latest ? reportState(latest, replaceDetail, canControl) : empty("进入资料页，选用一个业务样本并提交执行。");
  async function refresh() {
    try {
      const next = await api.run(latest.id, signal);
      if (!signal.aborted) {
        replaceDetail(next);
        const badge = document.querySelector(`[data-run="${next.id}"] .status`);
        if (badge) badge.replaceWith(status(next.status));
      }
      return ACTIVE.has(next.status);
    } catch (error) {
      if (!signal.aborted) detail.append(el("p", {role: "alert"}, error.message));
      return false;
    }
  }
  if (latest && ACTIVE.has(latest.status)) poll(refresh, signal);
  return el("div", {class: "page"},
    pageHeading(config.modules[2], config.parameter_label),
    latest ? section(latest.case_title, detail) : detail,
    section("执行批次", items.length ? table(["资料", "编号", "参数", "状态", "提交时间"],
      items.map(run => [el("a", {"data-run": run.id, href: `#runs/${run.id}`}, run.case_title), run.id,
        run.parameter, el("span", {"data-run": run.id}, status(run.status)), time(run.created_at)]), "执行批次") :
      empty("暂无执行记录。")));
}
