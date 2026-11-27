const routes = ["overview", "cases", "runs", "evidence"];
const receiptAliases = new Set(["receipt", "receipts", "clip-receipt"]);

export function activeRoute() {
  const [name, id] = (location.hash.slice(1) || "overview").split("/");
  const normalized = receiptAliases.has(name) ? "evidence" : name;
  return {
    name: routes.includes(normalized) ? normalized : "overview",
    id: /^\d+$/.test(id || "") ? Number(id) : null,
  };
}

export function navigate(route) {
  location.hash = route;
}

export { routes };
