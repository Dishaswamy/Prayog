# linear-regression

Fits y = w·x + b to `data/train.csv` (20 points on y = 2x + 1) with plain gradient descent, logging `loss`
and saving a checkpoint each step. It uses no randomness, so a rebuild reproduces it exactly.

| `--hp` | Default | Meaning |
|---|---|---|
| `lr` | 0.05 | learning rate |
| `epochs` | 10 | number of steps |

Supply the dataset with `--data demo/sample-projects/linear-regression/data`. Dataset files are fingerprinted at
upload, not stored with the code.
