"""Optional exclusive GPU energy counters and client-metered session cost."""

import ctypes
import math
from pathlib import Path
from statistics import median
from decision_learning import update
from experiment_support import load


def nvml_joules(uuids):
    """NVML millijoules since driver load, summed only over declared exclusive physical GPUs."""
    lib = ctypes.CDLL("libnvidia-ml.so.1")
    if lib.nvmlInit_v2() != 0:
        return None
    try:
        total = 0
        for uuid in uuids:
            handle, value = ctypes.c_void_p(), ctypes.c_ulonglong()
            if (lib.nvmlDeviceGetHandleByUUID(uuid.encode(), ctypes.byref(handle)) or
                    lib.nvmlDeviceGetTotalEnergyConsumption(handle, ctypes.byref(value))):
                return None
            total += value.value
        return total / 1000
    finally:
        lib.nvmlShutdown()


def energy(config):
    try:
        if config.get("exclusive_energy_j_path"):
            value = float(Path(config["exclusive_energy_j_path"]).read_text().strip())
            return value if math.isfinite(value) and value >= 0 else None
        if config.get("exclusive_gpu_uuids"):
            return nvml_joules(config["exclusive_gpu_uuids"])
    except (ValueError, OSError, AttributeError):
        return None


def token_usage(run, total):
    """Cumulative campaign totals. Associate deltas only with isolated evidence commands."""
    from resource_meter import ledger
    if type(total) is not int or total < 0:
        raise ValueError("session tokens must be a nonnegative cumulative count")
    rows = ledger(run)
    with update(run / "session-usage.json") as usage:
        if total < usage.get("session_tokens", 0):
            raise ValueError("cumulative session tokens decreased")
        if "position" in usage:
            window = rows[usage["position"]:]
            commands = [r["label"] for r in window if r["scope"] == "command"]
            providers = {r["label"] for r in window if r["scope"] == "capture" and r.get("usable")}
            if commands == ["evidence"] and len(providers) == 1 and total > usage["session_tokens"]:
                samples = usage.setdefault("evidence_interval_tokens", {}).setdefault(providers.pop(), [])
                samples.append(total - usage["session_tokens"])
                del samples[:-16]
        usage.update(session_tokens=total, source="client-reported-campaign-total", position=len(rows))
    return {"session_tokens": total}


def token_forecast(run, provider, packet):
    samples = load(run / "session-usage.json", {}).get("evidence_interval_tokens", {}).get(provider, [])
    return max(packet, median(samples)) if samples else None
