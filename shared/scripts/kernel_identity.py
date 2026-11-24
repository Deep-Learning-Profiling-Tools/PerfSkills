"""Conservative joins across profiler spellings of the same compiled kernel."""

import re


def simple_base(name):
    base = (name or "").split("(", 1)[0].strip()
    return base if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9:]*", base) else None


def matching(resources, name):
    rows = resources.get("all_kernels", resources.get("kernels", []))
    compact = re.sub(r"\s+", "", name or "")
    exact = [r for r in rows if compact in {re.sub(r"\s+", "", r.get("name", "")), r.get("symbol")}]
    if exact:
        return exact, "exact"
    base = simple_base(name)
    family = [r for r in rows if base and simple_base(r.get("name")) == base]
    unique = {r.get("symbol"): r for r in family}
    return (list(unique.values()), "unique-compiled-kernel-name") if len(unique) == 1 else ([], None)


def same_target(resources, left, right):
    if left and left == right:
        return True
    before, _ = matching(resources,left)
    after, _ = matching(resources,right)
    return len(before) == len(after) == 1 and bool(before[0].get("symbol")) and before[0]["symbol"] == after[0].get("symbol")


def capture_regex(name, resources):
    rows, method = matching(resources, name)
    base = simple_base(name)
    # Qualifier spelling differs (const double* / double const*). A basename
    # filter is safe only when this binary contains exactly one such kernel.
    family = {r.get("symbol") for r in resources.get("all_kernels", resources.get("kernels", []))
              if base and simple_base(r.get("name")) == base}
    if rows and base and len(family) == 1:
        return "^" + re.escape(base) + r"\("
    return "^" + re.escape(name) + "$"


def capture_selector(name, resources, tool):
    if not name:
        return {"target_regex": None}
    if tool == "kernel":
        rows, method = matching(resources, name)
        symbols = {row.get("symbol") for row in rows}
        if method == "exact" and len(symbols) == 1:
            symbol = next(iter(symbols)) or ""
            if re.fullmatch(r"_Z[A-Za-z0-9_]+", symbol):
                return {"target_regex": "^" + symbol + "$", "target_name_base": "mangled"}
    return {"target_regex": capture_regex(name, resources)}
