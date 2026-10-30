#pragma once

#include <cuda_runtime.h>
#include <nvtx3/nvToolsExt.h>

// A synchronized, profiler-visible ROI shared by framework workloads.
#define HPCPERF_ROI_BEGIN_SYNC() do { cudaDeviceSynchronize(); nvtxRangePushA("hpcperf:roi"); } while (0)
#define HPCPERF_ROI_END_SYNC() do { cudaDeviceSynchronize(); nvtxRangePop(); } while (0)
#define HPCPERF_ROI_EXCLUDE_BEGIN_SYNC() do { cudaDeviceSynchronize(); nvtxRangePushA("hpcperf:roi-excluded"); } while (0)
#define HPCPERF_ROI_EXCLUDE_END() do { cudaDeviceSynchronize(); nvtxRangePop(); } while (0)
