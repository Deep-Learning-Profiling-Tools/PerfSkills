"""Keep whole-process GPU evidence distinct from a declared application ROI."""


def requires_roi(plan):
    metric = plan.get("metric") or {}
    return (metric.get("source") != "wall" and metric.get("scope") not in {"whole-process", "end-to-end"}
            and bool((plan.get("profiling") or {}).get("roi_name")))


def roi_scope(scope):
    if not isinstance(scope, dict):
        return False
    return scope.get("policy") in {"roi", "nvtx-roi",
        "outside-ROI activity is collateral evidence; ROI hotspots remain primary"}


def primary_target(plan, capsule):
    return (not requires_roi(plan) or roi_scope(capsule.get("scope")) or
            roi_scope((capsule.get("acquisition") or {}).get("target_scope")))
