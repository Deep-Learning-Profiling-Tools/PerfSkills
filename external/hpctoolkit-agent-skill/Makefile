NVCC = nvcc
NVCC_FLAGS = -O3 -lineinfo

TARGET = matmul
SRC = matmul.cu

all: $(TARGET)

$(TARGET): $(SRC)
	$(NVCC) $(NVCC_FLAGS) -o $(TARGET) $(SRC)

run:
	./$(TARGET)

clean:
	rm -f $(TARGET)
	rm -rf hpctoolkit-*-measurements
	rm -rf hpctoolkit-*-database
