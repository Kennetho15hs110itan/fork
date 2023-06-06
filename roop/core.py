#!/usr/bin/env python3

import os
import sys
# single thread doubles performance of gpu-mode - needs to be set before torch import
if any(arg.startswith('--gpu-vendor') for arg in sys.argv):
    os.environ['OMP_NUM_THREADS'] = '1'
# reduce tensorflow log level
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
import warnings
from typing import List
import platform
import signal
import shutil
import argparse
import psutil
import torch
import tensorflow
import multiprocessing
from opennsfw2 import predict_video_frames, predict_image
import cv2

import roop.globals
import roop.ui as ui
from roop.swapper import process_video, process_img
from roop.utilities import has_image_extention, is_image, is_video, detect_fps, create_video, extract_frames, get_temp_frames_paths, restore_audio, create_temp, move_temp, clean_temp
from roop.analyser import get_face_single

if 'ROCMExecutionProvider' in roop.globals.providers:
    del torch

warnings.simplefilter(action='ignore', category=FutureWarning)


def parse_args() -> None:
    signal.signal(signal.SIGINT, lambda signal_number, frame: destroy())
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--face', help='use this face', dest='source_path')
    parser.add_argument('-t', '--target', help='replace this face', dest='target_path')
    parser.add_argument('-o', '--output', help='save output to this file', dest='output_path')
    parser.add_argument('--keep-fps', help='maintain original fps', dest='keep_fps', action='store_true', default=False)
    parser.add_argument('--keep-audio', help='maintain original audio', dest='keep_audio', action='store_true', default=True)
    parser.add_argument('--keep-frames', help='keep frames directory', dest='keep_frames', action='store_true', default=False)
    parser.add_argument('--all-faces', help='swap all faces in frame', dest='all_faces', action='store_true', default=False)
    parser.add_argument('--max-memory', help='maximum amount of RAM in GB to be used', dest='max_memory', type=int)
    parser.add_argument('--cpu-cores', help='number of CPU cores to use', dest='cpu_cores', type=int, default=max(psutil.cpu_count() / 2, 1))
    parser.add_argument('--gpu-threads', help='number of threads to be use for the GPU', dest='gpu_threads', type=int, default=8)
    parser.add_argument('--gpu-vendor', help='select your GPU vendor', dest='gpu_vendor', choices=['apple', 'amd', 'intel', 'nvidia'])

    args = parser.parse_known_args()[0]

    roop.globals.source_path = args.source_path
    roop.globals.target_path = args.target_path
    roop.globals.output_path = args.output_path
    roop.globals.headless = args.source_path or args.target_path or args.output_path
    roop.globals.keep_fps = args.keep_fps
    roop.globals.keep_audio = args.keep_audio
    roop.globals.keep_frames = args.keep_frames
    roop.globals.all_faces = args.all_faces

    if args.cpu_cores:
        roop.globals.cpu_cores = int(args.cpu_cores)

    # cpu thread fix for mac
    if sys.platform == 'darwin':
        roop.globals.cpu_cores = 1

    if args.gpu_threads:
        roop.globals.gpu_threads = int(args.gpu_threads)

    # gpu thread fix for amd
    if args.gpu_vendor == 'amd':
        roop.globals.gpu_threads = 1

    if args.gpu_vendor:
        roop.globals.gpu_vendor = args.gpu_vendor
    else:
        roop.globals.providers = ['CPUExecutionProvider']


def limit_resources():
    # prevent tensorflow memory leak
    gpus = tensorflow.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tensorflow.config.experimental.set_memory_growth(gpu, True)
    if roop.globals.max_memory:
        memory = roop.globals.max_memory * 1024 * 1024 * 1024
        if str(platform.system()).lower() == 'windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))


def pre_check():
    if sys.version_info < (3, 9):
        quit('Python version is not supported - please upgrade to 3.9 or higher.')
    if not shutil.which('ffmpeg'):
        quit('ffmpeg is not installed!')
    model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), '../inswapper_128.onnx')
    if not os.path.isfile(model_path):
        quit('File "inswapper_128.onnx" does not exist!')
    if roop.globals.gpu_vendor == 'apple':
        if 'CoreMLExecutionProvider' not in roop.globals.providers:
            quit('You are using --gpu=apple flag but CoreML is not available or properly installed on your system.')
    if roop.globals.gpu_vendor == 'amd':
        if 'ROCMExecutionProvider' not in roop.globals.providers:
            quit('You are using --gpu=amd flag but ROCM is not available or properly installed on your system.')
    if roop.globals.gpu_vendor == 'nvidia':
        if not torch.cuda.is_available():
            quit('You are using --gpu=nvidia flag but CUDA is not available or properly installed on your system.')
        if torch.version.cuda > '11.8':
            quit(f'CUDA version {torch.version.cuda} is not supported - please downgrade to 11.8')
        if torch.version.cuda < '11.4':
            quit(f'CUDA version {torch.version.cuda} is not supported - please upgrade to 11.8')
        if torch.backends.cudnn.version() < 8220:
            quit(f'CUDNN version { torch.backends.cudnn.version()} is not supported - please upgrade to 8.9.1')
        if torch.backends.cudnn.version() > 8910:
            quit(f'CUDNN version { torch.backends.cudnn.version()} is not supported - please downgrade to 8.9.1')


def conditional_process_video(source_path: str, frame_paths: List[str]) -> None:
    pool_amount = len(frame_paths) // roop.globals.cpu_cores
    if pool_amount > 2 and roop.globals.cpu_cores > 1 and roop.globals.gpu_vendor is None:
        update_status('Pool-Swapping in progress...')
        global POOL
        POOL = multiprocessing.Pool(roop.globals.cpu_cores, maxtasksperchild=1)
        pools = []
        for i in range(0, len(frame_paths), pool_amount):
            pool = POOL.apply_async(process_video, args=(source_path, frame_paths[i:i + pool_amount]))
            pools.append(pool)
        for pool in pools:
            pool.get()
        POOL.close()
        POOL.join()
    else:
         update_status('Swapping in progress...')
         process_video(roop.globals.source_path, frame_paths)


def update_status(message: str):
    value = 'Status: ' + message
    print(value)
    if not roop.globals.headless:
        ui.update_status(value)


def start() -> None:
    if not roop.globals.source_path or not os.path.isfile(roop.globals.source_path):
        update_status('Please select an image containing a face.')
        return
    elif not roop.globals.target_path or not os.path.isfile(roop.globals.target_path):
        update_status('Please select a video/image target!')
        return
    test_face = get_face_single(cv2.imread(roop.globals.source_path))
    if not test_face:
        update_status('No face detected in source image. Please try with another one!')
        return
    # process image to image
    if has_image_extention(roop.globals.target_path):
        if predict_image(roop.globals.target_path) > 0.85:
            destroy()
        process_img(roop.globals.source_path, roop.globals.target_path, roop.globals.output_path)
        if is_image(roop.globals.target_path):
            update_status('Swapping to image succeed!')
        else:
            update_status('Swapping to image failed!')
        return
    # process image to videos
    seconds, probabilities = predict_video_frames(video_path=roop.globals.target_path, frame_interval=100)
    if any(probability > 0.85 for probability in probabilities):
        destroy()
    update_status('Creating temp resources...')
    create_temp(roop.globals.target_path)
    update_status('Extracting frames...')
    extract_frames(roop.globals.target_path)
    frame_paths = get_temp_frames_paths(roop.globals.target_path)
    conditional_process_video(roop.globals.source_path, frame_paths)
    # prevent memory leak using ffmpeg with cuda
    if roop.globals.gpu_vendor == 'nvidia':
        torch.cuda.empty_cache()
    if roop.globals.keep_fps:
        update_status('Detecting fps...')
        fps = detect_fps(roop.globals.source_path)
        update_status(f'Creating video with {fps} fps...')
        create_video(roop.globals.target_path, fps)
    else:
        update_status('Creating video with 30 fps...')
        create_video(roop.globals.target_path, 30)
    if roop.globals.keep_audio:
        if roop.globals.keep_fps:
            update_status('Restoring audio...')
        else:
            update_status('Restoring audio might cause issues as fps are not kept...')
        restore_audio(roop.globals.target_path, roop.globals.output_path)
    else:
        move_temp(roop.globals.target_path, roop.globals.output_path)
    clean_temp(roop.globals.target_path)
    if is_video(roop.globals.target_path):
        update_status('Swapping to video succeed!')
    else:
        update_status('Swapping to video failed!')


def destroy() -> None:
    if roop.globals.target_path:
        clean_temp(roop.globals.target_path)
    quit()


def run() -> None:
    parse_args()
    pre_check()
    limit_resources()
    if roop.globals.headless:
        start()
    else:
        window = ui.init(start)
        window.mainloop()