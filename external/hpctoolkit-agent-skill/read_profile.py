"""Print the GPU-bearing rows of an HPCToolkit database.

    python3 read_profile.py [database-directory]

Defaults to hpctoolkit-matmul-database (the bundled matmul example).
Requires hatchet and pandas.
"""

import sys

import hatchet as ht
import pandas as pd

database = sys.argv[1] if len(sys.argv) > 1 else "hpctoolkit-matmul-database"

metrics = [
    "time",
    "gpuop",
    "gicopy",
    "gxcopy",
    "gxcopy:count",
    "gxcopy:h2d",
    "gxcopy:d2h",
    "gsync",
    "gsync:count",
    "gsync:ctx",
]

gf = ht.GraphFrame.from_hpctoolkit_latest(
    database,
    metric_names=metrics
)

df = gf.dataframe.reset_index()

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 400)

print("Loaded metrics:")
print(gf.show_metric_columns())

# Metric columns actually present in this database
metric_cols = [
    c for c in df.columns
    if c not in ["node", "name"]
]

# Rows containing at least one GPU measurement
gpu_cols = [
    c for c in metric_cols
    if c.startswith(("gpuop", "gicopy", "gxcopy", "gsync"))
]

gpu_rows = df[df[gpu_cols].notna().any(axis=1)]

print("\nFirst 5 rows containing GPU measurements:")
print(
    gpu_rows[["name"] + gpu_cols]
    .head(5)
    .to_string(index=False)
)
