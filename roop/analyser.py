from typing import Any
import insightface
import roop.globals

FACE_ANALYSER = None


def get_face_analyser() -> Any:
    global FACE_ANALYSER
    if FACE_ANALYSER is None:
        FACE_ANALYSER = insightface.app.FaceAnalysis(name='buffalo_l', providers=roop.globals.providers)
        FACE_ANALYSER.prepare(ctx_id=0, det_size=(640, 640))
    return FACE_ANALYSER


def get_face_single(image_data) -> Any:
    face = get_face_analyser().get(image_data)
    try:
        return min(face, key=lambda x: x.bbox[0])
    except ValueError:
        return None


def get_face_many(image_data) -> Any:
    try:
        return get_face_analyser().get(image_data)
    except IndexError:
        return None