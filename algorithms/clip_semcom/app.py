#!/usr/bin/env python3
"""
clip_semcom/app.py — CLIP semantic communication as a plain uploaded algorithm.

NO import from the PHY framework, no radio code — only transmit()/receive(); a make(role)
binding lets the uniform API read it.

    tx (base station): transmit() = the next image's CLIP embedding (float32)
    rx (user):         receive(embedding) = zero-shot classify
                              transmit() = the predicted class index (the reply)
    tx: receive(label) -> compare to ground truth -> accuracy

Reuses the existing semcom_core library (mock CLIP, no torch weights needed).
Run:  python3 union/run_algo.py --algo clip_semcom --role loopback --steps 45
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "applications", "CLIP_SemCom_Union"))
import semcom_core as core                              # noqa: E402


class CLIP:
    def __init__(self, role, per_class=4):
        self.role = role
        imgs, y, labels = core.synth_dataset(per_class=per_class, texture=0.12)
        clip = core.load_clip("vit-b32", force_mock=True)
        if role == "tx":
            self.embs = np.stack([clip.encode_image(im) for im in imgs])
            self.y, self.i, self.correct = y, 0, 0
            self.spec = ("float32", (clip.dim,))        # transmits embeddings
        else:
            self.ftext = clip.encode_texts(labels)       # RX builds text features locally
            self.last_pred = 0
            self.spec = ("float32", (1,))                # transmits a label reply

    def transmit(self):
        if self.role == "rx":
            return np.array([self.last_pred], np.float32)   # reply = predicted class
        if self.i >= len(self.embs):
            if self.i:
                print(f"    [clip] classification accuracy = {self.correct}/{self.i}")
            return None
        self.cur, self.i = self.i, self.i + 1
        return self.embs[self.cur].astype(np.float32)

    def receive(self, msg):
        m = np.asarray(msg, np.float32)
        if self.role == "rx":
            self.last_pred = core.classify(m, self.ftext)[0]
        else:
            self.correct += int(int(m[0]) == int(self.y[self.cur]))


def make(role):
    return CLIP(role)
