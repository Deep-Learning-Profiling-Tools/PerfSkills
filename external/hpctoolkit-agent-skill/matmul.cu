#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>

#define N 1024
#define BLOCK_SIZE 16

#define CUDA_CHECK(call)                                              \
do {                                                                  \
    cudaError_t err = call;                                           \
    if (err != cudaSuccess) {                                         \
        fprintf(stderr, "CUDA error at %s:%d: %s\n",                  \
                __FILE__, __LINE__, cudaGetErrorString(err));         \
        exit(EXIT_FAILURE);                                           \
    }                                                                 \
} while (0)

__global__ void matmul(const float *A,
                       const float *B,
                       float *C,
                       int n)
{
    int row = blockIdx.y * blockDim.y + threadIdx.y;
    int col = blockIdx.x * blockDim.x + threadIdx.x;

    if (row < n && col < n) {
        float sum = 0.0f;

        for (int k = 0; k < n; ++k) {
            sum += A[row * n + k] * B[k * n + col];
        }

        C[row * n + col] = sum;
    }
}

int main()
{
    size_t bytes = N * N * sizeof(float);

    float *h_A = (float *)malloc(bytes);
    float *h_B = (float *)malloc(bytes);
    float *h_C = (float *)malloc(bytes);

    for (int i = 0; i < N * N; ++i) {
        h_A[i] = 1.0f;
        h_B[i] = 2.0f;
        h_C[i] = 0.0f;
    }

    float *d_A = nullptr;
    float *d_B = nullptr;
    float *d_C = nullptr;

    CUDA_CHECK(cudaMalloc(&d_A, bytes));
    CUDA_CHECK(cudaMalloc(&d_B, bytes));
    CUDA_CHECK(cudaMalloc(&d_C, bytes));

    CUDA_CHECK(cudaMemcpy(
        d_A, h_A, bytes, cudaMemcpyHostToDevice));

    CUDA_CHECK(cudaMemcpy(
        d_B, h_B, bytes, cudaMemcpyHostToDevice));

    dim3 block(BLOCK_SIZE, BLOCK_SIZE);

    dim3 grid(
        (N + BLOCK_SIZE - 1) / BLOCK_SIZE,
        (N + BLOCK_SIZE - 1) / BLOCK_SIZE
    );

    printf("Launching grid (%u,%u), block (%u,%u)\n",
           grid.x, grid.y, block.x, block.y);

    for (int iter = 0; iter < 20; ++iter) {
        matmul<<<grid, block>>>(d_A, d_B, d_C, N);

        // Check kernel launch
        CUDA_CHECK(cudaGetLastError());
    }

    // Check asynchronous kernel execution
    CUDA_CHECK(cudaDeviceSynchronize());

    CUDA_CHECK(cudaMemcpy(
        h_C, d_C, bytes, cudaMemcpyDeviceToHost));

    printf("C[0] = %f\n", h_C[0]);
    printf("C[N*N-1] = %f\n", h_C[N * N - 1]);

    CUDA_CHECK(cudaFree(d_A));
    CUDA_CHECK(cudaFree(d_B));
    CUDA_CHECK(cudaFree(d_C));

    free(h_A);
    free(h_B);
    free(h_C);

    return 0;
}
