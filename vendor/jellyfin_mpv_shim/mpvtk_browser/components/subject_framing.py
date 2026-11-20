"""Bounded, once-per-artwork framing; never mistake texture for a character.

Face boxes describe visible faces, not a semantic lead-character prediction.
Optional local OpenCV models are used only when already installed. Images with
no reliable evidence retain a neutral centre. No downloads occur here.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import hashlib
import json
import math
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from PIL import Image
_VERSION = 2
_CACHE = OrderedDict()
_LOCK = threading.RLock()
_DETECTOR = None
_DETECTOR_READY = False
_REVIEWED_FILE = Path(_cinema_path('data/reviewed-artwork-framing.json'))

@lru_cache(maxsize=1)
def _reviewed_profiles():
    try:
        data = json.loads(_REVIEWED_FILE.read_text(encoding='utf-8'))
        return data.get('artworks', []) if isinstance(data, dict) else []
    except (OSError, ValueError, TypeError):
        return []

def attach_reviewed_profile(image, item_id, spec):
    """Bind an explicit framing decision to this exact work and image tag."""
    result = None
    if image is not None and spec and (spec[1] == 'Backdrop') and (spec[0] == item_id):
        for record in _reviewed_profiles():
            if not isinstance(record, dict):
                continue
            if record.get('item_id') != item_id or record.get('image_tag') != spec[2] or record.get('image_type') != 'Backdrop' or (record.get('provenance') != 'reviewed_from_user_reference'):
                continue
            try:
                focus = float(record['focal_y'])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(focus) or not 0.0 <= focus <= 1.0:
                continue
            result = {'focal_y': focus, 'subject_box': None, 'method': 'reviewed_from_user_reference', 'item_id': item_id, 'image_tag': spec[2]}
            break
    if image is not None:
        if result is not None:
            image.info['_cinema_reviewed_framing'] = result
        else:
            image.info.pop('_cinema_reviewed_framing', None)
    return result

def _signature(image):
    small = image.convert('RGB').resize((96, 54), Image.Resampling.BILINEAR)
    return (image.width, image.height, hashlib.sha256(small.tobytes()).hexdigest())

def _local_detector():
    global _DETECTOR, _DETECTOR_READY
    if _DETECTOR_READY:
        return _DETECTOR
    _DETECTOR_READY = True
    try:
        import cv2
        import numpy
        anime = Path(__file__).with_name('models') / 'lbpcascade_animeface.xml'
        default = Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'
        classifiers = []
        for model in (anime, default):
            if not model.is_file():
                continue
            classifier = cv2.CascadeClassifier(str(model))
            if not classifier.empty():
                classifiers.append(classifier)
        if classifiers:
            cv2.setNumThreads(1)
            _DETECTOR = (cv2, numpy, classifiers)
    except (ImportError, AttributeError, OSError, RuntimeError):
        pass
    return _DETECTOR

def _face_profile(image):
    detector = _local_detector()
    if detector is None:
        return None
    cv2, numpy, classifiers = detector
    small = image.convert('L')
    small.thumbnail((640, 640), Image.Resampling.BILINEAR)
    gray = cv2.equalizeHist(numpy.asarray(small))
    detected = []
    for classifier in classifiers:
        try:
            faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
            detected.extend((tuple((int(v) for v in face)) for face in faces[:32]))
        except cv2.error:
            continue
    faces = []
    for face in sorted(detected, key=lambda b: b[2] * b[3], reverse=True):
        x, y, width, height = face
        if any((max(0, min(x + width, a + c) - max(x, a)) * max(0, min(y + height, b + d) - max(y, b)) > 0.55 * min(width * height, c * d) for a, b, c, d in faces)):
            continue
        faces.append(face)
    boxes = []
    centres = []
    for x, y, width, height in faces[:32]:
        if width * height < small.width * small.height * 0.002:
            continue
        boxes.append((max(0, x - width * 0.15) / small.width, max(0, y - height * 0.25) / small.height, min(small.width, x + width * 1.15) / small.width, min(small.height, y + height * 1.35) / small.height))
        centres.append(((y + height * 0.55) / small.height, width * height))
    if not boxes:
        return None
    box = (min((b[0] for b in boxes)), min((b[1] for b in boxes)), max((b[2] for b in boxes)), max((b[3] for b in boxes)))
    half = sum((weight for _, weight in centres)) / 2
    accumulated = 0
    focal = 0.5
    for centre, weight in sorted(centres):
        accumulated += weight
        if accumulated >= half:
            focal = centre
            break
    return {'focal_y': focal, 'subject_box': box, 'method': 'local_face_group', 'face_count': len(boxes)}

def subject_profile(image):
    """Return normalized source focal_y, optional subject_box, and evidence.

    Call when preparing immutable artwork, not in a frame callback. A small
    in-memory cache also survives viewport-size changes for the same image.
    """
    neutral = {'focal_y': 0.5, 'subject_box': None, 'method': 'neutral_center'}
    if image is None or image.width <= 0 or image.height <= 0:
        return neutral
    reviewed = image.info.get('_cinema_reviewed_framing')
    if reviewed is not None:
        return dict(reviewed)
    attached = image.info.get('_cinema_subject_profile')
    if attached and attached.get('image_id') == id(image) and (attached.get('version') == _VERSION):
        return dict(attached['profile'])
    key = _signature(image)
    with _LOCK:
        result = _CACHE.get(key)
        if result is None:
            result = _face_profile(image) or neutral
            _CACHE[key] = result
            while len(_CACHE) > 128:
                _CACHE.popitem(last=False)
        else:
            _CACHE.move_to_end(key)
    image.info['_cinema_subject_profile'] = {'image_id': id(image), 'version': _VERSION, 'profile': dict(result)}
    return dict(result)
