#!/usr/bin/env bash
# vendor espressif/esp-tflite-micro v1.3.8 到固件 lib/（一次性）
# 说明：eloquentarduino/tflm_esp32 对 microWakeWord 流式模型输出恒 0，
# 必须用 espressif 官方运行时。详见 docs/firmware.md。
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/voice-client/xvf3800-satellite/lib/esp-tflite-micro"
mkdir -p "$HERE/voice-client/xvf3800-satellite/lib" /tmp/etm
cd /tmp/etm
curl -sL -o etm.tar.gz https://github.com/espressif/esp-tflite-micro/archive/refs/tags/v1.3.8.tar.gz
tar xzf etm.tar.gz
rm -rf "$DEST"
mkdir -p "$DEST/src"
cp -r esp-tflite-micro-1.3.8/tensorflow "$DEST/src/"
cp -r esp-tflite-micro-1.3.8/third_party "$DEST/src/"
cp -r esp-tflite-micro-1.3.8/signal "$DEST/src/"
# array.h shim：上游 kernel_util.cc 引用全量 TF 仓库的头，tflite-micro 下仅需占位
cat > "$DEST/src/tensorflow/lite/array.h" <<'SHIM'
#ifndef TENSORFLOW_LITE_ARRAY_H_
#define TENSORFLOW_LITE_ARRAY_H_
#include <memory>
#include "tensorflow/lite/c/common.h"
namespace tflite {
struct TfLiteIntArrayDeleter {
  void operator()(TfLiteIntArray *p) const { if (p) TfLiteIntArrayFree(p); }
};
using IntArrayUniquePtr = std::unique_ptr<TfLiteIntArray, TfLiteIntArrayDeleter>;
}  // namespace tflite
#endif
SHIM
cat > "$DEST/library.json" <<'LIBJSON'
{
  "name": "esp-tflite-micro",
  "version": "1.3.8",
  "build": {
    "srcFilter": [
      "+<tensorflow/>",
      "+<third_party/flatbuffers/>",
      "+<third_party/gemmlowp/>",
      "+<third_party/ruy/>",
      "-<tensorflow/lite/kernels/>",
      "-<tensorflow/lite/micro/kernels/esp_nn/>",
      "-<tensorflow/lite/micro/examples/>",
      "-<tensorflow/lite/micro/benchmarks/>",
      "-<tensorflow/lite/micro/integration_tests/>"
    ],
    "flags": ["-std=gnu++17"]
  }
}
LIBJSON
echo "vendored to $DEST"
