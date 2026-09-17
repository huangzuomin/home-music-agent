# vendor espressif/esp-tflite-micro v1.3.8 到固件 lib/（一次性）
# 说明：eloquentarduino/tflm_esp32 对 microWakeWord 流式模型输出恒 0，
# 必须用 espressif 官方运行时。详见 docs/firmware.md。
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $PSScriptRoot
$Dest = Join-Path $Here 'voice-client\xvf3800-satellite\lib\esp-tflite-micro'
$tmp = Join-Path $env:TEMP 'etm'
New-Item -ItemType Directory -Force -Path $tmp, "$Here\voice-client\xvf3800-satellite\lib" | Out-Null
Invoke-WebRequest -Uri 'https://github.com/espressif/esp-tflite-micro/archive/refs/tags/v1.3.8.tar.gz' -OutFile "$tmp\etm.tar.gz"
if (Test-Path "$tmp\etm") { Remove-Item -Recurse -Force "$tmp\etm" }
tar xzf "$tmp\etm.tar.gz" -C $tmp
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path "$Dest\src" | Out-Null
Copy-Item -Recurse "$tmp\esp-tflite-micro-1.3.8\tensorflow" "$Dest\src\"
Copy-Item -Recurse "$tmp\esp-tflite-micro-1.3.8\third_party" "$Dest\src\"
Copy-Item -Recurse "$tmp\esp-tflite-micro-1.3.8\signal" "$Dest\src\"
@'
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
'@ | Set-Content -Encoding UTF8 "$Dest\src\tensorflow\lite\array.h"
@'
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
'@ | Set-Content -Encoding UTF8 "$Dest\library.json"
Write-Output "vendored to $Dest"
