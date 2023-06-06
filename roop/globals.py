import onnxruntime

source_path = None
target_path = None
output_path = None
keep_fps = None
keep_audio = None
keep_frames = None
all_faces = None
cpu_cores = None
gpu_threads = None
gpu_vendor = None
max_memory = None
headless = None
log_level = 'error'
providers = onnxruntime.get_available_providers()

if 'TensorrtExecutionProvider' in providers:
    providers.remove('TensorrtExecutionProvider')