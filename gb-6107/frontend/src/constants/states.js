export const States = Object.freeze({
  queued: "等待执行", pending: "留队等待",
  processing: "扫页中", running: "扫页中",
  waiting: "留队等页",
  done: "已完成", succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已撤单",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败", review: "复核签署",
  retry: "断点续跑", source_control: "主备控制", requeue: "重新入队",
});
