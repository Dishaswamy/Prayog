# cv

A stand-in for a segmentation model. `main.py` thresholds synthetic images that each hold one bright square
and logs the mean `iou` per step, saving a checkpoint each step (the best one marked). It uses a fixed seed, so
a rebuild reproduces it exactly. Replace it with a real model, keeping the same inputs and outputs.

| `--hp` | Default | Meaning |
|---|---|---|
| `size` | 32 | image side, in pixels |
| `steps` | 5 | number of steps |
| `seed` | 0 | random seed |

It needs no dataset.
