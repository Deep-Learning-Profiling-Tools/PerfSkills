"""Share a primary CUPTI execution with correctness when unprofiled collateral is optional."""

import time
from pathlib import Path
from types import SimpleNamespace
from campaign_io import campaign_fingerprint, extract
from campaign_run import run_build
from campaign_stats import summarize
from experiment_support import REPO, load, write
from selection_objective import kernel_mode


def enabled(plan, state, phase):
    spec = state.get("selection_objective") or {}
    return (phase == "screen" and kernel_mode(state) and spec.get("provider") == "pw-cupti" and
            spec.get("collateral") == "profiled" and
            plan.get("sampling",{}).get("measurement_policy") == "single-or-two-v3")


def baseline_sample(plan, run, baseline):
    data = load(run/"candidates/baseline/profile/cupti-activity.json",{})
    wall = data.get("elapsed_s")
    raw = run/"raw/cupti/baseline"
    if data.get("status") != "complete" or not (data.get("oracle") or {}).get("passed") or not wall:
        raise ValueError("shared activity requires the validated baseline activity sample")
    values = {"workspace":str(baseline),"run_dir":str(run),"run_id":plan["run_id"],"repo":str(REPO),"id":"baseline"}
    try:
        metric = extract(plan["metric"],(raw/"capture.stdout.txt").read_text(),
                         (raw/"capture.stderr.txt").read_text(),wall,values)
    except (ValueError,KeyError,OSError):
        metric = None
    return {"metric":metric,"wall_s":wall,"reused":True,"measurement_role":"profiled-collateral"}


def ratio(before, after, direction="lower"):
    if before is None or after is None or before <= 0 or after <= 0:
        return {"ci95":None}
    return {"point":before/after if direction == "lower" else after/before,"ci95":None}


def run(plan, run, state, baseline, generated, batch):
    started = time.perf_counter()
    variables = {"repo":str(REPO),"run_id":generated["run_id"],"run_dir":str(batch)}
    fingerprint = campaign_fingerprint(generated,variables)
    saved = load(batch/"campaign-result.json",{})
    if saved.get("status") == "complete" and saved.get("fingerprint_after") == fingerprint:
        return SimpleNamespace(returncode=0,stdout='{"status":"cached"}',stderr="")
    candidate = generated["candidates"][0]
    cid = candidate["id"]
    base = baseline_sample(plan,run,baseline)
    build = run_build(candidate,generated,batch,variables)
    from cupti_activity import capture
    capsule = capture(plan,run,cid,Path(state["candidates"][cid]["workspace"]))
    # Save the capture before source mapping or publication can fail.
    write(batch/"primary-activity.json",capsule)
    if capsule.get("status") != "complete" or not capsule.get("correct"):
        raise ValueError("shared activity did not establish both correctness and complete primary timing")
    after = campaign_fingerprint(generated,variables)
    if after != fingerprint:
        raise ValueError("source or execution inputs changed during shared activity")
    metric = capsule.get("diagnostic_metric")
    sample = {"metric":metric,"wall_s":capsule["spent_s"],"oracle":{"passed":True},
              "measurement_role":"profiled-collateral","profiled":True}
    improvement = ratio(base["metric"],metric,plan["metric"].get("direction","lower"))
    wall = ratio(base["wall_s"],sample["wall_s"])
    row = {"id":cid,"description":candidate.get("description"),"mechanism":candidate.get("mechanism"),
           "workspace":state["candidates"][cid]["workspace"],"pairs":0,"runs":1,"benchmark_executions":0,
           "profiled_executions":1,"baseline_reused":True,"stop":"shared-primary-activity",
           "confidence":"single-profiled-execution","measurement_role":"profiled-collateral",
           "metric":summarize([metric]) if metric is not None else {},
           "baseline_metric":summarize([base["metric"]]) if base["metric"] is not None else {},
           "improvement":improvement,"wall_improvement":wall,"correctness":{"passed":True},
           "ranking":{"status":"profiled-collateral","incumbent_at_screen":1.},
           "samples":{"baseline":[base],"candidate":[sample]},"warmup_samples":[]}
    final = {"winner":cid,"baseline_vs_winner":improvement,"wall_baseline_vs_winner":wall,
             "pairs":0,"gates":{"correctness":True},"stop":"shared-primary-activity"}
    result = {"protocol":"profileweaver.campaign-result/v1","status":"complete","run_id":generated["run_id"],
              "fingerprint":fingerprint,"fingerprint_after":after,"candidates":[row],"final":final,
              "elapsed_s":time.perf_counter()-started,"metric":plan["metric"],"sampling":generated["sampling"],
              "cost":{"build_s":build.get("spent_s",0),"screening":{"executions":0,"execution_s":0,"oracle_s":0},
                      "shared_primary":{"executions":1,"execution_s":sample["wall_s"]}},
              "builds":{cid:build}}
    write(batch/"campaign-result.json",result)
    return SimpleNamespace(returncode=0,stdout='{"status":"complete","shared_primary":true}',stderr="")


def publish_capture(plan, run, state, batch, ids, fingerprint):
    capsule = load(batch/"primary-activity.json",{})
    if not capsule or capsule.get("id") not in ids or capsule.get("status") != "complete":
        return
    cid = capsule["id"]
    from capture_history import preserve
    from skill_binding import binding
    capsule["capture_binding"] = binding(plan)["key"]
    capsule["acquisition"] = {"tool":"activity","provider":"pw-cupti","needs":["gpu-hotspot","roi-attribution"],
                            "reason":"shared-primary-and-correctness"}
    preserve(run,cid,capsule,fingerprint)
    # Checkpoint the already measured evidence before the optional source pass.
    state["candidates"][cid]["profile"] = capsule
    from selection_objective import reconcile
    reconcile(plan,run,state)
    from experiment_state import save_state
    save_state(run,state)
    locate(run,state,cid,capsule)
    from skill_runtime import remember
    remember(plan,run,state,capsule)


def locate(run,state,cid,capsule):
    directory = run/"candidates"/cid/"profile"
    if (directory/"source-targets.json").is_file():
        return
    raw = run/"raw/iteration"/cid
    raw.mkdir(parents=True,exist_ok=True)
    try:
        from iteration_profile import _locate_source
        _locate_source(run,Path(state["candidates"][cid]["workspace"]),Path(capsule["profile_artifact"]),
                       directory/"source-targets.json",raw)
    except (OSError,ValueError,RuntimeError) as exc:
        capsule["source_unresolved"] = str(exc)[:200]


def resume(run,state,cid,entry):
    capsule = load(run/"batches"/entry["id"]/"primary-activity.json",{})
    if capsule.get("status") == "complete" and capsule.get("id") == cid:
        locate(run,state,cid,capsule)
